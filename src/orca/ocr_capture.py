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

"""Screen capture for the OCR pipeline.

Phase 1 only ships an X11 backend. The session-type detection here is
deliberately permissive: XDG_SESSION_TYPE often reports "wayland" on
GNOME/MATE setups where Xorg is actually the display server, so we
attempt Gdk capture regardless of the env var and only fall back when
the call returns None or raises.

Wayland-native capture (via xdg-desktop-portal) is intentionally
deferred to a later phase.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf  # noqa: E402

from . import debug  # noqa: E402


class OCRCaptureError(Exception):
    """Raised when no capture backend can produce an image for the requested region."""


def capture_region(x: int, y: int, width: int, height: int) -> bytes:
    """Capture the requested screen region and return PNG bytes.

    Tries Gdk first (in-process, no subprocess overhead, ~50ms on
    typical hardware). Falls back to ImageMagick `import` if Gdk
    cannot produce a pixbuf for the region (Wayland without Xwayland
    fallback, missing display, etc.).
    """

    if width <= 0 or height <= 0:
        raise OCRCaptureError(f"Invalid capture region: {width}x{height}")

    try:
        png = _capture_via_gdk(x, y, width, height)
        if png is not None:
            return png
    except Exception as error:
        msg = f"OCR CAPTURE: Gdk capture failed: {error}"
        debug.print_message(debug.LEVEL_INFO, msg, True)

    png = _capture_via_imagemagick(x, y, width, height)
    if png is not None:
        return png

    raise OCRCaptureError(
        "No working screen-capture backend. Tried Gdk (X11) and "
        "ImageMagick 'import'. Install ImageMagick or run under X11."
    )


def _capture_via_gdk(x: int, y: int, width: int, height: int) -> bytes | None:
    root = Gdk.get_default_root_window()
    if root is None:
        return None

    pixbuf = Gdk.pixbuf_get_from_window(root, x, y, width, height)
    if pixbuf is None:
        return None

    success, buf = pixbuf.save_to_bufferv("png", [], [])
    if not success:
        return None
    return bytes(buf)


def _capture_via_imagemagick(x: int, y: int, width: int, height: int) -> bytes | None:
    if not shutil.which("import"):
        return None

    # `import -window root -crop WxH+X+Y png:-` is the most reliable
    # invocation; writing to a temp file is a hair slower but safer
    # against ImageMagick versions that ignore `png:-`.
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [
                "import",
                "-window", "root",
                "-crop", f"{width}x{height}+{x}+{y}",
                str(tmp_path),
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            msg = f"OCR CAPTURE: ImageMagick failed: {result.stderr.decode(errors='replace')}"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return None
        return tmp_path.read_bytes()
    except subprocess.TimeoutExpired:
        msg = "OCR CAPTURE: ImageMagick timed out after 5s"
        debug.print_message(debug.LEVEL_WARNING, msg, True)
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def upscale_png(png_bytes: bytes, factor: float = 2.0) -> bytes:
    """Upscale a PNG via bilinear interpolation.

    Tesseract's recognition rate on default-size UI text climbs sharply
    when input pixels are scaled 2-3x before recognition. Cost is ~50ms
    on a typical window. Returns the original bytes unchanged on any
    failure -- the engine will still attempt recognition.
    """

    if factor <= 1.0:
        return png_bytes
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png_bytes)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return png_bytes
        new_w = int(pixbuf.get_width() * factor)
        new_h = int(pixbuf.get_height() * factor)
        scaled = pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
        if scaled is None:
            return png_bytes
        success, buf = scaled.save_to_bufferv("png", [], [])
        if not success:
            return png_bytes
        return bytes(buf)
    except Exception as error:
        msg = f"OCR CAPTURE: Upscale failed, using original: {error}"
        debug.print_message(debug.LEVEL_INFO, msg, True)
        return png_bytes
