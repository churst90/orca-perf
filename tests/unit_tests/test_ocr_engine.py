# Unit tests for ocr_engine.py methods.
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

"""Unit tests for ocr_engine.py.

Focus on the TSV parser (pure-logic, no subprocess required) and the
coord-translation arithmetic. `recognize()` itself is a subprocess
wrapper that shells out to tesseract; we don't reach the subprocess
layer in these tests.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from orca.ocr_engine import _parse_tsv, is_available


# Header row Tesseract emits for `tesseract <image> - tsv`. Columns are
# tab-separated. The order matches Tesseract 4.x and 5.x output.
_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
    "left\ttop\twidth\theight\tconf\ttext"
)


def _row(level: int = 5, block: int = 0, par: int = 0, line: int = 1,
         word: int = 1, left: int = 100, top: int = 50,
         width: int = 40, height: int = 12, conf: int = 95,
         text: str = "hello") -> str:
    """Build a TSV row matching _HEADER's column order."""

    return "\t".join(str(x) for x in (
        level, 1, block, par, line, word,
        left, top, width, height, conf, text,
    ))


def _tsv(*rows: str) -> str:
    return "\n".join((_HEADER, *rows))


@pytest.mark.unit
class TestParseTSV:
    """The TSV parser filters non-word rows, applies confidence + coord math."""

    def test_empty_input_yields_no_words(self) -> None:
        assert _parse_tsv("", capture_x=0, capture_y=0, upscale_factor=1.0) == []

    def test_header_only_yields_no_words(self) -> None:
        assert _parse_tsv(_HEADER, capture_x=0, capture_y=0,
                          upscale_factor=1.0) == []

    def test_single_high_confidence_word_is_returned(self) -> None:
        words = _parse_tsv(
            _tsv(_row(text="hello", conf=95, left=100, top=50)),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert len(words) == 1
        assert words[0].text == "hello"
        assert words[0].confidence == 95

    def test_words_below_confidence_threshold_dropped(self) -> None:
        # Default threshold in ocr_engine is 30; conf=20 should be filtered.
        words = _parse_tsv(
            _tsv(_row(text="garbage", conf=20)),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert words == []

    def test_confidence_minus_one_dropped(self) -> None:
        # Tesseract uses -1 for "could not classify"; must be filtered.
        words = _parse_tsv(
            _tsv(_row(text="??", conf=-1)),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert words == []

    def test_empty_text_word_dropped(self) -> None:
        words = _parse_tsv(
            _tsv(_row(text="", conf=95)),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert words == []

    def test_whitespace_only_text_dropped(self) -> None:
        words = _parse_tsv(
            _tsv(_row(text="   ", conf=95)),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert words == []

    def test_non_word_level_rows_skipped(self) -> None:
        # Tesseract emits level=1..5 rows (page/block/par/line/word).
        # Only level 5 (word) should appear in the output.
        words = _parse_tsv(
            _tsv(
                _row(level=1, text="page"),
                _row(level=2, text="block"),
                _row(level=3, text="par"),
                _row(level=4, text="line"),
                _row(level=5, text="actual_word", conf=95),
            ),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert [w.text for w in words] == ["actual_word"]

    def test_capture_origin_added_to_coords(self) -> None:
        words = _parse_tsv(
            _tsv(_row(left=100, top=50, conf=95)),
            capture_x=200, capture_y=300, upscale_factor=1.0,
        )
        assert len(words) == 1
        # screen_x = capture_x + int(left / scale) = 200 + 100 = 300
        # screen_y = capture_y + int(top  / scale) = 300 +  50 = 350
        assert words[0].screen_x == 300
        assert words[0].screen_y == 350

    def test_upscale_factor_divides_image_coords(self) -> None:
        # If the image was 2x upscaled before OCR, Tesseract sees coords in
        # the upscaled space. We must divide by 2 to recover the original
        # screen coordinates.
        words = _parse_tsv(
            _tsv(_row(left=200, top=100, width=80, height=24, conf=95)),
            capture_x=0, capture_y=0, upscale_factor=2.0,
        )
        assert words[0].screen_x == 100
        assert words[0].screen_y == 50
        assert words[0].width == 40
        assert words[0].height == 12

    def test_upscale_factor_combined_with_capture_origin(self) -> None:
        # Both transformations apply: divide by scale, then add origin.
        words = _parse_tsv(
            _tsv(_row(left=600, top=400, conf=95)),
            capture_x=50, capture_y=25, upscale_factor=2.0,
        )
        # int(600/2) + 50 = 350; int(400/2) + 25 = 225
        assert words[0].screen_x == 350
        assert words[0].screen_y == 225

    def test_block_par_line_metadata_preserved(self) -> None:
        words = _parse_tsv(
            _tsv(_row(block=2, par=3, line=5, conf=95, text="word")),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert words[0].block_num == 2
        assert words[0].par_num == 3
        assert words[0].line_num == 5

    def test_invalid_numeric_fields_skip_row_not_crash(self) -> None:
        # A malformed row (non-integer in a numeric field) should be
        # silently skipped, not raise.
        bad_row = (
            "5\t1\t0\t0\t1\t1\tNOT_AN_INT\t50\t40\t12\t95\thello"
        )
        good_row = _row(text="hello", conf=95)
        words = _parse_tsv(
            _tsv(bad_row, good_row),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert len(words) == 1
        assert words[0].text == "hello"

    def test_short_row_skipped_not_crash(self) -> None:
        # A truncated row that doesn't have all 12 columns should be
        # silently skipped.
        short = "5\t1\t0\t0\t1\t1"  # only 6 of 12 columns
        good = _row(text="hello", conf=95)
        words = _parse_tsv(
            _tsv(short, good),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert len(words) == 1
        assert words[0].text == "hello"

    def test_multiple_words_returned_in_input_order(self) -> None:
        # The parser does not sort; OCRBuffer.from_words handles sorting.
        words = _parse_tsv(
            _tsv(
                _row(text="first",  word=1, left=100, conf=95),
                _row(text="second", word=2, left=200, conf=90),
                _row(text="third",  word=3, left=300, conf=85),
            ),
            capture_x=0, capture_y=0, upscale_factor=1.0,
        )
        assert [w.text for w in words] == ["first", "second", "third"]


@pytest.mark.unit
class TestIsAvailable:
    """is_available() returns True iff tesseract is on $PATH."""

    def test_returns_true_when_tesseract_on_path(self) -> None:
        with patch("orca.ocr_engine.shutil.which",
                   return_value="/usr/bin/tesseract"):
            assert is_available() is True

    def test_returns_false_when_tesseract_missing(self) -> None:
        with patch("orca.ocr_engine.shutil.which", return_value=None):
            assert is_available() is False
