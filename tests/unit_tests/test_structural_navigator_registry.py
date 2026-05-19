# Orca
#
# Copyright 2026 Cody Hurst
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

"""Unit tests for orca.structural_navigator_registry (Phase 2 skeleton).

The registry has no runtime dependency on ``structural_navigator`` or
AT-SPI; ``mode_support`` is a ``frozenset`` of opaque enum values, so
these tests substitute a tiny local ``Enum`` rather than importing
the real ``NavigationMode`` (which would drag in the entire
structural-navigation import chain for tests that should be fast and
hermetic).
"""

from __future__ import annotations

from enum import Enum

import pytest


class _FakeMode(Enum):
    OFF = "OFF"
    DOCUMENT = "DOCUMENT"
    GUI = "GUI"


def _matcher(_script):
    return []


def _row(_script, _obj):
    return []


def _make_type(name: str, *modes: _FakeMode, **overrides):
    from orca.structural_navigator_registry import ElementType

    defaults = {
        "name": name,
        "mode_support": frozenset(modes) if modes else frozenset({_FakeMode.DOCUMENT}),
        "matcher": _matcher,
        "no_more_message": f"NO_MORE_{name.upper()}",
    }
    defaults.update(overrides)
    return ElementType(**defaults)


def _fresh_registry():
    from orca.structural_navigator_registry import ElementRegistry

    return ElementRegistry()


@pytest.mark.unit
class TestElementType:
    """Test the ElementType dataclass."""

    def test_minimal_construction(self) -> None:
        et = _make_type("buttons")
        assert et.name == "buttons"
        assert et.cache_key == "buttons"
        assert et.matcher is _matcher
        assert et.no_more_message == "NO_MORE_BUTTONS"
        assert et.list_dialog_headers == ()
        assert et.list_row_builder is None
        assert et.cache_invalidation_roles == frozenset()

    def test_cache_key_defaults_to_name(self) -> None:
        assert _make_type("buttons").cache_key == "buttons"

    def test_cache_key_can_be_overridden_for_parameterized_types(self) -> None:
        # The 6 heading-level variants share a single cache slot.
        h1 = _make_type("headings_level_1", cache_key="headings")
        h2 = _make_type("headings_level_2", cache_key="headings")
        assert h1.cache_key == h2.cache_key == "headings"

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            _make_type("")

    def test_empty_mode_support_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode_support must list at least one"):
            from orca.structural_navigator_registry import ElementType
            ElementType(
                name="buttons",
                mode_support=frozenset(),
                matcher=_matcher,
                no_more_message="x",
            )

    def test_frozen_instance_is_immutable(self) -> None:
        et = _make_type("buttons")
        with pytest.raises((AttributeError, TypeError)):
            et.name = "links"  # type: ignore[misc]

    def test_list_dialog_metadata_round_trip(self) -> None:
        et = _make_type(
            "buttons",
            list_dialog_title="Buttons",
            list_dialog_headers=("Name", "Role"),
            list_row_builder=_row,
        )
        assert et.list_dialog_title == "Buttons"
        assert et.list_dialog_headers == ("Name", "Role")
        assert et.list_row_builder is _row


@pytest.mark.unit
class TestElementRegistry:
    """Test the ElementRegistry container."""

    def test_register_and_get(self) -> None:
        reg = _fresh_registry()
        et = _make_type("buttons")
        reg.register(et)
        assert reg.get("buttons") is et

    def test_register_duplicate_raises(self) -> None:
        reg = _fresh_registry()
        reg.register(_make_type("buttons"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_make_type("buttons"))

    def test_get_missing_raises_key_error(self) -> None:
        reg = _fresh_registry()
        with pytest.raises(KeyError):
            reg.get("buttons")

    def test_contains_and_len(self) -> None:
        reg = _fresh_registry()
        assert "buttons" not in reg
        assert len(reg) == 0
        reg.register(_make_type("buttons"))
        reg.register(_make_type("links"))
        assert "buttons" in reg
        assert "headings" not in reg
        assert len(reg) == 2

    def test_iter_for_mode_filters_by_mode_support(self) -> None:
        reg = _fresh_registry()
        doc_only = _make_type("headings", _FakeMode.DOCUMENT)
        gui_only = _make_type("buttons_gui", _FakeMode.GUI)
        both = _make_type("buttons", _FakeMode.DOCUMENT, _FakeMode.GUI)
        reg.register(doc_only)
        reg.register(gui_only)
        reg.register(both)

        assert list(reg.iter_for_mode(_FakeMode.DOCUMENT)) == [doc_only, both]
        assert list(reg.iter_for_mode(_FakeMode.GUI)) == [gui_only, both]
        assert list(reg.iter_for_mode(_FakeMode.OFF)) == []

    def test_iter_for_mode_preserves_registration_order(self) -> None:
        reg = _fresh_registry()
        names = ["headings", "links", "buttons", "tables"]
        for name in names:
            reg.register(_make_type(name, _FakeMode.DOCUMENT))
        assert [et.name for et in reg.iter_for_mode(_FakeMode.DOCUMENT)] == names


@pytest.mark.unit
class TestRegistrySingleton:
    """Test the module-level singleton."""

    def test_get_registry_returns_same_instance(self) -> None:
        from orca.structural_navigator_registry import get_registry
        assert get_registry() is get_registry()

    def test_singleton_starts_empty(self) -> None:
        # conftest.py wipes orca modules from sys.modules before each test,
        # so the singleton is fresh per test.
        from orca.structural_navigator_registry import get_registry
        assert len(get_registry()) == 0


@pytest.mark.unit
class TestFormatArg:
    """Test the lazy-format resolve_* helpers."""

    def test_resolve_no_more_message_without_arg(self) -> None:
        et = _make_type("buttons", no_more_message="no more buttons")
        assert et.resolve_no_more_message() == "no more buttons"

    def test_resolve_no_more_message_with_arg(self) -> None:
        et = _make_type(
            "heading_level_3",
            no_more_message="no more headings at level %d",
            format_arg=3,
        )
        assert et.resolve_no_more_message() == "no more headings at level 3"

    def test_resolve_list_dialog_title_without_arg(self) -> None:
        et = _make_type(
            "buttons",
            list_dialog_title="Buttons",
        )
        assert et.resolve_list_dialog_title() == "Buttons"

    def test_resolve_list_dialog_title_with_arg(self) -> None:
        et = _make_type(
            "heading_level_3",
            list_dialog_title="Headings at level %d",
            format_arg=3,
        )
        assert et.resolve_list_dialog_title() == "Headings at level 3"
