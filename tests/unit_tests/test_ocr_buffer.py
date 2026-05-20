# Unit tests for ocr_buffer.py methods.
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

"""Unit tests for ocr_buffer.py.

The buffer module is pure-Python with no Orca runtime dependencies,
so these tests can run standalone without the OrcaTestContext
mocking machinery used by other unit tests in this directory.
"""

from __future__ import annotations

import pytest

from orca.ocr_buffer import OCRBuffer, OCRLine, OCRWord


def _word(text: str, x: int, y: int, *, block: int = 0, par: int = 0,
          line: int = 1, w: int = 40, h: int = 12, conf: int = 95) -> OCRWord:
    """Build an OCRWord with sensible defaults for tests."""

    return OCRWord(
        text=text,
        screen_x=x,
        screen_y=y,
        width=w,
        height=h,
        confidence=conf,
        block_num=block,
        par_num=par,
        line_num=line,
    )


@pytest.mark.unit
class TestOCRWord:
    """OCRWord is a frozen dataclass; verify field shape and immutability."""

    def test_fields_round_trip(self) -> None:
        word = _word("hello", 100, 50)
        assert word.text == "hello"
        assert word.screen_x == 100
        assert word.screen_y == 50
        assert word.width == 40
        assert word.height == 12
        assert word.confidence == 95
        assert word.block_num == 0
        assert word.par_num == 0
        assert word.line_num == 1

    def test_is_frozen(self) -> None:
        word = _word("hello", 100, 50)
        with pytest.raises(Exception):  # FrozenInstanceError, but be permissive
            word.text = "world"  # type: ignore[misc]


@pytest.mark.unit
class TestOCRLine:
    """Line aggregates words; text property joins with spaces, bbox sums."""

    def test_text_joins_words_with_spaces(self) -> None:
        line = OCRLine(words=(
            _word("hello", 100, 50),
            _word("world", 145, 50),
        ))
        assert line.text == "hello world"

    def test_single_word_line(self) -> None:
        line = OCRLine(words=(_word("solo", 0, 0),))
        assert line.text == "solo"

    def test_screen_x_is_leftmost(self) -> None:
        line = OCRLine(words=(
            _word("middle", 200, 50),
            _word("left",   100, 50),
            _word("right",  300, 50),
        ))
        assert line.screen_x == 100

    def test_screen_y_is_topmost(self) -> None:
        line = OCRLine(words=(
            _word("a", 100, 50),
            _word("b", 200, 48),  # slightly higher
            _word("c", 300, 52),
        ))
        assert line.screen_y == 48

    def test_screen_width_spans_all_words(self) -> None:
        line = OCRLine(words=(
            _word("a", 100, 50, w=30),   # 100..130
            _word("b", 200, 50, w=40),   # 200..240
        ))
        # leftmost = 100; rightmost edge = 240; width = 140
        assert line.screen_x == 100
        assert line.screen_width == 140

    def test_screen_height_spans_tallest_word(self) -> None:
        line = OCRLine(words=(
            _word("a", 100, 50, h=12),   # 50..62
            _word("b", 200, 48, h=20),   # 48..68
        ))
        # top = 48, bottom = 68, height = 20
        assert line.screen_y == 48
        assert line.screen_height == 20


@pytest.mark.unit
class TestOCRBufferFromWords:
    """OCRBuffer.from_words groups words into lines and sorts deterministically."""

    def test_empty_word_list_yields_empty_buffer(self) -> None:
        buf = OCRBuffer.from_words([], 0, 0, 800, 600, "test")
        assert buf.is_empty
        assert buf.lines == ()
        assert buf.text == ""
        assert buf.capture_x == 0
        assert buf.capture_y == 0
        assert buf.capture_width == 800
        assert buf.capture_height == 600
        assert buf.source_window_name == "test"

    def test_words_with_same_block_par_line_form_one_line(self) -> None:
        buf = OCRBuffer.from_words(
            [_word("hello", 100, 50, line=1),
             _word("world", 145, 50, line=1)],
            0, 0, 800, 600,
        )
        assert len(buf.lines) == 1
        assert buf.lines[0].text == "hello world"

    def test_words_with_different_lines_form_separate_lines(self) -> None:
        buf = OCRBuffer.from_words(
            [_word("hello",  100, 50, line=1),
             _word("second", 100, 70, line=2)],
            0, 0, 800, 600,
        )
        assert len(buf.lines) == 2
        assert buf.lines[0].text == "hello"
        assert buf.lines[1].text == "second"

    def test_lines_sorted_top_to_bottom(self) -> None:
        # Input intentionally out of order; from_words should sort by screen_y.
        buf = OCRBuffer.from_words(
            [_word("bottom", 100, 200, line=3),
             _word("top",    100,  50, line=1),
             _word("middle", 100, 120, line=2)],
            0, 0, 800, 600,
        )
        assert [line.text for line in buf.lines] == ["top", "middle", "bottom"]

    def test_words_within_line_sorted_left_to_right(self) -> None:
        # Words supplied in random order on the same line; should sort by screen_x.
        buf = OCRBuffer.from_words(
            [_word("third",  300, 50, line=1),
             _word("first",  100, 50, line=1),
             _word("second", 200, 50, line=1)],
            0, 0, 800, 600,
        )
        assert len(buf.lines) == 1
        assert buf.lines[0].text == "first second third"

    def test_different_block_par_combinations_form_separate_lines(self) -> None:
        # Same line_num but different block_num -> separate lines.
        buf = OCRBuffer.from_words(
            [_word("a", 100, 50, block=0, par=0, line=1),
             _word("b", 100, 70, block=1, par=0, line=1)],
            0, 0, 800, 600,
        )
        assert len(buf.lines) == 2

    def test_text_property_joins_lines_with_newlines(self) -> None:
        buf = OCRBuffer.from_words(
            [_word("line",   100,  50, line=1),
             _word("one",    150,  50, line=1),
             _word("line",   100,  70, line=2),
             _word("two",    150,  70, line=2)],
            0, 0, 800, 600,
        )
        assert buf.text == "line one\nline two"

    def test_capture_metadata_preserved(self) -> None:
        buf = OCRBuffer.from_words(
            [_word("hello", 100, 50)],
            capture_x=200, capture_y=100,
            capture_width=400, capture_height=300,
            source_window_name="Calculator",
        )
        assert buf.capture_x == 200
        assert buf.capture_y == 100
        assert buf.capture_width == 400
        assert buf.capture_height == 300
        assert buf.source_window_name == "Calculator"

    def test_buffer_with_many_lines_is_not_empty(self) -> None:
        buf = OCRBuffer.from_words(
            [_word(f"w{i}", 100, i*20, line=i) for i in range(1, 10)],
            0, 0, 800, 600,
        )
        assert not buf.is_empty
        assert len(buf.lines) == 9
