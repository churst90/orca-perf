# Orca
#
# Copyright 2005-2009 Sun Microsystems Inc.
# Copyright 2011-2025 Igalia, S.L.
# Author: Joanmarie Diggs <jdiggs@igalia.com>
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

# pylint: disable=too-many-lines
# pylint: disable=too-many-arguments
# pylint: disable=too-many-positional-arguments
# pylint: disable=too-many-public-methods

"""Implements structural navigation."""

from __future__ import annotations

import functools
import threading
import time
from enum import Enum
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from . import (
    cmdnames,
    command_manager,
    dbus_service,
    debug,
    focus_manager,
    gsettings_registry,
    guilabels,
    input_event_manager,
    keybindings,
    live_region_presenter,
    messages,
    object_properties,
    orca_gui_navlist,
    presentation_manager,
    say_all_presenter,
    script_manager,
)
from .ax_hypertext import AXHypertext
from .ax_object import AXObject
from .util.debounce import DebouncedCallable
from .ax_table import AXTable
from .ax_text import AXText
from .ax_utilities import AXUtilities
from .command import Command, KeyboardCommand
from .extension import Extension
from .structural_navigator_registry import get_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from .dbus_service import UInt32
    from .input_event import InputEvent
    from .scripts import default


class NavigationMode(Enum):
    """Represents the structural navigation modes available."""

    OFF = "OFF"
    DOCUMENT = "DOCUMENT"
    GUI = "GUI"


@gsettings_registry.get_registry().gsettings_schema(
    "org.gnome.Orca.StructuralNavigation",
    name="structural-navigation",
)
class StructuralNavigator(Extension):
    """Implements the structural navigation support available to scripts."""

    _SCHEMA = "structural-navigation"
    KEY_WRAPS = "wraps"
    KEY_LARGE_OBJECT_TEXT_LENGTH = "large-object-text-length"
    KEY_ENABLED = "enabled"
    KEY_TRIGGERS_FOCUS_MODE = "triggers-focus-mode"
    KEY_SKIP_UNLABELED_IMAGES = "skip-unlabeled-images"

    def _get_setting(self, key: str, gtype: str, default: Any) -> Any:
        """Returns the dconf value for key, or default if not in dconf."""

        return gsettings_registry.get_registry().layered_lookup(
            self._SCHEMA,
            key,
            gtype,
            default=default,
        )

    GROUP_LABEL = guilabels.KB_GROUP_STRUCTURAL_NAVIGATION

    @staticmethod
    def navigation_command(func):
        """Decorator that logs the command, then dispatches to it.

        Mirrors the decorator JD added in caret_navigator /
        math_navigator / object_navigator / table_navigator. Applies
        only to the handful of structural-navigator commands that
        AREN'T routed through the generic _dispatch_{previous,next,
        list} pipeline -- the per-element-type shims already log
        inside the dispatcher and don't need the wrapper.
        """

        @functools.wraps(func)
        def wrapper(self, script, event=None, notify_user=True) -> bool:
            tokens = [
                "STRUCTURAL NAVIGATOR:",
                func,
                "\nScript:",
                script,
                "\nEvent:",
                event,
                "\nnotify_user:",
                notify_user,
            ]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return func(self, script, event, notify_user)

        return wrapper

    def __init__(self) -> None:
        self._last_input_event: InputEvent | None = None

        # To make it possible for focus mode to suspend this navigation without
        # changing the user's preferred setting.
        self._suspended: bool = False
        self._mode_for_script: dict[default.Script, NavigationMode] = {}
        self._previous_mode_for_script: dict[default.Script, NavigationMode] = {}

        # Per-root cache of structural-nav matches. Without this every H/K/F/B
        # keystroke walks the entire AT-SPI tree to find candidates; on heavy
        # web pages (hundreds of headings, thousands of links) that walk costs
        # tens of milliseconds. Key: (hash(root), cache_key); value: list of
        # accessibles. Invalidated when the tree under root changes -- see
        # invalidate_nav_cache_for_event().
        self._nav_cache: dict[tuple[int, str], list[Atspi.Accessible]] = {}
        # Parallel to _nav_cache: stores a zero-arg callable that recomputes
        # the value for the same key. Used by the background rebuild path
        # below so we can refresh stale entries without dropping them first.
        self._nav_cache_rebuilders: dict[
            tuple[int, str], Callable[[], list[Atspi.Accessible]]
        ] = {}
        self._nav_cache_lock = threading.Lock()
        self._NAV_CACHE_MAX = 200
        self._nav_cache_hits = 0
        self._nav_cache_misses = 0

        # Deferred-rebuild state. Children-changed events from a dynamic page
        # (live regions, infinite scroll, ad refresh) often fire in dense
        # bursts; dropping the cache synchronously on each one means every
        # H/K/B keypress during the burst pays a full tree walk. We mark
        # affected keys stale, schedule a single 150ms GLib timer, and when
        # it fires, schedule each stale entry's rebuild on idle priority.
        # Reads keep serving the cached lists in the meantime, and by the
        # time the next H/K press lands the rebuild is usually done so the
        # press is served instantly from the now-fresh cache.
        self._nav_cache_stale_keys: set[tuple[int, str]] = set()
        self._NAV_CACHE_DROP_DELAY_MS = 150
        self._nav_cache_drop_debouncer = DebouncedCallable(self._drop_stale_keys)
        # Keys that are queued for idle rebuild. Bounds the work even if
        # bursts keep marking entries stale faster than idle ticks fire.
        self._nav_cache_rebuilding: set[tuple[int, str]] = set()

        # Coalesce-with-delay state for held-key structural nav. Each
        # script.present_object() call below in _present_object does a
        # scroll-to-center which blocks the main thread in
        # time.sleep(0.05) up to 3 times (ax_event_synthesizer.py:403). At
        # 30Hz key auto-repeat the main thread backs up and exceeds the
        # 6 sec systemd watchdog, killing Orca. We coalesce the final
        # present (scroll + speech) when a burst is detected; focus and
        # cursor still move on each press via emit_region_changed.
        self._present_debouncer = DebouncedCallable(self._present_fire)
        self._present_pending: tuple | None = None
        self._last_present_time: float = 0.0
        self._PRESENT_BURST_WINDOW = 0.10  # 100ms
        self._PRESENT_DEFER_MS = 50

        super().__init__()

        # Populate the ElementType registry (Phase 2 step 3).
        # Imported locally to break the circular import: this module
        # is imported transitively from structural_navigator_builtins
        # via NavigationMode.
        from .structural_navigator_builtins import (  # pylint: disable=import-outside-toplevel
            register_builtins,
        )
        register_builtins(self)

    def _cached_or_compute(
        self,
        root: Atspi.Accessible,
        cache_key: str,
        compute_fn: Callable[[], list[Atspi.Accessible]],
    ) -> list[Atspi.Accessible]:
        """Returns cached matches keyed by (root, cache_key) or computes and stores them."""

        if root is None:
            return compute_fn()
        full_key = (hash(root), cache_key)
        with self._nav_cache_lock:
            cached = self._nav_cache.get(full_key)
        if cached is not None:
            self._nav_cache_hits += 1
            self._log_nav_cache(cache_key, hit=True, n=len(cached))
            return cached
        t0 = time.monotonic()
        result = compute_fn()
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        with self._nav_cache_lock:
            if len(self._nav_cache) >= self._NAV_CACHE_MAX:
                self._nav_cache.clear()
                self._nav_cache_rebuilders.clear()
            self._nav_cache[full_key] = result
            self._nav_cache_rebuilders[full_key] = compute_fn
        self._nav_cache_misses += 1
        self._log_nav_cache(cache_key, hit=False, n=len(result), elapsed_ms=elapsed_ms)
        return result

    def _log_nav_cache(
        self,
        cache_key: str,
        hit: bool,
        n: int,
        elapsed_ms: float = 0.0,
    ) -> None:
        """Append a line to ~/orca-perf.log when ORCA_PERF_LOG=1."""

        from .ax_object import _perf_log_write
        if hit:
            _perf_log_write(f"nav_cache[{cache_key}] HIT n={n}")
        else:
            _perf_log_write(f"nav_cache[{cache_key}] MISS n={n} fetch={elapsed_ms:.1f}ms")

    def invalidate_nav_cache_for_event(
        self,
        event_type: str,
        source: Atspi.Accessible,
    ) -> None:
        """Invalidates the structural-nav cache based on an AT-SPI event.

        Defunct events drop entries rooted at the source immediately
        (the root itself is gone, no point serving stale matches under it).
        Children-changed events mark ancestor-rooted entries as stale and
        defer the drop until the burst settles -- see _drop_stale_keys().
        """

        if not event_type or source is None:
            return

        if event_type == "object:defunct":
            source_hash = hash(source)
            with self._nav_cache_lock:
                doomed = [k for k in self._nav_cache if k[0] == source_hash]
                for k in doomed:
                    self._nav_cache.pop(k, None)
                    self._nav_cache_rebuilders.pop(k, None)
                    self._nav_cache_stale_keys.discard(k)
                    self._nav_cache_rebuilding.discard(k)
            return

        if not event_type.startswith("object:children-changed"):
            return

        # Walk up from source; any cached entry whose root is an ancestor is
        # now stale. get_parent() is itself cached (long-lived) so this walk
        # is cheap.
        ancestor_hashes: set[int] = set()
        current = source
        depth = 0
        while current is not None and depth < 100:
            ancestor_hashes.add(hash(current))
            current = AXObject.get_parent(current)
            depth += 1
        if not ancestor_hashes:
            return
        with self._nav_cache_lock:
            for k in self._nav_cache:
                if k[0] in ancestor_hashes:
                    self._nav_cache_stale_keys.add(k)
        self._nav_cache_drop_debouncer.arm(self._NAV_CACHE_DROP_DELAY_MS)

    def _drop_stale_keys(self) -> bool:
        """GLib timer callback: schedule background rebuild for stale keys.

        Instead of dropping entries (which would force the next H/K press
        to pay the full tree walk synchronously), schedule each stale
        key's recompute on idle priority. The cache keeps serving the
        old list to any reads in between. Once the idle handler runs
        compute_fn and atomically swaps the new list in, subsequent
        reads see the fresh result with no user-visible recompute cost.
        """

        with self._nav_cache_lock:
            stale = list(self._nav_cache_stale_keys)
            self._nav_cache_stale_keys.clear()

            # Build the list of (key, rebuilder) pairs we can refresh
            # in place. Drop keys with no rebuilder -- those were never
            # populated via _cached_or_compute (defunct edge case).
            to_rebuild: list[tuple[tuple[int, str], Callable[[], list[Atspi.Accessible]]]] = []
            for k in stale:
                rebuilder = self._nav_cache_rebuilders.get(k)
                if rebuilder is None:
                    self._nav_cache.pop(k, None)
                    continue
                if k in self._nav_cache_rebuilding:
                    continue
                self._nav_cache_rebuilding.add(k)
                to_rebuild.append((k, rebuilder))

        for k, rebuilder in to_rebuild:
            GLib.idle_add(self._rebuild_cache_entry, k, rebuilder)
        self._log_nav_cache("schedule_rebuild", hit=False, n=len(to_rebuild))
        return False  # one-shot

    def _rebuild_cache_entry(
        self,
        full_key: tuple[int, str],
        rebuilder: Callable[[], list[Atspi.Accessible]],
    ) -> bool:
        """GLib idle callback: recompute one cache entry and swap it in."""

        try:
            t0 = time.monotonic()
            fresh = rebuilder()
            elapsed_ms = (time.monotonic() - t0) * 1000.0
        except Exception:  # pylint: disable=broad-except
            # If the rebuilder raises (root went defunct mid-rebuild,
            # toolkit threw), drop the entry so the next read does the
            # work on the foreground path with proper error handling.
            with self._nav_cache_lock:
                self._nav_cache.pop(full_key, None)
                self._nav_cache_rebuilders.pop(full_key, None)
                self._nav_cache_rebuilding.discard(full_key)
            return False

        with self._nav_cache_lock:
            # If the entry was evicted while we were rebuilding (defunct
            # event hit it, cache wraparound), discard the result rather
            # than re-inserting -- another reader has moved on.
            if full_key in self._nav_cache_rebuilding:
                self._nav_cache[full_key] = fresh
                self._nav_cache_rebuilding.discard(full_key)
        self._log_nav_cache(full_key[1], hit=False, n=len(fresh), elapsed_ms=elapsed_ms)
        return False  # one-shot

    # pylint: disable-next=too-many-locals
    def _get_commands(self) -> list[Command]:
        # Navigation bindings - (key, prev_mod, next_mod, list_mod, base_name)
        nav_bindings = [
            (
                "q",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "blockquote",
            ),
            (
                "b",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "button",
            ),
            (
                "x",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "checkbox",
            ),
            (
                "c",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "combobox",
            ),
            (
                "e",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "entry",
            ),
            (
                "f",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "form_field",
            ),
            (
                "h",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "heading",
            ),
            (
                "g",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "image",
            ),
            (
                "m",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "landmark",
            ),
            (
                "l",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "list",
            ),
            (
                "i",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "list_item",
            ),
            (
                "p",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "paragraph",
            ),
            (
                "r",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "radio_button",
            ),
            (
                "t",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "table",
            ),
            (
                "k",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "link",
            ),
            (
                "u",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "unvisited_link",
            ),
            (
                "v",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "visited_link",
            ),
            (
                "o",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "large_object",
            ),
            (
                "a",
                keybindings.SHIFT_MODIFIER_MASK,
                keybindings.NO_MODIFIER_MASK,
                keybindings.SHIFT_ALT_MODIFIER_MASK,
                "clickable",
            ),
        ]

        # Build command name -> keybinding mapping
        cmd_bindings: dict[str, keybindings.KeyBinding | None] = {}
        for key, prev_mod, next_mod, list_mod, base_name in nav_bindings:
            cmd_bindings[f"previous_{base_name}"] = keybindings.KeyBinding(key, prev_mod)
            cmd_bindings[f"next_{base_name}"] = keybindings.KeyBinding(key, next_mod)
            if base_name == "entry":
                plural = "entries"
            elif base_name in ("checkbox", "combobox"):
                plural = f"{base_name}es"
            else:
                plural = f"{base_name}s"
            cmd_bindings[f"list_{plural}"] = keybindings.KeyBinding(key, list_mod)

        cmd_bindings["previous_separator"] = keybindings.KeyBinding(
            "s", keybindings.SHIFT_MODIFIER_MASK
        )
        cmd_bindings["next_separator"] = keybindings.KeyBinding("s", keybindings.NO_MODIFIER_MASK)
        cmd_bindings["previous_live_region"] = keybindings.KeyBinding(
            "d", keybindings.SHIFT_MODIFIER_MASK
        )
        cmd_bindings["next_live_region"] = keybindings.KeyBinding("d", keybindings.NO_MODIFIER_MASK)
        cmd_bindings["last_live_region"] = keybindings.KeyBinding("y", keybindings.NO_MODIFIER_MASK)
        cmd_bindings["container_start"] = keybindings.KeyBinding(
            "comma", keybindings.SHIFT_MODIFIER_MASK
        )
        cmd_bindings["container_end"] = keybindings.KeyBinding(
            "comma", keybindings.NO_MODIFIER_MASK
        )
        cmd_bindings["previous_annotation"] = None
        cmd_bindings["next_annotation"] = None
        cmd_bindings["list_annotations"] = None
        cmd_bindings["previous_iframe"] = None
        cmd_bindings["next_iframe"] = None
        cmd_bindings["list_iframes"] = None

        commands_data = [
            ("previous_annotation", self.previous_annotation, cmdnames.ANNOTATION_PREV),
            ("next_annotation", self.next_annotation, cmdnames.ANNOTATION_NEXT),
            ("list_annotations", self.list_annotations, cmdnames.ANNOTATION_LIST),
            ("previous_blockquote", self.previous_blockquote, cmdnames.BLOCKQUOTE_PREV),
            ("next_blockquote", self.next_blockquote, cmdnames.BLOCKQUOTE_NEXT),
            ("list_blockquotes", self.list_blockquotes, cmdnames.BLOCKQUOTE_LIST),
            ("previous_button", self.previous_button, cmdnames.BUTTON_PREV),
            ("next_button", self.next_button, cmdnames.BUTTON_NEXT),
            ("list_buttons", self.list_buttons, cmdnames.BUTTON_LIST),
            ("previous_checkbox", self.previous_checkbox, cmdnames.CHECK_BOX_PREV),
            ("next_checkbox", self.next_checkbox, cmdnames.CHECK_BOX_NEXT),
            ("list_checkboxes", self.list_checkboxes, cmdnames.CHECK_BOX_LIST),
            ("previous_combobox", self.previous_combobox, cmdnames.COMBO_BOX_PREV),
            ("next_combobox", self.next_combobox, cmdnames.COMBO_BOX_NEXT),
            ("list_comboboxes", self.list_comboboxes, cmdnames.COMBO_BOX_LIST),
            ("previous_entry", self.previous_entry, cmdnames.ENTRY_PREV),
            ("next_entry", self.next_entry, cmdnames.ENTRY_NEXT),
            ("list_entries", self.list_entries, cmdnames.ENTRY_LIST),
            ("previous_form_field", self.previous_form_field, cmdnames.FORM_FIELD_PREV),
            ("next_form_field", self.next_form_field, cmdnames.FORM_FIELD_NEXT),
            ("list_form_fields", self.list_form_fields, cmdnames.FORM_FIELD_LIST),
            ("previous_heading", self.previous_heading, cmdnames.HEADING_PREV),
            ("next_heading", self.next_heading, cmdnames.HEADING_NEXT),
            ("list_headings", self.list_headings, cmdnames.HEADING_LIST),
            ("previous_iframe", self.previous_iframe, cmdnames.IFRAME_PREV),
            ("next_iframe", self.next_iframe, cmdnames.IFRAME_NEXT),
            ("list_iframes", self.list_iframes, cmdnames.IFRAME_LIST),
            ("previous_image", self.previous_image, cmdnames.IMAGE_PREV),
            ("next_image", self.next_image, cmdnames.IMAGE_NEXT),
            ("list_images", self.list_images, cmdnames.IMAGE_LIST),
            ("previous_landmark", self.previous_landmark, cmdnames.LANDMARK_PREV),
            ("next_landmark", self.next_landmark, cmdnames.LANDMARK_NEXT),
            ("list_landmarks", self.list_landmarks, cmdnames.LANDMARK_LIST),
            ("previous_list", self.previous_list, cmdnames.LIST_PREV),
            ("next_list", self.next_list, cmdnames.LIST_NEXT),
            ("list_lists", self.list_lists, cmdnames.LIST_LIST),
            ("previous_list_item", self.previous_list_item, cmdnames.LIST_ITEM_PREV),
            ("next_list_item", self.next_list_item, cmdnames.LIST_ITEM_NEXT),
            ("list_list_items", self.list_list_items, cmdnames.LIST_ITEM_LIST),
            ("previous_live_region", self.previous_live_region, cmdnames.LIVE_REGION_PREV),
            ("next_live_region", self.next_live_region, cmdnames.LIVE_REGION_NEXT),
            ("last_live_region", self._last_live_region, cmdnames.LIVE_REGION_LAST),
            ("previous_paragraph", self.previous_paragraph, cmdnames.PARAGRAPH_PREV),
            ("next_paragraph", self.next_paragraph, cmdnames.PARAGRAPH_NEXT),
            ("list_paragraphs", self.list_paragraphs, cmdnames.PARAGRAPH_LIST),
            ("previous_radio_button", self.previous_radio_button, cmdnames.RADIO_BUTTON_PREV),
            ("next_radio_button", self.next_radio_button, cmdnames.RADIO_BUTTON_NEXT),
            ("list_radio_buttons", self.list_radio_buttons, cmdnames.RADIO_BUTTON_LIST),
            ("previous_separator", self.previous_separator, cmdnames.SEPARATOR_PREV),
            ("next_separator", self.next_separator, cmdnames.SEPARATOR_NEXT),
            ("previous_table", self.previous_table, cmdnames.TABLE_PREV),
            ("next_table", self.next_table, cmdnames.TABLE_NEXT),
            ("list_tables", self.list_tables, cmdnames.TABLE_LIST),
            ("previous_link", self.previous_link, cmdnames.LINK_PREV),
            ("next_link", self.next_link, cmdnames.LINK_NEXT),
            ("list_links", self.list_links, cmdnames.LINK_LIST),
            ("previous_unvisited_link", self.previous_unvisited_link, cmdnames.UNVISITED_LINK_PREV),
            ("next_unvisited_link", self.next_unvisited_link, cmdnames.UNVISITED_LINK_NEXT),
            ("list_unvisited_links", self.list_unvisited_links, cmdnames.UNVISITED_LINK_LIST),
            ("previous_visited_link", self.previous_visited_link, cmdnames.VISITED_LINK_PREV),
            ("next_visited_link", self.next_visited_link, cmdnames.VISITED_LINK_NEXT),
            ("list_visited_links", self.list_visited_links, cmdnames.VISITED_LINK_LIST),
            ("previous_large_object", self.previous_large_object, cmdnames.LARGE_OBJECT_PREV),
            ("next_large_object", self.next_large_object, cmdnames.LARGE_OBJECT_NEXT),
            ("list_large_objects", self.list_large_objects, cmdnames.LARGE_OBJECT_LIST),
            ("previous_clickable", self.previous_clickable, cmdnames.CLICKABLE_PREV),
            ("next_clickable", self.next_clickable, cmdnames.CLICKABLE_NEXT),
            ("list_clickables", self.list_clickables, cmdnames.CLICKABLE_LIST),
            ("container_start", self.container_start, cmdnames.CONTAINER_START),
            ("container_end", self.container_end, cmdnames.CONTAINER_END),
        ]

        kb_z = keybindings.KeyBinding("z", keybindings.ORCA_MODIFIER_MASK)
        commands: list[Command] = [
            KeyboardCommand(
                "structural_navigator_mode_cycle",
                self.cycle_mode,
                self.GROUP_LABEL,
                cmdnames.STRUCTURAL_NAVIGATION_MODE_CYCLE,
                desktop_keybinding=kb_z,
                laptop_keybinding=kb_z,
                is_group_toggle=True,
            ),
        ]

        for name, function, description in commands_data:
            kb = cmd_bindings.get(name)
            commands.append(
                KeyboardCommand(
                    name,
                    function,
                    self.GROUP_LABEL,
                    description,
                    desktop_keybinding=kb,
                    laptop_keybinding=kb,
                ),
            )

        for i in range(1, 7):
            kb_prev = keybindings.KeyBinding(str(i), keybindings.SHIFT_MODIFIER_MASK)
            kb_next = keybindings.KeyBinding(str(i), keybindings.NO_MODIFIER_MASK)
            kb_list = keybindings.KeyBinding(str(i), keybindings.SHIFT_ALT_MODIFIER_MASK)

            heading_commands = [
                (
                    f"previous_heading_level_{i}",
                    getattr(self, f"previous_heading_level_{i}"),
                    cmdnames.HEADING_AT_LEVEL_PREV % i,
                    kb_prev,
                ),
                (
                    f"next_heading_level_{i}",
                    getattr(self, f"next_heading_level_{i}"),
                    cmdnames.HEADING_AT_LEVEL_NEXT % i,
                    kb_next,
                ),
                (
                    f"list_headings_level_{i}",
                    getattr(self, f"list_headings_level_{i}"),
                    cmdnames.HEADING_AT_LEVEL_LIST % i,
                    kb_list,
                ),
            ]
            for name, function, description, kb in heading_commands:
                commands.append(
                    KeyboardCommand(
                        name,
                        function,
                        self.GROUP_LABEL,
                        description,
                        desktop_keybinding=kb,
                        laptop_keybinding=kb,
                    ),
                )

        return commands

    def _is_active_script(self, script):
        active_script = script_manager.get_manager().get_active_script()
        if active_script == script:
            return True

        tokens = ["STRUCTURAL NAVIGATOR:", script, "is not the active script", active_script]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        return False

    def get_mode(self, script: default.Script) -> NavigationMode:
        """Returns the current structural-navigator mode associated with script."""

        mode = self._mode_for_script.get(script, NavigationMode.OFF)
        tokens = ["STRUCTURAL NAVIGATOR: Mode for", script, f"is {mode}"]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        return mode

    def set_mode(self, script: default.Script, mode: NavigationMode) -> None:
        """Sets the structural-navigator mode."""

        tokens = ["STRUCTURAL NAVIGATOR: Setting mode for", script, f"to {mode}"]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        self._mode_for_script[script] = mode

        if not (script and self._is_active_script(script)):
            return

        # Use the per-script mode combined with the user's preference to determine
        # whether commands should be active, without overwriting the preference.
        effective = mode != NavigationMode.OFF and self.get_is_enabled()
        command_manager.get_manager().set_group_enabled(
            guilabels.KB_GROUP_STRUCTURAL_NAVIGATION,
            effective,
        )

    def last_input_event_was_navigation_command(self) -> bool:
        """Returns true if the last input event was a navigation command."""

        if self._last_input_event is None:
            return False

        manager = input_event_manager.get_manager()
        result = manager.last_event_equals_or_is_release_for_event(self._last_input_event)
        if self._last_input_event is not None:
            string = self._last_input_event.as_single_line_string()
        else:
            string = "None"

        msg = (
            f"STRUCTURAL NAVIGATOR: Last navigation event ({string}) is last input event: {result}"
        )
        debug.print_message(debug.LEVEL_INFO, msg, True)
        return result

    def last_command_prevents_focus_mode(self) -> bool:
        """Returns True if the last command was navigation but the setting disallows focus mode."""

        if not self.last_input_event_was_navigation_command():
            return False

        return not self.get_triggers_focus_mode()

    @gsettings_registry.get_registry().gsetting(
        key=KEY_WRAPS,
        schema="structural-navigation",
        gtype="b",
        default=True,
        summary="Wrap when reaching top/bottom",
        migration_key="wrappedStructuralNavigation",
    )
    @dbus_service.getter
    def get_navigation_wraps(self) -> bool:
        """Returns whether navigation wraps when reaching the top/bottom of the document."""

        return self._get_setting(self.KEY_WRAPS, "b", True)

    @dbus_service.setter
    def set_navigation_wraps(self, value: bool) -> bool:
        """Sets whether navigation wraps when reaching the top/bottom of the document."""

        msg = f"STRUCTURAL NAVIGATOR: Setting navigation wraps to {value}."
        debug.print_message(debug.LEVEL_INFO, msg, True)
        gsettings_registry.get_registry().set_runtime_value(self._SCHEMA, self.KEY_WRAPS, value)
        return True

    @gsettings_registry.get_registry().gsetting(
        key=KEY_LARGE_OBJECT_TEXT_LENGTH,
        schema="structural-navigation",
        gtype="i",
        default=75,
        summary="Minimum text length for large objects",
        migration_key="largeObjectTextLength",
    )
    @dbus_service.getter
    def get_large_object_text_length(self) -> UInt32:
        """Returns the minimum number of characters to be considered a 'large object'."""

        return self._get_setting(self.KEY_LARGE_OBJECT_TEXT_LENGTH, "i", 75)

    @dbus_service.setter
    def set_large_object_text_length(self, value: UInt32) -> bool:
        """Sets the minimum number of characters to be considered a 'large object'."""

        msg = f"STRUCTURAL NAVIGATOR: Setting large object text length to {value}."
        debug.print_message(debug.LEVEL_INFO, msg, True)
        gsettings_registry.get_registry().set_runtime_value(
            self._SCHEMA,
            self.KEY_LARGE_OBJECT_TEXT_LENGTH,
            value,
        )
        return True

    @gsettings_registry.get_registry().gsetting(
        key=KEY_ENABLED,
        schema="structural-navigation",
        gtype="b",
        default=True,
        summary="Enable structural navigation",
        migration_key="structuralNavigationEnabled",
    )
    @dbus_service.getter
    def get_is_enabled(self) -> bool:
        """Returns whether structural navigation is enabled."""

        return self._get_setting(self.KEY_ENABLED, "b", True)

    @dbus_service.setter
    def set_is_enabled(self, value: bool) -> bool:
        """Sets whether structural navigation is enabled."""

        if self.get_is_enabled() == value:
            msg = f"STRUCTURAL NAVIGATOR: Enabled already {value}. Refreshing command group."
            debug.print_message(debug.LEVEL_INFO, msg, True)
            command_manager.get_manager().set_group_enabled(
                guilabels.KB_GROUP_STRUCTURAL_NAVIGATION,
                value,
            )
            return True

        msg = f"STRUCTURAL NAVIGATOR: Setting enabled to {value}."
        debug.print_message(debug.LEVEL_INFO, msg, True)
        gsettings_registry.get_registry().set_runtime_value(
            self._SCHEMA,
            self.KEY_ENABLED,
            value,
        )

        script = script_manager.get_manager().get_active_script()
        if not script:
            return True

        current_mode = self.get_mode(script)
        if not value and current_mode == NavigationMode.OFF:
            return True

        self._last_input_event = None
        if value:
            if previous_mode := self._previous_mode_for_script.get(script):
                tokens = ["STRUCTURAL NAVIGATOR: Restoring mode for", script, "to", previous_mode]
                debug.print_tokens(debug.LEVEL_INFO, tokens, True)
                self._mode_for_script[script] = previous_mode
        else:
            self._previous_mode_for_script[script] = current_mode
            tokens = ["STRUCTURAL NAVIGATOR: Saving", current_mode, "as previous mode for", script]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            self._mode_for_script[script] = NavigationMode.OFF

        command_manager.get_manager().set_group_enabled(
            guilabels.KB_GROUP_STRUCTURAL_NAVIGATION,
            value,
        )
        return True

    @gsettings_registry.get_registry().gsetting(
        key=KEY_TRIGGERS_FOCUS_MODE,
        schema="structural-navigation",
        gtype="b",
        default=False,
        summary="Structural navigation triggers focus mode",
        migration_key="structNavTriggersFocusMode",
    )
    @dbus_service.getter
    def get_triggers_focus_mode(self) -> bool:
        """Returns whether structural navigation triggers focus mode."""

        return self._get_setting(self.KEY_TRIGGERS_FOCUS_MODE, "b", False)

    @dbus_service.setter
    def set_triggers_focus_mode(self, value: bool) -> bool:
        """Sets whether structural navigation triggers focus mode."""

        if self.get_triggers_focus_mode() == value:
            return True

        msg = f"STRUCTURAL NAVIGATOR: Setting triggers focus mode to {value}."
        debug.print_message(debug.LEVEL_INFO, msg, True)
        gsettings_registry.get_registry().set_runtime_value(
            self._SCHEMA,
            self.KEY_TRIGGERS_FOCUS_MODE,
            value,
        )
        return True

    @gsettings_registry.get_registry().gsetting(
        key=KEY_SKIP_UNLABELED_IMAGES,
        schema="structural-navigation",
        gtype="b",
        default=False,
        summary="Skip unlabeled images during navigation",
    )
    @dbus_service.getter
    def get_skip_unlabeled_images(self) -> bool:
        """Returns whether unlabeled images are skipped during navigation."""

        return self._get_setting(self.KEY_SKIP_UNLABELED_IMAGES, "b", False)

    @dbus_service.setter
    def set_skip_unlabeled_images(self, value: bool) -> bool:
        """Sets whether unlabeled images are skipped during navigation."""

        msg = f"STRUCTURAL NAVIGATOR: Setting skip unlabeled images to {value}."
        debug.print_message(debug.LEVEL_INFO, msg, True)
        gsettings_registry.get_registry().set_runtime_value(
            self._SCHEMA,
            self.KEY_SKIP_UNLABELED_IMAGES,
            value,
        )
        return True

    @dbus_service.command
    @navigation_command
    def cycle_mode(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Cycles among the structural navigation modes."""

        if not (script and self._is_active_script(script)):
            return False

        self._last_input_event = None
        previous_mode = self.get_mode(script)
        msg = ""
        mode = None
        if previous_mode == NavigationMode.OFF:
            mode = NavigationMode.DOCUMENT
            msg = messages.STRUCTURAL_NAVIGATION_KEYS_DOCUMENT
        elif previous_mode == NavigationMode.GUI:
            mode = NavigationMode.OFF
            msg = messages.STRUCTURAL_NAVIGATION_KEYS_OFF
        else:
            mode = NavigationMode.GUI
            msg = messages.STRUCTURAL_NAVIGATION_KEYS_GUI

        if notify_user:
            presentation_manager.get_manager().present_message(msg)
        self.set_mode(script, mode)
        if mode == NavigationMode.DOCUMENT:
            root = self._determine_root_container(script)
            if not AXObject.supports_collection(root) and notify_user:
                presentation_manager.get_manager().present_message(
                    messages.STRUCTURAL_NAVIGATION_NOT_SUPPORTED_FULL,
                    messages.STRUCTURAL_NAVIGATION_NOT_SUPPORTED_BRIEF,
                )
        return True

    def suspend_commands(self, script, suspended, reason=""):
        """Suspends structural navigation independent of the enabled setting."""

        if not (script and self._is_active_script(script)):
            return

        msg = f"STRUCTURAL NAVIGATOR: Suspended: {suspended}"
        if reason:
            msg += f": {reason}"
        debug.print_message(debug.LEVEL_INFO, msg, True)

        self._suspended = suspended
        command_manager.get_manager().set_group_suspended(
            guilabels.KB_GROUP_STRUCTURAL_NAVIGATION,
            suspended,
        )

    def _get_container_for_nested_item(self, obj: Atspi.Accessible) -> Atspi.Accessible:
        # If an author put an ARIA heading inside a native heading (or vice versa), obj
        # could be the inner heading. If we treat the outer heading as as the previous heading
        # and then set the caret context to the first position inside the outer heading, i.e.
        # the inner heading, we'll get stuck. Thanks authors.
        if AXUtilities.is_heading(obj):
            if ancestor := AXUtilities.find_ancestor(obj, AXUtilities.is_heading):
                tokens = [
                    "STRUCTURAL NAVIGATOR: Current heading",
                    obj,
                    "is inside another heading",
                    ancestor,
                    "Treating the outer heading as current.",
                ]
                debug.print_tokens(debug.LEVEL_INFO, tokens, True)
                return ancestor
            return obj

        candidate = obj
        if AXUtilities.is_live_region(obj):
            while ancestor := AXUtilities.find_ancestor(candidate, AXUtilities.is_live_region):
                candidate = ancestor
            if candidate != obj:
                tokens = [
                    "STRUCTURAL NAVIGATOR: Current live region",
                    obj,
                    "is inside another ",
                    "live region",
                    candidate,
                    "Treating the outer region as current.",
                ]
                debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        return candidate

    @staticmethod
    def _get_adjacent_or_wrap(
        objects: list[Atspi.Accessible],
        index: int,
        is_next: bool,
        should_wrap: bool,
        notify_user: bool,
    ) -> Atspi.Accessible | None:
        """Returns the adjacent object in the list, wrapping if enabled."""

        if is_next:
            if index + 1 < len(objects):
                return objects[index + 1]
            wrap_msg = messages.WRAPPING_TO_TOP
            wrap_target = objects[0]
        else:
            if index > 0:
                return objects[index - 1]
            wrap_msg = messages.WRAPPING_TO_BOTTOM
            wrap_target = objects[-1]

        if not should_wrap:
            return None
        if notify_user:
            presentation_manager.get_manager().present_message(wrap_msg)
        return wrap_target

    def _get_object_in_direction(
        self,
        script: default.Script,
        objects: list[Atspi.Accessible],
        is_next: bool,
        should_wrap: bool | None = None,
        notify_user: bool = True,
    ) -> Atspi.Accessible | None:
        """Returns the next/previous object in relation to the current location."""

        if not objects:
            return None

        if should_wrap is None:
            should_wrap = self.get_navigation_wraps()

        # If we're in a matching object, return the next/previous one in the list.
        obj = focus_manager.get_manager().get_locus_of_focus()
        candidate = obj
        while candidate:
            if candidate not in objects:
                candidate = AXObject.get_parent(candidate)
                continue

            if not is_next:
                alternative = self._get_container_for_nested_item(candidate)
                if alternative in objects:
                    candidate = alternative

            index = objects.index(candidate)
            return self._get_adjacent_or_wrap(
                objects,
                index,
                is_next,
                should_wrap,
                notify_user,
            )

        # If we're not in a matching object, find the next/previous one based on the path.
        if not is_next:
            objects.reverse()

        current_path = AXObject.get_path(obj)
        for match in objects:
            path = AXObject.get_path(match)
            comparison = script.utilities.path_comparison(path, current_path)
            # A descendant of the focused object is always "after" it in path terms,
            # but the caret may have already moved past that descendant's location.
            if comparison > 0 and self._caret_is_past_descendant(obj, match):
                comparison = -1
            if (comparison > 0 and is_next) or (comparison < 0 and not is_next):
                return match

        if not should_wrap:
            return None

        wrap_msg = messages.WRAPPING_TO_TOP if is_next else messages.WRAPPING_TO_BOTTOM
        if notify_user:
            presentation_manager.get_manager().present_message(wrap_msg)
        return objects[0] if obj != objects[0] else None

    def _caret_is_past_descendant(
        self,
        obj: Atspi.Accessible,
        match: Atspi.Accessible,
    ) -> bool:
        """Returns True if match is a descendant of obj and the caret in obj is past it."""

        if not AXUtilities.is_ancestor(match, obj):
            return False

        child = match
        parent = AXObject.get_parent(child)
        while parent and parent != obj:
            child = parent
            parent = AXObject.get_parent(child)

        child_offset = AXHypertext.get_character_offset_in_parent(child)
        if child_offset < 0:
            return False

        caret_offset = AXText.get_caret_offset(obj)
        if caret_offset < 0:
            return False

        tokens = [
            "STRUCTURAL NAVIGATOR: Match",
            match,
            "is descendant of",
            obj,
            f"at offset {child_offset}; caret is at {caret_offset}",
        ]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        return caret_offset > child_offset

    def _get_state_string(self, obj: Atspi.Accessible) -> str:
        if AXUtilities.is_switch(obj):
            off, on = object_properties.SWITCH_INDICATORS_SPEECH
            return on if AXUtilities.is_checked(obj) else off

        if AXUtilities.is_check_box(obj):
            unchecked, checked, partially = object_properties.CHECK_BOX_INDICATORS_SPEECH
            if AXUtilities.is_indeterminate(obj):
                return partially
            return checked if AXUtilities.is_checked(obj) else unchecked

        if AXUtilities.is_radio_button(obj):
            unselected, selected = object_properties.RADIO_BUTTON_INDICATORS_SPEECH
            return selected if AXUtilities.is_checked(obj) else unselected

        if AXUtilities.is_link(obj):
            return (
                object_properties.STATE_VISITED
                if AXUtilities.is_visited(obj)
                else object_properties.STATE_UNVISITED
            )

        return ""

    def _get_item_string_by_role(
        self,
        script: default.Script,
        obj: Atspi.Accessible,
    ) -> str | None:
        """Returns a string for the object based on its role, or None if not role-specific."""

        if AXUtilities.is_table(obj):
            caption = AXTable.get_caption(obj)
            return AXText.get_all_text(caption) if caption else ""

        if AXUtilities.is_internal_frame(obj):
            result = self._get_item_string(script, AXObject.get_child(obj, 0))
            return result or AXUtilities.get_localized_role_name(obj)

        if AXUtilities.is_list(obj):
            children = list(AXObject.iter_children(obj, AXUtilities.is_list_item))
            count = len(children)
            counter = (
                messages.nested_list_item_count
                if AXUtilities.get_nesting_level(obj)
                else messages.list_item_count
            )
            return counter(count)

        if AXUtilities.is_description_list(obj):
            return messages.description_list_term_count(
                len(AXUtilities.find_all_description_terms(obj)),
            )

        if AXUtilities.is_image(obj):
            result = AXObject.get_image_description(obj)
            if not result:
                parent = AXObject.get_parent(obj)
                if AXUtilities.is_link(parent):
                    result = self._get_item_string(script, parent)
                else:
                    result = AXUtilities.get_localized_role_name(obj)
            return result

        return None

    def _get_item_string(self, script: default.Script, obj: Atspi.Accessible) -> str:
        if obj is None:
            return ""

        result = (
            AXObject.get_name(obj)
            or AXObject.get_description(obj)
            or AXUtilities.get_displayed_label(obj)
            or AXUtilities.get_displayed_description(obj)
        )
        if result:
            return result

        role_result = self._get_item_string_by_role(script, obj)
        if role_result is not None:
            return role_result

        if AXUtilities.is_page_tab_list(obj):
            return messages.tab_list_item_count(
                len(list(AXObject.iter_children(obj, AXUtilities.is_page_tab))),
            )

        if result := script.utilities.expand_eocs(obj):
            return result

        if AXUtilities.is_link(obj):
            result = AXHypertext.get_link_basename(obj)

        return result

    def _present_line(
        self,
        script: default.Script,
        obj: Atspi.Accessible | None = None,
        offset: int | None = None,
        notify_user: bool = True,
    ) -> None:
        if obj is None:
            return

        manager = focus_manager.get_manager()
        presenter = say_all_presenter.get_presenter()
        if manager.in_say_all() and presenter.get_structural_navigation_enabled():
            presenter.say_all(script, event=None, obj=obj, offset=offset)
            return

        manager.emit_region_changed(obj, offset, mode=focus_manager.STRUCTURAL_NAVIGATOR)
        if not notify_user:
            msg = "STRUCTURAL NAVIGATOR: _present_line called with notify_user=False"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            manager.set_locus_of_focus(None, obj, False)
            if AXObject.supports_text(obj):
                script.utilities.set_caret_position(obj, offset or 0)
            return

        script.update_braille(obj)
        script.say_line(obj, offset)

    def _present_object(
        self,
        script: default.Script,
        obj: Atspi.Accessible | None = None,
        not_found_message: str = messages.STRUCTURAL_NAVIGATION_NOT_FOUND,
        offset: int | None = None,
        notify_user: bool = True,
    ) -> None:
        if obj is None:
            if notify_user:
                presentation_manager.get_manager().present_message(
                    not_found_message,
                    messages.STRUCTURAL_NAVIGATION_NOT_FOUND,
                )
            return

        if offset is None:
            offset = 0

        manager = focus_manager.get_manager()
        if self.get_mode(script) == NavigationMode.GUI:
            manager.set_locus_of_focus(None, obj)
            AXObject.grab_focus(obj)
            AXObject.clear_cache(obj, False, "Checking state after focus grab")
            if not AXUtilities.is_focused(obj) and notify_user:
                presentation_manager.get_manager().present_message(messages.NOT_FOCUSED)
            return

        presenter = say_all_presenter.get_presenter()
        if manager.in_say_all() and presenter.get_structural_navigation_enabled():
            presenter.say_all(script, event=None, obj=obj, offset=offset)
            return

        manager.emit_region_changed(obj, offset, mode=focus_manager.STRUCTURAL_NAVIGATOR)
        if not notify_user:
            msg = "STRUCTURAL NAVIGATOR: _present_object called with notify_user=False"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            manager.set_locus_of_focus(None, obj, False)
            if AXObject.supports_text(obj):
                script.utilities.set_caret_position(obj, offset)
            return

        # Held-key coalescing: script.present_object below calls
        # scroll_to_center which blocks on time.sleep up to 150ms per
        # invocation. At 30Hz auto-repeat the main thread can't keep up
        # and the systemd watchdog fires. Defer the actual present when
        # a burst is detected; the cursor still moved via the
        # emit_region_changed above so progress through the document
        # is visible to focus tracking.
        now = time.monotonic()
        in_burst = (
            self._present_debouncer.is_pending()
            or (now - self._last_present_time) < self._PRESENT_BURST_WINDOW
        )
        if in_burst:
            # arm_or_reset: each new event cancels the prior fire and
            # reschedules, so we present only the final target after the
            # burst settles.
            self._present_pending = (script, obj, offset)
            self._present_debouncer.arm_or_reset(self._PRESENT_DEFER_MS)
            return

        # IMPORTANT: stamp _last_present_time AFTER the call returns, not
        # before. script.present_object can block for ~150ms in
        # scroll-with-sleeps. If we stamped before, events that queued
        # during the block would see "150ms elapsed > 100ms window" and
        # bypass the coalesce. Stamping after means subsequent queued
        # events see "~0ms since last present" and properly defer.
        presentation_manager.get_manager().interrupt_if_needed_for_object_presentation()
        script.present_object(obj, offset=offset)
        self._last_present_time = time.monotonic()

    def _present_fire(self) -> bool:
        """Timer callback: present the final target of a held-key burst."""

        pending = self._present_pending
        self._present_pending = None
        if pending is None:
            return False

        script, obj, offset = pending
        if not AXObject.is_valid(obj):
            return False

        presentation_manager.get_manager().interrupt_if_needed_for_object_presentation()
        script.present_object(obj, offset=offset)
        self._last_present_time = time.monotonic()
        return False

    def _present_object_list(
        self,
        script: default.Script,
        objects: list[Atspi.Accessible],
        dialog_title: str,
        column_headers: list[str],
        row_data_func: Callable,
        notify_user: bool = True,
    ) -> None:
        dialog_title = f"{dialog_title}: {messages.items_found(len(objects))}"
        if not objects:
            if notify_user:
                presentation_manager.get_manager().present_message(dialog_title)
            return

        current_object = script.utilities.get_caret_context()[0]
        try:
            index = objects.index(current_object)
        except ValueError:
            index = 0

        rows = [(obj, -1, *row_data_func(obj)) for obj in objects]
        orca_gui_navlist.show_ui(dialog_title, column_headers, rows, index)

    def _dispatch_previous(
        self,
        name: str,
        script: default.Script,
        event: InputEvent | None,
        notify_user: bool,
    ) -> bool:
        """Generic previous-element dispatcher driven by the registry."""

        return self._dispatch_directional(name, script, event, notify_user, forward=False)

    def _dispatch_next(
        self,
        name: str,
        script: default.Script,
        event: InputEvent | None,
        notify_user: bool,
    ) -> bool:
        """Generic next-element dispatcher driven by the registry."""

        return self._dispatch_directional(name, script, event, notify_user, forward=True)

    def _dispatch_directional(
        self,
        name: str,
        script: default.Script,
        event: InputEvent | None,
        notify_user: bool,
        forward: bool,
    ) -> bool:
        verb = "next" if forward else "previous"
        tokens = [
            f"STRUCTURAL NAVIGATOR: {verb}_{name}. Script:",
            script,
            "Event:",
            event,
            "notify_user:",
            notify_user,
        ]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        self._last_input_event = event
        element_type = get_registry().get(name)
        matches = element_type.matcher(script)
        result = self._get_object_in_direction(script, matches, forward)
        # Landmark is the only type whose presenter cares about a *found*
        # object (it does a named-region announcement). Every other type
        # routes through _present_object. Hardcoding the exception here
        # is intentional -- pushing it onto ElementType as another
        # callback would add a field that exactly one record sets.
        if name == "landmark":
            self._present_landmark(script, result, notify_user)
        else:
            self._present_object(
                script,
                result,
                element_type.resolve_no_more_message(),
                notify_user=notify_user,
            )
        return True

    def _dispatch_list(
        self,
        name: str,
        script: default.Script,
        event: InputEvent | None,
        notify_user: bool,
    ) -> bool:
        """Generic list-element dispatcher driven by the registry."""

        tokens = [
            f"STRUCTURAL NAVIGATOR: list_{name}. Script:",
            script,
            "Event:",
            event,
            "notify_user:",
            notify_user,
        ]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        self._last_input_event = event
        element_type = get_registry().get(name)
        row_builder = element_type.list_row_builder
        if row_builder is None:
            # live_region / separator: no list dialog. Reaching here
            # would mean the wrapper was registered in error.
            return False
        self._present_object_list(
            script,
            element_type.matcher(script),
            element_type.resolve_list_dialog_title(),
            list(element_type.list_dialog_headers),
            lambda obj, _s=script, _b=row_builder: _b(_s, obj),
            notify_user=notify_user,
        )
        return True

    def _determine_root_container(self, script: default.Script) -> Atspi.Accessible:
        mode = self.get_mode(script)
        focus = focus_manager.get_manager().get_locus_of_focus()
        root = AXUtilities.find_ancestor_inclusive(focus, AXUtilities.is_modal_dialog)
        if root is None:
            if mode == NavigationMode.DOCUMENT:
                root = script.utilities.get_top_level_document_for_object(focus)
            elif mode == NavigationMode.GUI:
                root = AXUtilities.find_ancestor_inclusive(focus, AXUtilities.is_dialog_or_window)
                if root is None:
                    root = focus_manager.get_manager().get_active_window()

        tokens = ["STRUCTURAL NAVIGATOR: Root for", focus, "is", root, f"mode: {mode}"]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        return root

    def _is_non_document_object(self, obj: Atspi.Accessible, must_be_showing: bool = True) -> bool:
        if AXUtilities.is_document_descendant(obj, inclusive=True):
            return False
        return not (must_be_showing and not AXUtilities.is_showing(obj))

    ########################
    #                      #
    # Annotations          #
    #                      #
    ########################

    def _get_all_annotations(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "annotations", lambda: AXUtilities.find_all_annotations(root),
            )
        return AXUtilities.find_all_annotations(root, pred=pred)

    @dbus_service.command
    def previous_annotation(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous annotation."""

        return self._dispatch_previous("annotation", script, event, notify_user)

    @dbus_service.command
    def next_annotation(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next annotation."""

        return self._dispatch_next("annotation", script, event, notify_user)

    @dbus_service.command
    def list_annotations(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of annotations."""

        return self._dispatch_list("annotation", script, event, notify_user)

    ########################
    #                      #
    # Blockquotes          #
    #                      #
    ########################

    def _get_all_blockquotes(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "blockquotes", lambda: AXUtilities.find_all_block_quotes(root),
            )
        return AXUtilities.find_all_block_quotes(root, pred=pred)

    @dbus_service.command
    def previous_blockquote(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous blockquote."""

        return self._dispatch_previous("blockquote", script, event, notify_user)

    @dbus_service.command
    def next_blockquote(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next blockquote."""

        return self._dispatch_next("blockquote", script, event, notify_user)

    @dbus_service.command
    def list_blockquotes(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of blockquotes."""

        return self._dispatch_list("blockquote", script, event, notify_user)

    ########################
    #                      #
    # Buttons              #
    #                      #
    ########################

    def _get_all_buttons(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "buttons", lambda: AXUtilities.find_all_buttons(root),
            )
        return AXUtilities.find_all_buttons(root, pred=pred)

    @dbus_service.command
    def previous_button(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous button."""

        return self._dispatch_previous("button", script, event, notify_user)

    @dbus_service.command
    def next_button(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next button."""

        return self._dispatch_next("button", script, event, notify_user)

    @dbus_service.command
    def list_buttons(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of buttons."""

        return self._dispatch_list("button", script, event, notify_user)

    ########################
    #                      #
    # Check boxes          #
    #                      #
    ########################

    def _get_all_checkboxes(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "checkboxes", lambda: AXUtilities.find_all_check_boxes(root),
            )
        return AXUtilities.find_all_check_boxes(root, pred=pred)

    @dbus_service.command
    def previous_checkbox(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous checkbox."""

        return self._dispatch_previous("checkbox", script, event, notify_user)

    @dbus_service.command
    def next_checkbox(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next checkbox."""

        return self._dispatch_next("checkbox", script, event, notify_user)

    @dbus_service.command
    def list_checkboxes(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of checkboxes."""

        return self._dispatch_list("checkbox", script, event, notify_user)

    ########################
    #                      #
    # Large Objects        #
    #                      #
    ########################

    def _get_all_large_objects(self, script: default.Script) -> list[Atspi.Accessible]:
        minimum_length = self.get_large_object_text_length()

        def _is_large(obj):
            if AXUtilities.is_heading(obj):
                return True
            if AXUtilities.is_list(obj):
                return True
            if AXUtilities.is_table(obj):
                return True
            text = AXText.get_all_text(obj)
            return len(text) > minimum_length and text.count("\ufffc") / len(text) < 0.05

        root = self._determine_root_container(script)
        roles = [
            *AXUtilities.get_large_container_roles(),
            Atspi.Role.HEADING,
            Atspi.Role.PARAGRAPH,
            Atspi.Role.SECTION,
        ]
        return AXUtilities.find_all_with_role(root, roles, pred=_is_large)

    @dbus_service.command
    def previous_large_object(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous large object."""

        return self._dispatch_previous("large_object", script, event, notify_user)

    @dbus_service.command
    def next_large_object(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next large object."""

        return self._dispatch_next("large_object", script, event, notify_user)

    @dbus_service.command
    def list_large_objects(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of large objects."""

        return self._dispatch_list("large_object", script, event, notify_user)

    ########################
    #                      #
    # Combo Boxes          #
    #                      #
    ########################

    def _get_all_comboboxes(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "comboboxes", lambda: AXUtilities.find_all_combo_boxes(root),
            )
        return AXUtilities.find_all_combo_boxes(root, pred=pred)

    @dbus_service.command
    def previous_combobox(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous combo box."""

        return self._dispatch_previous("combobox", script, event, notify_user)

    @dbus_service.command
    def next_combobox(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next combo box."""

        return self._dispatch_next("combobox", script, event, notify_user)

    @dbus_service.command
    def list_comboboxes(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of combo boxes."""

        return self._dispatch_list("combobox", script, event, notify_user)

    ########################
    #                      #
    # Entries              #
    #                      #
    ########################

    def _get_all_entries(self, script: default.Script) -> list[Atspi.Accessible]:
        def parent_is_not_editable(obj):
            parent = AXObject.get_parent(obj)
            return parent is not None and not AXUtilities.is_editable(parent)

        if self.get_mode(script) == NavigationMode.GUI:

            def pred(x):
                return self._is_non_document_object(x)
        else:
            pred = parent_is_not_editable

        root = self._determine_root_container(script)
        return AXUtilities.find_all_editable_objects(root, pred=pred)

    @dbus_service.command
    def previous_entry(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous entry."""

        return self._dispatch_previous("entry", script, event, notify_user)

    @dbus_service.command
    def next_entry(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next entry."""

        return self._dispatch_next("entry", script, event, notify_user)

    @dbus_service.command
    def list_entries(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of entries."""

        return self._dispatch_list("entry", script, event, notify_user)

    ########################
    #                      #
    # Form Fields          #
    #                      #
    ########################

    def _get_all_form_fields(self, script: default.Script) -> list[Atspi.Accessible]:
        def is_not_noneditable_doc_frame(obj):
            if AXUtilities.is_document_frame(obj):
                return AXUtilities.is_editable(obj)
            return True

        def pred(x):
            if self.get_mode(script) == NavigationMode.GUI:
                return self._is_non_document_object(x)
            return is_not_noneditable_doc_frame(x)

        root = self._determine_root_container(script)
        return AXUtilities.find_all_form_fields(root, pred=pred)

    @dbus_service.command
    def previous_form_field(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous form field."""

        return self._dispatch_previous("form_field", script, event, notify_user)

    @dbus_service.command
    def next_form_field(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next form field."""

        return self._dispatch_next("form_field", script, event, notify_user)

    @dbus_service.command
    def list_form_fields(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of form fields."""

        return self._dispatch_list("form_field", script, event, notify_user)

    ########################
    #                      #
    # Headings             #
    #                      #
    ########################

    def _get_all_headings(
        self,
        script: default.Script,
        level: int | None = None,
    ) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        # Cache the unfiltered case; level/pred filtered queries fall through
        # to live AT-SPI to keep the cache simple.
        if level is None and pred is None:
            return self._cached_or_compute(
                root,
                "headings",
                lambda: AXUtilities.find_all_headings(root),
            )
        if level is None:
            return AXUtilities.find_all_headings(root, pred=pred)
        return AXUtilities.find_all_headings_at_level(root, level, pred=pred)

    @dbus_service.command
    def previous_heading(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous heading."""

        return self._dispatch_previous("heading", script, event, notify_user)

    @dbus_service.command
    def next_heading(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next heading."""

        return self._dispatch_next("heading", script, event, notify_user)

    @dbus_service.command
    def list_headings(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of headings."""

        return self._dispatch_list("heading", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_1(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 1 heading."""

        return self._dispatch_previous("heading_level_1", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_1(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 1 heading."""

        return self._dispatch_next("heading_level_1", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_1(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 1 headings."""

        return self._dispatch_list("heading_level_1", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_2(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 2 heading."""

        return self._dispatch_previous("heading_level_2", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_2(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 2 heading."""

        return self._dispatch_next("heading_level_2", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_2(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 2 headings."""

        return self._dispatch_list("heading_level_2", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_3(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 3 heading."""

        return self._dispatch_previous("heading_level_3", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_3(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 3 heading."""

        return self._dispatch_next("heading_level_3", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_3(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 3 headings."""

        return self._dispatch_list("heading_level_3", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_4(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 4 heading."""

        return self._dispatch_previous("heading_level_4", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_4(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 4 heading."""

        return self._dispatch_next("heading_level_4", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_4(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 4 headings."""

        return self._dispatch_list("heading_level_4", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_5(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 5 heading."""

        return self._dispatch_previous("heading_level_5", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_5(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 5 heading."""

        return self._dispatch_next("heading_level_5", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_5(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 5 headings."""

        return self._dispatch_list("heading_level_5", script, event, notify_user)

    @dbus_service.command
    def previous_heading_level_6(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous level 6 heading."""

        return self._dispatch_previous("heading_level_6", script, event, notify_user)

    @dbus_service.command
    def next_heading_level_6(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next level 6 heading."""

        return self._dispatch_next("heading_level_6", script, event, notify_user)

    @dbus_service.command
    def list_headings_level_6(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of level 6 headings."""

        return self._dispatch_list("heading_level_6", script, event, notify_user)

    ########################
    #                      #
    # Iframes              #
    #                      #
    ########################

    def _get_all_iframes(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "iframes", lambda: AXUtilities.find_all_internal_frames(root),
            )
        return AXUtilities.find_all_internal_frames(root, pred=pred)

    @dbus_service.command
    def previous_iframe(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous iframe."""

        return self._dispatch_previous("iframe", script, event, notify_user)

    @dbus_service.command
    def next_iframe(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next iframe."""

        return self._dispatch_next("iframe", script, event, notify_user)

    @dbus_service.command
    def list_iframes(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of iframes."""

        return self._dispatch_list("iframe", script, event, notify_user)

    ########################
    #                      #
    # Images               #
    #                      #
    ########################

    @staticmethod
    def _image_is_labeled(obj: Atspi.Accessible) -> bool:
        """Returns True if the image has an accessible name or description."""

        return bool(
            AXObject.get_name(obj)
            or AXObject.get_description(obj)
            or AXObject.get_image_description(obj)
        )

    def _get_all_images(self, script: default.Script) -> list[Atspi.Accessible]:
        is_gui_mode = self.get_mode(script) == NavigationMode.GUI
        skip_unlabeled = self.get_skip_unlabeled_images()

        def pred(obj):
            if is_gui_mode and not self._is_non_document_object(obj):
                return False
            return not (skip_unlabeled and not self._image_is_labeled(obj))

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "images", lambda: AXUtilities.find_all_images_and_image_maps(root),
            )
        return AXUtilities.find_all_images_and_image_maps(root, pred=pred)

    @dbus_service.command
    def previous_image(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous image."""

        return self._dispatch_previous("image", script, event, notify_user)

    @dbus_service.command
    def next_image(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next image."""

        return self._dispatch_next("image", script, event, notify_user)

    @dbus_service.command
    def list_images(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of images."""

        return self._dispatch_list("image", script, event, notify_user)

    ########################
    #                      #
    # Landmarks            #
    #                      #
    ########################

    def _get_all_landmarks(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "landmarks", lambda: AXUtilities.find_all_landmarks(root),
            )
        return AXUtilities.find_all_landmarks(root, pred=pred)

    def _present_landmark(
        self,
        script: default.Script,
        obj: Atspi.Accessible,
        notify_user: bool,
    ) -> None:
        if obj is None:
            self._present_object(script, obj, messages.NO_LANDMARK_FOUND, notify_user=notify_user)
            return

        if notify_user:
            presentation_manager.get_manager().present_message(AXObject.get_name(obj))
        self._present_line(script, obj, 0)

    @dbus_service.command
    def previous_landmark(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous landmark."""

        return self._dispatch_previous("landmark", script, event, notify_user)

    @dbus_service.command
    def next_landmark(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next landmark."""

        return self._dispatch_next("landmark", script, event, notify_user)

    @dbus_service.command
    def list_landmarks(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of landmarks."""

        return self._dispatch_list("landmark", script, event, notify_user)

    ########################
    #                      #
    # Lists                #
    #                      #
    ########################

    def _get_all_lists(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root,
                "lists",
                lambda: AXUtilities.find_all_lists(
                    root,
                    include_description_lists=True,
                    include_tab_lists=True,
                ),
            )
        return AXUtilities.find_all_lists(
            root,
            include_description_lists=True,
            include_tab_lists=True,
            pred=pred,
        )

    def _get_first_item(self, obj: Atspi.Accessible) -> Atspi.Accessible | None:
        # The reason we present the item (or first child) rather than the full list are twofold:
        # 1. Given a huge list, navigating to the item and presenting the ancestor list is more
        #    performant.
        # 2. When we calculate what's on the same line, it should be based on the item's bounding
        #    box; not the list's.
        # TODO - JD: Handle the second issue in the utilities which calculate the line.
        return AXObject.get_child(obj, 0)

    @dbus_service.command
    def previous_list(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous list."""

        return self._dispatch_previous("list", script, event, notify_user)

    @dbus_service.command
    def next_list(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next list."""

        return self._dispatch_next("list", script, event, notify_user)

    @dbus_service.command
    def list_lists(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of lists."""

        return self._dispatch_list("list", script, event, notify_user)

    ########################
    #                      #
    # List Items           #
    #                      #
    ########################

    def _get_all_list_items(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root,
                "list_items",
                lambda: AXUtilities.find_all_list_items(
                    root,
                    include_description_terms=True,
                    include_tabs=True,
                ),
            )
        return AXUtilities.find_all_list_items(
            root,
            include_description_terms=True,
            include_tabs=True,
            pred=pred,
        )

    @dbus_service.command
    def previous_list_item(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous list item."""

        return self._dispatch_previous("list_item", script, event, notify_user)

    @dbus_service.command
    def next_list_item(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next list item."""

        return self._dispatch_next("list_item", script, event, notify_user)

    @dbus_service.command
    def list_list_items(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of list items."""

        return self._dispatch_list("list_item", script, event, notify_user)

    ########################
    #                      #
    # Live Regions         #
    #                      #
    ########################

    def _get_all_live_regions(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "live_regions", lambda: AXUtilities.find_all_live_regions(root),
            )
        return AXUtilities.find_all_live_regions(root, pred=pred)

    @dbus_service.command
    def previous_live_region(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous live region."""

        return self._dispatch_previous("live_region", script, event, notify_user)

    @dbus_service.command
    def next_live_region(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next live region."""

        return self._dispatch_next("live_region", script, event, notify_user)

    @navigation_command
    def _last_live_region(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the last live region."""

        self._last_input_event = event
        live_region_presenter.get_presenter().go_last_live_region(script, event)
        return True

    ########################
    #                      #
    # Paragraphs           #
    #                      #
    ########################

    def _get_all_paragraphs(self, script: default.Script) -> list[Atspi.Accessible]:
        def has_at_least_three_characters(obj):
            if AXUtilities.is_heading(obj):
                return True
            # We're choosing 3 characters as the minimum because some paragraphs contain a single
            # image or link and a text of length 2: An embedded object character and a space.
            # We want to skip these.
            return AXText.get_character_count(obj) > 2

        def pred(x):
            if self.get_mode(script) == NavigationMode.GUI:
                return self._is_non_document_object(x)
            return has_at_least_three_characters(x)

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "paragraphs", lambda: AXUtilities.find_all_paragraphs(root, True),
            )
        return AXUtilities.find_all_paragraphs(root, True, pred=pred)

    @dbus_service.command
    def previous_paragraph(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous paragraph."""

        return self._dispatch_previous("paragraph", script, event, notify_user)

    @dbus_service.command
    def next_paragraph(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next paragraph."""

        return self._dispatch_next("paragraph", script, event, notify_user)

    @dbus_service.command
    def list_paragraphs(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of paragraphs."""

        return self._dispatch_list("paragraph", script, event, notify_user)

    ########################
    #                      #
    # Radio Buttons        #
    #                      #
    ########################

    def _get_all_radio_buttons(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "radio_buttons", lambda: AXUtilities.find_all_radio_buttons(root),
            )
        return AXUtilities.find_all_radio_buttons(root, pred=pred)

    @dbus_service.command
    def previous_radio_button(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous radio button."""

        return self._dispatch_previous("radio_button", script, event, notify_user)

    @dbus_service.command
    def next_radio_button(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next radio button."""

        return self._dispatch_next("radio_button", script, event, notify_user)

    @dbus_service.command
    def list_radio_buttons(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of radio buttons."""

        return self._dispatch_list("radio_button", script, event, notify_user)

    ########################
    #                      #
    # Separators           #
    #                      #
    ########################

    def _get_all_separators(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "separators", lambda: AXUtilities.find_all_separators(root),
            )
        return AXUtilities.find_all_separators(root, pred=pred)

    @dbus_service.command
    def previous_separator(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous separator."""

        return self._dispatch_previous("separator", script, event, notify_user)

    @dbus_service.command
    def next_separator(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next separator."""

        return self._dispatch_next("separator", script, event, notify_user)

    ########################
    #                      #
    # Tables               #
    #                      #
    ########################

    def _get_all_tables(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "tables", lambda: AXUtilities.find_all_tables(root),
            )
        return AXUtilities.find_all_tables(root, pred=pred)

    def _get_first_table_cell(self, table: Atspi.Accessible) -> Atspi.Accessible | None:
        # The reason we present the cell rather than the full table are twofold:
        # 1. Given a huge table, navigating to the cell and presenting the ancestor table is more
        #    performant.
        # 2. When we calculate what's on the same line, it should be based on the cell's bounding
        #    box; not the table's.
        # TODO - JD: Handle the second issue in the utilities which calculate the line.
        if not AXUtilities.is_table(table):
            return None

        if cell := AXTable.get_cell_at(table, 0, 0):
            return cell

        tokens = ["STRUCTURAL NAVIGATOR: Broken table interface for", table]
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        cell = AXUtilities.get_table_cell(table)
        if cell:
            tokens = ["STRUCTURAL NAVIGATOR: Located", cell, "for first cell"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        return None

    @dbus_service.command
    def previous_table(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous table."""

        return self._dispatch_previous("table", script, event, notify_user)

    @dbus_service.command
    def next_table(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next table."""

        return self._dispatch_next("table", script, event, notify_user)

    @dbus_service.command
    def list_tables(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of tables."""

        return self._dispatch_list("table", script, event, notify_user)

    ########################
    #                      #
    # Unvisited Links      #
    #                      #
    ########################

    def _get_all_unvisited_links(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "unvisited_links", lambda: AXUtilities.find_all_unvisited_links(root),
            )
        return AXUtilities.find_all_unvisited_links(root, pred=pred)

    @dbus_service.command
    def previous_unvisited_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous unvisited link."""

        return self._dispatch_previous("unvisited_link", script, event, notify_user)

    @dbus_service.command
    def next_unvisited_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next unvisited link."""

        return self._dispatch_next("unvisited_link", script, event, notify_user)

    @dbus_service.command
    def list_unvisited_links(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of unvisited links."""

        return self._dispatch_list("unvisited_link", script, event, notify_user)

    ########################
    #                      #
    # Visited Links        #
    #                      #
    ########################

    def _get_all_visited_links(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "visited_links", lambda: AXUtilities.find_all_visited_links(root),
            )
        return AXUtilities.find_all_visited_links(root, pred=pred)

    @dbus_service.command
    def previous_visited_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous visited link."""

        return self._dispatch_previous("visited_link", script, event, notify_user)

    @dbus_service.command
    def next_visited_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next visited link."""

        return self._dispatch_next("visited_link", script, event, notify_user)

    @dbus_service.command
    def list_visited_links(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of visited links."""

        return self._dispatch_list("visited_link", script, event, notify_user)

    ########################
    #                      #
    # Links                #
    #                      #
    ########################

    def _get_all_links(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        if pred is None:
            return self._cached_or_compute(
                root, "links", lambda: AXUtilities.find_all_links(root),
            )
        return AXUtilities.find_all_links(root, pred=pred)

    @dbus_service.command
    def previous_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous link."""

        return self._dispatch_previous("link", script, event, notify_user)

    @dbus_service.command
    def next_link(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next link."""

        return self._dispatch_next("link", script, event, notify_user)

    @dbus_service.command
    def list_links(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of links."""

        return self._dispatch_list("link", script, event, notify_user)

    ########################
    #                      #
    # Clickables           #
    #                      #
    ########################

    def _get_all_clickables(self, script: default.Script) -> list[Atspi.Accessible]:
        pred = None
        if self.get_mode(script) == NavigationMode.GUI:
            pred = self._is_non_document_object

        root = self._determine_root_container(script)
        result = AXUtilities.find_all_clickables(root, pred=pred)
        result += AXUtilities.find_all_focusable_objects_with_click_ancestor(root, pred=pred)
        return result

    @dbus_service.command
    def previous_clickable(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the previous clickable."""

        return self._dispatch_previous("clickable", script, event, notify_user)

    @dbus_service.command
    def next_clickable(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Goes to the next clickable."""

        return self._dispatch_next("clickable", script, event, notify_user)

    @dbus_service.command
    def list_clickables(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Displays a list of clickables."""

        return self._dispatch_list("clickable", script, event, notify_user)

    ########################
    #                      #
    # Containers           #
    #                      #
    ########################

    def _get_current_container(self, script: default.Script) -> Atspi.Accessible | None:
        focus = focus_manager.get_manager().get_locus_of_focus()
        if container := AXUtilities.find_ancestor_inclusive(focus, AXUtilities.is_large_container):
            root = self._determine_root_container(script)
            if not AXUtilities.is_ancestor(container, root):
                return None
        return container

    @dbus_service.command
    @navigation_command
    def container_start(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Moves to the start of the current container."""

        self._last_input_event = event
        container = self._get_current_container(script)
        if container is None:
            if notify_user:
                presentation_manager.get_manager().present_message(messages.CONTAINER_NOT_IN_A)
            return True

        obj, offset = script.utilities.next_context(container, -1)
        self._present_line(script, obj, offset, notify_user)
        return True

    @dbus_service.command
    @navigation_command
    def container_end(
        self,
        script: default.Script,
        event: InputEvent | None = None,
        notify_user: bool = True,
    ) -> bool:
        """Moves to the end of the current container."""

        self._last_input_event = event
        container = self._get_current_container(script)
        if container is None:
            if notify_user:
                presentation_manager.get_manager().present_message(messages.CONTAINER_NOT_IN_A)
            return True

        # Unlike going to the start of the container, when we move to the next edge
        # we pass beyond it on purpose. This makes us consistent with NVDA.
        obj, offset = script.utilities.last_context(container)
        next_object, next_offset = script.utilities.next_context(obj, offset)
        if next_object is None:
            next_object, next_offset = obj, offset

        self._present_line(script, next_object, next_offset, notify_user)
        return True


_navigator = StructuralNavigator()


def get_navigator() -> StructuralNavigator:
    """Returns the Structural Navigator"""

    return _navigator
