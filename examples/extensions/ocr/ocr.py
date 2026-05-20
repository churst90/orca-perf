# Orca OCR extension -- entry point.
#
# Copyright 2026 The Orca Team
# License: LGPL-2.1-or-later

"""OcrExtension -- the user-extension entry point.

Subclasses orca.extension.Extension. Imports its helpers (buffer,
capture, engine) as siblings within the same package.

Press Orca+R to capture the focused window and enter OCR mode.
See the package README for the full key map.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

from orca import debug, keybindings  # noqa: E402
from orca.command import Command, KeyboardCommand  # noqa: E402
from orca.extension import Extension  # noqa: E402

from .buffer import OCRBuffer, OCRWord
from .capture import capture_region_async, upscale_png
from .engine import OCREngineError, is_available, recognize_async

if TYPE_CHECKING:
    pass


# Cursor: (line_index, word_index, char_index).
_Position = tuple[int, int, int]


_OCR_LANG = "eng"
_OCR_UPSCALE = 2.0
_OCR_CONFIDENCE = 30
_MAX_CAPTURE_DIM = 4096


class OcrExtension(Extension):
    """OCR user extension. See package README for usage."""

    GROUP_LABEL = "OCR"

    _MODE_KEYS: list[tuple[str, int, str]] = [
        ("KP_Up",        keybindings.NO_MODIFIER_MASK,    "go_previous_line"),
        ("KP_Down",      keybindings.NO_MODIFIER_MASK,    "go_next_line"),
        ("KP_Left",      keybindings.NO_MODIFIER_MASK,    "go_previous_word"),
        ("KP_Right",     keybindings.NO_MODIFIER_MASK,    "go_next_word"),
        ("KP_End",       keybindings.NO_MODIFIER_MASK,    "go_previous_character"),
        ("KP_Page_Down", keybindings.NO_MODIFIER_MASK,    "go_next_character"),
        ("KP_Home",      keybindings.NO_MODIFIER_MASK,    "go_first_word"),
        ("KP_Page_Up",   keybindings.NO_MODIFIER_MASK,    "go_last_word"),
        ("KP_Begin",     keybindings.NO_MODIFIER_MASK,    "speak_current_word"),
        ("KP_Up",        keybindings.SHIFT_MODIFIER_MASK, "select_previous_line"),
        ("KP_Down",      keybindings.SHIFT_MODIFIER_MASK, "select_next_line"),
        ("KP_Left",      keybindings.SHIFT_MODIFIER_MASK, "select_previous_word"),
        ("KP_Right",     keybindings.SHIFT_MODIFIER_MASK, "select_next_word"),
        ("KP_End",       keybindings.SHIFT_MODIFIER_MASK, "select_previous_character"),
        ("KP_Page_Down", keybindings.SHIFT_MODIFIER_MASK, "select_next_character"),
        ("KP_Decimal",   keybindings.NO_MODIFIER_MASK,    "set_anchor"),
        ("KP_Add",       keybindings.NO_MODIFIER_MASK,    "copy_selection_or_line"),
        ("KP_Divide",    keybindings.NO_MODIFIER_MASK,    "left_click_current"),
        ("KP_Multiply",  keybindings.NO_MODIFIER_MASK,    "right_click_current"),
        ("KP_Enter",     keybindings.NO_MODIFIER_MASK,    "left_click_current"),
        ("KP_Subtract",  keybindings.NO_MODIFIER_MASK,    "exit_ocr_mode"),
    ]

    def __init__(self) -> None:
        self._buffer: OCRBuffer | None = None
        self._source_window: Atspi.Accessible | None = None
        self._cursor: _Position | None = None
        self._anchor: _Position | None = None
        self._mode_active: bool = False
        self._pending_pid: int | None = None
        self._pending_context: dict | None = None
        super().__init__()

    # ---- command registration --------------------------------------

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
    def _ocr_command_name(keysym: str, mod: int, attr: str) -> str:
        return f"ocr_{keysym}_m{mod}_{attr}Handler"

    # ---- mode toggle / enter / exit -------------------------------

    def _say(self, text: str) -> None:
        self.controller.present_message_internal(text)

    def toggle_ocr_mode(self) -> bool:
        if self._mode_active:
            return self.exit_ocr_mode()
        return self._enter_ocr_mode()

    def _enter_ocr_mode(self) -> bool:
        if not is_available():
            self._say("OCR unavailable: tesseract is not installed.")
            return True
        window = self.controller.get_active_window()
        if window is None:
            self._say("OCR: no focused window.")
            return True
        screen_rect = self.controller.get_active_window_screen_rect()
        if screen_rect is None:
            self._say("OCR: cannot determine window position.")
            return True
        x, y, width, height = screen_rect
        if width <= 0 or height <= 0:
            self._say("OCR: focused window has no measurable size.")
            return True
        if width > _MAX_CAPTURE_DIM or height > _MAX_CAPTURE_DIM:
            self._say(f"OCR: window too large ({width} by {height}).")
            return True

        window_name = ""
        try:
            window_name = Atspi.Accessible.get_name(window) or ""
        except GLib.Error:
            pass

        if self._pending_context is not None:
            self._say("OCR is still recognizing the previous window. Please wait.")
            return True

        self._say("Recognizing.")
        self._pending_context = {
            "window": window, "window_name": window_name,
            "x": x, "y": y, "width": width, "height": height,
            "start": time.time(),
        }
        capture_region_async(x, y, width, height, self._on_capture_done)
        return True

    def _on_capture_done(
        self, png_bytes: bytes | None, error: str | None,
    ) -> None:
        ctx = self._pending_context
        if ctx is None:
            return
        if error is not None or png_bytes is None:
            self._pending_context = None
            self._pending_pid = None
            self._say(f"OCR capture failed: {error or 'unknown'}")
            return
        png_bytes = upscale_png(png_bytes, _OCR_UPSCALE)
        pid = recognize_async(
            png_bytes, capture_x=ctx["x"], capture_y=ctx["y"],
            upscale_factor=_OCR_UPSCALE, lang=_OCR_LANG,
            min_confidence=_OCR_CONFIDENCE,
            on_done=self._on_recognize_done,
        )
        if pid is None:
            self._pending_context = None
            self._pending_pid = None
            return
        self._pending_pid = pid

    def _on_recognize_done(
        self, words: list[OCRWord] | None,
        error: OCREngineError | None,
    ) -> None:
        ctx = self._pending_context
        self._pending_context = None
        self._pending_pid = None
        if ctx is None:
            return
        if error is not None:
            self._say(f"OCR engine failed: {error}")
            return
        word_list = words or []
        buffer = OCRBuffer.from_words(
            word_list, capture_x=ctx["x"], capture_y=ctx["y"],
            capture_width=ctx["width"], capture_height=ctx["height"],
            source_window_name=ctx["window_name"],
        )
        if buffer.is_empty:
            self._say("OCR found no readable text.")
            return
        self._buffer = buffer
        self._source_window = ctx["window"]
        self._cursor = (0, 0, 0)
        self._anchor = None
        self._activate_mode_keys()
        self._mode_active = True
        first = self._current_word()
        first_text = first.text if first is not None else ""
        self._say(f"OCR mode on. {len(buffer.lines)} lines. {first_text}")

    def exit_ocr_mode(self) -> bool:
        if not self._mode_active:
            self._say("OCR mode is not active.")
            return True
        self._deactivate_mode_keys()
        self._mode_active = False
        self._buffer = None
        self._source_window = None
        self._cursor = None
        self._anchor = None
        self._say("OCR mode off.")
        return True

    # ---- modal key activation -- via controller --------------------

    def _activate_mode_keys(self) -> None:
        target_pairs = [(k, m) for k, m, _ in self._MODE_KEYS]
        self.controller.enter_modal_mode(self, target_pairs)

    def _deactivate_mode_keys(self) -> None:
        self.controller.exit_modal_mode(self)

    # ---- cursor helpers --------------------------------------------

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
        return word.text[ci] if 0 <= ci < len(word.text) else None

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

    def _move_next_char(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, ci = self._cursor
        word = self._buffer.lines[li].words[wi]
        if ci + 1 < len(word.text):
            self._cursor = (li, wi, ci + 1)
            return True
        return self._move_next_word()

    def _move_prev_char(self) -> bool:
        if self._buffer is None or self._cursor is None:
            return False
        li, wi, ci = self._cursor
        if ci > 0:
            self._cursor = (li, wi, ci - 1)
            return True
        if wi > 0:
            new_wi = wi - 1
            self._cursor = (li, new_wi, self._last_char_of_word(li, new_wi))
            return True
        if li > 0:
            new_li = li - 1
            new_wi = self._last_word_of_line(new_li)
            self._cursor = (new_li, new_wi,
                            self._last_char_of_word(new_li, new_wi))
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

    # ---- nav commands ----------------------------------------------

    def _nav_clear_anchor(self) -> None:
        self._anchor = None

    def go_next_word(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_word():
            self._say("End of OCR text.")
            return True
        word = self._current_word()
        if word is not None:
            self._say(word.text)
        return True

    def go_previous_word(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_word():
            self._say("Start of OCR text.")
            return True
        word = self._current_word()
        if word is not None:
            self._say(word.text)
        return True

    def go_next_line(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_line():
            self._say("End of OCR text.")
            return True
        assert self._buffer is not None and self._cursor is not None
        self._say(self._buffer.lines[self._cursor[0]].text)
        return True

    def go_previous_line(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_line():
            self._say("Start of OCR text.")
            return True
        assert self._buffer is not None and self._cursor is not None
        self._say(self._buffer.lines[self._cursor[0]].text)
        return True

    def go_next_character(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_next_char():
            self._say("End of OCR text.")
            return True
        char = self._current_char()
        if char is not None:
            self._say(char)
        return True

    def go_previous_character(self) -> bool:
        if not self._mode_active:
            return False
        self._nav_clear_anchor()
        if not self._move_prev_char():
            self._say("Start of OCR text.")
            return True
        char = self._current_char()
        if char is not None:
            self._say(char)
        return True

    def go_first_word(self) -> bool:
        if not self._mode_active or self._buffer is None:
            return False
        self._nav_clear_anchor()
        self._cursor = (0, 0, 0)
        word = self._current_word()
        if word is not None:
            self._say(word.text)
        return True

    def go_last_word(self) -> bool:
        if not self._mode_active or self._buffer is None:
            return False
        self._nav_clear_anchor()
        last_li = len(self._buffer.lines) - 1
        self._cursor = (last_li, self._last_word_of_line(last_li), 0)
        word = self._current_word()
        if word is not None:
            self._say(word.text)
        return True

    def speak_current_word(self) -> bool:
        if not self._mode_active:
            return False
        word = self._current_word()
        if word is not None:
            self._say(word.text)
        return True

    # ---- selection -------------------------------------------------

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
            last_word = e_w if li == e_l else len(line.words) - 1
            words_out: list[str] = []
            for wi in range(first_word, min(last_word + 1, len(line.words))):
                word = line.words[wi]
                first_char = s_c if (li == s_l and wi == s_w) else 0
                last_char = (e_c if (li == e_l and wi == e_w)
                             else len(word.text) - 1)
                if 0 <= first_char < len(word.text):
                    words_out.append(word.text[first_char:last_char + 1])
            lines_out.append(" ".join(words_out))
        return "\n".join(lines_out)

    def _select_move(self, mover) -> bool:
        if not self._mode_active:
            return False
        self._ensure_anchor()
        if not mover():
            self._say("End of OCR text.")
            return True
        text = self._selection_text()
        preview = text if len(text) <= 60 else text[:50] + "..."
        self._say(preview)
        return True

    def select_next_character(self) -> bool:
        return self._select_move(self._move_next_char)

    def select_previous_character(self) -> bool:
        return self._select_move(self._move_prev_char)

    def select_next_word(self) -> bool:
        return self._select_move(self._move_next_word)

    def select_previous_word(self) -> bool:
        return self._select_move(self._move_prev_word)

    def select_next_line(self) -> bool:
        return self._select_move(self._move_next_line)

    def select_previous_line(self) -> bool:
        return self._select_move(self._move_prev_line)

    def set_anchor(self) -> bool:
        if not self._mode_active or self._cursor is None:
            return False
        self._anchor = self._cursor
        word = self._current_word()
        text = word.text if word else ""
        self._say(f"Anchor set at {text}.")
        return True

    def copy_selection_or_line(self) -> bool:
        if not self._mode_active or self._buffer is None or self._cursor is None:
            return False
        if self._anchor is not None:
            text = self._selection_text()
            label = "selection"
        else:
            li, _, _ = self._cursor
            text = (self._buffer.lines[li].text
                    if 0 <= li < len(self._buffer.lines) else "")
            label = "line"
        if not text:
            self._say("Nothing to copy.")
            return True
        self.controller.set_clipboard_text(text)
        preview = text if len(text) <= 60 else text[:50] + "..."
        self._say(f"Copied {label}: {preview}")
        return True

    # ---- click pass-through ----------------------------------------

    def left_click_current(self) -> bool:
        if not self._mode_active:
            return False
        return self._click("left", "Click")

    def right_click_current(self) -> bool:
        if not self._mode_active:
            return False
        return self._click("right", "Right-click")

    def _click(self, button: str, verb: str) -> bool:
        if self._buffer is None or self._cursor is None or self._source_window is None:
            return True
        word = self._current_word()
        if word is None:
            self._say("OCR: cursor is not on a word.")
            return True
        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        ok = self.controller.synthesize_mouse_event(
            screen_x, screen_y, button,
        )
        if not ok:
            self._say(f"OCR: {verb.lower()} did not reach the window.")
            return True
        self._say(f"{verb}: {word.text}")
        return True
