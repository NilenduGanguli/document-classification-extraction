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

**Every check in this module is blind to a lone false claim, and always will be.** They all
compare the registry against itself, so they can only fire when a *second* doctype declares
the same string — which means one pack declaring ``BIRTH CERTIFICATE`` decisive is invisible
here by construction, however many foreign birth certificates it goes on to match. Four such
claims survived review that way. Two things close the gap, and neither of them can live in
this module:

* :class:`dce.models.Controls` makes the claim **typed**: a decisive anchor has to name its
  grounds, so the author of a document-class name has to write
  ``controls=CLASS_NAME_UNCONTESTED`` and say out loud that the claim is weak. That much *is*
  enforced here (:func:`_check_anchor`), and it buys the stricter uniqueness rule in
  :func:`_check_class_name_uncontested`.
* ``tests/test_registry_corpus_decisive.py`` enforces the real property —
  *a decisive anchor must not match a document of a different doctype* — against the corpus,
  because the evidence that contradicts a lone false claim is documents, and documents are
  outside the registry. It stays a **test**: the service must not depend on ``corpus/``, and
  this loader must stay pure. Note the direction of travel — the checks here get *weaker* as
  the registry grows relative to the evidence; that test gets *stronger* as the corpus grows.

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
from collections.abc import Callable, Iterable, Mapping
from importlib import import_module

from dce.models import Anchor, Controls, DocTypeSpec, FieldSpec
from dce.normalize import fold, skeletonize, tokenize_unicode

__all__ = [
    "ATTRIBUTE_KEYS",
    "KNOWN_FIELD_TYPES",
    "KNOWN_LOCATORS",
    "PENDING_VALIDATORS",
    "VALIDATOR_CONTRACT",
    "RegistryError",
    "all_specs",
    "anchor_claim_key",
    "anchor_claims",
    "by_country",
    "clear",
    "contested_claims",
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


def anchor_claim_key(text: str) -> tuple[str, ...]:
    """Identity of an anchor *string* as L1 will actually match it: skeleton tokens.

    Three differences from :func:`_anchor_key`, each of which is a hole this function closes.

    **Zone is dropped.** ``us_state_id`` gates ``IDENTIFICATION CARD`` to ``zone=title`` and
    ``ca_provincial_photo_id`` leaves ``Identification Card`` ungated; the ungated anchor
    matches in the title too, so the two claims meet on the page even though their
    ``_anchor_key`` differs. Zone controls whether a claim is *audible*, never whether it is
    the same claim — and the audibility question is the cascade's, which already has
    :attr:`~dce.classify.anchors.AnchorOutcome.muted_decisive` for it.

    **Case folding is replaced by the matcher's own normalisation.** L1 matches on
    :func:`dce.normalize.skeletonize` — accents stripped, OCR confusions folded — so two
    strings differing only by an accent or by ``O``/``0`` are one claim at runtime and must be
    one claim here. Case folding alone would file ``RÉSIDENT`` and ``RESIDENT`` as unrelated.

    **The result is a token tuple, not a string.** L1 matches contiguous token n-grams, so
    punctuation and whitespace are not part of the claim.

    Args:
        text: The anchor as declared in a pack.

    Returns:
        The token tuple L1 would search for. Two anchors sharing this key are, for every
        purpose this registry has, the same string.
    """
    return tuple(tokenize_unicode(skeletonize(fold(text))))


def anchor_claims(specs: Iterable[DocTypeSpec]) -> dict[tuple[str, ...], frozenset[str]]:
    """Every anchor string in ``specs``, mapped to the doctypes that print it.

    Decisiveness is deliberately ignored. The question this answers is "who *claims* this
    string appears on their document", and a pack that declared the anchor non-decisive has
    made that claim just as loudly. Reading only the decisive declarations is exactly how the
    ``PERMANENT RESIDENT CARD`` asymmetry survived review — see
    :func:`_check_decisive_asymmetry` and ``tests/test_registry_jurisdiction.py``.

    Args:
        specs: The registry, or any subset.

    Returns:
        ``anchor_claim_key -> frozenset of doctype ids``.
    """
    claims: dict[tuple[str, ...], set[str]] = {}
    for spec in specs:
        for anchor in spec.anchors:
            key = anchor_claim_key(anchor.text)
            if key:
                claims.setdefault(key, set()).add(spec.doctype_id)
    return {key: frozenset(owners) for key, owners in claims.items()}


def contested_claims(specs: Iterable[DocTypeSpec]) -> dict[tuple[str, ...], frozenset[str]]:
    """The subset of :func:`anchor_claims` that more than one doctype claims.

    The registry's own admission of where an anchor is *not* unique to a document type. A
    decisive anchor in this set contradicts the definition of decisive
    (:class:`dce.models.Anchor`: "near-proof of the doctype"), and the cascade must refuse to
    short-circuit on it — being the only doctype *heard* saying a string that two doctypes
    *print* is a fact about the payload's zones, not about the document.
    """
    return {key: owners for key, owners in anchor_claims(specs).items() if len(owners) > 1}


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
        # ``controls`` is the justification for a decisive claim. On an anchor that makes no
        # such claim it is decoration, and decoration is exactly what stops
        # ``grep -c class_name_uncontested`` from being an honest count of the weak claims.
        if anchor.controls is not None:
            _fail(
                doctype_id,
                f"anchor {text!r} is not decisive but declares controls="
                f"{anchor.controls.value!r}; controls justifies a decisive claim and means "
                "nothing without one — drop it, or mark the anchor decisive",
            )
        return
    # A decisive anchor asserts near-proof of the doctype. Until this check existed, making
    # that assertion cost one keystroke and required no justification, so declaring
    # 'OMB No. 1545-0074' decisive and declaring 'BIRTH CERTIFICATE' decisive were
    # indistinguishable acts — and the two cross-spec checks below cannot tell them apart
    # either, because both compare the registry against itself and a *lone* false claim has
    # no second claimant to collide with. Requiring the author to name the grounds is what
    # converts that invisible violation into a question asked at authoring time: whoever
    # wrote 'BIRTH CERTIFICATE' would have had nothing honest to put here.
    if anchor.controls is None:
        _fail(
            doctype_id,
            f"decisive anchor {text!r} does not say what makes it decisive; set controls= "
            f"to one of {[c.value for c in Controls]} (see dce.models.Controls). A decisive "
            "anchor asserts the string appears on this document type and no other, and that "
            "assertion has to have grounds. If the honest answer is 'it is the name of the "
            "document class and nothing collides with it yet', say exactly that with "
            f"controls={Controls.CLASS_NAME_UNCONTESTED.value!r} — it is a real value, it is "
            "counted and reported as a weak claim, and the loader holds it to a stricter "
            "uniqueness rule than the others",
        )
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


def _check_decisive_asymmetry(errors: list[str]) -> None:
    """A decisive anchor may not be a string another doctype also prints, undeclared.

    :func:`_check_decisive_collisions` compares decisive anchors against **other decisive
    anchors** only, and that is the hole this closes. The measured escape:

        ``PERMANENT RESIDENT CARD`` is DECISIVE for ``us_green_card`` and NON-decisive for
        ``ca_pr_card``. Two doctypes print the string; only one calls it decisive; the
        collision check never looked at the non-decisive side, so the registry passed
        validation. On a bilingual card whose French line the OCR dropped, exactly one
        doctype held a decisive anchor and L1 short-circuited to the US doctype at 0.90.

    Decisiveness is a claim about the *world* — :class:`dce.models.Anchor` defines a decisive
    anchor as near-proof of the doctype — so it is contradicted by any other doctype declaring
    the same string, whatever *that* doctype chose to call it. Asymmetry is in fact the more
    dangerous shape of the two the collision check already catches: two decisive claims cancel
    (``len(decisive_doctypes()) != 1`` and the short-circuit declines), whereas a
    decisive/non-decisive pair leaves exactly one claimant standing and silently picks it.

    **The remedy depends on whether the overlap crosses a jurisdiction, and the two cases are
    not interchangeable.** This was measured rather than reasoned, and the measurement is the
    reason the rule is shaped the way it is.

    *Same country — declare it.* A masked and an unmasked Aadhaar genuinely share the UIDAI
    header; ``in_certificate_incorporation`` and ``in_llp_incorporation`` genuinely share
    ``MINISTRY OF CORPORATE AFFAIRS``. Declaring the overlap in ``confusable_with``, both ways,
    naming the separating term, is the honest description of a one-issuer document family. At
    classification time :func:`contested_claims` then stops the shared string from carrying the
    conclusive-L1 identification route, and the two-channel rule arbitrates on the full
    evidence — which is the correct outcome for a family, since the two really are ambiguous
    until something else separates them.

    *Different country — demote it.* Declaring is **not** sufficient here, and assuming it was
    would have been a silent partial fix. ``us_green_card`` and ``ca_pr_card`` already declared
    each other, in both directions, with the separating term spelled out — and the Canadian
    card was still classified ``us_green_card``. Measured with the declaration in place and the
    contested-claim rule active: ``corpus/ca/ca_pr_card.pdf`` still returned ``us_green_card``
    at 0.545, because suppressing the conclusive-L1 route does not suppress the *concurrence*
    route, and ``decisive=True`` is worth a 2.0 multiplier in the anchor score that concurrence
    reads. The declaration closes one door; the multiplier walks through the other. Only
    removing the false claim removes the failure.

    That asymmetry is also the right policy independent of the mechanism. A within-family
    confusion resolves to "which of this issuer's documents is this" and abstains safely; a
    cross-jurisdiction confusion resolves to a confident identity determination in the wrong
    country's legal regime, which is the worst error a KYC classifier can make.

    **How to tell which you have — and why the old answer here was wrong.** This paragraph
    used to read: a decisive anchor must be a string *one issuer controls* — a form number, an
    OMB control number, a statute title, an MRZ prefix, an issuing-authority name. That was a
    proxy for the property that actually matters, and measuring it against the corpus showed
    it is a bad proxy in **both** directions. It keeps 26 anchors that demonstrably match
    another doctype's documents (``Form W-9``, ``1099-NEC``, ``I-766``, ``FORM 51-101F1``,
    ``SOCIAL SECURITY ADMINISTRATION``, ``TELMEX`` — every one of them issuer-controlled), and
    it demotes 163 that demonstrably match nothing else (``RATION CARD``, ``PASSBOOK``,
    ``ACTA DE NACIMIENTO``). The property is:

        **A decisive anchor must not appear on a document of another type — WHICH INCLUDES
        BEING CITED BY ONE.**

    Citation is the mechanism the old rule never accounted for, and it is not incidental in
    this domain: documents here reference each other's form numbers constantly ("Give Form W-9
    to the requester", a 20-F listing the tax forms its holders file), and KYC onboarding
    paperwork *enumerates the document classes it accepts* —
    ``corpus/ca/ca_sin_confirmation.pdf`` alone breaks six anchors (``BIRTH CERTIFICATE``,
    ``CERTIFICATE OF BIRTH``, ``CERTIFICATE OF MARRIAGE``,
    ``CERTIFICATE OF CANADIAN CITIZENSHIP``, ``CONFIRMATION OF PERMANENT RESIDENCE``,
    ``DRIVER'S LICENSE``) because it lists the ID it will accept, and
    ``corpus/in/in_form60.pdf`` breaks four the same way. Issuer control is therefore not
    sufficient, and — as the 163 show — not necessary either.

    Two consequences for how a pack is written:

    * The claim is now **typed**. Every decisive anchor names its grounds in
      :class:`dce.models.Controls`, and the honest value for a document-class name is
      ``CLASS_NAME_UNCONTESTED``, which keeps the anchor, marks it weak, and subjects it to
      :func:`_check_class_name_uncontested`. The old prose gave an author with a class name
      nothing to write but ``decisive=True``.
    * The property is **enforced against documents**, not against the registry, by
      ``tests/test_registry_corpus_decisive.py``. It has to be a test and not a check here:
      the evidence a lone false claim contradicts lives outside the registry, and the service
      must not depend on the corpus. Everything in this module gets *weaker* as the registry
      grows relative to the evidence; that test gets *stronger* as the corpus grows.

    Same-doctype pairs are exempt throughout. A pack that declares a string decisive at
    ``zone=title`` and non-decisive ungated is expressing "worth more in the title" about one
    document, and a doctype cannot be confused with itself.
    """
    claims = anchor_claims(all_specs())
    for spec in all_specs():
        for anchor in spec.anchors:
            if not anchor.decisive:
                continue
            key = anchor_claim_key(anchor.text)
            others = sorted(claims.get(key, frozenset()) - {spec.doctype_id})
            if not others:
                continue

            foreign = sorted(o for o in others if _REGISTRY[o].country != spec.country)
            if foreign:
                errors.append(
                    f"{spec.doctype_id!r} ({spec.country}) declares {anchor.text!r} DECISIVE, "
                    f"but {foreign} of other jurisdictions also declare that string as an "
                    "anchor. A cross-jurisdiction decisive claim must be DEMOTED, not "
                    "declared: confusable_with does not make it safe. us_green_card and "
                    "ca_pr_card declared each other in both directions and a Canadian PR card "
                    "was still classified us_green_card, because suppressing the conclusive-L1 "
                    "route leaves the concurrence route, which reads the anchor score that "
                    "decisive=True multiplies by 2.0. If no string on this document is printed "
                    "by only this issuer, this doctype has no decisive anchor — say so"
                )
                continue

            undeclared = sorted(
                other
                for other in others
                if other not in spec.confusable_with
                or spec.doctype_id not in (_REGISTRY[other].confusable_with or {})
            )
            if undeclared:
                errors.append(
                    f"{spec.doctype_id!r} declares {anchor.text!r} DECISIVE, but "
                    f"{undeclared} also declare(s) that string as an anchor without a "
                    "two-way confusable_with declaration. A decisive anchor asserts the "
                    "string appears on one document type and nowhere else, and the registry "
                    "here says otherwise — demote it if the string is a document-class or "
                    "shared-issuer name, or, since these are doctypes of one jurisdiction, "
                    "declare the overlap in both directions if they are one document family"
                )


def _check_class_name_uncontested(errors: list[str]) -> None:
    """A ``CLASS_NAME_UNCONTESTED`` decisive anchor must have no other claimant at all.

    This is the tripwire the whole tier exists for, and it is deliberately stricter than
    both checks above it.

    :func:`_check_decisive_collisions` permits a shared decisive anchor when the doctypes
    declare each other as one document family. :func:`_check_decisive_asymmetry` permits a
    shared string outright when the sharing doctypes are of one jurisdiction and declare the
    overlap. Neither escape hatch is available here, because the value's own meaning removes
    it: ``CLASS_NAME_UNCONTESTED`` says *"this is a document-class name, and it is decisive
    only because nothing currently collides with it"*. A second claimant is precisely the
    condition the claim was conditioned on, and a class name shared by two doctypes is not a
    family — it is two issuers who independently chose the same words, which is what a class
    name is.

    Today the rule costs nothing: every one of these anchors is claimed by exactly one
    doctype, which is why they were kept decisive rather than demoted. That is the point. The
    registry grows, and the day a pack author adds a doctype that prints ``RATION CARD`` or
    ``MORTGAGE STATEMENT``, the import fails and names both sides — instead of the older
    behaviour, where the new doctype would simply have become unclassifiable and nothing
    would have said so.
    """
    claims = anchor_claims(all_specs())
    for spec in all_specs():
        for anchor in spec.anchors:
            if not anchor.decisive or anchor.controls is not Controls.CLASS_NAME_UNCONTESTED:
                continue
            others = sorted(claims.get(anchor_claim_key(anchor.text), frozenset()) - {
                spec.doctype_id
            })
            if others:
                errors.append(
                    f"{spec.doctype_id!r} declares {anchor.text!r} DECISIVE with "
                    f"controls={Controls.CLASS_NAME_UNCONTESTED.value!r}, but {others} also "
                    "declare that string. That value means 'a document-class name, decisive "
                    "only because nothing collides with it' — something now does, so the "
                    "condition it was kept under has expired. Demote it (both sides print "
                    "it, so it identifies neither), or, if this doctype really does own a "
                    "string one issuer controls, anchor on that string instead and give it "
                    "the controls= value that describes it"
                )


def _check_issuer_name_not_shared(errors: list[str]) -> None:
    """An ``ISSUER_NAME`` may head at most one doctype's decisive claim.

    :func:`_check_decisive_asymmetry` states the rule in prose — "an *issuer* name that heads
    several doctypes in this registry (``INCOME TAX DEPARTMENT``) proves the issuer, not the
    document" — and then does not enforce it, because the same-jurisdiction branch accepts a
    two-way ``confusable_with`` declaration as the remedy for any shared string.

    Measured, that acceptance is wrong for this species specifically.
    ``in_certificate_incorporation`` and ``in_llp_incorporation`` both declared
    ``MINISTRY OF CORPORATE AFFAIRS`` decisive, declared each other, passed validation — and
    the string is printed by ``in_brsr`` and ``in_statutory_auditor_report`` documents in the
    corpus as well. Declaring a family is the right remedy when the shared string genuinely
    identifies the family and something *else* separates the members. An issuer name does not
    identify the family; it identifies the issuer, who also issues everything else it issues.

    Scope is deliberately narrow, and the narrowness is the measured part. The rule asks who
    declares the string **decisive**, not who declares it. ``in_aadhaar`` anchors decisively
    on the UIDAI header and ``in_aadhaar_masked`` lists the same header as ordinary lexical
    evidence: one doctype is staking the identification on it, which is the shape this rule
    permits and :func:`_check_decisive_asymmetry` already governs.
    """
    owners: dict[tuple[str, ...], list[str]] = {}
    for spec in all_specs():
        for anchor in spec.anchors:
            if anchor.decisive and anchor.controls is Controls.ISSUER_NAME:
                owners.setdefault(anchor_claim_key(anchor.text), []).append(spec.doctype_id)
    for key, doc_ids in sorted(owners.items()):
        unique = sorted(set(doc_ids))
        if len(unique) > 1:
            errors.append(
                f"{unique} all declare the issuer name {' '.join(key)!r} DECISIVE. An issuer "
                "name that heads several doctypes proves the issuer, not the document, and a "
                "two-way confusable_with declaration does not change that — the string is "
                "printed on every document this body issues, including the ones this "
                "registry has not modelled yet. Demote it in all of them and let each "
                "doctype's own form number, statute or template wording carry the claim"
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


#: The one class of anchor a cross-country generic is allowed to share with a country pack:
#: the noun that names the document class it is the fallback for. ``xx_passport_generic``
#: has to be able to say "passport" or it cannot do its job, and every passport pack says it
#: too. The exception is deliberately narrow — it covers *naming* nouns only, never field
#: labels, never structure — and it is written out per doctype rather than inferred, so that
#: adding one is a visible decision in review rather than a rule that quietly stopped biting.
#: Everything else is governed by :func:`_check_generic_not_greedy`.
_GENERIC_NAMING_ANCHORS: Mapping[str, frozenset[str]] = {
    "xx_passport_generic": frozenset({"passport", "passeport", "pasaporte"}),
    "xx_utility_bill": frozenset({"utility bill"}),
}


def _check_generic_not_greedy(errors: list[str]) -> None:
    """A cross-country generic must not declare vocabulary a country pack also declares.

    ``crosscountry`` doctypes exist to catch a document whose issuer is not modelled, and
    the module promises that "a country-specific doctype always outranks the generic one
    when both fire". Forbidding the decisive flag on ``XX`` anchors — which
    :func:`_check_anchor` already does — is not enough to keep that promise, because a spec
    can win on the *number* of anchors it matches rather than on their weight.

    That is not hypothetical: ``xx_bank_statement`` declared ``Beginning Balance``,
    ``Ending Balance``, ``Statement Period``, ``Account Summary`` and ``Routing Number`` —
    five of ``us_bank_statement``'s seven anchors — plus fifteen more. On a US bank
    statement the generic therefore matched more of its own declared vocabulary than the
    specific doctype matched of its own, and "issuer not modelled" outranked "US bank
    statement". Adding a US doctype had made the US doctype harder to reach.

    The rule is the contrapositive of why a pack declares an anchor at all: if a string were
    not evidence about the issuer, the pack would have no reason to claim it. So a claimed
    string belongs to the pack, and the generic keeps only what no jurisdiction owns.
    """
    claimed: dict[str, list[str]] = {}
    for spec in all_specs():
        if spec.country == "XX":
            continue
        for anchor in spec.anchors:
            claimed.setdefault(_anchor_key(anchor)[0], []).append(spec.doctype_id)

    for spec in all_specs():
        if spec.country != "XX":
            continue
        allowed = _GENERIC_NAMING_ANCHORS.get(spec.doctype_id, frozenset())
        for anchor in spec.anchors:
            text = _anchor_key(anchor)[0]
            if text in allowed:
                continue
            owners = claimed.get(text)
            if owners:
                errors.append(
                    f"cross-country doctype {spec.doctype_id!r} declares anchor "
                    f"{anchor.text!r}, which country pack(s) {sorted(set(owners))} also "
                    "declare; a generic that repeats a pack's vocabulary competes with the "
                    "pack on documents the pack should win, so either drop it from the "
                    "generic or stop claiming it in the pack"
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
    _check_decisive_asymmetry(errors)
    _check_class_name_uncontested(errors)
    _check_issuer_name_not_shared(errors)
    _check_confusable_targets(errors)
    _check_generic_not_greedy(errors)
    _check_validators(errors)
    if errors:
        joined = "\n  - ".join(errors)
        raise RegistryError(f"registry validation failed ({len(errors)} problem(s)):\n  - {joined}")
