"""Canada doctype pack — 37 :class:`~dce.models.DocTypeSpec` entries.

Canadian federal documents are bilingual by law, and OCR sees both halves. Every federal
doctype here therefore carries French anchors alongside the English ones, and the French
string is often the *better* discriminator: "PERMANENT RESIDENT CARD" is printed on the US
green card too, but "CARTE DE RÉSIDENT PERMANENT" is only ever printed on the Canadian one.

The conventions are the ones documented in :mod:`dce.registry.usa`:

* decisive anchors stay unique across the registry — a shared issuing header such as
  "Canada Revenue Agency / Agence du revenu du Canada" appears on the notice of assessment,
  the T4, the T1 and the business-number letter alike, so it is deliberately **not**
  decisive; the form's own bilingual title is. Uniqueness in the registry is necessary and
  **not sufficient**: every decisive anchor here also names its grounds in
  :class:`dce.models.Controls`, and the property that matters — *it must not appear on a
  document of another type, including by being cited by one* — is checked against ``corpus/``
  by ``tests/test_registry_corpus_decisive.py``. That check is what demoted the English
  ID titles in this pack: ``corpus/ca/ca_sin_confirmation.pdf`` lists the identity documents
  Service Canada accepts, and thereby prints ``CERTIFICATE OF CANADIAN CITIZENSHIP``,
  ``CONFIRMATION OF PERMANENT RESIDENCE``, ``BIRTH CERTIFICATE``, ``CERTIFICATE OF BIRTH``,
  ``CERTIFICATE OF MARRIAGE`` and ``DRIVER'S LICENSE`` on a document that is none of them.
  The French halves survive, which is the same point the paragraph above makes about
  ``CARTE DE RÉSIDENT PERMANENT`` — and they survive as ``CLASS_NAME_UNCONTESTED``, not as
  proof, because the only thing separating them from their English twins is that the corpus
  holds no French-language list of acceptable ID;
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

from dce.models import Anchor, Category, Controls, DocTypeSpec, FieldSpec, Zone

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
    # The five keys below are shared with the US and Mexican packs, which reached them in
    # the same round for the same reason. Their wording is copied verbatim from
    # :mod:`dce.registry.usa` — the pack this one's conventions are documented against —
    # because the loader merges the extension dicts with ``setdefault``, so a pack that
    # phrases a shared key differently gets whichever wording happened to import first.
    # ``tests/test_registry_na.py::test_pack_namespace_extensions_agree_with_each_other``
    # is the check that keeps that from becoming an import-order-dependent ontology.
    "entity.auditor": "Independent accounting firm that signed the audit report",
    "entity.exchange": "Exchange on which a class of securities is registered",
    "entity.security_class": "Title of the class of securities a filing concerns",
    "entity.shares_outstanding": "Shares of a class outstanding as of a stated date",
    "entity.ticker": "Trading symbol under which a class of securities trades",
    "doc.period_covered": "Reporting period a periodic report covers",
    # Shared with the Mexican pack only; its wording, for the same reason.
    "entity.fiscal_year_end": "Financial year end the report or filing closes on",
    # Canadian-only additions.
    "property.name": "Named mineral or oil-and-gas property a technical report covers",
    "ownership.securities_held": "Number of securities a reported holder owns, controls "
    "or directs",
    "ownership.signer_title": "Office an authorised signatory signed a filing in",
    "doc.filing_date": "Date a document was filed with, or dated for, a regulator",
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
    controls: Controls | None = None,
    zone: Zone | None = None,
) -> Anchor:
    """Build an :class:`~dce.models.Anchor` (``lang`` is "en" or "fr" in this pack).

    ``controls`` is mandatory when ``decisive`` is set and forbidden otherwise — see
    :class:`dce.models.Controls`. It has no default here on purpose: a builder that supplied
    one would re-create the invisible claim the field exists to prevent.
    """
    return Anchor(text=text, lang=lang, decisive=decisive, controls=controls, zone=zone)


def _fr(
    text: str,
    *,
    decisive: bool = False,
    controls: Controls | None = None,
    zone: Zone | None = None,
) -> Anchor:
    """Build a French anchor — the half of a bilingual header OCR often reads best."""
    return Anchor(text=text, lang="fr", decisive=decisive, controls=controls, zone=zone)


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


# -- reporting-issuer builders ----------------------------------------------
# The continuous-disclosure filings below are prose documents rather than forms with boxes,
# so their fields are found by label far more often than by key/value pair. The locator order
# reflects that; ``kv`` is kept last rather than dropped because SEDAR+ cover pages are
# tabular and Azure does emit pairs for them.
_PROSE_LOCATORS = ["label", "table", "kv"]


def _issuer_name_field(*, required: bool = True) -> FieldSpec:
    """Legal name of the reporting issuer a filing is made by or about.

    Distinct from :func:`_entity_name_field` only in its labels: a continuous-disclosure
    filing says "the Company" / "the Issuer", not "Name of Corporation".
    """
    return FieldSpec(
        name="entity_legal_name",
        attribute_key="entity.legal_name",
        type="name",
        required=required,
        labels=_bilingual(
            ["Name of Company", "Name of Issuer", "Name of the Company", "Reporting Issuer"],
            ["Dénomination de la société", "Nom de l'émetteur", "Émetteur assujetti"],
        ),
        validator="name",
        locators=_PROSE_LOCATORS,
    )


def _period_covered_field(*, required: bool = False) -> FieldSpec:
    """The reporting period a periodic filing covers, as printed.

    Captured as a string rather than a date because the printed form is a *range* on most of
    these filings ("for the three and nine months ended September 30, 2025"); splitting it
    into two dates here would invent a structure the document does not have.
    """
    return FieldSpec(
        name="period_covered",
        attribute_key="doc.period_covered",
        type="string",
        required=required,
        labels=_bilingual(
            [
                "For the year ended",
                "For the financial year ended",
                "For the three months ended",
                "Period covered",
            ],
            ["Pour l'exercice clos le", "Pour la période close le", "Période visée"],
        ),
        locators=_PROSE_LOCATORS,
    )


def _fiscal_year_end_field() -> FieldSpec:
    """Financial year end of the issuer."""
    return FieldSpec(
        name="fiscal_year_end",
        attribute_key="entity.fiscal_year_end",
        type="date",
        labels=_bilingual(
            ["Financial year end", "Fiscal year end", "Year ended"],
            ["Fin d'exercice", "Clôture de l'exercice", "Exercice clos le"],
        ),
        validator="generic_date",
        locators=_PROSE_LOCATORS,
    )


def _filing_date_field(*, required: bool = False) -> FieldSpec:
    """The date the filing bears — the date it was dated or filed, not the period it covers."""
    return FieldSpec(
        name="filing_date",
        attribute_key="doc.filing_date",
        type="date",
        required=required,
        labels=_bilingual(
            ["Date of this report", "Date of report", "Dated", "Date filed"],
            ["Date du présent rapport", "Fait le", "Date de dépôt"],
        ),
        validator="generic_date",
        locators=_PROSE_LOCATORS,
    )


def _listing_fields() -> list[FieldSpec]:
    """Trading symbol and the exchange it trades on.

    Neither carries a pattern. A ticker is one to five letters with an optional class suffix,
    which is also the shape of a great many ordinary words in a prose filing, so a regex here
    would bind noise with more confidence than a missing value deserves — the label is the
    only trustworthy locator for it.
    """
    return [
        FieldSpec(
            name="ticker",
            attribute_key="entity.ticker",
            type="string",
            labels=_bilingual(
                ["Trading symbol", "Ticker symbol", "Symbol"],
                ["Symbole boursier", "Symbole"],
            ),
            locators=["label", "kv", "table"],
        ),
        FieldSpec(
            name="exchange",
            attribute_key="entity.exchange",
            type="string",
            labels=_bilingual(
                ["Stock exchange", "Exchange", "Listed on", "Toronto Stock Exchange"],
                ["Bourse", "Inscrite à la cote de"],
            ),
            locators=["label", "kv", "table"],
        ),
    ]


def _jurisdiction_field() -> FieldSpec:
    """Province, territory or country under whose law the issuer exists."""
    return FieldSpec(
        name="jurisdiction",
        attribute_key="entity.jurisdiction",
        type="string",
        labels=_bilingual(
            ["Jurisdiction of incorporation", "Incorporated under the laws of", "Jurisdiction"],
            ["Territoire de constitution", "Constituée sous le régime des lois"],
        ),
        locators=_PROSE_LOCATORS,
    )


def _auditor_field() -> FieldSpec:
    """The independent auditor named on an annual filing."""
    return FieldSpec(
        name="auditor",
        attribute_key="entity.auditor",
        type="name",
        labels=_bilingual(
            ["Auditor", "Independent Auditor", "Chartered Professional Accountants"],
            ["Auditeur", "Auditeur indépendant", "Comptables professionnels agréés"],
        ),
        validator="name",
        locators=_PROSE_LOCATORS,
    )


def _signatory_fields(*, name_labels_en: list[str] | None = None) -> list[FieldSpec]:
    """The individual who signed a filing, and the office they signed in.

    The name is ``pii=True``: a certifying officer on a 52-109 certificate or a qualified
    person on a 43-101 report is a natural person, and the fact that the document is a
    corporate filing does not make their name any less personal data.

    Bare ``Name``, ``Nom`` and ``Per`` are deliberately absent from the defaults. A label is
    not free: :func:`dce.classify.profiles.declarative_counts` folds every field label into
    the doctype's term profile at ``field_label`` weight, so a label that appears on every
    document in the world buys no extraction — it binds the first "Name:" on the page, which
    on a multi-page filing is almost never the signatory — while spending profile mass on
    vocabulary that cannot discriminate anything.
    """
    return [
        FieldSpec(
            name="signatory_name",
            attribute_key="ownership.authorized_signer",
            type="name",
            pii=True,
            labels=_bilingual(
                name_labels_en or ["Signature", "Signed by", "Name and title"],
                ["Signature", "Signé par", "Nom et titre"],
            ),
            validator="name",
            locators=_PROSE_LOCATORS,
        ),
        FieldSpec(
            name="signatory_title",
            attribute_key="ownership.signer_title",
            type="string",
            labels=_bilingual(
                ["Title", "Office", "Chief Executive Officer", "Chief Financial Officer"],
                ["Titre", "Fonction", "Chef de la direction", "Chef des finances"],
            ),
            locators=_PROSE_LOCATORS,
        ),
    ]


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
            _a("P<CAN", decisive=True, controls=Controls.MRZ_PREFIX),
            _a("PASSPORT", zone=Zone.title),
            _fr("PASSEPORT", zone=Zone.title),
            _a("Government of Canada"),
            _fr("Gouvernement du Canada"),
            _fr("Autorité"),
            # "Place of birth" was removed: an ICAO 9303 visual-inspection-zone label
            # printed on every state's passport, and on birth certificates and immigration
            # forms besides, so it cannot say the book is Canadian. The MRZ prefix and the
            # bilingual Government of Canada masthead do that.
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
            # Pinned to the title zone, exactly as us_drivers_license pins its two spellings.
            # Both of these are ordinary phrases as well as document titles: a form that
            # lists acceptable identity documents prints "driver's licence" in prose, and so
            # does a lease, a bank's account-opening pack and an insurance policy. Unpinned,
            # they were near-proof of a Canadian licence merely because the words appeared —
            # and they fired on a Virginia DMV publication, which spells the heading of one
            # of its own pages "Standard Driver's Licence Card Under 21 - Encoding". A US
            # specimen sheet was classified as a Canadian licence at confidence 0.90 on the
            # strength of a single letter in a typo. Requiring the title zone is what makes
            # the claim "this document is titled a driver's licence" instead of "this
            # document mentions one", and it makes the two licence specs symmetric: whatever
            # a caller's layout provider can prove for one, it can prove for the other.
            #
            # "PERMIS DE CONDUIRE" is pinned for the same reason and one more: it is the
            # title of the French, Belgian and Swiss licences too, none of which this
            # registry models, so as unpinned near-proof it was claiming three jurisdictions
            # it knows nothing about.
            _a("DRIVER'S LICENCE", zone=Zone.title),
            _fr(
                "PERMIS DE CONDUIRE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
                zone=Zone.title,
            ),
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
            _a("ONTARIO PHOTO CARD", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a(
                "ALBERTA IDENTIFICATION CARD",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "MANITOBA IDENTIFICATION CARD",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _fr(
                "CARTE DE RÉSIDENT PERMANENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr("RÉSIDENT PERMANENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("CONFIRMATION OF PERMANENT RESIDENCE"),
            _fr(
                "CONFIRMATION DE RÉSIDENCE PERMANENTE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("IMM 5292", decisive=True, controls=Controls.FORM_NUMBER),
            _a("IMM 5688", decisive=True, controls=Controls.FORM_NUMBER),
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
            _a("CERTIFICATE OF CANADIAN CITIZENSHIP"),
            _fr(
                "CERTIFICAT DE CITOYENNETÉ CANADIENNE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a(
                "SECURE CERTIFICATE OF INDIAN STATUS",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr(
                "CERTIFICAT SÉCURISÉ DE STATUT D'INDIEN",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a(
                "REFUGEE PROTECTION CLAIMANT DOCUMENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr(
                "DOCUMENT DU DEMANDEUR D'ASILE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("IMM 1442", decisive=True, controls=Controls.FORM_NUMBER),
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
            _fr(
                "CARTE D'ASSURANCE MALADIE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr(
                "RÉGIE DE L'ASSURANCE MALADIE DU QUÉBEC",
                decisive=True,
                controls=Controls.ISSUER_NAME,
            ),
            _a("ONTARIO HEALTH CARD", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("NEXUS", decisive=True, controls=Controls.ISSUER_NAME, zone=Zone.title),
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
            _a(
                "CONFIRMATION OF SOCIAL INSURANCE NUMBER",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr(
                "CONFIRMATION DU NUMÉRO D'ASSURANCE SOCIALE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a("NOTICE OF ASSESSMENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _fr("AVIS DE COTISATION", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("STATEMENT OF REMUNERATION PAID", decisive=True, controls=Controls.ISSUER_TEMPLATE),
            _fr("ÉTAT DE LA RÉMUNÉRATION PAYÉE", decisive=True, controls=Controls.ISSUER_TEMPLATE),
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
            _a("INCOME TAX AND BENEFIT RETURN"),
            _fr(
                "DÉCLARATION DE REVENUS ET DE PRESTATIONS",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("T1 GENERAL", decisive=True, controls=Controls.FORM_NUMBER),
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
            _a(
                "BUSINESS NUMBER (BN) REGISTRATION",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
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
            _a("CANADA BUSINESS CORPORATIONS ACT"),
            _fr(
                "LOI CANADIENNE SUR LES SOCIÉTÉS PAR ACTIONS",
                decisive=True,
                controls=Controls.STATUTE_TITLE,
            ),
            _fr("STATUTS CONSTITUTIFS", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            "ca_isc_register": "both cite the CBCA — the articles constitute the "
            "corporation, the s.21.1 register lists its individuals with significant control",
        },
        # ``CANADA BUSINESS CORPORATIONS ACT`` is decisive here, and it is printed on every
        # CBCA document, not only on the articles — including a s.21.1 ISC register. The
        # register's own statutory title is therefore a negative anchor, so that the decisive
        # statute name cannot carry an ISC register to this doctype.
        negative_anchors=[
            "CERTIFICATE OF COMPLIANCE",
            "Secretary of State",
            "REGISTER OF INDIVIDUALS WITH SIGNIFICANT CONTROL",
        ],
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
            _a(
                "BUSINESS CORPORATIONS ACT (ONTARIO)",
                decisive=True,
                controls=Controls.STATUTE_TITLE,
            ),
            _a("BUSINESS CORPORATIONS ACT (BRITISH COLUMBIA)"),
            _a("ALBERTA BUSINESS CORPORATIONS ACT", decisive=True, controls=Controls.STATUTE_TITLE),
            _fr(
                "LOI SUR LES SOCIÉTÉS PAR ACTIONS (QUÉBEC)",
                decisive=True,
                controls=Controls.STATUTE_TITLE,
            ),
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
            _a(
                "CERTIFICATE OF COMPLIANCE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("CERTIFICATE OF STATUS"),
            _fr(
                "CERTIFICAT DE CONFORMITÉ",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a("ANNUAL RETURN"),
            _fr("DÉCLARATION ANNUELLE", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("PARTNERSHIP AGREEMENT"),
            _fr("CONTRAT DE SOCIÉTÉ", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("DEED OF TRUST", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("TRUST DEED", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _fr("ACTE DE FIDUCIE", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
    # ------------------------------------------- securities / reporting issuer
    #
    # Everything in this block is anchored on a Canadian Securities Administrators
    # instrument or form number — ``Form 51-102F2``, ``NI 43-101``, ``Form 52-109F1``.
    # That choice is the whole design of the block and it is worth stating once:
    #
    # A continuous-disclosure filing's *title* is a document-class name. "Annual
    # Information Form", "Management's Discussion and Analysis", "Material Change Report",
    # "Prospectus" and "Annual Report" are printed by issuers on four continents, in
    # English, on documents governed by four different regulators. Declaring any of them
    # decisive would produce a confident, wrong, cross-jurisdiction answer — which is the
    # worst error this service can make. The CSA form number is the opposite: the CSA
    # alone assigns it, no other regulator uses that numbering, and an issuer who prints
    # ``Form 51-102F3`` on a document is telling us which document it is. So the form
    # number and the CSA instrument number carry ``decisive=True``, the class name is a
    # supporting anchor, and a filing that omits its form number scores on supporting
    # evidence only and abstains — which routes to a human and is safe.
    #
    # Two consequences of that, both deliberate:
    #
    # *The shared boilerplate is declared uniformly.* ``National Instrument 51-102`` and
    # ``Règlement 51-102`` appear on the AIF, the MD&A, the material change report, the
    # business acquisition report and the information circular alike. All five declare it,
    # so it contributes equally to all five and cancels out of the margin between them.
    # Declaring it on only some of them would push a document that prints it toward
    # whichever subset happened to claim it.
    #
    # *Form numbers are safe to keep one character apart* — ``51-102F1`` vs ``51-102F5`` —
    # only because :func:`dce.classify.anchors._match_kind` refuses fuzzy matching to a
    # decisive anchor. That refusal exists because ``Form W-2`` once fuzzy-matched
    # ``Form W-9``. These anchors depend on it; do not relax it.
    #
    # Québec filings carry the AMF's French numbering (``Annexe 51-102A2`` for
    # ``Form 51-102F2``), which is a different string, not a translation of one — so the
    # French annex numbers are declared as decisive anchors in their own right.
    DocTypeSpec(
        doctype_id="ca_aif",
        label="Annual Information Form (Form 51-102F2)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, filed on SEDAR+ under NI 51-102 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 51-102F2", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 51-102A2", decisive=True, controls=Controls.FORM_NUMBER),
            _a("ANNUAL INFORMATION FORM"),
            _fr("NOTICE ANNUELLE"),
            _a("National Instrument 51-102"),
            _fr("Règlement 51-102"),
            # ---------------------------------------------------------------
            # The form's own prescribed structure, as *supporting* anchors.
            #
            # An AIF that omits its form number used to be unclassifiable: the class name
            # is shared (``ANNUAL INFORMATION FORM`` is printed on this corpus's
            # information circular, its prospectus and its standalone reserves statement
            # too) and ``National Instrument 51-102`` is declared by five doctypes on
            # purpose, so it cancels out of every margin between them. What is left has to
            # be the thing the CSA actually prescribes about an AIF, which is its ITEM LIST.
            #
            # These are the Part 2 item headings of Form 51-102F2, verbatim from the CSA
            # unofficial consolidation effective 30 June 2015, Items 3 to 18. Two rules
            # govern the set, and both are properties of the form rather than of any
            # document:
            #
            # *Item granularity, not sub-item granularity.* The form numbers Item 12
            # ``Legal Proceedings and Regulatory Actions`` and then sub-numbers 12.1
            # ``Legal Proceedings``. The item heading is the form's name for the item; the
            # sub-heading is a fragment of it, and the fragments are exactly the strings
            # that mean something else elsewhere — ``Risk Factors`` (5.2), ``Constraints``
            # (7.2), ``Ratings`` (7.3), ``Trading Price and Volume`` (8.1), ``Prior Sales``
            # (8.2), ``Legal Proceedings`` (12.1), ``Conflicts of Interest`` (10.3). Every
            # one of those is printed by a US Form 10-K, 20-F or proxy statement in this
            # corpus. Taking the list at the granularity the form numbers it drops all of
            # them without anyone having to judge them one at a time.
            #
            # That granularity was chosen by measurement, not by taste. Declaring the item
            # headings AND all nineteen sub-item headings — same source, one level deeper —
            # was run over the whole corpus: precision-when-answered falls from 100.0% to
            # 99.2%, it produces a wrong answer (``us_operating_agreement`` returned as
            # ``us_articles_incorporation``) and it additionally costs both information
            # circulars and the prospectus, which the shared fragments pull toward this
            # doctype. Item granularity: 133 correct, 0 wrong. Sub-item granularity: 130
            # correct, 1 wrong. A wrong doctype is a compliance incident and an abstention
            # is not, so the deeper list is refused even though it is just as faithful to
            # the published form.
            #
            # *Items 1 and 2 are excluded.* ``Cover Page`` and ``Table of Contents`` are
            # instructions about the document's front matter, not disclosure items; every
            # long filing has both.
            #
            # None is decisive and none could be: Form 41-101F1 (long form prospectus)
            # prescribes several of the same headings. What separates this doctype from
            # ca_prospectus is the regulator's no-opinion legend, which only the prospectus
            # carries. What the item list separates it from is a standalone Form
            # 51-101F1/F2/F3 reserves statement, which carries none of it — see the
            # containment note on ca_ni_51_101_oil_gas.
            #
            # The point of taking the WHOLE list is that no single heading has to carry the
            # doctype. A genuine AIF prints most of the list and accumulates; a document
            # that prints one or two of them accumulates one or two anchors' worth, which
            # is what one or two headings are worth. That is what makes this a property of
            # the form and not a patch for one specimen.
            _a("Corporate Structure"),
            _a("General Development of the Business"),
            _a("Describe the Business"),
            _a("Dividends and Distributions"),
            _a("Description of Capital Structure"),
            _a("Market for Securities"),
            _a(
                "Escrowed Securities and Securities Subject to Contractual Restriction "
                "on Transfer"
            ),
            _a("Directors and Officers"),
            _a("Promoters"),
            _a("Legal Proceedings and Regulatory Actions"),
            _a("Interest of Management and Others in Material Transactions"),
            _a("Transfer Agents and Registrars"),
            _a("Material Contracts"),
            _a("Interests of Experts"),
            _a("Additional Information"),
            _a("Additional Disclosure for Companies Not Sending Information Circulars"),
            # Not a Form 51-102F2 item, and listed apart because its provenance is a
            # different instrument: NI 52-110 s.5.1 requires a non-venture issuer's AIF to
            # include the disclosure of Form 52-110F1 *Audit Committee Information Required
            # in an AIF*, and the heading travels with it. It qualifies for the same reason
            # as the items above — prescribed content of this document type by an
            # instrument, not a string one specimen happened to print.
            _a("Audit Committee Information"),
        ],
        confusable_with={
            "ca_mda": "the AIF describes the business and its risks; the MD&A explains the "
            "period's financial results, and is Form 51-102F1",
            "ca_prospectus": "a prospectus carries the CSA receipt legend and offers "
            "securities; the AIF offers nothing",
            "ca_information_circular": "the circular solicits proxies for a meeting and is "
            "Form 51-102F5",
            "ca_ni_51_101_oil_gas": "CONTAINMENT, not confusion. NI 51-101 s.2.1 requires an "
            "issuer with oil and gas activities to file Forms 51-101F1, "
            "F2 and F3 annually, and issuers routinely bind them into "
            "the AIF as schedules — so those form numbers inside an AIF "
            "identify an *enclosure*, not the enclosing document. The "
            "AIF is separated from a standalone reserves statement by "
            "its own Form 51-102F2 item headings, which the reserves "
            "statement never carries",
        },
        negative_anchors=[
            "FORM 10-K",
            "FORM 20-F",
            "SECURITIES AND EXCHANGE COMMISSION",
        ],
        fields=[
            _issuer_name_field(),
            _period_covered_field(),
            _fiscal_year_end_field(),
            _filing_date_field(),
            _jurisdiction_field(),
            _auditor_field(),
            *_listing_fields(),
            _address_field(
                name="head_office",
                key="entity.registered_office",
                en=["Head office", "Registered office", "Principal office"],
                fr=["Siège social", "Établissement principal"],
            ),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels=_bilingual(
                    ["Shares outstanding", "Issued and outstanding", "Common shares outstanding"],
                    ["Actions en circulation", "Émises et en circulation"],
                ),
                pattern=r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b",
                locators=["label", "table", "kv", "regex"],
            ),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels=_bilingual(
                    ["Director", "Directors and Officers"],
                    ["Administrateur", "Administrateurs et dirigeants"],
                ),
                validator="name",
                locators=["table", "label", "kv"],
            ),
        ],
        notes="An AIF that does not print its form number is common, and the class name alone "
        "is shared with three other doctypes here, so identification rests on the Form "
        "51-102F2 item list above and on how much of it the document prints. That is a "
        "property of the form: an AIF carries most of the list by construction, a standalone "
        "Form 51-101F1/F2/F3 reserves statement carries none of it, and no single heading has "
        "to be right. Where the item evidence and the reserves vocabulary genuinely disagree "
        "— an oil-and-gas AIF whose text is mostly reserves tables — the two tiers dissent and "
        "the classifier abstains, which routes to a human and is safe.",
    ),
    DocTypeSpec(
        doctype_id="ca_mda",
        label="Management's Discussion and Analysis (Form 51-102F1)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, filed on SEDAR+ under NI 51-102 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 51-102F1", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 51-102A1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("MANAGEMENT'S DISCUSSION AND ANALYSIS"),
            _fr("RAPPORT DE GESTION"),
            _a("National Instrument 51-102"),
            _fr("Règlement 51-102"),
            _a("Selected Annual Information"),
            _a("Summary of Quarterly Results"),
        ],
        confusable_with={
            "ca_aif": "the MD&A explains the period's results and is Form 51-102F1; the AIF "
            "describes the business and is Form 51-102F2",
            "ca_ni_52_109_certification": "the certificate attests to the MD&A rather than "
            "being it, and is a one-page Form 52-109F1/F2",
        },
        negative_anchors=["FORM 10-K", "FORM 10-Q", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            _issuer_name_field(),
            _period_covered_field(required=True),
            _fiscal_year_end_field(),
            _filing_date_field(),
            *_listing_fields(),
            _auditor_field(),
            *_signatory_fields(),
        ],
        notes="``MANAGEMENT'S DISCUSSION AND ANALYSIS`` is deliberately NOT decisive. The same "
        "heading is Item 7 of a US Form 10-K and appears on annual reports worldwide; the "
        "string that belongs to the CSA and to nobody else is ``Form 51-102F1``.",
    ),
    DocTypeSpec(
        doctype_id="ca_material_change_report",
        label="Material Change Report (Form 51-102F3)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, filed on SEDAR+ under NI 51-102 Part 7 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 51-102F3", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 51-102A3", decisive=True, controls=Controls.FORM_NUMBER),
            _a(
                "FULL DESCRIPTION OF MATERIAL CHANGE",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("MATERIAL CHANGE REPORT"),
            _fr("DÉCLARATION DE CHANGEMENT IMPORTANT"),
            _a("Date of Material Change"),
            _a("National Instrument 51-102"),
            _fr("Règlement 51-102"),
        ],
        confusable_with={
            "ca_business_acquisition_report": "a BAR reports a completed significant "
            "acquisition and is Form 51-102F4; an MCR reports any material change",
            "ca_early_warning_report": "the early warning report is filed by an acquiror of "
            "securities, not by the issuer",
        },
        negative_anchors=["FORM 8-K", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            _issuer_name_field(),
            _address_field(
                name="head_office",
                key="entity.registered_office",
                en=["Head office", "Address of head office"],
                fr=["Siège social"],
            ),
            FieldSpec(
                name="material_change_date",
                attribute_key="doc.period_covered",
                type="string",
                required=True,
                labels=_bilingual(
                    ["Date of Material Change", "Date of the material change"],
                    ["Date du changement important"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            _filing_date_field(required=True),
            *_signatory_fields(),
            FieldSpec(
                name="news_release_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["News Release", "Date of news release", "Press release"],
                    ["Communiqué de presse", "Date du communiqué"],
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
        ],
        notes="``FULL DESCRIPTION OF MATERIAL CHANGE`` is decisive because it is a CSA-prescribed "
        "item heading of Form 51-102F3, printed verbatim — not because it names the document "
        "class. ``MATERIAL CHANGE REPORT`` is the class name and stays supporting.",
    ),
    DocTypeSpec(
        doctype_id="ca_business_acquisition_report",
        label="Business Acquisition Report (Form 51-102F4)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, filed on SEDAR+ under NI 51-102 Part 8 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 51-102F4", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 51-102A4", decisive=True, controls=Controls.FORM_NUMBER),
            _a("BUSINESS ACQUISITION REPORT"),
            _fr("DÉCLARATION D'ACQUISITION D'ENTREPRISE"),
            _a("significant acquisition"),
            _a("Details of Acquisition"),
            _a("National Instrument 51-102"),
            _fr("Règlement 51-102"),
        ],
        confusable_with={
            "ca_material_change_report": "an MCR reports the change; the BAR carries the "
            "acquired business's financial statements and is Form 51-102F4",
            "ca_aif": "the BAR concerns one acquisition, the AIF the whole business",
        },
        negative_anchors=["FORM 8-K", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            _issuer_name_field(),
            FieldSpec(
                name="acquired_business_name",
                attribute_key="entity.trade_name",
                type="name",
                labels=_bilingual(
                    ["Name of the business acquired", "Acquired business", "Vendor"],
                    ["Entreprise acquise", "Dénomination de l'entreprise acquise"],
                ),
                validator="name",
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="acquisition_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Date of Acquisition", "Acquisition date", "Closing date"],
                    ["Date de l'acquisition", "Date de clôture"],
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            _amount_field(
                "consideration",
                key="account.amount_due",
                en=["Consideration", "Purchase price", "Total consideration"],
                fr=["Contrepartie", "Prix d'achat"],
            ),
            _filing_date_field(),
            _period_covered_field(),
            *_signatory_fields(),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_information_circular",
        label="Management Information Circular / Proxy Circular (Form 51-102F5)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, filed on SEDAR+ under NI 51-102 Part 9 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 51-102F5"),
            _fr("ANNEXE 51-102A5", decisive=True, controls=Controls.FORM_NUMBER),
            _a("MANAGEMENT INFORMATION CIRCULAR"),
            _fr("CIRCULAIRE DE SOLLICITATION DE PROCURATIONS"),
            _a("Statement of Executive Compensation"),
            _a("Appointment of Proxyholder"),
            _a("Notice of Annual Meeting of Shareholders"),
            _a("National Instrument 51-102"),
            _fr("Règlement 51-102"),
        ],
        confusable_with={
            "ca_aif": "the circular solicits proxies for a meeting; the AIF is the annual "
            "description of the business",
            "ca_annual_return": "the annual return is a corporate-registry filing, not a "
            "securities-law disclosure document",
        },
        negative_anchors=["SCHEDULE 14A", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            _issuer_name_field(),
            FieldSpec(
                name="meeting_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Date of the meeting", "Meeting date", "to be held on"],
                    ["Date de l'assemblée", "qui se tiendra le"],
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="record_date",
                attribute_key="doc.due_date",
                type="date",
                labels=_bilingual(["Record date"], ["Date de clôture des registres"]),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            _filing_date_field(),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels=_bilingual(
                    ["Shares outstanding", "entitled to vote", "Issued and outstanding"],
                    ["Actions en circulation", "habiles à voter"],
                ),
                pattern=r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b",
                locators=["label", "table", "kv", "regex"],
            ),
            FieldSpec(
                name="directors",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels=_bilingual(
                    ["Nominee", "Director", "Nominees for election as directors"],
                    ["Candidat", "Administrateur"],
                ),
                validator="name",
                locators=["table", "label", "kv"],
            ),
            _auditor_field(),
            *_signatory_fields(),
        ],
        notes="Form 51-102F6 Statement of Executive Compensation is normally bound into the "
        "circular rather than filed alone, so it is a supporting anchor here rather than a "
        "doctype of its own.",
    ),
    DocTypeSpec(
        doctype_id="ca_ni_52_109_certification",
        label="Certification of Annual / Interim Filings (Form 52-109F1 / F2)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Certifying officer of a reporting issuer, under NI 52-109 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 52-109F1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("FORM 52-109F2", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 52-109A1", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("ANNEXE 52-109A2", decisive=True, controls=Controls.FORM_NUMBER),
            _a("CERTIFICATION OF ANNUAL FILINGS"),
            _a("CERTIFICATION OF INTERIM FILINGS"),
            _fr("ATTESTATION DES DOCUMENTS ANNUELS"),
            _a("National Instrument 52-109"),
            _fr("Règlement 52-109"),
            _a("internal control over financial reporting"),
        ],
        confusable_with={
            "ca_mda": "the certificate attests to the annual filings including the MD&A; it "
            "is one page and names a certifying officer",
        },
        negative_anchors=["SECURITIES AND EXCHANGE COMMISSION", "18 U.S.C. SECTION 1350"],
        fields=[
            _issuer_name_field(),
            _period_covered_field(required=True),
            _fiscal_year_end_field(),
            _filing_date_field(required=True),
            *_signatory_fields(
                name_labels_en=[
                    "Certifying officer",
                    "Chief Executive Officer",
                    "Chief Financial Officer",
                ]
            ),
        ],
        notes="One doctype covers the whole 52-109 certificate family — F1/F2 (full), the "
        "F1R/F2R venture-issuer variants and the F1 — IPO/RTO variant. They differ in which "
        "representations they carry and in the period certified, not in what a DD reviewer "
        "needs off them, and each variant's number is a decisive anchor of the same document.",
    ),
    DocTypeSpec(
        doctype_id="ca_ni_43_101_technical_report",
        label="NI 43-101 Technical Report (Form 43-101F1)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Qualified person, filed by a reporting issuer under NI 43-101 (CSA)",
        applies_to="corporate",
        anchors=[
            _a("FORM 43-101F1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("NATIONAL INSTRUMENT 43-101"),
            _a("NI 43-101", decisive=True, controls=Controls.STATUTE_TITLE),
            _fr("RÈGLEMENT 43-101", decisive=True, controls=Controls.STATUTE_TITLE),
            _a("Standards of Disclosure for Mineral Projects"),
            _a("qualified person"),
            _fr("personne qualifiée"),
            _a("CIM Definition Standards"),
            _a("Mineral Resource Estimate"),
        ],
        confusable_with={
            "ca_ni_51_101_oil_gas": "NI 43-101 governs mineral projects and expressly "
            "excludes petroleum and natural gas, which are NI 51-101's",
        },
        fields=[
            _issuer_name_field(),
            FieldSpec(
                name="property_name",
                attribute_key="property.name",
                type="string",
                required=True,
                labels=_bilingual(
                    ["Property", "Project", "Name of the property", "Property name"],
                    ["Propriété", "Projet", "Nom de la propriété"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="qualified_person",
                attribute_key="identity.full_name",
                type="name",
                multi=True,
                pii=True,
                labels=_bilingual(
                    ["Qualified Person", "Prepared by", "P.Geo.", "P.Eng."],
                    ["Personne qualifiée", "Préparé par"],
                ),
                validator="name",
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="effective_date",
                attribute_key="doc.issue_date",
                type="date",
                required=True,
                labels=_bilingual(
                    ["Effective Date", "Effective date of the report"],
                    ["Date de prise d'effet", "Date d'entrée en vigueur"],
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            _filing_date_field(),
            _jurisdiction_field(),
            *_listing_fields(),
        ],
        notes="``NI 43-101`` and ``NATIONAL INSTRUMENT 43-101`` are separate decisive anchors "
        "rather than one, because they tokenise differently and a report that prints only the "
        "abbreviation must still reach L1.",
    ),
    DocTypeSpec(
        doctype_id="ca_ni_51_101_oil_gas",
        label="NI 51-101 Oil and Gas Disclosure (Form 51-101F1 / F2 / F3)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer and its independent qualified reserves evaluator, "
        "under NI 51-101 (CSA)",
        applies_to="corporate",
        # ------------------------------------------------------------------
        # THIS DOCTYPE HAS NO DECISIVE ANCHOR, AND CANNOT HAVE ONE.
        #
        # It held five: ``FORM 51-101F1``, ``FORM 51-101F2``, ``FORM 51-101F3``,
        # ``NATIONAL INSTRUMENT 51-101`` and ``RÈGLEMENT 51-101``. Each is a CSA form or
        # instrument number, which is the strongest ground a decisive claim can have
        # (:attr:`dce.models.Controls.FORM_NUMBER`) — and every one of them is wrong here,
        # for a reason that is about the regulation rather than about any document.
        #
        # NI 51-101 s.2.1 requires a reporting issuer with oil and gas activities to file
        # Forms 51-101F1, F2 and F3 *annually*, and Form 51-102F2 Item 5.5 ("Companies with
        # Oil and Gas Activities") is where the AIF is told to carry that disclosure. So an
        # oil-and-gas issuer's ANNUAL INFORMATION FORM prints all three form numbers, the
        # instrument number, the form titles, ``future net revenue``, the ``COGE Handbook``
        # citation and the evaluator's name — not by coincidence but because the CSA
        # requires it to. This is CONTAINMENT: the 51-101 material is an enclosure, and
        # every string that identifies the enclosure appears in the enclosing document too.
        # Measured on this corpus, all five former decisive anchors match
        # ``corpus/ca/ca_aif__oilgas_issuer.pdf``, which is a ``ca_aif``; and
        # ``NATIONAL INSTRUMENT 51-101`` also matches the blank Form 51-102F3, whose
        # instructions cite it.
        #
        # The obvious repair — anchor on the standalone form's own TITLE instead of its
        # number — was tried and does not work, and the measurement is recorded here so
        # that nobody tries it again. Of the three titles NI 51-101 prescribes:
        # "Statement of Reserves Data and Other Oil and Gas Information" (F1) matches only
        # the standalone report; "Report on Reserves Data by Independent Qualified Reserves
        # Evaluator or Auditor" (F2) matches only the *AIF*; "Report of Management and
        # Directors on Oil and Gas Disclosure" (F3) matches both. That is containment one
        # level down — the AIF binds F2 and F3 in verbatim, titles included. There is no
        # string in the 51-101 family that a compliant oil-and-gas AIF does not also print.
        #
        # So the honest conclusion is the one at the top: this doctype owns no string that
        # only it prints, and therefore has no decisive anchor. All five are kept as
        # supporting anchors — the evidence is real, it is simply not exclusive — and this
        # doctype is now identified the ordinary way, by both channels concurring. On a
        # standalone reserves statement that is easy and stays easy: it holds every anchor
        # below and the AIF holds two or three. Inside an AIF it is correctly hard, because
        # inside an AIF this material is a schedule.
        anchors=[
            _a("FORM 51-101F1"),
            _a("FORM 51-101F2"),
            _a("FORM 51-101F3"),
            _a("NATIONAL INSTRUMENT 51-101"),
            _fr("RÈGLEMENT 51-101"),
            # The three form titles, for what they are worth as supporting evidence. They
            # are NOT a discriminator; the note above says why, with the measurement.
            _a("Statement of Reserves Data and Other Oil and Gas Information"),
            _a(
                "Report on Reserves Data by Independent Qualified Reserves Evaluator or "
                "Auditor"
            ),
            _a("Report of Management and Directors on Oil and Gas Disclosure"),
            _a("Statement of Reserves Data"),
            _a("Independent Qualified Reserves Evaluator"),
            _a("Standards of Disclosure for Oil and Gas Activities"),
            _a("future net revenue"),
            _a("COGE Handbook"),
        ],
        confusable_with={
            "ca_ni_43_101_technical_report": "NI 51-101 covers oil and gas; NI 43-101 covers "
            "mineral projects and excludes petroleum",
            "ca_aif": "CONTAINMENT, not confusion. An oil-and-gas issuer's AIF "
            "legitimately carries all three 51-101 form numbers and all three "
            "51-101 form titles, because NI 51-101 s.2.1 obliges the issuer to "
            "file F1/F2/F3 and Form 51-102F2 Item 5.5 is where they get bound "
            "in. Nothing in the 51-101 family distinguishes the two, which is "
            "why this doctype has no decisive anchor — see the note above its "
            "anchors. What distinguishes them is on the OTHER side: ca_aif "
            "declares the whole Form 51-102F2 item list, a genuine AIF prints "
            "most of it, and a standalone reserves statement prints none of it",
        },
        # There is deliberately no ``negative_anchors`` entry here any more.
        #
        # There was one: the ten Form 51-102F2 item headings, declared as evidence AGAINST
        # this doctype so that an AIF with the reserves disclosure bound in would be pushed
        # off it. That was compensation for evidence missing on the other side — ca_aif
        # declared four item headings, so the AIF could not win on its own merits and this
        # doctype had to be pushed down instead. ca_aif now declares the form's whole item
        # list and wins the anchor channel outright on both corpus AIFs on its own evidence
        # — 18.0 bits to 1.3 on ``ca_aif.htm``, 12.6 to 9.4 on the oil-and-gas one, with no
        # penalty applied to either — so the penalty has nothing left to do.
        #
        # Measured, not assumed: removing the ten negative anchors changes the outcome of
        # none of the four documents in this cluster — both AIFs and both standalone
        # reserves statements land exactly where they land with them. A control that cannot
        # change an outcome is a control that misinforms a reviewer about what is protecting
        # them, so it is gone rather than kept for comfort. The protection is now positive
        # evidence for the enclosing document, which is the thing that actually generalises:
        # it works on an AIF whatever subset of the item list that AIF happens to print.
        fields=[
            _issuer_name_field(),
            _fiscal_year_end_field(),
            _period_covered_field(),
            FieldSpec(
                name="reserves_evaluator",
                attribute_key="entity.auditor",
                type="name",
                labels=_bilingual(
                    [
                        "Independent Qualified Reserves Evaluator",
                        "Reserves Evaluator",
                        "Reserves Auditor",
                    ],
                    ["Évaluateur de réserves indépendant", "Évaluateur de réserves"],
                ),
                validator="name",
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="effective_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Effective Date", "as at"], ["Date de prise d'effet", "au"]
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="property_name",
                attribute_key="property.name",
                type="string",
                labels=_bilingual(["Property", "Field", "Area"], ["Propriété", "Champ"]),
                locators=_PROSE_LOCATORS,
            ),
            _filing_date_field(),
            *_signatory_fields(),
        ],
        notes="The three forms are filed together as one annual package (F1 the reserves data, "
        "F2 the evaluator's report on it, F3 management's and the directors' report), so they "
        "are one doctype with three decisive form numbers rather than three doctypes that "
        "would compete on every page of the same PDF.",
    ),
    DocTypeSpec(
        doctype_id="ca_early_warning_report",
        label="Early Warning Report (Form 62-103F1)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Acquiror of securities, filed on SEDAR+ under NI 62-103 (CSA)",
        applies_to="both",
        anchors=[
            _a("FORM 62-103F1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("NATIONAL INSTRUMENT 62-103", decisive=True, controls=Controls.STATUTE_TITLE),
            _fr("ANNEXE 62-103A1", decisive=True, controls=Controls.FORM_NUMBER),
            _fr("RÈGLEMENT 62-103", decisive=True, controls=Controls.STATUTE_TITLE),
            _a("EARLY WARNING REPORT"),
            _a("early warning requirements"),
            _fr("système d'alerte"),
            _a("the acquiror"),
        ],
        confusable_with={
            "ca_sedi_insider_report": "an insider report is filed by an insider through SEDI "
            "on Form 55-102F2; an early warning report is filed by any acquiror crossing 10%",
            "ca_material_change_report": "the MCR is the issuer's filing; the early warning "
            "report is the acquiror's",
        },
        negative_anchors=["SCHEDULE 13D", "SCHEDULE 13G", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            FieldSpec(
                name="acquiror_name",
                attribute_key="ownership.beneficial_owner",
                type="name",
                required=True,
                pii=True,
                labels=_bilingual(
                    ["Name of the acquiror", "Acquiror", "Name and address of the acquiror"],
                    ["Nom de l'acquéreur", "Acquéreur"],
                ),
                validator="name",
                locators=_PROSE_LOCATORS,
                notes="An acquiror may be a corporation or a natural person; the field is "
                "marked pii because it is often the latter.",
            ),
            _issuer_name_field(),
            FieldSpec(
                name="security_class",
                attribute_key="entity.security_class",
                type="string",
                labels=_bilingual(
                    ["Designation of the class", "Class of securities", "Designation"],
                    ["Désignation de la catégorie", "Catégorie de titres"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="securities_held",
                attribute_key="ownership.securities_held",
                type="number",
                labels=_bilingual(
                    ["Number of securities", "Number or principal amount", "securities held"],
                    ["Nombre de titres", "Nombre ou valeur nominale"],
                ),
                pattern=r"\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b",
                locators=["label", "table", "kv", "regex"],
            ),
            FieldSpec(
                name="percentage_of_class",
                attribute_key="ownership.share",
                type="string",
                labels=_bilingual(
                    ["Percentage of outstanding", "Percentage of the class"],
                    ["Pourcentage des titres en circulation", "Pourcentage de la catégorie"],
                ),
                pattern=r"\d{1,3}(?:\.\d{1,4})?\s?%",
                locators=["label", "table", "kv", "regex"],
            ),
            FieldSpec(
                name="transaction_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Date of the transaction", "Date of transaction"],
                    ["Date de l'opération"],
                ),
                validator="generic_date",
                locators=_PROSE_LOCATORS,
            ),
            _filing_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="ca_sedi_insider_report",
        label="SEDI Insider Report (Form 55-102F2)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Insider of a reporting issuer, filed through SEDI under NI 55-102 (CSA)",
        applies_to="individual",
        anchors=[
            _a("FORM 55-102F2", decisive=True, controls=Controls.FORM_NUMBER),
            _a(
                "SYSTEM FOR ELECTRONIC DISCLOSURE BY INSIDERS",
                decisive=True,
                controls=Controls.ISSUER_NAME,
            ),
            _a("NATIONAL INSTRUMENT 55-102", decisive=True, controls=Controls.STATUTE_TITLE),
            _fr(
                "SYSTÈME ÉLECTRONIQUE DE DÉCLARATION DES INITIÉS",
                decisive=True,
                controls=Controls.ISSUER_NAME,
            ),
            _a("Insider Report"),
            _fr("Déclaration d'initié"),
            _a("Nature of transaction"),
            _a("Ownership type"),
            _a("Insider's relationship to issuer"),
        ],
        confusable_with={
            "ca_early_warning_report": "an early warning report is filed by any acquiror "
            "crossing the 10% threshold, on Form 62-103F1",
        },
        negative_anchors=["FORM 4", "SECURITIES AND EXCHANGE COMMISSION"],
        fields=[
            _name_field(
                name="insider_name",
                key="identity.full_name",
                en=["Insider name", "Name of insider", "Insider"],
                fr=["Nom de l'initié", "Initié"],
            ),
            _issuer_name_field(),
            FieldSpec(
                name="relationship_to_issuer",
                attribute_key="ownership.signer_title",
                type="string",
                labels=_bilingual(
                    ["Insider's relationship to issuer", "Relationship to issuer"],
                    ["Lien de l'initié avec l'émetteur"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="security_class",
                attribute_key="entity.security_class",
                type="string",
                labels=_bilingual(
                    ["Security designation", "Class of securities"],
                    ["Désignation du titre", "Catégorie de titres"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="securities_held",
                attribute_key="ownership.securities_held",
                type="number",
                labels=_bilingual(
                    ["Balance of securities held", "Number of securities held"],
                    ["Solde des titres détenus", "Nombre de titres détenus"],
                ),
                pattern=r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b",
                locators=["table", "label", "kv", "regex"],
            ),
            FieldSpec(
                name="transaction_date",
                attribute_key="doc.issue_date",
                type="date",
                labels=_bilingual(
                    ["Date of transaction", "Transaction date"], ["Date de l'opération"]
                ),
                validator="generic_date",
                locators=["table", "label", "kv"],
            ),
            _filing_date_field(),
        ],
        handling="An insider report names a natural person and discloses their personal "
        "securityholdings. It is public on SEDI, which does not make it non-personal — the "
        "person fields stay pii-flagged downstream.",
    ),
    DocTypeSpec(
        doctype_id="ca_prospectus",
        label="Prospectus (long form / short form)",
        country="CA",
        category=Category.corporate,
        issuing_authority="Reporting issuer, receipted by a Canadian securities regulatory "
        "authority under NI 41-101 / NI 44-101",
        applies_to="corporate",
        anchors=[
            _a("FORM 41-101F1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("FORM 44-101F1", decisive=True, controls=Controls.FORM_NUMBER),
            _a("NATIONAL INSTRUMENT 41-101", decisive=True, controls=Controls.STATUTE_TITLE),
            _a("NATIONAL INSTRUMENT 44-101", decisive=True, controls=Controls.STATUTE_TITLE),
            _a(
                "No securities regulatory authority has expressed an opinion about these "
                "securities and it is an offence to claim otherwise",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("PRELIMINARY PROSPECTUS"),
            _a("SHORT FORM PROSPECTUS"),
            _fr("PROSPECTUS SIMPLIFIÉ"),
            _fr("Aucune autorité en valeurs mobilières ne s'est prononcée sur la qualité"),
            _a("a receipt for the prospectus"),
        ],
        confusable_with={
            "ca_aif": "a short form prospectus incorporates the AIF by reference; only the "
            "prospectus carries the regulator's no-opinion legend and a receipt",
            "ca_information_circular": "a circular solicits proxies; a prospectus qualifies a "
            "distribution of securities",
        },
        negative_anchors=[
            "SECURITIES AND EXCHANGE COMMISSION",
            "Securities Act of 1933",
            "RULE 424",
        ],
        fields=[
            _issuer_name_field(),
            _filing_date_field(),
            _jurisdiction_field(),
            *_listing_fields(),
            FieldSpec(
                name="prospectus_type",
                attribute_key="doc.reference_number",
                type="string",
                labels=_bilingual(
                    ["Preliminary Prospectus", "Short Form Prospectus", "Final Prospectus"],
                    ["Prospectus provisoire", "Prospectus simplifié", "Prospectus définitif"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            FieldSpec(
                name="security_class",
                attribute_key="entity.security_class",
                type="string",
                labels=_bilingual(
                    ["Class of securities", "Securities offered", "Offering"],
                    ["Catégorie de titres", "Titres offerts"],
                ),
                locators=_PROSE_LOCATORS,
            ),
            _amount_field(
                "offering_amount",
                key="account.amount_due",
                en=["Offering", "Aggregate offering", "Price to the public"],
                fr=["Montant de l'offre", "Prix au public"],
            ),
            _auditor_field(),
            _address_field(
                name="head_office",
                key="entity.registered_office",
                en=["Head office", "Registered office"],
                fr=["Siège social"],
            ),
        ],
        notes="The no-opinion legend is decisive because NI 41-101 prescribes its wording and "
        "requires it on the cover page of every Canadian prospectus — it is a regulator's "
        "string, not the issuer's. The word ``PROSPECTUS`` alone is not claimed at all: every "
        "securities regulator in the world uses it.",
    ),
    DocTypeSpec(
        doctype_id="ca_isc_register",
        label="Register of Individuals with Significant Control (CBCA s.21.1)",
        country="CA",
        category=Category.corporate,
        issuing_authority="The corporation itself — a CBCA s.21.1 / provincial-equivalent "
        "transparency register",
        applies_to="corporate",
        anchors=[
            _a(
                "REGISTER OF INDIVIDUALS WITH SIGNIFICANT CONTROL",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _fr(
                "REGISTRE DES PARTICULIERS AYANT UN CONTRÔLE IMPORTANT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("individual with significant control"),
            _fr("particulier ayant un contrôle important"),
            _a("significant number of shares"),
            _a("25% or more of the voting rights"),
            _a("control in fact"),
            _a("reasonable steps"),
        ],
        confusable_with={
            "ca_articles_incorporation_federal": "both cite the CBCA; only the ISC register "
            "lists individuals with significant control and their step-in dates",
            "ca_annual_return": "ISC information is delivered with the annual return but the "
            "register itself is a corporate record, not a registry filing",
        },
        negative_anchors=[
            "PERSONS WITH SIGNIFICANT CONTROL",
            "CERTIFICATE OF INCORPORATION",
            "ARTICLES OF INCORPORATION",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="corporation_number",
                attribute_key="doc.registration_number",
                type="id",
                labels=_bilingual(["Corporation number"], ["Numéro de la société"]),
                notes="Federal and provincial registries number corporations differently; no "
                "single format applies.",
            ),
            FieldSpec(
                name="individuals_with_significant_control",
                attribute_key="ownership.beneficial_owner",
                type="name",
                required=True,
                multi=True,
                pii=True,
                labels=_bilingual(
                    ["Name of individual", "Individual with significant control"],
                    ["Nom du particulier", "Particulier ayant un contrôle important"],
                ),
                validator="name",
                locators=["table", "label", "kv"],
            ),
            _dob_field(required=False),
            _address_field(
                name="isc_address",
                key="address.residential",
                en=["Residential address", "Last known address"],
                fr=["Adresse résidentielle", "Dernière adresse connue"],
            ),
            FieldSpec(
                name="nature_of_control",
                attribute_key="ownership.share",
                type="string",
                multi=True,
                labels=_bilingual(
                    ["Description of the significant control", "Nature of control"],
                    ["Description du contrôle important", "Nature du contrôle"],
                ),
                locators=["table", "label", "kv"],
            ),
            FieldSpec(
                name="date_control_began",
                attribute_key="doc.issue_date",
                type="date",
                multi=True,
                labels=_bilingual(
                    ["Date on which the individual became", "Date became an ISC"],
                    ["Date à laquelle le particulier est devenu"],
                ),
                validator="generic_date",
                locators=["table", "label", "kv"],
            ),
        ],
        handling="An ISC register is a list of natural persons with their residential "
        "addresses and dates of birth. Every person field here is pii and the whole document "
        "should be treated as a personal-data record despite being a corporate one.",
        notes="``REGISTER OF INDIVIDUALS WITH SIGNIFICANT CONTROL`` is the statutory title in "
        "CBCA s.21.1 and its provincial equivalents. The UK's register uses PERSONS, not "
        "INDIVIDUALS, which is why that string is a negative anchor rather than a synonym.",
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
            _fr("HYDRO-QUÉBEC", decisive=True, controls=Controls.ISSUER_NAME),
            _a("HYDRO ONE", decisive=True, controls=Controls.ISSUER_NAME),
            _a("BC HYDRO", decisive=True, controls=Controls.ISSUER_NAME),
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
            _a(
                "PROPERTY ASSESSMENT NOTICE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "MUNICIPAL PROPERTY ASSESSMENT CORPORATION",
                decisive=True,
                controls=Controls.ISSUER_NAME,
            ),
            _a("BC ASSESSMENT", decisive=True, controls=Controls.ISSUER_NAME),
            _fr(
                "AVIS D'ÉVALUATION FONCIÈRE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a(
                "RESIDENTIAL TENANCY AGREEMENT",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("STANDARD FORM OF LEASE", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _fr("BAIL DE LOGEMENT", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
