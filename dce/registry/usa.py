"""United States doctype pack — 35 :class:`~dce.models.DocTypeSpec` entries.

This module is **data**. It is the knowledge the classifier and the extraction resolver run
on, so every string here is meant to be something that actually appears on the document.

Conventions honoured by this pack (and by its siblings ``canada`` / ``mexico``):

* **Decisive anchors stay distinguishing.** A decisive anchor carries ``fuse_weight_anchor``
  (3.0) on its own, so this pack never lets two doctypes claim the same one. Shared issuing
  headers — "Internal Revenue Service", "Department of Homeland Security", "Secretary of
  State" — appear on many documents and are therefore **non-decisive**; what is decisive is
  the part that only ever appears on one document: the form number, the OMB control number,
  or the form's full title.
* **Zone-restricted anchors.** A generic title such as "IDENTIFICATION CARD" is decisive
  only when it is found in the title zone, which is what ``Anchor.zone`` is for. The loader
  also *requires* a zone for any short single-word decisive anchor.
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

from dce.models import Anchor, Category, DocTypeSpec, FieldSpec, Zone

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


# ---------------------------------------------------------------------------
# Small builders — the pack is data, these only remove repetition
# ---------------------------------------------------------------------------
def _a(
    text: str,
    *,
    lang: str = "en",
    decisive: bool = False,
    zone: Zone | None = None,
) -> Anchor:
    """Build an :class:`~dce.models.Anchor`.

    Args:
        text: Verbatim string as it is printed on the document.
        lang: Language tag of the string ("en" throughout this pack).
        decisive: True only when the string alone is near-proof of the doctype.
        zone: Restrict the match to a layout zone (used for generic titles).

    Returns:
        The anchor.
    """
    return Anchor(text=text, lang=lang, decisive=decisive, zone=zone)


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
            _a("P<USA", decisive=True),
            _a("United States of America", zone=Zone.title),
            _a("PASSPORT", zone=Zone.title),
            _a("Department of State"),
            _a("Authority"),
            _a("Place of Birth"),
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
            _a("PASSPORT CARD", decisive=True),
            _a("I<USA", decisive=True),
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
            _a("DRIVER LICENSE", decisive=True, zone=Zone.title),
            _a("DRIVER'S LICENSE", decisive=True, zone=Zone.title),
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
            "us_real_id": "REAL ID is a star marking on a licence or ID card, not a "
            "separate credential",
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
            _a("IDENTIFICATION CARD", decisive=True, zone=Zone.title),
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
        ],
    ),
    DocTypeSpec(
        doctype_id="us_real_id",
        label="US REAL ID-compliant Licence or Identification Card",
        country="US",
        category=Category.identity,
        issuing_authority="State department of motor vehicles (REAL ID Act of 2005)",
        officially_valid=True,
        anchors=[
            _a("REAL ID", decisive=True, zone=Zone.title),
            _a("REAL ID COMPLIANT", decisive=True),
            _a("Federally compliant"),
            _a("DMV"),
        ],
        confusable_with={
            "us_drivers_license": "REAL ID compliance is a marking on a licence — when the "
            "card also says DRIVER LICENSE, classify it as a licence "
            "and keep REAL ID as an attribute",
            "us_state_id": "the same, for a non-driver card",
        },
        negative_anchors=["FEDERAL LIMITS APPLY", "NOT FOR FEDERAL IDENTIFICATION"],
        fields=[
            FieldSpec(
                name="real_id_compliant",
                attribute_key="doc.real_id_compliant",
                type="bool",
                labels={"en": ["REAL ID", "Federally compliant"]},
                locators=["mark", "label", "regex"],
                notes="Compliance is usually signalled by a gold or black star in the top "
                "right corner rather than by printed text, and that star is a figure, "
                "not a selection mark — a negative result here is NOT proof of "
                "non-compliance. 'FEDERAL LIMITS APPLY' is the reliable negative.",
            ),
            _name_field(),
            _dob_field(),
            _address_field(required=True),
            _expiry_date_field(),
        ],
        handling="A REAL ID card is still a licence or a state ID. Prefer the concrete "
        "doctype and record compliance as an attribute.",
    ),
    DocTypeSpec(
        doctype_id="us_ssn_card",
        label="US Social Security Card",
        country="US",
        category=Category.identity,
        issuing_authority="Social Security Administration (SSA)",
        anchors=[
            _a("SOCIAL SECURITY ADMINISTRATION", decisive=True, zone=Zone.title),
            _a("THIS NUMBER HAS BEEN ESTABLISHED FOR", decisive=True),
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
            _a("CP 565", decisive=True),
            _a("We assigned you an Individual Taxpayer Identification Number", decisive=True),
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
            _a("PERMANENT RESIDENT CARD", decisive=True),
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
            _a("EMPLOYMENT AUTHORIZATION CARD", decisive=True),
            _a("EMPLOYMENT AUTHORIZATION DOCUMENT", decisive=True),
            _a("I-766", decisive=True),
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
            _a("CERTIFICATE OF LIVE BIRTH", decisive=True),
            _a("CERTIFICATION OF BIRTH", decisive=True),
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
            _a("CERTIFICATE OF NATURALIZATION", decisive=True),
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
            _a("CERTIFICATE OF CITIZENSHIP", decisive=True),
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
            _a("UNIFORMED SERVICES IDENTIFICATION CARD", decisive=True),
            _a("COMMON ACCESS CARD", decisive=True),
            _a("Geneva Conventions Identification Card", decisive=True),
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
            _a("Request for Taxpayer Identification Number and Certification", decisive=True),
            _a("Form W-9", decisive=True),
            _a("Go to www.irs.gov/FormW9", decisive=True),
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
                decisive=True,
            ),
            _a("For use by individuals. Entities must use Form W-8BEN-E", decisive=True),
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
                decisive=True,
            ),
            _a("For use by entities. Individuals must use Form W-8BEN", decisive=True),
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
            _a("OMB No. 1545-0008", decisive=True),
            _a("Wage and Tax Statement", decisive=True),
            _a("Form W-2", decisive=True),
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
            _a("1099-NEC", decisive=True),
            _a("1099-MISC", decisive=True),
            _a("1099-INT", decisive=True),
            _a("1099-DIV", decisive=True),
            _a("OMB No. 1545-0116", decisive=True),
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
            _a("U.S. Individual Income Tax Return", decisive=True),
            _a("OMB No. 1545-0074", decisive=True),
            _a("Form 1040", decisive=True),
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
            _a("CP 575", decisive=True),
            _a("We assigned you an Employer Identification Number", decisive=True),
            _a("Thank you for applying for an Employer Identification Number", decisive=True),
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
            _a("EIN Verification Letter", decisive=True),
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
            _a("ARTICLES OF INCORPORATION", decisive=True),
            # NOT decisive: "Certificate of Incorporation" is the exact registered title of
            # the Indian MCA's incorporation certificate, which has the stronger claim on
            # the string. Delaware and New York do title their filing that way, so the
            # anchor is kept — it contributes lexically and the Secretary of State
            # vocabulary below carries the rest.
            _a("CERTIFICATE OF INCORPORATION"),
            _a("Secretary of State"),
            _a("Registered Agent"),
            _a("Authorized Shares"),
            _a("Incorporator"),
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
            _a("ARTICLES OF ORGANIZATION", decisive=True),
            _a("CERTIFICATE OF FORMATION", decisive=True),
            _a("Limited Liability Company"),
            _a("Secretary of State"),
            _a("Registered Agent"),
            _a("Organizer"),
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
            _a("OPERATING AGREEMENT", decisive=True),
            _a("LIMITED LIABILITY COMPANY OPERATING AGREEMENT", decisive=True),
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
            _a("BYLAWS", decisive=True, zone=Zone.title),
            _a("AMENDED AND RESTATED BYLAWS", decisive=True),
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
            _a("CERTIFICATE OF GOOD STANDING", decisive=True),
            _a("CERTIFICATE OF EXISTENCE", decisive=True),
            _a("is in good standing", decisive=True),
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
            _a("BENEFICIAL OWNERSHIP INFORMATION REPORT", decisive=True),
            _a("Corporate Transparency Act", decisive=True),
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
            _a("BOIR SUBMISSION CONFIRMATION", decisive=True),
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
            _a("RESIDENTIAL LEASE AGREEMENT", decisive=True),
            _a("LEASE AGREEMENT", decisive=True, zone=Zone.title),
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
            _a("MORTGAGE STATEMENT", decisive=True),
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
            _a("EARNINGS STATEMENT", decisive=True),
            _a("STATEMENT OF EARNINGS", decisive=True),
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
            _a("VOIDED CHECK", decisive=True),
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
            _a("TRUST AGREEMENT", decisive=True, zone=Zone.title),
            _a("DECLARATION OF TRUST", decisive=True),
            _a("REVOCABLE LIVING TRUST", decisive=True),
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
