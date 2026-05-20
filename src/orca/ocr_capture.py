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

Three backends are tried in order. The session-type detection here is
deliberately permissive: XDG_SESSION_TYPE often reports "wayland" on
GNOME/MATE setups where Xorg is actually the display server, so we
attempt Gdk capture regardless of the env var and only fall back when
the call returns None or raises.

  1. Gdk.pixbuf_get_from_window  -- in-process, ~50ms on X11.
  2. ImageMagick `import`        -- subprocess, ~150ms on X11.
  3. xdg-desktop-portal          -- async D-Bus signal, real Wayland.

The capture_region_async() entry point chains through the three in
that order. The first two are synchronous (and never reach the portal
on X11). The third (portal) is required on real Wayland because the
compositor refuses pixel access to non-privileged clients.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib  # noqa: E402

from . import debug  # noqa: E402


class OCRCaptureError(Exception):
    """Raised when no capture backend can produce an image for the requested region."""


# (png_bytes, error_message) -- exactly one is None when the callback fires.
CaptureCallback = Callable[["bytes | None", "str | None"], None]


def capture_region(x: int, y: int, width: int, height: int) -> bytes:
    """Capture the requested screen region and return PNG bytes (X11 only).

    Tries Gdk first (in-process, ~50ms on typical hardware). Falls back
    to ImageMagick `import` if Gdk cannot produce a pixbuf for the
    region.

    Does not attempt the xdg-desktop-portal path. Wayland callers
    should use capture_region_async, which adds portal fallback and
    runs asynchronously so the GLib main loop stays responsive
    through the portal's permission prompt.
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


# ===== Async capture pipeline ==========================================


def capture_region_async(
    x: int,
    y: int,
    width: int,
    height: int,
    on_done: CaptureCallback,
) -> None:
    """Capture the requested region; invoke on_done with PNG bytes or error.

    Tries the same three backends in the same order as capture_region,
    plus the xdg-desktop-portal Screenshot path for real Wayland. On
    X11, the first or second backend succeeds synchronously and
    on_done is invoked from this function before it returns. On real
    Wayland, the portal path runs asynchronously: this function
    returns immediately after starting the D-Bus request, and on_done
    is invoked from the GLib main loop when the portal responds.

    on_done(png_bytes, None) on success.
    on_done(None, error_message) on failure.
    """

    if width <= 0 or height <= 0:
        on_done(None, f"Invalid capture region: {width}x{height}")
        return

    # Synchronous fast paths first.
    try:
        png = _capture_via_gdk(x, y, width, height)
        if png is not None:
            on_done(png, None)
            return
    except Exception as error:
        msg = f"OCR CAPTURE: Gdk capture failed: {error}"
        debug.print_message(debug.LEVEL_INFO, msg, True)

    png = _capture_via_imagemagick(x, y, width, height)
    if png is not None:
        on_done(png, None)
        return

    # Last resort: xdg-desktop-portal (async). This is the Wayland
    # path. On X11 we never reach here because Gdk succeeds first.
    _capture_via_portal_async(x, y, width, height, on_done)


def _capture_via_portal_async(
    x: int,
    y: int,
    width: int,
    height: int,
    on_done: CaptureCallback,
) -> None:
    """Capture via xdg-desktop-portal Screenshot, crop to (x, y, w, h).

    The portal returns a URI to a full-screen screenshot file. We
    crop client-side via Gdk to extract the requested region.

    The first call in a session may trigger a permission prompt; the
    user may click Allow once and subsequent calls are typically
    granted automatically by the portal. The whole flow is async --
    on_done runs from the GLib main loop when the portal responds.
    """

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        on_done(None, f"cannot connect to session bus: {error}")
        return

    # Per-request handle token so multiple in-flight portal requests
    # never collide on their Response signal subscription.
    token = f"orca_ocr_{secrets.token_hex(8)}"
    unique = bus.get_unique_name() or ""
    sender = unique.lstrip(":").replace(".", "_")
    expected_handle = (
        f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    )

    state: dict = {"sub_id": None, "timeout_id": None, "done": False}

    def cleanup() -> None:
        if state["sub_id"] is not None:
            bus.signal_unsubscribe(state["sub_id"])
            state["sub_id"] = None
        if state["timeout_id"] is not None:
            GLib.source_remove(state["timeout_id"])
            state["timeout_id"] = None

    def finish(png: bytes | None, error: str | None) -> None:
        if state["done"]:
            return
        state["done"] = True
        cleanup()
        on_done(png, error)

    def on_response(
        _connection, _sender, _obj_path, _iface, _signal, parameters,
    ) -> None:
        try:
            response_code, results = parameters.unpack()
        except Exception as error:  # pylint: disable=broad-exception-caught
            finish(None, f"could not unpack portal response: {error}")
            return
        if response_code != 0:
            finish(None, f"portal denied (response code {response_code})")
            return
        uri = results.get("uri", "")
        if not uri:
            finish(None, "portal returned empty URI")
            return
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            finish(None, f"portal returned non-file URI: {uri}")
            return
        try:
            png_bytes = Path(parsed.path).read_bytes()
        except OSError as error:
            finish(None, f"cannot read screenshot file: {error}")
            return
        cropped = _crop_png(png_bytes, x, y, width, height)
        if cropped is None:
            finish(None, "failed to crop screenshot")
            return
        finish(cropped, None)

    def on_timeout() -> bool:
        finish(None, "portal request timed out (30s)")
        return GLib.SOURCE_REMOVE

    state["sub_id"] = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop",
        "org.freedesktop.portal.Request",
        "Response",
        expected_handle,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )
    state["timeout_id"] = GLib.timeout_add_seconds(30, on_timeout)

    options = GLib.Variant("a{sv}", {
        "handle_token": GLib.Variant("s", token),
        "interactive": GLib.Variant("b", False),
        "modal": GLib.Variant("b", False),
    })

    def on_call_complete(source, result) -> None:
        try:
            source.call_finish(result)
        except GLib.Error as error:
            finish(None, f"portal call failed: {error}")

    bus.call(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot",
        "Screenshot",
        GLib.Variant("(sa{sv})", ("", options)),
        GLib.VariantType("(o)"),
        Gio.DBusCallFlags.NONE,
        30000,
        None,
        on_call_complete,
    )


def _crop_png(
    png_bytes: bytes, x: int, y: int, width: int, height: int,
) -> bytes | None:
    """Crop a PNG byte string to (x, y, width, height). None on failure.

    Coordinates are clamped to the source pixbuf's extents so a slightly
    off-by-one window rect from AT-SPI does not crash the crop.
    """

    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png_bytes)
        loader.close()
        full = loader.get_pixbuf()
        if full is None:
            return None
        fw = full.get_width()
        fh = full.get_height()
        cx = max(0, min(x, fw - 1))
        cy = max(0, min(y, fh - 1))
        cw = max(1, min(width, fw - cx))
        ch = max(1, min(height, fh - cy))
        cropped = full.new_subpixbuf(cx, cy, cw, ch)
        if cropped is None:
            return None
        success, buf = cropped.save_to_bufferv("png", [], [])
        return bytes(buf) if success else None
    except Exception as error:  # pylint: disable=broad-exception-caught
        msg = f"OCR CAPTURE: crop failed: {error}"
        debug.print_message(debug.LEVEL_INFO, msg, True)
        return None
