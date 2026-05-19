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

"""Single-pending GLib timer source for coalesced deferred work.

Before this helper existed, three subsystems hand-rolled the same
pattern: keep a `_pending_timer_id` member, arm it once on first event,
no-op subsequent arms, reset to zero when the timer fires.
structural_navigator's deferred nav-cache drop, mouse_review's burst
coalesce, and flat_review_presenter's idle-deferred event processing
all had slightly different bugs around cancel/cleanup. Consolidating
into one helper eliminates the duplication and lets future timer-based
coalescing reuse the well-tested implementation.

Usage:

    debouncer = DebouncedCallable(self._do_work)
    debouncer.arm(150)              # fires _do_work() in 150ms if not already armed
    debouncer.cancel()              # remove pending fire, if any
    debouncer.arm_or_reset(150)     # cancel any pending fire and reschedule

`arm` is the burst-coalesce pattern: many events in a burst result in
ONE deferred fire. `arm_or_reset` is the trailing-edge pattern: the
fire is pushed out by each new event, executing only after the burst
settles.

Callbacks must return a bool: False to indicate one-shot (the standard
mode this helper assumes), True to keep firing on a recurring schedule
(rare; in that case do not use this helper, schedule directly).

Thread safety: the internal source-id is read/written under a lock so
arming from one thread while another fires is safe. The callback itself
runs on the GLib main thread (per GLib.timeout_add / idle_add semantics).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from gi.repository import GLib

if TYPE_CHECKING:
    from collections.abc import Callable


class DebouncedCallable:
    """Wraps a callback with a single pending GLib timer source.

    Multiple calls to arm() while a fire is pending are no-ops -- the
    callback runs once at the scheduled time. Use arm_or_reset() to
    push the fire time out on each call (trailing-edge debounce).
    """

    def __init__(self, callback: Callable[[], bool]) -> None:
        """Initialize the debouncer.

        callback must return False (one-shot) -- the helper does not
        support recurring schedules.
        """

        self._callback = callback
        self._source_id: int = 0
        self._lock = threading.Lock()

    def arm(self, delay_ms: int, priority: int = GLib.PRIORITY_DEFAULT) -> None:
        """Schedule the callback in delay_ms if no fire is already pending.

        Subsequent arms before the fire are no-ops -- the original
        scheduled time stands. This is the burst-coalesce pattern.
        """

        with self._lock:
            if self._source_id:
                return
            self._source_id = GLib.timeout_add(delay_ms, self._fire, priority=priority)

    def arm_or_reset(self, delay_ms: int, priority: int = GLib.PRIORITY_DEFAULT) -> None:
        """Cancel any pending fire and schedule afresh in delay_ms.

        Use when each new event should push the fire time out -- the
        callback runs only after a quiet window of delay_ms elapses.
        """

        with self._lock:
            if self._source_id:
                GLib.source_remove(self._source_id)
                self._source_id = 0
            self._source_id = GLib.timeout_add(delay_ms, self._fire, priority=priority)

    def arm_idle(self, priority: int = GLib.PRIORITY_DEFAULT_IDLE) -> None:
        """Schedule the callback at idle priority if no fire is already pending.

        For deferred work that should run "when the main loop has nothing
        else to do" rather than at a fixed delay.
        """

        with self._lock:
            if self._source_id:
                return
            self._source_id = GLib.idle_add(self._fire, priority=priority)

    def cancel(self) -> None:
        """Remove the pending fire, if any. Safe to call when no fire is pending."""

        with self._lock:
            if self._source_id:
                GLib.source_remove(self._source_id)
                self._source_id = 0

    def is_pending(self) -> bool:
        """Returns True if a fire is scheduled but has not yet run."""

        with self._lock:
            return self._source_id != 0

    def _fire(self) -> bool:
        """Internal: reset state then invoke the callback."""

        with self._lock:
            self._source_id = 0
        # Run the user callback outside the lock so a callback that
        # arms() this same debouncer (e.g. for self-rescheduling
        # patterns) doesn't deadlock.
        self._callback()
        return False  # one-shot
