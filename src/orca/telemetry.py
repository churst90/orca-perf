# Orca
#
# Copyright 2026 Cody Hurst
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

"""Read-only D-Bus telemetry counters for diagnosing Orca in the field.

Without this, diagnosing a performance bug report requires asking the
user to reproduce with `--debug-file` enabled and then poring through
megabytes of log. With it, an external tool (or a shell one-liner using
`gdbus call`) can sample live counters without touching the user's
working session.

Counters surfaced (all read-only D-Bus getters under
`org.gnome.Orca1.Telemetry`):

  ATSPICacheHitRate         double  (last-minute moving avg, 0.0-1.0)
  EventQueueDepth           uint    (current event_manager queue size)
  HungObjectCount           uint    (AXObject.HUNG_OBJECTS size)
  KnownDeadObjectCount      uint    (AXObject.KNOWN_DEAD size)
  LongLivedCacheSize        uint    (sum across LL caches)
  NavCacheSize              uint    (StructuralNavigator._nav_cache size)
  SpeechDispatcherConnected boolean
  BrailleConnected          boolean
  StructuralNavCacheHits    uint    (lifetime hits)
  StructuralNavCacheMisses  uint    (lifetime misses)

These are *snapshots*, not subscriptions -- the consumer polls. That
keeps the implementation trivial and the cost during normal use is
zero (the snapshot only runs when a peer asks).

Probe from the shell::

    gdbus call --session \\
        --dest org.gnome.Orca \\
        --object-path /org/gnome/Orca/Service \\
        --method org.freedesktop.DBus.Properties.Get \\
        org.gnome.Orca1.Telemetry ATSPICacheHitRate
"""

from __future__ import annotations

from typing import Any

from . import (  # pylint: disable=no-name-in-module
    dbus_service,
)
from .dbus_service import UInt32
from .extension import Extension


class Telemetry(Extension):
    """Read-only D-Bus telemetry counters."""

    _MODULE_NAME = "Telemetry"

    def __init__(self) -> None:
        super().__init__()

    def _get_commands(self) -> list[Any]:
        # No keyboard commands; this Extension is exposed purely via
        # D-Bus getters below.
        return []

    @dbus_service.getter
    def get_atspi_cache_hit_rate(self) -> float:
        """Returns the AT-SPI property-cache hit rate since process start (0.0-1.0).

        Lifetime aggregate, not a moving window. Aggregate is more stable
        for diagnosis ("hit rate is 0.94 across the last 200 events"
        is more useful than "hit rate over the last 60 seconds depends
        on whether the user was idle"). To get instantaneous rate,
        sample the underlying CacheHitsTotal / CacheMissesTotal counters
        between two timestamps.
        """

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        hits = AXObject.LIFETIME_CACHE_HITS + AXObject.LIFETIME_LL_CACHE_HITS
        misses = AXObject.LIFETIME_CACHE_MISSES
        total = hits + misses
        if total == 0:
            return 0.0
        return hits / total

    @dbus_service.getter
    def get_cache_hits_total(self) -> UInt32:
        """Returns the cumulative event-scope + LL cache hit count since process start."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        return dbus_service.UInt32(
            AXObject.LIFETIME_CACHE_HITS + AXObject.LIFETIME_LL_CACHE_HITS,
        )

    @dbus_service.getter
    def get_cache_misses_total(self) -> UInt32:
        """Returns the cumulative cache miss count (= D-Bus calls) since process start."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        return dbus_service.UInt32(AXObject.LIFETIME_CACHE_MISSES)

    @dbus_service.getter
    def get_event_queue_depth(self) -> UInt32:
        """Returns the current event_manager event queue size."""

        from . import event_manager  # pylint: disable=import-outside-toplevel
        try:
            return dbus_service.UInt32(event_manager.get_manager()._event_queue.qsize())
        except Exception:  # pylint: disable=broad-except
            return dbus_service.UInt32(0)

    @dbus_service.getter
    def get_hung_object_count(self) -> UInt32:
        """Returns the number of accessibles currently marked as hung."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        with AXObject._lock:  # pylint: disable=protected-access
            return dbus_service.UInt32(len(AXObject.HUNG_OBJECTS))

    @dbus_service.getter
    def get_known_dead_object_count(self) -> UInt32:
        """Returns the number of accessibles currently marked as known-dead."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        return dbus_service.UInt32(len(AXObject.KNOWN_DEAD))

    @dbus_service.getter
    def get_cache_divergence_count(self) -> UInt32:
        """Returns the number of cache-vs-live divergences observed.

        Only non-zero when ORCA_CACHE_DIVERGENCE_CHECK=1 is set at startup;
        zero otherwise. Use the orca-cache-divergence.log for per-event detail.
        """

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        return dbus_service.UInt32(AXObject.CACHE_DIVERGENCE_COUNT)

    @dbus_service.getter
    def get_long_lived_cache_size(self) -> UInt32:
        """Returns total entries across LONG_LIVED_ROLES and LONG_LIVED_STATES."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel
        size = (
            len(AXObject.LONG_LIVED_ROLES)
            + len(AXObject.LONG_LIVED_STATES)
        )
        return dbus_service.UInt32(size)

    @dbus_service.getter
    def get_nav_cache_size(self) -> UInt32:
        """Returns the number of structural-nav match-list cache entries."""

        from . import structural_navigator  # pylint: disable=import-outside-toplevel
        try:
            nav = structural_navigator.get_navigator()
            return dbus_service.UInt32(len(nav._nav_cache))  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            return dbus_service.UInt32(0)

    @dbus_service.getter
    def get_nav_cache_hits(self) -> UInt32:
        """Returns lifetime nav-cache hit count."""

        from . import structural_navigator  # pylint: disable=import-outside-toplevel
        try:
            nav = structural_navigator.get_navigator()
            return dbus_service.UInt32(nav._nav_cache_hits)  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            return dbus_service.UInt32(0)

    @dbus_service.getter
    def get_nav_cache_misses(self) -> UInt32:
        """Returns lifetime nav-cache miss count."""

        from . import structural_navigator  # pylint: disable=import-outside-toplevel
        try:
            nav = structural_navigator.get_navigator()
            return dbus_service.UInt32(nav._nav_cache_misses)  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            return dbus_service.UInt32(0)

    @dbus_service.getter
    def get_speech_dispatcher_connected(self) -> bool:
        """Returns True if the speech-dispatcher SSIP client is currently connected."""

        try:
            from . import speech_manager  # pylint: disable=import-outside-toplevel
            server = speech_manager.get_manager().get_server()
            if server is None:
                return False
            # speechdispatcherfactory.SpeechServer sets _client to None on disconnect.
            return getattr(server, "_client", None) is not None
        except Exception:  # pylint: disable=broad-except
            return False

    @dbus_service.getter
    def get_braille_connected(self) -> bool:
        """Returns True if the BrlAPI braille connection is currently live."""

        try:
            from . import braille  # pylint: disable=import-outside-toplevel
            return bool(braille._STATE.brlapi_running)  # pylint: disable=protected-access
        except Exception:  # pylint: disable=broad-except
            return False


# Module-level singleton. Extension.__init__ self-registers with
# dbus_service, so simply instantiating here is enough to publish the
# interface at process start. Other Orca subsystems follow this same
# pattern (notification_presenter, sound_presenter, etc.).
_telemetry: Telemetry | None = None


def get_telemetry() -> Telemetry:
    """Returns the Telemetry singleton, instantiating on first call."""

    global _telemetry  # pylint: disable=global-statement
    if _telemetry is None:
        _telemetry = Telemetry()
    return _telemetry
