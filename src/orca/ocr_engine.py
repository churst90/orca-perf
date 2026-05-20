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

"""Tesseract subprocess wrapper for the OCR pipeline.

Runs `tesseract <image> - -l <lang> tsv` and parses the TSV output into
OCRWord objects. The TSV format gives one row per recognized item; we
keep only word-level rows (level=5) with non-empty text and confidence
above a quality threshold.

The subprocess takes the bulk of the end-to-end OCR latency (typically
300-2000ms depending on the input area), so callers should run this
off the GLib main thread or display a progress cue.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import debug
from .ocr_buffer import OCRWord


class OCREngineError(Exception):
    """Raised when tesseract is unavailable or fails to produce parseable output."""


# Tesseract TSV row level for individual words. Lower levels are page/
# block/paragraph/line aggregates we don't need.
_WORD_LEVEL = 5

# Words below this confidence are dropped. Tesseract reports confidence
# as 0-100 (or -1 for items it couldn't classify). 30 is the threshold
# below which words are usually garbage; tuned empirically by NVDA and
# others on similar UI-text workloads. Callers can override via the
# `min_confidence` parameter to recognize() / _parse_tsv().
_DEFAULT_MIN_CONFIDENCE = 30


def is_available() -> bool:
    """Returns True iff a tesseract binary is on $PATH."""

    return shutil.which("tesseract") is not None


def recognize(
    png_bytes: bytes,
    capture_x: int,
    capture_y: int,
    upscale_factor: float = 1.0,
    lang: str = "eng",
    timeout: float = 10.0,
    min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
) -> list[OCRWord]:
    """Run tesseract on the supplied PNG and return recognized words.

    The bbox coordinates in returned OCRWord objects are absolute screen
    coordinates: tesseract reports them in image-local pixels, this
    function translates them by (capture_x, capture_y) and divides by
    upscale_factor so the result matches the on-screen layout
    regardless of pre-OCR scaling.

    Raises OCREngineError if tesseract is not installed or the
    subprocess fails. An empty list is a valid result (e.g. blank
    capture); only invocation errors raise.
    """

    if not is_available():
        raise OCREngineError(
            "tesseract binary not found on PATH. Install the 'tesseract' "
            "package and at least one language data pack."
        )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp_path.write_bytes(png_bytes)

    try:
        result = subprocess.run(
            ["tesseract", str(tmp_path), "-", "-l", lang, "tsv"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        tmp_path.unlink(missing_ok=True)
        raise OCREngineError(f"tesseract timed out after {timeout}s") from error
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise OCREngineError(f"tesseract exited {result.returncode}: {stderr}")

    return _parse_tsv(
        result.stdout.decode(errors="replace"),
        capture_x=capture_x,
        capture_y=capture_y,
        upscale_factor=upscale_factor,
        min_confidence=min_confidence,
    )


def _parse_tsv(
    tsv: str,
    capture_x: int,
    capture_y: int,
    upscale_factor: float,
    min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
) -> list[OCRWord]:
    lines = tsv.splitlines()
    if not lines:
        return []

    header = lines[0].split("\t")
    try:
        col = {name: header.index(name) for name in (
            "level", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text",
        )}
    except ValueError as error:
        msg = f"OCR ENGINE: Unexpected TSV header: {header} ({error})"
        debug.print_message(debug.LEVEL_WARNING, msg, True)
        return []

    scale = upscale_factor if upscale_factor > 0 else 1.0
    words: list[OCRWord] = []
    for raw in lines[1:]:
        fields = raw.split("\t")
        if len(fields) <= col["text"]:
            continue
        try:
            level = int(fields[col["level"]])
        except ValueError:
            continue
        if level != _WORD_LEVEL:
            continue
        text = fields[col["text"]].strip()
        if not text:
            continue
        try:
            confidence = int(float(fields[col["conf"]]))
        except ValueError:
            continue
        if confidence < min_confidence:
            continue
        try:
            left = int(fields[col["left"]])
            top = int(fields[col["top"]])
            width = int(fields[col["width"]])
            height = int(fields[col["height"]])
            block_num = int(fields[col["block_num"]])
            par_num = int(fields[col["par_num"]])
            line_num = int(fields[col["line_num"]])
        except ValueError:
            continue
        words.append(
            OCRWord(
                text=text,
                screen_x=capture_x + int(left / scale),
                screen_y=capture_y + int(top / scale),
                width=int(width / scale),
                height=int(height / scale),
                confidence=confidence,
                block_num=block_num,
                par_num=par_num,
                line_num=line_num,
            )
        )

    return words
