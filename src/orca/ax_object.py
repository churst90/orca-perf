# Orca
#
# Copyright 2023 Igalia, S.L.
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
# pylint: disable=too-many-public-methods

"""Wrapper for the Atspi.Accessible interface."""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib

from . import debug

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import ClassVar


# When set (env var ORCA_PERF_LOG=1), event_scope writes cache hit-rate
# lines to ~/orca-perf.log (or $ORCA_PERF_LOG_FILE if set). Bypasses Orca's
# debug system so the file is written regardless of --debug-file.
_PERF_LOG_ENABLED = os.environ.get("ORCA_PERF_LOG", "").lower() in ("1", "true", "yes")
_PERF_LOG_PATH = os.environ.get(
    "ORCA_PERF_LOG_FILE",
    os.path.expanduser("~/orca-perf.log"),
)
_PERF_LOG_FILE = None
_PERF_LOG_LOCK = threading.Lock()

def _perf_log_write(msg: str) -> None:
    """Append a line to the perf log file. Opens lazily, line-buffered."""

    global _PERF_LOG_FILE
    if not _PERF_LOG_ENABLED:
        return
    with _PERF_LOG_LOCK:
        if _PERF_LOG_FILE is None:
            try:
                _PERF_LOG_FILE = open(  # noqa: SIM115
                    _PERF_LOG_PATH, "a", buffering=1, encoding="utf-8",
                )
                _PERF_LOG_FILE.write(
                    f"# orca-perf log opened {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                )
            except OSError:
                return
        try:
            _PERF_LOG_FILE.write(f"{time.monotonic():.3f} {msg}\n")
        except OSError:
            pass


class AXObject:
    """Wrapper for the Atspi.Accessible interface."""

    KNOWN_DEAD: ClassVar[dict[int, bool]] = {}
    OBJECT_ATTRIBUTES: ClassVar[dict[int, dict[str, str]]] = {}
    HUNG_OBJECTS: ClassVar[dict[int, float]] = {}
    HUNG_TIMEOUT = 1.0

    # Long-lived caches that survive across events. AT-SPI roles essentially
    # never change during an object's lifetime; parents rarely do; names
    # change occasionally (~3% of events). Entries are removed on defunct
    # or relevant property-change events. Each dict is hard-capped at
    # _LL_CACHE_MAX entries and cleared wholesale when exceeded to bound
    # memory in long sessions.
    #
    # NOTE: State sets are NOT long-lived cached. Atspi.StateSet objects
    # hold an internal pointer to the underlying AT-SPI object; if that
    # object becomes defunct between events (e.g. window closes), calling
    # .contains() on a stale StateSet segfaults inside libatspi. States
    # are still memoized within a single event via the event_scope cache.
    LONG_LIVED_ROLES: ClassVar[dict[int, Atspi.Role]] = {}
    LONG_LIVED_PARENTS: ClassVar[dict[int, Atspi.Accessible | None]] = {}
    LONG_LIVED_NAMES: ClassVar[dict[int, str]] = {}
    # Primitive-bits state cache. The earlier StateSet-object cache
    # (commits 7dd0f3905 / 6445b86e4) segfaulted because
    # Atspi.StateSet.contains() dereferences an internal pointer to the
    # underlying object; if that object went defunct between the cache
    # write and the later read, libatspi crashed. Storing just the
    # frozenset of int state values dodges the issue entirely -- no
    # StateSet object is held, so no stale pointer can be dereferenced.
    # Populated once-per-object from a freshly-fetched StateSet (at
    # which point we know the underlying object is alive). Invalidated
    # on any object:state-changed:* event.
    LONG_LIVED_STATES: ClassVar[dict[int, frozenset[int]]] = {}

    _LL_CACHE_MAX = 8000

    # Long-lived name cache enabled. The held-key coalesce fix in
    # structural_navigator (ccda9d591) was the actual cure for the
    # wrong-window-title-on-Alt-Tab behavior, not this cache. Reclaims
    # ~3 percentage points of steady-state hit rate (95% -> 98%).
    _NAME_LL_CACHE_DISABLED = False

    _lock = threading.Lock()

    # Per-thread event-scoped caches. Populated by event_scope() so that
    # repeated property reads within a single event handler hit the cache
    # instead of crossing D-Bus. Cleared on scope exit so values cannot go
    # stale across events.
    _event_cache_tls = threading.local()
    _perf_stats_tls = threading.local()

    # Class-level lifetime counters consumed by orca.telemetry. Always
    # incremented (no env-var gate) so the D-Bus telemetry interface
    # returns meaningful values even without ORCA_PERF_LOG=1. The cost
    # of the increments is in the noise floor of a cache hit.
    LIFETIME_CACHE_HITS: ClassVar[int] = 0
    LIFETIME_LL_CACHE_HITS: ClassVar[int] = 0
    LIFETIME_CACHE_MISSES: ClassVar[int] = 0

    @staticmethod
    @contextmanager
    def event_scope(label: str = "") -> Generator[None, None, None]:
        """Memoizes AT-SPI property reads within a single event handler.

        Within the scope, get_role(), get_parent(), get_name(), and
        get_state_set() return cached values keyed by hash(obj). The cache is
        discarded on scope exit so values cannot go stale across events.
        """

        AXObject._event_cache_tls.roles = {}
        AXObject._event_cache_tls.parents = {}
        AXObject._event_cache_tls.names = {}
        AXObject._event_cache_tls.state_sets = {}
        AXObject._event_cache_tls.cleared = set()

        log_perf = _PERF_LOG_ENABLED
        if log_perf:
            AXObject._perf_stats_tls.stats = {
                "hits": 0,
                "ll_hits": 0,
                "misses": 0,
                "start": time.monotonic(),
            }

        try:
            yield
        finally:
            if log_perf:
                stats = AXObject._perf_stats_tls.stats
                cached = stats["hits"] + stats["ll_hits"]
                total = cached + stats["misses"]
                if total > 0:
                    elapsed_ms = (time.monotonic() - stats["start"]) * 1000.0
                    cached_pct = 100.0 * cached / total
                    _perf_log_write(
                        f"event_scope[{label}]: "
                        f"{stats['hits']} ev-hits, "
                        f"{stats['ll_hits']} ll-hits, "
                        f"{stats['misses']} misses "
                        f"({cached_pct:.0f}% cached) in {elapsed_ms:.1f}ms",
                    )
                AXObject._perf_stats_tls.stats = None
            AXObject._event_cache_tls.roles = None
            AXObject._event_cache_tls.parents = None
            AXObject._event_cache_tls.names = None
            AXObject._event_cache_tls.state_sets = None
            AXObject._event_cache_tls.cleared = None

    @staticmethod
    def _record_cache_hit() -> None:
        AXObject.LIFETIME_CACHE_HITS += 1
        if _PERF_LOG_ENABLED:
            stats = getattr(AXObject._perf_stats_tls, "stats", None)
            if stats is not None:
                stats["hits"] += 1

    @staticmethod
    def _record_ll_hit() -> None:
        AXObject.LIFETIME_LL_CACHE_HITS += 1
        if _PERF_LOG_ENABLED:
            stats = getattr(AXObject._perf_stats_tls, "stats", None)
            if stats is not None:
                stats["ll_hits"] += 1

    @staticmethod
    def _record_cache_miss() -> None:
        AXObject.LIFETIME_CACHE_MISSES += 1
        if _PERF_LOG_ENABLED:
            stats = getattr(AXObject._perf_stats_tls, "stats", None)
            if stats is not None:
                stats["misses"] += 1

    @staticmethod
    def invalidate_for_event(event_type: str, source: Atspi.Accessible) -> None:
        """Invalidates long-lived cache entries based on an AT-SPI event.

        Called from the event manager before each event is dispatched.
        Defunct events remove the object entirely. Property-change and
        state-change events drop only the affected entry so the rest of the
        cache stays warm.
        """

        if not event_type:
            return
        key = hash(source)
        if event_type == "object:defunct":
            with AXObject._lock:
                AXObject.LONG_LIVED_ROLES.pop(key, None)
                AXObject.LONG_LIVED_PARENTS.pop(key, None)
                AXObject.LONG_LIVED_NAMES.pop(key, None)
                AXObject.LONG_LIVED_STATES.pop(key, None)
                AXObject.KNOWN_DEAD[key] = True
        elif event_type == "object:property-change:accessible-name":
            with AXObject._lock:
                AXObject.LONG_LIVED_NAMES.pop(key, None)
        elif event_type == "object:property-change:accessible-role":
            with AXObject._lock:
                AXObject.LONG_LIVED_ROLES.pop(key, None)
        elif event_type == "object:property-change:accessible-parent":
            with AXObject._lock:
                AXObject.LONG_LIVED_PARENTS.pop(key, None)
        elif event_type.startswith("object:state-changed:"):
            with AXObject._lock:
                AXObject.LONG_LIVED_STATES.pop(key, None)

    @staticmethod
    def _ll_store(d: dict, key: int, value) -> None:
        """Stores a value in a long-lived cache dict, enforcing the size cap."""

        with AXObject._lock:
            if len(d) >= AXObject._LL_CACHE_MAX:
                d.clear()
            d[key] = value

    @staticmethod
    def _clear_stored_data() -> None:
        """Clears any data we have cached for objects"""

        while True:
            time.sleep(60)
            AXObject._clear_all_dictionaries()

    @staticmethod
    def _prune_hung_objects() -> None:
        """Removes objects whose hung-status has expired."""

        while True:
            time.sleep(AXObject.HUNG_TIMEOUT)
            now = time.monotonic()
            with AXObject._lock:
                expired = [
                    key for key, ts in AXObject.HUNG_OBJECTS.items()
                    if now - ts >= AXObject.HUNG_TIMEOUT
                ]
                for key in expired:
                    AXObject.HUNG_OBJECTS.pop(key, None)

    @staticmethod
    def _clear_all_dictionaries(reason: str = "") -> None:
        msg = "AXObject: Clearing local cache."
        if reason:
            msg += f" Reason: {reason}"
        debug.print_message(debug.LEVEL_INFO, msg, True)

        with AXObject._lock:
            AXObject.KNOWN_DEAD.clear()
            AXObject.OBJECT_ATTRIBUTES.clear()

    @staticmethod
    def clear_cache_now(reason: str = "") -> None:
        """Clears all cached information immediately."""

        AXObject._clear_all_dictionaries(reason)

    @staticmethod
    def start_cache_clearing_thread() -> None:
        """Starts thread to periodically clear cached details."""

        thread = threading.Thread(target=AXObject._clear_stored_data)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=AXObject._prune_hung_objects)
        thread.daemon = True
        thread.start()

    @staticmethod
    def get_toolkit_name(obj: Atspi.Accessible) -> str:
        """Returns the toolkit name of obj as a lowercase string"""

        try:
            app = Atspi.Accessible.get_application(obj)
            name = Atspi.Accessible.get_toolkit_name(app) or ""
        except GLib.GError as error:
            tokens = ["AXObject: Exception calling _get_toolkit_name_on", app, f": {error}"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return ""

        return name.lower()

    @staticmethod
    def is_bogus(obj: Atspi.Accessible) -> bool:
        """Hack to ignore certain objects. All entries must have a bug."""

        # TODO - JD: Periodically check for fixes and remove hacks which are no
        # longer needed.

        # https://bugzilla.mozilla.org/show_bug.cgi?id=1879750
        if (
            AXObject.get_role(obj) == Atspi.Role.SECTION
            and AXObject.get_role(AXObject.get_parent(obj)) == Atspi.Role.FRAME
            and AXObject.get_toolkit_name(obj) == "gecko"
        ):
            tokens = ["AXObject:", obj, "is bogus. See mozilla bug 1879750."]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True, True)
            return True

        return False

    @staticmethod
    def _can_reach_application(obj: Atspi.Accessible) -> bool:
        """Returns True if we can ascend the ancestry of obj all the way to the application."""

        reached_app = False
        parent = AXObject.get_parent(obj)
        while parent and not reached_app:
            reached_app = AXObject.get_role(parent) == Atspi.Role.APPLICATION
            parent = AXObject.get_parent(parent)

        return reached_app

    @staticmethod
    def has_broken_ancestry(obj: Atspi.Accessible) -> bool:
        """Returns True if obj's ancestry is broken."""

        if obj is None:
            return False

        # https://bugreports.qt.io/browse/QTBUG-130116
        toolkit_name = AXObject.get_toolkit_name(obj)
        if not toolkit_name.startswith("qt"):
            return False

        if not AXObject._can_reach_application(obj):
            tokens = ["AXObject:", obj, "has broken ancestry. See qt bug 130116."]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return True

        return False

    @staticmethod
    def has_broken_popup_ancestry(obj: Atspi.Accessible) -> bool:
        """Returns True if obj is a popup item whose ancestry is broken."""

        if obj is None or AXObject.is_dead(obj):
            return False

        # TODO - JD: File a bug. The scenario is that when the omnibox popup is closed and then
        # re-opened, we cannot ascend all the way to the frame. In addition, parents along the
        # way claim to have 0 children.
        if not AXObject.get_toolkit_name(obj).startswith("chromium"):
            return False

        if AXObject.get_role(obj) != Atspi.Role.LIST_ITEM:
            return False

        if not AXObject._can_reach_application(obj):
            tokens = ["AXObject:", obj, "has broken ancestry."]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return True

        return False

    @staticmethod
    def is_valid(obj: Atspi.Accessible, app: Atspi.Accessible | None = None) -> bool:
        """Returns False if we know for certain this object is invalid"""

        return not (
            obj is None or AXObject.object_is_known_dead(obj) or AXObject.check_hung(obj, app)
        )

    @staticmethod
    def object_is_known_dead(obj: Atspi.Accessible) -> bool:
        """Returns True if we know for certain this object no longer exists"""

        return bool(obj and AXObject.KNOWN_DEAD.get(hash(obj))) is True

    @staticmethod
    def check_hung(
        obj: Atspi.Accessible | None,
        app: Atspi.Accessible | None = None,
    ) -> bool:
        """Returns True if obj or its app is hung, propagating obj-hung to app."""

        obj_hung_ts = AXObject.HUNG_OBJECTS.get(hash(obj)) if obj is not None else None
        app_hung = app is not None and hash(app) in AXObject.HUNG_OBJECTS
        if obj_hung_ts is not None and app is not None and not app_hung:
            tokens = ["AXObject: Marking", app, "as hung due to hung source"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            AXObject.HUNG_OBJECTS[hash(app)] = obj_hung_ts
            app_hung = True
        return obj_hung_ts is not None or app_hung

    @staticmethod
    def _set_known_dead_status(obj: Atspi.Accessible, is_dead: bool) -> None:
        """Updates the known-dead status of obj"""

        if obj is None:
            return

        current_status = AXObject.KNOWN_DEAD.get(hash(obj))
        if current_status == is_dead:
            return

        AXObject.KNOWN_DEAD[hash(obj)] = is_dead
        if is_dead:
            msg = "AXObject: Adding to known dead objects"
            debug.print_message(debug.LEVEL_INFO, msg, True, True)
            return

        if current_status:
            tokens = ["AXObject: Removing", obj, "from known-dead objects"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

    @staticmethod
    def handle_error(obj: Atspi.Accessible, error: Exception, msg: str) -> None:
        """Parses the exception and potentially updates our status for obj"""

        if AXObject.object_is_known_dead(obj):
            return

        error_string = str(error)
        if "Object does not exist at path" in error_string:
            debug.print_message(debug.LEVEL_INFO, msg, True)
        elif "The application no longer exists" in error_string:
            msg = msg.replace(error_string, "app no longer exists")
            debug.print_message(debug.LEVEL_INFO, msg, True)
        elif "The process appears to be hung" in error_string:
            debug.print_message(debug.LEVEL_INFO, msg, True)
            with AXObject._lock:
                AXObject.HUNG_OBJECTS[hash(obj)] = time.monotonic()
            return
        elif re.search(r"accessible/\d+ does not exist", error_string):
            msg = msg.replace(error_string, "object no longer exists")
            debug.print_message(debug.LEVEL_INFO, msg, True)
        else:
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return

        if AXObject.KNOWN_DEAD.get(hash(obj)) is False:
            AXObject._set_known_dead_status(obj, True)

    @staticmethod
    def supports_action(obj: Atspi.Accessible) -> bool:
        """Returns True if the action interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_action_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_action_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def _find_ancestor_with_role(
        obj: Atspi.Accessible,
        role: Atspi.Role,
    ) -> Atspi.Accessible | None:
        """Returns obj or the nearest ancestor with the specified role."""

        current = obj
        while current:
            if AXObject.get_role(current) == role:
                return current
            current = AXObject.get_parent(current)
        return None

    @staticmethod
    def _has_document_spreadsheet(obj: Atspi.Accessible) -> bool:
        # To avoid circular import. pylint: disable=import-outside-toplevel
        from .ax_collection import AXCollection

        rule = AXCollection.create_match_rule(roles=[Atspi.Role.DOCUMENT_SPREADSHEET])
        if rule is None:
            return False

        frame = AXObject._find_ancestor_with_role(obj, Atspi.Role.FRAME)
        if frame is None:
            return False
        return bool(
            Atspi.Collection.get_matches(frame, rule, Atspi.CollectionSortOrder.CANONICAL, 1, True),
        )

    @staticmethod
    def supports_collection(obj: Atspi.Accessible) -> bool:
        """Returns True if the collection interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            app = Atspi.Accessible.get_application(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in supports_collection: {error}"
            debug.print_message(debug.LEVEL_INFO, msg, True)
            return False

        try:
            iface = Atspi.Accessible.get_collection_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_collection_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        app_name = AXObject.get_name(app)
        if app_name != "soffice":
            result = iface is not None
        elif AXObject._find_ancestor_with_role(obj, Atspi.Role.DOCUMENT_TEXT):
            result = True
        elif AXObject._has_document_spreadsheet(obj):
            msg = "AXObject: Treating soffice as not supporting collection due to spreadsheet."
            debug.print_message(debug.LEVEL_INFO, msg, True)
            result = False
        else:
            result = True
        return result

    @staticmethod
    def supports_component(obj: Atspi.Accessible) -> bool:
        """Returns True if the component interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_component_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_component_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_document(obj: Atspi.Accessible) -> bool:
        """Returns True if the document interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_document_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_document_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_editable_text(obj: Atspi.Accessible) -> bool:
        """Returns True if the editable-text interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_editable_text_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_editable_text_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_hyperlink(obj: Atspi.Accessible) -> bool:
        """Returns True if the hyperlink interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_hyperlink(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_hyperlink on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_hypertext(obj: Atspi.Accessible) -> bool:
        """Returns True if the hypertext interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_hypertext_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_hypertext_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_image(obj: Atspi.Accessible) -> bool:
        """Returns True if the image interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_image_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_image_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_selection(obj: Atspi.Accessible) -> bool:
        """Returns True if the selection interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_selection_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_selection_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_table(obj: Atspi.Accessible) -> bool:
        """Returns True if the table interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_table_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_table_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_table_cell(obj: Atspi.Accessible) -> bool:
        """Returns True if the table cell interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_table_cell(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_table_cell on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def supports_text(obj: Atspi.Accessible) -> bool:
        """Returns True if the text interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_text_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_text_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False
        return iface is not None

    @staticmethod
    def supports_value(obj: Atspi.Accessible) -> bool:
        """Returns True if the value interface is supported on obj"""

        if not AXObject.is_valid(obj):
            return False

        try:
            iface = Atspi.Accessible.get_value_iface(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception calling get_value_iface on {obj}: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        return iface is not None

    @staticmethod
    def get_path(obj: Atspi.Accessible) -> list[int]:
        """Returns the path from application to obj as list of child indices"""

        if not AXObject.is_valid(obj):
            return []

        path = []
        acc = obj
        while acc:
            try:
                path.append(Atspi.Accessible.get_index_in_parent(acc))
            except GLib.GError as error:
                msg = f"AXObject: Exception getting index in parent for {acc}: {error}"
                AXObject.handle_error(acc, error, msg)
                return []
            acc = AXObject.get_parent_checked(acc)

        path.reverse()
        return path

    @staticmethod
    def get_index_in_parent(obj: Atspi.Accessible) -> int:
        """Returns the child index of obj within its parent"""

        if not AXObject.is_valid(obj):
            return -1

        try:
            index = Atspi.Accessible.get_index_in_parent(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_index_in_parent: {error}"
            AXObject.handle_error(obj, error, msg)
            return -1

        return index

    @staticmethod
    def get_parent(obj: Atspi.Accessible) -> Atspi.Accessible | None:
        """Returns the accessible parent of obj. See also get_parent_checked."""

        if not AXObject.is_valid(obj):
            return None

        key = hash(obj)
        cache = getattr(AXObject._event_cache_tls, "parents", None)

        # Layer 1: event-scope cache (within-event redundancy)
        if cache is not None and key in cache:
            AXObject._record_cache_hit()
            return cache[key]

        # Layer 2: long-lived parent cache (cross-event; tree mutations are
        # signalled by children-changed events but we only invalidate on
        # defunct here, accepting brief staleness for reparenting cases)
        ll_lookup_done = False
        with AXObject._lock:
            if key in AXObject.LONG_LIVED_PARENTS:
                ll_parent = AXObject.LONG_LIVED_PARENTS[key]
                ll_lookup_done = True
        if ll_lookup_done:
            if cache is not None:
                cache[key] = ll_parent
            AXObject._record_ll_hit()
            return ll_parent

        # Layer 3: AT-SPI call (D-Bus round-trip)
        try:
            parent = Atspi.Accessible.get_parent(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_parent: {error}"
            AXObject.handle_error(obj, error, msg)
            return None

        if parent == obj:
            tokens = ["AXObject:", obj, "claims to be its own parent"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            if cache is not None:
                cache[key] = None
                AXObject._record_cache_miss()
            return None

        if parent is None and AXObject.get_role(obj) not in [
            Atspi.Role.INVALID,
            Atspi.Role.DESKTOP_FRAME,
        ]:
            tokens = ["AXObject:", obj, "claims to have no parent"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        AXObject._ll_store(AXObject.LONG_LIVED_PARENTS, key, parent)
        if cache is not None:
            cache[key] = parent
            AXObject._record_cache_miss()

        return parent

    @staticmethod
    def get_parent_checked(obj: Atspi.Accessible) -> Atspi.Accessible | None:
        """Returns the parent of obj, doing checks for tree validity"""

        if AXObject.get_role(obj) in (Atspi.Role.INVALID, Atspi.Role.APPLICATION):
            return None

        parent = AXObject.get_parent(obj)
        if parent is None:
            return None

        if debug.debugLevel > debug.LEVEL_INFO or AXObject.is_dead(obj):
            return parent

        index = AXObject.get_index_in_parent(obj)
        n_children = AXObject.get_child_count(parent)
        if index < 0 or index >= n_children:
            tokens = [
                "AXObject:",
                obj,
                "has index",
                index,
                "; parent",
                parent,
                "has",
                n_children,
                "children",
            ]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
        else:
            # This performs our check and includes any errors.
            AXObject.get_active_descendant_checked(parent, obj)

        return parent

    @staticmethod
    def get_child(
        obj: Atspi.Accessible,
        index: int,
        n_children: int | None = None,
    ) -> Atspi.Accessible | None:
        """Returns the nth child of obj. See also get_child_checked."""

        if not AXObject.is_valid(obj):
            return None

        if n_children is None:
            n_children = AXObject.get_child_count(obj)

        if n_children <= 0:
            return None

        if index == -1:
            index = n_children - 1

        if not 0 <= index < n_children:
            return None

        try:
            child = Atspi.Accessible.get_child_at_index(obj, index)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_child: {error}"
            AXObject.handle_error(obj, error, msg)
            return None

        if child == obj:
            tokens = ["AXObject:", obj, "claims to be its own child"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return None

        return child

    @staticmethod
    def get_child_checked(obj: Atspi.Accessible, index: int) -> Atspi.Accessible | None:
        """Returns the nth child of obj, doing checks for tree validity"""

        if not AXObject.is_valid(obj):
            return None

        child = AXObject.get_child(obj, index)
        if debug.debugLevel > debug.LEVEL_INFO:
            return child

        parent = AXObject.get_parent(child)
        if obj != parent:
            tokens = ["AXObject:", obj, "claims", child, "as child; child's parent is", parent]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        return child

    @staticmethod
    def get_active_descendant_checked(
        container: Atspi.Accessible,
        reported_child: Atspi.Accessible,
    ) -> Atspi.Accessible | None:
        """Checks the reported active descendant and return the real/valid one."""

        if not AXObject.has_state(container, Atspi.StateType.MANAGES_DESCENDANTS):
            return reported_child

        index = AXObject.get_index_in_parent(reported_child)
        try:
            real_child = Atspi.Accessible.get_child_at_index(container, index)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_active_descendant_checked: {error}"
            AXObject.handle_error(container, error, msg)
            return reported_child

        if real_child != reported_child:
            tokens = [
                "AXObject: ",
                container,
                f"'s child at {index} is ",
                real_child,
                "; not reported child",
                reported_child,
            ]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        return real_child

    @staticmethod
    def get_role(obj: Atspi.Accessible) -> Atspi.Role:
        """Returns the accessible role of obj"""

        if not AXObject.is_valid(obj):
            return Atspi.Role.INVALID

        key = hash(obj)
        cache = getattr(AXObject._event_cache_tls, "roles", None)

        # Layer 1: event-scope cache (within-event redundancy)
        if cache is not None and key in cache:
            AXObject._record_cache_hit()
            return cache[key]

        # Layer 2: long-lived role cache (cross-event; role rarely changes)
        with AXObject._lock:
            ll_role = AXObject.LONG_LIVED_ROLES.get(key)
        if ll_role is not None:
            if cache is not None:
                cache[key] = ll_role
            AXObject._record_ll_hit()
            return ll_role

        # Layer 3: AT-SPI call (D-Bus round-trip)
        try:
            role = Atspi.Accessible.get_role(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_role: {error}"
            AXObject.handle_error(obj, error, msg)
            return Atspi.Role.INVALID

        AXObject._set_known_dead_status(obj, False)

        AXObject._ll_store(AXObject.LONG_LIVED_ROLES, key, role)
        if cache is not None:
            cache[key] = role
            AXObject._record_cache_miss()

        return role

    @staticmethod
    def get_role_name(obj: Atspi.Accessible, localized: bool = False) -> str:
        """Returns the accessible role name of obj"""

        if not AXObject.is_valid(obj):
            return ""

        try:
            if not localized:
                role_name = Atspi.Accessible.get_role_name(obj)
            else:
                role_name = Atspi.Accessible.get_localized_role_name(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_role_name: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        return role_name

    @staticmethod
    def get_role_description(obj: Atspi.Accessible, is_braille: bool = False) -> str:
        """Returns the accessible role description of obj"""

        if not AXObject.is_valid(obj):
            return ""

        attrs = AXObject.get_attributes_dict(obj)
        rv = attrs.get("roledescription", "")
        if is_braille:
            rv = attrs.get("brailleroledescription", rv)
        return rv

    @staticmethod
    def get_accessible_id(obj: Atspi.Accessible) -> str:
        """Returns the accessible id of obj"""

        if not AXObject.is_valid(obj):
            return ""

        try:
            result = Atspi.Accessible.get_accessible_id(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_accessible_id: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        AXObject._set_known_dead_status(obj, False)
        return result

    @staticmethod
    def get_name(obj: Atspi.Accessible) -> str:
        """Returns the accessible name of obj"""

        if not AXObject.is_valid(obj):
            return ""

        key = hash(obj)
        cache = getattr(AXObject._event_cache_tls, "names", None)

        # Layer 1: event-scope cache
        if cache is not None and key in cache:
            AXObject._record_cache_hit()
            return cache[key]

        # Layer 2: long-lived name cache (invalidated on name-change events).
        # Skipped when the diagnostic flag is set so we can isolate whether
        # this layer is contributing to the wrong-window-title issue.
        if not AXObject._NAME_LL_CACHE_DISABLED:
            with AXObject._lock:
                ll_name = AXObject.LONG_LIVED_NAMES.get(key)
            if ll_name is not None:
                if cache is not None:
                    cache[key] = ll_name
                AXObject._record_ll_hit()
                return ll_name

        # Layer 3: AT-SPI call
        try:
            name = Atspi.Accessible.get_name(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_name: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        AXObject._set_known_dead_status(obj, False)

        # Only cache non-empty names; empty string could mean "not yet set"
        # in some toolkit implementations and we don't want to lock that in.
        if name and not AXObject._NAME_LL_CACHE_DISABLED:
            AXObject._ll_store(AXObject.LONG_LIVED_NAMES, key, name)
        if cache is not None:
            cache[key] = name
            AXObject._record_cache_miss()

        return name

    @staticmethod
    def has_same_non_empty_name(obj1: Atspi.Accessible, obj2: Atspi.Accessible) -> bool:
        """Returns true if obj1 and obj2 share the same non-empty name"""

        name1 = AXObject.get_name(obj1)
        if not name1:
            return False

        return name1 == AXObject.get_name(obj2)

    @staticmethod
    def get_description(obj: Atspi.Accessible) -> str:
        """Returns the accessible description of obj"""

        if not AXObject.is_valid(obj):
            return ""

        try:
            description = Atspi.Accessible.get_description(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_description: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        return description

    @staticmethod
    def get_image_description(obj: Atspi.Accessible) -> str:
        """Returns the accessible image description of obj"""

        if not AXObject.supports_image(obj):
            return ""

        try:
            description = Atspi.Image.get_image_description(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_image_description: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        return description

    @staticmethod
    def get_image_size(obj: Atspi.Accessible) -> tuple[int, int]:
        """Returns a (width, height) tuple of the image in obj"""

        if not AXObject.supports_image(obj):
            return 0, 0

        try:
            result = Atspi.Image.get_image_size(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_image_size: {error}"
            AXObject.handle_error(obj, error, msg)
            return 0, 0

        # The return value is an AtspiPoint, hence x and y.
        return result.x, result.y

    @staticmethod
    def get_help_text(obj: Atspi.Accessible) -> str:
        """Returns the accessible help text of obj"""

        if not AXObject.is_valid(obj):
            return ""

        try:
            # Added in Atspi 2.52.
            text = Atspi.Accessible.get_help_text(obj) or ""
        except GLib.GError:
            # This is for prototyping in the meantime.
            text = AXObject.get_attribute(obj, "helptext") or ""

        return text

    @staticmethod
    def get_child_count(obj: Atspi.Accessible) -> int:
        """Returns the child count of obj"""

        if not AXObject.is_valid(obj):
            return 0

        try:
            count = Atspi.Accessible.get_child_count(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_child_count: {error}"
            AXObject.handle_error(obj, error, msg)
            return 0

        return count

    @staticmethod
    def iter_children(
        obj: Atspi.Accessible,
        pred: Callable[[Atspi.Accessible], bool] | None = None,
    ) -> Generator[Atspi.Accessible, None, None]:
        """Generator to iterate through obj's children. If the function pred is
        specified, children for which pred is False will be skipped."""

        if not AXObject.is_valid(obj):
            return

        child_count = AXObject.get_child_count(obj)
        if child_count > 500:
            tokens = ["AXObject:", obj, "has more than 500 children"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True, True)

        for index in range(child_count):
            child = AXObject.get_child(obj, index, child_count)
            if child is None and not AXObject.is_valid(obj):
                tokens = ["AXObject:", obj, "is no longer valid"]
                debug.print_tokens(debug.LEVEL_INFO, tokens, True)
                return

            if child is not None and (pred is None or pred(child)):
                yield child

    @staticmethod
    def get_previous_sibling(obj: Atspi.Accessible) -> Atspi.Accessible | None:
        """Returns the previous sibling of obj, based on child indices"""

        if not AXObject.is_valid(obj):
            return None

        parent = AXObject.get_parent(obj)
        if parent is None:
            return None

        index = AXObject.get_index_in_parent(obj)
        if index <= 0:
            return None

        sibling = AXObject.get_child(parent, index - 1)
        if sibling == obj:
            tokens = ["AXObject:", obj, "claims to be its own sibling"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return None

        return sibling

    @staticmethod
    def get_next_sibling(obj: Atspi.Accessible) -> Atspi.Accessible | None:
        """Returns the next sibling of obj, based on child indices"""

        if not AXObject.is_valid(obj):
            return None

        parent = AXObject.get_parent(obj)
        if parent is None:
            return None

        index = AXObject.get_index_in_parent(obj)
        if index < 0:
            return None

        sibling = AXObject.get_child(parent, index + 1)
        if sibling == obj:
            tokens = ["AXObject:", obj, "claims to be its own sibling"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return None

        return sibling

    @staticmethod
    def get_locale(obj: Atspi.Accessible) -> str:
        """Returns the locale of obj"""

        if not AXObject.is_valid(obj):
            return ""

        try:
            locale = Atspi.Accessible.get_object_locale(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_locale: {error}"
            AXObject.handle_error(obj, error, msg)
            return ""

        return locale or ""

    @staticmethod
    def get_state_set(obj: Atspi.Accessible) -> Atspi.StateSet:
        """Returns the state set associated with obj"""

        if not AXObject.is_valid(obj):
            return Atspi.StateSet()

        key = hash(obj)
        cache = getattr(AXObject._event_cache_tls, "state_sets", None)

        # Event-scope cache only. StateSet is NOT long-lived cached because
        # Atspi.StateSet holds an internal ref to a possibly-dead object and
        # calling .contains() on a stale instance segfaults inside libatspi.
        if cache is not None and key in cache:
            AXObject._record_cache_hit()
            return cache[key]

        try:
            state_set = Atspi.Accessible.get_state_set(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_state_set: {error}"
            AXObject.handle_error(obj, error, msg)
            return Atspi.StateSet()

        if state_set is None:
            tokens = ["AXObject: get_state_set failed for", obj]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)
            return Atspi.StateSet()

        AXObject._set_known_dead_status(obj, False)

        if cache is not None:
            cache[key] = state_set
            AXObject._record_cache_miss()

        # Populate the primitive-bits LL cache while we have a freshly
        # fetched StateSet (so .get_states() is safe -- the underlying
        # object is provably alive right now). Storing only the int
        # values means subsequent has_state() calls never need to touch
        # the StateSet object again, so a later defunct cannot cause a
        # stale-pointer dereference inside libatspi.
        try:
            states_frozen = frozenset(int(s) for s in state_set.get_states())
        except (GLib.GError, TypeError, ValueError):
            states_frozen = None
        if states_frozen is not None:
            AXObject._ll_store(AXObject.LONG_LIVED_STATES, key, states_frozen)

        return state_set

    @staticmethod
    def has_state(obj: Atspi.Accessible, state: Atspi.StateType) -> bool:
        """Returns true if obj has the specified state"""

        if not AXObject.is_valid(obj):
            return False

        # LL fast path: no StateSet dereference, so a defunct object
        # between the cache write and this read cannot crash libatspi.
        key = hash(obj)
        with AXObject._lock:
            cached_states = AXObject.LONG_LIVED_STATES.get(key)
        if cached_states is not None:
            AXObject._record_ll_hit()
            return int(state) in cached_states

        return AXObject.get_state_set(obj).contains(state)

    @staticmethod
    def clear_cache(obj: Atspi.Accessible, recursive: bool = False, reason: str = "") -> None:
        """Clears the Atspi cached information associated with obj"""

        if obj is None:
            return

        # Suppress non-recursive duplicates within a single event scope.
        # Multiple handlers in one event commonly call clear_cache on the
        # same object (locus-of-focus change, on-screen check, scroll
        # confirm). After the first call libatspi's cache is empty for
        # this obj; intervening get_* calls are reads that don't repopulate
        # remote state, so a second clear in the same handler is a D-Bus
        # round-trip to no effect. Recursive clears are not suppressed --
        # they affect descendants and the descendants may have been
        # touched between the first and second call.
        if not recursive:
            cleared = getattr(AXObject._event_cache_tls, "cleared", None)
            if cleared is not None:
                key = hash(obj)
                if key in cleared:
                    return
                cleared.add(key)

        tokens = ["AXObject: Clearing AT-SPI cache on", obj, f"Recursive: {recursive}."]
        if reason:
            tokens.append(f" Reason: {reason}")
        debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        if not recursive:
            try:
                Atspi.Accessible.clear_cache_single(obj)
            except GLib.GError as error:
                msg = f"AXObject: Exception in clear_cache_single: {error}"
                debug.print_message(debug.LEVEL_INFO, msg, True)
            return

        try:
            Atspi.Accessible.clear_cache(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in clear_cache: {error}"
            AXObject.handle_error(obj, error, msg)

    @staticmethod
    def get_process_id(obj: Atspi.Accessible) -> int:
        """Returns the process id associated with obj"""

        if not AXObject.is_valid(obj):
            return -1

        try:
            pid = Atspi.Accessible.get_process_id(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_process_id: {error}"
            AXObject.handle_error(obj, error, msg)
            return -1

        return pid

    @staticmethod
    def is_dead(obj: Atspi.Accessible, app: Atspi.Accessible | None = None) -> bool:
        """Returns true of obj exists but is believed to be dead."""

        if obj is None:
            return False

        if not AXObject.is_valid(obj, app):
            return True

        try:
            # We use the Atspi function rather than the AXObject function because the
            # latter intentionally handles exceptions.
            Atspi.Accessible.get_name(obj)
        except GLib.GError as error:
            msg = f"AXObject: Accessible is dead: {error}"
            AXObject.handle_error(obj, error, msg)
            return True

        AXObject._set_known_dead_status(obj, False)
        return False

    @staticmethod
    def get_attributes_dict(obj: Atspi.Accessible, use_cache: bool = True) -> dict[str, str]:
        """Returns the object attributes of obj as a dictionary."""

        if not AXObject.is_valid(obj):
            return {}

        if use_cache:
            attributes = AXObject.OBJECT_ATTRIBUTES.get(hash(obj))
            if attributes:
                return attributes

        try:
            attributes = Atspi.Accessible.get_attributes(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in get_attributes_dict: {error}"
            AXObject.handle_error(obj, error, msg)
            return {}

        if attributes is None:
            return {}

        AXObject.OBJECT_ATTRIBUTES[hash(obj)] = attributes
        return attributes

    @staticmethod
    def get_attribute(obj: Atspi.Accessible, attribute_name: str, use_cache: bool = True) -> str:
        """Returns the value of the specified attribute as a string."""

        if not AXObject.is_valid(obj):
            return ""

        attributes = AXObject.get_attributes_dict(obj, use_cache)
        return attributes.get(attribute_name, "")

    @staticmethod
    def grab_focus(obj: Atspi.Accessible) -> bool:
        """Attempts to grab focus on obj. Returns true if successful."""

        if not AXObject.supports_component(obj):
            return False

        try:
            result = Atspi.Component.grab_focus(obj)
        except GLib.GError as error:
            msg = f"AXObject: Exception in grab_focus: {error}"
            AXObject.handle_error(obj, error, msg)
            return False

        if debug.debugLevel > debug.LEVEL_INFO:
            return result

        if result and not AXObject.has_state(obj, Atspi.StateType.FOCUSED):
            tokens = ["AXObject:", obj, "lacks focused state after focus grab"]
            debug.print_tokens(debug.LEVEL_INFO, tokens, True)

        return result


AXObject.start_cache_clearing_thread()
