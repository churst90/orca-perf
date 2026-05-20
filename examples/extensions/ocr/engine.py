# Orca OCR extension -- Tesseract wrapper.
#
# Copyright 2026 The Orca Team
# License: LGPL-2.1-or-later

"""Tesseract subprocess wrapper + TSV parser.

Spawns `tesseract <png> - tsv` with stdout redirected to a temp
file (not a pipe, to avoid pipe-buffer deadlock on large outputs)
and uses GLib.child_watch_add to be notified of completion. The
TSV is parsed into OCRWord instances with absolute screen
coordinates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from gi.repository import GLib

from .buffer import OCRWord


class OCREngineError(Exception):
    """Raised when tesseract is unavailable or fails."""


_WORD_LEVEL = 5
_DEFAULT_MIN_CONFIDENCE = 30
RecognizeCallback = Callable[
    ["list[OCRWord] | None", "OCREngineError | None"], None,
]


def is_available() -> bool:
    return shutil.which("tesseract") is not None


def list_available_languages() -> list[str]:
    if not is_available():
        return ["eng"]
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True, timeout=2, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ["eng"]
    if result.returncode != 0:
        return ["eng"]
    lines = result.stdout.decode(errors="replace").splitlines()
    langs = [line.strip() for line in lines[1:] if line.strip()]
    langs = [lang for lang in langs if lang != "osd"]
    return langs or ["eng"]


def recognize_async(
    png_bytes: bytes, capture_x: int, capture_y: int,
    on_done: RecognizeCallback,
    upscale_factor: float = 1.0, lang: str = "eng",
    min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
) -> int | None:
    """Non-blocking Tesseract invocation. Returns child PID or None."""

    if not is_available():
        on_done(None, OCREngineError("tesseract not on PATH"))
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png_tmp:
        png_path = Path(png_tmp.name)
        png_path.write_bytes(png_bytes)
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as tsv_tmp:
        tsv_path = Path(tsv_tmp.name)
    tsv_fd = os.open(tsv_path, os.O_WRONLY | os.O_TRUNC)
    try:
        proc = subprocess.Popen(
            ["tesseract", str(png_path), "-", "-l", lang, "tsv"],
            stdout=tsv_fd, stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        os.close(tsv_fd)
        png_path.unlink(missing_ok=True)
        tsv_path.unlink(missing_ok=True)
        on_done(None, OCREngineError(f"failed to spawn tesseract: {error}"))
        return None
    finally:
        try:
            os.close(tsv_fd)
        except OSError:
            pass

    def on_child_done(_pid: int, status: int) -> None:
        png_path.unlink(missing_ok=True)
        try:
            if status != 0:
                tsv_path.unlink(missing_ok=True)
                on_done(None, OCREngineError(f"tesseract exited {status}"))
                return
            try:
                tsv = tsv_path.read_text(errors="replace")
            finally:
                tsv_path.unlink(missing_ok=True)
            words = parse_tsv(
                tsv, capture_x=capture_x, capture_y=capture_y,
                upscale_factor=upscale_factor, min_confidence=min_confidence,
            )
            on_done(words, None)
        except Exception as error:  # pylint: disable=broad-exception-caught
            on_done(None, OCREngineError(str(error)))

    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, on_child_done)
    return proc.pid


def parse_tsv(
    tsv: str, capture_x: int, capture_y: int,
    upscale_factor: float, min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
) -> list[OCRWord]:
    """Parse Tesseract TSV output into OCRWord objects."""

    lines = tsv.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    try:
        col = {name: header.index(name) for name in (
            "level", "block_num", "par_num", "line_num", "word_num",
            "left", "top", "width", "height", "conf", "text",
        )}
    except ValueError:
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
        words.append(OCRWord(
            text=text,
            screen_x=capture_x + int(left / scale),
            screen_y=capture_y + int(top / scale),
            width=int(width / scale), height=int(height / scale),
            confidence=confidence, block_num=block_num,
            par_num=par_num, line_num=line_num,
        ))
    return words
