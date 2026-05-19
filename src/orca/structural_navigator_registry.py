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

"""Element-type registry for structural navigation (Phase 2 skeleton).

Phase 2 of the perf branch collapses the ~4,000-line
``structural_navigator.py`` body. Today each of the 23 element types
(buttons, headings, links, ...) has a hand-written quadruple
``_get_all_X`` / ``previous_X`` / ``next_X`` / ``list_X``. The
``previous`` / ``next`` / ``list`` methods are almost pure boilerplate
that differs only in (a) which matcher they call, (b) which "no more"
message they present, and (c) the list-dialog title / headers / row
builder. ``ElementType`` captures (a)-(c) as data; ``ElementRegistry``
holds the table.

This module ships the skeleton only -- no element types are registered
yet and ``StructuralNavigator`` is unchanged. Subsequent commits will
migrate the existing per-type methods to ``ElementType`` records and
finally replace the hand-written ``next_*``/``previous_*``/``list_*``
trio with a generic dispatcher that looks up the type by name.

``NavigationMode`` deliberately lives in ``structural_navigator``: it
is referenced by 15+ external call sites (document_presenter, script,
the web script, the existing structural-navigation test suite),
moving it would force every one of them to change for a commit whose
only job is to introduce the registry. The annotation here is a
``TYPE_CHECKING`` forward reference; the enum values stored in
``mode_support`` come from the caller, so the registry never has to
import ``structural_navigator`` at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from gi.repository import Atspi

    from .structural_navigator import NavigationMode
    from .scripts import default


@dataclass(frozen=True, slots=True)
class ElementType:
    """One row of the structural-navigation element table.

    ``matcher`` is the only piece of behavior; everything else is the
    presentation metadata that the hand-written commands currently
    repeat verbatim. ``cache_key`` defaults to ``name`` and exists as
    a separate field only because heading-at-level variants share one
    underlying cache slot ("headings") while having distinct names
    ("headings_level_1", ...).
    """

    name: str
    mode_support: frozenset[NavigationMode]
    matcher: Callable[[default.Script], list[Atspi.Accessible]]
    no_more_message: str
    list_dialog_title: str = ""
    list_dialog_headers: tuple[str, ...] = ()
    list_row_builder: Callable[[default.Script, Atspi.Accessible], list[str]] | None = None
    cache_key: str = ""
    cache_invalidation_roles: frozenset[str] = field(default_factory=frozenset)
    # If set, the dispatcher will format ``no_more_message`` and
    # ``list_dialog_title`` as ``template % format_arg`` at presentation
    # time. Keeping the templates unformatted at registration time means
    # the dataclass holds the gettext-friendly form, and means
    # ``register_builtins`` does not need to evaluate ``%`` against
    # constants -- useful for tests that mock the messages module.
    format_arg: int | None = None

    def resolve_no_more_message(self) -> str:
        """Returns ``no_more_message`` with ``format_arg`` substituted if set."""

        if self.format_arg is None:
            return self.no_more_message
        return self.no_more_message % self.format_arg

    def resolve_list_dialog_title(self) -> str:
        """Returns ``list_dialog_title`` with ``format_arg`` substituted if set."""

        if self.format_arg is None:
            return self.list_dialog_title
        return self.list_dialog_title % self.format_arg

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ElementType.name must be non-empty")
        if not self.mode_support:
            raise ValueError(
                f"ElementType {self.name!r}: mode_support must list at least one mode",
            )
        if not self.cache_key:
            # frozen dataclass: assign via object.__setattr__
            object.__setattr__(self, "cache_key", self.name)


class ElementRegistry:
    """Ordered table of ``ElementType`` records.

    Order matters for two reasons: ``iter_for_mode`` yields in
    registration order so the future generic dispatcher can build
    deterministic keybinding tables, and a follow-on optimization may
    classify a single tree walk against every matcher in this order
    (first match wins per node).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, ElementType] = {}

    def register(self, element_type: ElementType) -> None:
        """Adds ``element_type``; raises ``ValueError`` on duplicate name."""

        if element_type.name in self._by_name:
            raise ValueError(f"ElementType {element_type.name!r} already registered")
        self._by_name[element_type.name] = element_type

    def get(self, name: str) -> ElementType:
        """Returns the registered ``ElementType``; raises ``KeyError`` if absent."""

        return self._by_name[name]

    def iter_for_mode(self, mode: NavigationMode) -> Iterator[ElementType]:
        """Yields registered types whose ``mode_support`` contains ``mode``."""

        for element_type in self._by_name.values():
            if mode in element_type.mode_support:
                yield element_type

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)


_REGISTRY = ElementRegistry()


def get_registry() -> ElementRegistry:
    """Returns the process-wide ``ElementRegistry`` singleton."""

    return _REGISTRY
