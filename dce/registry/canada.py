"""Canada doctype pack — 25 :class:`~dce.models.DocTypeSpec` entries.

Canadian federal documents are bilingual by law, and OCR sees both halves. Every federal
doctype here therefore carries French anchors alongside the English ones, and the French
string is often the *better* discriminator: "PERMANENT RESIDENT CARD" is printed on the US
green card too, but "CARTE DE RÉSIDENT PERMANENT" is only ever printed on the Canadian one.

The conventions are the ones documented in :mod:`dce.registry.usa`:

* decisive anchors stay unique across the registry — a shared issuing header such as
  "Canada Revenue Agency / Agence du revenu du Canada" appears on the notice of assessment,
  the T4, the T1 and the business-number letter alike, so it is deliberately **not**
  decisive; the form's own bilingual title is;
* validators are declared before they are used, and an identifier without a published
  checksum (a PR card number, a provincial licence number) gets a note rather than an
  invented regex;
* ``officially_valid`` marks a credential FINTRAC accepts as government-issued photo
  identification for identity verification. Provincial health cards are excluded on purpose
  — several provinces prohibit their use for identification.

Accents matter here: the classifier folds diacritics before matching (``dce.normalize``
produces a skeleton form), so "RÉSIDENT" written here still matches an OCR read of
"RESIDENT". Anchors are stored accented and NFC-normalised, which is what the registry
loader enforces.
"""

from __future__ import annotations

from importlib import import_module

from dce.models import Anchor, Category, DocTypeSpec, FieldSpec, Zone

try:  # pragma: no cover - the loader is authored alongside this pack
    from dce.registry import loader as _loader
except ImportError:  # pragma: no cover - the pack stays importable on its own
    _loader = None


# ---------------------------------------------------------------------------
# Namespace declarations — see the long note in dce.registry.usa for why a pack
# declares these rather than editing the loader. The shared entries are repeated
# verbatim in each North-American pack so that any one of them can be imported on
# its own; re-declaring the same name with the same text is a no-op.
# ---------------------------------------------------------------------------
ATTRIBUTE_KEY_EXTENSIONS: dict[str, str] = {
    "id.sin": "Canadian Social Insurance Number",
    "id.business_number": "CRA Business Number / BN15 program account",
    "id.pr_card_number": "Permanent Resident Card number",
    "id.uci": "IRCC Unique Client Identifier (UCI / Client ID)",
    "id.health_card_number": "Provincial health insurance number",
    "id.nexus_number": "NEXUS / trusted-traveller membership number",
    "id.indian_status_number": "Indian Register number on a status card",
    "id.driver_license": "Driver licence number (US state / Canadian provincial)",
    "id.passport_number": "Passport number",
    "account.statement_period": "Period covered by an account statement",
    "account.amount_due": "Amount payable on a statement or bill",
    "account.transit_number": "Canadian branch transit number",
    "property.roll_number": "Municipal assessment roll number",
    "property.assessed_value": "Assessed value of a property",
    "doc.mrz": "Machine-readable zone exactly as printed (ICAO 9303)",
    "doc.tax_year": "Tax year a return or information return covers",
    "doc.issuing_state": "State / province that issued the document",
    "doc.immigration_category": "Immigration category code printed on an immigration document",
    "entity.jurisdiction": "State / province / country of incorporation or organisation",
    "entity.status": "Registry status of the entity (good standing, active, dissolved)",
}

#: ``name -> what the validator must enforce``. ``sin_luhn`` is Canada's; the rest are the
#: shared North-American value validators, declared identically in every NA pack.
VALIDATOR_EXTENSIONS: dict[str, str] = {
    "sin_luhn": (
        "Canadian Social Insurance Number: 9 digits with a Luhn check digit. A SIN "
        "beginning with 9 is a temporary-resident SIN — valid, but flag it."
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

    Declares only what the registry does not already know — see
    :func:`dce.registry.usa._declare_namespace`.
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
# ---------------------------------------------------------------------------
#: SIN, printed 3-3-3 with spaces or hyphens.
SIN_PATTERN = r"\b\d{3}[-\s]?\d{3}[-\s]?\d{3}\b"
#: CRA Business Number: 9 digits, optionally followed by a program account (RT/RP/RC/RM/RZ).
BN_PATTERN = r"\b\d{9}\s?(?:RC|RM|RP|RT|RZ)\s?\d{4}\b|\b\d{9}\b"
#: ICAO 9303 TD3 first line of a Canadian passport.
MRZ_TD3_CAN = r"P[<K]CAN[A-Z0-9<]{5,}"
#: Canadian passport number: two letters followed by six digits.
PASSPORT_NUMBER_PATTERN = r"\b[A-Z]{2}\d{6}\b"
#: IRCC UCI, printed either as NNNN-NNNN or NN-NNNN-NNNN.
UCI_PATTERN = r"\b\d{2,4}-\d{4}-\d{4}\b|\b\d{4}-\d{4}\b"
#: RAMQ (Québec) health number: four letters then eight digits.
RAMQ_PATTERN = r"\b[A-Z]{4}\d{8}\b"
#: OHIP (Ontario) health number: ten digits plus a two-letter version code.
OHIP_PATTERN = r"\b\d{4}-?\d{3}-?\d{3}-?[A-Z]{2}\b"
CURRENCY_PATTERN = r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?"


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------
def _a(
    text: str,
    *,
    lang: str = "en",
    decisive: bool = False,
    zone: Zone | None = None,
) -> Anchor:
    """Build an :class:`~dce.models.Anchor` (``lang`` is "en" or "fr" in this pack)."""
    return Anchor(text=text, lang=lang, decisive=decisive, zone=zone)


def _fr(text: str, *, decisive: bool = False, zone: Zone | None = None) -> Anchor:
    """Build a French anchor — the half of a bilingual header OCR often reads best."""
    return Anchor(text=text, lang="fr", decisive=decisive, zone=zone)


def _bilingual(en: list[str], fr: list[str]) -> dict[str, list[str]]:
    """Label map for a bilingual form."""
    return {"en": en, "fr": fr}


def _name_field(
    *,
    name: str = "full_name",
    key: str = "identity.full_name",
    required: bool = True,
    en: list[str] | None = None,
    fr: list[str] | None = None,
) -> FieldSpec:
    """A person's printed name."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="name",
        required=required,
        pii=True,
        labels=_bilingual(
            en or ["Name", "Surname", "Given Name", "Last Name", "First Name"],
            fr or ["Nom", "Prénom", "Nom de famille"],
        ),
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
        labels=_bilingual(
            ["Date of Birth", "DOB", "Birth Date"],
            ["Date de naissance", "Né(e) le"],
        ),
        validator="generic_date",
    )


def _sex_field() -> FieldSpec:
    """Sex / gender marker."""
    return FieldSpec(
        name="sex",
        attribute_key="identity.sex",
        type="string",
        pii=True,
        labels=_bilingual(["Sex", "Gender"], ["Sexe"]),
        pattern=r"^[MFXmfx]$",
    )


def _address_field(
    *,
    name: str = "address",
    key: str = "address.residential",
    required: bool = False,
    en: list[str] | None = None,
    fr: list[str] | None = None,
) -> FieldSpec:
    """A postal address."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="address",
        required=required,
        pii=True,
        labels=_bilingual(
            en or ["Address", "Mailing Address", "Street Address"],
            fr or ["Adresse", "Adresse postale"],
        ),
        validator="address",
    )


def _issue_date_field(*, required: bool = False) -> FieldSpec:
    """Document issue date."""
    return FieldSpec(
        name="issue_date",
        attribute_key="doc.issue_date",
        type="date",
        required=required,
        labels=_bilingual(
            ["Date of Issue", "Issued", "Issue Date"],
            ["Date de délivrance", "Délivré le"],
        ),
        validator="generic_date",
    )


def _expiry_date_field(*, required: bool = False) -> FieldSpec:
    """Document expiry date."""
    return FieldSpec(
        name="expiry_date",
        attribute_key="doc.expiry_date",
        type="date",
        required=required,
        labels=_bilingual(
            ["Expiry Date", "Date of Expiry", "Expires"],
            ["Date d'expiration", "Expire le"],
        ),
        validator="generic_date",
    )


def _amount_field(
    name: str,
    *,
    key: str,
    en: list[str],
    fr: list[str],
    required: bool = False,
) -> FieldSpec:
    """A currency amount pulled from a labelled box."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="number",
        required=required,
        labels=_bilingual(en, fr),
        pattern=CURRENCY_PATTERN,
        validator="amount",
        locators=["table", "kv", "label", "regex"],
    )


def _sin_field(*, required: bool = True) -> FieldSpec:
    """Canadian Social Insurance Number."""
    return FieldSpec(
        name="sin",
        attribute_key="id.sin",
        type="id",
        required=required,
        pii=True,
        labels=_bilingual(
            ["Social Insurance Number", "SIN"],
            ["Numéro d'assurance sociale", "NAS"],
        ),
        pattern=SIN_PATTERN,
        validator="sin_luhn",
    )


def _entity_name_field(*, required: bool = True) -> FieldSpec:
    """Legal name of a corporation or firm."""
    return FieldSpec(
        name="entity_legal_name",
        attribute_key="entity.legal_name",
        type="name",
        required=required,
        labels=_bilingual(
            ["Corporate Name", "Name of Corporation", "Legal Name", "Business Name"],
            ["Dénomination sociale", "Nom de la société", "Raison sociale"],
        ),
        validator="name",
    )


def _mrz_fields(td: str) -> list[FieldSpec]:
    """MRZ block plus the person fields it decodes. See :func:`dce.registry.usa._mrz_fields`."""
    src = ["mrz", "kv", "label"]
    return [
        FieldSpec(
            name="machine_readable_zone",
            attribute_key="doc.mrz",
            type="string",
            pii=True,
            validator=td,
            locators=["mrz", "regex"],
            notes="Captured verbatim; its check digits are what make the decoded fields "
            "checksum-verified rather than merely read.",
        ),
        FieldSpec(
            name="surname",
            attribute_key="identity.surname",
            type="name",
            required=True,
            pii=True,
            labels=_bilingual(["Surname", "Last Name"], ["Nom", "Nom de famille"]),
            validator="name",
            locators=src,
        ),
        FieldSpec(
            name="given_names",
            attribute_key="identity.given_names",
            type="name",
            required=True,
            pii=True,
            labels=_bilingual(["Given Names", "First Name"], ["Prénoms", "Prénom"]),
            validator="name",
            locators=src,
        ),
        FieldSpec(
            name="nationality",
            attribute_key="identity.nationality",
            type="string",
            labels=_bilingual(["Nationality"], ["Nationalité"]),
            locators=src,
        ),
        FieldSpec(
            name="date_of_birth",
            attribute_key="identity.date_of_birth",
            type="date",
            required=True,
            pii=True,
            labels=_bilingual(["Date of Birth"], ["Date de naissance"]),
            validator="generic_date",
            locators=src,
        ),
        FieldSpec(
            name="sex",
            attribute_key="identity.sex",
            type="string",
            pii=True,
            labels=_bilingual(["Sex"], ["Sexe"]),
            pattern=r"^[MFXmfx]$",
            locators=src,
        ),
        FieldSpec(
            name="expiry_date",
            attribute_key="doc.expiry_date",
            type="date",
            required=True,
            labels=_bilingual(["Date of Expiry"], ["Date d'expiration"]),
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
        doctype_id="ca_passport",
        label="Canadian Passport",
        country="CA",
        category=Category.identity,
        issuing_authority="Immigration, Refugees and Citizenship Canada (IRCC) — Passport Program",
        officially_valid=True,
        handling="Under PIPEDA collect only the identity fields the verification needs; do not "
        "retain the data page image by default.",
        anchors=[
            _a("P<CAN", decisive=True),
            _a("PASSPORT", zone=Zone.title),
            _fr("PASSEPORT", zone=Zone.title),
            _a("Government of Canada"),
            _fr("Gouvernement du Canada"),
            _fr("Autorité"),
            _a("Place of birth"),
        ],
        id_patterns=[MRZ_TD3_CAN],
        confusable_with={
            "us_passport": "the US book's MRZ starts P<USA and its data page is English "
            "with French and Spanish field labels; the Canadian page is "
            "English and French only",
        },
        negative_anchors=["P<USA", "P<MEX", "PASAPORTE"],
        fields=[
            *_mrz_fields("mrz_td3"),
            FieldSpec(
                name="passport_number",
                attribute_key="id.passport_number",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Passport No.", "Passport Number"], ["No de passeport", "Numéro de passeport"]
                ),
                pattern=PASSPORT_NUMBER_PATTERN,
                locators=["mrz", "kv", "label"],
                notes="Canadian passport numbers are two letters followed by six digits. "
                "The printed number carries no check digit — only the MRZ does.",
            ),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                pii=True,
                labels=_bilingual(["Place of birth"], ["Lieu de naissance"]),
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_drivers_license",
        label="Canadian Driver's Licence (provincial)",
        country="CA",
        category=Category.identity,
        issuing_authority="Provincial or territorial licensing authority",
        officially_valid=True,
        handling="Several provinces restrict retaining licence images and magnetic-stripe data. "
        "Keep the extracted fields, not the scan.",
        anchors=[
            _a("DRIVER'S LICENCE", decisive=True),
            _fr("PERMIS DE CONDUIRE", decisive=True),
            _a("Class"),
            _fr("Classe"),
            _a("Restrictions"),
            _a("Conditions"),
        ],
        confusable_with={
            "us_drivers_license": "US cards spell LICENSE and name a state DMV; Canadian "
            "cards spell LICENCE and name a province",
            "ca_provincial_photo_id": "a photo card grants no driving privileges and shows "
            "no class",
        },
        negative_anchors=["DRIVER LICENSE", "IDENTIFICATION CARD", "DMV"],
        fields=[
            FieldSpec(
                name="license_number",
                attribute_key="id.driver_license",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Licence Number", "DL", "No."], ["Numéro de permis", "No du permis"]
                ),
                notes="Every province defines its own licence-number scheme (Ontario "
                "encodes the surname and date of birth, Québec does not) and none "
                "publishes a check digit usable here. No pattern is asserted.",
            ),
            _name_field(),
            _dob_field(),
            _sex_field(),
            _address_field(required=True),
            _issue_date_field(),
            _expiry_date_field(required=True),
            FieldSpec(
                name="province",
                attribute_key="doc.issuing_state",
                type="string",
                labels=_bilingual(["Province"], ["Province"]),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_provincial_photo_id",
        label="Canadian Provincial Photo Identification Card (non-driver)",
        country="CA",
        category=Category.identity,
        issuing_authority="Provincial or territorial licensing authority",
        officially_valid=True,
        handling="Same provincial retention limits as a driver's licence: keep the fields, not the "
        "scan.",
        anchors=[
            _a("ONTARIO PHOTO CARD", decisive=True),
            _a("ALBERTA IDENTIFICATION CARD", decisive=True),
            _a("MANITOBA IDENTIFICATION CARD", decisive=True),
            _fr("CARTE PHOTO"),
            _a("Photo Card"),
            _a("Identification Card"),
        ],
        confusable_with={
            "ca_drivers_license": "the photo card shows no licence class and no driving conditions",
            "us_state_id": "a US card names a state DMV; these name a province",
        },
        negative_anchors=["DRIVER'S LICENCE", "PERMIS DE CONDUIRE", "DMV"],
        fields=[
            FieldSpec(
                name="id_number",
                attribute_key="id.driver_license",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(["Card Number", "No."], ["Numéro de la carte"]),
                notes="Provincial photo cards are numbered from the same series as driver's "
                "licences in most provinces, which is why they share an attribute key.",
            ),
            _name_field(),
            _dob_field(),
            _sex_field(),
            _address_field(required=True),
            _expiry_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_pr_card",
        label="Canadian Permanent Resident Card",
        country="CA",
        category=Category.identity,
        issuing_authority="Immigration, Refugees and Citizenship Canada (IRCC)",
        officially_valid=True,
        handling="Card expiry and permanent-resident status are different facts — an expired card "
        "does not mean status was lost, so never derive a status decision from the expiry "
        "alone.",
        anchors=[
            _fr("CARTE DE RÉSIDENT PERMANENT", decisive=True),
            _fr("RÉSIDENT PERMANENT", decisive=True),
            _a("PERMANENT RESIDENT CARD"),
            _a("IRCC"),
            _a("Citizenship and Immigration Canada"),
            _a("Sponsor"),
        ],
        confusable_with={
            "us_green_card": "the US I-551 is titled PERMANENT RESIDENT CARD in English "
            "only and names USCIS; the Canadian card is bilingual and "
            "names IRCC",
            "ca_citizenship_certificate": "a PR card has an expiry date and says RÉSIDENT "
            "PERMANENT; a citizenship certificate never "
            "expires and says CITOYENNETÉ / CITIZENSHIP",
        },
        negative_anchors=["USCIS", "United States of America", "CITIZENSHIP"],
        fields=[
            *_mrz_fields("mrz_td1"),
            FieldSpec(
                name="pr_card_number",
                attribute_key="id.pr_card_number",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Card No.", "Document No."], ["No de la carte", "No du document"]
                ),
                notes="IRCC does not publish the PR card's number format or any check "
                "digit, and the machine-readable zone's document-code prefix is not "
                "documented either — the MRZ locator parses the zone structurally "
                "rather than by prefix. No pattern is asserted.",
            ),
            FieldSpec(
                name="uci",
                attribute_key="id.uci",
                type="id",
                pii=True,
                labels=_bilingual(["UCI", "Client ID"], ["IUC", "ID client"]),
                pattern=UCI_PATTERN,
                notes="The UCI is 8 or 10 digits, printed in hyphenated groups. No check "
                "digit is published.",
            ),
            FieldSpec(
                name="category",
                attribute_key="doc.immigration_category",
                type="string",
                labels=_bilingual(["Category"], ["Catégorie"]),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_copr",
        label="Confirmation of Permanent Residence (IMM 5292 / IMM 5688)",
        country="CA",
        category=Category.identity,
        issuing_authority="Immigration, Refugees and Citizenship Canada (IRCC)",
        anchors=[
            _a("CONFIRMATION OF PERMANENT RESIDENCE", decisive=True),
            _fr("CONFIRMATION DE RÉSIDENCE PERMANENTE", decisive=True),
            _a("IMM 5292", decisive=True),
            _a("IMM 5688", decisive=True),
            _a("Client ID"),
            _a("Date of landing"),
        ],
        confusable_with={
            "ca_pr_card": "the CoPR is the paper landing document issued once; the card is "
            "the renewable travel credential",
        },
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="uci",
                attribute_key="id.uci",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(["Client ID", "UCI"], ["ID client", "IUC"]),
                pattern=UCI_PATTERN,
            ),
            FieldSpec(
                name="landing_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Date of landing", "Date of confirmation"], ["Date d'établissement"]
                ),
                validator="generic_date",
            ),
            FieldSpec(
                name="category",
                attribute_key="doc.immigration_category",
                type="string",
                labels=_bilingual(["Category", "Immigration category"], ["Catégorie"]),
            ),
            _address_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_citizenship_certificate",
        label="Canadian Citizenship Certificate",
        country="CA",
        category=Category.identity,
        issuing_authority="Immigration, Refugees and Citizenship Canada (IRCC)",
        officially_valid=True,
        handling="A citizenship certificate does not expire; do not derive a re-verification date "
        "from any date printed on it.",
        anchors=[
            _a("CERTIFICATE OF CANADIAN CITIZENSHIP", decisive=True),
            _fr("CERTIFICAT DE CITOYENNETÉ CANADIENNE", decisive=True),
            _a("Citizenship certificate"),
            _fr("Certificat de citoyenneté"),
            _a("IRCC"),
        ],
        confusable_with={
            "ca_pr_card": "a citizenship certificate carries no expiry and says "
            "CITIZENSHIP / CITOYENNETÉ; a PR card expires and says RÉSIDENT "
            "PERMANENT",
            "us_citizenship_cert": "the US N-560 names USCIS and the Department of "
            "Homeland Security",
        },
        negative_anchors=["RÉSIDENT PERMANENT", "USCIS"],
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
                labels=_bilingual(["Certificate No."], ["No du certificat"]),
                notes="IRCC certificate numbering has changed between issues; no format is "
                "asserted.",
            ),
            FieldSpec(
                name="uci",
                attribute_key="id.uci",
                type="id",
                pii=True,
                labels=_bilingual(["UCI", "Client ID"], ["IUC"]),
                pattern=UCI_PATTERN,
            ),
            FieldSpec(
                name="effective_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Effective date of citizenship", "Date"], ["Date d'entrée en vigueur"]
                ),
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_secure_status_card",
        label="Secure Certificate of Indian Status (SCIS)",
        country="CA",
        category=Category.identity,
        issuing_authority="Indigenous Services Canada",
        officially_valid=True,
        anchors=[
            _a("SECURE CERTIFICATE OF INDIAN STATUS", decisive=True),
            _fr("CERTIFICAT SÉCURISÉ DE STATUT D'INDIEN", decisive=True),
            _a("Indigenous Services Canada"),
            _fr("Services aux Autochtones Canada"),
            _a("Registry Number"),
            _fr("Numéro de registre"),
        ],
        confusable_with={
            "ca_provincial_photo_id": "the status card is federal and names Indigenous "
            "Services Canada rather than a province",
        },
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="registry_number",
                attribute_key="id.indian_status_number",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Registry Number", "Registration Number"], ["Numéro de registre"]
                ),
                notes="The Indian Register number is a 10-digit band/family/position "
                "composite. No public check digit exists.",
            ),
            _expiry_date_field(),
        ],
        handling="Indian Register data is sensitive personal information; collect only what "
        "the identity check requires.",
    ),
    DocTypeSpec(
        doctype_id="ca_refugee_protection_doc",
        label="Refugee Protection Claimant Document (IMM 1442)",
        country="CA",
        category=Category.identity,
        issuing_authority="Immigration, Refugees and Citizenship Canada (IRCC) / CBSA",
        anchors=[
            _a("REFUGEE PROTECTION CLAIMANT DOCUMENT", decisive=True),
            _fr("DOCUMENT DU DEMANDEUR D'ASILE", decisive=True),
            _a("IMM 1442", decisive=True),
            _a("Client ID"),
            _a("Conditions"),
        ],
        confusable_with={
            "ca_copr": "the claimant document is issued while a claim is pending; the CoPR "
            "confirms permanent residence has been granted",
        },
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="uci",
                attribute_key="id.uci",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(["Client ID", "UCI"], ["ID client", "IUC"]),
                pattern=UCI_PATTERN,
            ),
            _issue_date_field(),
            _expiry_date_field(),
        ],
        notes="IMM 1442 is the shared form number for several immigration documents "
        "(visitor record, study permit, work permit); the printed title is what "
        "separates them, which is why the title anchors are the decisive ones.",
    ),
    DocTypeSpec(
        doctype_id="ca_health_card",
        label="Canadian Provincial Health Card",
        country="CA",
        category=Category.identity,
        issuing_authority="Provincial health insurance plan",
        anchors=[
            _fr("CARTE D'ASSURANCE MALADIE", decisive=True),
            _fr("RÉGIE DE L'ASSURANCE MALADIE DU QUÉBEC", decisive=True),
            _a("ONTARIO HEALTH CARD", decisive=True),
            _a("Health Card"),
            _fr("Carte santé"),
            _a("OHIP"),
            _a("BC Services Card"),
        ],
        confusable_with={
            "ca_provincial_photo_id": "a health card names a provincial health plan (RAMQ, "
            "OHIP) and carries a health number, not a licence "
            "number",
        },
        fields=[
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="health_number",
                attribute_key="id.health_card_number",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Health Number", "Health Card Number"], ["Numéro d'assurance maladie"]
                ),
                pattern=f"{RAMQ_PATTERN}|{OHIP_PATTERN}",
                notes="Formats are provincial: Québec RAMQ is four letters plus eight "
                "digits, Ontario is ten digits plus a two-letter version code. Other "
                "provinces differ again, so the pattern covers only these two and "
                "must not be treated as exhaustive.",
            ),
            _expiry_date_field(),
        ],
        handling="Several provinces (Ontario under PHIPA, Manitoba, Prince Edward Island) "
        "restrict or prohibit collecting a health number for identification, and "
        "FINTRAC does not accept provincial health cards as identity documents "
        "there. Route to legal review before storing — hence officially_valid is "
        "False for this doctype.",
    ),
    DocTypeSpec(
        doctype_id="ca_nexus",
        label="NEXUS Card (trusted traveller)",
        country="CA",
        category=Category.identity,
        issuing_authority="Canada Border Services Agency (CBSA) with U.S. Customs and "
        "Border Protection",
        officially_valid=True,
        handling="Acceptable as government-issued photo identification, but it evidences programme "
        "membership only — not status, and not address.",
        anchors=[
            _a("NEXUS", decisive=True, zone=Zone.title),
            _a("Trusted Traveler"),
            _a("CBSA"),
            _fr("ASFC"),
            _a("Customs and Border Protection"),
        ],
        confusable_with={
            "ca_pr_card": "a NEXUS card is a border-clearance membership card, not a status "
            "document — it names CBSA and CBP",
        },
        fields=[
            _name_field(),
            _dob_field(),
            FieldSpec(
                name="membership_number",
                attribute_key="id.nexus_number",
                type="id",
                required=True,
                pii=True,
                labels=_bilingual(["Membership Number", "PASSID"], ["Numéro de membre"]),
                notes="The NEXUS PASSID has no published check digit.",
            ),
            _expiry_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_sin_confirmation",
        label="Social Insurance Number Confirmation Letter",
        country="CA",
        category=Category.identity,
        issuing_authority="Service Canada (Employment and Social Development Canada)",
        anchors=[
            _a("CONFIRMATION OF SOCIAL INSURANCE NUMBER", decisive=True),
            _fr("CONFIRMATION DU NUMÉRO D'ASSURANCE SOCIALE", decisive=True),
            _a("Service Canada"),
            _a("Social Insurance Number"),
            _fr("Numéro d'assurance sociale"),
        ],
        id_patterns=[SIN_PATTERN],
        confusable_with={
            "ca_cra_noa": "a notice of assessment also prints a SIN, but is issued by the "
            "Canada Revenue Agency and assesses a tax year",
        },
        negative_anchors=["NOTICE OF ASSESSMENT", "AVIS DE COTISATION"],
        fields=[
            _sin_field(),
            _name_field(),
            _address_field(key="address.mailing"),
            _issue_date_field(),
        ],
        handling="The SIN is a restricted identifier: collect it only where legislation "
        "requires it, store it masked, and never use it as a general customer key. "
        "Plastic SIN cards were discontinued in 2014, so this letter is what "
        "clients now hold.",
    ),
    # ---------------------------------------------------------------------- tax
    DocTypeSpec(
        doctype_id="ca_cra_noa",
        label="CRA Notice of Assessment",
        country="CA",
        category=Category.tax,
        issuing_authority="Canada Revenue Agency / Agence du revenu du Canada",
        applies_to="both",
        anchors=[
            _a("NOTICE OF ASSESSMENT", decisive=True),
            _fr("AVIS DE COTISATION", decisive=True),
            _a("Canada Revenue Agency"),
            _fr("Agence du revenu du Canada"),
            _a("Tax year"),
            _a("Balance owing"),
            _a("RRSP deduction limit"),
        ],
        id_patterns=[SIN_PATTERN],
        confusable_with={
            "ca_t1_general": "the T1 is the return the taxpayer files; the notice of "
            "assessment is the CRA's reply to it",
            "ca_property_tax_assessment": "a property assessment notice is municipal and "
            "is titled AVIS D'ÉVALUATION FONCIÈRE",
        },
        negative_anchors=["AVIS D'ÉVALUATION FONCIÈRE", "PROPERTY ASSESSMENT NOTICE"],
        fields=[
            _name_field(),
            _sin_field(required=False),
            _address_field(key="address.mailing"),
            FieldSpec(
                name="tax_year",
                attribute_key="doc.tax_year",
                type="number",
                required=True,
                labels=_bilingual(
                    ["Tax year", "For the year"], ["Année d'imposition", "Pour l'année"]
                ),
                pattern=r"\b(19|20)\d{2}\b",
                locators=["label", "table", "regex"],
            ),
            _amount_field(
                "total_income",
                key="income.total_income",
                en=["Total income", "Line 15000"],
                fr=["Revenu total", "Ligne 15000"],
                required=True,
            ),
            _amount_field(
                "net_income",
                key="income.net_pay",
                en=["Net income", "Line 23600"],
                fr=["Revenu net"],
            ),
            _amount_field(
                "balance_owing",
                key="account.amount_due",
                en=["Balance owing", "Refund"],
                fr=["Solde dû", "Remboursement"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_t4",
        label="T4 Statement of Remuneration Paid",
        country="CA",
        category=Category.tax,
        issuing_authority="Employer; filed with the Canada Revenue Agency",
        anchors=[
            _a("STATEMENT OF REMUNERATION PAID", decisive=True),
            _fr("ÉTAT DE LA RÉMUNÉRATION PAYÉE", decisive=True),
            _a("Employment income"),
            _fr("Revenus d'emploi"),
            _a("Canada Revenue Agency"),
            _a("T4"),
            _a("Employer's name"),
        ],
        id_patterns=[SIN_PATTERN, BN_PATTERN],
        confusable_with={
            "ca_cra_noa": "the T4 is an employer's slip; the notice of assessment is the "
            "CRA's assessment of a whole return",
            "us_w2": "the US equivalent is the W-2, which carries OMB No. 1545-0008",
        },
        negative_anchors=["Wage and Tax Statement", "OMB No. 1545-0008"],
        fields=[
            _sin_field(required=False),
            FieldSpec(
                name="employer_name",
                attribute_key="income.employer",
                type="name",
                required=True,
                labels=_bilingual(["Employer's name"], ["Nom de l'employeur"]),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _name_field(),
            _address_field(),
            _amount_field(
                "employment_income",
                key="income.gross_salary",
                en=["Employment income", "Box 14"],
                fr=["Revenus d'emploi", "Case 14"],
                required=True,
            ),
            _amount_field(
                "income_tax_deducted",
                key="income.tax_deducted",
                en=["Income tax deducted", "Box 22"],
                fr=["Impôt sur le revenu retenu", "Case 22"],
            ),
            FieldSpec(
                name="tax_year",
                attribute_key="doc.tax_year",
                type="number",
                required=True,
                labels=_bilingual(["Year", "Tax year"], ["Année"]),
                pattern=r"\b(19|20)\d{2}\b",
                locators=["label", "table", "regex"],
            ),
        ],
        notes="The bare form number 'T4' also appears on T4A, T4E and T4RSP slips, so it is "
        "kept non-decisive; only the full bilingual title is decisive.",
    ),
    DocTypeSpec(
        doctype_id="ca_t1_general",
        label="T1 General Income Tax and Benefit Return",
        country="CA",
        category=Category.tax,
        issuing_authority="Canada Revenue Agency / Agence du revenu du Canada",
        anchors=[
            _a("INCOME TAX AND BENEFIT RETURN", decisive=True),
            _fr("DÉCLARATION DE REVENUS ET DE PRESTATIONS", decisive=True),
            _a("T1 GENERAL", decisive=True),
            _a("Canada Revenue Agency"),
            _a("Marital status"),
            _fr("État civil"),
        ],
        id_patterns=[SIN_PATTERN],
        confusable_with={
            "ca_cra_noa": "the return is filed by the taxpayer; the notice of assessment is "
            "issued by the CRA in response",
        },
        negative_anchors=["NOTICE OF ASSESSMENT", "AVIS DE COTISATION"],
        fields=[
            _name_field(),
            _sin_field(),
            _address_field(key="address.mailing"),
            FieldSpec(
                name="tax_year",
                attribute_key="doc.tax_year",
                type="number",
                required=True,
                labels=_bilingual(["Tax year", "For the year"], ["Année d'imposition"]),
                pattern=r"\b(19|20)\d{2}\b",
                locators=["label", "table", "regex"],
            ),
            _amount_field(
                "total_income",
                key="income.total_income",
                en=["Total income", "Line 15000"],
                fr=["Revenu total"],
                required=True,
            ),
            _amount_field(
                "taxable_income",
                key="income.net_pay",
                en=["Taxable income", "Line 26000"],
                fr=["Revenu imposable"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_bn_letter",
        label="CRA Business Number Registration Letter",
        country="CA",
        category=Category.tax,
        issuing_authority="Canada Revenue Agency / Agence du revenu du Canada",
        applies_to="corporate",
        anchors=[
            _a("BUSINESS NUMBER (BN) REGISTRATION", decisive=True),
            _a("Business Number"),
            _fr("Numéro d'entreprise"),
            _a("Program account"),
            _fr("Compte de programme"),
            _a("Canada Revenue Agency"),
            _a("RT0001"),
        ],
        id_patterns=[BN_PATTERN],
        confusable_with={
            "ca_certificate_status": "the BN letter is a CRA tax registration; a "
            "certificate of status is a corporate-registry document",
        },
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="business_number",
                attribute_key="id.business_number",
                type="id",
                required=True,
                labels=_bilingual(
                    ["Business Number", "BN", "Program account number"],
                    ["Numéro d'entreprise", "NE"],
                ),
                pattern=BN_PATTERN,
                notes="The nine-digit BN root carries a Luhn-style check digit and the "
                "program account adds a two-letter identifier plus four digits. No "
                "validator is declared for it here, so the value is captured "
                "unverified rather than checked with an unproven rule.",
            ),
            _address_field(key="address.registered"),
            _issue_date_field(),
        ],
        notes="CRA business-number correspondence has no stable printed title — 'Business "
        "Number (BN) registration', 'Confirmation of registration' and program-account "
        "summaries all occur. The decisive anchor is best-effort; the BN itself is the "
        "dependable signal.",
    ),
    # ---------------------------------------------------------------- corporate
    DocTypeSpec(
        doctype_id="ca_articles_incorporation_federal",
        label="Federal Articles of Incorporation (CBCA)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Corporations Canada (Innovation, Science and Economic Development)",
        applies_to="corporate",
        anchors=[
            _a("CANADA BUSINESS CORPORATIONS ACT", decisive=True),
            _fr("LOI CANADIENNE SUR LES SOCIÉTÉS PAR ACTIONS", decisive=True),
            _fr("STATUTS CONSTITUTIFS", decisive=True),
            _a("Articles of Incorporation"),
            _a("Corporations Canada"),
            _a("Corporation number"),
            _fr("Numéro de la société"),
        ],
        confusable_with={
            "ca_articles_incorporation_provincial": "federal articles cite the Canada "
            "Business Corporations Act; provincial "
            "articles cite their own province's act",
            "us_articles_incorporation": "US articles name a Secretary of State and are "
            "English-only",
            "ca_certificate_status": "a certificate of compliance attests current standing; "
            "the articles constitute the corporation",
        },
        negative_anchors=["CERTIFICATE OF COMPLIANCE", "Secretary of State"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="corporation_number",
                attribute_key="doc.registration_number",
                type="id",
                required=True,
                labels=_bilingual(["Corporation number"], ["Numéro de la société"]),
                notes="Corporations Canada numbers are 7 digits followed by a hyphen and a "
                "single check-style digit in most issues, but the format has changed "
                "over time — no pattern is asserted.",
            ),
            FieldSpec(
                name="incorporation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                required=True,
                labels=_bilingual(
                    ["Date of incorporation", "Effective date"],
                    ["Date de constitution", "Date d'entrée en vigueur"],
                ),
                validator="generic_date",
            ),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                labels=_bilingual(
                    ["Director", "Number of directors"],
                    ["Administrateur", "Nombre d'administrateurs"],
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _address_field(
                name="registered_office",
                key="entity.registered_office",
                en=["Registered office", "Registered office address"],
                fr=["Siège social", "Adresse du siège social"],
            ),
            FieldSpec(
                name="jurisdiction",
                attribute_key="entity.jurisdiction",
                type="string",
                labels=_bilingual(["Jurisdiction"], ["Compétence"]),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_articles_incorporation_provincial",
        label="Provincial Articles / Certificate of Incorporation",
        country="CA",
        category=Category.corporate,
        issuing_authority="Provincial corporate registry",
        applies_to="corporate",
        anchors=[
            _a("BUSINESS CORPORATIONS ACT (ONTARIO)", decisive=True),
            _a("BUSINESS CORPORATIONS ACT (BRITISH COLUMBIA)", decisive=True),
            _a("ALBERTA BUSINESS CORPORATIONS ACT", decisive=True),
            _fr("LOI SUR LES SOCIÉTÉS PAR ACTIONS (QUÉBEC)", decisive=True),
            _a("Certificate of Incorporation"),
            _a("Articles of Incorporation"),
            _fr("Registraire des entreprises"),
            _a("Ontario Corporation Number"),
        ],
        confusable_with={
            "ca_articles_incorporation_federal": "the federal articles cite the Canada "
            "Business Corporations Act and name "
            "Corporations Canada",
            "us_articles_incorporation": "US articles name a Secretary of State",
        },
        negative_anchors=["CANADA BUSINESS CORPORATIONS ACT", "Corporations Canada"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="corporation_number",
                attribute_key="doc.registration_number",
                type="id",
                required=True,
                labels=_bilingual(
                    ["Ontario Corporation Number", "Incorporation Number"],
                    ["Numéro d'entreprise du Québec", "NEQ"],
                ),
                notes="Each province numbers corporations differently — Ontario uses a "
                "7-digit OCN, Québec a 10-digit NEQ, British Columbia a "
                "letter-prefixed number. No single pattern is asserted.",
            ),
            FieldSpec(
                name="incorporation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                required=True,
                labels=_bilingual(["Date of incorporation"], ["Date de constitution"]),
                validator="generic_date",
            ),
            FieldSpec(
                name="province",
                attribute_key="entity.jurisdiction",
                type="string",
                required=True,
                labels=_bilingual(["Province", "Jurisdiction"], ["Province"]),
            ),
            _address_field(
                name="registered_office",
                key="entity.registered_office",
                en=["Registered office"],
                fr=["Siège social"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_certificate_status",
        label="Certificate of Status / Compliance",
        country="CA",
        category=Category.corporate,
        issuing_authority="Corporations Canada or a provincial corporate registry",
        applies_to="corporate",
        anchors=[
            _a("CERTIFICATE OF COMPLIANCE", decisive=True),
            _a("CERTIFICATE OF STATUS", decisive=True),
            _fr("CERTIFICAT DE CONFORMITÉ", decisive=True),
            _a("Corporations Canada"),
            _a("has not been dissolved"),
            _fr("n'a pas été dissoute"),
        ],
        confusable_with={
            "us_certificate_good_standing": "the US equivalent is a CERTIFICATE OF GOOD "
            "STANDING or CERTIFICATE OF EXISTENCE",
            "ca_annual_return": "the annual return is the corporation's own filing; the "
            "certificate is the registry's attestation",
        },
        negative_anchors=["CERTIFICATE OF GOOD STANDING", "Secretary of State"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="status",
                attribute_key="entity.status",
                type="string",
                required=True,
                labels=_bilingual(["Status", "in compliance"], ["Statut", "en conformité"]),
            ),
            FieldSpec(
                name="corporation_number",
                attribute_key="doc.registration_number",
                type="id",
                labels=_bilingual(["Corporation number"], ["Numéro de la société"]),
                notes="Federal and provincial registries number corporations differently; no "
                "single format applies.",
            ),
            _issue_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_annual_return",
        label="Corporate Annual Return",
        country="CA",
        category=Category.corporate,
        issuing_authority="Corporations Canada or a provincial corporate registry",
        applies_to="corporate",
        anchors=[
            _a("ANNUAL RETURN", decisive=True),
            _fr("DÉCLARATION ANNUELLE", decisive=True),
            _a("Anniversary date"),
            _fr("Date anniversaire"),
            _a("Corporations Canada"),
            _fr("Registraire des entreprises"),
        ],
        confusable_with={
            "ca_certificate_status": "the annual return is filed by the corporation; the "
            "certificate of status is issued by the registry",
            "ca_cra_noa": "the annual return is a corporate-registry filing, not a tax assessment",
        },
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="corporation_number",
                attribute_key="doc.registration_number",
                type="id",
                required=True,
                labels=_bilingual(["Corporation number"], ["Numéro de la société"]),
                notes="Federal and provincial registries number corporations differently; no "
                "single format applies.",
            ),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                labels=_bilingual(["Director"], ["Administrateur"]),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            _address_field(
                name="registered_office",
                key="entity.registered_office",
                en=["Registered office"],
                fr=["Siège social"],
            ),
            FieldSpec(
                name="filing_year",
                attribute_key="doc.tax_year",
                type="number",
                labels=_bilingual(["Year", "Anniversary year"], ["Année"]),
                pattern=r"\b(19|20)\d{2}\b",
                locators=["label", "table", "regex"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_partnership_agreement",
        label="Partnership Agreement",
        country="CA",
        category=Category.corporate,
        issuing_authority="Private instrument executed by the partners",
        applies_to="corporate",
        anchors=[
            _a("PARTNERSHIP AGREEMENT", decisive=True),
            _fr("CONTRAT DE SOCIÉTÉ", decisive=True),
            _a("General Partner"),
            _a("Limited Partner"),
            _fr("Associés"),
            _a("Profit sharing"),
        ],
        confusable_with={
            "us_operating_agreement": "an operating agreement governs an LLC and names "
            "members; a partnership agreement names partners",
            "ca_trust_deed": "a trust deed settles property on a trustee rather than "
            "forming a partnership",
        },
        negative_anchors=["OPERATING AGREEMENT", "DEED OF TRUST"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="partners",
                attribute_key="ownership.partner",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Partner", "General Partner", "Limited Partner"],
                    ["Associé", "Commandité", "Commanditaire"],
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="profit_shares",
                attribute_key="ownership.share",
                type="number",
                multi=True,
                labels=_bilingual(
                    ["Profit share", "Percentage"], ["Part des bénéfices", "Pourcentage"]
                ),
                pattern=r"\d{1,3}(?:\.\d+)?\s?%",
                locators=["table", "label", "regex"],
            ),
            FieldSpec(
                name="effective_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(["Effective date", "Dated"], ["Date d'entrée en vigueur"]),
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_trust_deed",
        label="Deed of Trust / Trust Deed",
        country="CA",
        category=Category.corporate,
        issuing_authority="Private instrument executed by the settlor and trustee",
        applies_to="corporate",
        anchors=[
            _a("DEED OF TRUST", decisive=True),
            _a("TRUST DEED", decisive=True),
            _fr("ACTE DE FIDUCIE", decisive=True),
            _a("Settlor"),
            _fr("Constituant"),
            _a("Trustee"),
            _fr("Fiduciaire"),
            _a("Beneficiary"),
        ],
        confusable_with={
            "us_trust_agreement": "the US instrument is usually titled TRUST AGREEMENT or "
            "DECLARATION OF TRUST",
            "us_mortgage_statement": "in the United States a 'deed of trust' is the "
            "security instrument for a mortgage loan, not a trust "
            "settlement — a US deed of trust names a lender and a "
            "property",
        },
        negative_anchors=["TRUST AGREEMENT", "MORTGAGE STATEMENT"],
        fields=[
            FieldSpec(
                name="trust_name",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels=_bilingual(["Name of Trust", "Trust"], ["Nom de la fiducie", "Fiducie"]),
                validator="name",
            ),
            FieldSpec(
                name="settlor",
                attribute_key="ownership.beneficial_owner",
                type="name",
                required=True,
                pii=True,
                labels=_bilingual(["Settlor", "Grantor"], ["Constituant"]),
                validator="name",
            ),
            FieldSpec(
                name="trustees",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels=_bilingual(["Trustee"], ["Fiduciaire"]),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="beneficiaries",
                attribute_key="ownership.partner",
                type="name",
                multi=True,
                pii=True,
                labels=_bilingual(["Beneficiary"], ["Bénéficiaire"]),
                validator="name",
            ),
            FieldSpec(
                name="execution_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(["Dated", "Executed"], ["Fait le"]),
                validator="generic_date",
            ),
        ],
    ),
    # ------------------------------------------------------ financial / address
    DocTypeSpec(
        doctype_id="ca_bank_statement",
        label="Canadian Bank Statement",
        country="CA",
        category=Category.financial,
        issuing_authority="Federally or provincially regulated financial institution",
        applies_to="both",
        anchors=[
            _a("Account Statement"),
            _fr("RELEVÉ DE COMPTE"),
            _a("Transit Number"),
            _fr("Numéro de transit"),
            _a("Institution Number"),
            _a("Opening Balance"),
            _a("Closing Balance"),
            _fr("Solde de clôture"),
            _a("Interac"),
        ],
        confusable_with={
            "us_bank_statement": "US statements print a nine-digit routing number; Canadian "
            "ones print a five-digit transit and a three-digit "
            "institution number",
            "mx_estado_cuenta": "the Mexican statement is titled ESTADO DE CUENTA and shows "
            "an 18-digit CLABE",
        },
        negative_anchors=["Routing Number", "ESTADO DE CUENTA", "Member FDIC"],
        fields=[
            _name_field(
                required=False, en=["Account Holder", "Customer Name"], fr=["Titulaire du compte"]
            ),
            _address_field(key="address.mailing", required=True),
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                type="id",
                required=True,
                pii=True,
                multi=True,
                labels=_bilingual(["Account Number", "Account No."], ["Numéro de compte"]),
                locators=["kv", "label", "table", "regex"],
                notes="Canadian account numbers run 7 to 12 digits depending on the institution "
                "and carry no check digit.",
            ),
            FieldSpec(
                name="transit_number",
                attribute_key="account.transit_number",
                type="id",
                labels=_bilingual(
                    ["Transit Number", "Branch Number"],
                    ["Numéro de transit", "Numéro de succursale"],
                ),
                pattern=r"\b\d{5}\b",
                notes="A Canadian transit number is five digits and is paired with a "
                "three-digit institution number. Neither carries a check digit.",
            ),
            _amount_field(
                "closing_balance",
                key="account.balance",
                en=["Closing Balance", "Ending Balance"],
                fr=["Solde de clôture", "Solde final"],
                required=True,
            ),
            FieldSpec(
                name="statement_period",
                attribute_key="account.statement_period",
                type="string",
                required=True,
                labels=_bilingual(["Statement Period", "For the period"], ["Période du relevé"]),
            ),
        ],
        notes="No decisive anchor: Canadian statements are issuer-branded. The transit and "
        "institution numbers are what separate them from US and Mexican statements.",
    ),
    DocTypeSpec(
        doctype_id="ca_utility_bill",
        label="Canadian Utility Bill (proof of address)",
        country="CA",
        category=Category.address_proof,
        issuing_authority="Provincial utility or telecommunications provider",
        applies_to="both",
        anchors=[
            _fr("HYDRO-QUÉBEC", decisive=True),
            _a("HYDRO ONE", decisive=True),
            _a("BC HYDRO", decisive=True),
            _a("Service Address"),
            _fr("Adresse de service"),
            _a("Amount Due"),
            _fr("Montant dû"),
            _fr("Consommation"),
            _a("kWh"),
        ],
        confusable_with={
            "us_utility_bill": "US bills print a rate schedule and a US service address; "
            "Canadian electricity bills name a Hydro utility",
            "ca_property_tax_assessment": "a property assessment notice values a property "
            "rather than billing consumption",
        },
        negative_anchors=["COMISIÓN FEDERAL DE ELECTRICIDAD", "Rate Schedule"],
        fields=[
            _name_field(
                required=True,
                en=["Customer Name", "Account Name"],
                fr=["Nom du client", "Titulaire"],
            ),
            _address_field(
                required=True,
                en=["Service Address", "Service Location"],
                fr=["Adresse de service", "Lieu de consommation"],
            ),
            FieldSpec(
                name="account_number",
                attribute_key="utility.consumer_number",
                type="id",
                pii=True,
                labels=_bilingual(
                    ["Account Number", "Customer Number"], ["Numéro de compte", "Numéro de client"]
                ),
                notes="Utility account numbers are assigned per provider; there is no shared "
                "format.",
            ),
            FieldSpec(
                name="provider",
                attribute_key="utility.service_provider",
                type="name",
                labels=_bilingual(["Utility", "Provider"], ["Fournisseur"]),
                validator="name",
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                en=["Amount Due", "Total Due"],
                fr=["Montant dû", "Total à payer"],
            ),
            FieldSpec(
                name="billing_period",
                attribute_key="utility.bill_period",
                type="string",
                required=True,
                labels=_bilingual(["Billing Period", "Service Period"], ["Période de facturation"]),
            ),
            _issue_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_property_tax_assessment",
        label="Property Assessment / Municipal Tax Notice",
        country="CA",
        category=Category.address_proof,
        issuing_authority="Provincial assessment authority or municipality",
        applies_to="both",
        anchors=[
            _a("PROPERTY ASSESSMENT NOTICE", decisive=True),
            _a("MUNICIPAL PROPERTY ASSESSMENT CORPORATION", decisive=True),
            _a("BC ASSESSMENT", decisive=True),
            _fr("AVIS D'ÉVALUATION FONCIÈRE", decisive=True),
            _a("Roll Number"),
            _fr("Numéro de matricule"),
            _a("Assessed Value"),
            _fr("Valeur foncière"),
            _a("MPAC"),
        ],
        confusable_with={
            "ca_cra_noa": "an assessment notice from the CRA assesses income tax; this one "
            "values real property and comes from a municipality or MPAC",
            "mx_predial": "the Mexican equivalent is the boleta del impuesto predial",
        },
        negative_anchors=["NOTICE OF ASSESSMENT", "AVIS DE COTISATION", "IMPUESTO PREDIAL"],
        fields=[
            _name_field(required=True, en=["Owner", "Property Owner"], fr=["Propriétaire"]),
            _address_field(
                required=True, en=["Property Address", "Location"], fr=["Adresse de l'immeuble"]
            ),
            FieldSpec(
                name="roll_number",
                attribute_key="property.roll_number",
                type="id",
                required=True,
                labels=_bilingual(
                    ["Roll Number", "Assessment Roll Number"],
                    ["Numéro de matricule", "Numéro de rôle"],
                ),
                notes="Roll numbers are municipal — Ontario's is 19 digits, other provinces differ "
                "— so no format is asserted.",
            ),
            _amount_field(
                "assessed_value",
                key="property.assessed_value",
                en=["Assessed Value", "Current Value Assessment"],
                fr=["Valeur foncière", "Valeur imposable"],
                required=True,
            ),
            FieldSpec(
                name="tax_year",
                attribute_key="doc.tax_year",
                type="number",
                labels=_bilingual(["Taxation Year", "Year"], ["Année d'imposition"]),
                pattern=r"\b(19|20)\d{2}\b",
                locators=["label", "table", "regex"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_lease_agreement",
        label="Canadian Residential Tenancy Agreement",
        country="CA",
        category=Category.address_proof,
        issuing_authority="Private instrument between landlord and tenant",
        applies_to="both",
        anchors=[
            _a("RESIDENTIAL TENANCY AGREEMENT", decisive=True),
            _a("STANDARD FORM OF LEASE", decisive=True),
            _fr("BAIL DE LOGEMENT", decisive=True),
            _a("Landlord"),
            _fr("Locateur"),
            _a("Tenant"),
            _fr("Locataire"),
            _a("Rent"),
            _fr("Loyer"),
        ],
        confusable_with={
            "us_lease_agreement": "US leases are titled RESIDENTIAL LEASE AGREEMENT and "
            "refer to a lessor and lessee",
        },
        negative_anchors=["RESIDENTIAL LEASE AGREEMENT", "CONTRATO DE ARRENDAMIENTO"],
        fields=[
            FieldSpec(
                name="landlord_name",
                attribute_key="tenancy.landlord_name",
                type="name",
                required=True,
                labels=_bilingual(["Landlord", "Lessor"], ["Locateur"]),
                validator="name",
            ),
            FieldSpec(
                name="tenant_name",
                attribute_key="tenancy.tenant_name",
                type="name",
                required=True,
                pii=True,
                labels=_bilingual(["Tenant", "Lessee"], ["Locataire"]),
                validator="name",
            ),
            _address_field(
                name="premises",
                required=True,
                en=["Rental Unit", "Premises", "Address of the rental unit"],
                fr=["Logement loué", "Adresse du logement"],
            ),
            _amount_field(
                "monthly_rent",
                key="tenancy.monthly_rent",
                en=["Rent", "Monthly Rent", "Base Rent"],
                fr=["Loyer", "Loyer mensuel"],
            ),
            FieldSpec(
                name="term",
                attribute_key="tenancy.term",
                type="string",
                labels=_bilingual(["Term", "Tenancy term"], ["Durée du bail"]),
            ),
            FieldSpec(
                name="term_start",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(["Start of tenancy", "Commencement"], ["Début du bail"]),
                validator="generic_date",
            ),
        ],
    ),
)

#: Fast lookup used by the tests and by callers that already hold a doctype id.
DOCTYPES_BY_ID: dict[str, DocTypeSpec] = {spec.doctype_id: spec for spec in SPECS}


def specs() -> tuple[DocTypeSpec, ...]:
    """Return every Canadian :class:`~dce.models.DocTypeSpec` in this pack."""
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
    for module_name in ("dce.registry.mexico", "dce.registry.usa"):
        import_module(module_name)


_load_sibling_packs()
