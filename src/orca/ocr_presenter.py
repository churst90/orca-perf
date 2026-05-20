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

"""Pure-virtual NVDA-style OCR buffer for Orca.

No GTK widget of any kind. Buffer state lives only inside this
presenter; navigation and clicks happen through commands routed via
Orca's existing keyboard-intercept layer (same mechanism flat review
uses for NumPad keys).

User presses Orca+R while focused on any window. Orca captures the
window's pixels, OCRs them, and enters OCR mode. While in mode, plain
NumPad keys navigate the virtual buffer; clicks pass straight through
to the source window because no Orca surface overlaps it. Re-pressing
Orca+R (or pressing NumPad -) exits mode and unsuspends the normal
flat-review bindings.

Keys (while OCR mode is active):

  NumPad 8 / KP_Up         previous line   (speak it)
  NumPad 2 / KP_Down       next line
  NumPad 4 / KP_Left       previous word
  NumPad 6 / KP_Right      next word
  NumPad 7 / KP_Home       first word in buffer
  NumPad 1 / KP_End        last word in buffer
  NumPad 5 / KP_Begin      re-speak current word
  NumPad /                 left-click in source window at cursor word
  NumPad *                 right-click at cursor word
  NumPad Enter             left-click (alias of NumPad /)
  NumPad +                 copy current line to clipboard
  NumPad -                 exit OCR mode
  Orca+R again             exit OCR mode (alternate)

While OCR mode is active, the flat-review commands that normally own
these keys are suspended. They are restored on exit. Other Orca
commands (anything with the Orca modifier, anything bound to non-
NumPad keys) keep working normally.
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


class OCRPresenter(Extension):
    """Owns the OCR mode flag, virtual buffer, and mode-gated commands."""

    GROUP_LABEL = GROUP_LABEL

    # Keys we take over while OCR mode is active. keysym → handler attr.
    # When mode is entered, every other command bound to one of these
    # keysyms (no-modifier) gets suspended; when exited, restored.
    _MODE_KEYS: dict[str, str] = {
        "KP_Up":       "go_previous_line",
        "KP_Down":     "go_next_line",
        "KP_Left":     "go_previous_word",
        "KP_Right":    "go_next_word",
        "KP_Home":     "go_first_word",
        "KP_End":      "go_last_word",
        "KP_Begin":    "speak_current_word",  # NumPad 5
        "KP_Divide":   "left_click_current",
        "KP_Multiply": "right_click_current",
        "KP_Enter":    "left_click_current",
        "KP_Add":      "copy_current_line",
        "KP_Subtract": "exit_ocr_mode",
    }

    def __init__(self) -> None:
        self._buffer: OCRBuffer | None = None
        self._source_window: Atspi.Accessible | None = None
        self._cursor: tuple[int, int] | None = None
        self._mode_active: bool = False
        # Commands we suspended on mode-entry, to be unsuspended on exit.
        self._externally_suspended: list[Command] = []
        super().__init__()

    # ---- command registration ------------------------------------------

    def _get_commands(self) -> list[Command]:
        commands: list[Command] = [
            # Toggle is the only always-active command. It enters OCR
            # mode if inactive, exits if active. Uses Orca+R so it
            # doesn't fight any plain key.
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
        # Mode-gated commands. Suspended-by-default; unsuspended only
        # while OCR mode is active.
        for keysym, attr in self._MODE_KEYS.items():
            handler = getattr(self, attr)
            cmd = KeyboardCommand(
                self._ocr_command_name(attr),
                handler,
                self.GROUP_LABEL,
                f"OCR: {attr.replace('_', ' ')}",
                desktop_keybinding=keybindings.KeyBinding(
                    keysym, keybindings.NO_MODIFIER_MASK,
                ),
                laptop_keybinding=keybindings.KeyBinding(
                    keysym, keybindings.NO_MODIFIER_MASK,
                ),
            )
            cmd.set_suspended(True)
            commands.append(cmd)
        return commands

    @staticmethod
    def _ocr_command_name(handler_attr: str) -> str:
        return f"ocr_{handler_attr}Handler"

    # ---- mode toggle ---------------------------------------------------

    @dbus_service.command
    def toggle_ocr_mode(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Orca+R: enter OCR mode on focused window, or exit if active."""

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
        self._cursor = (0, 0)
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
        """Exit OCR mode, restore the keybindings we suspended."""

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
        if notify_user:
            presentation_manager.get_manager().present_message("OCR mode off.")
        return True

    # ---- mode key activation -------------------------------------------

    def _activate_mode_keys(self) -> None:
        """Suspend other commands on our keys; unsuspend ours."""

        manager = command_manager.get_manager()
        target_keysyms = set(self._MODE_KEYS.keys())
        ocr_command_names = {
            self._ocr_command_name(attr) for attr in self._MODE_KEYS.values()
        }

        self._externally_suspended.clear()
        # Walk every registered keyboard command. Pylint-private access
        # here is deliberate: command_manager doesn't expose an iter
        # API, and we need to identify external commands by binding.
        # pylint: disable-next=protected-access
        for cmd in list(manager._keyboard_commands.values()):
            if cmd.get_name() in ocr_command_names:
                continue
            binding = cmd.get_keybinding()
            if binding is None:
                continue
            if binding.modifiers != keybindings.NO_MODIFIER_MASK:
                continue
            if binding.keysymstring not in target_keysyms:
                continue
            if cmd.is_suspended():
                continue
            cmd.set_suspended(True)
            self._externally_suspended.append(cmd)
            tokens = [
                "OCR PRESENTER: Suspended", cmd.get_name(),
                f"on {binding.keysymstring}",
            ]
            debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        for attr in self._MODE_KEYS.values():
            cmd = manager.get_command(self._ocr_command_name(attr))
            if cmd is not None:
                cmd.set_suspended(False)

    def _deactivate_mode_keys(self) -> None:
        """Re-suspend ours, unsuspend the ones we suspended on entry."""

        manager = command_manager.get_manager()
        for attr in self._MODE_KEYS.values():
            cmd = manager.get_command(self._ocr_command_name(attr))
            if cmd is not None:
                cmd.set_suspended(True)
        for cmd in self._externally_suspended:
            cmd.set_suspended(False)
            tokens = ["OCR PRESENTER: Restored", cmd.get_name()]
            debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)
        self._externally_suspended.clear()

    # ---- cursor helpers ------------------------------------------------

    def _current_word(self) -> OCRWord | None:
        if self._buffer is None or self._cursor is None:
            return None
        li, wi = self._cursor
        if not (0 <= li < len(self._buffer.lines)):
            return None
        line = self._buffer.lines[li]
        if not (0 <= wi < len(line.words)):
            return None
        return line.words[wi]

    # ---- navigation commands -------------------------------------------

    @dbus_service.command
    def go_next_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False  # let other commands handle the key
        li, wi = self._cursor
        line = self._buffer.lines[li]
        if wi + 1 < len(line.words):
            self._cursor = (li, wi + 1)
        elif li + 1 < len(self._buffer.lines):
            self._cursor = (li + 1, 0)
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
        del script, event
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        li, wi = self._cursor
        if wi > 0:
            self._cursor = (li, wi - 1)
        elif li > 0:
            prev_line = self._buffer.lines[li - 1]
            self._cursor = (li - 1, len(prev_line.words) - 1)
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
        del script, event
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        li, _ = self._cursor
        if li + 1 >= len(self._buffer.lines):
            if notify_user:
                presentation_manager.get_manager().present_message("End of OCR text.")
            return True
        self._cursor = (li + 1, 0)
        if notify_user:
            presentation_manager.get_manager().present_message(
                self._buffer.lines[self._cursor[0]].text
            )
        return True

    @dbus_service.command
    def go_previous_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        li, _ = self._cursor
        if li <= 0:
            if notify_user:
                presentation_manager.get_manager().present_message("Start of OCR text.")
            return True
        self._cursor = (li - 1, 0)
        if notify_user:
            presentation_manager.get_manager().present_message(
                self._buffer.lines[self._cursor[0]].text
            )
        return True

    @dbus_service.command
    def go_first_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if not self._mode_active or self._buffer is None:
            return False
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
        del script, event
        if not self._mode_active or self._buffer is None:
            return False
        last_line = len(self._buffer.lines) - 1
        last_word = len(self._buffer.lines[last_line].words) - 1
        self._cursor = (last_line, last_word)
        if notify_user:
            word = self._current_word()
            if word is not None:
                presentation_manager.get_manager().present_message(word.text)
        return True

    @dbus_service.command
    def speak_current_word(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
        if not self._mode_active or self._buffer is None:
            return False
        word = self._current_word()
        if word is not None and notify_user:
            presentation_manager.get_manager().present_message(word.text)
        return True

    # ---- copy ---------------------------------------------------------

    @dbus_service.command
    def copy_current_line(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Copy the OCR text of the current line to the system clipboard."""

        del script, event
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        li, _ = self._cursor
        if not (0 <= li < len(self._buffer.lines)):
            return True
        text = self._buffer.lines[li].text
        clipboard.get_presenter().set_text(text)
        if notify_user:
            presentation_manager.get_manager().present_message(
                f"Copied line: {text}"
            )
        return True

    # ---- click --------------------------------------------------------

    @dbus_service.command
    def left_click_current(
        self, script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        del script, event
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
        if not self._mode_active:
            return False
        return self._click("b3c", "Right-click", notify_user)

    def _click(self, button: str, verb: str, notify_user: bool) -> bool:
        if self._buffer is None or self._cursor is None or self._source_window is None:
            return True
        word = self._current_word()
        if word is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: cursor is not on a word."
                )
            return True

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

        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        rel_x = screen_x - int(rect.x)
        rel_y = screen_y - int(rect.y)

        tokens = [
            "OCR PRESENTER:", verb, f"on {word.text!r}",
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
            presentation_manager.get_manager().present_message(f"{verb}: {word.text}")
        return True

    # ---- introspection -----------------------------------------------

    def is_mode_active(self) -> bool:
        return self._mode_active

    def get_buffer(self) -> OCRBuffer | None:
        return self._buffer

    def get_cursor(self) -> tuple[int, int] | None:
        return self._cursor


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
