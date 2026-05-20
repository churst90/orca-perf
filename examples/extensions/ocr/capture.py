# Orca OCR extension -- screen-capture backends.
#
# Copyright 2026 The Orca Team
# License: LGPL-2.1-or-later

"""Three screen-capture backends:

  1. Gdk.pixbuf_get_from_window  -- in-process, X11.
  2. ImageMagick `import`        -- subprocess, X11.
  3. xdg-desktop-portal          -- async D-Bus, Wayland.

capture_region_async chains all three in order. On X11 the first
or second succeeds before the function returns; on real Wayland
the portal path runs asynchronously and the callback fires from
the GLib main loop when the portal responds.

Also exposes upscale_png as a separate utility; pre-OCR upscale
roughly doubles Tesseract's recognition rate on small UI text.
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


# (png_bytes, error_message) -- exactly one is None when fired.
CaptureCallback = Callable[["bytes | None", "str | None"], None]


def _capture_via_gdk(x: int, y: int, w: int, h: int) -> bytes | None:
    root = Gdk.get_default_root_window()
    if root is None:
        return None
    pixbuf = Gdk.pixbuf_get_from_window(root, x, y, w, h)
    if pixbuf is None:
        return None
    success, buf = pixbuf.save_to_bufferv("png", [], [])
    return bytes(buf) if success else None


def _capture_via_imagemagick(x: int, y: int, w: int, h: int) -> bytes | None:
    if not shutil.which("import"):
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["import", "-window", "root", "-crop",
             f"{w}x{h}+{x}+{y}", str(tmp_path)],
            capture_output=True, timeout=5, check=False,
        )
        if result.returncode != 0:
            return None
        return tmp_path.read_bytes()
    except subprocess.TimeoutExpired:
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _crop_png(
    png_bytes: bytes, x: int, y: int, w: int, h: int,
) -> bytes | None:
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(png_bytes)
        loader.close()
        full = loader.get_pixbuf()
        if full is None:
            return None
        fw, fh = full.get_width(), full.get_height()
        cx = max(0, min(x, fw - 1))
        cy = max(0, min(y, fh - 1))
        cw = max(1, min(w, fw - cx))
        ch = max(1, min(h, fh - cy))
        cropped = full.new_subpixbuf(cx, cy, cw, ch)
        if cropped is None:
            return None
        success, buf = cropped.save_to_bufferv("png", [], [])
        return bytes(buf) if success else None
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _capture_via_portal_async(
    x: int, y: int, w: int, h: int, on_done: CaptureCallback,
) -> None:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as error:
        on_done(None, f"cannot connect to session bus: {error}")
        return
    token = f"orca_ocr_{secrets.token_hex(8)}"
    unique = bus.get_unique_name() or ""
    sender = unique.lstrip(":").replace(".", "_")
    expected_handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
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

    def on_response(_c, _s, _o, _i, _sig, parameters) -> None:
        try:
            response_code, results = parameters.unpack()
        except Exception as e:  # pylint: disable=broad-exception-caught
            finish(None, f"could not unpack portal response: {e}")
            return
        if response_code != 0:
            finish(None, f"portal denied (code {response_code})")
            return
        uri = results.get("uri", "")
        if not uri:
            finish(None, "portal returned empty URI")
            return
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            finish(None, f"non-file URI: {uri}")
            return
        try:
            png_bytes = Path(parsed.path).read_bytes()
        except OSError as e:
            finish(None, f"cannot read screenshot: {e}")
            return
        cropped = _crop_png(png_bytes, x, y, w, h)
        if cropped is None:
            finish(None, "crop failed")
            return
        finish(cropped, None)

    def on_timeout() -> bool:
        finish(None, "portal request timed out (30s)")
        return GLib.SOURCE_REMOVE

    state["sub_id"] = bus.signal_subscribe(
        "org.freedesktop.portal.Desktop",
        "org.freedesktop.portal.Request",
        "Response", expected_handle, None,
        Gio.DBusSignalFlags.NONE, on_response,
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
        except GLib.Error as e:
            finish(None, f"portal call failed: {e}")

    bus.call(
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot", "Screenshot",
        GLib.Variant("(sa{sv})", ("", options)),
        GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
        30000, None, on_call_complete,
    )


def capture_region_async(
    x: int, y: int, w: int, h: int, on_done: CaptureCallback,
) -> None:
    """Capture (x, y, w, h); invoke on_done with PNG bytes or error.

    Tries Gdk, then ImageMagick, then xdg-desktop-portal. The first
    two are synchronous (on_done fires before return on X11). The
    third is async via D-Bus (on_done fires from the GLib main loop
    when the portal responds, possibly several seconds later if
    user-permission is being requested for the first time).
    """

    if w <= 0 or h <= 0:
        on_done(None, f"invalid region {w}x{h}")
        return
    try:
        png = _capture_via_gdk(x, y, w, h)
        if png is not None:
            on_done(png, None)
            return
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    png = _capture_via_imagemagick(x, y, w, h)
    if png is not None:
        on_done(png, None)
        return
    _capture_via_portal_async(x, y, w, h, on_done)


def upscale_png(png_bytes: bytes, factor: float = 2.0) -> bytes:
    """Bilinear-upscale a PNG by `factor`. Returns input on failure.

    Tesseract recognition rate on UI text climbs sharply when input
    pixels are scaled 2-3x before OCR. Cost is ~50ms.
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
        return bytes(buf) if success else png_bytes
    except Exception:  # pylint: disable=broad-exception-caught
        return png_bytes
