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

"""NVDA-style virtual OCR buffer with character cursor and selection.

User presses Orca+R to enter OCR mode on the focused window. While in
mode, NumPad keys navigate the recognized text. The cursor lives at
character granularity: (line_index, word_index, char_index_in_word).

Navigation:

  NumPad 8  / KP_Up         previous line
  NumPad 2  / KP_Down       next line
  NumPad 4  / KP_Left       previous word
  NumPad 6  / KP_Right      next word
  NumPad 1  / KP_End        previous character
  NumPad 3  / KP_Page_Down  next character
  NumPad 7  / KP_Home       first word of buffer
  NumPad 9  / KP_Page_Up    last word of buffer
  NumPad 5  / KP_Begin      re-speak current word

Selection (Shift held while navigating):

  Shift+NumPad 2,4,6,8      extend selection by line/word
  Shift+NumPad 1,3          extend selection by character
  NumPad .  / KP_Decimal    set selection anchor at cursor
  NumPad +  / KP_Add        copy selection to clipboard
                            (falls back to current line if no selection)

Clicks (passed through to source window at the cursor's screen coords):

  NumPad /  / KP_Divide     left-click
  NumPad *  / KP_Multiply   right-click
  NumPad Enter              left-click (alias)

Exit:

  NumPad -  / KP_Subtract   exit OCR mode
  Orca+R again              exit OCR mode

While OCR mode is active, the flat-review commands that normally own
these NumPad bindings are suspended. They are restored on exit.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

from . import (
    ax_device_manager,
    clipboard,
    command_manager,
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

_MAX_CAPTURE_DIM = 4096
_UPSCALE_FACTOR = 2.0

# (line_idx, word_idx, char_idx). char_idx is in [0, len(word.text)).
Position = tuple[int, int, int]


class OCRPresenter(Extension):
    """OCR buffer with character cursor, selection, click pass-through."""

    GROUP_LABEL = GROUP_LABEL

    # (keysym, modifier, handler_attribute) for every key OCR mode owns.
    # Each entry becomes a KeyboardCommand whose name is unique even when
    # two entries share a handler (e.g. KP_Divide and KP_Enter both call
    # left_click_current). Built once at class definition.
    _MODE_KEYS: list[tuple[str, int, str]] = [
        # Plain navigation
        ("KP_Up",        keybindings.NO_MODIFIER_MASK,    "go_previous_line"),
        ("KP_Down",      keybindings.NO_MODIFIER_MASK,    "go_next_line"),
        ("KP_Left",      keybindings.NO_MODIFIER_MASK,    "go_previous_word"),
        ("KP_Right",     keybindings.NO_MODIFIER_MASK,    "go_next_word"),
        ("KP_End",       keybindings.NO_MODIFIER_MASK,    "go_previous_character"),
        ("KP_Page_Down", keybindings.NO_MODIFIER_MASK,    "go_next_character"),
        ("KP_Home",      keybindings.NO_MODIFIER_MASK,    "go_first_word"),
        ("KP_Page_Up",   keybindings.NO_MODIFIER_MASK,    "go_last_word"),
        ("KP_Begin",     keybindings.NO_MODIFIER_MASK,    "speak_current_word"),
        # Selection (Shift + same nav keys)
        ("KP_Up",        keybindings.SHIFT_MODIFIER_MASK, "select_previous_line"),
        ("KP_Down",      keybindings.SHIFT_MODIFIER_MASK, "select_next_line"),
        ("KP_Left",      keybindings.SHIFT_MODIFIER_MASK, "select_previous_word"),
        ("KP_Right",     keybindings.SHIFT_MODIFIER_MASK, "select_next_word"),
        ("KP_End",       keybindings.SHIFT_MODIFIER_MASK, "select_previous_character"),
        ("KP_Page_Down", keybindings.SHIFT_MODIFIER_MASK, "select_next_character"),
        # Anchor / copy
        ("KP_Decimal",   keybindings.NO_MODIFIER_MASK,    "set_anchor"),
        ("KP_Add",       keybindings.NO_MODIFIER_MASK,    "copy_selection_or_line"),
        # Click
        ("KP_Divide",    keybindings.NO_MODIFIER_MASK,    "left_click_current"),
        ("KP_Multiply",  keybindings.NO_MODIFIER_MASK,    "right_click_current"),
        ("KP_Enter",     keybindings.NO_MODIFIER_MASK,    "left_click_current"),
        # Exit
        ("KP_Subtract",  keybindings.NO_MODIFIER_MASK,    "exit_ocr_mode"),
    ]

    def __init__(self) -> None:
        self._buffer: OCRBuffer | None = None
        self._source_window: Atspi.Accessible | None = None
        self._cursor: Position | None = None
        self._anchor: Position | None = None
        self._mode_active: bool = False
        self._externally_suspended: list[Command] = []
        super().__init__()

    # ---- command registration ------------------------------------------

    def _get_commands(self) -> list[Command]:
        commands: list[Command] = [
            KeyboardCommand(
                "ocrToggleHandler",
                self.toggle_ocr_mode,
                self.GROUP_LABEL,
                "Toggle OCR mode on the focused window",
                desktop_keybinding=keybindings.KeyBinding(
                    "r", keybindings.ORCA_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    "r", keybindings.ORCA_MODIFIER_MASK,
                ),
            ),
        ]
        for keysym, mod, attr in self._MODE_KEYS:
            cmd = KeyboardCommand(
                self._ocr_command_name(keysym, mod, attr),
                getattr(self, attr),
                self.GROUP_LABEL,
                f"OCR: {attr.replace('_', ' ')} ({keysym}, mod={mod})",
                desktop_keybinding=keybindings.KeyBinding(keysym, mod),
                laptop_keybinding=keybindings.KeyBinding(keysym, mod),
            )
            cmd.set_suspended(True)
            commands.append(cmd)
        return commands

    @staticmethod
    def _ocr_command_name(keysym: str, mod: int, handler_attr: str) -> str:
        return f"ocr_{keysym}_m{mod}_{handler_attr}Handler"

    # ---- mode toggle ---------------------------------------------------

    @dbus_service.command
    def toggle_ocr_mode(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if self._mode_active:
            return self.exit_ocr_mode(None, None, notify_user)
        return self._enter_ocr_mode(notify_user)

    def _enter_ocr_mode(self, notify_user: bool) -> bool:
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

        try:
            rect = Atspi.Component.get_extents(window, Atspi.CoordType.SCREEN)
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: cannot determine window position."
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
                    f"OCR: window too large ({width} by {height})."
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
                png, capture_x=x, capture_y=y, upscale_factor=_UPSCALE_FACTOR,
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
            words, capture_x=x, capture_y=y,
            capture_width=width, capture_height=height,
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

        self._buffer = buffer
        self._source_window = window
        self._cursor = (0, 0, 0)
        self._anchor = None
        self._activate_mode_keys()
        self._mode_active = True

        if notify_user:
            first = self._current_word()
            first_text = first.text if first is not None else ""
            presentation_manager.get_manager().present_message(
                f"OCR mode on. {len(buffer.lines)} lines. {first_text}"
            )
        return True

    @dbus_service.command
    def exit_ocr_mode(
        self, script: default.Script | None,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if not self._mode_active:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR mode is not active."
                )
            return True
        self._deactivate_mode_keys()
        self._mode_active = False
        self._buffer = None
        self._source_window = None
        self._cursor = None
        self._anchor = None
        if notify_user:
            presentation_manager.get_manager().present_message("OCR mode off.")
        return True

    # ---- mode key activation -------------------------------------------

    def _activate_mode_keys(self) -> None:
        manager = command_manager.get_manager()
        target_pairs = {(k, m) for k, m, _ in self._MODE_KEYS}
        ocr_command_names = {
            self._ocr_command_name(k, m, a) for k, m, a in self._MODE_KEYS
        }

        self._externally_suspended.clear()
        suspended_count = 0
        for cmd in manager.get_all_keyboard_commands():
            if cmd.get_name() in ocr_command_names:
                continue
            binding = cmd.get_keybinding()
            if binding is None:
                continue
            if (binding.keysymstring, binding.modifiers) not in target_pairs:
                continue
            if cmd.is_suspended():
                continue
            cmd.set_suspended(True)
            self._externally_suspended.append(cmd)
            suspended_count += 1
            tokens = [
                "OCR PRESENTER: Suspended", cmd.get_name(),
                f"on {binding.keysymstring} mod={binding.modifiers}",
                f"click={binding.click_count}",
            ]
            debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        unsuspended_count = 0
        for keysym, mod, attr in self._MODE_KEYS:
            cmd = manager.get_command(self._ocr_command_name(keysym, mod, attr))
            if cmd is None:
                tokens = [
                    "OCR PRESENTER: MISSING",
                    self._ocr_command_name(keysym, mod, attr),
                ]
                debug.print_message(debug.LEVEL_WARNING, " ".join(tokens), True)
                continue
            cmd.set_suspended(False)
            unsuspended_count += 1

        tokens = [
            "OCR PRESENTER: _activate_mode_keys done.",
            f"Suspended {suspended_count} external,",
            f"activated {unsuspended_count} OCR.",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

    def _deactivate_mode_keys(self) -> None:
        manager = command_manager.get_manager()
        for keysym, mod, attr in self._MODE_KEYS:
            cmd = manager.get_command(self._ocr_command_name(keysym, mod, attr))
            if cmd is not None:
                cmd.set_suspended(True)
        for cmd in self._externally_suspended:
            cmd.set_suspended(False)
        self._externally_suspended.clear()

    # ---- cursor / selection helpers ------------------------------------

    def _trace(self, handler: str) -> None:
        tokens = [
            "OCR PRESENTER: handler", handler, "called.",
            f"cursor={self._cursor}", f"anchor={self._anchor}",
            f"mode={self._mode_active}",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

    def _current_word(self) -> OCRWord | None:
        if self._buffer is None or self._cursor is None:
            return None
        li, wi, _ = self._cursor
        if not (0 <= li < len(self._buffer.lines)):
            return None
        line = self._buffer.lines[li]
        if not (0 <= wi < len(line.words)):
            return None
        return line.words[wi]

    def _current_char(self) -> str | None:
        word = self._current_word()
        if word is None or self._cursor is None:
            return None
        _, _, ci = self._cursor
        if not (0 <= ci < len(word.text)):
            return None
        return word.text[ci]

    def _clamp_to_word_start(self, li: int, wi: int) -> Position:
        return (li, wi, 0)

    def _last_word_of_line(self, li: int) -> int:
        if self._buffer is None or not (0 <= li < len(self._buffer.lines)):
            return 0
        return max(0, len(self._buffer.lines[li].words) - 1)

    def _last_char_of_word(self, li: int, wi: int) -> int:
        if self._buffer is None:
            return 0
        line = self._buffer.lines[li]
        if not (0 <= wi < len(line.words)):
            return 0
        return max(0, len(line.words[wi].text) - 1)

    # ---- core movement primitives (return True if position changed) ----

    def _move_next_char(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, ci = self._cursor
        word = self._buffer.lines[li].words[wi]
        if ci + 1 < len(word.text):
            self._cursor = (li, wi, ci + 1)
            return True
        # End of word -> first char of next word
        return self._move_next_word()

    def _move_prev_char(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, ci = self._cursor
        if ci > 0:
            self._cursor = (li, wi, ci - 1)
            return True
        # Start of word -> last char of previous word
        if wi > 0:
            new_wi = wi - 1
            self._cursor = (li, new_wi, self._last_char_of_word(li, new_wi))
            return True
        if li > 0:
            new_li = li - 1
            new_wi = self._last_word_of_line(new_li)
            self._cursor = (new_li, new_wi, self._last_char_of_word(new_li, new_wi))
            return True
        return False

    def _move_next_word(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, _ = self._cursor
        line = self._buffer.lines[li]
        if wi + 1 < len(line.words):
            self._cursor = (li, wi + 1, 0)
            return True
        if li + 1 < len(self._buffer.lines):
            self._cursor = (li + 1, 0, 0)
            return True
        return False

    def _move_prev_word(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, _ = self._cursor
        if wi > 0:
            self._cursor = (li, wi - 1, 0)
            return True
        if li > 0:
            new_li = li - 1
            self._cursor = (new_li, self._last_word_of_line(new_li), 0)
            return True
        return False

    def _move_next_line(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, _, _ = self._cursor
        if li + 1 < len(self._buffer.lines):
            self._cursor = (li + 1, 0, 0)
            return True
        return False

    def _move_prev_line(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, _, _ = self._cursor
        if li > 0:
            self._cursor = (li - 1, 0, 0)
            return True
        return False

    # ---- speak helpers --------------------------------------------------

    def _say(self, text: str) -> None:
        presentation_manager.get_manager().present_message(text)

    def _say_current_word(self) -> None:
        w = self._current_word()
        if w is not None:
            self._say(w.text)

    def _say_current_char(self) -> None:
        c = self._current_char()
        if c is not None:
            self._say(c)

    def _say_current_line(self) -> None:
        if self._buffer is None or self._cursor is None:
            return
        li, _, _ = self._cursor
        if 0 <= li < len(self._buffer.lines):
            self._say(self._buffer.lines[li].text)

    # ---- navigation commands (plain; clear anchor) ----------------------

    def _nav_clear_anchor(self) -> None:
        self._anchor = None

    @dbus_service.command
    def go_next_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_next_word")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_word():
            if notify_user:
                self._say("End of OCR text.")
            return True
        if notify_user:
            self._say_current_word()
        return True

    @dbus_service.command
    def go_previous_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_previous_word")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_word():
            if notify_user:
                self._say("Start of OCR text.")
            return True
        if notify_user:
            self._say_current_word()
        return True

    @dbus_service.command
    def go_next_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_next_line")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_line():
            if notify_user:
                self._say("End of OCR text.")
            return True
        if notify_user:
            self._say_current_line()
        return True

    @dbus_service.command
    def go_previous_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_previous_line")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_line():
            if notify_user:
                self._say("Start of OCR text.")
            return True
        if notify_user:
            self._say_current_line()
        return True

    @dbus_service.command
    def go_next_character(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_next_character")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_char():
            if notify_user:
                self._say("End of OCR text.")
            return True
        if notify_user:
            self._say_current_char()
        return True

    @dbus_service.command
    def go_previous_character(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_previous_character")
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_char():
            if notify_user:
                self._say("Start of OCR text.")
            return True
        if notify_user:
            self._say_current_char()
        return True

    @dbus_service.command
    def go_first_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_first_word")
        if not self._mode_active or self._buffer is None:
            return False
        self._nav_clear_anchor()
        self._cursor = (0, 0, 0)
        if notify_user:
            self._say_current_word()
        return True

    @dbus_service.command
    def go_last_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("go_last_word")
        if not self._mode_active or self._buffer is None:
            return False
        self._nav_clear_anchor()
        last_li = len(self._buffer.lines) - 1
        last_wi = self._last_word_of_line(last_li)
        self._cursor = (last_li, last_wi, 0)
        if notify_user:
            self._say_current_word()
        return True

    @dbus_service.command
    def speak_current_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("speak_current_word")
        if not self._mode_active:
            return False
        if notify_user:
            self._say_current_word()
        return True

    # ---- selection commands (Shift + nav; preserve / set anchor) -------

    def _ensure_anchor(self) -> None:
        if self._anchor is None and self._cursor is not None:
            self._anchor = self._cursor

    def _selection_text(self) -> str:
        if self._buffer is None or self._anchor is None or self._cursor is None:
            return ""
        start, end = self._anchor, self._cursor
        if start > end:
            start, end = end, start
        s_l, s_w, s_c = start
        e_l, e_w, e_c = end
        lines_out: list[str] = []
        for li in range(s_l, min(e_l + 1, len(self._buffer.lines))):
            line = self._buffer.lines[li]
            first_word = s_w if li == s_l else 0
            last_word_inclusive = e_w if li == e_l else len(line.words) - 1
            words_out: list[str] = []
            for wi in range(first_word, min(last_word_inclusive + 1, len(line.words))):
                word = line.words[wi]
                first_char = s_c if (li == s_l and wi == s_w) else 0
                last_char_inclusive = (
                    e_c if (li == e_l and wi == e_w) else len(word.text) - 1
                )
                if 0 <= first_char < len(word.text):
                    words_out.append(word.text[first_char:last_char_inclusive + 1])
            lines_out.append(" ".join(words_out))
        return "\n".join(lines_out)

    def _selection_summary(self) -> str:
        text = self._selection_text()
        # Speak a brief summary -- the entire selection can be very long.
        if len(text) <= 80:
            return text
        return f"{text[:60]}... ({len(text)} characters)"

    def _select_move(self, mover, name: str, notify_user: bool) -> bool:
        if not self._mode_active:
            return False
        self._ensure_anchor()
        moved = mover()
        if not moved:
            if notify_user:
                self._say("End of OCR text.")
            return True
        if notify_user:
            self._say(self._selection_summary())
        return True

    @dbus_service.command
    def select_next_character(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_next_character")
        return self._select_move(self._move_next_char, "next_char", notify_user)

    @dbus_service.command
    def select_previous_character(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_previous_character")
        return self._select_move(self._move_prev_char, "prev_char", notify_user)

    @dbus_service.command
    def select_next_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_next_word")
        return self._select_move(self._move_next_word, "next_word", notify_user)

    @dbus_service.command
    def select_previous_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_previous_word")
        return self._select_move(self._move_prev_word, "prev_word", notify_user)

    @dbus_service.command
    def select_next_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_next_line")
        return self._select_move(self._move_next_line, "next_line", notify_user)

    @dbus_service.command
    def select_previous_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("select_previous_line")
        return self._select_move(self._move_prev_line, "prev_line", notify_user)

    @dbus_service.command
    def set_anchor(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Mark the selection anchor at the current cursor position."""

        del script, event
        self._trace("set_anchor")
        if not self._mode_active or self._cursor is None:
            return False
        self._anchor = self._cursor
        if notify_user:
            word = self._current_word()
            text = word.text if word else ""
            self._say(f"Anchor set at {text}.")
        return True

    @dbus_service.command
    def copy_selection_or_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Copy the active selection to the clipboard, or the current line if none."""

        del script, event
        self._trace("copy_selection_or_line")
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        if self._anchor is not None:
            text = self._selection_text()
            label = "selection"
        else:
            li, _, _ = self._cursor
            text = self._buffer.lines[li].text if 0 <= li < len(self._buffer.lines) else ""
            label = "line"
        if not text:
            if notify_user:
                self._say("Nothing to copy.")
            return True
        clipboard.get_presenter().set_text(text)
        if notify_user:
            preview = text if len(text) <= 60 else text[:50] + "..."
            self._say(f"Copied {label}: {preview}")
        return True

    # ---- click handlers ------------------------------------------------

    @dbus_service.command
    def left_click_current(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("left_click_current")
        if not self._mode_active:
            return False
        return self._click("b1c", "Click", notify_user)

    @dbus_service.command
    def right_click_current(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        self._trace("right_click_current")
        if not self._mode_active:
            return False
        return self._click("b3c", "Right-click", notify_user)

    def _click(self, button: str, verb: str, notify_user: bool) -> bool:
        if self._buffer is None or self._cursor is None or self._source_window is None:
            return True
        word = self._current_word()
        if word is None:
            if notify_user:
                self._say("OCR: cursor is not on a word.")
            return True

        try:
            rect = Atspi.Component.get_extents(
                self._source_window, Atspi.CoordType.SCREEN,
            )
        except GLib.GError as error:
            msg = f"OCR PRESENTER: SCREEN get_extents failed at click: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                self._say("OCR: source window no longer accessible.")
            return True

        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        rel_x = screen_x - int(rect.x)
        rel_y = screen_y - int(rect.y)

        tokens = [
            "OCR PRESENTER:", verb, f"on {word.text!r}",
            f"@ screen ({screen_x},{screen_y}) =",
            f"window-rel ({rel_x},{rel_y})",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        ok = ax_device_manager.get_manager().generate_mouse_event(
            self._source_window, rel_x, rel_y, button,
        )
        if not ok:
            if notify_user:
                self._say(f"OCR: {verb.lower()} did not reach the window.")
            return True
        if notify_user:
            self._say(f"{verb}: {word.text}")
        return True

    # ---- introspection -----------------------------------------------

    def is_mode_active(self) -> bool:
        return self._mode_active

    def get_buffer(self) -> OCRBuffer | None:
        return self._buffer

    def get_cursor(self) -> Position | None:
        return self._cursor

    def get_anchor(self) -> Position | None:
        return self._anchor


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
