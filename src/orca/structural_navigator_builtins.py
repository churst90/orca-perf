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

"""Builtin ``ElementType`` registrations for structural navigation.

Each of the 24 hand-written ``_get_all_X`` / ``previous_X`` / ``next_X`` /
``list_X`` clusters in ``structural_navigator.py`` is mirrored here as
one ``ElementType`` record. The matchers and row builders close over the
``StructuralNavigator`` instance passed to ``register_builtins`` so they
can dispatch to the navigator's existing helpers without duplicating
them.

This commit is purely additive -- no consumer reads from the registry
yet, so behavior is unchanged. The follow-on commit (Phase 2 step 4)
will introduce the generic dispatcher that consults the registry
instead of the hand-written per-type commands.

Element-type names mirror the existing ``previous_X`` / ``next_X`` /
``list_X`` suffixes so the future dispatcher's lookup table matches
the keybinding command names one-to-one. The heading-by-level
variants share ``cache_key="headings"`` because the underlying
matcher reuses the unfiltered headings cache slot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import guilabels, messages
from .ax_hypertext import AXHypertext
from .ax_text import AXText
from .ax_utilities import AXUtilities
from .structural_navigator_registry import ElementType, get_registry

if TYPE_CHECKING:
    from .structural_navigator import StructuralNavigator


_HEADING_LEVELS = (1, 2, 3, 4, 5, 6)


def register_builtins(navigator: StructuralNavigator) -> None:
    """Populate the singleton registry with the 24 built-in element types.

    Idempotent across navigator re-construction within a single Python
    process: clears any prior registrations first so re-running
    ``StructuralNavigator.__init__`` (e.g. across tests that wipe and
    re-import ``orca`` modules) does not raise ``ValueError`` from
    ``register()`` rejecting duplicates.
    """

    # Import locally to avoid a top-of-module circular import:
    # structural_navigator imports this module (transitively, via
    # __init__), so we cannot import NavigationMode at module load.
    from .structural_navigator import NavigationMode  # pylint: disable=import-outside-toplevel

    registry = get_registry()
    registry._by_name.clear()  # pylint: disable=protected-access

    doc_only = frozenset({NavigationMode.DOCUMENT})
    both = frozenset({NavigationMode.DOCUMENT, NavigationMode.GUI})

    # The matcher closures below all take the form
    #   lambda script, _nav=navigator: _nav._get_all_X(script)
    # The default-argument binding is required: a bare reference to
    # ``navigator`` would late-bind, which is harmless today but would
    # break the moment two navigators coexist (tests sometimes wipe and
    # rebuild). Defaults bind at definition time -- safer and idiomatic.

    # ---- Annotations ----
    registry.register(ElementType(
        name="annotation",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_annotations(s),
        no_more_message=messages.NO_MORE_ANNOTATIONS,
        list_dialog_title=guilabels.SN_TITLE_ANNOTATION,
        list_dialog_headers=(guilabels.SN_HEADER_ANNOTATION, guilabels.SN_HEADER_ROLE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_localized_role_name(o),
        ],
    ))

    # ---- Blockquotes ----
    registry.register(ElementType(
        name="blockquote",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_blockquotes(s),
        no_more_message=messages.NO_MORE_BLOCKQUOTES,
        list_dialog_title=guilabels.SN_TITLE_BLOCKQUOTE,
        list_dialog_headers=(guilabels.SN_HEADER_BLOCKQUOTE,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Buttons ----
    registry.register(ElementType(
        name="button",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_buttons(s),
        no_more_message=messages.NO_MORE_BUTTONS,
        list_dialog_title=guilabels.SN_TITLE_BUTTON,
        list_dialog_headers=(guilabels.SN_HEADER_BUTTON,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Check boxes ----
    registry.register(ElementType(
        name="checkbox",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_checkboxes(s),
        no_more_message=messages.NO_MORE_CHECK_BOXES,
        list_dialog_title=guilabels.SN_TITLE_CHECK_BOX,
        list_dialog_headers=(guilabels.SN_HEADER_CHECK_BOX, guilabels.SN_HEADER_STATE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            _n._get_state_string(o),
        ],
    ))

    # ---- Large objects ----
    registry.register(ElementType(
        name="large_object",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_large_objects(s),
        no_more_message=messages.NO_MORE_LARGE_OBJECTS,
        list_dialog_title=guilabels.SN_TITLE_LARGE_OBJECT,
        list_dialog_headers=(guilabels.SN_HEADER_OBJECT, guilabels.SN_HEADER_ROLE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_localized_role_name(o),
        ],
    ))

    # ---- Combo boxes ----
    registry.register(ElementType(
        name="combobox",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_comboboxes(s),
        no_more_message=messages.NO_MORE_COMBO_BOXES,
        list_dialog_title=guilabels.SN_TITLE_COMBO_BOX,
        list_dialog_headers=(guilabels.SN_HEADER_COMBO_BOX,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Entries ----
    registry.register(ElementType(
        name="entry",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_entries(s),
        no_more_message=messages.NO_MORE_ENTRIES,
        list_dialog_title=guilabels.SN_TITLE_ENTRY,
        list_dialog_headers=(guilabels.SN_HEADER_LABEL, guilabels.SN_HEADER_VALUE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXText.get_all_text(o),
        ],
    ))

    # ---- Form fields ----
    registry.register(ElementType(
        name="form_field",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_form_fields(s),
        no_more_message=messages.NO_MORE_FORM_FIELDS,
        list_dialog_title=guilabels.SN_TITLE_FORM_FIELD,
        list_dialog_headers=(
            guilabels.SN_HEADER_LABEL,
            guilabels.SN_HEADER_ROLE,
            guilabels.SN_HEADER_VALUE,
        ),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_localized_role_name(o),
            AXText.get_all_text(o),
        ],
    ))

    # ---- Headings (any level) ----
    registry.register(ElementType(
        name="heading",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_headings(s),
        no_more_message=messages.NO_MORE_HEADINGS,
        list_dialog_title=guilabels.SN_TITLE_HEADING,
        list_dialog_headers=(guilabels.SN_HEADER_HEADING, guilabels.SN_HEADER_LEVEL),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            str(AXUtilities.get_heading_level(o)),
        ],
    ))

    # ---- Headings by level (1..6) ----
    # no_more_message / list_dialog_title are stored as the raw template
    # strings; format_arg=level tells the dispatcher to do `% level` at
    # present time. See ElementType.resolve_no_more_message().
    for level in _HEADING_LEVELS:
        registry.register(ElementType(
            name=f"heading_level_{level}",
            mode_support=both,
            matcher=lambda s, _n=navigator, _lv=level: _n._get_all_headings(s, _lv),
            no_more_message=messages.NO_MORE_HEADINGS_AT_LEVEL,
            list_dialog_title=guilabels.SN_TITLE_HEADING_AT_LEVEL,
            list_dialog_headers=(guilabels.SN_HEADER_HEADING,),
            list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
            cache_key="headings",
            format_arg=level,
        ))

    # ---- Iframes ----
    registry.register(ElementType(
        name="iframe",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_iframes(s),
        no_more_message=messages.NO_MORE_IFRAMES,
        list_dialog_title=guilabels.SN_TITLE_IFRAME,
        list_dialog_headers=(guilabels.SN_HEADER_IFRAME,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Images ----
    registry.register(ElementType(
        name="image",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_images(s),
        no_more_message=messages.NO_MORE_IMAGES,
        list_dialog_title=guilabels.SN_TITLE_IMAGE,
        list_dialog_headers=(guilabels.SN_HEADER_IMAGE,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Landmarks ----
    registry.register(ElementType(
        name="landmark",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_landmarks(s),
        # Landmarks use the special _present_landmark presenter, which
        # passes NO_LANDMARK_FOUND rather than a NO_MORE_LANDMARKS string.
        no_more_message=messages.NO_LANDMARK_FOUND,
        list_dialog_title=guilabels.SN_TITLE_LANDMARK,
        list_dialog_headers=(guilabels.SN_HEADER_LANDMARK, guilabels.SN_HEADER_ROLE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_localized_role_name(o),
        ],
    ))

    # ---- Lists ----
    registry.register(ElementType(
        name="list",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_lists(s),
        no_more_message=messages.NO_MORE_LISTS,
        list_dialog_title=guilabels.SN_TITLE_LIST,
        list_dialog_headers=(guilabels.SN_HEADER_LIST,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- List items ----
    registry.register(ElementType(
        name="list_item",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_list_items(s),
        no_more_message=messages.NO_MORE_LIST_ITEMS,
        list_dialog_title=guilabels.SN_TITLE_LIST_ITEM,
        list_dialog_headers=(guilabels.SN_HEADER_LIST_ITEM,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Live regions (no list dialog) ----
    registry.register(ElementType(
        name="live_region",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_live_regions(s),
        no_more_message=messages.NO_MORE_LIVE_REGIONS,
    ))

    # ---- Paragraphs ----
    registry.register(ElementType(
        name="paragraph",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_paragraphs(s),
        no_more_message=messages.NO_MORE_PARAGRAPHS,
        list_dialog_title=guilabels.SN_TITLE_PARAGRAPH,
        list_dialog_headers=(guilabels.SN_HEADER_PARAGRAPH,),
        list_row_builder=lambda s, o, _n=navigator: [_n._get_item_string(s, o)],
    ))

    # ---- Radio buttons ----
    registry.register(ElementType(
        name="radio_button",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_radio_buttons(s),
        no_more_message=messages.NO_MORE_RADIO_BUTTONS,
        list_dialog_title=guilabels.SN_TITLE_RADIO_BUTTON,
        list_dialog_headers=(guilabels.SN_HEADER_RADIO_BUTTON, guilabels.SN_HEADER_STATE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            _n._get_state_string(o),
        ],
    ))

    # ---- Separators (no list dialog) ----
    registry.register(ElementType(
        name="separator",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_separators(s),
        no_more_message=messages.NO_MORE_SEPARATORS,
    ))

    # ---- Tables ----
    registry.register(ElementType(
        name="table",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_tables(s),
        no_more_message=messages.NO_MORE_TABLES,
        list_dialog_title=guilabels.SN_TITLE_TABLE,
        list_dialog_headers=(guilabels.SN_HEADER_CAPTION, guilabels.SN_HEADER_DESCRIPTION),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_table_description_for_presentation(o),
        ],
    ))

    # ---- Unvisited links ----
    registry.register(ElementType(
        name="unvisited_link",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_unvisited_links(s),
        no_more_message=messages.NO_MORE_UNVISITED_LINKS,
        list_dialog_title=guilabels.SN_TITLE_UNVISITED_LINK,
        list_dialog_headers=(guilabels.SN_HEADER_LINK, guilabels.SN_HEADER_URI),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXHypertext.get_link_uri(o),
        ],
    ))

    # ---- Visited links ----
    registry.register(ElementType(
        name="visited_link",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_visited_links(s),
        no_more_message=messages.NO_MORE_VISITED_LINKS,
        list_dialog_title=guilabels.SN_TITLE_VISITED_LINK,
        list_dialog_headers=(guilabels.SN_HEADER_LINK, guilabels.SN_HEADER_URI),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXHypertext.get_link_uri(o),
        ],
    ))

    # ---- Links (all) ----
    registry.register(ElementType(
        name="link",
        mode_support=both,
        matcher=lambda s, _n=navigator: _n._get_all_links(s),
        no_more_message=messages.NO_MORE_LINKS,
        list_dialog_title=guilabels.SN_TITLE_LINK,
        list_dialog_headers=(
            guilabels.SN_HEADER_LINK,
            guilabels.SN_HEADER_STATE,
            guilabels.SN_HEADER_URI,
        ),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            _n._get_state_string(o),
            AXHypertext.get_link_uri(o),
        ],
    ))

    # ---- Clickables ----
    registry.register(ElementType(
        name="clickable",
        mode_support=doc_only,
        matcher=lambda s, _n=navigator: _n._get_all_clickables(s),
        no_more_message=messages.NO_MORE_CLICKABLES,
        list_dialog_title=guilabels.SN_TITLE_CLICKABLE,
        list_dialog_headers=(guilabels.SN_HEADER_CLICKABLE, guilabels.SN_HEADER_ROLE),
        list_row_builder=lambda s, o, _n=navigator: [
            _n._get_item_string(s, o),
            AXUtilities.get_localized_role_name(o),
        ],
    ))


# Names of every type registered by register_builtins(), in order.
# Exposed for the unit test (and any future debug surface) so the
# expected set stays a single source of truth.
BUILTIN_ELEMENT_TYPE_NAMES: tuple[str, ...] = (
    "annotation",
    "blockquote",
    "button",
    "checkbox",
    "large_object",
    "combobox",
    "entry",
    "form_field",
    "heading",
    "heading_level_1",
    "heading_level_2",
    "heading_level_3",
    "heading_level_4",
    "heading_level_5",
    "heading_level_6",
    "iframe",
    "image",
    "landmark",
    "list",
    "list_item",
    "live_region",
    "paragraph",
    "radio_button",
    "separator",
    "table",
    "unvisited_link",
    "visited_link",
    "link",
    "clickable",
)
