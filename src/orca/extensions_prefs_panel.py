# Orca
#
# Copyright 2026 Cody Hurst.
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

# pylint: disable=broad-exception-caught
# pylint: disable=protected-access
# pylint: disable=too-many-instance-attributes

"""Extensions management page in Orca's preferences."""

from __future__ import annotations

import os
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # pylint: disable=no-name-in-module

from . import (
    debug,
    extension,
    extension_loader,
    guilabels,
    preferences_grid_base,
    profile_manager,
)

if TYPE_CHECKING:
    from .extension import Extension


_APPROVAL_OK = "approved"
_APPROVAL_MODIFIED = "modified"
_APPROVAL_UNAPPROVED = "unapproved"


@dataclass
class _ExtensionInfo:
    """Snapshot of one installed extension as the prefs UI sees it."""

    dir_name: str
    path: str
    is_package: bool
    display_name: str
    version: str
    author: str
    license: str
    url: str
    description: str
    module_name: str | None
    approval_state: str
    current_hash: str
    approved_hash: str | None
    is_disabled: bool
    instance: "Extension | None"
    preferences_style: str | None = None


class ExtensionsPreferencesGrid(preferences_grid_base.PreferencesGridBase):
    """Preferences grid for managing user extensions.

    Changes made here (enable/disable, install, uninstall) are written
    to dconf immediately. Cancel on the parent preferences window does
    not revert them; this matches the destructive nature of install
    and uninstall, and keeps Stage 1 simple. A future refinement could
    snapshot the disabled list on open and restore it on Cancel.
    """

    _COL_DISPLAY = 0
    _COL_VERSION = 1
    _COL_STATUS = 2
    _COL_DIR_NAME = 3

    def __init__(self) -> None:
        super().__init__(guilabels.EXTENSIONS_PAGE_TITLE)
        self._loader = extension_loader.get_loader()
        self._extensions_dir = os.path.join(
            GLib.get_user_data_dir(), "orca", "extensions",  # pylint: disable=no-value-for-parameter
        )
        self._extensions: list[_ExtensionInfo] = []

        self._all_on_radio: Gtk.RadioButton | None = None
        self._all_off_radio: Gtk.RadioButton | None = None
        self._tree_store: Gtk.ListStore | None = None
        self._tree_view: Gtk.TreeView | None = None
        self._actions_listbox: preferences_grid_base.FocusManagedListBox | None = None
        self._install_btn: Gtk.Button | None = None
        self._uninstall_btn: Gtk.Button | None = None
        self._toggle_btn: Gtk.Button | None = None
        self._settings_btn: Gtk.Button | None = None
        self._about_btn: Gtk.Button | None = None
        self._status_label: Gtk.Label | None = None

        self._build()
        self._reload_data()

    # ---- UI construction --------------------------------------------

    # pylint: disable=no-member
    def _build(self) -> None:
        """Lay out the page widgets."""

        row = 0

        info_listbox = self._create_info_listbox(guilabels.EXTENSIONS_INFO)
        info_listbox.set_margin_bottom(12)
        self.attach(info_listbox, 0, row, 1, 1)
        row += 1

        master_frame, master_grid = self._create_frame(
            guilabels.EXTENSIONS_MASTER_FRAME_LABEL,
        )
        self._all_on_radio = Gtk.RadioButton.new_with_mnemonic(
            None, guilabels.EXTENSIONS_LOAD_NORMALLY,
        )
        self._all_off_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self._all_on_radio, guilabels.EXTENSIONS_DISABLE_ALL,
        )
        self._set_margins(self._all_on_radio, start=12, end=12, top=6, bottom=2)
        self._set_margins(self._all_off_radio, start=12, end=12, top=2, bottom=6)
        master_grid.attach(self._all_on_radio, 0, 0, 1, 1)
        master_grid.attach(self._all_off_radio, 0, 1, 1, 1)
        self._all_on_radio.connect("toggled", self._on_master_toggled)
        self.attach(master_frame, 0, row, 1, 1)
        row += 1

        list_label = self._create_heading_label(
            guilabels.EXTENSIONS_LIST_HEADING,
        )
        self.attach(list_label, 0, row, 1, 1)
        row += 1

        self._tree_store = Gtk.ListStore(str, str, str, str)
        self._tree_view = Gtk.TreeView(model=self._tree_store)
        self._tree_view.set_headers_visible(True)
        self._tree_view.set_enable_search(True)
        self._tree_view.set_search_column(self._COL_DISPLAY)
        tree_a11y = self._tree_view.get_accessible()
        if tree_a11y is not None:
            tree_a11y.set_name(guilabels.EXTENSIONS_LIST_HEADING)

        for col_idx, title in (
            (self._COL_DISPLAY, guilabels.EXTENSIONS_COL_NAME),
            (self._COL_VERSION, guilabels.EXTENSIONS_COL_VERSION),
            (self._COL_STATUS, guilabels.EXTENSIONS_COL_STATUS),
        ):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=col_idx)
            column.set_resizable(True)
            if col_idx == self._COL_DISPLAY:
                column.set_expand(True)
            self._tree_view.append_column(column)

        selection = self._tree_view.get_selection()
        selection.set_mode(Gtk.SelectionMode.SINGLE)
        selection.connect("changed", self._on_selection_changed)
        self._tree_view.connect("row-activated", self._on_row_activated)
        self._tree_view.connect("key-press-event", self._on_tree_key_press)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(180)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        scrolled.add(self._tree_view)
        scrolled.set_hexpand(True)
        scrolled.set_vexpand(True)
        self.attach(scrolled, 0, row, 1, 1)
        row += 1

        self._actions_listbox = preferences_grid_base.FocusManagedListBox()
        self._actions_listbox.set_margin_top(6)
        actions_a11y = self._actions_listbox.get_accessible()
        if actions_a11y is not None:
            actions_a11y.set_name(guilabels.EXTENSIONS_ACTIONS_GROUP)

        self._install_btn = self._add_action_button(
            guilabels.EXTENSIONS_INSTALL_BTN, self._on_install_clicked,
        )
        self._uninstall_btn = self._add_action_button(
            guilabels.EXTENSIONS_UNINSTALL_BTN, self._on_uninstall_clicked,
        )
        self._toggle_btn = self._add_action_button(
            guilabels.EXTENSIONS_DISABLE_BTN, self._on_toggle_clicked,
        )
        self._settings_btn = self._add_action_button(
            guilabels.EXTENSIONS_SETTINGS_BTN, self._on_settings_clicked,
        )
        self._about_btn = self._add_action_button(
            guilabels.EXTENSIONS_ABOUT_BTN, self._on_about_clicked,
        )

        self.attach(self._actions_listbox, 0, row, 1, 1)
        row += 1

        self._status_label = Gtk.Label(label="", xalign=0)
        self._status_label.set_line_wrap(True)
        self._status_label.set_margin_top(12)
        self.attach(self._status_label, 0, row, 1, 1)
        row += 1
    # pylint: enable=no-member

    def _add_action_button(
        self,
        label: str,
        handler,
    ) -> Gtk.Button:
        """Add a single action button as its own row in the actions listbox."""

        assert self._actions_listbox is not None
        button = Gtk.Button.new_with_mnemonic(label)
        button.set_valign(Gtk.Align.CENTER)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", handler)

        row = Gtk.ListBoxRow()
        row.set_can_focus(True)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._set_margins(hbox, start=12, end=12, top=6, bottom=6)
        hbox.pack_start(button, False, False, 0)
        row.add(hbox)

        self._actions_listbox.add_row_with_widget(row, button)
        return button

    # ---- Data model --------------------------------------------------

    def _reload_data(self) -> None:
        """Re-scan disk and dconf, refresh widgets, restore selection."""

        previous = self._selected_dir_name()
        self._extensions = self._scan_extensions()

        assert self._tree_store is not None
        self._tree_store.clear()
        for ext in self._extensions:
            self._tree_store.append([
                ext.display_name,
                ext.version,
                self._format_status(ext),
                ext.dir_name,
            ])

        assert self._all_on_radio is not None
        assert self._all_off_radio is not None
        master_disabled = self._is_master_disable_active()
        self._all_on_radio.handler_block_by_func(self._on_master_toggled)
        if master_disabled:
            self._all_off_radio.set_active(True)
        else:
            self._all_on_radio.set_active(True)
        self._all_on_radio.handler_unblock_by_func(self._on_master_toggled)

        # Restore selection.
        if self._extensions:
            assert self._tree_view is not None
            target_idx = 0
            if previous:
                for i, ext in enumerate(self._extensions):
                    if ext.dir_name == previous:
                        target_idx = i
                        break
            self._tree_view.get_selection().select_path(
                Gtk.TreePath.new_from_indices([target_idx]),
            )
        else:
            self._update_button_sensitivity(None)
            self._update_status_label(None)

    def _scan_extensions(self) -> list[_ExtensionInfo]:
        """Walk ``extensions_dir`` and pair entries with dconf state."""

        approved = self._loader.get_approved_extensions()
        disabled = set(self._loader.get_disabled_extensions())
        live_by_module: dict[str, "Extension"] = {
            ext.module_name: ext
            for ext in self._loader.get_user_extensions()
            if ext.module_name
        }

        results: list[_ExtensionInfo] = []
        if not os.path.isdir(self._extensions_dir):
            return results

        for entry in sorted(os.listdir(self._extensions_dir)):
            if entry.startswith(("_", ".")):
                continue
            entry_path = os.path.join(self._extensions_dir, entry)

            info: _ExtensionInfo | None = None
            if os.path.isdir(entry_path):
                manifest_path = os.path.join(entry_path, "manifest.toml")
                if os.path.isfile(manifest_path):
                    info = self._info_from_package(
                        entry, entry_path, approved, disabled, live_by_module,
                    )
            elif os.path.isfile(entry_path) and entry.endswith(".py"):
                info = self._info_from_single_file(
                    entry, entry_path, approved, disabled, live_by_module,
                )

            if info is not None:
                results.append(info)
        return results

    def _info_from_package(
        self,
        dir_name: str,
        package_dir: str,
        approved: dict[str, str],
        disabled: set[str],
        live_by_module: dict[str, "Extension"],
    ) -> _ExtensionInfo | None:
        manifest_path = os.path.join(package_dir, "manifest.toml")
        try:
            with open(manifest_path, "rb") as fp:
                manifest = tomllib.load(fp)
        except (OSError, tomllib.TOMLDecodeError) as error:
            debug.print_message(
                debug.LEVEL_WARNING,
                f"EXTENSIONS PREFS: cannot read {manifest_path}: {error}",
                True,
            )
            return None

        ext_section = manifest.get("extension", {}) or {}
        entry_section = manifest.get("entry", {}) or {}
        prefs_section = manifest.get("preferences", {}) or {}

        display = (
            ext_section.get("display-name")
            or ext_section.get("name")
            or dir_name
        )
        module_name = entry_section.get("class") or None
        current_hash = extension_loader.ExtensionLoader._compute_package_hash(package_dir)
        approved_hash = approved.get(dir_name)
        state = _approval_state(current_hash, approved_hash)
        is_disabled = bool(module_name and module_name in disabled)

        # Validate manifest's preferences.style. Unrecognized styles
        # are dropped silently with a debug warning so an extension
        # that asks for "category" (not yet implemented) doesn't get
        # treated as if it asked for "dialog".
        preferences_style: str | None = prefs_section.get("style")
        if preferences_style is not None and preferences_style not in extension.PREFERENCES_STYLES:
            debug.print_message(
                debug.LEVEL_WARNING,
                f"EXTENSIONS PREFS: {dir_name} manifest declares unknown "
                f"preferences.style='{preferences_style}'; ignoring",
                True,
            )
            preferences_style = None

        return _ExtensionInfo(
            dir_name=dir_name,
            path=package_dir,
            is_package=True,
            display_name=str(display),
            version=str(ext_section.get("version", "")),
            author=str(ext_section.get("author", "")),
            license=str(ext_section.get("license", "")),
            url=str(ext_section.get("url", "")),
            description=str(ext_section.get("description", "")).strip(),
            module_name=module_name,
            approval_state=state,
            current_hash=current_hash,
            approved_hash=approved_hash,
            is_disabled=is_disabled,
            instance=live_by_module.get(module_name) if module_name else None,
            preferences_style=preferences_style,
        )

    def _info_from_single_file(
        self,
        filename: str,
        filepath: str,
        approved: dict[str, str],
        disabled: set[str],
        live_by_module: dict[str, "Extension"],
    ) -> _ExtensionInfo | None:
        module_name = self._loader.get_class_name(filepath)
        current_hash = extension_loader.ExtensionLoader._compute_hash(filepath)
        approved_hash = approved.get(filename)
        state = _approval_state(current_hash, approved_hash)
        is_disabled = bool(module_name and module_name in disabled)

        return _ExtensionInfo(
            dir_name=filename,
            path=filepath,
            is_package=False,
            display_name=module_name or filename,
            version="",
            author="",
            license="",
            url="",
            description="",
            module_name=module_name,
            approval_state=state,
            current_hash=current_hash,
            approved_hash=approved_hash,
            is_disabled=is_disabled,
            instance=live_by_module.get(module_name) if module_name else None,
        )

    def _format_status(self, ext: _ExtensionInfo) -> str:
        if ext.approval_state == _APPROVAL_MODIFIED:
            return guilabels.EXTENSIONS_STATUS_MODIFIED
        if ext.approval_state == _APPROVAL_UNAPPROVED:
            return guilabels.EXTENSIONS_STATUS_UNAPPROVED
        if ext.is_disabled:
            return guilabels.EXTENSIONS_STATUS_DISABLED
        if ext.instance is not None:
            return guilabels.EXTENSIONS_STATUS_LOADED
        return guilabels.EXTENSIONS_STATUS_NOT_LOADED

    # ---- Selection ---------------------------------------------------

    def _selected_dir_name(self) -> str | None:
        if self._tree_view is None:
            return None
        model, treeiter = self._tree_view.get_selection().get_selected()
        if treeiter is None:
            return None
        return model[treeiter][self._COL_DIR_NAME]

    def _get_selected(self) -> _ExtensionInfo | None:
        dir_name = self._selected_dir_name()
        if dir_name is None:
            return None
        for ext in self._extensions:
            if ext.dir_name == dir_name:
                return ext
        return None

    def _on_selection_changed(self, _selection: Gtk.TreeSelection) -> None:
        ext = self._get_selected()
        self._update_button_sensitivity(ext)
        self._update_status_label(ext)

    def _update_button_sensitivity(self, ext: _ExtensionInfo | None) -> None:
        assert self._uninstall_btn is not None
        assert self._toggle_btn is not None
        assert self._settings_btn is not None
        assert self._about_btn is not None

        has = ext is not None
        self._uninstall_btn.set_sensitive(has and ext.is_package)
        self._about_btn.set_sensitive(has)
        # Settings is reachable when the extension is loaded AND
        # declares preferences.style="dialog" in its manifest. Other
        # styles ("category") aren't implemented yet; if/when they
        # are, this gate widens.
        self._settings_btn.set_sensitive(
            has
            and ext.instance is not None
            and ext.preferences_style == extension.PREFERENCES_STYLE_DIALOG
        )
        self._toggle_btn.set_sensitive(has and ext.module_name is not None)

        if ext is not None:
            self._toggle_btn.set_label(
                guilabels.EXTENSIONS_ENABLE_BTN
                if ext.is_disabled
                else guilabels.EXTENSIONS_DISABLE_BTN
            )

    def _update_status_label(self, ext: _ExtensionInfo | None) -> None:
        assert self._status_label is not None
        if ext is None:
            self._status_label.set_text("")
            return
        bits = [ext.display_name]
        if ext.version:
            bits.append(ext.version)
        bits.append(self._format_status(ext))
        if ext.author:
            bits.append(ext.author)
        self._status_label.set_text(" — ".join(bits))

    def _on_row_activated(
        self,
        _tree: Gtk.TreeView,
        _path: Gtk.TreePath,
        _column: Gtk.TreeViewColumn,
    ) -> None:
        if self._about_btn is not None:
            self._on_about_clicked(self._about_btn)

    def _on_tree_key_press(
        self, _widget: Gtk.Widget, event: Gdk.EventKey,
    ) -> bool:
        if event.keyval == Gdk.KEY_space and self._toggle_btn is not None:
            self._on_toggle_clicked(self._toggle_btn)
            return True
        return False

    # ---- Master toggle ----------------------------------------------

    def _is_master_disable_active(self) -> bool:
        """True if every installed extension's module name is in disabled."""

        disabled = set(self._loader.get_disabled_extensions())
        module_names = {
            ext.module_name for ext in self._extensions if ext.module_name
        }
        if not module_names:
            return False
        return module_names.issubset(disabled)

    def _on_master_toggled(self, _radio: Gtk.RadioButton) -> None:
        assert self._all_off_radio is not None
        self._apply_master_disable(self._all_off_radio.get_active())
        self._reload_data()

    def _apply_master_disable(self, disable_all: bool) -> None:
        disabled = list(self._loader.get_disabled_extensions())
        changed = False
        for ext in self._extensions:
            if not ext.module_name:
                continue
            if disable_all:
                if ext.module_name not in disabled:
                    disabled.append(ext.module_name)
                    changed = True
                if ext.instance is not None and not ext.is_disabled:
                    self._safe_disable(ext)
            else:
                if ext.module_name in disabled:
                    disabled.remove(ext.module_name)
                    changed = True
                if ext.instance is not None and ext.is_disabled:
                    self._safe_enable(ext)
        if changed:
            self._loader.set_disabled_extensions(disabled)

    # ---- Toggle / Install / Uninstall --------------------------------

    def _on_toggle_clicked(self, _btn: Gtk.Button) -> None:
        ext = self._get_selected()
        if ext is None or not ext.module_name:
            return
        disabled = list(self._loader.get_disabled_extensions())
        if ext.is_disabled:
            if ext.module_name in disabled:
                disabled.remove(ext.module_name)
            self._loader.set_disabled_extensions(disabled)
            if ext.instance is not None:
                self._safe_enable(ext)
            else:
                self._show_status(
                    guilabels.EXTENSIONS_RESTART_TO_ENABLE % ext.display_name,
                )
        else:
            if ext.module_name not in disabled:
                disabled.append(ext.module_name)
            self._loader.set_disabled_extensions(disabled)
            if ext.instance is not None:
                self._safe_disable(ext)
        self._reload_data()

    def _on_install_clicked(self, _btn: Gtk.Button) -> None:
        chooser = Gtk.FileChooserDialog(
            title=guilabels.EXTENSIONS_INSTALL_DIALOG_TITLE,
            transient_for=self.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.add_button(guilabels.DIALOG_CANCEL, Gtk.ResponseType.CANCEL)
        chooser.add_button(
            guilabels.EXTENSIONS_INSTALL_BTN, Gtk.ResponseType.OK,
        )
        filt = Gtk.FileFilter()
        filt.set_name(guilabels.EXTENSIONS_FILE_FILTER)
        filt.add_pattern("*.orca-ext")
        chooser.add_filter(filt)

        response = chooser.run()
        archive_path = (
            chooser.get_filename() if response == Gtk.ResponseType.OK else None
        )
        chooser.destroy()
        if not archive_path:
            return

        preview = self._preview_archive(archive_path)
        if preview is None:
            self._error_dialog(guilabels.EXTENSIONS_INSTALL_BAD_ARCHIVE)
            return

        message = guilabels.EXTENSIONS_INSTALL_CONFIRM % preview
        if not self._confirm(
            guilabels.EXTENSIONS_INSTALL_DIALOG_TITLE,
            message,
            guilabels.EXTENSIONS_INSTALL_BTN,
        ):
            return

        os.makedirs(self._extensions_dir, exist_ok=True)
        name, error = self._loader.install_orca_ext(
            archive_path, self._extensions_dir,
        )
        if error is not None:
            self._error_dialog(guilabels.EXTENSIONS_INSTALL_FAILED % error)
            return
        self._show_status(guilabels.EXTENSIONS_INSTALLED % name)
        self._reload_data()

    def _on_uninstall_clicked(self, _btn: Gtk.Button) -> None:
        ext = self._get_selected()
        if ext is None:
            return
        if not ext.is_package:
            self._error_dialog(guilabels.EXTENSIONS_UNINSTALL_PACKAGE_ONLY)
            return
        if not self._confirm(
            guilabels.EXTENSIONS_UNINSTALL_DIALOG_TITLE,
            guilabels.EXTENSIONS_UNINSTALL_CONFIRM % ext.display_name,
            guilabels.EXTENSIONS_UNINSTALL_BTN,
        ):
            return
        if ext.instance is not None:
            self._safe_disable(ext)
        ok, error = self._loader.uninstall_extension(
            ext.dir_name, self._extensions_dir,
        )
        if not ok:
            self._error_dialog(guilabels.EXTENSIONS_UNINSTALL_FAILED % error)
            return
        self._show_status(guilabels.EXTENSIONS_UNINSTALLED % ext.display_name)
        self._reload_data()

    def _on_about_clicked(self, _btn: Gtk.Button) -> None:
        ext = self._get_selected()
        if ext is None:
            return
        lines: list[str] = []
        if ext.author:
            lines.append(f"{guilabels.EXTENSIONS_ABOUT_AUTHOR}: {ext.author}")
        if ext.version:
            lines.append(f"{guilabels.EXTENSIONS_ABOUT_VERSION}: {ext.version}")
        if ext.license:
            lines.append(f"{guilabels.EXTENSIONS_ABOUT_LICENSE}: {ext.license}")
        if ext.url:
            lines.append(f"{guilabels.EXTENSIONS_ABOUT_URL}: {ext.url}")
        if ext.module_name:
            lines.append(
                f"{guilabels.EXTENSIONS_ABOUT_MODULE}: {ext.module_name}",
            )
        lines.append(f"{guilabels.EXTENSIONS_ABOUT_PATH}: {ext.path}")
        lines.append(f"{guilabels.EXTENSIONS_ABOUT_SHA256}: {ext.current_hash}")
        if ext.description:
            lines.extend(["", ext.description])

        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=ext.display_name,
        )
        dialog.format_secondary_text("\n".join(lines))
        dialog.run()
        dialog.destroy()

    def _on_settings_clicked(self, _btn: Gtk.Button) -> None:
        ext = self._get_selected()
        if (
            ext is None
            or ext.instance is None
            or ext.preferences_style != extension.PREFERENCES_STYLE_DIALOG
        ):
            return

        try:
            controls = ext.instance.get_preference_controls()
        except Exception as error:
            debug.print_message(
                debug.LEVEL_WARNING,
                f"EXTENSIONS PREFS: {ext.module_name} "
                f"get_preference_controls raised: {error}",
                True,
            )
            self._error_dialog(guilabels.EXTENSIONS_SETTINGS_LOAD_FAILED)
            return
        if not controls:
            self._error_dialog(
                guilabels.EXTENSIONS_SETTINGS_EMPTY % ext.display_name,
            )
            return

        grid = preferences_grid_base.AutoPreferencesGrid(
            ext.display_name, controls,
        )
        grid.show_all()

        dialog, _ok_button = self._create_header_bar_dialog(
            title=guilabels.EXTENSIONS_SETTINGS_DIALOG_TITLE % ext.display_name,
            cancel_label=guilabels.DIALOG_CANCEL,
            ok_label=guilabels.BTN_SAVE,
        )
        dialog.get_content_area().pack_start(grid, True, True, 0)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            try:
                profile = profile_manager.get_manager().get_active_profile()
                grid.save_settings(profile, "")
            except Exception as error:
                debug.print_message(
                    debug.LEVEL_WARNING,
                    f"EXTENSIONS PREFS: save settings for "
                    f"{ext.module_name} failed: {error}",
                    True,
                )
        else:
            try:
                grid.revert_changes()
            except Exception as error:
                debug.print_message(
                    debug.LEVEL_WARNING,
                    f"EXTENSIONS PREFS: revert changes for "
                    f"{ext.module_name} failed: {error}",
                    True,
                )
        dialog.destroy()

    # ---- Runtime enable/disable helpers ------------------------------

    def _safe_disable(self, ext: _ExtensionInfo) -> None:
        instance = ext.instance
        if instance is None:
            return
        try:
            instance.disable()
        except Exception as error:
            debug.print_message(
                debug.LEVEL_WARNING,
                f"EXTENSIONS PREFS: disable {ext.module_name} failed: {error}",
                True,
            )

    def _safe_enable(self, ext: _ExtensionInfo) -> None:
        """Reverse a runtime disable() on a still-loaded extension."""

        instance = ext.instance
        if instance is None:
            return
        try:
            instance._disabled = False
            instance._commands_initialized = False
            instance.set_up_commands()
        except Exception as error:
            debug.print_message(
                debug.LEVEL_WARNING,
                f"EXTENSIONS PREFS: enable {ext.module_name} failed: {error}",
                True,
            )

    # ---- Confirmation / message helpers ------------------------------

    def _preview_archive(self, archive_path: str) -> str | None:
        """Peek the .orca-ext for "display — version — author" or None."""

        if not zipfile.is_zipfile(archive_path):
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="orca-ext-preview-") as tmp:
                with zipfile.ZipFile(archive_path) as zf:
                    for member in zf.namelist():
                        normalized = os.path.normpath(member)
                        if (
                            normalized.startswith(("/", ".."))
                            or os.path.isabs(normalized)
                        ):
                            return None
                    zf.extractall(tmp)

                manifest_path: str | None = None
                entries = os.listdir(tmp)
                if (
                    len(entries) == 1
                    and os.path.isdir(os.path.join(tmp, entries[0]))
                ):
                    candidate = os.path.join(tmp, entries[0], "manifest.toml")
                    if os.path.isfile(candidate):
                        manifest_path = candidate
                if manifest_path is None:
                    candidate = os.path.join(tmp, "manifest.toml")
                    if os.path.isfile(candidate):
                        manifest_path = candidate
                if manifest_path is None:
                    return None

                with open(manifest_path, "rb") as fp:
                    data = tomllib.load(fp)
                ext_section = data.get("extension", {}) or {}
                display = (
                    ext_section.get("display-name")
                    or ext_section.get("name")
                    or os.path.basename(archive_path)
                )
                bits = [str(display)]
                if ext_section.get("version"):
                    bits.append(str(ext_section["version"]))
                if ext_section.get("author"):
                    bits.append(str(ext_section["author"]))
                return " — ".join(bits)
        except (OSError, zipfile.BadZipFile, tomllib.TOMLDecodeError):
            return None

    def _confirm(self, title: str, message: str, ok_label: str) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=title,
        )
        dialog.format_secondary_text(message)
        dialog.add_button(guilabels.DIALOG_CANCEL, Gtk.ResponseType.CANCEL)
        ok = dialog.add_button(ok_label, Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        ok.grab_focus()
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _error_dialog(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=message,
        )
        dialog.run()
        dialog.destroy()

    def _show_status(self, message: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(message)

    # ---- PreferencesGridBase contract --------------------------------

    def reload(self) -> None:
        self._has_unsaved_changes = False
        self._reload_data()

    def save_settings(
        self, _profile: str = "", _app_name: str = "",
    ) -> dict[str, list[str]]:
        # All mutations are committed to dconf immediately.
        self._has_unsaved_changes = False
        return {}

    def revert_changes(self) -> None:
        # No in-memory pending state to revert.
        return


def _approval_state(current_hash: str, approved_hash: str | None) -> str:
    if approved_hash is None:
        return _APPROVAL_UNAPPROVED
    if approved_hash != current_hash:
        return _APPROVAL_MODIFIED
    return _APPROVAL_OK


_grid: ExtensionsPreferencesGrid | None = None


def create_preferences_grid() -> ExtensionsPreferencesGrid:
    """Returns a fresh ExtensionsPreferencesGrid for the prefs window."""

    # Always return a new grid so each preferences window opens fresh.
    return ExtensionsPreferencesGrid()
