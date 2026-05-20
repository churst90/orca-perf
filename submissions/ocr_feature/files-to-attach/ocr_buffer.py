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

"""Data structures for OCR-recognized content.

An OCRBuffer is the result of running the OCR pipeline against a single
captured region of the screen. It owns the recognized words, groups them
into lines, and preserves enough geometry that a later mouse-routing
phase can click on the original on-screen position of any word.

Phase 1 only uses .lines for read-out. The screen-coord fields are
populated and preserved for Phase 2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OCRWord:
    """A single word recognized by the OCR engine.

    Coordinates are absolute screen coordinates -- the engine emits image-
    local bbox, and OCRBuffer.from_words translates them by the capture
    origin before storing. This lets the mouse-routing layer click at
    (screen_x + width // 2, screen_y + height // 2) directly.
    """

    text: str
    screen_x: int
    screen_y: int
    width: int
    height: int
    confidence: int
    # Original tesseract grouping keys -- used by line grouping.
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
        """Build a buffer by grouping words into lines.

        Words from the same tesseract (block, par, line) triple belong to
        the same line. Within a line, words are sorted left-to-right by
        screen_x. Lines themselves are sorted top-to-bottom by screen_y.
        """

        if not words:
            return cls(
                lines=(),
                capture_x=capture_x,
                capture_y=capture_y,
                capture_width=capture_width,
                capture_height=capture_height,
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
            lines=tuple(lines),
            capture_x=capture_x,
            capture_y=capture_y,
            capture_width=capture_width,
            capture_height=capture_height,
            source_window_name=source_window_name,
        )
