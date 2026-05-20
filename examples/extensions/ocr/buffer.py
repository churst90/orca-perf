# Orca OCR extension -- recognized-text data structures.
#
# Copyright 2026 The Orca Team
# License: LGPL-2.1-or-later

"""OCRWord / OCRLine / OCRBuffer dataclasses.

Pure-Python with no Orca runtime deps. The buffer preserves
per-word absolute screen coordinates so the click-routing layer
can hit the original pixel without re-running OCR.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OCRWord:
    """A single word recognized by Tesseract, with screen coordinates."""

    text: str
    screen_x: int
    screen_y: int
    width: int
    height: int
    confidence: int
    block_num: int = 0
    par_num: int = 0
    line_num: int = 0


@dataclass(frozen=True)
class OCRLine:
    """A line of recognized text. Words are in left-to-right order."""

    words: tuple[OCRWord, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def screen_y(self) -> int:
        return min(w.screen_y for w in self.words)

    @property
    def screen_height(self) -> int:
        bottom = max(w.screen_y + w.height for w in self.words)
        return bottom - self.screen_y

    @property
    def screen_x(self) -> int:
        return min(w.screen_x for w in self.words)

    @property
    def screen_width(self) -> int:
        right = max(w.screen_x + w.width for w in self.words)
        return right - self.screen_x


@dataclass
class OCRBuffer:
    """The output of one capture-and-recognize cycle."""

    lines: tuple[OCRLine, ...]
    capture_x: int
    capture_y: int
    capture_width: int
    capture_height: int
    source_window_name: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @classmethod
    def from_words(
        cls,
        words: list[OCRWord],
        capture_x: int,
        capture_y: int,
        capture_width: int,
        capture_height: int,
        source_window_name: str = "",
    ) -> OCRBuffer:
        if not words:
            return cls(
                lines=(), capture_x=capture_x, capture_y=capture_y,
                capture_width=capture_width, capture_height=capture_height,
                source_window_name=source_window_name,
            )
        groups: dict[tuple[int, int, int], list[OCRWord]] = {}
        for word in words:
            key = (word.block_num, word.par_num, word.line_num)
            groups.setdefault(key, []).append(word)
        lines: list[OCRLine] = []
        for line_words in groups.values():
            line_words.sort(key=lambda w: w.screen_x)
            lines.append(OCRLine(words=tuple(line_words)))
        lines.sort(key=lambda line: line.screen_y)
        return cls(
            lines=tuple(lines), capture_x=capture_x, capture_y=capture_y,
            capture_width=capture_width, capture_height=capture_height,
            source_window_name=source_window_name,
        )
