"""The doctype registry: registration, lookup, and structural validation.

This module is *mechanism*. The knowledge lives in the packs (:mod:`dce.registry.india`,
:mod:`dce.registry.crosscountry`), which call :func:`register_all` at import time.

Two design decisions are worth stating up front, because they are what stop the registry
from rotting into a pile of grep strings:

**Registration validates eagerly.** :func:`register` runs every per-spec check before the
spec enters the table, so a malformed pack raises at *import*, not at the first request
that happens to touch the broken doctype. A KYC classifier that silently carries a doctype
with an uncompilable regex is worse than one that refuses to start.

**Decisive anchors must stay distinguishing.** The cascade treats a decisive anchor as
near-proof of a doctype (``fuse_weight_anchor`` is 3.0 against 1.0 for lexical). Two
doctypes claiming the same decisive anchor therefore make each other unclassifiable at L1.
That is *sometimes legitimate* — a masked Aadhaar and a full Aadhaar really do share the
UIDAI header — so the rule is not "never collide", it is
:func:`_check_decisive_collisions`: colliding doctypes must declare each other in
``confusable_with`` (naming the term that separates them) **and** each must keep at least
one decisive anchor of its own. An undeclared collision is a bug and fails loudly.

Word counting here deliberately does *not* use :func:`dce.models.tokenize`. That tokenizer
is ``[^\\W_]+``, and Python's ``\\w`` excludes Unicode combining marks (categories Mn/Mc),
so every Devanagari matra splits a word: ``"आधार"`` tokenizes to ``["आध", "र"]``. Anchor
*specificity* is a property of the human-readable string, so :func:`_words` splits on
whitespace and punctuation only and is script-agnostic. See the note in
``tests/test_registry_india.py``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from importlib import import_module

from dce.models import Anchor, DocTypeSpec, FieldSpec

__all__ = [
    "ATTRIBUTE_KEYS",
    "KNOWN_FIELD_TYPES",
    "KNOWN_LOCATORS",
    "PENDING_VALIDATORS",
    "VALIDATOR_CONTRACT",
    "RegistryError",
    "all_specs",
    "by_country",
    "clear",
    "get",
    "register",
    "register_all",
    "require",
    "required_validators",
    "validate_registry",
]


class RegistryError(RuntimeError):
    """A doctype pack is malformed. Always raised at import time, never swallowed."""


# ---------------------------------------------------------------------------
# Canonical attribute keys — the dotted namespace the fleet merges facts on.
# Carried over verbatim from di.ontology where a key already existed, so a fact
# extracted here lands in the same bucket as the same fact extracted by the US/CA/MX
# pipeline. New keys below are India-specific extensions of the same namespaces.
# ---------------------------------------------------------------------------
ATTRIBUTE_KEYS: dict[str, str] = {
    # -- identity ---------------------------------------------------------
    "identity.full_name": "Full legal name",
    "identity.given_names": "Given name(s)",
    "identity.surname": "Surname / family name",
    "identity.father_name": "Father's name (near-universal on Indian IDs)",
    "identity.mother_name": "Mother's name",
    "identity.spouse_name": "Spouse / husband / wife name",
    "identity.guardian_name": "Guardian — the C/O or S/O line on an Aadhaar letter",
    "identity.date_of_birth": "Date of birth",
    "identity.year_of_birth": "Year of birth only (Aadhaar prints YOB when DOB is unknown)",
    "identity.age": "Stated age, as of a stated date",
    "identity.sex": "Sex / gender marker",
    "identity.nationality": "Nationality",
    "identity.place_of_birth": "Place of birth",
    "identity.mobile": "Registered mobile number",
    "identity.email": "Registered email address",
    "identity.category": "Social category (SC / ST / OBC / General)",
    # -- government identifiers ------------------------------------------
    "id.aadhaar": "Aadhaar number (12 digits, Verhoeff) — see UIDAI masking obligation",
    "id.aadhaar_last4": "Last four digits of an Aadhaar number (the only part normally storable)",
    "id.aadhaar_vid": "Aadhaar Virtual ID (16 digits)",
    "id.aadhaar_enrolment": "Aadhaar enrolment number (pre-issue acknowledgement slip)",
    "id.pan": "Permanent Account Number",
    "id.tan": "Tax Deduction and Collection Account Number",
    "id.passport_number": "Passport number",
    "id.voter_epic": "Voter EPIC number",
    "id.driving_licence": "Driving licence number",
    "id.gstin": "Goods and Services Tax Identification Number",
    "id.cin": "Corporate Identity Number (MCA)",
    "id.llpin": "LLP Identification Number (MCA)",
    "id.firm_registration_number": "Registrar of Firms registration number",
    "id.uan": "EPFO Universal Account Number",
    "id.esic": "ESIC insurance number",
    "id.ckyc_kin": "CKYC Identification Number (KIN)",
    "id.ration_card": "Ration card number",
    "id.nrega_job_card": "MGNREGA job card number",
    "id.ppo_number": "Pension Payment Order number",
    "id.property_id": "Municipal property / khata identifier",
    # -- address ----------------------------------------------------------
    "address.residential": "Residential address",
    "address.mailing": "Mailing address",
    "address.registered": "Registered / principal place of business",
    "address.postal_code": "PIN code",
    "address.district": "District",
    "address.state": "State / union territory",
    "address.village": "Village / town / locality",
    # -- financial / income ------------------------------------------------
    "account.number": "Bank account number",
    "account.balance": "Reported balance",
    "account.ifsc": "IFSC of the account's branch",
    "account.micr": "MICR code",
    "account.bank_name": "Bank name",
    "account.branch": "Branch name / address",
    "account.type": "Account type (savings / current / …)",
    "account.customer_id": "Bank customer / CIF id",
    "account.cheque_number": "Cheque serial number",
    "income.employer": "Employer name",
    "income.amount": "Declared / observed income",
    "income.gross_salary": "Gross salary for the period",
    "income.net_pay": "Net pay for the period",
    "income.total_income": "Total income declared for the year",
    "income.tax_deducted": "Tax deducted at source",
    "income.pension_amount": "Basic pension sanctioned",
    # -- corporate / ownership --------------------------------------------
    "entity.legal_name": "Company / firm legal name",
    "entity.trade_name": "Trade name, where it differs from the legal name",
    "entity.incorporation_date": "Incorporation / constitution / registration date",
    "entity.constitution": "Constitution of business (private limited, LLP, partnership, …)",
    "entity.registered_office": "Registered office address",
    "entity.authorised_capital": "Authorised share capital",
    "entity.paid_up_capital": "Subscribed / paid-up share capital",
    "entity.objects": "Objects clause of the memorandum",
    "ownership.director": "Director / designated partner",
    "ownership.partner": "Partner in a firm",
    "ownership.beneficial_owner": "Beneficial owner (>=25% / control)",
    "ownership.authorized_signer": "Authorised signatory",
    "ownership.share": "Profit-sharing / shareholding proportion",
    # -- utility / tenancy --------------------------------------------------
    "utility.consumer_number": "Consumer / connection / service number",
    "utility.service_provider": "DISCOM / board / operator name",
    "utility.units_consumed": "Units consumed in the billing period",
    "utility.bill_amount": "Amount billed",
    "utility.bill_period": "Billing period covered",
    "tenancy.landlord_name": "Lessor / licensor name",
    "tenancy.tenant_name": "Lessee / licensee name",
    "tenancy.monthly_rent": "Monthly rent / licence fee",
    "tenancy.term": "Term of the tenancy",
    # -- document meta ------------------------------------------------------
    "doc.issue_date": "Document issue date",
    "doc.expiry_date": "Document expiry / validity date",
    "doc.issuing_authority": "Named issuing authority / office",
    "doc.reference_number": "Certificate / acknowledgement / serial number",
    "doc.assessment_year": "Assessment year (Indian tax documents)",
    "doc.due_date": "Payment due date",
    "doc.place_of_issue": "Place of issue",
    "doc.registration_number": "Registration number assigned by the issuing registry",
}

#: Locator names the extraction tier implements (``dce.extract``). A FieldSpec may only
#: ask for these; anything else is a typo that would silently never run.
KNOWN_LOCATORS: frozenset[str] = frozenset({"kv", "label", "table", "mark", "regex", "mrz"})

#: ``FieldSpec.type`` values the normalisers understand.
KNOWN_FIELD_TYPES: frozenset[str] = frozenset(
    {"string", "date", "number", "name", "address", "id", "bool"}
)

#: Module that must expose every named validator the packs reference.
VALIDATOR_MODULE = "dce.extract.validate"

#: The registry's declared validator surface: ``name -> what the validator must enforce``.
#:
#: Names here are the names :mod:`dce.extract.validate` actually exposes. The registry does
#: not get to invent its own vocabulary: a FieldSpec naming a validator that does not exist
#: is a field that silently never validates, which in a KYC gate means unverified data
#: presented as verified. :func:`_check_field` rejects any name not listed here at *register*
#: time (local, loud, independent of import order) and :func:`_check_validators` then confirms
#: each one resolves for real.
#:
#: Where a genuine check digit exists it is named. Where one does **not** exist, or is not
#: published, the entry says so — a validator that pretends to checksum an unchecksummed
#: identifier silently rejects genuine documents, which is the worst failure mode this
#: service has.
VALIDATOR_CONTRACT: dict[str, str] = {
    "verhoeff_aadhaar": (
        "12 digits, first digit 2-9, Verhoeff check digit over all 12. Accept the "
        "'NNNN NNNN NNNN' printed grouping; compare on the compact form."
    ),
    "pan": (
        "10 chars, [A-Z]{5}[0-9]{4}[A-Z]. 4th char is the holder type (one of ABCFGHJLPT), "
        "5th is the first letter of the surname/entity name. The 10th character IS a check "
        "character but the algorithm is NOT published by the Income Tax Department — "
        "validate structure only, never a computed check digit."
    ),
    "gstin": (
        "15 chars: 2-digit state code + 10-char PAN + 1 entity code + 'Z' + 1 check "
        "character. The check character IS a published mod-36 weighted algorithm — compute "
        "and enforce it."
    ),
    "epic_voter": (
        "Current EPIC numbers are 3 letters (state functional code) + 7 digits. Older "
        "state-issued series must NOT be rejected outright — accept the modern form, note "
        "the rest."
    ),
    "in_dl": (
        "Indian driving licence shape: 2-letter state code + 2-digit RTO code, then a "
        "state-specific serial. No national standard and no checksum — heuristic only."
    ),
    "in_passport": (
        "Indian passport number shape: 1 letter + 7 digits. No check digit on the printed "
        "number; the only verifiable digits live in the MRZ — see mrz_td3."
    ),
    "mrz_td3": (
        "ICAO 9303 TD3: two 44-character lines, per-field and composite check digits over "
        "the 7-3-1 weighting. Enforce every check digit."
    ),
    "generic_date": (
        "Indian documents print day-first, so day/month ambiguity must resolve DMY and the "
        "assumption must be reported, not hidden. Normalise to ISO."
    ),
    "amount": (
        "Currency amount including the Indian lakh grouping (12,34,567.89). Normalise to a "
        "plain decimal."
    ),
    "name": "A person or entity name — must reject a date or a bare digit run binding to it.",
    "address": (
        "A postal address — must reject a date binding to it because it sat right of the label."
    ),
}

#: Validators the registry NEEDS but :mod:`dce.extract.validate` does not implement yet.
#:
#: These are not referenced by any FieldSpec — doing so would (correctly) fail
#: :func:`_check_field`. The affected fields fall back to their ``pattern``, which covers
#: the structural case but not the normalisation. This mapping is the checklist for whoever
#: extends the validator module; once a name lands there, wire it into the FieldSpecs that
#: need it and move the entry up into :data:`VALIDATOR_CONTRACT`.
#:
#: ``tests/test_registry_india.py::test_pending_validators_are_still_pending`` fails the
#: moment one of these is implemented, so the list cannot quietly go stale.
PENDING_VALIDATORS: dict[str, str] = {
    "aadhaar_last4": "Exactly 4 digits — the only portion of an Aadhaar number normally storable.",
    "assessment_year": "Indian AY printed as YYYY-YY (e.g. 2024-25); normalise to the full span.",
    "cheque_number": "6 digits under the CTS-2010 standard. Structure only.",
    "cin": (
        "21 chars: listing status [LU] + 5-digit industry code + 2-letter state + 4-digit "
        "year + 3-letter ownership code + 6-digit registration number. No check digit."
    ),
    "ckyc_kin": "CKYC Identification Number: 14 digits. Structure only; no published check digit.",
    "ifsc": (
        "11 chars: 4-letter bank code + '0' + 6 alphanumeric branch code. Structure only; "
        "the branch code is a lookup, not a checksum."
    ),
    "indian_bank_account": (
        "9-18 digits after stripping separators. NO checksum exists across Indian banks — "
        "each bank has its own internal scheme. Length and charset only; do not invent one."
    ),
    "indian_mobile": "10 digits beginning 6-9, optionally prefixed +91 / 0.",
    "indian_pincode": "6 digits, first digit 1-9.",
    "itr_acknowledgement_number": "ITR-V acknowledgement number: 15 digits. Structure only.",
    "llpin": (
        "LLP identification number: 3 letters + '-' + 4 digits (e.g. AAA-1234). Structure only."
    ),
    "micr": "9 digits: 3-digit city + 3-digit bank + 3-digit branch. Structure only.",
    "tan": "10 chars, [A-Z]{4}[0-9]{5}[A-Z]. Structure only; no published check digit.",
    "uan_epfo": "EPFO Universal Account Number: 12 digits. Structure only; no public check digit.",
    "year_yyyy": "A 4-digit year in a plausible range (Aadhaar prints 'Year of Birth' alone).",
}


# ---------------------------------------------------------------------------
# Registration table
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, DocTypeSpec] = {}

_DOCTYPE_ID_RE = re.compile(r"^[a-z]{2}_[a-z0-9]+(?:_[a-z0-9]+)*$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
#: Script-agnostic word splitter — see the module docstring for why ``tokenize`` is unusable
#: for this. Splits on whitespace and the punctuation that separates words on a form.
_WORD_SPLIT_RE = re.compile(r"[\s/\\,;:()\[\]{}<>\"'`|.\-–—_+*&!?]+")  # noqa: RUF001


def _words(text: str) -> list[str]:
    """Split ``text`` into human-readable words, script-agnostically.

    Args:
        text: An anchor or label string, in any script.

    Returns:
        Non-empty word chunks with surrounding punctuation removed.
    """
    return [w for w in _WORD_SPLIT_RE.split(text.strip()) if w]


def _anchor_key(anchor: Anchor) -> tuple[str, str | None]:
    """Identity of an anchor for collision purposes: normalised text plus its zone.

    Case-folded and NFC-normalised so ``"AADHAAR"`` and ``"Aadhaar"`` collide, and so two
    spellings of the same Devanagari string that differ only in composition do too.
    """
    text = unicodedata.normalize("NFC", anchor.text).strip().casefold()
    return (text, anchor.zone.value if anchor.zone is not None else None)


# ---------------------------------------------------------------------------
# Per-spec validation — runs at register() time, i.e. at pack import
# ---------------------------------------------------------------------------
def _fail(doctype_id: str, problem: str) -> None:
    raise RegistryError(f"doctype {doctype_id!r}: {problem}")


def _check_anchor(doctype_id: str, anchor: Anchor, country: str) -> None:
    """Validate a single anchor. See :func:`_validate_spec` for the caller."""
    text = anchor.text.strip()
    if not text:
        _fail(doctype_id, "has an empty anchor")
    if not any(ch.isalnum() for ch in text):
        _fail(
            doctype_id, f"anchor {text!r} contains no alphanumeric characters and can never match"
        )
    if not _words(text):
        _fail(doctype_id, f"anchor {text!r} splits into no words")
    if anchor.text != unicodedata.normalize("NFC", anchor.text):
        _fail(
            doctype_id,
            f"anchor {text!r} is not NFC-normalised; store anchors in NFC so a "
            "decomposed OCR read and the pack agree",
        )
    if not anchor.decisive:
        return
    # A decisive anchor carries fuse_weight_anchor (3.0) on its own. Short, single-word
    # decisive anchors are how a registry starts producing confident nonsense — "DL",
    # "PAN", "GST". Allow a short one only when it is pinned to a zone, because a bare
    # word in the *title* really is an issuing-authority header (e.g. "आधार").
    if len(_words(text)) < 2 and len(text) < 8 and anchor.zone is None:
        _fail(
            doctype_id,
            f"decisive anchor {text!r} is a single short word with no zone constraint; "
            "either lengthen it, pin it to a zone, or drop the decisive flag",
        )
    if country == "XX":
        _fail(
            doctype_id,
            f"cross-country doctypes must not carry decisive anchors (found {text!r}); "
            "a country-specific doctype has to be able to outrank them",
        )


def _check_field(doctype_id: str, field: FieldSpec) -> None:
    """Validate a single FieldSpec. See :func:`_validate_spec` for the caller."""
    if not _FIELD_NAME_RE.match(field.name):
        _fail(doctype_id, f"field name {field.name!r} is not snake_case")
    if field.type not in KNOWN_FIELD_TYPES:
        _fail(
            doctype_id,
            f"field {field.name!r} has type {field.type!r}; known types are "
            f"{sorted(KNOWN_FIELD_TYPES)}",
        )
    if field.attribute_key:
        if not _ATTRIBUTE_KEY_RE.match(field.attribute_key):
            _fail(
                doctype_id,
                f"field {field.name!r} attribute_key {field.attribute_key!r} is not a "
                "dotted lowercase namespace",
            )
        if field.attribute_key not in ATTRIBUTE_KEYS:
            _fail(
                doctype_id,
                f"field {field.name!r} attribute_key {field.attribute_key!r} is not in "
                "ATTRIBUTE_KEYS; add it there first so the merge view knows the key",
            )
    if not field.locators:
        _fail(doctype_id, f"field {field.name!r} declares no locators and can never be filled")
    unknown = [loc for loc in field.locators if loc not in KNOWN_LOCATORS]
    if unknown:
        _fail(
            doctype_id,
            f"field {field.name!r} asks for locators {unknown} which are not implemented; "
            f"known locators are {sorted(KNOWN_LOCATORS)}",
        )
    if len(set(field.locators)) != len(field.locators):
        _fail(doctype_id, f"field {field.name!r} repeats a locator: {field.locators}")
    if field.pattern is not None:
        try:
            re.compile(field.pattern)
        except re.error as exc:
            _fail(doctype_id, f"field {field.name!r} pattern does not compile: {exc}")
    if field.validator and field.validator not in VALIDATOR_CONTRACT:
        _fail(
            doctype_id,
            f"field {field.name!r} names validator {field.validator!r}, which is not in "
            "VALIDATOR_CONTRACT; declare what the validator must enforce there first",
        )
    for lang, labels in field.labels.items():
        if not re.fullmatch(r"[a-z]{2}", lang):
            _fail(doctype_id, f"field {field.name!r} has label language {lang!r}, want ISO-639-1")
        for label in labels:
            if not label.strip():
                _fail(doctype_id, f"field {field.name!r} has an empty {lang} label")


def _validate_spec(spec: DocTypeSpec) -> None:
    """Run every check that can be made on one spec in isolation.

    Args:
        spec: The doctype being registered.

    Raises:
        RegistryError: On the first problem found, naming the doctype and the problem.
    """
    did = spec.doctype_id
    if not _DOCTYPE_ID_RE.match(did):
        _fail(did, "doctype_id must be lowercase snake_case prefixed by the country, e.g. 'in_pan'")
    if not _COUNTRY_RE.match(spec.country):
        _fail(did, f"country {spec.country!r} must be a 2-letter uppercase code (IN, US, XX, …)")
    if not did.startswith(f"{spec.country.lower()}_"):
        _fail(did, f"doctype_id prefix does not match country {spec.country!r}")
    if not spec.label.strip():
        _fail(did, "label is empty")
    if spec.applies_to not in {"individual", "corporate", "both"}:
        _fail(did, f"applies_to {spec.applies_to!r} must be individual | corporate | both")
    if not spec.anchors:
        _fail(did, "has no anchors; it could never be classified")

    seen_anchors: set[tuple[str, str | None, bool]] = set()
    for anchor in spec.anchors:
        _check_anchor(did, anchor, spec.country)
        key = (*_anchor_key(anchor), anchor.decisive)
        if key in seen_anchors:
            _fail(did, f"repeats anchor {anchor.text!r} (zone={anchor.zone}) within its own pack")
        seen_anchors.add(key)

    for pattern in spec.id_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            _fail(did, f"id_pattern {pattern!r} does not compile: {exc}")

    for negative in spec.negative_anchors:
        if not negative.strip():
            _fail(did, "has an empty negative_anchor")
        if not _words(negative):
            _fail(did, f"negative_anchor {negative!r} splits into no words")

    names: set[str] = set()
    for field in spec.fields:
        if field.name in names:
            _fail(did, f"declares field {field.name!r} twice")
        names.add(field.name)
        _check_field(did, field)

    if spec.doctype_id in spec.confusable_with:
        _fail(did, "lists itself in confusable_with")
    for other, discriminator in spec.confusable_with.items():
        if not discriminator.strip():
            _fail(
                did,
                f"confusable_with[{other!r}] has no discriminating term; the whole point "
                "of the mapping is to name what separates them",
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def register(spec: DocTypeSpec) -> DocTypeSpec:
    """Validate ``spec`` and add it to the registry.

    Args:
        spec: The doctype to register.

    Returns:
        The same spec, so packs can write ``SPEC = register(DocTypeSpec(...))``.

    Raises:
        RegistryError: If the spec is malformed, or its id is already registered.
    """
    _validate_spec(spec)
    existing = _REGISTRY.get(spec.doctype_id)
    if existing is not None and existing is not spec:
        raise RegistryError(
            f"duplicate doctype_id {spec.doctype_id!r}: already registered as "
            f"{existing.label!r}, cannot re-register as {spec.label!r}"
        )
    _REGISTRY[spec.doctype_id] = spec
    return spec


def register_all(specs: list[DocTypeSpec]) -> list[DocTypeSpec]:
    """Register a whole pack in order. Raises on the first malformed spec."""
    return [register(spec) for spec in specs]


def get(doctype_id: str) -> DocTypeSpec | None:
    """Look up a doctype, or ``None`` if it is not registered."""
    return _REGISTRY.get(doctype_id)


def require(doctype_id: str) -> DocTypeSpec:
    """Look up a doctype, raising if it is absent.

    Raises:
        KeyError: If ``doctype_id`` is not registered.
    """
    spec = _REGISTRY.get(doctype_id)
    if spec is None:
        raise KeyError(f"unknown doctype_id {doctype_id!r}")
    return spec


def all_specs() -> list[DocTypeSpec]:
    """Every registered doctype, sorted by id for deterministic iteration."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def by_country() -> dict[str, list[DocTypeSpec]]:
    """Registered doctypes grouped by country code, each group sorted by id."""
    grouped: dict[str, list[DocTypeSpec]] = {}
    for spec in all_specs():
        grouped.setdefault(spec.country, []).append(spec)
    return grouped


def required_validators() -> frozenset[str]:
    """Every validator name referenced by a registered FieldSpec.

    This is the exact surface :mod:`dce.extract.validate` has to implement.
    """
    return frozenset(
        field.validator for spec in _REGISTRY.values() for field in spec.fields if field.validator
    )


def clear() -> None:
    """Empty the registry. Test helper — production code never calls this."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Cross-spec validation
# ---------------------------------------------------------------------------
def _check_decisive_collisions(errors: list[str]) -> None:
    """Decisive anchors must leave every colliding doctype distinguishable.

    Sharing a decisive anchor is allowed when the doctypes are genuinely the same document
    family (a masked and an unmasked Aadhaar share the UIDAI header), but only if they
    *say so*: each must list the others in ``confusable_with`` with the term that separates
    them, and each must retain at least one decisive anchor the others do not have.
    """
    owners: dict[tuple[str, str | None], list[str]] = {}
    decisive_by_doc: dict[str, set[tuple[str, str | None]]] = {}
    for spec in all_specs():
        keys = {_anchor_key(a) for a in spec.anchors if a.decisive}
        decisive_by_doc[spec.doctype_id] = keys
        for key in keys:
            owners.setdefault(key, []).append(spec.doctype_id)

    for key, doc_ids in sorted(owners.items()):
        if len(doc_ids) < 2:
            continue
        text = key[0]
        for did in doc_ids:
            spec = _REGISTRY[did]
            undeclared = [o for o in doc_ids if o != did and o not in spec.confusable_with]
            if undeclared:
                errors.append(
                    f"decisive anchor {text!r} is claimed by {sorted(doc_ids)}, but "
                    f"{did!r} does not declare {sorted(undeclared)} in confusable_with — "
                    "an undeclared decisive collision makes them indistinguishable at L1"
                )
            others = set().union(*(decisive_by_doc[o] for o in doc_ids if o != did))
            if not (decisive_by_doc[did] - others):
                errors.append(
                    f"{did!r} shares every one of its decisive anchors with "
                    f"{sorted(o for o in doc_ids if o != did)}; it needs at least one "
                    "decisive anchor of its own or it can never win L1"
                )


def _check_confusable_targets(errors: list[str]) -> None:
    """``confusable_with`` keys must name real, registered doctypes."""
    for spec in all_specs():
        for other in spec.confusable_with:
            if other not in _REGISTRY:
                errors.append(
                    f"{spec.doctype_id!r} lists confusable_with[{other!r}] but no such "
                    "doctype is registered"
                )


def _validator_probe() -> Callable[[str], bool] | None:
    """Return a ``name -> bool`` probe against :mod:`dce.extract.validate`, or ``None``.

    ``None`` means the module is not importable yet. That is a *build-order* condition, not
    a malformed pack, so :func:`validate_registry` does not fail on it — the packs are
    already checked against :data:`VALIDATOR_CONTRACT`, which catches typos locally. As
    soon as the module exists, every name is checked for real.

    The probe accepts any of the plausible module shapes (a ``VALIDATORS`` mapping, a
    ``get_validator``/``has_validator`` accessor, or plain module-level functions) so the
    registry does not dictate the validator module's internal design.
    """
    try:
        module = import_module(VALIDATOR_MODULE)
    except ImportError:
        return None

    registry_map = getattr(module, "VALIDATORS", None)
    has_validator = getattr(module, "has_validator", None)
    get_validator = getattr(module, "get_validator", None)

    def probe(name: str) -> bool:
        if isinstance(registry_map, dict) and name in registry_map:
            return True
        if callable(has_validator):
            try:
                if has_validator(name):
                    return True
            except Exception:  # noqa: BLE001 - a probe must never mask the real error
                pass
        if callable(get_validator):
            try:
                if get_validator(name) is not None:
                    return True
            except Exception:  # noqa: BLE001 - get_validator may raise on unknown names
                pass
        return any(
            callable(getattr(module, candidate, None))
            for candidate in (name, f"validate_{name}", f"is_valid_{name}")
        )

    return probe


def _check_validators(errors: list[str]) -> None:
    """Every referenced validator must resolve, once the validator module exists."""
    probe = _validator_probe()
    if probe is None:
        return
    for name in sorted(required_validators()):
        if not probe(name):
            errors.append(
                f"validator {name!r} is referenced by a FieldSpec but does not resolve in "
                f"{VALIDATOR_MODULE}; it must enforce: {VALIDATOR_CONTRACT[name]}"
            )


def validate_registry() -> None:
    """Run every cross-spec check over the whole registry.

    Called at the end of :mod:`dce.registry`'s import, so a bad pack combination stops the
    process rather than degrading classification quietly.

    Raises:
        RegistryError: With every problem found, not just the first — a pack author fixing
            the registry wants the full list.
    """
    errors: list[str] = []
    if not _REGISTRY:
        raise RegistryError("registry is empty; no doctype packs were imported")
    _check_decisive_collisions(errors)
    _check_confusable_targets(errors)
    _check_validators(errors)
    if errors:
        joined = "\n  - ".join(errors)
        raise RegistryError(f"registry validation failed ({len(errors)} problem(s)):\n  - {joined}")
