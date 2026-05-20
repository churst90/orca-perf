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

"""Phase 1 OCR presenter for Orca.

User presses Orca+R while focused on an arbitrary window. The presenter
captures the window's pixel region, runs Tesseract over it, and shows
the recognized lines in a simple GTK dialog. Standard accessible-widget
navigation handles arrow-key reading inside the dialog; Orca speaks
each line as the user moves through the TreeView.

Phase 1 scope: capture + recognize + show. No mouse routing, no
overlay-style cursor, no async pipeline (Tesseract runs synchronously
on the main loop with an audible "Recognizing..." cue beforehand).
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
    clipboard,
    dbus_service,
    debug,
    focus_manager,
    input_event,
    keybindings,
    presentation_manager,
)
from .ax_action import AXAction
from .ax_component import AXComponent
from .ax_object import AXObject
from .command import Command, KeyboardCommand
from .extension import Extension
from . import ocr_capture, ocr_engine
from .ocr_buffer import OCRBuffer, OCRWord

if TYPE_CHECKING:
    from collections.abc import Callable

    from .scripts import default


# Group label exposed to the keybindings preferences dialog. Kept as a
# bare string for Phase 1; promote to guilabels when this graduates from
# perf-branch experiment.
GROUP_LABEL = "OCR"

# Maximum dimension we'll feed to Tesseract before refusing the capture.
# 4096x4096 covers fullscreen on most modern displays; beyond that the
# subprocess can balloon past 10s and timeout.
_MAX_CAPTURE_DIM = 4096

# Upscale factor for the captured pixbuf before OCR. 2.0 is the sweet
# spot for UI text on typical 1080p / 1440p displays per the design
# notes; bump to 3.0 if the user reports poor recognition on a HiDPI
# display where text is already crisp.
_UPSCALE_FACTOR = 2.0


class OCRPresenter(Extension):
    """Captures the focused window and presents its recognized text."""

    GROUP_LABEL = GROUP_LABEL

    def __init__(self) -> None:
        self._last_buffer: OCRBuffer | None = None
        self._dialog: _OCRResultDialog | None = None
        super().__init__()

    def _get_commands(self) -> list[Command]:
        desktop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        laptop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        return [
            KeyboardCommand(
                "recognizeFocusedWindowHandler",
                self.recognize_focused_window,
                self.GROUP_LABEL,
                "Recognize text in the focused window via OCR",
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
        """Capture the focused window, OCR it, and show the recognized text."""

        del event  # unused in Phase 1

        if not ocr_engine.is_available():
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR unavailable: tesseract is not installed."
                )
            return True

        window = focus_manager.get_manager().get_active_window()
        if window is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: no focused window."
                )
            return True

        # Use SCREEN coords explicitly: AXComponent.get_rect uses
        # CoordType.WINDOW which returns (0, 0) for a top-level window
        # (since the window is at the origin of its own coordinate space).
        # We need the actual on-screen position so the captured pixels
        # and the per-word screen coordinates downstream are correct.
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
        x, y, width, height = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
        if width <= 0 or height <= 0:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: focused window has no measurable size."
                )
            return True
        if width > _MAX_CAPTURE_DIM or height > _MAX_CAPTURE_DIM:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR: window is too large to recognize ({width}x{height})."
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
        self._last_buffer = buffer

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
                    "OCR found no readable text in this window."
                )
            return True

        self._show_dialog(script, buffer, window)
        return True

    def get_last_buffer(self) -> OCRBuffer | None:
        """Returns the most recently produced buffer, or None.

        Exposed so external callers can read the same data without
        re-running the pipeline.
        """

        return self._last_buffer

    def _show_dialog(
        self,
        script: default.Script,
        buffer: OCRBuffer,
        source_window: Atspi.Accessible,
    ) -> None:
        if self._dialog is not None:
            self._dialog.destroy()
            self._dialog = None

        self._dialog = _OCRResultDialog(
            script,
            buffer,
            source_window,
            destroyed_callback=self._on_dialog_destroyed,
        )
        self._dialog.show()

    def _on_dialog_destroyed(self, _dialog: Gtk.Dialog) -> None:
        self._dialog = None


class _OCRResultDialog:
    """GTK dialog displaying the recognized text as a read-only TextView.

    The buffer is plain text -- one line per recognized line, words
    separated by single spaces -- with the cursor visible and editing
    disabled. Standard TextView semantics apply: Left/Right move by
    character, Ctrl+Left/Right by word, Up/Down by line, Home/End to
    line ends, Ctrl+Home/End to document ends, Shift+arrow to extend
    the selection, Ctrl+C to copy the selection. Orca reads navigation
    out of the widget the same way it reads any other accessible
    text component.

    Phase 2.6: dialog is non-modal and stays open after click. NumPad /
    (left), NumPad * (right), Enter / NumPad Enter (left) invoke the
    accessibility action on the UI element under the OCR'd word at the
    caret. This is NOT a synthesized mouse event -- it is an AT-SPI
    do_action invocation, which means:
      - The OCR dialog never has to move or close.
      - Z-order and modal grabs don't matter; the action goes straight
        to the target accessible's implementation.
      - The user can click many things in sequence without re-running
        OCR. Escape closes the dialog; Orca+R re-recognizes.
    Trade-off: only works for accessible targets. Truly inaccessible
    apps (Electron, games, etc.) still need an XTest fallback, which
    is the Phase 2.7 follow-up.
    """

    def __init__(
        self,
        script: default.Script,
        buffer: OCRBuffer,
        source_window: Atspi.Accessible,
        destroyed_callback: Callable[[Gtk.Dialog], None],
    ) -> None:
        self._script = script
        self._buffer = buffer
        self._source_window = source_window
        self._view: Gtk.TextView | None = None
        self._text_buffer: Gtk.TextBuffer | None = None
        # (start_offset, end_offset_exclusive, OCRWord) sorted by start.
        self._word_offsets: list[tuple[int, int, OCRWord]] = []
        self._gui = self._build(buffer)
        self._gui.connect("destroy", destroyed_callback)

    def _build(self, buffer: OCRBuffer) -> Gtk.Dialog:
        title_name = buffer.source_window_name or "the focused window"
        title = f"OCR result: {title_name}"
        dialog = Gtk.Dialog(
            title,
            None,
            Gtk.DialogFlags(0),  # NOT modal: clicks via do_action need to
                                 # route to the source window without us
                                 # holding a grab.
            (
                "Copy all", Gtk.ResponseType.APPLY,
                Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE,
            ),
        )
        dialog.set_default_size(700, 450)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        dialog.get_content_area().add(scrolled)

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(True)
        view.set_monospace(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_hexpand(True)
        view.set_vexpand(True)
        view.set_left_margin(6)
        view.set_right_margin(6)
        view.set_top_margin(4)
        view.set_bottom_margin(4)
        scrolled.add(view)  # pylint: disable=no-member

        text_buffer, word_offsets = self._build_text_and_offsets(buffer)
        view.set_buffer(text_buffer)
        self._view = view
        self._text_buffer = text_buffer
        self._word_offsets = word_offsets

        # Give the TextView an accessible name so Orca announces the
        # widget meaningfully when focus lands inside the dialog.
        accessible = view.get_accessible()
        if accessible is not None:
            accessible.set_name("OCR recognized text")

        view.connect("key-press-event", self._on_view_keypress)

        # Land the caret at the very start so Orca speaks the first
        # line immediately when the dialog opens.
        text_buffer.place_cursor(text_buffer.get_start_iter())
        view.grab_focus()

        dialog.connect("response", self._on_response)
        return dialog

    @staticmethod
    def _build_text_and_offsets(
        buffer: OCRBuffer,
    ) -> tuple[Gtk.TextBuffer, list[tuple[int, int, OCRWord]]]:
        """Render the buffer to plain text and remember per-word offsets."""

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

    def _on_response(self, dialog: Gtk.Dialog, response: int) -> None:
        if response == Gtk.ResponseType.APPLY:
            clipboard.get_presenter().set_text(self._buffer.text)
            presentation_manager.get_manager().present_message(
                f"Copied {len(self._buffer.lines)} lines to clipboard."
            )
            return
        dialog.destroy()

    def _on_view_keypress(self, _widget: Gtk.TextView, event: Gdk.EventKey) -> bool:
        """Intercept the mouse-routing keys; everything else (incl. Ctrl+C) propagates."""

        keyval = event.keyval
        if keyval == Gdk.KEY_KP_Divide:
            return self._click_current_word(button="b1c")
        if keyval == Gdk.KEY_KP_Multiply:
            return self._click_current_word(button="b3c")
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return self._click_current_word(button="b1c")
        return False

    def _current_cursor_offset(self) -> int | None:
        if self._text_buffer is None:
            return None
        mark = self._text_buffer.get_insert()
        iter_ = self._text_buffer.get_iter_at_mark(mark)
        return iter_.get_offset()

    def _find_word_at_offset(self, offset: int) -> OCRWord | None:
        """Return the OCRWord whose range contains offset; else the nearest."""

        if not self._word_offsets:
            return None
        # Exact / inclusive containment first. End is exclusive in the
        # table but we include the boundary so a cursor sitting at the
        # last character of a word still picks that word.
        for start, end, word in self._word_offsets:
            if start <= offset <= end:
                return word
        # Cursor is on whitespace, a newline, or past the last word --
        # fall back to the word whose start is closest.
        best = min(self._word_offsets, key=lambda we: abs(we[0] - offset))
        return best[2]

    # Action names we prefer for left- and right-click semantics, in
    # priority order. Most accessibles expose either "click" or an
    # equivalent localized name; we fall back to action index 0 if no
    # named match is found.
    _LEFT_ACTION_NAMES = ("click", "activate", "press", "jump", "open")
    _RIGHT_ACTION_NAMES = ("menu", "popup", "show menu", "contextual menu")

    def _click_current_word(self, button: str) -> bool:
        """Invoke the accessibility action on the UI element under the caret's word.

        Dialog stays open. Returns True so GTK stops propagating the
        keypress (we own it).
        """

        offset = self._current_cursor_offset()
        if offset is None:
            presentation_manager.get_manager().present_message("OCR: no caret position.")
            return True

        word = self._find_word_at_offset(offset)
        if word is None:
            presentation_manager.get_manager().present_message(
                "OCR: no word at the caret position."
            )
            return True

        source_window = self._source_window

        # Re-fetch the source window's SCREEN rect at click time so we
        # cope with windows that have moved since OCR. SCREEN coord
        # type is mandatory here: WINDOW coord type returns (0, 0) for
        # the top-level itself, which would silently break the math.
        try:
            window_rect = Atspi.Component.get_extents(
                source_window, Atspi.CoordType.SCREEN,
            )
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed at click: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            presentation_manager.get_manager().present_message(
                "OCR: source window no longer accessible."
            )
            return True

        # Center of the word bbox, screen coordinates.
        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        # WINDOW-relative coords for get_accessible_at_point().
        rel_x = screen_x - int(window_rect.x)
        rel_y = screen_y - int(window_rect.y)

        target = self._find_leaf_accessible_at(source_window, rel_x, rel_y)
        if target is None:
            presentation_manager.get_manager().present_message(
                f"OCR: no accessible element under {word.text!r}."
            )
            tokens = [
                "OCR PRESENTER: no accessible at window-rel",
                f"({rel_x},{rel_y}) for word {word.text!r};",
                "source window:", AXObject.get_name(source_window),
            ]
            debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)
            return True

        if button == "b3c":
            ok = self._invoke_named_action(target, self._RIGHT_ACTION_NAMES)
            verb = "Right-click"
        else:
            ok = self._invoke_named_action(target, self._LEFT_ACTION_NAMES)
            verb = "Click"

        target_name = AXObject.get_name(target) or AXObject.get_role_name(target) or "element"
        if ok:
            presentation_manager.get_manager().present_message(
                f"{verb}: {target_name}"
            )
        else:
            presentation_manager.get_manager().present_message(
                f"OCR: {verb.lower()} not supported on {target_name}."
            )

        tokens = [
            "OCR PRESENTER:", verb, "on", target,
            f"for word {word.text!r}",
            f"@ screen ({screen_x},{screen_y}) =",
            f"window-rel ({rel_x},{rel_y});",
            "result:", str(ok),
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)
        return True

    @staticmethod
    def _find_leaf_accessible_at(
        root: Atspi.Accessible, x: int, y: int,
    ) -> Atspi.Accessible | None:
        """Descend the accessible tree to the deepest descendant at (x, y).

        Coordinates are interpreted as Atspi.CoordType.WINDOW (constant
        through the descent because WINDOW coords are relative to the
        same top-level for every descendant).
        """

        current = AXComponent.get_object_at_point(root, x, y)
        if current is None:
            return None
        # Recurse until get_object_at_point stops descending. Cap the
        # depth to defend against pathological trees.
        for _ in range(32):
            child = AXComponent.get_object_at_point(current, x, y)
            if child is None or child == current:
                return current
            current = child
        return current

    @staticmethod
    def _invoke_named_action(
        target: Atspi.Accessible, preferred_names: tuple[str, ...],
    ) -> bool:
        """Find an action whose name matches any preferred string, then invoke it.

        Falls back to action index 0 if no name matches and at least one
        action is exposed. Returns True if any action was invoked
        successfully.
        """

        n = AXAction.get_n_actions(target)
        if n <= 0:
            return False
        for i in range(n):
            name = (AXAction.get_action_name(target, i) or "").strip().lower()
            if name in preferred_names:
                return AXAction.do_action(target, i)
        return AXAction.do_action(target, 0)

    def show(self) -> None:
        self._gui.show_all()  # pylint: disable=no-member
        self._gui.present_with_time(time.time())

    def destroy(self) -> None:
        self._gui.destroy()


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
