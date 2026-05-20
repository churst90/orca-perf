# Orca OCR user extension.
#
# Adapts the perf-branch built-in OCR feature to Orca's user-extension
# framework (~/.local/share/orca/extensions/). Single file because
# the loader hashes per-file and approves per-file; multi-file
# extensions would require approving each helper module separately.
#
# Copyright 2026 The Orca Team
# License: LGPL-2.1-or-later

"""OCR user extension for Orca.

Press Orca+R to capture the focused window's pixels, recognize the
text via Tesseract, and enter a virtual buffer that you navigate
with NumPad keys. Selection via Shift+nav, copy via NumPad+, click
pass-through via NumPad/ and NumPad*.

This extension is the user-extension-framework adaptation of the
perf-branch built-in OCR presenter. Differences from the built-in
are documented inline as "GAP-N:" comments and correspond to
issues filed against gitlab.gnome.org/GNOME/orca to extend the
controller API.

The full pre-conversion implementation lives at
src/orca/ocr_presenter.py in github.com/churst90/orca-perf and
remains the source of record for performance / correctness.
"""

# pylint: disable=too-many-lines

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TYPE_CHECKING
from urllib.parse import urlparse

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Atspi, Gdk, GdkPixbuf, Gio, GLib  # noqa: E402

# Focused-window, clipboard, and synthesized-mouse-event access all
# go through the controller, which gained the relevant wrappers in
# perf-branch commit 766a2e96a (see submissions/ext_api_gaps/
# 02-controller-active-window.md, 03-controller-clipboard.md, and
# 04-controller-mouse-event.md for the upstream issue filings).

# GAP-4: modal key discipline. To take temporary ownership of
# NumPad keys while OCR mode is active without conflicting with
# flat-review, we still have to reach into command_manager and
# call set_suspended(True/False) on individual commands. No
# controller "enter modal mode" API exists yet.
# Proposed: controller.enter_modal_mode(keys) /
# controller.exit_modal_mode() -- see issue
# submissions/ext_api_gaps/05-modal-key-discipline.md.
from orca import command_manager  # noqa: E402

from orca import debug, keybindings  # noqa: E402
from orca.command import Command, KeyboardCommand  # noqa: E402
from orca.extension import Extension  # noqa: E402

if TYPE_CHECKING:
    from orca.scripts import default


# =====================================================================
# Buffer dataclasses (consolidated from ocr_buffer.py)
# =====================================================================


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


# =====================================================================
# Capture backends (consolidated from ocr_capture.py)
# =====================================================================


class _OCRCaptureError(Exception):
    pass


# Type for the async capture callback.
_CaptureCallback = Callable[["bytes | None", "str | None"], None]


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
        cx, cy = max(0, min(x, fw - 1)), max(0, min(y, fh - 1))
        cw, ch = max(1, min(w, fw - cx)), max(1, min(h, fh - cy))
        cropped = full.new_subpixbuf(cx, cy, cw, ch)
        if cropped is None:
            return None
        success, buf = cropped.save_to_bufferv("png", [], [])
        return bytes(buf) if success else None
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def _capture_via_portal_async(
    x: int, y: int, w: int, h: int, on_done: _CaptureCallback,
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


def _capture_region_async(
    x: int, y: int, w: int, h: int, on_done: _CaptureCallback,
) -> None:
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


def _upscale_png(png_bytes: bytes, factor: float = 2.0) -> bytes:
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


# =====================================================================
# Tesseract wrapper (consolidated from ocr_engine.py)
# =====================================================================


class _OCREngineError(Exception):
    pass


_WORD_LEVEL = 5
_DEFAULT_MIN_CONFIDENCE = 30
_RecognizeCallback = Callable[
    ["list[OCRWord] | None", "_OCREngineError | None"], None,
]


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _recognize_async(
    png_bytes: bytes, capture_x: int, capture_y: int,
    on_done: _RecognizeCallback,
    upscale_factor: float = 1.0, lang: str = "eng",
    min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
) -> int | None:
    if not _tesseract_available():
        on_done(None, _OCREngineError("tesseract not on PATH"))
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
        on_done(None, _OCREngineError(f"failed to spawn tesseract: {error}"))
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
                on_done(None, _OCREngineError(f"tesseract exited {status}"))
                return
            try:
                tsv = tsv_path.read_text(errors="replace")
            finally:
                tsv_path.unlink(missing_ok=True)
            words = _parse_tsv(
                tsv, capture_x=capture_x, capture_y=capture_y,
                upscale_factor=upscale_factor, min_confidence=min_confidence,
            )
            on_done(words, None)
        except Exception as error:  # pylint: disable=broad-exception-caught
            on_done(None, _OCREngineError(str(error)))

    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, proc.pid, on_child_done)
    return proc.pid


def _parse_tsv(
    tsv: str, capture_x: int, capture_y: int,
    upscale_factor: float, min_confidence: int = _DEFAULT_MIN_CONFIDENCE,
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


# =====================================================================
# OCR presenter Extension subclass
# =====================================================================


# Position = (line_idx, word_idx, char_idx).
_Position = tuple[int, int, int]


# GAP-5: configuration. Built-in version exposes lang, upscale-factor,
# and confidence-threshold under org.gnome.Orca.OCR gsettings. The
# user-extension framework cannot register gsettings schemas yet
# (Joanie's docs/user-extensions.md "Status" section calls this out).
# We hard-code defaults here. When extension-settings land in the
# framework, switch to controller.get_extension_setting(key) or
# similar.
_OCR_LANG = "eng"
_OCR_UPSCALE = 2.0
_OCR_CONFIDENCE = 30
_MAX_CAPTURE_DIM = 4096


class OcrExtension(Extension):
    """OCR user extension. See module docstring for usage."""

    # GAP-8: i18n. The built-in version routes every string through
    # messages.py / cmdnames.py / guilabels.py for translation. User
    # extensions cannot ship .mo files (no per-extension locale
    # directory in the framework). Strings here are bare English.
    # Proposed: per-extension i18n -- gettext domain loading from an
    # extension-local locale/ subdirectory, or a "register translation
    # function" controller API. See issue #(TBD).
    GROUP_LABEL = "OCR"

    # Same modal-key map as the built-in version. See GAP-4 below for
    # how we actually take ownership of these keys.
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
        self._externally_suspended: list = []
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

    # ---- toggle ----------------------------------------------------

    # GAP-9: handler arity. The user-extension example in the docs
    # shows command functions taking no arguments other than self.
    # But KeyboardCommand-bound handlers actually receive (script,
    # event) when dispatched by command_manager. The Extension base
    # class _wrap_function() in extension.py handles this for user
    # extensions but the documented contract is unclear. We follow
    # the documented contract (def foo(self): ...) and observe that
    # the wrap is correctly applied because mark_as_user_extension()
    # was called by the loader.

    def toggle_ocr_mode(self) -> bool:
        if self._mode_active:
            return self.exit_ocr_mode()
        return self._enter_ocr_mode()

    def _say(self, text: str) -> None:
        self.controller.present_message_internal(text)

    def _enter_ocr_mode(self) -> bool:
        if not _tesseract_available():
            self._say("OCR unavailable: tesseract is not installed.")
            return True

        # Use the public controller API rather than reaching into
        # focus_manager directly. Works on perf-branch >= 766a2e96a;
        # falls back gracefully if running on older Orca (the
        # AttributeError is caught and we route around via the same
        # internal that the controller method wraps).
        window = self.controller.get_active_window()
        if window is None:
            self._say("OCR: no focused window.")
            return True

        # Use the public controller API; gives us (x, y, w, h) in
        # absolute screen coords with CoordType.SCREEN already
        # applied correctly.
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
        _capture_region_async(x, y, width, height, self._on_capture_done)
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
        png_bytes = _upscale_png(png_bytes, _OCR_UPSCALE)
        pid = _recognize_async(
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
        error: _OCREngineError | None,
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

    # ---- modal key activation -- GAP-4 in action -------------------

    def _activate_mode_keys(self) -> None:
        # GAP-4: reach into command_manager and individually suspend
        # every command bound to a NumPad key we want, then
        # unsuspend our own. There is no controller API for "I
        # want to own these keys while in my mode."
        manager = command_manager.get_manager()
        target_pairs = {(k, m) for k, m, _ in self._MODE_KEYS}
        ocr_command_names = {
            self._ocr_command_name(k, m, a) for k, m, a in self._MODE_KEYS
        }
        self._externally_suspended.clear()
        for cmd in manager.get_all_keyboard_commands():
            if cmd.get_name() in ocr_command_names:
                continue
            binding = cmd.get_keybinding()
            if binding is None:
                continue
            if (binding.keysymstring, binding.modifiers) not in target_pairs:
                continue
            if cmd.is_suspended():
                continue
            cmd.set_suspended(True)
            self._externally_suspended.append(cmd)
        for keysym, mod, attr in self._MODE_KEYS:
            cmd = manager.get_command(self._ocr_command_name(keysym, mod, attr))
            if cmd is not None:
                cmd.set_suspended(False)

    def _deactivate_mode_keys(self) -> None:
        manager = command_manager.get_manager()
        for keysym, mod, attr in self._MODE_KEYS:
            cmd = manager.get_command(self._ocr_command_name(keysym, mod, attr))
            if cmd is not None:
                cmd.set_suspended(True)
        for cmd in self._externally_suspended:
            cmd.set_suspended(False)
        self._externally_suspended.clear()

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
        return self._click("b1c", "Click")

    def right_click_current(self) -> bool:
        if not self._mode_active:
            return False
        return self._click("b3c", "Right-click")

    def _click(self, button: str, verb: str) -> bool:
        if self._buffer is None or self._cursor is None or self._source_window is None:
            return True
        word = self._current_word()
        if word is None:
            self._say("OCR: cursor is not on a word.")
            return True
        # Use the controller's synthesize_mouse_event; it does the
        # absolute-screen-to-window-relative coord conversion for us.
        # Translate our two-char internal button code ("b1c"/"b3c")
        # to the controller's friendly name.
        button_name = {"b1c": "left", "b3c": "right"}.get(button, "left")
        screen_x = word.screen_x + word.width // 2
        screen_y = word.screen_y + word.height // 2
        ok = self.controller.synthesize_mouse_event(
            screen_x, screen_y, button_name,
        )
        if not ok:
            self._say(f"OCR: {verb.lower()} did not reach the window.")
            return True
        self._say(f"{verb}: {word.text}")
        return True
