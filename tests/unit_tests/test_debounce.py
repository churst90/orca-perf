# Orca
#
# Copyright 2026 Cody Hurst
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

"""Unit tests for orca.util.debounce.DebouncedCallable."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import gi
import pytest

gi.require_version("Atspi", "2.0")
from gi.repository import GLib

if TYPE_CHECKING:
    from unittest.mock import MagicMock

    from .orca_test_context import OrcaTestContext


@pytest.mark.unit
class TestDebouncedCallable:
    """Test DebouncedCallable."""

    def _import(self):
        """Import the helper without requiring the heavy orca setup."""

        # The helper is dependency-free; no module mocking needed.
        # Just make sure src/ is on the path (it is via pytest config).
        from orca.util.debounce import DebouncedCallable
        return DebouncedCallable

    def test_arm_schedules_timeout(self, test_context: OrcaTestContext) -> None:
        """arm() must call GLib.timeout_add and store the source id."""

        timeout_add = test_context.Mock(return_value=42)
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        DebouncedCallable = self._import()  # noqa: N806

        callback = test_context.Mock(return_value=False)
        d = DebouncedCallable(callback)
        d.arm(150)

        timeout_add.assert_called_once()
        assert d.is_pending() is True
        callback.assert_not_called()

    def test_arm_while_pending_is_noop(self, test_context: OrcaTestContext) -> None:
        """A second arm() before the timer fires must not schedule again."""

        timeout_add = test_context.Mock(side_effect=[42, 43])
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(test_context.Mock(return_value=False))
        d.arm(150)
        d.arm(150)
        d.arm(150)

        assert timeout_add.call_count == 1

    def test_arm_or_reset_cancels_and_reschedules(self, test_context: OrcaTestContext) -> None:
        """arm_or_reset() must cancel any pending timer and arm a new one."""

        timeout_add = test_context.Mock(side_effect=[42, 43])
        source_remove = test_context.Mock()
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        test_context.patch("gi.repository.GLib.source_remove", new=source_remove)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(test_context.Mock(return_value=False))
        d.arm(150)
        d.arm_or_reset(150)

        source_remove.assert_called_once_with(42)
        assert timeout_add.call_count == 2

    def test_arm_idle_schedules_idle_add(self, test_context: OrcaTestContext) -> None:
        """arm_idle() must call GLib.idle_add, not timeout_add."""

        timeout_add = test_context.Mock()
        idle_add = test_context.Mock(return_value=99)
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        test_context.patch("gi.repository.GLib.idle_add", new=idle_add)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(test_context.Mock(return_value=False))
        d.arm_idle()

        idle_add.assert_called_once()
        timeout_add.assert_not_called()

    def test_cancel_removes_pending_source(self, test_context: OrcaTestContext) -> None:
        """cancel() must call GLib.source_remove and clear the pending flag."""

        timeout_add = test_context.Mock(return_value=42)
        source_remove = test_context.Mock()
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        test_context.patch("gi.repository.GLib.source_remove", new=source_remove)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(test_context.Mock(return_value=False))
        d.arm(150)
        d.cancel()

        source_remove.assert_called_once_with(42)
        assert d.is_pending() is False

    def test_cancel_when_not_armed_is_safe(self, test_context: OrcaTestContext) -> None:
        """cancel() must be a no-op when no fire is pending."""

        source_remove = test_context.Mock()
        test_context.patch("gi.repository.GLib.source_remove", new=source_remove)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(test_context.Mock(return_value=False))
        d.cancel()

        source_remove.assert_not_called()

    def test_fire_resets_pending_and_invokes_callback(
        self,
        test_context: OrcaTestContext,
    ) -> None:
        """When the GLib timer fires, the pending flag must reset and callback run."""

        timeout_add = test_context.Mock(return_value=42)
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        DebouncedCallable = self._import()  # noqa: N806

        callback = test_context.Mock(return_value=False)
        d = DebouncedCallable(callback)
        d.arm(150)
        # Simulate GLib firing the timer.
        result = d._fire()

        assert result is False  # one-shot
        callback.assert_called_once()
        assert d.is_pending() is False

    def test_fire_callback_can_rearm_without_deadlock(
        self,
        test_context: OrcaTestContext,
    ) -> None:
        """A self-rescheduling callback must not deadlock on the internal lock.

        The probe pattern in braille and speechdispatcherfactory uses
        this: the timer fires, runs the work, then arms itself again.
        """

        timeout_add = test_context.Mock(side_effect=[42, 43])
        test_context.patch("gi.repository.GLib.timeout_add", new=timeout_add)
        DebouncedCallable = self._import()  # noqa: N806

        d = DebouncedCallable(lambda: False)  # placeholder, replaced below

        def rearming_callback():
            # The lock is released before the callback runs, so this
            # arm() inside the fire path must succeed.
            d.arm(150)
            return False

        d._callback = rearming_callback
        d.arm(150)
        d._fire()

        # First arm + the in-fire rearm = 2 timeout_add calls.
        assert timeout_add.call_count == 2
        assert d.is_pending() is True
