# Orca
#
# Copyright 2026 Cody Hurst
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

"""Optional stress-test harness for Orca's robustness work.

Activated by exporting ORCA_STRESS=1 (plus optional comma-separated
flags) in the environment before Orca starts. Off by default; zero
cost when unset.

Purpose: regression-test the concurrency, recovery, and hung-process
mitigations we landed across rounds 4-7 (HUNG_OBJECTS lock,
_latest_event race, BrlAPI health probe, speech-dispatcher reconnect,
nav-cache debounce + background rebuild). Without a harness like this,
those code paths only execute under real-world failure conditions that
are hard to reproduce on demand.

The harness runs as background GLib timers; the synthetic load it
generates is independent of (and additive to) the real user workload,
so you can browse normally while it's active. Toggle individual
stressors via the ORCA_STRESS flags (default "all"):

  ORCA_STRESS=1                       # all stressors enabled
  ORCA_STRESS=hung                    # only hung-process simulation
  ORCA_STRESS=speechd,brlapi          # specific subsystems
  ORCA_STRESS=event-flood,hung,navcache

Stressors:

  hung          Every 30s, picks 1-3 currently-cached accessibles and
                marks them as HUNG via AXObject.HUNG_OBJECTS. Exercises
                the HUNG_OBJECTS lock + propagation paths.

  speechd       Every 45s, force-resets the speech-dispatcher SSIP
                connection via SpeechServer.reset(). Exercises the
                30s health-probe / reconnect path.

  brlapi        Every 60s, marks BrlAPI dead via _mark_brlapi_dead()
                with a synthetic reason. Exercises the retry +
                reconnect cycle.

  navcache      Every 20s, marks all nav-cache entries stale and
                fires the deferred-rebuild. Exercises the 150ms
                debounce + idle-priority rebuild.

  event-flood   Every 10s, generates 200 synthetic AT-SPI events on
                the queue in a tight burst. Exercises event-queue
                backpressure + obsolescence filtering.

All stressors log at debug.LEVEL_WARNING so they're visible in
--debug-file output without manual instrumentation.
"""

from __future__ import annotations

import os
import random
import time
from typing import TYPE_CHECKING

from . import debug
from .util.debounce import DebouncedCallable

if TYPE_CHECKING:
    pass


_ENV_VAR = "ORCA_STRESS"
_ALL_STRESSORS = frozenset({"hung", "speechd", "brlapi", "navcache", "event-flood"})


def _parse_flags(value: str) -> frozenset[str]:
    """Parse ORCA_STRESS value into a set of enabled stressor names."""

    if not value or value.lower() in ("0", "false", "no", "off"):
        return frozenset()
    if value.lower() in ("1", "true", "yes", "on", "all"):
        return _ALL_STRESSORS
    tokens = {t.strip() for t in value.split(",")}
    unknown = tokens - _ALL_STRESSORS
    if unknown:
        debug.print_message(
            debug.LEVEL_WARNING,
            f"STRESS MODE: Unknown stressors ignored: {sorted(unknown)}",
            True,
        )
    return frozenset(tokens & _ALL_STRESSORS)


# Intervals chosen to be (a) frequent enough to actually exercise the
# recovery paths in a short test session, (b) infrequent enough that
# the user can still get real work done with stress mode on.
_INTERVAL_MS = {
    "hung": 30000,
    "speechd": 45000,
    "brlapi": 60000,
    "navcache": 20000,
    "event-flood": 10000,
}


class StressMode:
    """Holds the per-stressor debouncers and runs the synthetic loads."""

    def __init__(self, enabled: frozenset[str]) -> None:
        self._enabled = enabled
        self._debouncers: dict[str, DebouncedCallable] = {}
        self._started = False
        msg = f"STRESS MODE: Enabled stressors: {sorted(enabled) if enabled else 'none'}"
        debug.print_message(debug.LEVEL_WARNING, msg, True)

    def start(self) -> None:
        """Arm the first fire for every enabled stressor."""

        if self._started or not self._enabled:
            return
        self._started = True
        if "hung" in self._enabled:
            self._debouncers["hung"] = DebouncedCallable(self._fire_hung)
            self._debouncers["hung"].arm(_INTERVAL_MS["hung"])
        if "speechd" in self._enabled:
            self._debouncers["speechd"] = DebouncedCallable(self._fire_speechd)
            self._debouncers["speechd"].arm(_INTERVAL_MS["speechd"])
        if "brlapi" in self._enabled:
            self._debouncers["brlapi"] = DebouncedCallable(self._fire_brlapi)
            self._debouncers["brlapi"].arm(_INTERVAL_MS["brlapi"])
        if "navcache" in self._enabled:
            self._debouncers["navcache"] = DebouncedCallable(self._fire_navcache)
            self._debouncers["navcache"].arm(_INTERVAL_MS["navcache"])
        if "event-flood" in self._enabled:
            self._debouncers["event-flood"] = DebouncedCallable(self._fire_event_flood)
            self._debouncers["event-flood"].arm(_INTERVAL_MS["event-flood"])

    def stop(self) -> None:
        """Cancel all pending stressor fires."""

        for d in self._debouncers.values():
            d.cancel()
        self._debouncers.clear()
        self._started = False

    def _rearm(self, name: str) -> None:
        """Reschedule the named stressor for its next fire."""

        d = self._debouncers.get(name)
        if d is not None:
            d.arm(_INTERVAL_MS[name])

    def _fire_hung(self) -> bool:
        """Mark a few cached accessibles as hung."""

        from .ax_object import AXObject  # pylint: disable=import-outside-toplevel

        with AXObject._lock:  # pylint: disable=protected-access
            candidates = list(AXObject.LONG_LIVED_ROLES.keys())
        if candidates:
            now = time.monotonic()
            picked = random.sample(candidates, min(3, len(candidates)))
            with AXObject._lock:  # pylint: disable=protected-access
                for key in picked:
                    AXObject.HUNG_OBJECTS[key] = now
            debug.print_message(
                debug.LEVEL_WARNING,
                f"STRESS MODE: Marked {len(picked)} accessibles as hung.",
                True,
            )
        self._rearm("hung")
        return False

    def _fire_speechd(self) -> bool:
        """Force a speech-dispatcher reconnect."""

        try:
            from . import speech_manager  # pylint: disable=import-outside-toplevel
            server = speech_manager.get_manager().get_server()
            if server is not None and hasattr(server, "reset"):
                debug.print_message(
                    debug.LEVEL_WARNING,
                    "STRESS MODE: Forcing speech-dispatcher reset.",
                    True,
                )
                server.reset()
        except Exception as error:  # pylint: disable=broad-except
            debug.print_message(
                debug.LEVEL_WARNING,
                f"STRESS MODE: speechd reset failed: {error}",
                True,
            )
        self._rearm("speechd")
        return False

    def _fire_brlapi(self) -> bool:
        """Force a BrlAPI reconnect cycle."""

        try:
            from . import braille  # pylint: disable=import-outside-toplevel
            if braille._STATE.brlapi_running:  # pylint: disable=protected-access
                debug.print_message(
                    debug.LEVEL_WARNING,
                    "STRESS MODE: Forcing BrlAPI mark-dead + retry.",
                    True,
                )
                braille._mark_brlapi_dead("stress-mode synthetic failure")  # pylint: disable=protected-access
        except Exception as error:  # pylint: disable=broad-except
            debug.print_message(
                debug.LEVEL_WARNING,
                f"STRESS MODE: brlapi force-dead failed: {error}",
                True,
            )
        self._rearm("brlapi")
        return False

    def _fire_navcache(self) -> bool:
        """Mark all nav-cache entries stale and trigger the rebuild path."""

        try:
            from . import structural_navigator  # pylint: disable=import-outside-toplevel
            nav = structural_navigator.get_navigator()
            with nav._nav_cache_lock:  # pylint: disable=protected-access
                stale = set(nav._nav_cache.keys())  # pylint: disable=protected-access
                nav._nav_cache_stale_keys.update(stale)  # pylint: disable=protected-access
            if stale:
                # Use the same debouncer arm() the real invalidator uses.
                nav._nav_cache_drop_debouncer.arm(  # pylint: disable=protected-access
                    nav._NAV_CACHE_DROP_DELAY_MS,  # pylint: disable=protected-access
                )
                debug.print_message(
                    debug.LEVEL_WARNING,
                    f"STRESS MODE: Marked {len(stale)} nav-cache entries stale.",
                    True,
                )
        except Exception as error:  # pylint: disable=broad-except
            debug.print_message(
                debug.LEVEL_WARNING,
                f"STRESS MODE: navcache stress failed: {error}",
                True,
            )
        self._rearm("navcache")
        return False

    def _fire_event_flood(self) -> bool:
        """Log a marker for now -- real event injection requires a fake AT-SPI source.

        Not yet implemented (synthetic Atspi.Event requires Atspi
        internal-state surgery). Placeholder leaves the hook in place
        so the design contract is visible.
        """

        debug.print_message(
            debug.LEVEL_WARNING,
            "STRESS MODE: event-flood stressor is a placeholder (not yet implemented).",
            True,
        )
        self._rearm("event-flood")
        return False


_stress_mode: StressMode | None = None


def init_from_environment() -> None:
    """Call once at startup. No-op if ORCA_STRESS is unset.

    Idempotent: a second call after the first init does nothing.
    """

    global _stress_mode  # pylint: disable=global-statement
    if _stress_mode is not None:
        return
    raw = os.environ.get(_ENV_VAR, "")
    enabled = _parse_flags(raw)
    if not enabled:
        return
    _stress_mode = StressMode(enabled)
    _stress_mode.start()


def shutdown() -> None:
    """Cancel all stressor timers. Called from Orca's shutdown path."""

    global _stress_mode  # pylint: disable=global-statement
    if _stress_mode is not None:
        _stress_mode.stop()
        _stress_mode = None
