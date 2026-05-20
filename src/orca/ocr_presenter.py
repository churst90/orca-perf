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

"""Phase 1 OCR presenter for Orca.

User presses Orca+R while focused on an arbitrary window. The presenter
captures the window's pixel region, runs Tesseract over it, and shows
the recognized lines in a simple GTK dialog. Standard accessible-widget
navigation handles arrow-key reading inside the dialog; Orca speaks
each line as the user moves through the TreeView.

Phase 1 scope: capture + recognize + show. No mouse routing, no
overlay-style cursor, no async pipeline (Tesseract runs synchronously
on the main loop with an audible "Recognizing..." cue beforehand).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk  # noqa: E402

from . import (
    clipboard,
    dbus_service,
    debug,
    focus_manager,
    input_event,
    keybindings,
    presentation_manager,
)
from .ax_component import AXComponent
from .ax_object import AXObject
from .command import Command, KeyboardCommand
from .extension import Extension
from . import ocr_capture, ocr_engine
from .ocr_buffer import OCRBuffer

if TYPE_CHECKING:
    from collections.abc import Callable

    from .scripts import default


# Group label exposed to the keybindings preferences dialog. Kept as a
# bare string for Phase 1; promote to guilabels when this graduates from
# perf-branch experiment.
GROUP_LABEL = "OCR"

# Maximum dimension we'll feed to Tesseract before refusing the capture.
# 4096x4096 covers fullscreen on most modern displays; beyond that the
# subprocess can balloon past 10s and timeout.
_MAX_CAPTURE_DIM = 4096

# Upscale factor for the captured pixbuf before OCR. 2.0 is the sweet
# spot for UI text on typical 1080p / 1440p displays per the design
# notes; bump to 3.0 if the user reports poor recognition on a HiDPI
# display where text is already crisp.
_UPSCALE_FACTOR = 2.0


class OCRPresenter(Extension):
    """Captures the focused window and presents its recognized text."""

    GROUP_LABEL = GROUP_LABEL

    def __init__(self) -> None:
        self._last_buffer: OCRBuffer | None = None
        self._dialog: _OCRResultDialog | None = None
        super().__init__()

    def _get_commands(self) -> list[Command]:
        desktop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        laptop_kb = keybindings.KeyBinding("r", keybindings.ORCA_MODIFIER_MASK)
        return [
            KeyboardCommand(
                "recognizeFocusedWindowHandler",
                self.recognize_focused_window,
                self.GROUP_LABEL,
                "Recognize text in the focused window via OCR",
                desktop_keybinding=desktop_kb,
                laptop_keybinding=laptop_kb,
            ),
        ]

    @dbus_service.command
    def recognize_focused_window(
        self,
        script: default.Script,
        event: input_event.InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Capture the focused window, OCR it, and show the recognized text."""

        del event  # unused in Phase 1

        if not ocr_engine.is_available():
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR unavailable: tesseract is not installed."
                )
            return True

        window = focus_manager.get_manager().get_active_window()
        if window is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: no focused window."
                )
            return True

        rect = AXComponent.get_rect(window)
        x, y, width, height = int(rect.x), int(rect.y), int(rect.width), int(rect.height)
        if width <= 0 or height <= 0:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR: focused window has no measurable size."
                )
            return True
        if width > _MAX_CAPTURE_DIM or height > _MAX_CAPTURE_DIM:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR: window is too large to recognize ({width}x{height})."
                )
            return True

        window_name = AXObject.get_name(window) or ""

        if notify_user:
            presentation_manager.get_manager().present_message("Recognizing.")

        start = time.time()
        try:
            png = ocr_capture.capture_region(x, y, width, height)
            png = ocr_capture.upscale_png(png, _UPSCALE_FACTOR)
            words = ocr_engine.recognize(
                png,
                capture_x=x,
                capture_y=y,
                upscale_factor=_UPSCALE_FACTOR,
            )
        except ocr_capture.OCRCaptureError as error:
            msg = f"OCR PRESENTER: Capture failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR capture failed: {error}"
                )
            return True
        except ocr_engine.OCREngineError as error:
            msg = f"OCR PRESENTER: Recognition failed: {error}"
            debug.print_message(debug.LEVEL_WARNING, msg, True)
            if notify_user:
                presentation_manager.get_manager().present_message(
                    f"OCR engine failed: {error}"
                )
            return True

        elapsed = time.time() - start
        buffer = OCRBuffer.from_words(
            words,
            capture_x=x,
            capture_y=y,
            capture_width=width,
            capture_height=height,
            source_window_name=window_name,
        )
        self._last_buffer = buffer

        tokens = [
            "OCR PRESENTER: Recognized",
            f"{len(words)} words / {len(buffer.lines)} lines",
            f"in {elapsed:.2f}s from",
            f"'{window_name}' ({width}x{height} at +{x}+{y})",
        ]
        debug.print_message(debug.LEVEL_INFO, " ".join(tokens), True)

        if buffer.is_empty:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    "OCR found no readable text in this window."
                )
            return True

        self._show_dialog(script, buffer)
        return True

    def get_last_buffer(self) -> OCRBuffer | None:
        """Returns the most recently produced buffer, or None.

        Exposed so Phase 2's mouse-routing layer can read the same data
        without re-running the pipeline.
        """

        return self._last_buffer

    def _show_dialog(self, script: default.Script, buffer: OCRBuffer) -> None:
        if self._dialog is not None:
            self._dialog.destroy()
            self._dialog = None

        self._dialog = _OCRResultDialog(
            script,
            buffer,
            destroyed_callback=self._on_dialog_destroyed,
        )
        self._dialog.show()

    def _on_dialog_destroyed(self, _dialog: Gtk.Dialog) -> None:
        self._dialog = None


class _OCRResultDialog:
    """GTK dialog displaying one row per recognized line.

    A real TreeView -- so Orca's existing widget-navigation handling
    speaks each row as the user arrows through it. Closing the dialog
    returns focus to the originating window.
    """

    def __init__(
        self,
        script: default.Script,
        buffer: OCRBuffer,
        destroyed_callback: Callable[[Gtk.Dialog], None],
    ) -> None:
        self._script = script
        self._buffer = buffer
        self._gui = self._build(buffer)
        self._gui.connect("destroy", destroyed_callback)

    def _build(self, buffer: OCRBuffer) -> Gtk.Dialog:
        title_name = buffer.source_window_name or "the focused window"
        title = f"OCR result: {title_name}"
        dialog = Gtk.Dialog(
            title,
            None,
            Gtk.DialogFlags.MODAL,
            (
                "Copy all", Gtk.ResponseType.APPLY,
                Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE,
            ),
        )
        dialog.set_default_size(700, 450)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        dialog.get_content_area().add(scrolled)

        tree = Gtk.TreeView()
        tree.set_hexpand(True)
        tree.set_vexpand(True)
        scrolled.add(tree)  # pylint: disable=no-member

        store = Gtk.ListStore(GObject.TYPE_STRING)
        for line in buffer.lines:
            store.append([line.text])
        tree.set_model(store)

        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("Recognized line", renderer, text=0)
        tree.append_column(column)

        # Land the cursor on the first row so Orca speaks something
        # immediately when the dialog opens.
        if len(store) > 0:
            tree.set_cursor(Gtk.TreePath.new_first(), column, False)
            tree.grab_focus()

        dialog.connect("response", self._on_response)
        return dialog

    def _on_response(self, dialog: Gtk.Dialog, response: int) -> None:
        if response == Gtk.ResponseType.APPLY:
            clipboard.get_presenter().set_text(self._buffer.text)
            presentation_manager.get_manager().present_message(
                f"Copied {len(self._buffer.lines)} lines to clipboard."
            )
            return
        dialog.destroy()

    def show(self) -> None:
        self._gui.show_all()  # pylint: disable=no-member
        self._gui.present_with_time(time.time())

    def destroy(self) -> None:
        self._gui.destroy()


_presenter: OCRPresenter | None = None


def get_presenter() -> OCRPresenter:
    """Returns (or creates) the singleton OCRPresenter."""

    global _presenter
    if _presenter is None:
        _presenter = OCRPresenter()
    return _presenter
