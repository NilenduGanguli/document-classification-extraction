"""Mexico doctype pack — 20 :class:`~dce.models.DocTypeSpec` entries.

Two things drive the design of this pack.

**Accents.** Mexican documents are printed with diacritics and OCR frequently drops them
("SITUACIÓN" comes back as "SITUACION"). Anchors are therefore stored *accented* and
NFC-normalised, and matching happens against the accent-folded skeleton produced by
``dce.normalize`` — so the accented anchor written here matches an unaccented read, and the
registry never has to carry two spellings of the same string.

**Language.** Anchors are Spanish, because that is what these documents print. English
anchors are declared only where the document itself prints English — the passport data
page's trilingual field labels, the bilingual statements the international banks issue, and
the consular card. Inventing an English "translation anchor" for a Spanish-only document
would be a fabricated string that can only produce false positives. Field *labels*, by
contrast, carry both languages: certified English translations of Mexican documents are
routinely submitted in KYC packs, and a label that never appears simply never matches.

The rest of the conventions are the ones documented in :mod:`dce.registry.usa`: decisive
anchors stay unique across the registry (the SAT header appears on four documents in this
pack, so it is *not* decisive — the form's own title is), validators are declared before
they are used, and an unpublished format goes in ``notes`` rather than into a regex.

``officially_valid`` marks the credentials Mexican financial institutions accept as
*identificación oficial* under the CNBV rules: INE/IFE, passport, cédula profesional,
cartilla del servicio militar, matrícula consular and the INM residence card.
"""

from __future__ import annotations

from importlib import import_module

from dce.models import Anchor, Category, DocTypeSpec, FieldSpec, Zone

try:  # pragma: no cover - the loader is authored alongside this pack
    from dce.registry import loader as _loader
except ImportError:  # pragma: no cover - the pack stays importable on its own
    _loader = None


# ---------------------------------------------------------------------------
# Namespace declarations — see the long note in dce.registry.usa. Shared entries are
# repeated verbatim across the NA packs so any one of them can be imported alone.
# ---------------------------------------------------------------------------
ATTRIBUTE_KEY_EXTENSIONS: dict[str, str] = {
    "id.curp": "Clave Única de Registro de Población",
    "id.rfc": "Registro Federal de Contribuyentes",
    "id.ine_clave_elector": "INE/IFE Clave de Elector",
    "id.ine_cic": "INE card identifier (CIC) from the reverse machine-readable block",
    "id.cedula_profesional": "Cédula profesional number (Dirección General de Profesiones)",
    "id.matricula_consular": "Matrícula consular de alta seguridad number",
    "id.passport_number": "Passport number",
    "identity.profession": "Profession or degree recorded on a professional credential",
    "account.clabe": "CLABE — the 18-digit Mexican interbank account key",
    "account.statement_period": "Period covered by an account statement",
    "account.amount_due": "Amount payable on a statement or bill",
    "property.roll_number": "Municipal assessment roll number",
    "property.assessed_value": "Assessed value of a property",
    "doc.mrz": "Machine-readable zone exactly as printed (ICAO 9303)",
    "doc.notary": "Notary (fedatario público) before whom an instrument was executed",
    "doc.immigration_category": "Immigration category code printed on an immigration document",
    "entity.jurisdiction": "State / province / country of incorporation or organisation",
    "entity.status": "Registry status of the entity (good standing, active, dissolved)",
}

#: ``name -> what the validator must enforce``. ``curp`` and ``rfc`` are Mexico's; the rest
#: are the shared North-American value validators, declared identically in every NA pack.
VALIDATOR_EXTENSIONS: dict[str, str] = {
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
#: CURP — 4 letters, YYMMDD, sex, 5 consonants, 1 alphanumeric, 1 check digit. "X" is
#: accepted in the sex position: RENAPO issues non-binary CURPs and hard-rejecting them
#: would silently exclude real people.
CURP_PATTERN = r"\b[A-Z]{4}\d{6}[HMX][A-Z]{5}[0-9A-Z]\d\b"
#: RFC — 3 letters (moral) or 4 (física) + YYMMDD + 3-character homoclave.
RFC_PATTERN = r"\b[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{3}\b"
#: Clave de Elector — 6 letters + YYMMDD + 2-digit state + sex + 3 digits.
CLAVE_ELECTOR_PATTERN = r"\b[A-Z]{6}\d{8}[HM]\d{3}\b"
#: Reverse-side machine-readable block of an INE card.
IDMEX_PATTERN = r"IDMEX\d{9,13}"
#: ICAO 9303 TD3 first line of a Mexican passport.
MRZ_TD3_MEX = r"P[<K]MEX[A-Z0-9<]{5,}"
#: CLABE — 18 digits.
CLABE_PATTERN = r"\b\d{18}\b"
CURRENCY_PATTERN = r"\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?"


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------
def _a(
    text: str,
    *,
    lang: str = "es",
    decisive: bool = False,
    zone: Zone | None = None,
) -> Anchor:
    """Build a Spanish :class:`~dce.models.Anchor` (the default language of this pack)."""
    return Anchor(text=text, lang=lang, decisive=decisive, zone=zone)


def _en(text: str, *, decisive: bool = False, zone: Zone | None = None) -> Anchor:
    """Build an English anchor — only for text the document itself prints in English."""
    return Anchor(text=text, lang="en", decisive=decisive, zone=zone)


def _labels(es: list[str], en: list[str] | None = None) -> dict[str, list[str]]:
    """Label map. English labels serve the certified translations KYC packs carry."""
    return {"es": es, "en": en} if en else {"es": es}


def _name_field(
    *,
    name: str = "full_name",
    key: str = "identity.full_name",
    required: bool = True,
    es: list[str] | None = None,
    en: list[str] | None = None,
) -> FieldSpec:
    """A person's printed name."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="name",
        required=required,
        pii=True,
        labels=_labels(
            es or ["Nombre", "Nombre completo", "Apellido paterno", "Apellido materno"],
            en or ["Name", "Full name", "Surname"],
        ),
        validator="name",
    )


def _curp_field(*, required: bool = True) -> FieldSpec:
    """CURP — the closest thing Mexico has to a universal personal identifier."""
    return FieldSpec(
        name="curp",
        attribute_key="id.curp",
        type="id",
        required=required,
        pii=True,
        labels=_labels(["CURP", "Clave Única de Registro de Población"], ["CURP"]),
        pattern=CURP_PATTERN,
        validator="curp",
    )


def _rfc_field(*, required: bool = True) -> FieldSpec:
    """RFC — the taxpayer registry key, held by both people and companies."""
    return FieldSpec(
        name="rfc",
        attribute_key="id.rfc",
        type="id",
        required=required,
        pii=True,
        labels=_labels(
            ["RFC", "Registro Federal de Contribuyentes"], ["RFC", "Taxpayer registry number"]
        ),
        pattern=RFC_PATTERN,
        validator="rfc",
    )


def _dob_field(*, required: bool = True) -> FieldSpec:
    """Date of birth."""
    return FieldSpec(
        name="date_of_birth",
        attribute_key="identity.date_of_birth",
        type="date",
        required=required,
        pii=True,
        labels=_labels(["Fecha de nacimiento", "Nacimiento"], ["Date of birth"]),
        validator="generic_date",
    )


def _sex_field() -> FieldSpec:
    """Sex / gender marker."""
    return FieldSpec(
        name="sex",
        attribute_key="identity.sex",
        type="string",
        pii=True,
        labels=_labels(["Sexo"], ["Sex"]),
        pattern=r"^[HMFXhmfx]$",
        notes="Mexican documents print H (hombre) / M (mujer); the normaliser maps H to M "
        "and M to F, so the raw letter must not be stored as an ISO sex code.",
    )


def _address_field(
    *,
    name: str = "address",
    key: str = "address.residential",
    required: bool = False,
    es: list[str] | None = None,
    en: list[str] | None = None,
) -> FieldSpec:
    """A postal address."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="address",
        required=required,
        pii=True,
        labels=_labels(
            es or ["Domicilio", "Dirección", "Calle", "Colonia"],
            en or ["Address"],
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
        labels=_labels(["Fecha de expedición", "Fecha de emisión", "Expedida"], ["Date of issue"]),
        validator="generic_date",
    )


def _expiry_date_field(*, required: bool = False) -> FieldSpec:
    """Document expiry / validity date."""
    return FieldSpec(
        name="expiry_date",
        attribute_key="doc.expiry_date",
        type="date",
        required=required,
        labels=_labels(
            ["Vigencia", "Válida hasta", "Fecha de vencimiento"], ["Valid until", "Expiry date"]
        ),
        validator="generic_date",
    )


def _amount_field(
    name: str,
    *,
    key: str,
    es: list[str],
    en: list[str] | None = None,
    required: bool = False,
) -> FieldSpec:
    """A currency amount pulled from a labelled box."""
    return FieldSpec(
        name=name,
        attribute_key=key,
        type="number",
        required=required,
        labels=_labels(es, en),
        pattern=CURRENCY_PATTERN,
        validator="amount",
        locators=["table", "kv", "label", "regex"],
    )


def _entity_name_field(*, required: bool = True) -> FieldSpec:
    """Razón social — the legal name of a company."""
    return FieldSpec(
        name="entity_legal_name",
        attribute_key="entity.legal_name",
        type="name",
        required=required,
        labels=_labels(
            ["Razón social", "Denominación social", "Nombre de la sociedad"],
            ["Legal name", "Company name"],
        ),
        validator="name",
    )


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------
SPECS: tuple[DocTypeSpec, ...] = (
    # ---------------------------------------------------------------- identity
    DocTypeSpec(
        doctype_id="mx_ine",
        label="Credencial para Votar (INE / IFE)",
        country="MX",
        category=Category.identity,
        issuing_authority="Instituto Nacional Electoral (INE), formerly the Instituto "
        "Federal Electoral (IFE)",
        officially_valid=True,
        anchors=[
            _a("INSTITUTO NACIONAL ELECTORAL", decisive=True),
            _a("CREDENCIAL PARA VOTAR", decisive=True),
            _a("INSTITUTO FEDERAL ELECTORAL", decisive=True),
            _a("CLAVE DE ELECTOR"),
            _a("SECCIÓN"),
            _a("VIGENCIA"),
            _a("MUNICIPIO"),
            _a("LOCALIDAD"),
            _a("IDMEX"),
        ],
        id_patterns=[CLAVE_ELECTOR_PATTERN, CURP_PATTERN, IDMEX_PATTERN],
        confusable_with={
            "mx_matricula_consular": "the INE is issued inside Mexico by the electoral "
            "authority and carries a CLAVE DE ELECTOR and a "
            "SECCIÓN; the matrícula consular is issued by a "
            "Mexican consulate abroad and says MATRÍCULA CONSULAR "
            "DE ALTA SEGURIDAD and SECRETARÍA DE RELACIONES "
            "EXTERIORES",
            "mx_curp_constancia": "both print a CURP, but the constancia is a RENAPO "
            "printout with no photo and no clave de elector",
        },
        negative_anchors=[
            "MATRÍCULA CONSULAR",
            "SECRETARÍA DE RELACIONES EXTERIORES",
            "INSTITUTO NACIONAL DE MIGRACIÓN",
        ],
        fields=[
            _name_field(),
            FieldSpec(
                name="clave_elector",
                attribute_key="id.ine_clave_elector",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Clave de elector"], ["Voter key"]),
                pattern=CLAVE_ELECTOR_PATTERN,
                notes="18 characters: six letters from the names, the birth date, the "
                "two-digit state code, the sex marker and a three-digit "
                "disambiguator. No public check digit — do not treat a well-formed "
                "clave as verified.",
            ),
            _curp_field(required=False),
            _dob_field(),
            _sex_field(),
            _address_field(required=True, es=["Domicilio", "Calle", "Colonia", "Municipio"]),
            FieldSpec(
                name="cic",
                attribute_key="id.ine_cic",
                type="id",
                pii=True,
                labels=_labels(["CIC", "IDMEX"]),
                pattern=IDMEX_PATTERN,
                locators=["regex", "label", "kv"],
                notes="The CIC is the card identifier in the reverse machine-readable "
                "block; it changes when the card is reissued, so it identifies the "
                "card, not the person.",
            ),
            FieldSpec(
                name="seccion",
                attribute_key="",
                type="string",
                labels=_labels(["Sección"]),
                notes="Electoral section. Kept without an attribute key on purpose: it is "
                "useful for review and for matching a reissued card, but it is not a "
                "customer attribute worth merging into the fleet ontology.",
            ),
            _expiry_date_field(required=True),
        ],
        handling="INE data is personal data under the Ley Federal de Protección de Datos "
        "Personales en Posesión de los Particulares; the clave de elector and CURP "
        "must be stored under the same controls as any national identifier.",
    ),
    DocTypeSpec(
        doctype_id="mx_passport",
        label="Pasaporte Mexicano",
        country="MX",
        category=Category.identity,
        issuing_authority="Secretaría de Relaciones Exteriores (SRE)",
        officially_valid=True,
        handling="Personal data under the LFPDPPP: retain the extracted fields rather than the "
        "data page image.",
        anchors=[
            _a("P<MEX", decisive=True),
            _a("PASAPORTE", decisive=True, zone=Zone.title),
            _a("ESTADOS UNIDOS MEXICANOS"),
            _a("SECRETARÍA DE RELACIONES EXTERIORES"),
            _en("PASSPORT", zone=Zone.title),
            _en("Surname"),
            _en("Nationality"),
        ],
        id_patterns=[MRZ_TD3_MEX],
        confusable_with={
            "us_passport": "the US book's MRZ starts P<USA",
            "mx_matricula_consular": "both are issued by the SRE, but only the passport "
            "carries a TD3 machine-readable zone",
        },
        negative_anchors=["P<USA", "P<CAN", "MATRÍCULA CONSULAR"],
        fields=[
            FieldSpec(
                name="machine_readable_zone",
                attribute_key="doc.mrz",
                type="string",
                pii=True,
                validator="mrz_td3",
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
                labels=_labels(["Apellidos"], ["Surname"]),
                validator="name",
                locators=["mrz", "kv", "label"],
            ),
            FieldSpec(
                name="given_names",
                attribute_key="identity.given_names",
                type="name",
                required=True,
                pii=True,
                labels=_labels(["Nombre(s)"], ["Given names"]),
                validator="name",
                locators=["mrz", "kv", "label"],
            ),
            FieldSpec(
                name="passport_number",
                attribute_key="id.passport_number",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["No. de pasaporte", "Número de pasaporte"], ["Passport No."]),
                locators=["mrz", "kv", "label"],
                notes="Mexican passport numbers are nine alphanumeric characters. No public "
                "check digit exists outside the MRZ.",
            ),
            FieldSpec(
                name="nationality",
                attribute_key="identity.nationality",
                type="string",
                labels=_labels(["Nacionalidad"], ["Nationality"]),
                locators=["mrz", "kv", "label"],
            ),
            _curp_field(required=False),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                pii=True,
                labels=_labels(["Lugar de nacimiento"], ["Place of birth"]),
            ),
            _expiry_date_field(required=True),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_curp_constancia",
        label="Constancia de la CURP (RENAPO)",
        country="MX",
        category=Category.identity,
        issuing_authority="Registro Nacional de Población (RENAPO), Secretaría de Gobernación",
        anchors=[
            _a("CLAVE ÚNICA DE REGISTRO DE POBLACIÓN", decisive=True),
            _a("CONSTANCIA DE LA CURP", decisive=True),
            _a("REGISTRO NACIONAL DE POBLACIÓN", decisive=True),
            _a("RENAPO"),
            _a("Entidad de registro"),
            _a("Folio"),
        ],
        id_patterns=[CURP_PATTERN],
        confusable_with={
            "mx_ine": "the constancia is a plain RENAPO printout with no photo and no "
            "clave de elector",
            "mx_acta_nacimiento": "the birth record is issued by a Registro Civil and "
            "names both parents; the constancia only restates the CURP",
        },
        negative_anchors=["CREDENCIAL PARA VOTAR", "ACTA DE NACIMIENTO"],
        fields=[
            _curp_field(),
            _name_field(),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="entidad_registro",
                attribute_key="address.state",
                type="string",
                labels=_labels(
                    ["Entidad de registro", "Entidad federativa"], ["State of registration"]
                ),
            ),
            FieldSpec(
                name="folio",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Folio"], ["Reference"]),
                notes="The RENAPO folio identifies the printout, not the person: it changes on "
                "every reprint.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_acta_nacimiento",
        label="Acta de Nacimiento",
        country="MX",
        category=Category.identity,
        issuing_authority="Registro Civil of the state of registration",
        anchors=[
            _a("ACTA DE NACIMIENTO", decisive=True),
            _a("REGISTRO CIVIL"),
            _a("Oficialía"),
            _a("Libro"),
            _a("Foja"),
            _a("Datos del registrado"),
            _a("Cadena original"),
        ],
        id_patterns=[CURP_PATTERN],
        confusable_with={
            "us_birth_certificate": "the US record is titled CERTIFICATE OF LIVE BIRTH and "
            "names a state vital-records office",
            "mx_curp_constancia": "the acta records the birth itself, with parents and a "
            "registry entry; the constancia only restates the CURP",
        },
        negative_anchors=["CERTIFICATE OF LIVE BIRTH", "Vital Records"],
        fields=[
            _name_field(es=["Nombre del registrado", "Nombre"], en=["Name"]),
            _curp_field(required=False),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                required=True,
                pii=True,
                labels=_labels(
                    ["Lugar de nacimiento", "Entidad de nacimiento"], ["Place of birth"]
                ),
            ),
            FieldSpec(
                name="mother_name",
                attribute_key="identity.mother_name",
                type="name",
                pii=True,
                labels=_labels(["Madre", "Nombre de la madre"], ["Mother"]),
                validator="name",
            ),
            FieldSpec(
                name="father_name",
                attribute_key="identity.father_name",
                type="name",
                pii=True,
                labels=_labels(["Padre", "Nombre del padre"], ["Father"]),
                validator="name",
            ),
            FieldSpec(
                name="folio",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Folio", "Número de acta"], ["Reference"]),
                notes="Acta folios are assigned by the registry office and their composition "
                "differs by state.",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_cedula_profesional",
        label="Cédula Profesional",
        country="MX",
        category=Category.identity,
        issuing_authority="Dirección General de Profesiones, Secretaría de Educación Pública (SEP)",
        officially_valid=True,
        handling="Acceptable as identificación oficial. It evidences a professional licence — not "
        "nationality, and not address.",
        anchors=[
            _a("CÉDULA PROFESIONAL", decisive=True),
            _a("DIRECCIÓN GENERAL DE PROFESIONES", decisive=True),
            _a("SECRETARÍA DE EDUCACIÓN PÚBLICA"),
            _a("Profesión"),
            _a("Número de cédula"),
        ],
        confusable_with={
            "mx_ine": "the cédula certifies a professional licence and carries no clave de "
            "elector or address",
        },
        fields=[
            _name_field(),
            FieldSpec(
                name="cedula_number",
                attribute_key="id.cedula_profesional",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Número de cédula", "Cédula"], ["Licence number"]),
                notes="Cédula numbers are 7 or 8 digits and have grown in length over time; "
                "no check digit is published, so only the digit shape is meaningful.",
            ),
            FieldSpec(
                name="profession",
                attribute_key="identity.profession",
                type="string",
                required=True,
                labels=_labels(["Profesión", "Carrera", "Título"], ["Profession", "Degree"]),
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_cartilla_militar",
        label="Cartilla del Servicio Militar Nacional",
        country="MX",
        category=Category.identity,
        issuing_authority="Secretaría de la Defensa Nacional (SEDENA)",
        officially_valid=True,
        handling="Acceptable as identificación oficial. It is issued only to men of conscription "
        "age, so its absence carries no signal.",
        anchors=[
            _a("CARTILLA DEL SERVICIO MILITAR NACIONAL", decisive=True),
            _a("SERVICIO MILITAR NACIONAL", decisive=True),
            _a("SECRETARÍA DE LA DEFENSA NACIONAL", decisive=True),
            _a("SEDENA"),
            _a("Matrícula"),
            _a("Clase"),
        ],
        confusable_with={
            "mx_ine": "the cartilla is issued by SEDENA to men of conscription age and "
            "carries a matrícula rather than a clave de elector",
        },
        fields=[
            _name_field(),
            _dob_field(),
            FieldSpec(
                name="matricula",
                attribute_key="doc.reference_number",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Matrícula", "Número de matrícula"], ["Service number"]),
                notes="SEDENA does not publish the matrícula's composition; no format is asserted.",
            ),
            FieldSpec(
                name="clase",
                attribute_key="",
                type="string",
                labels=_labels(["Clase"]),
                notes="The conscription 'class' is the year cohort. No attribute key: it "
                "is document-local context, not a customer attribute.",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_matricula_consular",
        label="Matrícula Consular de Alta Seguridad (MCAS)",
        country="MX",
        category=Category.identity,
        issuing_authority="Secretaría de Relaciones Exteriores, via a Mexican consulate abroad",
        officially_valid=True,
        handling="Personal data under the LFPDPPP. The address shown is a residence abroad, which "
        "is the point of the document — do not treat it as a Mexican address.",
        anchors=[
            _a("MATRÍCULA CONSULAR DE ALTA SEGURIDAD", decisive=True),
            _a("MATRÍCULA CONSULAR", decisive=True),
            _a("CONSULADO GENERAL DE MÉXICO"),
            _a("SECRETARÍA DE RELACIONES EXTERIORES"),
            _en("CONSULAR IDENTIFICATION"),
        ],
        confusable_with={
            "mx_ine": "the matrícula is issued abroad by a consulate and shows a foreign "
            "residential address; the INE is issued inside Mexico and carries a "
            "CLAVE DE ELECTOR",
            "mx_passport": "both come from the SRE, but only the passport has a TD3 "
            "machine-readable zone",
        },
        negative_anchors=[
            "CREDENCIAL PARA VOTAR",
            "CLAVE DE ELECTOR",
            "INSTITUTO NACIONAL ELECTORAL",
        ],
        fields=[
            _name_field(),
            FieldSpec(
                name="matricula_number",
                attribute_key="id.matricula_consular",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Matrícula", "Número de matrícula"], ["Registration number"]),
                notes="The MCAS number's composition is not published by the SRE; no "
                "pattern is asserted.",
            ),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="place_of_birth",
                attribute_key="identity.place_of_birth",
                type="string",
                pii=True,
                labels=_labels(["Lugar de nacimiento"], ["Place of birth"]),
            ),
            _address_field(
                required=True,
                es=["Domicilio en el extranjero", "Domicilio"],
                en=["Address in the United States", "Address"],
            ),
            _issue_date_field(),
            _expiry_date_field(required=True),
        ],
        notes="The MCAS is accepted as identification by many US institutions and by "
        "Mexican consular services; it is not a travel document.",
    ),
    DocTypeSpec(
        doctype_id="mx_tarjeta_residente",
        label="Tarjeta de Residente (temporal o permanente)",
        country="MX",
        category=Category.identity,
        issuing_authority="Instituto Nacional de Migración (INM)",
        officially_valid=True,
        handling="Immigration status lapses with the card; record the expiry and re-verify at it.",
        anchors=[
            _a("TARJETA DE RESIDENTE PERMANENTE", decisive=True),
            _a("TARJETA DE RESIDENTE TEMPORAL", decisive=True),
            _a("INSTITUTO NACIONAL DE MIGRACIÓN", decisive=True),
            _a("Condición de estancia"),
            _a("Número único de trámite"),
            _a("NUT"),
        ],
        id_patterns=[CURP_PATTERN],
        confusable_with={
            "us_green_card": "the INM card names the Instituto Nacional de Migración and "
            "states a condición de estancia",
            "mx_ine": "only Mexican nationals hold an INE credential; the residence card is "
            "issued to foreign nationals",
        },
        negative_anchors=["USCIS", "PERMANENT RESIDENT CARD", "CREDENCIAL PARA VOTAR"],
        fields=[
            _name_field(),
            _curp_field(required=False),
            _dob_field(),
            _sex_field(),
            FieldSpec(
                name="nationality",
                attribute_key="identity.nationality",
                type="string",
                required=True,
                labels=_labels(["Nacionalidad"], ["Nationality"]),
            ),
            FieldSpec(
                name="condicion_estancia",
                attribute_key="doc.immigration_category",
                type="string",
                required=True,
                labels=_labels(["Condición de estancia"], ["Immigration status"]),
            ),
            FieldSpec(
                name="nut",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Número único de trámite", "NUT"], ["Case number"]),
                notes="The NUT is an INM case reference whose composition is not published.",
            ),
            _expiry_date_field(required=True),
        ],
    ),
    # ---------------------------------------------------------------------- tax
    DocTypeSpec(
        doctype_id="mx_rfc_csf",
        label="Constancia de Situación Fiscal (RFC)",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administración Tributaria (SAT)",
        applies_to="both",
        anchors=[
            _a("CONSTANCIA DE SITUACIÓN FISCAL", decisive=True),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("REGISTRO FEDERAL DE CONTRIBUYENTES"),
            _a("Datos de identificación del contribuyente"),
            _a("Régimen"),
            _a("idCIF"),
            _a("Domicilio fiscal"),
        ],
        id_patterns=[RFC_PATTERN, CURP_PATTERN],
        confusable_with={
            "mx_cif": "the CSF is the multi-page fiscal-status statement; the cédula de "
            "identificación fiscal is the one-page card with the QR code and the "
            "idCIF",
            "mx_opinion_cumplimiento": "the opinión states whether obligations are up to "
            "date; the constancia states who the taxpayer is",
        },
        negative_anchors=[
            "CÉDULA DE IDENTIFICACIÓN FISCAL",
            "OPINIÓN DEL CUMPLIMIENTO DE OBLIGACIONES FISCALES",
        ],
        fields=[
            _rfc_field(),
            _curp_field(required=False),
            _name_field(required=False),
            _entity_name_field(required=False),
            _address_field(
                key="address.registered",
                required=True,
                es=["Domicilio fiscal", "Vialidad", "Colonia", "Código postal"],
                en=["Registered address"],
            ),
            FieldSpec(
                name="regimen",
                attribute_key="entity.constitution",
                type="string",
                labels=_labels(["Régimen", "Regímenes"], ["Tax regime"]),
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="status",
                attribute_key="entity.status",
                type="string",
                labels=_labels(["Estatus en el padrón", "Situación del contribuyente"], ["Status"]),
            ),
            FieldSpec(
                name="start_of_operations",
                attribute_key="entity.incorporation_date",
                type="date",
                labels=_labels(["Fecha de inicio de operaciones"], ["Start of operations"]),
                validator="generic_date",
            ),
            FieldSpec(
                name="idcif",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["idCIF"]),
                notes="The idCIF identifies the printed copy, not the taxpayer — it changes on "
                "each reprint, so never use it as a customer key.",
            ),
        ],
        handling="A CSF carries the taxpayer's fiscal address and RFC; both are personal "
        "data for a persona física.",
    ),
    DocTypeSpec(
        doctype_id="mx_cif",
        label="Cédula de Identificación Fiscal (CIF)",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administración Tributaria (SAT)",
        applies_to="both",
        anchors=[
            _a("CÉDULA DE IDENTIFICACIÓN FISCAL", decisive=True),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("REGISTRO FEDERAL DE CONTRIBUYENTES"),
            _a("idCIF"),
        ],
        id_patterns=[RFC_PATTERN],
        confusable_with={
            "mx_rfc_csf": "the CIF is a single page with the QR code; the constancia runs "
            "to several pages and lists regimes and obligations",
        },
        negative_anchors=["CONSTANCIA DE SITUACIÓN FISCAL", "Régimen"],
        fields=[
            _rfc_field(),
            _name_field(required=False),
            _entity_name_field(required=False),
            FieldSpec(
                name="idcif",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["idCIF"]),
                notes="The idCIF identifies the printed copy, not the taxpayer — it changes on "
                "each reprint.",
            ),
            _issue_date_field(),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_efirma_certificado",
        label="Certificado de e.firma (FIEL)",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administración Tributaria (SAT)",
        applies_to="both",
        anchors=[
            _a("CERTIFICADO DE FIRMA ELECTRÓNICA AVANZADA", decisive=True),
            _a("FIRMA ELECTRÓNICA AVANZADA", decisive=True),
            _a("e.firma"),
            _a("Número de serie del certificado"),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("Vigencia del certificado"),
        ],
        id_patterns=[RFC_PATTERN],
        confusable_with={
            "mx_rfc_csf": "the e.firma certificate proves possession of a signing key; the "
            "constancia states fiscal status",
        },
        fields=[
            _rfc_field(),
            _name_field(required=False),
            _entity_name_field(required=False),
            FieldSpec(
                name="serial_number",
                attribute_key="doc.reference_number",
                type="id",
                required=True,
                labels=_labels(
                    ["Número de serie del certificado", "Número de serie"],
                    ["Certificate serial number"],
                ),
                notes="SAT certificate serials are 20 digits in current issues, but older "
                "certificates used other lengths — no pattern is asserted.",
            ),
            _issue_date_field(),
            _expiry_date_field(required=True),
        ],
        notes="The e.firma itself is a .cer/.key pair, not a document. What reaches this "
        "service is the printed acuse or certificate summary, which is what these "
        "anchors describe.",
    ),
    DocTypeSpec(
        doctype_id="mx_opinion_cumplimiento",
        label="Opinión del Cumplimiento de Obligaciones Fiscales (32-D)",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administración Tributaria (SAT)",
        applies_to="both",
        anchors=[
            _a("OPINIÓN DEL CUMPLIMIENTO DE OBLIGACIONES FISCALES", decisive=True),
            _a("Sentido de la opinión", decisive=True),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("Positivo"),
            _a("Negativo"),
            _a("32-D"),
        ],
        id_patterns=[RFC_PATTERN],
        confusable_with={
            "mx_rfc_csf": "the opinión answers 'are this taxpayer's obligations current?'; "
            "the constancia answers 'who is this taxpayer?'",
        },
        negative_anchors=["CONSTANCIA DE SITUACIÓN FISCAL"],
        fields=[
            _rfc_field(),
            _name_field(required=False),
            _entity_name_field(required=False),
            FieldSpec(
                name="sentido",
                attribute_key="entity.status",
                type="string",
                required=True,
                labels=_labels(["Sentido de la opinión", "Opinión"], ["Result"]),
                notes="'Positivo' means obligations are current; 'Negativo' or 'No inscrito' "
                "is an adverse result and should route to review.",
            ),
            FieldSpec(
                name="folio",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Folio"], ["Reference"]),
                notes="The folio identifies this opinion; a fresh one is issued every time the "
                "opinion is requested.",
            ),
            _issue_date_field(required=True),
        ],
    ),
    # ---------------------------------------------------------------- corporate
    DocTypeSpec(
        doctype_id="mx_acta_constitutiva",
        label="Acta Constitutiva (escritura de constitución de sociedad)",
        country="MX",
        category=Category.corporate,
        issuing_authority="Notario público; registered with the Registro Público de Comercio",
        applies_to="corporate",
        anchors=[
            _a("ACTA CONSTITUTIVA", decisive=True),
            _a("CONSTITUCIÓN DE SOCIEDAD", decisive=True),
            _a("ESCRITURA PÚBLICA"),
            _a("NOTARIO PÚBLICO"),
            _a("FOLIO MERCANTIL"),
            _a("REGISTRO PÚBLICO DE COMERCIO"),
            _a("OBJETO SOCIAL"),
            _a("CAPITAL SOCIAL"),
            _a("SOCIEDAD ANÓNIMA DE CAPITAL VARIABLE"),
        ],
        confusable_with={
            "mx_poder_notarial": "both are notarial instruments; the acta constitutiva "
            "creates the company, the poder grants an agent authority "
            "on its behalf",
            "us_articles_incorporation": "the US filing is made with a Secretary of State, "
            "not before a notary",
        },
        negative_anchors=["PODER GENERAL", "ARTICLES OF INCORPORATION"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="incorporation_date",
                attribute_key="entity.incorporation_date",
                type="date",
                required=True,
                labels=_labels(["Fecha de constitución", "Fecha"], ["Date of constitution"]),
                validator="generic_date",
            ),
            FieldSpec(
                name="objeto_social",
                attribute_key="entity.objects",
                type="string",
                labels=_labels(["Objeto social"], ["Corporate purpose"]),
            ),
            _amount_field(
                "capital_social",
                key="entity.authorised_capital",
                es=["Capital social", "Capital mínimo fijo"],
                en=["Share capital"],
            ),
            FieldSpec(
                name="socios",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                required=True,
                pii=True,
                labels=_labels(["Socios", "Accionistas", "Socio"], ["Shareholders", "Members"]),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="administradores",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                labels=_labels(
                    ["Administrador único", "Consejo de administración", "Administradores"],
                    ["Directors"],
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="notario",
                attribute_key="doc.notary",
                type="name",
                labels=_labels(["Notario público", "Notario", "Fedatario"], ["Notary"]),
                validator="name",
            ),
            FieldSpec(
                name="escritura_number",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Escritura número", "Instrumento número"], ["Instrument number"]),
                notes="Instrument numbers run sequentially per notary, so they are unique only "
                "together with the notary's number and state.",
            ),
            FieldSpec(
                name="folio_mercantil",
                attribute_key="doc.registration_number",
                type="id",
                labels=_labels(
                    ["Folio mercantil", "Folio mercantil electrónico"],
                    ["Commercial registry folio"],
                ),
                notes="The folio mercantil electrónico is assigned by the Registro Público de "
                "Comercio and is formatted differently by each state registry.",
            ),
            _address_field(
                name="domicilio_social",
                key="entity.registered_office",
                es=["Domicilio social"],
                en=["Registered office"],
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_poder_notarial",
        label="Poder Notarial (power of attorney)",
        country="MX",
        category=Category.corporate,
        issuing_authority="Notario público",
        applies_to="corporate",
        anchors=[
            _a("PODER GENERAL PARA PLEITOS Y COBRANZAS", decisive=True),
            _a("PODER GENERAL PARA ACTOS DE ADMINISTRACIÓN", decisive=True),
            _a("PODER GENERAL PARA ACTOS DE DOMINIO", decisive=True),
            _a("PODER NOTARIAL", decisive=True),
            _a("APODERADO"),
            _a("MANDANTE"),
            _a("NOTARIO PÚBLICO"),
            _a("FACULTADES"),
        ],
        confusable_with={
            "mx_acta_constitutiva": "the acta constitutiva creates the company; a poder "
            "delegates authority and names an apoderado",
        },
        negative_anchors=["ACTA CONSTITUTIVA", "OBJETO SOCIAL"],
        fields=[
            FieldSpec(
                name="apoderado",
                attribute_key="ownership.authorized_signer",
                type="name",
                required=True,
                multi=True,
                pii=True,
                labels=_labels(["Apoderado", "Mandatario"], ["Attorney-in-fact"]),
                validator="name",
            ),
            _entity_name_field(),
            FieldSpec(
                name="facultades",
                attribute_key="",
                type="string",
                labels=_labels(["Facultades", "Poderes otorgados"], ["Powers granted"]),
                notes="The three classical Mexican powers (pleitos y cobranzas, actos de "
                "administración, actos de dominio) are what a bank cares about. Kept "
                "without an attribute key: it is a document-local clause, not a "
                "mergeable customer attribute.",
            ),
            FieldSpec(
                name="notario",
                attribute_key="doc.notary",
                type="name",
                labels=_labels(["Notario público", "Notario"], ["Notary"]),
                validator="name",
            ),
            FieldSpec(
                name="escritura_number",
                attribute_key="doc.reference_number",
                type="id",
                labels=_labels(["Escritura número", "Instrumento número"], ["Instrument number"]),
                notes="Instrument numbers run sequentially per notary, so they are unique only "
                "together with the notary's number and state.",
            ),
            _issue_date_field(required=True),
        ],
    ),
    # ------------------------------------------------------ financial / address
    DocTypeSpec(
        doctype_id="mx_estado_cuenta",
        label="Estado de Cuenta Bancario",
        country="MX",
        category=Category.financial,
        issuing_authority="Institución de banca múltiple",
        applies_to="both",
        anchors=[
            _a("ESTADO DE CUENTA", decisive=True),
            _a("CLABE"),
            _a("Saldo"),
            _a("Periodo"),
            _a("Sucursal"),
            _a("Número de cuenta"),
            _en("Account Statement"),
            _en("Statement Period"),
        ],
        id_patterns=[CLABE_PATTERN, RFC_PATTERN],
        confusable_with={
            "mx_comprobante_telmex": "telephone bills are also headed 'estado de cuenta' — "
            "a TELMEX header or a phone number in the account "
            "field settles it",
            "us_bank_statement": "US statements print a routing number rather than a CLABE",
        },
        negative_anchors=[
            "TELMEX",
            "COMISIÓN FEDERAL DE ELECTRICIDAD",
            "AVISO-RECIBO",
            "Routing Number",
        ],
        fields=[
            _name_field(
                required=False, es=["Titular", "Nombre del cliente"], en=["Account holder"]
            ),
            _address_field(key="address.mailing", required=True),
            FieldSpec(
                name="clabe",
                attribute_key="account.clabe",
                type="id",
                pii=True,
                labels=_labels(["CLABE", "CLABE interbancaria"], ["CLABE"]),
                pattern=CLABE_PATTERN,
                notes="The CLABE is 18 digits and its last digit IS a published mod-10 "
                "weighted check digit, but no validator is declared for it here, so "
                "the value is captured unverified rather than checked with an "
                "unproven implementation.",
            ),
            FieldSpec(
                name="account_number",
                attribute_key="account.number",
                type="id",
                required=True,
                pii=True,
                multi=True,
                labels=_labels(["Número de cuenta", "Cuenta"], ["Account number"]),
                locators=["kv", "label", "table", "regex"],
                notes="Account-number formats differ by bank; the CLABE is the interbank key and "
                "the dependable one.",
            ),
            FieldSpec(
                name="bank_name",
                attribute_key="account.bank_name",
                type="name",
                labels=_labels(["Banco", "Institución"], ["Bank"]),
                validator="name",
            ),
            _amount_field(
                "closing_balance",
                key="account.balance",
                es=["Saldo final", "Saldo al corte"],
                en=["Closing balance"],
                required=True,
            ),
            FieldSpec(
                name="statement_period",
                attribute_key="account.statement_period",
                type="string",
                required=True,
                labels=_labels(["Periodo", "Del", "Al"], ["Statement period"]),
            ),
            _rfc_field(required=False),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_comprobante_cfe",
        label="Recibo de Luz CFE (comprobante de domicilio)",
        country="MX",
        category=Category.address_proof,
        issuing_authority="Comisión Federal de Electricidad (CFE)",
        applies_to="both",
        anchors=[
            _a("COMISIÓN FEDERAL DE ELECTRICIDAD", decisive=True),
            _a("AVISO-RECIBO", decisive=True),
            _a("CFE"),
            _a("Número de servicio"),
            _a("Tarifa"),
            _a("Medidor"),
            _a("kWh"),
            _a("Total a pagar"),
            _a("Periodo facturado"),
        ],
        confusable_with={
            "mx_comprobante_generico": "prefer this doctype whenever the CFE header is "
            "present; the generic one exists for issuers this "
            "pack does not model",
            "us_utility_bill": "a US bill prints a rate schedule and a US service address",
        },
        fields=[
            _name_field(required=True, es=["Nombre", "Titular"], en=["Customer name"]),
            _address_field(
                required=True, es=["Domicilio", "Dirección del suministro"], en=["Service address"]
            ),
            FieldSpec(
                name="service_number",
                attribute_key="utility.consumer_number",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Número de servicio", "RMU", "RPU"], ["Service number"]),
                notes="CFE prints a 12-digit servicio number and, separately, an RPU. The "
                "two are not interchangeable; capture whichever is labelled.",
            ),
            FieldSpec(
                name="consumption",
                attribute_key="utility.units_consumed",
                type="number",
                labels=_labels(["Consumo", "kWh"], ["Consumption"]),
                pattern=r"\b\d{1,6}\b",
                locators=["table", "kv", "label", "regex"],
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                es=["Total a pagar", "Importe"],
                en=["Total due"],
            ),
            FieldSpec(
                name="billing_period",
                attribute_key="utility.bill_period",
                type="string",
                required=True,
                labels=_labels(["Periodo facturado", "Periodo"], ["Billing period"]),
            ),
            FieldSpec(
                name="due_date",
                attribute_key="doc.due_date",
                type="date",
                labels=_labels(["Fecha límite de pago", "Vencimiento"], ["Due date"]),
                validator="generic_date",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_comprobante_agua",
        label="Recibo de Agua (comprobante de domicilio)",
        country="MX",
        category=Category.address_proof,
        issuing_authority="Municipal or state water utility",
        applies_to="both",
        anchors=[
            _a("SISTEMA DE AGUAS DE LA CIUDAD DE MÉXICO", decisive=True),
            _a("SACMEX", decisive=True, zone=Zone.title),
            _a("Agua potable"),
            _a("Drenaje"),
            _a("Toma"),
            _a("Consumo"),
            _a("Boleta del agua"),
            _a("SIAPA"),
            _a("JAPAC"),
        ],
        confusable_with={
            "mx_comprobante_cfe": "a CFE bill names the Comisión Federal de Electricidad "
            "and bills kWh; a water bill bills cubic metres",
            "mx_comprobante_generico": "prefer this doctype when a water operator is named",
        },
        negative_anchors=["COMISIÓN FEDERAL DE ELECTRICIDAD", "kWh"],
        fields=[
            _name_field(required=True, es=["Nombre", "Titular"], en=["Customer name"]),
            _address_field(
                required=True, es=["Domicilio", "Ubicación de la toma"], en=["Service address"]
            ),
            FieldSpec(
                name="account_number",
                attribute_key="utility.consumer_number",
                type="id",
                required=True,
                pii=True,
                labels=_labels(["Cuenta", "Número de cuenta", "Toma"], ["Account number"]),
                notes="Water operators number accounts individually; there is no shared format.",
            ),
            FieldSpec(
                name="provider",
                attribute_key="utility.service_provider",
                type="name",
                labels=_labels(["Organismo operador", "Prestador"], ["Provider"]),
                validator="name",
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                es=["Total a pagar", "Importe"],
                en=["Total due"],
            ),
            FieldSpec(
                name="billing_period",
                attribute_key="utility.bill_period",
                type="string",
                required=True,
                labels=_labels(["Periodo", "Bimestre"], ["Billing period"]),
            ),
        ],
        notes="Water is billed by municipal operators whose names change from city to city "
        "— SACMEX in Mexico City, SIAPA in Guadalajara, JAPAC in Culiacán, Interapas "
        "in San Luis Potosí and dozens more. Only the largest are named here; the "
        "rest fall through to mx_comprobante_generico by design.",
    ),
    DocTypeSpec(
        doctype_id="mx_comprobante_telmex",
        label="Recibo Telefónico TELMEX (comprobante de domicilio)",
        country="MX",
        category=Category.address_proof,
        issuing_authority="Teléfonos de México (TELMEX)",
        applies_to="both",
        anchors=[
            _a("TELÉFONOS DE MÉXICO", decisive=True),
            _a("TELMEX", decisive=True, zone=Zone.title),
            _a("Línea telefónica"),
            _a("Número telefónico"),
            _a("Cargos"),
            _a("Total a pagar"),
            _a("Paquete"),
        ],
        confusable_with={
            "mx_estado_cuenta": "TELMEX also heads its bill 'estado de cuenta'; the TELMEX "
            "brand and a telephone number in the account field separate "
            "them",
            "mx_comprobante_generico": "prefer this doctype when TELMEX is named",
        },
        negative_anchors=["CLABE", "Sucursal", "COMISIÓN FEDERAL DE ELECTRICIDAD"],
        fields=[
            _name_field(required=True, es=["Nombre", "Titular"], en=["Customer name"]),
            _address_field(required=True, es=["Domicilio", "Dirección"], en=["Address"]),
            FieldSpec(
                name="phone_number",
                attribute_key="identity.mobile",
                type="id",
                pii=True,
                labels=_labels(["Número telefónico", "Teléfono"], ["Telephone number"]),
                pattern=r"\b\d{2,3}[-\s]?\d{4}[-\s]?\d{4}\b",
                notes="Mexican numbers are ten digits with a two- or three-digit area code; "
                "the printed grouping varies.",
            ),
            FieldSpec(
                name="account_number",
                attribute_key="utility.consumer_number",
                type="id",
                pii=True,
                labels=_labels(["Número de cuenta", "Cuenta"], ["Account number"]),
                notes="TELMEX account numbers have no published format or check digit.",
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                es=["Total a pagar", "Importe"],
                en=["Total due"],
            ),
            FieldSpec(
                name="billing_period",
                attribute_key="utility.bill_period",
                type="string",
                labels=_labels(["Periodo", "Fecha de corte"], ["Billing period"]),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_predial",
        label="Boleta del Impuesto Predial",
        country="MX",
        category=Category.address_proof,
        issuing_authority="Municipal treasury (Tesorería municipal)",
        applies_to="both",
        anchors=[
            _a("IMPUESTO PREDIAL", decisive=True),
            _a("Cuenta catastral"),
            _a("Valor catastral"),
            _a("Tesorería"),
            _a("Bimestre"),
            _a("Predio"),
        ],
        confusable_with={
            "ca_property_tax_assessment": "the Canadian equivalent is a property assessment "
            "notice from MPAC or BC Assessment",
            "mx_comprobante_generico": "prefer this doctype when the predial header is present",
        },
        negative_anchors=["PROPERTY ASSESSMENT NOTICE", "AVIS D'ÉVALUATION FONCIÈRE"],
        fields=[
            _name_field(required=True, es=["Propietario", "Contribuyente"], en=["Owner"]),
            _address_field(
                required=True, es=["Ubicación del predio", "Domicilio"], en=["Property address"]
            ),
            FieldSpec(
                name="cuenta_catastral",
                attribute_key="property.roll_number",
                type="id",
                required=True,
                labels=_labels(["Cuenta catastral", "Clave catastral"], ["Cadastral account"]),
                notes="Cadastral account numbers are municipal; their length and grouping "
                "differ between municipalities, so no pattern is asserted.",
            ),
            _amount_field(
                "valor_catastral",
                key="property.assessed_value",
                es=["Valor catastral"],
                en=["Assessed value"],
            ),
            _amount_field(
                "amount_due",
                key="account.amount_due",
                es=["Importe a pagar", "Total a pagar"],
                en=["Amount due"],
            ),
            FieldSpec(
                name="period",
                attribute_key="utility.bill_period",
                type="string",
                labels=_labels(["Bimestre", "Periodo", "Ejercicio"], ["Period"]),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_comprobante_generico",
        label="Comprobante de Domicilio (issuer not modelled)",
        country="MX",
        category=Category.address_proof,
        issuing_authority="Any Mexican utility, telecom, bank or municipal issuer not "
        "modelled by a specific doctype",
        applies_to="both",
        anchors=[
            _a("Comprobante de domicilio"),
            _a("Total a pagar"),
            _a("Fecha de corte"),
            _a("Periodo"),
            _a("Domicilio"),
            _a("Importe"),
        ],
        confusable_with={
            "mx_comprobante_cfe": "if the Comisión Federal de Electricidad is named, that "
            "doctype wins",
            "mx_comprobante_agua": "if a water operator is named, that doctype wins",
            "mx_comprobante_telmex": "if TELMEX is named, that doctype wins",
            "mx_predial": "if the bill is for impuesto predial, that doctype wins",
        },
        fields=[
            _name_field(required=True, es=["Nombre", "Titular"], en=["Customer name"]),
            _address_field(required=True),
            FieldSpec(
                name="provider",
                attribute_key="utility.service_provider",
                type="name",
                labels=_labels(["Proveedor", "Empresa"], ["Provider"]),
                validator="name",
            ),
            FieldSpec(
                name="account_number",
                attribute_key="utility.consumer_number",
                type="id",
                pii=True,
                labels=_labels(["Número de cuenta", "Referencia"], ["Account number"]),
                notes="The issuer is unknown by construction, so no format can be asserted.",
            ),
            _amount_field(
                "amount_due",
                key="utility.bill_amount",
                es=["Total a pagar", "Importe"],
                en=["Total due"],
            ),
            _issue_date_field(required=True),
        ],
        notes="Deliberately anchor-weak and carrying no decisive anchor: it is the "
        "fallback for a Mexican proof of address whose issuer is not modelled here, "
        "and any issuer-specific doctype must be able to outrank it.",
    ),
)

#: Fast lookup used by the tests and by callers that already hold a doctype id.
DOCTYPES_BY_ID: dict[str, DocTypeSpec] = {spec.doctype_id: spec for spec in SPECS}


def specs() -> tuple[DocTypeSpec, ...]:
    """Return every Mexican :class:`~dce.models.DocTypeSpec` in this pack."""
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
    for module_name in ("dce.registry.canada", "dce.registry.usa"):
        import_module(module_name)


_load_sibling_packs()
