# Orca
#
# Copyright 2026 The Orca Team
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., Franklin Street, Fifth Floor,
# Boston MA  02110-1301 USA.

"""NVDA-style transparent OCR buffer for Orca.

User presses Orca+R while focused on any window. Orca captures the
window's pixels, OCRs them, and pops up an *invisible* GTK toplevel
(opacity zero, undecorated, 1x1, off-screen) containing a read-only
GtkTextView populated with the recognized text. The TextView grabs
focus.

To the user, this feels like a document loaded into the screen
reader's review buffer: plain arrow keys navigate by character/line,
Ctrl+Left/Right by word, Shift+arrow selects, Ctrl+C copies, Orca
reads each position as the caret moves. The "transparent" model
means no overlay is on screen, so synthesized mouse clicks pass
straight through to the source window.

  Plain arrows                Move caret (TextView built-in;
                              Orca speaks the new position)
  Ctrl+Left / Ctrl+Right      Word jump (TextView built-in)
  Home / End                  Line edges
  Ctrl+Home / Ctrl+End        Buffer edges
  Shift+anything              Extend selection
  Ctrl+C                      Copy selection
  NumPad /                    Left-click in source window at the
                              screen position of the word at caret
  NumPad *                    Right-click at same position
  Return / NumPad Enter       Left-click (same as NumPad /)
  Escape                      Close the OCR buffer; focus returns
                              to the source window
  Orca+R (again)              Re-recognize the source window and
                              replace the buffer

The source window is captured at OCR time so a second Orca+R while
the OCR buffer has focus re-recognizes the original window, not the
invisible OCR buffer itself.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Atspi, Gdk, GLib, Gtk  # noqa: E402

from . import (
    ax_device_manager,
    dbus_service,
    debug,
    focus_manager,
    input_event,
    keybindings,
    presentation_manager,
)
from .ax_object import AXObject
from .command import Command, KeyboardCommand
from .extension import Extension
from . import ocr_capture, ocr_engine
from .ocr_buffer import OCRBuffer, OCRWord

if TYPE_CHECKING:
    from collections.abc import Callable

    from .scripts import default


GROUP_LABEL = "OCR"

_MAX_CAPTURE_DIM = 4096
_UPSCALE_FACTOR = 2.0


class OCRPresenter(Extension):
    """Owns the Orca+R binding and the currently-open OCR buffer window."""

    GROUP_LABEL = GROUP_LABEL

    def __init__(self) -> None:
        self._window: _OCRBufferWindow | None = None
        # Remembered between recognitions so Orca+R while focus is in
        # our invisible buffer re-recognizes the original source, not
        # the buffer.
        self._last_source_window: Atspi.Accessible | None = None
        super().__init__()

    def _get_commands(self) -> list[Command]:
        desktop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        laptop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        return [
            KeyboardCommand(
                "recognizeFocusedWindowHandler",
                self.recognize_focused_window,
                self.GROUP_LABEL,
                "Recognize the focused window via OCR",
                desktop_keybinding=desktop_kb,
                laptop_keybinding=laptop_kb,
            ),
        ]

    @dbus_service.command
    def recognize_focused_window(
        self,
        script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Capture + OCR + open / replace the transparent buffer window."""

        del script, event

        if not ocr_engine.is_available():
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR unavailable: tesseract is not installed."
                )
            return True

        # If our buffer window currently has focus, re-OCR the
        # remembered source window rather than ourselves.
        window = focus_manager.get_manager().get_active_window()
        if (
            window is not None
            and self._window is not None
            and self._window.owns_accessible(window)
            and self._last_source_window is not None
        ):
            window = self._last_source_window

        if window is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: no focused window."
                )
            return True

        try:
            rect = Atspi.Component.get_extents(window, Atspi.CoordType.SCREEN)
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: cannot determine focused window's screen position."
                )
            return True

        x, y = int(rect.x), int(rect.y)
        width, height = int(rect.width), int(rect.height)
        if width <= 0 or height <= 0:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: focused window has no measurable size."
                )
            return True
        if width > _MAX_CAPTURE_DIM or height > _MAX_CAPTURE_DIM:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR: window is too large ({width} by {height})."
                )
            return True

        window_name = AXObject.get_name(window) or ""

        if notify_user:
            presentation_manager.get_manager().present_message("Recognizing.")

        start = time.time()
        try:
            png = ocr_capture.capture_region(x, y, width, height)
            png = ocr_capture.upscale_png(png, _UPSCALE_FACTOR)
            words = ocr_engine.recognize(
                png,
                capture_x=x,
                capture_y=y,
                upscale_factor=_UPSCALE_FACTOR,
            )
        except ocr_capture.OCRCaptureError as error:
            msg = f"OCR PRESENTER: Capture failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR capture failed: {error}"
                )
            return True
        except ocr_engine.OCREngineError as error:
            msg = f"OCR PRESENTER: Recognition failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR engine failed: {error}"
                )
            return True

        elapsed = time.time() - start
        buffer = OCRBuffer.from_words(
            words,
            capture_x=x,
            capture_y=y,
            capture_width=width,
            capture_height=height,
            source_window_name=window_name,
        )

        tokens = [
            "OCR PRESENTER: Recognized",
            f"{len(words)} words / {len(buffer.lines)} lines",
            f"in {elapsed:.2f}s from",
            f"'{window_name}' ({width}x{height} at +{x}+{y})",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        if buffer.is_empty:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR found no readable text."
                )
            return True

        self._last_source_window = window
        self._open_window(buffer, window)
        return True

    def _open_window(self, buffer: OCRBuffer, source_window: Atspi.Accessible) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None

        self._window = _OCRBufferWindow(
            buffer,
            source_window,
            destroyed_callback=self._on_window_destroyed,
        )
        self._window.show()

    def _on_window_destroyed(self, _widget: Gtk.Widget) -> None:
        self._window = None


class _OCRBufferWindow:
    """Invisible toplevel containing a read-only TextView of the OCR text.

    Opacity zero, undecorated, 1x1, positioned just off-screen. The
    TextView grabs focus on show, so plain arrow keys navigate the
    recognized text and Orca speaks each position. The window itself
    has accessible name "OCR buffer" so the user gets a clean spoken
    cue when focus moves into it.
    """

    def __init__(
        self,
        buffer: OCRBuffer,
        source_window: Atspi.Accessible,
        destroyed_callback: Callable[[Gtk.Widget], None],
    ) -> None:
        self._buffer = buffer
        self._source_window = source_window
        self._text_buffer: Gtk.TextBuffer | None = None
        self._view: Gtk.TextView | None = None
        # (start_offset, end_offset_exclusive, OCRWord) for click routing.
        self._word_offsets: list[tuple[int, int, OCRWord]] = []
        self._win = self._build()
        self._win.connect("destroy", destroyed_callback)

    def _build(self) -> Gtk.Window:
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title(f"OCR: {self._buffer.source_window_name or 'window'}")
        win.set_opacity(0.0)
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_resizable(False)
        win.set_default_size(1, 1)
        # Park it just off-screen so a 1x1 visible footprint never
        # competes with a synthesized click coord on a small display.
        win.move(-2, -2)

        accessible = win.get_accessible()
        if accessible is not None:
            accessible.set_name(
                f"OCR buffer for {self._buffer.source_window_name or 'window'}"
            )
            accessible.set_description(
                "Recognized text. Use arrow keys to navigate, "
                "NumPad slash to click, NumPad star to right click, "
                "Escape to close."
            )

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(0)
        view.set_right_margin(0)
        view.set_top_margin(0)
        view.set_bottom_margin(0)
        win.add(view)

        text_buffer, word_offsets = self._build_text_and_offsets(self._buffer)
        view.set_buffer(text_buffer)
        self._view = view
        self._text_buffer = text_buffer
        self._word_offsets = word_offsets

        view_accessible = view.get_accessible()
        if view_accessible is not None:
            view_accessible.set_name("OCR text")

        view.connect("key-press-event", self._on_view_keypress)
        return win

    @staticmethod
    def _build_text_and_offsets(
        buffer: OCRBuffer,
    ) -> tuple[Gtk.TextBuffer, list[tuple[int, int, OCRWord]]]:
        text_buffer = Gtk.TextBuffer()
        word_offsets: list[tuple[int, int, OCRWord]] = []

        cursor = 0
        parts: list[str] = []
        for line_idx, line in enumerate(buffer.lines):
            for word_idx, word in enumerate(line.words):
                start = cursor
                parts.append(word.text)
                cursor += len(word.text)
                word_offsets.append((start, cursor, word))
                if word_idx < len(line.words) - 1:
                    parts.append(" ")
                    cursor += 1
            if line_idx < len(buffer.lines) - 1:
                parts.append("\n")
                cursor += 1

        text_buffer.set_text("".join(parts))
        return text_buffer, word_offsets

    def owns_accessible(self, accessible: Atspi.Accessible) -> bool:
        """True if the given Atspi accessible is the buffer window's toplevel."""

        if accessible is None:
            return False
        name = AXObject.get_name(accessible) or ""
        return name.startswith("OCR buffer for ")

    def show(self) -> None:
        if self._view is None or self._text_buffer is None:
            return
        self._win.show_all()  # pylint: disable=no-member
        self._win.present_with_time(int(time.time()))
        self._text_buffer.place_cursor(self._text_buffer.get_start_iter())
        self._view.grab_focus()

    def destroy(self) -> None:
        self._win.destroy()

    # ---- click handlers ------------------------------------------------

    def _on_view_keypress(self, _widget: Gtk.TextView, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        if keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        if keyval == Gdk.KEY_KP_Divide:
            self._click(button="b1c", verb="Click")
            return True
        if keyval == Gdk.KEY_KP_Multiply:
            self._click(button="b3c", verb="Right-click")
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._click(button="b1c", verb="Click")
            return True
        return False

    def _current_cursor_offset(self) -> int | None:
        if self._text_buffer is None:
            return None
        mark = self._text_buffer.get_insert()
        return self._text_buffer.get_iter_at_mark(mark).get_offset()

    def _find_word_at_offset(self, offset: int) -> OCRWord | None:
        if not self._word_offsets:
            return None
        for start, end, word in self._word_offsets:
            if start <= offset <= end:
                return word
        # Cursor on whitespace, newline, or past the last word:
        # fall back to the word with the closest start offset.
        best = min(self._word_offsets, key=lambda we: abs(we[0] - offset))
        return best[2]

    def _click(self, button: str, verb: str) -> None:
        offset = self._current_cursor_offset()
        if offset is None:
            presentation_manager.get_manager().present_message(
                "OCR: no caret position."
            )
            return

        word = self._find_word_at_offset(offset)
        if word is None:
            presentation_manager.get_manager().present_message(
                "OCR: no word at the caret position."
            )
            return

        try:
            rect = Atspi.Component.get_extents(
                self._source_window, Atspi.CoordType.SCREEN,
            )
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed at click: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            presentation_manager.get_manager().present_message(
                "OCR: source window no longer accessible."
            )
            return

        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        rel_x = screen_x - int(rect.x)
        rel_y = screen_y - int(rect.y)

        tokens = [
            "OCR PRESENTER:", verb,
            f"on word {word.text!r}",
            f"@ screen ({screen_x},{screen_y}) =",
            f"window-rel ({rel_x},{rel_y});",
            f"window origin ({int(rect.x)},{int(rect.y)})",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        ok = ax_device_manager.get_manager().generate_mouse_event(
            self._source_window, rel_x, rel_y, button,
        )
        if not ok:
            presentation_manager.get_manager().present_message(
                f"OCR: {verb.lower()} did not reach the window."
            )
            return
        presentation_manager.get_manager().present_message(f"{verb}: {word.text}")


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
