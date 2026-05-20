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

"""NVDA-style virtual OCR buffer for Orca.

The user presses Orca+R while focused on any window. Orca captures the
window's pixels, OCRs them, and creates an in-memory virtual buffer
holding the recognized words plus their screen coordinates. No GTK
window is shown.

Once a buffer exists, the user navigates it through Orca-modified keys
that mirror flat-review semantics:

  Orca+Right / Orca+Left   next / previous word
  Orca+Down  / Orca+Up     next / previous line
  Orca+Home  / Orca+End    first / last word
  Orca+Enter / Orca+KP_/   left-click at current word's screen position
  Orca+KP_*                right-click at current word's screen position

The cursor is virtual -- there is no widget on screen tracking it.
Each navigation command speaks the word or line it lands on, the
same way flat-review commands speak the position they land on. Clicks
pass straight through to the source window because no Orca surface
overlaps it. Pressing Orca+R again re-recognizes the focused window
and replaces the buffer. The buffer otherwise persists until the user
restarts Orca.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

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
    from .scripts import default


GROUP_LABEL = "OCR"

# Refuse to feed Tesseract anything larger than this on a side; protects
# the main loop from a multi-second freeze on a fullscreen 4K capture.
_MAX_CAPTURE_DIM = 4096

# Pre-OCR upscale factor. 2x is the sweet spot for typical UI text;
# bump to 3x if recognition rate drops on small-font targets.
_UPSCALE_FACTOR = 2.0


class OCRPresenter(Extension):
    """Holds the active OCR buffer + virtual cursor and routes commands at it."""

    GROUP_LABEL = GROUP_LABEL

    def __init__(self) -> None:
        self._buffer: OCRBuffer | None = None
        self._source_window: Atspi.Accessible | None = None
        # (line_index, word_index_within_line); None when no buffer.
        self._cursor: tuple[int, int] | None = None
        super().__init__()

    # ---- command registration -------------------------------------------

    # pylint: disable-next=too-many-locals
    def _get_commands(self) -> list[Command]:
        def kb(keysym: str, mod: int = keybindings.ORCA_MODIFIER_MASK) -> keybindings.KeyBinding:
            return keybindings.KeyBinding(keysym, mod)

        specs = [
            ("ocrRecognizeHandler", self.recognize_focused_window,
             kb("r"), kb("r"),
             "Recognize the focused window via OCR"),
            ("ocrNextWordHandler", self.go_next_word,
             kb("Right"), kb("Right"),
             "OCR: next word"),
            ("ocrPreviousWordHandler", self.go_previous_word,
             kb("Left"), kb("Left"),
             "OCR: previous word"),
            ("ocrNextLineHandler", self.go_next_line,
             kb("Down"), kb("Down"),
             "OCR: next line"),
            ("ocrPreviousLineHandler", self.go_previous_line,
             kb("Up"), kb("Up"),
             "OCR: previous line"),
            ("ocrFirstWordHandler", self.go_first_word,
             kb("Home"), kb("Home"),
             "OCR: first word"),
            ("ocrLastWordHandler", self.go_last_word,
             kb("End"), kb("End"),
             "OCR: last word"),
            ("ocrLeftClickHandler", self.left_click_current,
             kb("KP_Divide"), kb("KP_Divide"),
             "OCR: left-click at cursor word"),
            ("ocrRightClickHandler", self.right_click_current,
             kb("KP_Multiply"), kb("KP_Multiply"),
             "OCR: right-click at cursor word"),
            ("ocrEnterClickHandler", self.left_click_current,
             kb("Return"), kb("Return"),
             "OCR: left-click at cursor word (alias of NumPad /)"),
        ]
        commands: list[Command] = []
        for name, function, desktop_kb, laptop_kb, description in specs:
            commands.append(
                KeyboardCommand(
                    name,
                    function,
                    self.GROUP_LABEL,
                    description,
                    desktop_keybinding=desktop_kb,
                    laptop_keybinding=laptop_kb,
                ),
            )
        return commands

    # ---- recognize ------------------------------------------------------

    @dbus_service.command
    def recognize_focused_window(
        self,
        script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Capture + OCR the focused window; replace the active buffer."""

        del script, event  # unused in Phase 1/2/3

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

        # SCREEN coords are mandatory: WINDOW coord type returns (0, 0)
        # for a top-level since the top-level is at the origin of its
        # own coordinate space. The previous AXComponent.get_rect path
        # silently broke on libatspi builds that respect that semantics.
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
            self._buffer = None
            self._source_window = None
            self._cursor = None
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR found no readable text."
                )
            return True

        self._buffer = buffer
        self._source_window = window
        self._cursor = (0, 0)
        if notify_user:
            first = self._current_word()
            if first is not None:
                presentation_manager.get_manager().present_message(first.text)
        return True

    # ---- navigation -----------------------------------------------------

    def _current_word(self) -> OCRWord | None:
        if self._buffer is None or self._cursor is None:
            return None
        line_idx, word_idx = self._cursor
        if not (0 <= line_idx < len(self._buffer.lines)):
            return None
        line = self._buffer.lines[line_idx]
        if not (0 <= word_idx < len(line.words)):
            return None
        return line.words[word_idx]

    def _require_buffer(self) -> bool:
        """Speak a hint and return False if no OCR buffer is active."""

        if self._buffer is None or self._cursor is None:
            presentation_manager.get_manager().present_message(
                "No OCR text. Press Orca R to recognize the focused window."
            )
            return False
        return True

    @dbus_service.command
    def go_next_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Advance the OCR cursor to the next word."""

        del script, event
        if not self._require_buffer():
            return True
        assert self._buffer is not None and self._cursor is not None
        line_idx, word_idx = self._cursor
        line = self._buffer.lines[line_idx]
        if word_idx + 1 < len(line.words):
            self._cursor = (line_idx, word_idx + 1)
        elif line_idx + 1 < len(self._buffer.lines):
            self._cursor = (line_idx + 1, 0)
        else:
            if notify_user:
                presentation_manager.get_manager().present_message("End of OCR text.")
            return True
        if notify_user:
            word = self._current_word()
            if word is not None:
                presentation_manager.get_manager().present_message(word.text)
        return True

    @dbus_service.command
    def go_previous_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Move the OCR cursor to the previous word."""

        del script, event
        if not self._require_buffer():
            return True
        assert self._buffer is not None and self._cursor is not None
        line_idx, word_idx = self._cursor
        if word_idx > 0:
            self._cursor = (line_idx, word_idx - 1)
        elif line_idx > 0:
            prev_line = self._buffer.lines[line_idx - 1]
            self._cursor = (line_idx - 1, len(prev_line.words) - 1)
        else:
            if notify_user:
                presentation_manager.get_manager().present_message("Start of OCR text.")
            return True
        if notify_user:
            word = self._current_word()
            if word is not None:
                presentation_manager.get_manager().present_message(word.text)
        return True

    @dbus_service.command
    def go_next_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Advance the OCR cursor to the first word of the next line."""

        del script, event
        if not self._require_buffer():
            return True
        assert self._buffer is not None and self._cursor is not None
        line_idx, _ = self._cursor
        if line_idx + 1 >= len(self._buffer.lines):
            if notify_user:
                presentation_manager.get_manager().present_message("End of OCR text.")
            return True
        self._cursor = (line_idx + 1, 0)
        if notify_user:
            line = self._buffer.lines[self._cursor[0]]
            presentation_manager.get_manager().present_message(line.text)
        return True

    @dbus_service.command
    def go_previous_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Move the OCR cursor to the first word of the previous line."""

        del script, event
        if not self._require_buffer():
            return True
        assert self._buffer is not None and self._cursor is not None
        line_idx, _ = self._cursor
        if line_idx <= 0:
            if notify_user:
                presentation_manager.get_manager().present_message("Start of OCR text.")
            return True
        self._cursor = (line_idx - 1, 0)
        if notify_user:
            line = self._buffer.lines[self._cursor[0]]
            presentation_manager.get_manager().present_message(line.text)
        return True

    @dbus_service.command
    def go_first_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Jump the OCR cursor to the first word of the buffer."""

        del script, event
        if not self._require_buffer():
            return True
        self._cursor = (0, 0)
        if notify_user:
            word = self._current_word()
            if word is not None:
                presentation_manager.get_manager().present_message(word.text)
        return True

    @dbus_service.command
    def go_last_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Jump the OCR cursor to the last word of the buffer."""

        del script, event
        if not self._require_buffer():
            return True
        assert self._buffer is not None
        last_line_idx = len(self._buffer.lines) - 1
        last_word_idx = len(self._buffer.lines[last_line_idx].words) - 1
        self._cursor = (last_line_idx, last_word_idx)
        if notify_user:
            word = self._current_word()
            if word is not None:
                presentation_manager.get_manager().present_message(word.text)
        return True

    # ---- click ----------------------------------------------------------

    @dbus_service.command
    def left_click_current(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Synthesize a left mouse click at the cursor word's screen position."""

        del script, event
        return self._click_current(button="b1c", verb="Click", notify_user=notify_user)

    @dbus_service.command
    def right_click_current(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Synthesize a right mouse click at the cursor word's screen position."""

        del script, event
        return self._click_current(button="b3c", verb="Right-click", notify_user=notify_user)

    def _click_current(self, button: str, verb: str, notify_user: bool) -> bool:
        if not self._require_buffer():
            return True
        word = self._current_word()
        if word is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: cursor is not on a word."
                )
            return True
        if self._source_window is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: source window is unknown."
                )
            return True

        # Re-fetch the source window's SCREEN rect at click time so the
        # click still lands correctly if the window moved since OCR.
        try:
            rect = Atspi.Component.get_extents(
                self._source_window, Atspi.CoordType.SCREEN,
            )
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed at click: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: source window no longer accessible."
                )
            return True

        # Absolute screen center of the word bbox.
        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        # ax_device_manager.generate_mouse_event treats coords as
        # relative to the obj it is given.
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
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR: {verb.lower()} did not reach the window."
                )
            return True
        if notify_user:
            presentation_manager.get_manager().present_message(
                f"{verb}: {word.text}"
            )
        return True

    # ---- introspection (for tests / external D-Bus callers) -------------

    def get_last_buffer(self) -> OCRBuffer | None:
        """Returns the most recently produced buffer, or None."""

        return self._buffer

    def get_cursor(self) -> tuple[int, int] | None:
        """Returns the current (line, word) cursor position, or None."""

        return self._cursor


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
