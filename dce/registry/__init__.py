"""The doctype registry — the knowledge base the whole service runs on.

Importing this package imports every doctype pack (their import side-effect is to register
themselves) and then runs :func:`~dce.registry.loader.validate_registry` over the combined
result. A malformed or mutually-indistinguishable pack therefore raises here, at process
start, rather than degrading classification quietly in production.

Typical use::

    from dce.registry import all_specs, get, by_country

    spec = get("in_aadhaar")
    indian = by_country()["IN"]

Nothing in this package performs I/O or touches the network. That is not incidental: the
registry is the entire classification knowledge base precisely so that classification can
run in-process, before the document type is known and therefore before anything may leave
the container.
"""

from __future__ import annotations

# Every pack is imported for its registration side-effect, and every pack must be imported
# HERE: a pack that only gets imported by its own test module is a pack the running service
# does not have. Import order does not matter — crosscountry imports india itself for the
# field builders it reuses, so Python resolves the dependency whichever way this list is
# sorted.
from dce.registry import canada as canada
from dce.registry import crosscountry as crosscountry
from dce.registry import india as india
from dce.registry import mexico as mexico
from dce.registry import usa as usa
from dce.registry.loader import (
    ATTRIBUTE_KEYS,
    KNOWN_FIELD_TYPES,
    KNOWN_LOCATORS,
    PENDING_VALIDATORS,
    VALIDATOR_CONTRACT,
    RegistryError,
    all_specs,
    by_country,
    get,
    register,
    register_all,
    require,
    required_validators,
    validate_registry,
)

validate_registry()

__all__ = [
    "ATTRIBUTE_KEYS",
    "KNOWN_FIELD_TYPES",
    "KNOWN_LOCATORS",
    "PENDING_VALIDATORS",
    "VALIDATOR_CONTRACT",
    "RegistryError",
    "all_specs",
    "by_country",
    "canada",
    "crosscountry",
    "get",
    "india",
    "mexico",
    "register",
    "register_all",
    "require",
    "required_validators",
    "usa",
    "validate_registry",
]
