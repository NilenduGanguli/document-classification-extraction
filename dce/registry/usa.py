"""United States doctype pack — 51 :class:`~dce.models.DocTypeSpec` entries.

This module is **data**. It is the knowledge the classifier and the extraction resolver run
on, so every string here is meant to be something that actually appears on the document.

Conventions honoured by this pack (and by its siblings ``canada`` / ``mexico``):

* **Decisive anchors stay distinguishing.** A decisive anchor carries ``fuse_weight_anchor``
  (3.0) on its own, so this pack never lets two doctypes claim the same one. Shared issuing
  headers — "Internal Revenue Service", "Department of Homeland Security", "Secretary of
  State" — appear on many documents and are therefore **non-decisive**; what is decisive is
  the part that only ever appears on one document: the form number, the OMB control number,
  or the form's full title.
* **Zone-restricted anchors weight a claim; they do not create one.** ``Anchor.zone`` says
  *where* a string carries its meaning, and the loader *requires* a zone for any short
  single-word decisive anchor. What a zone cannot do is make a shared string exclusive, and
  this pack used to say it could — "a generic title such as IDENTIFICATION CARD is decisive
  only when it is found in the title zone". That reasoning is retracted, because it produced
  a measured cross-jurisdiction misclassification. ``PERMANENT RESIDENT CARD`` was decisive
  for ``us_green_card`` while ``ca_pr_card`` — whose own decisive anchors are the French
  ``CARTE DE RÉSIDENT PERMANENT`` / ``RÉSIDENT PERMANENT`` — declared the identical English
  string non-decisive. On a bilingual Canadian card whose French line OCR dropped, exactly
  one doctype held a decisive anchor, L1 short-circuited, and a Canadian permanent resident
  was classified ``us_green_card`` at 0.900 with ``country="US"``. A *document-class name*
  ("Identification Card", "Permanent Resident Card", "Articles of Incorporation",
  "Account Statement", "Pasaporte") is chosen independently by every issuer in the world, and
  a zone does not fix that.

  The rule that followed — *what is decisive is a string one issuer controls: a form number,
  an OMB control number, a statute title, an MRZ prefix* — was a **proxy**, and measuring it
  against ``corpus/`` retired it. It is wrong in both directions. It keeps ``Form W-9``
  (which the corpus prints on a 1099 and on a 20-F), ``I-766`` (printed on a Canadian
  technical report) and ``SOCIAL SECURITY ADMINISTRATION`` (printed on a W-2 and a W-9) —
  all impeccably issuer-controlled. And it demotes ``VOIDED CHECK`` and
  ``MORTGAGE STATEMENT``, which nothing in the registry or the corpus contradicts. The
  property that is actually load-bearing is

      **a decisive anchor must not appear on a document of another type — WHICH INCLUDES
      BEING CITED BY ONE.**

  Citation is not incidental in this domain: filings quote each other's form numbers ("Give
  Form W-9 to the requester"), and KYC onboarding paperwork enumerates the ID classes it
  accepts, which is how one Canadian SIN letter falsified six anchors at once.

  So the claim is now **typed** rather than forbidden. Every decisive anchor names its
  grounds in :class:`dce.models.Controls`; a document-class name is permitted only under
  ``CLASS_NAME_UNCONTESTED``, which states out loud that the claim is weak and subjects it to
  a stricter uniqueness rule than the strong values; and the real property is enforced
  against documents by ``tests/test_registry_corpus_decisive.py``.
  :func:`dce.registry.loader._check_decisive_asymmetry` still enforces the registry-internal
  half at import time.
* **Validators come from a declared contract.** :data:`_VALIDATOR_EXTENSIONS` states what
  each North-American validator must enforce; ``dce.extract.validate`` implements them. A
  field never names a validator that is not declared, and where no checksum exists for an
  identifier (a US driver-licence number, a bank routing number) the field says so in
  ``notes`` instead of pretending.
* **Where a format is uncertain it goes in ``notes``, never into an invented regex.** A
  wrong regex silently rejects genuine documents, which in a KYC system means silently
  rejecting genuine people.
* ``officially_valid`` marks a credential that is acceptable *primary* photo identity
  evidence under the USA PATRIOT Act customer identification programme rule
  (31 CFR 1020.220) — the US analogue of the RBI "Officially Valid Document" list.

Attribute keys reuse the fleet ontology namespace (``identity.*``, ``id.*``, ``address.*``,
``income.*``, ``account.*``, ``entity.*``, ``ownership.*``, ``tenancy.*``, ``utility.*``,
``doc.*``) so a fact extracted here merges with the same fact extracted elsewhere.
"""

from __future__ import annotations

from importlib import import_module

from dce.models import Anchor, Category, Controls, DocTypeSpec, FieldSpec, Zone

try:  # pragma: no cover - the loader is authored alongside this pack
    from dce.registry import loader as _loader
except ImportError:  # pragma: no cover - the pack stays importable on its own
    _loader = None


# ---------------------------------------------------------------------------
# Namespace declarations
#
# The registry ships with the India pack's attribute-key catalog and validator contract,
# and rejects anything it has not been told about — deliberately, so that a typo in a pack
# fails at import instead of silently producing a field nothing can merge. Its own error
# message says to declare the key first. A pack is the right place for that declaration:
# what "id.ssn" means, and what the ``ssn`` validator has to enforce, is knowledge about
# US documents and belongs next to the US doctypes. Declaring from here also means the
# loader module itself is never edited by a pack author.
# ---------------------------------------------------------------------------
ATTRIBUTE_KEY_EXTENSIONS: dict[str, str] = {
    "id.ssn": "US Social Security Number",
    "id.itin": "US Individual Taxpayer Identification Number",
    "id.spouse_ssn": "Spouse's SSN as declared on a joint return",
    "id.ein": "US Employer Identification Number",
    "id.foreign_tin": "Foreign (non-US) taxpayer identification number declared on a W-8",
    "id.giin": "FATCA Global Intermediary Identification Number",
    "id.fincen_identifier": "FinCEN Identifier issued to a person or reporting company",
    "id.driver_license": "Driver licence number (US state / Canadian provincial)",
    "id.state_id_number": "US state-issued non-driver identification card number",
    "id.alien_registration": "USCIS Alien Registration Number (A-Number)",
    "id.uscis_card_number": "Card number printed on a USCIS card (I-551 / I-766)",
    "id.dod_id": "US Department of Defense ID number",
    "account.routing_number": "US bank routing number (ABA / RTN)",
    "account.statement_period": "Period covered by an account statement",
    "account.amount_due": "Amount payable on a statement or bill",
    "income.ytd_amount": "Year-to-date earnings on a pay statement",
    "income.total_tax": "Total tax liability declared for a tax year",
    "doc.mrz": "Machine-readable zone exactly as printed (ICAO 9303)",
    "doc.tax_year": "Tax year a return or information return covers",
    "doc.filing_status": "Tax filing status (single, married filing jointly, …)",
    "doc.immigration_category": "Immigration category code printed on an immigration document",
    "doc.pay_grade": "Military pay grade / rank",
    "doc.real_id_compliant": "Whether a US licence or ID card is REAL ID compliant",
    "doc.issuing_state": "State / province that issued the document",
    "doc.treaty_country": "Country of residence claimed for tax-treaty benefits",
    "entity.jurisdiction": "State / province / country of incorporation or organisation",
    "entity.fatca_status": "Chapter 4 (FATCA) status declared on a W-8BEN-E",
    "entity.status": "Registry status of the entity (good standing, active, dissolved)",
    # -- SEC-registered issuers and the US due-diligence pack --------------
    "id.cik": "SEC Central Index Key — the EDGAR filer id, printed as 'CIK (Filer ID Number)'",
    "id.commission_file_number": (
        "SEC Commission File Number carried on an Exchange Act filing cover page "
        "(e.g. 001-36743)"
    ),
    "id.cusip": "CUSIP identifier of a class of securities, as printed on a Schedule 13D/13G",
    "id.crd_number": "FINRA Central Registration Depository (CRD) number of a firm",
    "id.sec_registration_number": (
        "SEC registration number of a regulated firm: 8-NNNNN for a broker-dealer, "
        "801-NNNNN for a registered investment adviser"
    ),
    "id.pcaob_firm_id": "PCAOB-assigned registration id of an audit firm ('PCAOB ID')",
    "entity.ticker": "Trading symbol under which a class of securities trades",
    "entity.exchange": "Exchange on which a class of securities is registered",
    "entity.shares_outstanding": "Shares of a class outstanding as of a stated date",
    "entity.auditor": "Independent accounting firm that signed the audit report",
    "entity.auditor_since": "Year the signing audit firm was first engaged (PCAOB tenure line)",
    "entity.security_class": "Title of the class of securities a filing concerns",
    "entity.net_capital": "Net capital a broker-dealer reports under SEC Rule 15c3-1",
    "entity.assets_under_management": "Regulatory assets under management reported on Form ADV",
    "doc.period_covered": "Reporting period a periodic report covers",
    "doc.event_date": "Date of the event that a report is filed about",
}

#: ``name -> what the validator must enforce``, in the same spirit as the loader's own
#: contract. These are the North-American validators ``dce.extract.validate`` implements.
VALIDATOR_EXTENSIONS: dict[str, str] = {
    "ssn": (
        "US SSN: 9 digits, printed NNN-NN-NNNN. There is no check digit — validate the "
        "issuance rules instead: area 000, 666 and 900-999 are invalid, group 00 is "
        "invalid, serial 0000 is invalid. Accept an undashed read and compare compact."
    ),
    "itin": (
        "US ITIN: 9 digits beginning with 9, group in 50-65, 70-88, 90-92 or 94-99. "
        "python-stdnum's itin module omits the 50-65 range, so a bare stdnum call "
        "rejects legitimate ITINs — patch or wrap it."
    ),
    "ein": (
        "US EIN: 9 digits printed NN-NNNNNNN. No check digit; validate that the 2-digit "
        "IRS campus prefix is one the IRS actually issues."
    ),
    "sin_luhn": (
        "Canadian Social Insurance Number: 9 digits with a Luhn check digit. A SIN "
        "beginning with 9 is a temporary-resident SIN — valid, but flag it."
    ),
    "curp": (
        "Mexican CURP: 18 chars, 4 letters + YYMMDD + H/M (accept X for non-binary) + "
        "5 letters + 1 alphanumeric + 1 check digit, RENAPO alphabet, check digit "
        "enforced. Reject on a check-digit mismatch — a wrong CURP is not a CURP."
    ),
    "rfc": (
        "Mexican RFC: 13 chars for a person (4 letters + YYMMDD + 3 homoclave), 12 for a "
        "company (3 letters). Enforce the structure strictly; treat the homoclave check "
        "digit as a SOFT signal only — OCR mangles it routinely, so a mismatch lowers "
        "confidence and must not reject."
    ),
    "mrz_td1": (
        "ICAO 9303 TD1: three 30-character lines, per-field and composite check digits "
        "over the 7-3-1 weighting. Enforce every check digit."
    ),
    "name": (
        "A personal or entity name: non-empty after trimming, contains at least one "
        "letter, is not a bare date or amount. Normalise whitespace and case only."
    ),
    "address": (
        "A postal address: at least one alphanumeric token and one line/comma break or a "
        "postal-code-shaped token. Never reject on shape alone — addresses are free text."
    ),
    "amount": (
        "A currency amount with thousands separators and an optional symbol; normalise to "
        "a plain decimal. Preserve a leading minus or parenthesised negative."
    ),
    "generic_date": (
        "A date in an unknown convention. US documents print MONTH-first (MM/DD/YYYY), "
        "Canadian federal forms print ISO (YYYY-MM-DD) and Mexican documents print "
        "DAY-first (DD/MM/YYYY), so an ambiguous NN/NN/YYYY read must be resolved by the "
        "document's country and flagged when it cannot be — never silently assumed."
    ),
}


def _declare_namespace() -> None:
    """Contribute this pack's attribute keys and validator contract to the loader.

    Declares only what the registry does not already know. A pack adds vocabulary; it never
    redefines another pack's declaration, so the meaning of a shared name (``name``,
    ``amount``, ``generic_date``) can never depend on which pack happened to import first.
    """
    if _loader is None:  # pragma: no cover - only when the loader is absent
        return
    for key, description in ATTRIBUTE_KEY_EXTENSIONS.items():
        _loader.ATTRIBUTE_KEYS.setdefault(key, description)
    for name, contract in VALIDATOR_EXTENSIONS.items():
        _loader.VALIDATOR_CONTRACT.setdefault(name, contract)


_declare_namespace()


# ---------------------------------------------------------------------------
# Identifier patterns
#
# Over-capture by *shape* and let the named validator decide: the checksum sweep in
# dce.classify.anchors only treats a hit as decisive once dce.extract.validate accepts it.
# ---------------------------------------------------------------------------
SSN_PATTERN = r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"
ITIN_PATTERN = r"\b9\d{2}[-\s]?\d{2}[-\s]?\d{4}\b"
EIN_PATTERN = r"\b\d{2}-\d{7}\b"
#: ICAO 9303 TD3 (passport book) first line for a US-issued book.
MRZ_TD3_USA = r"P[<K]USA[A-Z0-9<]{5,}"
#: ICAO 9303 TD1 (wallet card) first line — passport card.
MRZ_TD1_USA = r"I[<K]USA[A-Z0-9<]{5,}"
#: TD1 first line of a Permanent Resident Card (I-551): "C1USA" / "C2USA".
MRZ_TD1_GREEN_CARD = r"C[12]USA[A-Z0-9<]{5,}"
#: USCIS Alien Registration Number ("A-Number").
A_NUMBER_PATTERN = r"\bA[-\s]?\d{8,9}\b"
#: Card number printed on an I-551 / I-766: three letters + 10 digits.
USCIS_CARD_NUMBER_PATTERN = r"\b[A-Z]{3}\d{10}\b"
#: DoD ID number on a CAC / uniformed services ID.
DOD_ID_PATTERN = r"\b\d{10}\b"
CURRENCY_PATTERN = r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?"
#: SEC Commission File Number as printed on an Exchange Act cover page: a 1-3 digit
#: series prefix, a hyphen, then the sequence ("001-36743", "1-15024"). Shape only — the
#: SEC publishes no check digit, and the prefix set is a lookup rather than a rule.
COMMISSION_FILE_NUMBER_PATTERN = r"\b\d{1,3}-\d{4,6}\b"
#: SEC registration number of a regulated firm: broker-dealers are numbered 8-NNNNN,
#: registered investment advisers 801-NNNNN. Anchored on the two known prefixes because a
#: bare "NNN-NNNNN" is indistinguishable from a Commission File Number.
SEC_FIRM_NUMBER_PATTERN = r"\b(?:801|8)-\d{4,6}\b"
#: CUSIP: 9 characters, 6-character issuer + 2-character issue + 1 check digit. The check
#: digit IS a published modulus-10 double-add-double, but ``dce.extract.validate`` has no
#: cusip validator, so this stays a shape test and the field says so. Do NOT promote it to
#: ``id_patterns``: an unvalidated 9-character alphanumeric run is not evidence.
CUSIP_PATTERN = r"\b[0-9A-Z]{8}\d\b"


# ---------------------------------------------------------------------------
# Small builders — the pack is data, these only remove repetition
# ---------------------------------------------------------------------------
def _a(
    text: str,
    *,
    lang: str = "en",
    decisive: bool = False,
    controls: Controls | None = None,
    zone: Zone | None = None,
) -> Anchor:
    """Build an :class:`~dce.models.Anchor`.

    Args:
        text: Verbatim string as it is printed on the document.
        lang: Language tag of the string ("en" throughout this pack).
        decisive: True only when the string alone is near-proof of the doctype.
        controls: What makes the decisive claim true — mandatory when ``decisive`` is
            set, and forbidden otherwise. See :class:`dce.models.Controls`. Deliberately
            without a default: a builder that supplied one would re-create the invisible
            claim the field exists to prevent.
        zone: Restrict the match to a layout zone (used for generic titles).

    Returns:
        The anchor.
    """
    return Anchor(text=text, lang=lang, decisive=decisive, controls=controls, zone=zone)


def _name_field(
    *,
    name: str = "full_name",
    key: str = "identity.full_name",
    required: bool = True,
    labels: list[str] | None = None,
) -> FieldSpec:
    """A person's printed name."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="name",
        required=required,
        pii=True,
        labels={"en": labels or ["Name", "Full Name", "Legal Name", "Print Name"]},
        validator="name",
    )


def _dob_field(*, required: bool = True) -> FieldSpec:
    """Date of birth."""
    return FieldSpec(
        name="date_of_birth",
        attribute_key="identity.date_of_birth",
        type="date",
        required=required,
        pii=True,
        labels={"en": ["Date of Birth", "DOB", "Birth Date", "Born"]},
        validator="generic_date",
    )


def _sex_field() -> FieldSpec:
    """Sex / gender marker."""
    return FieldSpec(
        name="sex",
        attribute_key="identity.sex",
        type="string",
        pii=True,
        labels={"en": ["Sex", "Gender"]},
        pattern=r"^[MFXmfx]$",
    )


def _address_field(
    *,
    name: str = "address",
    key: str = "address.residential",
    required: bool = False,
    labels: list[str] | None = None,
) -> FieldSpec:
    """A postal address."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="address",
        required=required,
        pii=True,
        labels={"en": labels or ["Address", "Street Address", "Mailing Address"]},
        validator="address",
    )


def _issue_date_field(*, required: bool = False) -> FieldSpec:
    """Document issue date."""
    return FieldSpec(
        name="issue_date",
        attribute_key="doc.issue_date",
        type="date",
        required=required,
        labels={"en": ["Issue Date", "Issued", "Date of Issue", "ISS"]},
        validator="generic_date",
    )


def _expiry_date_field(*, required: bool = False) -> FieldSpec:
    """Document expiry date."""
    return FieldSpec(
        name="expiry_date",
        attribute_key="doc.expiry_date",
        type="date",
        required=required,
        labels={"en": ["Expiration Date", "Expires", "EXP", "Valid Until", "Date of Expiry"]},
        validator="generic_date",
    )


def _real_id_compliant_field() -> FieldSpec:
    """Whether a state-issued licence or ID card meets the federal REAL ID standard.

    This is a **property of a licence or an ID card**, not a document type of its own, and
    saying so here is the whole point. The REAL ID Act of 2005 (Pub. L. 109-13, Div. B,
    Title II) and its implementing rule, 6 CFR Part 37, set *minimum standards* that a
    state-issued driver licence or identification card must meet before a federal agency
    may accept it. They create no new credential: 6 CFR 37.17 lists the data the card must
    carry on its face and 6 CFR 37.17(n) requires the DHS-approved security marking — the
    star — while the card's title stays ``DRIVER LICENSE`` / ``DRIVER'S LICENSE`` or
    ``IDENTIFICATION CARD``. There is no issuer anywhere that prints a document whose type
    is "REAL ID".

    The consequence for the registry is structural, not a matter of anchor tuning. The set
    of REAL ID cards is a strict *subset* of ``us_drivers_license`` united with
    ``us_state_id``: every REAL ID legitimately matches a superset doctype's anchors in
    full, because the superset's issuer prints everything the subset does. No string can
    separate a subset from its own superset, so any anchor asserted as decisive for a
    "REAL ID" doctype would violate the rule that a decisive anchor picks out exactly one
    document type. ``docs/PHOTO-ID-SOURCING.md`` predicted this before a specimen existed;
    the specimen confirmed it, and the doctype was merged away rather than propped up.

    Two facts make the same point from the issuer's side:

    * The AAMVA DL/ID Card Design Standard (2020, version 10) carries compliance in element
      ``DDA`` — "Compliance Type" — whose published values are ``F`` (fully compliant,
      covering all REAL ID licences and ID cards) and ``N`` (non-compliant). A one-character
      *field* in the PDF417 payload is the issuer's own answer to how this fact is recorded.
    * The words "REAL ID" are not exclusive to a compliant card. The Virginia DMV's AAMVA
      calibration sheet for the *standard, non-REAL-ID* licence prints them too, in the
      legend that explains what ``DDA`` means. A string that appears on the very documents
      it is supposed to exclude is not a discriminator in any zone.

    A negative result here is **not** proof of non-compliance: the marking is usually a gold
    or black star, which is a figure rather than text or a selection mark, and an OCR dump of
    a compliant card routinely contains neither. ``FEDERAL LIMITS APPLY`` is the reliable
    signal, and it is a *negative* one — it is the legend a state prints on a
    non-compliant card.
    """
    return FieldSpec(
        name="real_id_compliant",
        attribute_key="doc.real_id_compliant",
        type="bool",
        labels={
            "en": [
                "REAL ID",
                "REAL ID COMPLIANT",
                "Federally compliant",
                "Compliance Type",
                "FEDERAL LIMITS APPLY",
            ]
        },
        locators=["mark", "label", "regex"],
        notes="Compliance is signalled by a star that is a figure, not text — absence here "
        "is absence of evidence, not evidence of non-compliance. 'FEDERAL LIMITS "
        "APPLY' is the reliable negative, and AAMVA element DDA ('F'/'N') is the "
        "authoritative value when the PDF417 payload is available. Never report "
        "'not REAL ID' from a blank result.",
    )


def _tax_year_field() -> FieldSpec:
    """Tax year printed on a return or information return."""
    return FieldSpec(
        name="tax_year",
        attribute_key="doc.tax_year",
        type="number",
        required=True,
        labels={"en": ["Tax Year", "For calendar year", "Year"]},
        pattern=r"\b(19|20)\d{2}\b",
        locators=["label", "table", "regex"],
    )


def _amount_field(
    name: str,
    *,
    key: str = "income.amount",
    labels: list[str],
    required: bool = False,
) -> FieldSpec:
    """A currency amount pulled from a labelled box."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="number",
        required=required,
        labels={"en": labels},
        pattern=CURRENCY_PATTERN,
        validator="amount",
        locators=["table", "kv", "label", "regex"],
    )


def _entity_name_field(*, required: bool = True) -> FieldSpec:
    """Legal name of a company."""
    return FieldSpec(
        name="entity_legal_name",
        attribute_key="entity.legal_name",
        type="name",
        required=required,
        labels={
            "en": [
                "Name of Corporation",
                "Company Name",
                "Legal Name",
                "Entity Name",
                "Name of Limited Liability Company",
                "Business Name",
            ]
        },
        validator="name",
    )


def _ssn_field(*, required: bool = True, labels: list[str] | None = None) -> FieldSpec:
    """US Social Security Number."""
    return FieldSpec(
        name="ssn",
        attribute_key="id.ssn",
        type="id",
        required=required,
        pii=True,
        labels={"en": labels or ["Social Security Number", "SSN", "Social Security No."]},
        pattern=SSN_PATTERN,
        validator="ssn",
    )


def _ein_field(*, required: bool = True, labels: list[str] | None = None) -> FieldSpec:
    """US Employer Identification Number."""
    return FieldSpec(
        name="ein",
        attribute_key="id.ein",
        type="id",
        required=required,
        labels={
            "en": labels
            or ["Employer Identification Number", "EIN", "Employer ID Number", "Federal Tax ID"]
        },
        pattern=EIN_PATTERN,
        validator="ein",
    )


def _registrant_name_field() -> FieldSpec:
    """The filer's legal name, under the SEC's prescribed cover-page caption."""
    return FieldSpec(
        name="entity_legal_name",
        attribute_key="entity.legal_name",
        type="name",
        required=True,
        labels={
            "en": [
                "Exact name of Registrant as specified in its charter",
                "Exact name of registrant as specified in its charter",
                "Name of Issuer",
                "Name of Registrant",
                "Registrant",
            ]
        },
        validator="name",
    )


def _commission_file_number_field() -> FieldSpec:
    """SEC Commission File Number from an Exchange Act cover page."""
    return FieldSpec(
        name="commission_file_number",
        attribute_key="id.commission_file_number",
        type="id",
        labels={"en": ["Commission File Number", "Commission file number"]},
        pattern=COMMISSION_FILE_NUMBER_PATTERN,
        notes="Shape only. The SEC publishes no check digit for this number, and the "
        "series prefix is a lookup rather than a rule, so a mismatch must not reject.",
    )


def _jurisdiction_field() -> FieldSpec:
    """State or country of incorporation, as captioned on an SEC cover page."""
    return FieldSpec(
        name="jurisdiction_of_incorporation",
        attribute_key="entity.jurisdiction",
        type="string",
        labels={
            "en": [
                "State or other jurisdiction of incorporation or organization",
                "State or other jurisdiction of incorporation",
                "Jurisdiction of incorporation or organization",
                "Jurisdiction of Incorporation/Organization",
            ]
        },
    )


def _ticker_fields() -> list[FieldSpec]:
    """The Section 12(b) securities table: symbol and listing venue.

    Both are ``multi`` because a large issuer registers several classes — Apple's 10-K
    cover lists common stock plus seven note series — and taking only the first row would
    silently assert the issuer has one listed security.
    """
    return [
        FieldSpec(
            name="ticker",
            attribute_key="entity.ticker",
            type="string",
            multi=True,
            labels={"en": ["Trading Symbol(s)", "Trading Symbol", "Ticker or Trading Symbol"]},
            locators=["table", "kv", "label"],
        ),
        FieldSpec(
            name="exchange",
            attribute_key="entity.exchange",
            type="string",
            multi=True,
            labels={"en": ["Name of each exchange on which registered", "Exchange"]},
            locators=["table", "kv", "label"],
        ),
    ]


def _sec_cover_fields() -> list[FieldSpec]:
    """The block every Exchange Act periodic/current report prints above the body.

    Deliberately identical across 10-K, 10-Q, 8-K, 20-F and 6-K: the SEC prescribes these
    captions, so an extractor that works on one works on all of them, and the merged fact
    ("this counterparty is Commission File Number 001-36743") is the same fact whichever
    report it was read from.
    """
    return [
        _registrant_name_field(),
        _commission_file_number_field(),
        _jurisdiction_field(),
        _ein_field(
            required=False,
            labels=["I.R.S. Employer Identification No.", "IRS Employer Identification No."],
        ),
        _address_field(
            name="principal_executive_offices",
            key="address.registered",
            labels=[
                "Address of principal executive offices",
                "Address of principal executive office",
            ],
        ),
        *_ticker_fields(),
    ]


def _period_covered_field(labels: list[str], *, required: bool = True) -> FieldSpec:
    """The period a periodic report covers, under its form's own caption."""
    return FieldSpec(
        name="period_covered",
        attribute_key="doc.period_covered",
        type="date",
        required=required,
        labels={"en": labels},
        validator="generic_date",
    )


def _reporting_person_fields() -> list[FieldSpec]:
    """Who filed a Section 16 / Section 13(d) ownership statement, and about which issuer.

    The reporting person is very often a natural person — an officer, a director, a 10%
    holder — so ``full_name`` carries ``pii`` even though the same caption sometimes holds
    a fund's name. Flagging the field on the doctype is the only place the distinction can
    be made safely; it cannot be made per-document at extraction time.
    """
    return [
        FieldSpec(
            name="reporting_person",
            attribute_key="identity.full_name",
            type="name",
            required=True,
            pii=True,
            multi=True,
            labels={
                "en": [
                    "Name and Address of Reporting Person",
                    "Name of Reporting Person",
                    "NAME OF REPORTING PERSON",
                    "Names of Reporting Persons",
                ]
            },
            validator="name",
            locators=["table", "kv", "label"],
        ),
        FieldSpec(
            name="issuer_name",
            attribute_key="entity.legal_name",
            type="name",
            required=True,
            labels={"en": ["Issuer Name and Ticker or Trading Symbol", "Name of Issuer"]},
            validator="name",
        ),
    ]


def _mrz_fields(td: str) -> list[FieldSpec]:
    """Return the fields a travel document's machine-readable zone yields.

    The MRZ itself is captured as a field with the ICAO validator, because that validator
    checks a *zone*, not a value: the 7-3-1 check digits verify the whole block. The person
    fields it decodes carry value-shaped validators and run the ``mrz`` locator first, so a
    checksum-verified read beats a fuzzy label match on the printed side of the card.

    Args:
        td: ``"mrz_td3"`` for a passport book, ``"mrz_td1"`` for a card-format document.

    Returns:
        The MRZ block plus surname, given names, nationality, date of birth, sex and expiry.
    """
    src = ["mrz", "kv", "label"]
    return [
        FieldSpec(
            name="machine_readable_zone",
            attribute_key="doc.mrz",
            type="string",
            pii=True,
            validator=td,
            locators=["mrz", "regex"],
            notes="Captured verbatim. The check digits are what make every field decoded "
            "from it checksum-verified rather than merely read.",
        ),
        FieldSpec(
            name="surname",
            attribute_key="identity.surname",
            type="name",
            required=True,
            pii=True,
            labels={"en": ["Surname", "Last Name", "Family Name"]},
            validator="name",
            locators=src,
        ),
        FieldSpec(
            name="given_names",
            attribute_key="identity.given_names",
            type="name",
            required=True,
            pii=True,
            labels={"en": ["Given Names", "First Name", "Given Name"]},
            validator="name",
            locators=src,
        ),
        FieldSpec(
            name="nationality",
            attribute_key="identity.nationality",
            type="string",
            labels={"en": ["Nationality"]},
            locators=src,
        ),
        FieldSpec(
            name="date_of_birth",
            attribute_key="identity.date_of_birth",
            type="date",
            required=True,
            pii=True,
            labels={"en": ["Date of Birth", "DOB"]},
            validator="generic_date",
            locators=src,
        ),
        FieldSpec(
            name="sex",
            attribute_key="identity.sex",
            type="string",
            pii=True,
            labels={"en": ["Sex"]},
            pattern=r"^[MFXmfx]$",
            locators=src,
        ),
        FieldSpec(
            name="expiry_date",
            attribute_key="doc.expiry_date",
            type="date",
            required=True,
            labels={"en": ["Date of Expiration", "Expiration Date", "Expires"]},
            validator="generic_date",
            locators=src,
        ),
    ]


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------
SPECS: tuple[DocTypeSpec, ...] = (
    # ---------------------------------------------------------------- identity
    DocTypeSpec(
        doctype_id="us_passport",
        label="US Passport (book)",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Department of State",
        officially_valid=True,
        anchors=[
            _a("P<USA", decisive=True, controls=Controls.MRZ_PREFIX),
            _a("United States of America", zone=Zone.title),
            _a("PASSPORT", zone=Zone.title),
            _a("Department of State"),
            # "Authority" and "Place of Birth" were removed: they are ICAO 9303
            # visual-inspection-zone labels present on every state's passport, so they carry
            # no evidence that the book is American — and "Authority" is an ordinary English
            # word that appears on most government correspondence. The MRZ prefix and the
            # issuing department are what identify this document.
        ],
        id_patterns=[MRZ_TD3_USA],
        confusable_with={
            "us_passport_card": "the card is a TD1 wallet card whose MRZ starts I<USA and "
            "whose title is PASSPORT CARD; the book is TD3 (two "
            "44-character lines) and is titled PASSPORT",
            "ca_passport": "a Canadian book's MRZ starts P<CAN and its data page is "
            "bilingual (PASSEPORT)",
        },
        negative_anchors=["PASSPORT CARD", "PASSEPORT", "PASAPORTE"],
        fields=[
            *_mrz_fields("mrz_td3"),
            FieldSpec(
                name="passport_number",
                attribute_key="id.passport_number",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["Passport No.", "Passport Number", "Document No."]},
                locators=["mrz", "kv", "label"],
                notes="US passport numbers are 9 characters. Books issued before 2021 are "
                "all digits; newer books may begin with a letter. No check digit is "
                "published for the printed number — only the MRZ carries one, which "
                "is why the mrz locator runs first.",
            ),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                pii=True,
                labels={"en": ["Place of Birth"]},
            ),
            _issue_date_field(),
        ],
        handling="A passport data page is identity source evidence: retain the extracted "
        "fields rather than the page image unless retention is required.",
    ),
    DocTypeSpec(
        doctype_id="us_passport_card",
        label="US Passport Card",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Department of State",
        officially_valid=True,
        handling="A travel document: retain the extracted fields rather than the card image unless "
        "retention is required.",
        anchors=[
            _a("PASSPORT CARD", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("I<USA", decisive=True, controls=Controls.MRZ_PREFIX),
            _a("United States of America"),
            _a("Department of State"),
        ],
        id_patterns=[MRZ_TD1_USA],
        confusable_with={
            "us_passport": "the book is TD3 and titled PASSPORT; the card is TD1 and "
            "titled PASSPORT CARD",
        },
        negative_anchors=["Endorsements", "Class"],
        fields=[
            *_mrz_fields("mrz_td1"),
            FieldSpec(
                name="card_number",
                attribute_key="id.passport_number",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["Card No.", "Document No."]},
                locators=["mrz", "kv", "label"],
                notes="The passport card number is a distinct series from the book's and "
                "is not interchangeable with it.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_drivers_license",
        label="US Driver License (state-issued)",
        country="US",
        category=Category.identity,
        issuing_authority="State department of motor vehicles",
        officially_valid=True,
        handling="Several states restrict what may be retained from a licence, in particular the "
        "scanned PDF417 barcode payload. Keep the fields the CIP requires, not the swipe "
        "data.",
        anchors=[
            _a(
                "DRIVER LICENSE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
                zone=Zone.title,
            ),
            _a("DRIVER'S LICENSE", zone=Zone.title),
            _a("Endorsements"),
            _a("Restrictions"),
            _a("Class"),
            _a("DMV"),
            _a("ANSI 636"),
            _a("USA"),
        ],
        confusable_with={
            "us_state_id": "a licence grants driving privileges and prints CLASS / "
            "ENDORSEMENTS / RESTRICTIONS; a non-driver card is titled "
            "IDENTIFICATION CARD and often says NOT FOR DRIVING",
            "ca_drivers_license": "Canadian cards spell LICENCE, name a province, and "
            "carry PERMIS DE CONDUIRE where they are bilingual",
        },
        negative_anchors=[
            "NOT FOR DRIVING",
            "IDENTIFICATION CARD",
            "PERMIS DE CONDUIRE",
            "Ontario",
            "British Columbia",
            "Alberta",
        ],
        fields=[
            FieldSpec(
                name="license_number",
                attribute_key="id.driver_license",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["DLN", "License Number", "DL", "License No.", "ID"]},
                notes="US driver-licence numbers have no national format and no check "
                "digit: every state defines its own scheme (numeric, alphanumeric, "
                "or a soundex of the surname). No pattern is asserted on purpose — a "
                "wrong one would reject genuine licences. Never use a DLN as a "
                "cross-state deduplication key.",
            ),
            _name_field(labels=["Name", "LN", "FN", "Last Name", "First Name"]),
            _dob_field(),
            _sex_field(),
            _address_field(required=True),
            _issue_date_field(),
            _expiry_date_field(required=True),
            FieldSpec(
                name="issuing_state",
                attribute_key="doc.issuing_state",
                type="string",
                labels={"en": ["State", "Issued By"]},
            ),
            _real_id_compliant_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_state_id",
        label="US State Identification Card (non-driver)",
        country="US",
        category=Category.identity,
        issuing_authority="State department of motor vehicles",
        officially_valid=True,
        handling="Same retention limits as a driver licence: keep the identity fields, not the "
        "scanned barcode payload.",
        anchors=[
            # "IDENTIFICATION CARD" was decisive here, gated to the title zone. It is a
            # document-class name, not an issuer's string: Alberta and Manitoba print it too
            # (ca_provincial_photo_id claims it non-decisively), so it never met the bar
            # Anchor.decisive sets — "near-proof of the doctype". Demoting it leaves this
            # doctype with no decisive anchor, which is the honest position: there is no
            # string on a US non-driver ID that no other jurisdiction's ID card prints. The
            # combination is what identifies it, and the lexical tier is what weighs
            # combinations. "ANSI 636" is the AAMVA issuer-identification prefix and comes
            # closest, but it lives in the PDF417 barcode payload rather than the printed
            # face, so it is not reliably in an OCR dump either.
            #
            # The ``zone=Zone.title`` restriction that survived that demotion has now been
            # removed too, and the reason is a symmetry rule rather than a preference.
            # ``ca_provincial_photo_id`` claims the identical string ``Identification Card``
            # with no zone restriction. Two doctypes claiming one string must be equally
            # audible on it: while this claim was title-only, any payload without a title
            # zone — every plain-text extraction — heard the Canadian claim and not the
            # American one, so the *zone gate* decided the jurisdiction and the evidence
            # never got to. Measured: the Virginia DMV's AAMVA specimen for a Standard
            # Identification Card, a document whose own barcode encodes ``DCG USA`` and
            # ``DAJ VA``, scored ca_provincial_photo_id above us_state_id on this one
            # asymmetric anchor. That is the same shape of defect as the decisive/
            # non-decisive asymmetry ``_check_decisive_asymmetry`` was written for
            # (``PERMANENT RESIDENT CARD`` on us_green_card against ca_pr_card), one level
            # down: there the exclusivity differed, here the audibility does, and either way
            # a bookkeeping difference between two packs settles a cross-border question.
            # Ungated on both sides, the string is worth the same to both claimants and the
            # rest of the evidence decides.
            _a("IDENTIFICATION CARD"),
            _a("NOT FOR DRIVING"),
            _a("Identification No."),
            _a("DMV"),
            _a("ANSI 636"),
        ],
        confusable_with={
            "us_drivers_license": "the ID card carries no CLASS / ENDORSEMENTS / "
            "RESTRICTIONS block and is titled IDENTIFICATION CARD",
            "us_military_id": "the DoD card is titled UNIFORMED SERVICES IDENTIFICATION "
            "CARD and names the Department of Defense",
            "ca_provincial_photo_id": "Canadian photo cards name a province (e.g. ONTARIO "
            "PHOTO CARD) and are frequently bilingual",
        },
        negative_anchors=[
            "DRIVER LICENSE",
            "Endorsements",
            "UNIFORMED SERVICES",
            "Department of Defense",
            "Ontario",
            "British Columbia",
            "Alberta",
        ],
        fields=[
            FieldSpec(
                name="id_number",
                attribute_key="id.state_id_number",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["ID", "Identification No.", "Card Number", "No."]},
                notes="Same caveat as the driver-licence number: state-specific format, no "
                "check digit, not a national identifier.",
            ),
            _name_field(),
            _dob_field(),
            _sex_field(),
            _address_field(required=True),
            _issue_date_field(),
            _expiry_date_field(required=True),
            _real_id_compliant_field(),
        ],
    ),
    # ``us_real_id`` used to sit here and was **merged away**, deliberately, into the
    # ``real_id_compliant`` field that ``us_drivers_license`` and ``us_state_id`` now both
    # carry. See :func:`_real_id_compliant_field` for the reasoning in full; the short form
    # is that the REAL ID Act creates a standard, not a credential, so the set of REAL ID
    # cards is a strict subset of licence-union-ID-card, and no anchor can separate a subset
    # from its own superset. Keeping the doctype cost a wrong answer on the one specimen that
    # exists — a Virginia DMV AAMVA calibration sheet titled "Real ID Driver's License",
    # which is a driver's licence and was classified as one — and could never have gained a
    # correct one. The fact a KYC reviewer actually needs ("is this card acceptable for
    # federal identification?") survives as an extractable attribute; the doctype id that
    # promised to answer it, and could not, does not.
    DocTypeSpec(
        doctype_id="us_ssn_card",
        label="US Social Security Card",
        country="US",
        category=Category.identity,
        issuing_authority="Social Security Administration (SSA)",
        anchors=[
            _a("SOCIAL SECURITY ADMINISTRATION", zone=Zone.title),
            _a(
                "THIS NUMBER HAS BEEN ESTABLISHED FOR",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("SOCIAL SECURITY"),
            _a("NOT FOR IDENTIFICATION"),
            _a("VALID FOR WORK ONLY WITH DHS AUTHORIZATION"),
        ],
        id_patterns=[SSN_PATTERN],
        confusable_with={
            "us_w2": "a W-2 also prints 'For Social Security Administration' and an SSN, "
            "but is titled 'Wage and Tax Statement' and carries OMB No. 1545-0008 "
            "— which is why the SSA header only counts in the title zone here",
            "us_itin_letter": "an ITIN notice is a CP-565 letter, not a card, and its "
            "number begins with 9",
        },
        negative_anchors=["Wage and Tax Statement", "OMB No. 1545-0008", "Form W-2"],
        fields=[
            _ssn_field(),
            _name_field(),
        ],
        handling="The SSN is a restricted identifier. Store it masked (last four digits) "
        "unless a documented legal basis requires the full number, and never log "
        "it in plaintext.",
    ),
    DocTypeSpec(
        doctype_id="us_itin_letter",
        label="IRS ITIN Assignment Notice (CP-565)",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        anchors=[
            _a("CP 565", decisive=True, controls=Controls.FORM_NUMBER),
            _a(
                "We assigned you an Individual Taxpayer Identification Number",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("Individual Taxpayer Identification Number"),
            _a("Internal Revenue Service"),
            _a("CP565"),
            _a("ITIN"),
        ],
        id_patterns=[ITIN_PATTERN],
        confusable_with={
            "us_ssn_card": "an ITIN always begins with 9 and is issued by the IRS to people "
            "who are not eligible for an SSN",
        },
        negative_anchors=["SOCIAL SECURITY ADMINISTRATION"],
        fields=[
            FieldSpec(
                name="itin",
                attribute_key="id.itin",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["ITIN", "Individual Taxpayer Identification Number"]},
                pattern=ITIN_PATTERN,
                validator="itin",
                notes="Valid ITIN group ranges include 50-65, 70-88, 90-92 and 94-99; a "
                "validator that only knows the older ranges rejects genuine ITINs.",
            ),
            _name_field(),
            _address_field(key="address.mailing"),
            _issue_date_field(),
        ],
        handling="Treat an ITIN with the same care as an SSN.",
    ),
    DocTypeSpec(
        doctype_id="us_green_card",
        label="US Permanent Resident Card (Form I-551, 'green card')",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Citizenship and Immigration Services (USCIS)",
        officially_valid=True,
        anchors=[
            # THE defect this pack was corrected for. "PERMANENT RESIDENT CARD" was decisive
            # here and non-decisive on ca_pr_card, which relies on the French
            # "CARTE DE RÉSIDENT PERMANENT" / "RÉSIDENT PERMANENT". Drop the French line —
            # ordinary OCR loss on a bilingual card — and this was the only doctype holding a
            # decisive anchor, so L1 short-circuited: a Canadian permanent resident classified
            # us_green_card, country US, confidence 0.900, and routed into US immigration
            # logic. Canada, and the UK before it, title their residence cards in English the
            # same way; the string is a document-class name and was never near-proof of the
            # I-551. Nothing replaces it as decisive: the strings unique to an I-551 are the
            # TD1 MRZ document-code prefix (already covered by MRZ_TD1_GREEN_CARD in
            # id_patterns, which carries a check digit and reaches the checksum path) and
            # nothing else. USCIS, RESIDENT SINCE and CARD EXPIRES stay non-decisive, which
            # is what they always were.
            _a("PERMANENT RESIDENT CARD"),
            _a("Resident Since"),
            _a("C1USA"),
            _a("USCIS"),
            _a("United States of America"),
            _a("Card Expires"),
        ],
        id_patterns=[MRZ_TD1_GREEN_CARD, A_NUMBER_PATTERN, USCIS_CARD_NUMBER_PATTERN],
        confusable_with={
            "ca_pr_card": "the Canadian PR card is bilingual — CARTE DE RÉSIDENT PERMANENT "
            "— and names IRCC; the I-551 says USCIS and UNITED STATES OF "
            "AMERICA",
            "us_ead": "the employment authorization card is titled EMPLOYMENT "
            "AUTHORIZATION and carries a CATEGORY code, not RESIDENT SINCE",
        },
        negative_anchors=["CARTE DE RÉSIDENT PERMANENT", "EMPLOYMENT AUTHORIZATION", "IRCC"],
        fields=[
            *_mrz_fields("mrz_td1"),
            FieldSpec(
                name="alien_number",
                attribute_key="id.alien_registration",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["USCIS#", "A#", "A-Number", "Alien Registration Number"]},
                pattern=A_NUMBER_PATTERN,
                notes="The A-Number is 8 or 9 digits after the leading 'A'; older cards "
                "print 8. No check digit exists.",
            ),
            FieldSpec(
                name="card_number",
                attribute_key="id.uscis_card_number",
                type="id",
                pii=True,
                labels={"en": ["Card No.", "Card Number"]},
                pattern=USCIS_CARD_NUMBER_PATTERN,
            ),
            FieldSpec(
                name="resident_since",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Resident Since"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="category",
                attribute_key="doc.immigration_category",
                type="string",
                labels={"en": ["Category"]},
            ),
        ],
        handling="Copying a green card is restricted in some employment contexts (Form I-9 "
        "rules); retain only the fields the CIP requires.",
    ),
    DocTypeSpec(
        doctype_id="us_ead",
        label="US Employment Authorization Document (Form I-766)",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Citizenship and Immigration Services (USCIS)",
        officially_valid=True,
        handling="Employment authorisation expires, often within a year. Record the expiry and "
        "re-verify at it rather than retaining the card image.",
        anchors=[
            _a(
                "EMPLOYMENT AUTHORIZATION CARD",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "EMPLOYMENT AUTHORIZATION DOCUMENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("I-766"),
            _a("USCIS"),
            _a("Category"),
            _a("Terms and Conditions"),
            _a("NOT VALID FOR REENTRY TO U.S."),
        ],
        id_patterns=[A_NUMBER_PATTERN, USCIS_CARD_NUMBER_PATTERN],
        confusable_with={
            "us_green_card": "the EAD carries a CATEGORY code and an expiry a year or two "
            "out; the I-551 says PERMANENT RESIDENT CARD and RESIDENT SINCE",
        },
        negative_anchors=["PERMANENT RESIDENT CARD", "Resident Since"],
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="alien_number",
                attribute_key="id.alien_registration",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["USCIS#", "A#", "A-Number"]},
                pattern=A_NUMBER_PATTERN,
            ),
            FieldSpec(
                name="category",
                attribute_key="doc.immigration_category",
                type="string",
                required=True,
                labels={"en": ["Category"]},
                notes="Category codes (C08, C09, A05, …) determine work eligibility and are "
                "not free text; the card face is the authority.",
            ),
            _expiry_date_field(required=True),
        ],
        notes="The printed title varies between issues — some cards read EMPLOYMENT "
        "AUTHORIZATION CARD, others EMPLOYMENT AUTHORIZATION DOCUMENT — so both are "
        "declared decisive.",
    ),
    DocTypeSpec(
        doctype_id="us_birth_certificate",
        label="US Birth Certificate",
        country="US",
        category=Category.identity,
        issuing_authority="State or county vital records office",
        anchors=[
            _a(
                "CERTIFICATE OF LIVE BIRTH",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("CERTIFICATION OF BIRTH", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("State File Number"),
            _a("Vital Records"),
            _a("Registrar"),
            _a("Date Filed"),
        ],
        confusable_with={
            "us_naturalization_cert": "a naturalization certificate records a grant of "
            "citizenship by USCIS, not a birth registration",
            "mx_acta_nacimiento": "the Mexican birth record is titled ACTA DE NACIMIENTO "
            "and is issued by a Registro Civil",
        },
        negative_anchors=["ACTA DE NACIMIENTO", "CERTIFICATE OF NATURALIZATION"],
        fields=[
            _name_field(labels=["Name of Child", "Child's Name", "Name"]),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                required=True,
                pii=True,
                labels={"en": ["Place of Birth", "City", "County of Birth", "Hospital"]},
            ),
            FieldSpec(
                name="mother_name",
                attribute_key="identity.mother_name",
                type="name",
                pii=True,
                labels={"en": ["Mother", "Mother's Name", "Maiden Name"]},
                validator="name",
            ),
            FieldSpec(
                name="father_name",
                attribute_key="identity.father_name",
                type="name",
                pii=True,
                labels={"en": ["Father", "Father's Name"]},
                validator="name",
            ),
            FieldSpec(
                name="file_number",
                attribute_key="doc.reference_number",
                type="id",
                labels={"en": ["State File Number", "Certificate Number", "File No."]},
                notes="Every state numbers its vital records differently; there is no "
                "national format.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_naturalization_cert",
        label="US Certificate of Naturalization (Form N-550/N-570)",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Citizenship and Immigration Services (USCIS)",
        anchors=[
            _a(
                "CERTIFICATE OF NATURALIZATION",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("Department of Homeland Security"),
            _a("USCIS Registration No."),
            _a("Petition No."),
            _a("Certificate No."),
        ],
        id_patterns=[A_NUMBER_PATTERN],
        confusable_with={
            "us_citizenship_cert": "N-550 is issued when an adult naturalises; N-560 "
            "(CERTIFICATE OF CITIZENSHIP) is issued when citizenship "
            "was acquired or derived through a parent",
        },
        negative_anchors=["CERTIFICATE OF CITIZENSHIP"],
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="certificate_number",
                attribute_key="doc.reference_number",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["Certificate No.", "Certificate Number"]},
                notes="USCIS certificate numbers have changed in length and prefix across issues; "
                "no format is asserted.",
            ),
            FieldSpec(
                name="alien_number",
                attribute_key="id.alien_registration",
                type="id",
                pii=True,
                labels={"en": ["USCIS Registration No.", "A#"]},
                pattern=A_NUMBER_PATTERN,
            ),
            FieldSpec(
                name="naturalization_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date of naturalization", "Date"]},
                validator="generic_date",
            ),
        ],
        handling="Federal law restricts reproduction of a naturalization certificate; "
        "prefer storing extracted fields over the image.",
    ),
    DocTypeSpec(
        doctype_id="us_citizenship_cert",
        label="US Certificate of Citizenship (Form N-560/N-561)",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Citizenship and Immigration Services (USCIS)",
        anchors=[
            _a(
                "CERTIFICATE OF CITIZENSHIP",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("Department of Homeland Security"),
            _a("USCIS Registration No."),
        ],
        id_patterns=[A_NUMBER_PATTERN],
        confusable_with={
            "us_naturalization_cert": "citizenship derived through a parent (N-560) versus "
            "naturalisation as an adult (N-550)",
            "ca_citizenship_certificate": "the Canadian certificate names IRCC and is "
            "bilingual (CERTIFICAT DE CITOYENNETÉ)",
        },
        negative_anchors=["CERTIFICATE OF NATURALIZATION", "CITOYENNETÉ"],
        fields=[
            _name_field(),
            _dob_field(),
            FieldSpec(
                name="certificate_number",
                attribute_key="doc.reference_number",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["Certificate No."]},
                notes="As on the naturalization certificate, the numbering has changed across "
                "issues; no format is asserted.",
            ),
            FieldSpec(
                name="alien_number",
                attribute_key="id.alien_registration",
                type="id",
                pii=True,
                labels={"en": ["USCIS Registration No.", "A#"]},
                pattern=A_NUMBER_PATTERN,
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_military_id",
        label="US Uniformed Services ID / Common Access Card",
        country="US",
        category=Category.identity,
        issuing_authority="U.S. Department of Defense (DoD)",
        officially_valid=True,
        anchors=[
            _a(
                "UNIFORMED SERVICES IDENTIFICATION CARD",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("COMMON ACCESS CARD", decisive=True, controls=Controls.ISSUER_NAME),
            _a(
                "Geneva Conventions Identification Card",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("Department of Defense"),
            _a("DoD ID"),
            _a("Pay Grade"),
            _a("Rank"),
        ],
        id_patterns=[DOD_ID_PATTERN],
        confusable_with={
            "us_state_id": "the DoD card names the Department of Defense and prints a pay "
            "grade; a state card names a state DMV",
        },
        negative_anchors=["DMV", "DRIVER LICENSE"],
        fields=[
            _name_field(),
            _dob_field(required=False),
            FieldSpec(
                name="dod_id",
                attribute_key="id.dod_id",
                type="id",
                required=True,
                pii=True,
                labels={"en": ["DoD ID Number", "DoD ID"]},
                pattern=DOD_ID_PATTERN,
                notes="The DoD ID number is 10 digits and carries no public check digit.",
            ),
            FieldSpec(
                name="pay_grade",
                attribute_key="doc.pay_grade",
                type="string",
                labels={"en": ["Pay Grade", "Rank"]},
            ),
            _expiry_date_field(),
        ],
        handling="Photocopying a US military ID is restricted (18 U.S.C. 701); prefer "
        "field extraction over image retention.",
    ),
    # ---------------------------------------------------------------------- tax
    DocTypeSpec(
        doctype_id="us_w9",
        label="IRS Form W-9 — Request for Taxpayer Identification Number and Certification",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        applies_to="both",
        anchors=[
            _a("Request for Taxpayer Identification Number and Certification"),
            _a("Form W-9"),
            _a("Go to www.irs.gov/FormW9", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _a("Internal Revenue Service"),
            _a("Department of the Treasury"),
            _a("Backup Withholding"),
            _a("Exempt payee code"),
            _a("Taxpayer Identification Number"),
        ],
        id_patterns=[SSN_PATTERN, EIN_PATTERN],
        confusable_with={
            "us_w8ben": "W-9 certifies U.S. person status; W-8BEN is the Certificate of "
            "Foreign Status of Beneficial Owner for a non-U.S. INDIVIDUAL",
            "us_w8bene": "the -E variant is the same certificate for a foreign ENTITY",
        },
        negative_anchors=[
            "Certificate of Foreign Status",
            "Form W-8BEN",
            "Chapter 4 Status",
            "GIIN",
        ],
        fields=[
            _name_field(labels=["Name (as shown on your income tax return)", "Name"]),
            FieldSpec(
                name="business_name",
                attribute_key="entity.legal_name",
                type="name",
                labels={"en": ["Business name/disregarded entity name", "Business name"]},
                validator="name",
            ),
            FieldSpec(
                name="tax_classification",
                attribute_key="entity.constitution",
                type="string",
                required=True,
                labels={
                    "en": [
                        "Federal tax classification",
                        "Individual/sole proprietor",
                        "C Corporation",
                        "S Corporation",
                        "Partnership",
                        "Trust/estate",
                        "Limited liability company",
                    ]
                },
                locators=["mark", "kv", "label"],
                notes="Answered by ticking one of seven boxes — the checkbox binding IS the "
                "answer here, which is why the mark locator runs first.",
            ),
            _address_field(
                key="address.mailing",
                required=True,
                labels=[
                    "Address (number, street, and apt. or suite no.)",
                    "City, state, and ZIP code",
                ],
            ),
            _ssn_field(required=False),
            _ein_field(required=False, labels=["Employer identification number", "EIN"]),
            FieldSpec(
                name="signature_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date", "Signature of U.S. person"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_w8ben",
        label="IRS Form W-8BEN — Certificate of Foreign Status (individual)",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        anchors=[
            _a(
                "Certificate of Foreign Status of Beneficial Owner for United States Tax "
                "Withholding and Reporting (Individuals)",
            ),
            _a(
                "For use by individuals. Entities must use Form W-8BEN-E",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("Internal Revenue Service"),
            _a("Foreign tax identifying number"),
            _a("Claim of Tax Treaty Benefits"),
            _a("OMB No. 1545-1621"),
        ],
        confusable_with={
            "us_w8bene": "W-8BEN is for a natural person; W-8BEN-E is for an entity and "
            "carries a Chapter 4 (FATCA) status block and a GIIN field. Both "
            "share OMB No. 1545-1621, which is why that number is not decisive "
            "for either",
            "us_w9": "W-9 is the U.S.-person counterpart",
        },
        negative_anchors=[
            "Chapter 4 Status (FATCA status)",
            "GIIN",
            "(Entities)",
            "Request for Taxpayer Identification Number",
        ],
        fields=[
            _name_field(labels=["Name of individual who is the beneficial owner", "Name"]),
            FieldSpec(
                name="country_of_citizenship",
                attribute_key="identity.nationality",
                type="string",
                required=True,
                labels={"en": ["Country of citizenship"]},
            ),
            _address_field(
                required=True,
                labels=["Permanent residence address", "City or town, state or province"],
            ),
            _address_field(
                name="mailing_address",
                key="address.mailing",
                labels=["Mailing address (if different from above)"],
            ),
            FieldSpec(
                name="foreign_tin",
                attribute_key="id.foreign_tin",
                type="id",
                pii=True,
                labels={"en": ["Foreign tax identifying number", "FTIN"]},
                notes="A foreign TIN has no US format; do not pattern-match it.",
            ),
            _ssn_field(
                required=False,
                labels=["U.S. taxpayer identification number (SSN or ITIN)"],
            ),
            FieldSpec(
                name="treaty_country",
                attribute_key="doc.treaty_country",
                type="string",
                labels={
                    "en": [
                        "I certify that the beneficial owner is a resident of",
                        "Claim of Tax Treaty Benefits",
                    ]
                },
            ),
            FieldSpec(
                name="signature_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date (MM-DD-YYYY)", "Date"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_w8bene",
        label="IRS Form W-8BEN-E — Certificate of Status of Beneficial Owner (entity)",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        applies_to="corporate",
        anchors=[
            _a(
                "Certificate of Status of Beneficial Owner for United States Tax Withholding "
                "and Reporting (Entities)",
            ),
            _a(
                "For use by entities. Individuals must use Form W-8BEN",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: the *individual* W-8BEN prints "Entities must use Form
            # W-8BEN-E" in its own header, so the bare form number appears on both forms
            # and would misclassify every W-8BEN as a W-8BEN-E.
            _a("Form W-8BEN-E"),
            _a("Chapter 4 Status (FATCA status)"),
            _a("GIIN"),
            _a("Disregarded Entity"),
            _a("Internal Revenue Service"),
        ],
        confusable_with={
            "us_w8ben": "the individual form has no Chapter 4 / FATCA status block",
            "us_w9": "W-9 is the U.S.-person counterpart",
        },
        negative_anchors=["(Individuals)", "Request for Taxpayer Identification Number"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="country_of_incorporation",
                attribute_key="entity.jurisdiction",
                type="string",
                required=True,
                labels={"en": ["Country of incorporation or organization"]},
            ),
            FieldSpec(
                name="chapter_3_status",
                attribute_key="entity.constitution",
                type="string",
                labels={"en": ["Chapter 3 Status (entity type)"]},
                locators=["mark", "kv", "label"],
            ),
            FieldSpec(
                name="chapter_4_status",
                attribute_key="entity.fatca_status",
                type="string",
                labels={"en": ["Chapter 4 Status (FATCA status)"]},
                locators=["mark", "kv", "label"],
            ),
            FieldSpec(
                name="giin",
                attribute_key="id.giin",
                type="id",
                labels={"en": ["GIIN"]},
                pattern=r"\b[A-Z0-9]{6}\.[A-Z0-9]{5}\.[A-Z]{2}\.\d{3}\b",
                notes="A GIIN is 19 characters in four dot-separated blocks; the IRS "
                "publishes no check digit, so this is a shape test only.",
            ),
            _address_field(
                key="address.registered", required=True, labels=["Permanent residence address"]
            ),
            _ein_field(required=False, labels=["U.S. taxpayer identification number (TIN)"]),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_w2",
        label="IRS Form W-2 — Wage and Tax Statement",
        country="US",
        category=Category.tax,
        issuing_authority="Employer; filed with the Social Security Administration",
        anchors=[
            _a("OMB No. 1545-0008", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("Wage and Tax Statement", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _a("Form W-2"),
            _a("Social security wages"),
            _a("Wages, tips, other compensation"),
            _a("Federal income tax withheld"),
            _a("Employer identification number"),
            _a("Copy B—To Be Filed With Employee's FEDERAL Tax Return"),
        ],
        id_patterns=[SSN_PATTERN, EIN_PATTERN],
        confusable_with={
            "us_1099": "a 1099 reports non-employee payments and names a PAYER; a W-2 names "
            "an EMPLOYER and carries OMB No. 1545-0008",
            "us_paystub": "a pay stub covers one pay period; a W-2 covers a calendar year",
        },
        negative_anchors=["Nonemployee compensation", "1099-NEC"],
        fields=[
            _ssn_field(labels=["Employee's social security number", "Employee's SSN"]),
            _ein_field(labels=["Employer identification number (EIN)"]),
            FieldSpec(
                name="employer_name",
                attribute_key="income.employer",
                type="name",
                required=True,
                labels={"en": ["Employer's name, address, and ZIP code", "Employer's name"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _name_field(labels=["Employee's first name and initial", "Employee's name"]),
            _address_field(labels=["Employee's address and ZIP code"]),
            _amount_field(
                "wages", labels=["Wages, tips, other compensation", "Box 1"], required=True
            ),
            _amount_field(
                "federal_tax_withheld",
                key="income.tax_deducted",
                labels=["Federal income tax withheld", "Box 2"],
            ),
            _tax_year_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_1099",
        label="IRS Form 1099 (information return: NEC / MISC / INT / DIV)",
        country="US",
        category=Category.tax,
        issuing_authority="Payer; filed with the Internal Revenue Service (IRS)",
        anchors=[
            _a("1099-NEC"),
            _a("1099-MISC"),
            _a("1099-INT"),
            _a("1099-DIV"),
            _a("OMB No. 1545-0116", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("Nonemployee compensation"),
            _a("PAYER'S TIN"),
            _a("RECIPIENT'S TIN"),
            _a("Miscellaneous Information"),
        ],
        id_patterns=[SSN_PATTERN, EIN_PATTERN],
        confusable_with={
            "us_w2": "the W-2 is for employees and carries OMB No. 1545-0008",
        },
        negative_anchors=["Wage and Tax Statement", "OMB No. 1545-0008"],
        fields=[
            FieldSpec(
                name="payer_name",
                attribute_key="income.employer",
                type="name",
                required=True,
                labels={"en": ["PAYER'S name, street address", "PAYER'S name"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _ein_field(labels=["PAYER'S TIN"]),
            FieldSpec(
                name="recipient_tin",
                attribute_key="id.ssn",
                type="id",
                pii=True,
                labels={"en": ["RECIPIENT'S TIN"]},
                locators=["table", "kv", "label", "regex"],
                notes="The recipient TIN may be an SSN, an ITIN or an EIN, and on the "
                "recipient copy it is usually masked to the last four digits — so a "
                "value that fails the SSN rules must be recorded unverified, not "
                "dropped.",
            ),
            _name_field(labels=["RECIPIENT'S name"]),
            _address_field(labels=["Street address (including apt. no.)"]),
            _amount_field(
                "amount",
                labels=["Nonemployee compensation", "Box 1", "Other income", "Interest income"],
                required=True,
            ),
            _amount_field(
                "federal_tax_withheld",
                key="income.tax_deducted",
                labels=["Federal income tax withheld", "Box 4"],
            ),
            _tax_year_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_1040",
        label="IRS Form 1040 — U.S. Individual Income Tax Return",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        anchors=[
            _a(
                "U.S. Individual Income Tax Return",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("OMB No. 1545-0074", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("Form 1040"),
            _a("Filing Status"),
            _a("Adjusted gross income"),
            _a("Standard Deduction"),
            _a("Internal Revenue Service"),
        ],
        id_patterns=[SSN_PATTERN],
        confusable_with={
            "us_w2": "the W-2 is an employer statement attached to the return, not the "
            "return itself",
        },
        negative_anchors=["Wage and Tax Statement"],
        fields=[
            _name_field(labels=["Your first name and middle initial", "Your name"]),
            _ssn_field(labels=["Your social security number"]),
            FieldSpec(
                name="spouse_ssn",
                attribute_key="id.spouse_ssn",
                type="id",
                pii=True,
                labels={"en": ["Spouse's social security number"]},
                pattern=SSN_PATTERN,
                validator="ssn",
            ),
            FieldSpec(
                name="filing_status",
                attribute_key="doc.filing_status",
                type="string",
                required=True,
                labels={
                    "en": [
                        "Filing Status",
                        "Single",
                        "Married filing jointly",
                        "Married filing separately",
                        "Head of household",
                    ]
                },
                locators=["mark", "kv", "label"],
            ),
            _address_field(key="address.mailing", labels=["Home address (number and street)"]),
            _amount_field(
                "adjusted_gross_income",
                labels=["Adjusted gross income", "This is your adjusted gross income", "Line 11"],
                required=True,
            ),
            _amount_field(
                "taxable_income", key="income.total_income", labels=["Taxable income", "Line 15"]
            ),
            _amount_field("total_tax", key="income.total_tax", labels=["Total tax"]),
            _tax_year_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_cp575",
        label="IRS EIN Assignment Notice (CP-575)",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        applies_to="corporate",
        anchors=[
            _a("CP 575", decisive=True, controls=Controls.FORM_NUMBER),
            _a(
                "We assigned you an Employer Identification Number",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "Thank you for applying for an Employer Identification Number",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("CP575"),
            _a("Internal Revenue Service"),
            _a("Employer Identification Number"),
        ],
        id_patterns=[EIN_PATTERN],
        confusable_with={
            "us_147c": "147C is the EIN *verification* letter issued on request; CP-575 is "
            "the one-time original assignment notice",
        },
        negative_anchors=["147C", "EIN Verification Letter"],
        fields=[
            _ein_field(),
            _entity_name_field(),
            _address_field(key="address.registered"),
            _issue_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_147c",
        label="IRS EIN Verification Letter (147C)",
        country="US",
        category=Category.tax,
        issuing_authority="Internal Revenue Service (IRS)",
        applies_to="corporate",
        anchors=[
            _a("EIN Verification Letter", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _a("147C"),
            _a("Internal Revenue Service"),
            _a("Employer Identification Number"),
        ],
        id_patterns=[EIN_PATTERN],
        confusable_with={
            "us_cp575": "CP-575 is the original assignment notice; a 147C is the "
            "replacement confirmation, and is what a bank normally receives",
        },
        negative_anchors=["CP 575", "We assigned you an Employer Identification Number"],
        fields=[
            _ein_field(),
            _entity_name_field(),
            _address_field(key="address.registered"),
            _issue_date_field(),
        ],
        notes="147C letters are produced by IRS agents and their layout varies "
        "considerably; the EIN and the entity name are the only dependable fields.",
    ),
    # ---------------------------------------------------------------- corporate
    DocTypeSpec(
        doctype_id="us_articles_incorporation",
        label="US Articles of Incorporation (corporation)",
        country="US",
        category=Category.corporate,
        issuing_authority="Secretary of State of the state of incorporation",
        applies_to="corporate",
        anchors=[
            # Demoted from decisive: "Articles of Incorporation" is the statutory title of
            # the same filing in Canada federally (CBCA) and in Ontario, BC and Alberta, and
            # both ca_articles_incorporation_federal and _provincial claim the string. What
            # separates the jurisdictions is the *statute* named on the document — the CA
            # doctypes are decisive on "CANADA BUSINESS CORPORATIONS ACT" and
            # "BUSINESS CORPORATIONS ACT (ONTARIO)" — and no equivalent single string exists
            # for the US, where fifty states each name their own act. "Secretary of State"
            # plus "For-Profit Corporation" is the real US signature, and it is a
            # combination, which is the lexical tier's job rather than L1's.
            _a("ARTICLES OF INCORPORATION"),
            # NOT decisive: "Certificate of Incorporation" is the exact registered title of
            # the Indian MCA's incorporation certificate, which has the stronger claim on
            # the string. Delaware and New York do title their filing that way, so the
            # anchor is kept — it contributes lexically and the Secretary of State
            # vocabulary below carries the rest.
            _a("CERTIFICATE OF INCORPORATION"),
            # Same reasoning, and it should have been applied here the first time.
            # "Certificate of Formation" is the shared statutory title of the entity-formation
            # instrument in Texas, New Jersey and Washington, and it titles the filing for BOTH
            # a for-profit corporation (TX Form 201) and an LLC (TX Form 205); the entity-type
            # line is the only discriminator. It was declared *decisive* on
            # us_articles_organization_llc alone, so a Texas corporation filing short-circuited
            # to "LLC" at confidence 0.90 — asserting an entity type the evidence contradicts,
            # which is the dangerous error direction in KYC. It is now a plain shared anchor on
            # both specs and the vocabulary below carries the decision.
            _a("CERTIFICATE OF FORMATION"),
            _a("Secretary of State"),
            _a("Registered Agent"),
            # The entity-type discriminators, which is where this decision belongs. On the
            # Texas pair they separate cleanly: Form 201 prints "For-Profit Corporation",
            # "Authorized Shares" and "Directors" and never "Limited Liability Company";
            # Form 205 is the mirror image.
            _a("For-Profit Corporation"),
            _a("Authorized Shares"),
            _a("Directors"),
            _a("Incorporator"),
            # Declared here as well as on the LLC because Texas uses an Organizer on both
            # forms. Left on the LLC alone it was a free edge on every corporation filing, for
            # a word that discriminates nothing.
            _a("Organizer"),
        ],
        confusable_with={
            "us_articles_organization_llc": "an LLC files ARTICLES OF ORGANIZATION or a "
            "CERTIFICATE OF FORMATION and has members "
            "rather than shares",
            "ca_articles_incorporation_federal": "the Canadian federal articles cite the "
            "Canada Business Corporations Act and are "
            "bilingual (STATUTS CONSTITUTIFS)",
            "in_certificate_incorporation": "Delaware and New York also title the filing "
            "CERTIFICATE OF INCORPORATION; the Indian "
            "certificate names the Ministry of Corporate "
            "Affairs and carries a CIN, the US one names a "
            "Secretary of State",
        },
        negative_anchors=[
            "ARTICLES OF ORGANIZATION",
            "STATUTS CONSTITUTIFS",
            "ACTA CONSTITUTIVA",
            "Canada Business Corporations Act",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="state_of_incorporation",
                attribute_key="entity.jurisdiction",
                type="string",
                required=True,
                labels={"en": ["State of Incorporation", "State"]},
            ),
            FieldSpec(
                name="incorporation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                required=True,
                labels={"en": ["Date of Incorporation", "Filed", "Effective Date"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="registered_agent",
                attribute_key="ownership.authorized_signer",
                type="name",
                labels={"en": ["Registered Agent", "Agent for Service"]},
                validator="name",
            ),
            _address_field(
                name="registered_office",
                key="address.registered",
                labels=["Registered Office", "Principal Office Address"],
            ),
            FieldSpec(
                name="file_number",
                attribute_key="doc.registration_number",
                type="id",
                labels={"en": ["File Number", "Entity Number", "Charter Number"]},
                notes="Each Secretary of State numbers filings differently; there is no "
                "national format.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_articles_organization_llc",
        label="US Articles of Organization / Certificate of Formation (LLC)",
        country="US",
        category=Category.corporate,
        issuing_authority="Secretary of State of the state of organization",
        applies_to="corporate",
        anchors=[
            _a("ARTICLES OF ORGANIZATION", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            # NOT decisive — see the matching comment on us_articles_incorporation. Texas,
            # New Jersey and Washington title the *corporation's* formation instrument
            # "Certificate of Formation" too, so this string identifies the filing but not the
            # entity type, and the entity type is the whole question.
            _a("CERTIFICATE OF FORMATION"),
            _a("Limited Liability Company"),
            _a("Secretary of State"),
            _a("Registered Agent"),
            _a("Organizer"),
            # The LLC side of the entity-type discrimination: an LLC is run by members or
            # managers, a corporation by directors, and neither form prints the other's word.
            _a("Managers"),
            _a("Members"),
        ],
        confusable_with={
            "us_articles_incorporation": "a corporation files ARTICLES OF INCORPORATION and "
            "issues shares",
            "us_operating_agreement": "the operating agreement is the members' private "
            "contract, not the state filing",
        },
        negative_anchors=["ARTICLES OF INCORPORATION", "OPERATING AGREEMENT"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="state_of_organization",
                attribute_key="entity.jurisdiction",
                type="string",
                required=True,
                labels={"en": ["State", "Jurisdiction"]},
            ),
            FieldSpec(
                name="formation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                required=True,
                labels={"en": ["Effective Date", "Date Filed", "Filed"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="registered_agent",
                attribute_key="ownership.authorized_signer",
                type="name",
                labels={"en": ["Registered Agent"]},
                validator="name",
            ),
            _address_field(
                name="registered_office",
                key="address.registered",
                labels=["Registered Office", "Principal Place of Business"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_operating_agreement",
        label="US LLC Operating Agreement",
        country="US",
        category=Category.corporate,
        issuing_authority="Private instrument executed by the members",
        applies_to="corporate",
        anchors=[
            _a("OPERATING AGREEMENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a(
                "LIMITED LIABILITY COMPANY OPERATING AGREEMENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("Membership Interest"),
            _a("Manager-Managed"),
            _a("Capital Contribution"),
            _a("Members"),
        ],
        confusable_with={
            "us_articles_organization_llc": "the articles are the public state filing; the "
            "operating agreement is the private contract "
            "that names members and their percentages",
            "ca_partnership_agreement": "a partnership agreement names partners, not "
            "members of an LLC",
        },
        negative_anchors=["ARTICLES OF ORGANIZATION", "PARTNERSHIP AGREEMENT"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="members",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels={"en": ["Member", "Members", "Name of Member"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="ownership_percentages",
                attribute_key="ownership.share",
                type="number",
                multi=True,
                labels={"en": ["Percentage Interest", "Membership Interest", "Units"]},
                pattern=r"\d{1,3}(?:\.\d+)?\s?%",
                locators=["table", "label", "regex"],
            ),
            FieldSpec(
                name="managers",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                labels={"en": ["Manager", "Managing Member"]},
                validator="name",
            ),
            FieldSpec(
                name="effective_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Effective Date", "Dated"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_bylaws",
        label="US Corporate Bylaws",
        country="US",
        category=Category.corporate,
        issuing_authority="Private instrument adopted by the board of directors",
        applies_to="corporate",
        anchors=[
            _a("BYLAWS", zone=Zone.title),
            # DEMOTED from decisive, and this is a measurement rather than a preference.
            # "Amended and Restated Bylaws of <company>" is how every SEC registrant captions
            # Exhibit 3.x of its annual report, so the string is printed, ungated and in the
            # body, on every Form 10-K that has ever restated its bylaws. Measured on Apple's
            # FY2025 10-K: this anchor fired, us_bylaws joined us_sec_10k in the decisive set,
            # the conclusive-L1 route declined because two doctypes held decisive evidence,
            # and the 10-K abstained at confidence 0.000 while its own exclusive cover-page
            # legend was matched. With the flag removed the same document classifies
            # us_sec_10k at 0.765.
            #
            # Nothing is lost. The claim was already false by the module docstring's rule —
            # "Amended and Restated Bylaws" is a document-CLASS name every issuer picks
            # independently — and the true claim, a title-zone "BYLAWS", is the line above.
            # Measured on corpus/us/us_bylaws.pdf with inferred zones, us_bylaws still
            # classifies at 0.747 with the flag gone; on a text-layer payload that specimen
            # abstained both before and after, because it never carried this string at all.
            _a("AMENDED AND RESTATED BYLAWS"),
            _a("Board of Directors"),
            _a("Annual Meeting of Shareholders"),
            _a("Quorum"),
            _a("Officers"),
        ],
        confusable_with={
            "us_articles_incorporation": "the articles are filed with the state; the bylaws "
            "are internal governance rules",
        },
        negative_anchors=["ARTICLES OF INCORPORATION", "Secretary of State"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                labels={"en": ["Director", "Board of Directors"]},
                validator="name",
                locators=["table", "label", "kv"],
            ),
            FieldSpec(
                name="officers",
                attribute_key="ownership.authorized_signer",
                type="name",
                multi=True,
                labels={"en": ["President", "Secretary", "Treasurer", "Officers"]},
                validator="name",
            ),
            FieldSpec(
                name="adoption_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Adopted", "Effective Date", "Dated"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_certificate_good_standing",
        label="US Certificate of Good Standing / Existence",
        country="US",
        category=Category.corporate,
        issuing_authority="Secretary of State",
        applies_to="corporate",
        anchors=[
            _a(
                "CERTIFICATE OF GOOD STANDING",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("CERTIFICATE OF EXISTENCE", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("is in good standing", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("Secretary of State"),
            _a("duly incorporated"),
        ],
        confusable_with={
            "ca_certificate_status": "the Canadian equivalent is a CERTIFICATE OF "
            "COMPLIANCE (federal) or a CERTIFICATE OF STATUS",
        },
        negative_anchors=["CERTIFICATE OF COMPLIANCE", "Corporations Canada"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="status",
                attribute_key="entity.status",
                type="string",
                required=True,
                labels={"en": ["Status", "in good standing", "Active"]},
            ),
            FieldSpec(
                name="incorporation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                labels={"en": ["Date of Incorporation", "Incorporated"]},
                validator="generic_date",
            ),
            _issue_date_field(required=True),
            FieldSpec(
                name="file_number",
                attribute_key="doc.registration_number",
                type="id",
                labels={"en": ["File Number", "Entity Number"]},
                notes="Each Secretary of State numbers its filings differently; there is no "
                "national format.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_fincen_boir",
        label="FinCEN Beneficial Ownership Information Report (BOIR)",
        country="US",
        category=Category.corporate,
        issuing_authority="Financial Crimes Enforcement Network (FinCEN)",
        applies_to="corporate",
        anchors=[
            _a(
                "BENEFICIAL OWNERSHIP INFORMATION REPORT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "Corporate Transparency Act",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("BOIR"),
            _a("FinCEN"),
            _a("Reporting Company"),
            _a("Company Applicant"),
            _a("Beneficial Owner"),
        ],
        confusable_with={
            "us_fincen_boi_cert": "the report is the filing; the confirmation is what "
            "FinCEN returns after a successful submission",
        },
        fields=[
            _entity_name_field(),
            _ein_field(required=False, labels=["Taxpayer Identification Number", "EIN"]),
            _address_field(
                key="address.registered",
                labels=["Current U.S. address", "Reporting company address"],
            ),
            FieldSpec(
                name="beneficial_owners",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels={
                    "en": ["Beneficial Owner", "Individual's last name", "Beneficial owner name"]
                },
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="company_applicants",
                attribute_key="ownership.authorized_signer",
                type="name",
                multi=True,
                pii=True,
                labels={"en": ["Company Applicant"]},
                validator="name",
            ),
            _issue_date_field(),
        ],
        handling="A BOIR carries beneficial owners' identity documents; FinCEN restricts "
        "onward disclosure — treat it as confidential.",
    ),
    DocTypeSpec(
        doctype_id="us_fincen_boi_cert",
        label="FinCEN BOIR Submission Confirmation / FinCEN ID certificate",
        country="US",
        category=Category.corporate,
        issuing_authority="Financial Crimes Enforcement Network (FinCEN)",
        applies_to="corporate",
        anchors=[
            _a("BOIR SUBMISSION CONFIRMATION", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _a("FinCEN Identifier"),
            _a("Submission Tracking ID"),
            _a("BOIR ID"),
            _a("FinCEN"),
        ],
        confusable_with={
            "us_fincen_boir": "the confirmation carries a tracking id and no beneficial "
            "owner detail; the report carries the owners themselves",
        },
        fields=[
            _entity_name_field(required=False),
            FieldSpec(
                name="fincen_id",
                attribute_key="id.fincen_identifier",
                type="id",
                labels={"en": ["FinCEN Identifier", "FinCEN ID"]},
                notes="Format deliberately not asserted — FinCEN has changed the "
                "identifier's layout since the January 2024 launch.",
            ),
            FieldSpec(
                name="tracking_id",
                attribute_key="doc.reference_number",
                type="id",
                required=True,
                labels={"en": ["Submission Tracking ID", "BOIR ID"]},
                notes="FinCEN does not publish the tracking id's layout and has changed it since "
                "the January 2024 launch.",
            ),
            _issue_date_field(),
        ],
        notes="The exact title of FinCEN's post-filing PDF has changed more than once since "
        "January 2024, so the decisive anchor here is best-effort: verify it against "
        "a live specimen, and prefer us_fincen_boir for an ambiguous FinCEN PDF.",
    ),
    # ------------------------------------------------------ financial / address
    DocTypeSpec(
        doctype_id="us_bank_statement",
        label="US Bank Statement",
        country="US",
        category=Category.financial,
        issuing_authority="Depository institution",
        applies_to="both",
        anchors=[
            _a("Beginning Balance"),
            _a("Ending Balance"),
            _a("Statement Period"),
            _a("Routing Number"),
            _a("Account Summary"),
            _a("Deposits and Additions"),
            _a("Member FDIC"),
            # Federally mandated boilerplate, which is the most reliable vocabulary a US
            # consumer bank statement has: unlike balances and column headings it is
            # prescribed rather than chosen by the bank, so it does not vary by institution
            # or by statement design.
            #
            # Regulation E, 12 CFR 1005.8(b), requires the error-resolution notice on the
            # periodic statement of any account that can receive an electronic fund
            # transfer, and 1005.8(b) publishes the model wording that essentially every US
            # bank reproduces verbatim. No non-US statement carries it, because no non-US
            # statement is subject to the EFTA.
            _a("In Case of Errors or Questions About Your Electronic Transfers"),
            _a("Electronic Fund Transfers"),
            # Fair Housing Act logotype requirement, 24 CFR 110. A US-only disclosure
            # printed on the statement itself. ("FDIC" alone is deliberately not an anchor:
            # the profile builder already derives it from "Member FDIC", and a four-letter
            # anchor is the class of token this registry has been burned by before.)
            _a("Equal Housing Lender"),
        ],
        confusable_with={
            "us_voided_check": "a voided cheque shows one MICR line and the word VOID, not "
            "a period of transactions",
            "ca_bank_statement": "Canadian statements print a TRANSIT NUMBER and often a "
            "French RELEVÉ DE COMPTE header",
            "mx_estado_cuenta": "the Mexican statement is titled ESTADO DE CUENTA and "
            "carries an 18-digit CLABE",
        },
        negative_anchors=["ESTADO DE CUENTA", "RELEVÉ DE COMPTE", "Transit Number"],
        fields=[
            _name_field(required=False, labels=["Account Holder", "Customer Name"]),
            _address_field(key="address.mailing", required=True),
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                type="id",
                required=True,
                pii=True,
                multi=True,
                labels={"en": ["Account Number", "Account No.", "Acct #"]},
                locators=["kv", "label", "table", "regex"],
                notes="Usually masked to the last four digits on a customer copy.",
            ),
            FieldSpec(
                name="routing_number",
                attribute_key="account.routing_number",
                type="id",
                labels={"en": ["Routing Number", "ABA", "RTN"]},
                notes="A US routing number is nine digits with a published mod-10 check "
                "digit, but no validator is declared for it — the value is captured "
                "unverified rather than checked with an invented rule.",
            ),
            _amount_field(
                "closing_balance",
                key="account.balance",
                labels=["Ending Balance", "Closing Balance", "New Balance"],
                required=True,
            ),
            FieldSpec(
                name="statement_period",
                attribute_key="account.statement_period",
                type="string",
                required=True,
                labels={"en": ["Statement Period", "For the period", "Statement Date"]},
            ),
        ],
        notes="Bank statements are issuer-branded with no common header, so this doctype "
        "declares no decisive anchor: classification rests on the lexical tier plus "
        "the US-specific routing-number vocabulary.",
    ),
    DocTypeSpec(
        doctype_id="us_utility_bill",
        label="US Utility Bill (proof of address)",
        country="US",
        category=Category.address_proof,
        issuing_authority="Utility provider",
        applies_to="both",
        anchors=[
            _a("Service Address"),
            _a("Billing Period"),
            _a("Rate Schedule"),
            _a("Budget Billing"),
            _a("Late Payment Charge"),
            _a("Meter Number"),
            _a("kWh"),
            _a("Total Amount Due"),
        ],
        confusable_with={
            "us_bank_statement": "a utility bill has a service address and a meter, not a "
            "transaction ledger",
            "ca_utility_bill": "Canadian electricity bills name a Hydro utility",
            "mx_comprobante_cfe": "the Mexican electricity bill names the Comisión Federal "
            "de Electricidad",
        },
        negative_anchors=["Hydro One", "Hydro-Québec", "COMISIÓN FEDERAL DE ELECTRICIDAD"],
        fields=[
            _name_field(required=True, labels=["Customer Name", "Account Name", "Name"]),
            _address_field(required=True, labels=["Service Address", "Service Location"]),
            FieldSpec(
                name="account_number",
                attribute_key="utility.consumer_number",
                type="id",
                pii=True,
                labels={"en": ["Account Number", "Customer Number"]},
                notes="Utility account numbers are assigned per provider; there is no shared "
                "format or check digit.",
            ),
            FieldSpec(
                name="provider",
                attribute_key="utility.service_provider",
                type="name",
                labels={"en": ["Utility", "Company"]},
                validator="name",
            ),
            FieldSpec(
                name="billing_period",
                attribute_key="utility.bill_period",
                type="string",
                required=True,
                labels={"en": ["Billing Period", "Service Period", "Bill Date"]},
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                labels=["Total Amount Due", "Amount Due", "Please Pay"],
            ),
            _issue_date_field(required=True),
        ],
        notes="No decisive anchor: utility bills are branded per provider. For "
        "proof-of-address purposes the age of the bill matters more than its issuer.",
    ),
    DocTypeSpec(
        doctype_id="us_lease_agreement",
        label="US Residential Lease Agreement",
        country="US",
        category=Category.address_proof,
        issuing_authority="Private instrument between landlord and tenant",
        applies_to="both",
        anchors=[
            _a(
                "RESIDENTIAL LEASE AGREEMENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("LEASE AGREEMENT", zone=Zone.title),
            _a("Landlord"),
            _a("Tenant"),
            _a("Premises"),
            _a("Security Deposit"),
            _a("Term of Lease"),
            _a("Monthly Rent"),
        ],
        confusable_with={
            "ca_lease_agreement": "Canadian leases are titled RESIDENTIAL TENANCY AGREEMENT "
            "or, in Ontario, STANDARD FORM OF LEASE",
            "us_mortgage_statement": "a mortgage statement is a monthly servicer statement, "
            "not a contract",
        },
        negative_anchors=["RESIDENTIAL TENANCY AGREEMENT", "CONTRATO DE ARRENDAMIENTO"],
        fields=[
            FieldSpec(
                name="landlord_name",
                attribute_key="tenancy.landlord_name",
                type="name",
                required=True,
                labels={"en": ["Landlord", "Lessor", "Owner"]},
                validator="name",
            ),
            FieldSpec(
                name="tenant_name",
                attribute_key="tenancy.tenant_name",
                type="name",
                required=True,
                pii=True,
                labels={"en": ["Tenant", "Lessee", "Resident"]},
                validator="name",
            ),
            _address_field(
                name="premises",
                required=True,
                labels=["Premises", "Property Address", "Leased Premises"],
            ),
            _amount_field(
                "monthly_rent",
                key="tenancy.monthly_rent",
                labels=["Monthly Rent", "Rent", "Base Rent"],
            ),
            FieldSpec(
                name="term_start",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Commencement Date", "Lease Start", "Beginning"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="term_end",
                attribute_key="doc.expiry_date",
                type="date",
                labels={"en": ["Expiration Date", "Lease End", "Ending"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_mortgage_statement",
        label="US Mortgage Statement",
        country="US",
        category=Category.financial,
        issuing_authority="Mortgage servicer",
        anchors=[
            _a("MORTGAGE STATEMENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("Principal Balance"),
            _a("Escrow"),
            _a("Loan Number"),
            _a("Payment Due Date"),
            _a("Interest Rate"),
            _a("Amount Due"),
        ],
        confusable_with={
            "us_bank_statement": "a mortgage statement has a loan number and an escrow "
            "balance, not a transaction ledger",
            "ca_trust_deed": "in the US a 'deed of trust' is the security instrument for a "
            "mortgage; in Canada a deed of trust settles a trust",
        },
        fields=[
            _name_field(required=True, labels=["Borrower", "Customer Name"]),
            _address_field(required=True, labels=["Property Address", "Mailing Address"]),
            FieldSpec(
                name="loan_number",
                attribute_key="account.number",
                type="id",
                pii=True,
                required=True,
                labels={"en": ["Loan Number", "Account Number"]},
                notes="A servicer's internal loan number: no public format, no check digit, and it "
                "changes when the loan is sold.",
            ),
            FieldSpec(
                name="servicer",
                attribute_key="entity.legal_name",
                type="name",
                labels={"en": ["Servicer", "Lender"]},
                validator="name",
            ),
            _amount_field(
                "principal_balance",
                key="account.balance",
                labels=["Principal Balance", "Outstanding Principal"],
                required=True,
            ),
            _amount_field(
                "payment_due",
                key="account.amount_due",
                labels=["Total Amount Due", "Regular Monthly Payment"],
            ),
            FieldSpec(
                name="statement_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Statement Date"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_paystub",
        label="US Pay Stub / Earnings Statement",
        country="US",
        category=Category.financial,
        issuing_authority="Employer or payroll provider",
        anchors=[
            _a("EARNINGS STATEMENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("STATEMENT OF EARNINGS", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("Gross Pay"),
            _a("Net Pay"),
            _a("Pay Period"),
            _a("YTD"),
            _a("Deductions"),
            _a("Federal Withholding"),
        ],
        confusable_with={
            "us_w2": "a W-2 is annual and carries OMB No. 1545-0008; a pay stub covers one "
            "pay period and shows year-to-date columns",
        },
        negative_anchors=["Wage and Tax Statement", "OMB No. 1545-0008"],
        fields=[
            _name_field(labels=["Employee", "Employee Name"]),
            FieldSpec(
                name="employer_name",
                attribute_key="income.employer",
                type="name",
                required=True,
                labels={"en": ["Employer", "Company"]},
                validator="name",
            ),
            _amount_field(
                "gross_pay",
                key="income.gross_salary",
                labels=["Gross Pay", "Total Gross", "Gross Earnings"],
                required=True,
            ),
            _amount_field("net_pay", key="income.net_pay", labels=["Net Pay", "Take Home Pay"]),
            _amount_field(
                "ytd_gross",
                key="income.ytd_amount",
                labels=["YTD Gross", "Year to Date", "YTD Earnings"],
            ),
            FieldSpec(
                name="pay_period",
                attribute_key="account.statement_period",
                type="string",
                required=True,
                labels={"en": ["Pay Period", "Period Beginning", "Period Ending"]},
            ),
            _address_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_voided_check",
        label="US Voided Check (bank account verification)",
        country="US",
        category=Category.financial,
        issuing_authority="Depository institution",
        anchors=[
            _a("VOIDED CHECK", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("Pay to the order of"),
            _a("VOID"),
            _a("Memo"),
            _a("Routing Number"),
        ],
        confusable_with={
            "us_bank_statement": "a cheque carries a MICR line and no transaction history",
        },
        negative_anchors=["Statement Period", "Beginning Balance"],
        fields=[
            _name_field(required=True, labels=["Account Holder", "Name"]),
            _address_field(),
            FieldSpec(
                name="routing_number",
                attribute_key="account.routing_number",
                type="id",
                required=True,
                labels={"en": ["Routing Number", "ABA"]},
                notes="On a cheque the routing and account numbers come from the MICR line. "
                "OCR of the E-13B font is unreliable and its symbols are usually "
                "dropped, so a low-confidence read belongs in review, not in a "
                "normalised field.",
            ),
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                type="id",
                pii=True,
                required=True,
                labels={"en": ["Account Number"]},
                notes="Read from the MICR line; length and grouping differ by bank, so no format "
                "is asserted.",
            ),
            FieldSpec(
                name="bank_name",
                attribute_key="account.bank_name",
                type="name",
                labels={"en": ["Bank", "Bank Name"]},
                validator="name",
            ),
        ],
        notes="A voided cheque is an ordinary cheque with VOID written across it. The bare "
        "word is weak evidence — it appears in contract prose too — so only the "
        "phrase VOIDED CHECK is decisive.",
    ),
    DocTypeSpec(
        doctype_id="us_trust_agreement",
        label="US Trust Agreement / Declaration of Trust",
        country="US",
        category=Category.corporate,
        issuing_authority="Private instrument executed by the settlor and trustee",
        applies_to="corporate",
        anchors=[
            _a("TRUST AGREEMENT", zone=Zone.title),
            _a("DECLARATION OF TRUST", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("REVOCABLE LIVING TRUST", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("Grantor"),
            _a("Settlor"),
            _a("Trustee"),
            _a("Beneficiary"),
            _a("Trust Corpus"),
        ],
        confusable_with={
            "ca_trust_deed": "the Canadian instrument is a DEED OF TRUST / ACTE DE FIDUCIE",
            "us_operating_agreement": "an operating agreement governs an LLC, not a trust",
        },
        negative_anchors=["DEED OF TRUST", "ACTE DE FIDUCIE", "OPERATING AGREEMENT"],
        fields=[
            FieldSpec(
                name="trust_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels={"en": ["Name of Trust", "Trust", "Known as"]},
                validator="name",
            ),
            FieldSpec(
                name="settlor",
                attribute_key="ownership.beneficial_owner",
                type="name",
                required=True,
                pii=True,
                labels={"en": ["Grantor", "Settlor", "Trustor"]},
                validator="name",
            ),
            FieldSpec(
                name="trustees",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels={"en": ["Trustee", "Successor Trustee"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="beneficiaries",
                attribute_key="ownership.partner",
                type="name",
                multi=True,
                pii=True,
                labels={"en": ["Beneficiary", "Beneficiaries"]},
                validator="name",
            ),
            FieldSpec(
                name="execution_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Dated", "Executed", "Effective Date"]},
                validator="generic_date",
            ),
        ],
    ),
    # ------------------------------------------------ SEC-registered issuers
    #
    # Everything from here to us_focus_x17a5 is filed with, or prescribed by, the U.S.
    # Securities and Exchange Commission, which makes this the one corner of the pack where
    # decisive anchors are easy to come by — and the one where the obvious decisive anchor is
    # the wrong one.
    #
    # **A bare form designator is NOT decisive.** "FORM 10-K" is a string the SEC controls, so
    # it passes the letter of the rule in the module docstring; it fails the substance, which
    # is that a decisive anchor asserts the string appears on ONE document type. Issuers print
    # each other's form designators as cross-references constantly. Measured over real EDGAR
    # filings (Apple's FY2025 10-K, Q3-FY2026 10-Q, July-2026 8-K and 2026 DEF 14A; Novartis'
    # FY2025 20-F; two Schedule 13D/13G filings), counting case-insensitive occurrences:
    #
    #     "form 10-k"  ->  84 in the 10-K, 11 in the 10-Q, 10 in the DEF 14A
    #     "form 10-q"  ->  41 in the 10-Q,  1 in the 10-K,  2 in the DEF 14A
    #     "form 8-k"   ->   3 in the 8-K,   1 in the 10-K,  1 in the DEF 14A
    #     "schedule 13g" -> 1 in the 13G, but also 2 in the DEF 14A, 2 in the 20-F, 1 in the 13D
    #
    # Had "FORM 10-K" been decisive, every 10-Q and every proxy statement would have carried a
    # decisive claim for the annual report. Two doctypes then hold decisive anchors, the
    # conclusive-L1 route declines, and the classification of the commonest filing in the pack
    # degrades to lexical — for a string that identifies the *form family*, not the document.
    #
    # **The SEC-prescribed cover-page legend IS decisive.** The same measurement over the same
    # documents:
    #
    #     "quarterly report pursuant to section 13"            -> 10-Q only
    #     "shell company report pursuant to section 13"        -> 20-F only
    #     "aggregate market value of the voting and non-voting" -> 10-K only
    #     "date of report (date of earliest event reported)"   -> 8-K only
    #     "proxy statement pursuant to section 14(a)"          -> DEF 14A only
    #     "rule 13d-101" / "rule 13d-102"                      -> SC 13D / SC 13G only
    #
    # A registrant transcribes the legend of the form it is *filing*; it refers to other forms
    # by number. So the legends are the decisive anchors here and the designators are ordinary
    # ones, which is also why "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES
    # EXCHANGE ACT OF 1934" is decisive for nothing: Form 10-K and Form 20-F print it verbatim,
    # and it was measured on both.
    #
    # OMB control numbers are the other genuinely exclusive string, but only where they survive
    # onto the filed copy. They do not on a 10-K, 10-Q, 8-K, DEF 14A or Schedule 13G — EDGAR
    # filings drop the OMB block that the blank form carries — and they do on the XSL-rendered
    # Forms 3/4/5 and Form D, which is exactly where they are used below.
    DocTypeSpec(
        doctype_id="us_sec_10k",
        label="SEC Form 10-K — Annual Report (US domestic registrant)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the registrant with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            # The Form 10-K cover-page requirement that no other Exchange Act form carries.
            # Truncated before "common equity"/"stock", which is the one part that varies
            # between issuers; everything up to "non-voting" is the settled wording.
            _a(
                "aggregate market value of the voting and non-voting",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: printed 11 times inside Apple's 10-Q and 10 times inside its proxy
            # statement. See the section note above.
            _a("FORM 10-K"),
            # NOT decisive: Form 20-F prints this same legend for a foreign private issuer's
            # annual report, and us_sec_20f declares it too.
            _a(
                "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934"
            ),
            _a(
                "TRANSITION REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934"
            ),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
            _a("Commission File Number"),
            _a("For the fiscal year ended"),
            _a("Securities registered pursuant to Section 12(b) of the Act"),
            # Form 10-K item numbering. The caption text alone is shared with Form 10-Q, which
            # carries it as Part I Item 2; the item number is what makes it 10-K-specific.
            _a("Item 9A. Controls and Procedures"),
            _a("DOCUMENTS INCORPORATED BY REFERENCE"),
        ],
        id_patterns=[EIN_PATTERN],
        confusable_with={
            "us_sec_10q": "the quarterly report covers a fiscal quarter and prints "
            "QUARTERLY REPORT PURSUANT TO SECTION 13 OR 15(d); the annual report "
            "prints ANNUAL REPORT and the aggregate-market-value cover legend",
            "us_sec_20f": "Form 20-F is the annual report of a FOREIGN private issuer and "
            "shares the ANNUAL REPORT legend verbatim; it adds the shell-company "
            "option and a Translation of Registrant's name into English line, and "
            "never carries the aggregate-market-value legend",
            "ca_aif": "the Canadian analogue is the Annual Information Form filed on SEDAR+ "
            "under National Instrument 51-102, not with the SEC",
            "us_auditor_report": "the audit report is a section INSIDE the 10-K as well as a "
            "standalone deliverable, which is why it claims no decisive anchor",
        },
        negative_anchors=[
            "ANNUAL INFORMATION FORM",
            "SEDAR",
            "REPORT OF FOREIGN PRIVATE ISSUER",
            "Ministry of Corporate Affairs",
        ],
        fields=[
            *_sec_cover_fields(),
            _period_covered_field(["For the fiscal year ended", "Fiscal Year Ended"]),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={
                    "en": [
                        "shares of common stock were issued and outstanding as of",
                        "shares outstanding",
                    ]
                },
                pattern=r"\b\d{1,3}(?:,\d{3})+\b",
                notes="Read from prose on the cover page, not from a labelled box; treat a "
                "miss as normal rather than as a failed extraction.",
            ),
            FieldSpec(
                name="auditor",
                attribute_key="entity.auditor",
                type="name",
                labels={"en": ["Auditor Name", "Auditor Firm ID", "PCAOB ID"]},
                validator="name",
                notes="Present as an inline-XBRL cover-page tag on filings from FY2021 "
                "onward; older 10-Ks name the auditor only inside the audit report.",
            ),
            FieldSpec(
                name="filing_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date", "Dated", "Filed"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_10q",
        label="SEC Form 10-Q — Quarterly Report",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the registrant with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            _a(
                "QUARTERLY REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: appears once inside the 10-K and twice inside the proxy statement.
            _a("FORM 10-Q"),
            _a("For the quarterly period ended"),
            _a(
                "TRANSITION REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934"
            ),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
            _a("Commission File Number"),
            # Form 10-Q item numbering: the annual report puts controls and procedures at
            # Item 9A, so the number carries the discrimination the caption cannot.
            _a("Item 4. Controls and Procedures"),
            _a("CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS"),
        ],
        id_patterns=[EIN_PATTERN],
        confusable_with={
            "us_sec_10k": "the annual report covers a fiscal year and carries the "
            "aggregate-market-value cover legend, which no 10-Q has",
        },
        negative_anchors=["ANNUAL INFORMATION FORM", "SEDAR", "REPORT OF FOREIGN PRIVATE ISSUER"],
        fields=[
            *_sec_cover_fields(),
            _period_covered_field(["For the quarterly period ended", "Quarterly Period Ended"]),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={"en": ["shares of common stock were issued and outstanding as of"]},
                pattern=r"\b\d{1,3}(?:,\d{3})+\b",
            ),
            FieldSpec(
                name="filing_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date", "Dated", "Filed"]},
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_8k",
        label="SEC Form 8-K — Current Report",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the registrant with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            # The 8-K's own dateline caption. Verified present once on a filed 8-K and absent
            # from the 10-K, 10-Q, DEF 14A, 20-F and both Schedule 13 filings measured.
            _a(
                "Date of Report (Date of earliest event reported)",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "Check the appropriate box below if the Form 8-K filing is intended to "
                "simultaneously satisfy the filing obligation",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: cross-referenced from the 10-K and the proxy statement.
            _a("FORM 8-K"),
            _a("CURRENT REPORT"),
            _a("Pursuant to Section 13 OR 15(d) of The Securities Exchange Act of 1934"),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
            _a("Commission File Number"),
            _a("Former name or former address, if changed since last report"),
        ],
        id_patterns=[EIN_PATTERN],
        confusable_with={
            "ca_material_change_report": "the Canadian analogue is the material change "
            "report filed on SEDAR+ under National Instrument 51-102",
        },
        negative_anchors=["MATERIAL CHANGE REPORT", "SEDAR", "National Instrument 51-102"],
        fields=[
            *_sec_cover_fields(),
            FieldSpec(
                name="event_date",
                attribute_key="doc.event_date",
                type="date",
                required=True,
                labels={
                    "en": [
                        "Date of Report (Date of earliest event reported)",
                        "Date of earliest event reported",
                    ]
                },
                validator="generic_date",
            ),
            FieldSpec(
                name="reported_items",
                attribute_key="",
                type="string",
                multi=True,
                labels={"en": ["Item 1.01", "Item 2.02", "Item 5.02", "Item 8.01"]},
                notes="An 8-K is identified in practice by WHICH numbered item it reports "
                "under; the item captions are prescribed by the form, the narrative is not.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_def14a",
        label="SEC Schedule 14A — Definitive Proxy Statement (DEF 14A)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the registrant with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            _a(
                "Proxy Statement Pursuant to Section 14(a) of the Securities Exchange Act of 1934",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "Confidential, for Use of the Commission Only (as permitted by Rule 14a-6(e)(2))",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: a 10-K that incorporates the proxy by reference names the schedule,
            # and DEFA14A / PRE 14A carry it too.
            _a("SCHEDULE 14A"),
            _a("Definitive Proxy Statement"),
            _a("Preliminary Proxy Statement"),
            _a("Filed by the Registrant"),
            _a("Payment of Filing Fee"),
            _a("Soliciting Material under"),
            _a("Name of Registrant as Specified in Its Charter"),
        ],
        confusable_with={
            "ca_information_circular": "the Canadian analogue is the management information "
            "circular filed on SEDAR+ under National Instrument 51-102, which cites "
            "no Rule 14a-6",
            # us_bylaws is deliberately NOT listed here, even though a proxy statement and a
            # set of bylaws share a great deal of vocabulary — directors, quorum, annual
            # meeting of shareholders — and the lexical channel does rank us_bylaws first on
            # a real DEF 14A. Measured on Apple's 2026 proxy statement, listing it cost the
            # verdict outright. ``confusable_with`` is not documentation to the cascade; it
            # is an operational cluster, and ``_muted_contenders`` suppresses the
            # conclusive-L1 route whenever a declared peer holds a decisive anchor that this
            # payload could not hear. us_bylaws gates "BYLAWS" to the title zone, a text-layer
            # read has no title zone, so the peer was muted and the proxy statement abstained
            # while holding two exclusive decisive anchors of its own.
            #
            # The declaration was wrong on the merits as well. These are not one document
            # family that has to be told apart: the bylaws are the standing governance
            # instrument, the proxy statement solicits votes for one dated meeting, and a
            # reviewer handed either would never wonder whether it was the other. The lexical
            # overlap is a property of the profile, not an ambiguity about the document.
        },
        negative_anchors=["MANAGEMENT INFORMATION CIRCULAR", "SEDAR", "National Instrument 51-102"],
        fields=[
            _registrant_name_field(),
            FieldSpec(
                name="meeting_date",
                attribute_key="doc.event_date",
                type="date",
                labels={"en": ["Annual Meeting", "Date and Time", "Meeting Date"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels={"en": ["Nominees for Election", "Director Nominees", "Our Directors"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="beneficial_owners",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                pii=True,
                labels={
                    "en": [
                        "Security Ownership of Certain Beneficial Owners and Management",
                        "Beneficial Owner",
                        "5% Holders",
                    ]
                },
                validator="name",
                locators=["table", "kv", "label"],
                notes="The 5%-holder table in a proxy statement is the cheapest public read "
                "on a listed counterparty's ownership; it is also the reason this doctype "
                "is worth carrying at all for KYC.",
            ),
            FieldSpec(
                name="auditor",
                attribute_key="entity.auditor",
                type="name",
                labels={"en": ["Ratification of the Appointment of", "Independent Auditors"]},
                validator="name",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_20f",
        label="SEC Form 20-F — Annual Report of a Foreign Private Issuer",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the registrant with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            # The fourth "Mark One" option, which exists only on Form 20-F. Verified on
            # Novartis AG's FY2025 20-F and absent from every other filing measured.
            _a(
                "SHELL COMPANY REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            # NOT decisive: the 6-K cover asks whether the issuer files under cover of Form
            # 20-F or Form 40-F, so the designator appears on a document that is not a 20-F.
            _a("FORM 20-F"),
            # NOT decisive: Form 10-K prints this legend verbatim. This is the string the
            # module docstring warns about — an annual-report legend is not a jurisdiction.
            _a(
                "ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934"
            ),
            _a(
                "TRANSITION REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES "
                "EXCHANGE ACT OF 1934"
            ),
            _a("REGISTRATION STATEMENT PURSUANT TO SECTION 12(b)"),
            _a("Translation of Registrant's name into English"),
            _a("Jurisdiction of incorporation or organization"),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
            _a("Commission file number"),
            _a("Date of event requiring this shell company report"),
        ],
        confusable_with={
            "us_sec_10k": "both are annual reports and both print the ANNUAL REPORT legend; "
            "only Form 20-F offers the shell-company option and asks for a "
            "translation of the registrant's name into English",
            "us_sec_6k": "Form 6-K is the foreign private issuer's INTERIM furnishing and "
            "names Rule 13a-16 / 15d-16",
        },
        negative_anchors=[
            "aggregate market value of the voting and non-voting",
            "REPORT OF FOREIGN PRIVATE ISSUER PURSUANT TO RULE 13a-16",
        ],
        fields=[
            *_sec_cover_fields(),
            _period_covered_field(["for the fiscal year ended", "For the fiscal year ended"]),
            FieldSpec(
                name="contact_person",
                attribute_key="ownership.authorized_signer",
                type="name",
                pii=True,
                labels={
                    "en": [
                        "Name, Telephone, E-mail and/or Facsimile number and Address of "
                        "Company Contact Person",
                        "Company Contact Person",
                    ]
                },
                validator="name",
            ),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={
                    "en": [
                        "Indicate the number of outstanding shares of each of the issuer's "
                        "classes of capital or common stock",
                    ]
                },
                pattern=r"\b\d{1,3}(?:,\d{3})+\b",
            ),
        ],
        handling="A 20-F identifies a NON-US issuer that has chosen a US listing. The "
        "doctype's country is US because the SEC prescribes the form; the counterparty's "
        "jurisdiction is whatever the jurisdiction_of_incorporation field says, and the two "
        "must not be conflated downstream.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_6k",
        label="SEC Form 6-K — Report of a Foreign Private Issuer",
        country="US",
        category=Category.corporate,
        issuing_authority="Furnished by the registrant to the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            _a(
                "REPORT OF FOREIGN PRIVATE ISSUER PURSUANT TO RULE 13a-16 OR 15d-16",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("FORM 6-K"),
            _a("UNDER THE SECURITIES EXCHANGE ACT OF 1934"),
            _a("For the month of"),
            _a(
                "Indicate by check mark whether the registrant files or will file annual "
                "reports under cover of"
            ),
            _a("Translation of registrant's name into English"),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
            _a("Commission File Number"),
        ],
        confusable_with={
            "us_sec_20f": "Form 20-F is the same issuer's ANNUAL report; the 6-K furnishes "
            "whatever the issuer has already made public at home",
            "us_sec_8k": "Form 8-K is the domestic registrant's current report and prints a "
            "Date of earliest event reported line, which a 6-K has no equivalent of",
        },
        negative_anchors=[
            "SHELL COMPANY REPORT PURSUANT TO SECTION 13",
            "Date of Report (Date of earliest event reported)",
        ],
        fields=[
            _registrant_name_field(),
            _commission_file_number_field(),
            _address_field(
                name="principal_executive_offices",
                key="address.registered",
                labels=["Address of principal executive office"],
            ),
            _period_covered_field(["For the month of"], required=False),
            FieldSpec(
                name="signatory",
                attribute_key="ownership.authorized_signer",
                type="name",
                pii=True,
                labels={"en": ["By", "Signature", "Name and Title"]},
                validator="name",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_sc13d",
        label="SEC Schedule 13D — Beneficial Ownership Report (activist / control intent)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the beneficial owner with the U.S. Securities and "
        "Exchange Commission",
        applies_to="both",
        anchors=[
            # The rule that prescribes Schedule 13D's own form. 13G is prescribed by Rule
            # 13d-102, so the two citations separate the schedules cleanly where the schedule
            # NAMES do not: a 13D cover routinely refers to Schedule 13G in the Rule 13d-1(e)
            # paragraph, and a proxy statement refers to both.
            _a("Rule 13d-101", decisive=True, controls=Controls.STATUTE_TITLE),
            _a("SCHEDULE 13D"),
            _a("Information to be Included in Statements Filed Pursuant"),
            _a("Under the Securities Exchange Act of 1934"),
            _a("Name of Issuer"),
            _a("Title of Class of Securities"),
            _a("CUSIP Number"),
            _a("Date of Event Which Requires Filing of This Statement"),
            _a("AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON"),
            _a("SOLE DISPOSITIVE POWER"),
        ],
        confusable_with={
            "us_sec_sc13g": "13G is the passive / exempt investor's short-form statement, "
            "prescribed by Rule 13d-102; 13D is filed by a holder with control "
            "intent and adds Item 4, Purpose of Transaction",
            "ca_early_warning_report": "the Canadian analogue is the early warning report "
            "under National Instrument 62-104, filed on SEDAR+ at a 10% threshold",
            "us_fincen_boir": "a BOIR reports beneficial owners of a private reporting "
            "company to FinCEN; a Schedule 13 reports a stake in a LISTED issuer to the SEC",
        },
        negative_anchors=["Rule 13d-102", "EARLY WARNING REPORT", "National Instrument 62-104"],
        fields=[
            *_reporting_person_fields(),
            FieldSpec(
                name="security_class",
                attribute_key="entity.security_class",
                type="string",
                labels={"en": ["Title of Class of Securities"]},
            ),
            FieldSpec(
                name="cusip",
                attribute_key="id.cusip",
                type="id",
                labels={"en": ["CUSIP Number", "CUSIP No."]},
                pattern=CUSIP_PATTERN,
                notes="A CUSIP carries a published modulus-10 check digit, but "
                "dce.extract.validate has no cusip validator, so this is a shape test only. "
                "Do not report a CUSIP as checksum_verified.",
            ),
            FieldSpec(
                name="shares_beneficially_owned",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={
                    "en": [
                        "AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON",
                        "Aggregate Amount Beneficially Owned",
                    ]
                },
                pattern=r"\b\d{1,3}(?:,\d{3})+\b",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="percent_of_class",
                attribute_key="ownership.share",
                type="string",
                labels={
                    "en": [
                        "PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW",
                        "Percent of Class",
                    ]
                },
                pattern=r"\b\d{1,3}(?:\.\d+)?\s?%",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="event_date",
                attribute_key="doc.event_date",
                type="date",
                labels={"en": ["Date of Event Which Requires Filing of This Statement"]},
                validator="generic_date",
            ),
            FieldSpec(
                name="citizenship",
                attribute_key="identity.nationality",
                type="string",
                pii=True,
                labels={"en": ["CITIZENSHIP OR PLACE OF ORGANIZATION"]},
                locators=["table", "kv", "label"],
            ),
        ],
        handling="A Schedule 13 names natural persons and their holdings; the reporting "
        "person block is personal data even when the filer is a fund.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_sc13g",
        label="SEC Schedule 13G — Beneficial Ownership Report (passive / exempt holder)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the beneficial owner with the U.S. Securities and "
        "Exchange Commission",
        applies_to="both",
        anchors=[
            _a("Rule 13d-102", decisive=True, controls=Controls.STATUTE_TITLE),
            # NOT decisive: measured on the 13D cover, the DEF 14A and the 20-F as well.
            _a("SCHEDULE 13G"),
            _a(
                "Check the appropriate box to designate the rule pursuant to which this "
                "Schedule is filed"
            ),
            _a("Name of Issuer"),
            _a("Title of Class of Securities"),
            _a("CUSIP Number"),
            _a("Date of Event Which Requires Filing of this Statement"),
            _a("SHARED VOTING POWER"),
            _a("TYPE OF REPORTING PERSON"),
        ],
        confusable_with={
            "us_sec_sc13d": "13D is the control-intent long form, prescribed by Rule 13d-101 "
            "and carrying Item 4, Purpose of Transaction",
        },
        negative_anchors=["Rule 13d-101", "Purpose of Transaction", "EARLY WARNING REPORT"],
        fields=[
            *_reporting_person_fields(),
            FieldSpec(
                name="security_class",
                attribute_key="entity.security_class",
                type="string",
                labels={"en": ["Title of Class of Securities"]},
            ),
            FieldSpec(
                name="cusip",
                attribute_key="id.cusip",
                type="id",
                labels={"en": ["CUSIP Number", "CUSIP No."]},
                pattern=CUSIP_PATTERN,
                notes="Shape only — see us_sec_sc13d.cusip.",
            ),
            FieldSpec(
                name="shares_beneficially_owned",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={"en": ["AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON"]},
                pattern=r"\b\d{1,3}(?:,\d{3})+\b",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="percent_of_class",
                attribute_key="ownership.share",
                type="string",
                labels={
                    "en": ["PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW", "Percent of Class"]
                },
                pattern=r"\b\d{1,3}(?:\.\d+)?\s?%",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="event_date",
                attribute_key="doc.event_date",
                type="date",
                labels={"en": ["Date of Event Which Requires Filing of this Statement"]},
                validator="generic_date",
            ),
        ],
        handling="See us_sec_sc13d — the reporting person block is personal data.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_form3",
        label="SEC Form 3 — Initial Statement of Beneficial Ownership (Section 16 insider)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the insider with the U.S. Securities and Exchange "
        "Commission",
        applies_to="both",
        anchors=[
            # The three Section 16 forms nest: Form 4's title is a contiguous substring of
            # Form 5's. Each doctype is therefore anchored on the part of its own title that
            # the other two cannot contain — "INITIAL" here, "ANNUAL ... OF SECURITIES" on
            # Form 5 — plus its own OMB control number, which the XSL-rendered filing keeps.
            _a(
                "INITIAL STATEMENT OF BENEFICIAL OWNERSHIP OF SECURITIES",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("OMB Number: 3235-0104", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("Date of Event Requiring Statement"),
            _a("Name and Address of Reporting Person"),
            _a("Issuer Name and Ticker or Trading Symbol"),
            _a("Relationship of Reporting Person(s) to Issuer"),
            _a("10% Owner"),
            _a("Form filed by One Reporting Person"),
        ],
        confusable_with={
            "us_sec_form4": "Form 4 reports a CHANGE and dates the earliest transaction; "
            "Form 3 reports the holdings an insider starts with",
            "us_sec_form5": "Form 5 is the annual catch-up and titles itself ANNUAL "
            "STATEMENT OF CHANGES",
            "ca_sedi_insider_report": "the Canadian analogue is the SEDI insider report "
            "under National Instrument 55-104",
        },
        negative_anchors=["STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP", "SEDI"],
        fields=[
            *_reporting_person_fields(),
            FieldSpec(
                name="relationship_to_issuer",
                attribute_key="ownership.director",
                type="string",
                labels={
                    "en": [
                        "Relationship of Reporting Person(s) to Issuer",
                        "Director",
                        "10% Owner",
                        "Officer",
                    ]
                },
                locators=["mark", "table", "kv", "label"],
                notes="Answered by ticking Director / Officer / 10% Owner / Other, so the "
                "checkbox binding is the answer and the mark locator runs first.",
            ),
            FieldSpec(
                name="ticker",
                attribute_key="entity.ticker",
                type="string",
                labels={"en": ["Issuer Name and Ticker or Trading Symbol", "Trading Symbol"]},
            ),
            FieldSpec(
                name="event_date",
                attribute_key="doc.event_date",
                type="date",
                required=True,
                labels={"en": ["Date of Event Requiring Statement"]},
                validator="generic_date",
            ),
            _address_field(
                key="address.mailing",
                labels=["Name and Address of Reporting Person", "Street", "City"],
            ),
        ],
        handling="A Section 16 form is about a named individual and carries their home "
        "address; treat the whole document as personal data.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_form4",
        label="SEC Form 4 — Statement of Changes in Beneficial Ownership (Section 16 insider)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the insider with the U.S. Securities and Exchange "
        "Commission",
        applies_to="both",
        anchors=[
            # NOT the form's title: "STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP" is a
            # contiguous token substring of Form 5's "ANNUAL STATEMENT OF CHANGES IN
            # BENEFICIAL OWNERSHIP OF SECURITIES", so declaring it decisive would classify
            # every Form 5 as a Form 4. These two captions are Form 4's alone.
            _a("Date of Earliest Transaction", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _a("OMB Number: 3235-0287", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP"),
            _a(
                "Filed pursuant to Section 16(a) of the Securities Exchange Act of 1934 or "
                "Section 30(h) of the Investment Company Act of 1940"
            ),
            _a("Name and Address of Reporting Person"),
            _a("Issuer Name and Ticker or Trading Symbol"),
            _a("Transaction Code"),
            _a("Securities Acquired (A) or Disposed Of (D)"),
            _a("Rule 10b5-1(c)"),
        ],
        confusable_with={
            "us_sec_form5": "Form 5 is the annual catch-up filing and dates itself by the "
            "issuer's fiscal year end rather than by an earliest transaction",
            "us_sec_form3": "Form 3 is the insider's initial holdings statement",
            "ca_sedi_insider_report": "the Canadian analogue is the SEDI insider report "
            "under National Instrument 55-104",
        },
        negative_anchors=[
            "ANNUAL STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP OF SECURITIES",
            "INITIAL STATEMENT OF BENEFICIAL OWNERSHIP",
            "SEDI",
        ],
        fields=[
            *_reporting_person_fields(),
            FieldSpec(
                name="relationship_to_issuer",
                attribute_key="ownership.director",
                type="string",
                labels={
                    "en": [
                        "Relationship of Reporting Person(s) to Issuer",
                        "Director",
                        "10% Owner",
                        "Officer",
                    ]
                },
                locators=["mark", "table", "kv", "label"],
            ),
            FieldSpec(
                name="ticker",
                attribute_key="entity.ticker",
                type="string",
                labels={"en": ["Issuer Name and Ticker or Trading Symbol", "Trading Symbol"]},
            ),
            FieldSpec(
                name="transaction_date",
                attribute_key="doc.event_date",
                type="date",
                required=True,
                labels={"en": ["Date of Earliest Transaction", "Transaction Date"]},
                validator="generic_date",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="shares_owned_following_transaction",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels={
                    "en": [
                        "Amount of Securities Beneficially Owned Following Reported Transaction",
                        "Shares Owned Following Transaction",
                    ]
                },
                pattern=r"\b\d{1,3}(?:,\d{3})*\b",
                locators=["table", "kv", "label"],
            ),
        ],
        handling="See us_sec_form3 — a Section 16 form is personal data throughout.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_form5",
        label="SEC Form 5 — Annual Statement of Changes in Beneficial Ownership (Section 16)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the insider with the U.S. Securities and Exchange "
        "Commission",
        applies_to="both",
        anchors=[
            _a(
                "ANNUAL STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP OF SECURITIES",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("OMB Number: 3235-0362", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a(
                "Statement for Issuer's Fiscal Year Ended",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "Filed pursuant to Section 16(a) of the Securities Exchange Act of 1934 or "
                "Section 30(h) of the Investment Company Act of 1940"
            ),
            _a("Form 3 Holdings Reported"),
            _a("Form 4 Transaction Reported"),
            _a("Name and Address of Reporting Person"),
            _a("Issuer Name and Ticker or Trading Symbol"),
        ],
        confusable_with={
            "us_sec_form4": "Form 4 reports one transaction promptly and dates itself by the "
            "earliest transaction; Form 5 sweeps up a whole fiscal year",
            "us_sec_form3": "Form 3 is the insider's initial holdings statement",
            "ca_sedi_insider_report": "the Canadian analogue is the SEDI insider report "
            "under National Instrument 55-104",
        },
        negative_anchors=[
            "INITIAL STATEMENT OF BENEFICIAL OWNERSHIP",
            "Date of Earliest Transaction",
            "SEDI",
        ],
        fields=[
            *_reporting_person_fields(),
            FieldSpec(
                name="relationship_to_issuer",
                attribute_key="ownership.director",
                type="string",
                labels={
                    "en": [
                        "Relationship of Reporting Person(s) to Issuer",
                        "Director",
                        "10% Owner",
                        "Officer",
                    ]
                },
                locators=["mark", "table", "kv", "label"],
            ),
            _period_covered_field(["Statement for Issuer's Fiscal Year Ended"]),
            FieldSpec(
                name="ticker",
                attribute_key="entity.ticker",
                type="string",
                labels={"en": ["Issuer Name and Ticker or Trading Symbol", "Trading Symbol"]},
            ),
        ],
        handling="See us_sec_form3 — a Section 16 form is personal data throughout.",
    ),
    DocTypeSpec(
        doctype_id="us_sec_form_d",
        label="SEC Form D — Notice of Exempt Offering of Securities (Regulation D)",
        country="US",
        category=Category.corporate,
        issuing_authority="Filed by the issuer with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            _a(
                "Notice of Exempt Offering of Securities",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("OMB Number: 3235-0076", decisive=True, controls=Controls.CONTROL_NUMBER),
            _a("FORM D"),
            _a("Issuer's Identity"),
            _a("CIK (Filer ID Number)"),
            _a("Principal Place of Business and Contact Information"),
            _a("Related Persons"),
            _a("Federal Exemption(s) and Exclusion(s) Claimed"),
            _a(
                "Intentional misstatements or omissions of fact constitute federal criminal "
                "violations"
            ),
        ],
        confusable_with={
            "us_articles_organization_llc": "Form D names the issuer and its entity type but "
            "is a notice to the SEC, not a state formation filing",
        },
        fields=[
            FieldSpec(
                name="entity_legal_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels={"en": ["Name of Issuer"]},
                validator="name",
            ),
            FieldSpec(
                name="cik",
                attribute_key="id.cik",
                type="id",
                labels={"en": ["CIK (Filer ID Number)", "CIK"]},
                pattern=r"\b\d{7,10}\b",
                notes="A CIK is a sequence number of up to 10 digits, usually zero-padded on "
                "EDGAR. No check digit exists; the pattern is a shape test under the label.",
            ),
            _jurisdiction_field(),
            FieldSpec(
                name="entity_type",
                attribute_key="entity.constitution",
                type="string",
                labels={
                    "en": [
                        "Entity Type",
                        "Corporation",
                        "Limited Partnership",
                        "Limited Liability Company",
                    ]
                },
                locators=["mark", "kv", "label"],
            ),
            _address_field(
                name="principal_place_of_business",
                key="address.registered",
                labels=["Principal Place of Business and Contact Information", "Street Address 1"],
            ),
            FieldSpec(
                name="related_persons",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels={"en": ["Related Persons", "Executive Officer", "Promoter"]},
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _amount_field(
                "total_offering_amount",
                labels=["Total Offering Amount", "Total Amount Sold"],
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_sec_form_adv",
        label="SEC Form ADV — Investment Adviser Registration / Exempt Reporting Adviser Report",
        country="US",
        category=Category.corporate,
        issuing_authority="U.S. Securities and Exchange Commission / IARD",
        applies_to="corporate",
        anchors=[
            _a(
                "UNIFORM APPLICATION FOR INVESTMENT ADVISER REGISTRATION",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "REPORT BY EXEMPT REPORTING ADVISERS",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("FORM ADV"),
            _a("Identifying Information"),
            _a("Regulatory Assets Under Management"),
            _a("SEC File Number"),
            _a("Investment Adviser Public Disclosure"),
            _a("Submit an annual updating amendment to your registration"),
        ],
        confusable_with={
            "us_focus_x17a5": "a FOCUS report is the BROKER-DEALER's financial and "
            "operational report under Rule 17a-5; Form ADV is the investment "
            "ADVISER's registration form under the Advisers Act",
        },
        fields=[
            FieldSpec(
                name="entity_legal_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels={"en": ["Your full legal name", "Primary Business Name", "Legal Name"]},
                validator="name",
            ),
            FieldSpec(
                name="sec_file_number",
                attribute_key="id.sec_registration_number",
                type="id",
                labels={"en": ["SEC File Number", "SEC Number"]},
                pattern=SEC_FIRM_NUMBER_PATTERN,
                notes="A registered adviser is numbered 801-NNNNN. Shape only; the SEC "
                "publishes no check digit.",
            ),
            FieldSpec(
                name="crd_number",
                attribute_key="id.crd_number",
                type="id",
                labels={"en": ["CRD Number", "Organization CRD Number"]},
                pattern=r"\b\d{4,7}\b",
                notes="A FINRA CRD number is an unchecked sequence number; the pattern only "
                "keeps a date or an amount from binding to the label.",
            ),
            _address_field(
                key="address.registered",
                labels=["Principal Office and Place of Business", "Address"],
            ),
            FieldSpec(
                name="assets_under_management",
                attribute_key="entity.assets_under_management",
                type="number",
                labels={"en": ["Regulatory Assets Under Management", "Discretionary Amount"]},
                pattern=CURRENCY_PATTERN,
                validator="amount",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="direct_owners",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                pii=True,
                labels={"en": ["Direct Owners and Executive Officers", "Schedule A"]},
                validator="name",
                locators=["table", "kv", "label"],
                notes="Schedules A and B of Part 1A are the adviser's own ownership "
                "disclosure and are the reason this doctype earns its place in a KYC pack.",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_focus_x17a5",
        label="SEC Form X-17A-5 — FOCUS Report / Broker-Dealer Annual Audited Report",
        country="US",
        category=Category.financial,
        issuing_authority="Filed by the broker-dealer with the U.S. Securities and Exchange "
        "Commission",
        applies_to="corporate",
        anchors=[
            # Unlike the periodic-report designators, X-17A-5 is not cross-referenced from
            # other document types: the only documents that name it are the FOCUS report and
            # the annual audited report, which are Parts IIA and III of the same form.
            _a("FORM X-17A-5", decisive=True, controls=Controls.FORM_NUMBER),
            _a("FOCUS Report"),
            _a("FACING PAGE"),
            _a("REGISTRANT IDENTIFICATION"),
            _a("ACCOUNTANT IDENTIFICATION"),
            _a("OATH OR AFFIRMATION"),
            _a("Net Capital"),
            _a("SEC FILE NUMBER"),
            _a("UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
        ],
        confusable_with={
            "us_sec_form_adv": "Form ADV is the investment adviser's registration form; "
            "X-17A-5 is the broker-dealer's financial and operational report",
            "us_auditor_report": "Part III of X-17A-5 CONTAINS an independent auditor's "
            "report, which is why that doctype claims no decisive anchor",
        },
        fields=[
            FieldSpec(
                name="entity_legal_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels={"en": ["NAME OF FIRM", "Name of Broker-Dealer", "NAME OF BROKER-DEALER"]},
                validator="name",
            ),
            FieldSpec(
                name="sec_file_number",
                attribute_key="id.sec_registration_number",
                type="id",
                labels={"en": ["SEC FILE NUMBER", "SEC File Number"]},
                pattern=SEC_FIRM_NUMBER_PATTERN,
                notes="A registered broker-dealer is numbered 8-NNNNN. Shape only.",
            ),
            FieldSpec(
                name="crd_number",
                attribute_key="id.crd_number",
                type="id",
                labels={"en": ["FIRM ID NO.", "CRD Number"]},
                pattern=r"\b\d{4,7}\b",
                notes="Unchecked sequence number; the pattern only rejects a wrong binding.",
            ),
            _address_field(
                name="principal_place_of_business",
                key="address.registered",
                labels=["ADDRESS OF PRINCIPAL PLACE OF BUSINESS", "No. and Street"],
            ),
            _period_covered_field(["FILING FOR THE PERIOD BEGINNING", "AND ENDING"]),
            FieldSpec(
                name="auditor",
                attribute_key="entity.auditor",
                type="name",
                labels={"en": ["INDEPENDENT PUBLIC ACCOUNTANT", "Name of Accountant"]},
                validator="name",
            ),
            FieldSpec(
                name="net_capital",
                attribute_key="entity.net_capital",
                type="number",
                labels={"en": ["Net Capital", "Excess Net Capital", "Total Net Capital"]},
                pattern=CURRENCY_PATTERN,
                validator="amount",
                locators=["table", "kv", "label", "regex"],
            ),
            FieldSpec(
                name="officer_signatory",
                attribute_key="ownership.authorized_signer",
                type="name",
                pii=True,
                labels={"en": ["OATH OR AFFIRMATION", "Signature", "Title"]},
                validator="name",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_auditor_report",
        label="Report of Independent Registered Public Accounting Firm (PCAOB audit opinion)",
        country="US",
        category=Category.financial,
        issuing_authority="PCAOB-registered independent audit firm",
        applies_to="corporate",
        # This doctype declares NO decisive anchor, deliberately.
        #
        # "REPORT OF INDEPENDENT REGISTERED PUBLIC ACCOUNTING FIRM" is a title the PCAOB
        # controls (AS 3101) and it is genuinely US-specific — the ISA/CAS wording used in
        # Canada and most of the world is "Independent Auditor's Report". By the module
        # docstring's test it therefore looks like a textbook decisive anchor.
        #
        # It is not one, because it is not the title of a *document a bank receives on its
        # own*. It is a section heading inside a larger filing: measured on real EDGAR
        # documents, the string occurs twice inside Apple's FY2025 10-K and four times inside
        # Novartis' FY2025 20-F, and Part III of Form X-17A-5 embeds one too. Making it
        # decisive would put a decisive claim for THIS doctype on every annual report in the
        # pack, the conclusive-L1 route would decline on all of them, and the cost would be
        # paid by the most important doctypes here to win a document that is usually a
        # fragment of one of them.
        #
        # The vocabulary below is distinctive enough to carry a standalone audit report
        # lexically. Where it is not, the service abstains and a human looks — which is the
        # correct trade, because the alternative is a confident "audit report" verdict on a
        # 10-K.
        anchors=[
            _a("Report of Independent Registered Public Accounting Firm"),
            _a("Opinion on the Financial Statements"),
            _a("Opinion on Internal Control over Financial Reporting"),
            _a("Basis for Opinion"),
            _a("Critical Audit Matters"),
            _a("Public Company Accounting Oversight Board"),
            _a("PCAOB"),
            _a("We have served as the Company's auditor since"),
            _a("present fairly, in all material respects"),
            _a(
                "in conformity with accounting principles generally accepted in the United States"
            ),
        ],
        confusable_with={
            "us_sec_10k": "the 10-K CONTAINS this report; the standalone deliverable is the "
            "opinion and the financial statements without the cover page, Item "
            "numbering or the aggregate-market-value legend",
            "us_focus_x17a5": "Part III of the broker-dealer's annual report embeds the same "
            "opinion behind a FACING PAGE and an OATH OR AFFIRMATION",
        },
        negative_anchors=[
            "aggregate market value of the voting and non-voting",
            "Independent Auditor's Report",
            "Canadian generally accepted auditing standards",
            "International Standards on Auditing",
        ],
        fields=[
            FieldSpec(
                name="entity_legal_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels={
                    "en": [
                        "To the Shareholders and the Board of Directors of",
                        "To the Board of Directors and Stockholders of",
                        "We have audited the accompanying",
                    ]
                },
                validator="name",
            ),
            FieldSpec(
                name="auditor",
                attribute_key="entity.auditor",
                type="name",
                required=True,
                labels={"en": ["PCAOB ID", "Auditor Name", "Signed"]},
                validator="name",
                notes="The firm signs at the foot of the report; the signature line is the "
                "only place the name appears on a standalone copy.",
            ),
            FieldSpec(
                name="pcaob_firm_id",
                attribute_key="id.pcaob_firm_id",
                type="id",
                labels={"en": ["PCAOB ID", "PCAOB Firm ID"]},
                pattern=r"\b\d{1,4}\b",
                notes="The PCAOB assigns short sequence numbers with no check digit. The "
                "pattern exists only to stop a date binding to the label.",
            ),
            FieldSpec(
                name="auditor_since",
                attribute_key="entity.auditor_since",
                type="number",
                labels={"en": ["We have served as the Company's auditor since"]},
                pattern=r"\b(19|20)\d{2}\b",
                notes="Auditor tenure has been a required disclosure since AS 3101 took "
                "effect for FY2017 audits; it is absent from older reports.",
            ),
            FieldSpec(
                name="opinion_date",
                attribute_key="doc.issue_date",
                type="date",
                labels={"en": ["Date", "Dated"]},
                validator="generic_date",
            ),
            _address_field(
                name="auditor_location",
                key="address.mailing",
                labels=["City", "Location"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="us_secretary_certificate",
        label="US Secretary's Certificate / Certificate of Incumbency",
        country="US",
        category=Category.corporate,
        issuing_authority="Corporate secretary of the entity (private instrument)",
        applies_to="corporate",
        # Also declares NO decisive anchor, for the reason the module docstring gives.
        # "CERTIFICATE OF INCUMBENCY" and "SECRETARY'S CERTIFICATE" are document-CLASS names:
        # a Cayman, BVI, Hong Kong or Canadian corporate secretary issues a document with the
        # identical title, and declaring either decisive on a US doctype is precisely the
        # us_green_card / ca_pr_card failure the pack has already paid for once.
        #
        # The two documents are modelled as one doctype rather than two because in practice
        # they are one document: the same certificate names the officers, states their titles,
        # attaches their specimen signatures and certifies the resolutions they act under.
        # Splitting them would create a confusable pair with no term that separates them,
        # which is a worse answer than a slightly broad label.
        anchors=[
            _a("SECRETARY'S CERTIFICATE"),
            _a("CERTIFICATE OF INCUMBENCY"),
            _a("INCUMBENCY CERTIFICATE"),
            _a("duly elected and qualified"),
            _a("the undersigned, being the duly elected"),
            _a("do hereby certify that the following persons"),
            _a("hold the offices set forth opposite their respective names"),
            _a("specimen signature"),
            _a("the foregoing resolutions have not been amended, modified or rescinded"),
            _a("IN WITNESS WHEREOF, the undersigned has executed this certificate"),
        ],
        confusable_with={
            "us_bylaws": "the bylaws create the offices; the certificate names who currently "
            "holds them on a stated date",
            "in_board_resolution": "the Indian instrument is headed CERTIFIED TRUE COPY OF "
            "THE RESOLUTION, cites the Companies Act and carries DINs",
        },
        negative_anchors=[
            "CERTIFIED TRUE COPY OF THE RESOLUTION",
            "Ministry of Corporate Affairs",
            "Companies Act, 2013",
            "DIN",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="state_of_incorporation",
                attribute_key="entity.jurisdiction",
                type="string",
                labels={
                    "en": ["a corporation organized under the laws of", "State of Incorporation"]
                },
            ),
            FieldSpec(
                name="certifying_secretary",
                attribute_key="ownership.authorized_signer",
                type="name",
                required=True,
                pii=True,
                labels={"en": ["Secretary", "Assistant Secretary", "the undersigned"]},
                validator="name",
            ),
            FieldSpec(
                name="officers",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels={"en": ["Name", "Officer", "Title", "Office"]},
                validator="name",
                locators=["table", "kv", "label"],
                notes="The officer/title/specimen-signature grid is the substance of the "
                "document, so the table locator runs before any label match.",
            ),
            FieldSpec(
                name="certificate_date",
                attribute_key="doc.issue_date",
                type="date",
                required=True,
                labels={"en": ["Dated", "as of", "this day of", "Date"]},
                validator="generic_date",
                notes="An incumbency certificate is only evidence as of its date; a bank "
                "will normally reject one older than its own staleness policy, so a missing "
                "date has to surface rather than default.",
            ),
        ],
        handling="Carries officers' names and specimen signatures — personal data, and "
        "signature images should not be retained beyond the verification decision.",
    ),
)

#: Fast lookup used by the tests and by callers that already hold a doctype id.
DOCTYPES_BY_ID: dict[str, DocTypeSpec] = {spec.doctype_id: spec for spec in SPECS}


def specs() -> tuple[DocTypeSpec, ...]:
    """Return every US :class:`~dce.models.DocTypeSpec` in this pack."""
    return SPECS


if _loader is not None:  # pragma: no cover - trivially exercised by importing the pack
    _loader.register_all(list(SPECS))


def _load_sibling_packs() -> None:
    """Import the other North-American packs, once this one has registered.

    The three packs cross-reference each other in ``confusable_with`` — a US green card and
    a Canadian PR card genuinely are confusable — and :func:`validate_registry` requires
    those targets to be registered, so the three have to load together however a caller
    reaches them. This runs *after* registration, and goes through ``import_module`` rather
    than an ``import`` statement for two reasons: a sibling that is still initialising (the
    cycle any of the three can start) is never dereferenced, and an autofixing linter cannot
    mistake a deliberate side-effect import for dead code.
    """
    for module_name in ("dce.registry.canada", "dce.registry.mexico"):
        import_module(module_name)


_load_sibling_packs()
