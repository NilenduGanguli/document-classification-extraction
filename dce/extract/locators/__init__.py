"""Locators: the six ways this service finds a value on a page.

Every locator exposes the same function::

    locate(field: FieldSpec, view: LayoutView, ctx: LocatorContext) -> list[Candidate]

and none of them decides anything. They propose candidates with a confidence and a
provenance trail; :mod:`dce.extract.resolve` scores, validates and picks. Adding a locator
is therefore additive by construction — register it in :data:`LOCATORS`, name it in a
``FieldSpec.locators`` list, and no existing field changes behaviour.

======  ==================================================================
Name    What it knows
======  ==================================================================
mrz     ICAO 9303 zones; carries its own check digits, so it wins outright
kv      Provider-detected key/value pairs; the provider did the geometry
table   Header-cell addressing, including merged spans
mark    Checkboxes bound to their labels — often the actual answer
label   Label-anchored lookup: same line, right of, or below
regex   Shape sweep in zone order; the last resort
======  ==================================================================
"""
from __future__ import annotations

from collections.abc import Callable

from dce.extract.locators import geometry, kv, label, mark, mrz, regex, table
from dce.extract.locators.base import (
    LOCATOR_PRIOR,
    Candidate,
    LocatorContext,
    clean_value,
    field_labels,
    label_similarity,
    match_label,
    normalize_label,
    passes_pattern,
    refine_to_pattern,
)
from dce.models import FieldSpec, LayoutView

__all__ = [
    "LOCATORS",
    "LOCATOR_PRIOR",
    "Candidate",
    "LocateFn",
    "LocatorContext",
    "clean_value",
    "field_labels",
    "geometry",
    "get_locator",
    "label_similarity",
    "match_label",
    "normalize_label",
    "passes_pattern",
    "refine_to_pattern",
]

LocateFn = Callable[[FieldSpec, LayoutView, LocatorContext], list[Candidate]]

#: Name -> locator function. ``FieldSpec.locators`` holds keys of this mapping.
LOCATORS: dict[str, LocateFn] = {
    "kv": kv.locate,
    "label": label.locate,
    "table": table.locate,
    "mark": mark.locate,
    "regex": regex.locate,
    "mrz": mrz.locate,
}


def get_locator(name: str) -> LocateFn | None:
    """Return the locator registered under ``name``, or ``None`` if there is none."""
    return LOCATORS.get(name)
