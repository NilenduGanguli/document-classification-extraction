"""Mexico doctype pack — 27 :class:`~dce.models.DocTypeSpec` entries.

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

**Listed issuers.** The BMV/CNBV filings in this pack (``mx_reporte_anual_cnbv``,
``mx_reporte_trimestral_bmv``, ``mx_prospecto_colocacion``) are the one corner of the pack
where a decisive anchor is easy to justify, because the regulator writes the words: the
annual report and the quarterly are generated from the exchange's XBRL template and print
its taxonomy role labels literally ("[411000-AR] Datos generales - Reporte Anual"), and the
Circular Única de Emisoras prescribes the prospectus legends verbatim. What those three
*share* — "Clave de Cotización", "Bolsa Mexicana de Valores", "Registro Nacional de
Valores" — is evidence that a document is a Mexican securities filing and not evidence of
which one, so it is declared non-decisively on all three.

The company-written instruments are the opposite case and are handled the opposite way.
``mx_acta_asamblea`` carries **no** decisive anchor at all: minutes are drafted by the
company, so no string on them is controlled by a single issuer, and the Ley General de
Sociedades Mercantiles vocabulary they reuse belongs to every Mexican company at once.
``mx_informe_comisario`` gets one only because the statute citation it must contain
(LGSM art. 166, fracción IV) is controlled by the legislature rather than by the author.

**The SAT pair is a third case, and it went the other way.** ``mx_cif`` and ``mx_rfc_csf``
each anchored decisively on their own title and declared each other confusable, because the
SAT reproduces the cédula as page one of the constancia. The declaration described the
relationship correctly and still left both claims false: measured on the corpus,
``CÉDULA DE IDENTIFICACIÓN FISCAL`` is printed on ``corpus/mx/mx_rfc_csf.pdf`` and
``CONSTANCIA DE SITUACIÓN FISCAL`` on ``corpus/mx/mx_cif.pdf``. A decisive anchor asserts the
string appears on one document type and no other, so neither title qualifies, and
``confusable_with`` cannot repair a false claim — it suppresses one of the two routes that
act on it and leaves the other. Both are demoted. The pair is separated where it always was,
by the constancia's ``Regímenes`` and ``Obligaciones`` sections on pages 2-3, and a
page-1-only extract abstains, which is the right answer to a question the caller did not send
the evidence for.

Every decisive anchor in this pack states its grounds in :class:`dce.models.Controls`; the
loader rejects one that does not.
"""

from __future__ import annotations

from importlib import import_module

from dce.models import Anchor, Category, Controls, DocTypeSpec, FieldSpec, Zone

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
    # -- listed issuers and employer registration ---------------------------
    "entity.ticker": "Trading symbol under which a class of securities trades",
    "entity.exchange": "Exchange on which a class of securities is registered",
    "entity.fiscal_year_end": "Financial year end the report or filing closes on",
    "entity.auditor": "Independent accounting firm that signed the audit report",
    "entity.statutory_examiner": "Comisario / statutory examiner elected to supervise the "
    "company, distinct from its external auditor",
    "entity.shares_outstanding": "Shares of a class outstanding as of a stated date",
    "doc.period_covered": "Reporting period a periodic report covers",
    "id.imss_registro_patronal": "IMSS employer registry number (registro patronal)",
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
    controls: Controls | None = None,
    zone: Zone | None = None,
) -> Anchor:
    """Build a Spanish :class:`~dce.models.Anchor` (the default language of this pack).

    ``controls`` is mandatory when ``decisive`` is set and forbidden otherwise — see
    :class:`dce.models.Controls`. It has no default here on purpose: a builder that supplied
    one would re-create the invisible claim the field exists to prevent.
    """
    return Anchor(text=text, lang=lang, decisive=decisive, controls=controls, zone=zone)


def _en(
    text: str,
    *,
    decisive: bool = False,
    controls: Controls | None = None,
    zone: Zone | None = None,
) -> Anchor:
    """Build an English anchor — only for text the document itself prints in English."""
    return Anchor(text=text, lang="en", decisive=decisive, controls=controls, zone=zone)


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
            _a("INSTITUTO NACIONAL ELECTORAL", decisive=True, controls=Controls.ISSUER_NAME),
            _a("CREDENCIAL PARA VOTAR", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("INSTITUTO FEDERAL ELECTORAL", decisive=True, controls=Controls.ISSUER_NAME),
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
            # "P<MEX" is the ICAO 9303 document code plus issuing State: printed by exactly
            # one issuer, on exactly one document, and adjacent to four check digits. That is
            # what a decisive anchor is meant to be, and it is the model for the rest of this
            # pack.
            _a("P<MEX", decisive=True, controls=Controls.MRZ_PREFIX),
            # "PASAPORTE" was decisive, gated to the title zone. Every Spanish-language
            # passport on earth is titled that way, and xx_passport_generic claims the string
            # too — a title zone cannot turn a document-class name into proof of a
            # jurisdiction. Demoted; P<MEX carries the decisive claim, and it is the one
            # string on the book that actually says "Mexico issued this".
            _a("PASAPORTE", zone=Zone.title),
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
            _a("CLAVE ÚNICA DE REGISTRO DE POBLACIÓN"),
            _a("CONSTANCIA DE LA CURP", decisive=True, controls=Controls.ISSUER_NAME),
            _a("REGISTRO NACIONAL DE POBLACIÓN", decisive=True, controls=Controls.ISSUER_NAME),
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
            _a("ACTA DE NACIMIENTO", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("CÉDULA PROFESIONAL", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
            _a("DIRECCIÓN GENERAL DE PROFESIONES", decisive=True, controls=Controls.ISSUER_NAME),
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
            _a(
                "CARTILLA DEL SERVICIO MILITAR NACIONAL",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("SERVICIO MILITAR NACIONAL", decisive=True, controls=Controls.ISSUER_NAME),
            _a("SECRETARÍA DE LA DEFENSA NACIONAL", decisive=True, controls=Controls.ISSUER_NAME),
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
            _a(
                "MATRÍCULA CONSULAR DE ALTA SEGURIDAD",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("MATRÍCULA CONSULAR", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a(
                "TARJETA DE RESIDENTE PERMANENTE",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "TARJETA DE RESIDENTE TEMPORAL",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("INSTITUTO NACIONAL DE MIGRACIÓN", decisive=True, controls=Controls.ISSUER_NAME),
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
            _a("CONSTANCIA DE SITUACIÓN FISCAL"),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("REGISTRO FEDERAL DE CONTRIBUYENTES"),
            _a("Datos de identificación del contribuyente"),
            _a("Régimen"),
            _a("idCIF"),
            _a("Domicilio fiscal"),
            # The sections the SAT prints only on the constancia. Page 1 of a CSF *is* the
            # cédula, so nothing on page 1 can separate the two documents — but a CSF always
            # continues into the regimes and obligations tables and a standalone cédula
            # never does. These are the only strings in this pair that discriminate.
            _a("Regímenes"),
            _a("Obligaciones"),
            _a("Actividades Económicas"),
        ],
        id_patterns=[RFC_PATTERN, CURP_PATTERN],
        confusable_with={
            "mx_cif": "the CSF is the multi-page fiscal-status statement and its own first "
            "page is the cédula, so the two are separated by what comes *after* "
            "page 1 — the Regímenes and Obligaciones tables — not by their titles",
            "mx_opinion_cumplimiento": "the opinión states whether obligations are up to "
            "date; the constancia states who the taxpayer is",
        },
        negative_anchors=[
            # "CÉDULA DE IDENTIFICACIÓN FISCAL" was removed. The SAT prints that exact
            # heading at the top of page 1 of every Constancia de Situación Fiscal — it is
            # the cédula, reproduced as the constancia's first page. As a negative anchor it
            # therefore fired on every genuine instance of this doctype, penalising the CSF
            # for being a CSF, while mx_cif's mirror-image negative penalised the cédula for
            # the constancia heading on the same sheet. The two specs cancelled each other
            # and the pair could only ever abstain.
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
            _a("CÉDULA DE IDENTIFICACIÓN FISCAL"),
            _a("SERVICIO DE ADMINISTRACIÓN TRIBUTARIA"),
            _a("REGISTRO FEDERAL DE CONTRIBUYENTES"),
            _a("idCIF"),
        ],
        id_patterns=[RFC_PATTERN],
        confusable_with={
            "mx_rfc_csf": "the CIF is a single page with the QR code; the constancia runs "
            "to several pages and lists regimes and obligations. Page 1 of a "
            "constancia IS the cédula, reproduced — so a page-1-only extract of "
            "a CSF carries both titles and is genuinely undecidable from its "
            "text. Abstaining on it is the correct answer, not a gap; see this "
            "doctype's notes",
        },
        negative_anchors=[
            # A standalone cédula does not carry the constancia's title, so this one is
            # sound and stays.
            "CONSTANCIA DE SITUACIÓN FISCAL",
            # "Régimen" was removed: the cédula's own identification block prints "Régimen
            # Capital", so this negative anchor fired on every genuine cédula. The plural
            # section heading "Regímenes" is what only the constancia has, and it is
            # declared as a positive anchor on mx_rfc_csf rather than as a negative here.
            "Regímenes",
        ],
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
        notes="**Kept as its own doctype, and the reason is worth stating, because the "
        "alternative was to merge it into mx_rfc_csf.** The two are not the same document: a "
        "cédula answers 'who is this taxpayer, and what is the RFC' and is the thing a "
        "counterparty is asked for; a constancia additionally publishes the regimes and the "
        "standing tax obligations, which is a materially larger disclosure with a different "
        "retention profile. Merging them would make DCE unable to say which of those two "
        "things it was handed.\n\n"
        "What is true — and what the abstention people keep asking about actually is — is "
        "that **page 1 of a constancia IS a cédula**. The SAT reproduces the cédula, QR code "
        "and all, as the CSF's first sheet. So a page-1-only extract carries "
        "'CÉDULA DE IDENTIFICACIÓN FISCAL' and 'CONSTANCIA DE SITUACIÓN FISCAL' together, "
        "and nothing in its text distinguishes 'a cédula' from 'the first page of a "
        "constancia'. That is not a registry defect and no anchor can repair it: the "
        "information required to decide is on pages 2-3, which the caller did not send. The "
        "correct behaviour is to abstain and route to a human, and that is what happens.\n\n"
        "The corpus makes the point sharply and the file is mislabelled rather than "
        "instructive: ``corpus/mx/mx_cif.pdf`` is byte-for-byte page 1 of "
        "``corpus/mx/mx_rfc_csf.pdf`` — identical extracted text, the same seven embedded "
        "images, and its own header reads 'Página [1] de [3]'. It is a truncated constancia, "
        "not a cédula, and the registry is right to refuse it. Do not add anchors to make "
        "that file classify: every string that would do it appears on genuine constancias "
        "too, so the coverage would be bought by turning a safe abstention into a wrong "
        "answer on the more common document. A real standalone cédula — one page, the QR "
        "code, no constancia title — classifies here on the decisive anchor already present; "
        "the corpus simply has no specimen of one.",
    ),
    DocTypeSpec(
        doctype_id="mx_efirma_certificado",
        label="Certificado de e.firma (FIEL)",
        country="MX",
        category=Category.tax,
        issuing_authority="Servicio de Administración Tributaria (SAT)",
        applies_to="both",
        anchors=[
            _a(
                "CERTIFICADO DE FIRMA ELECTRÓNICA AVANZADA",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "FIRMA ELECTRÓNICA AVANZADA",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
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
            _a(
                "OPINIÓN DEL CUMPLIMIENTO DE OBLIGACIONES FISCALES",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("Sentido de la opinión", decisive=True, controls=Controls.ISSUER_TEMPLATE),
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
            _a("ACTA CONSTITUTIVA"),
            _a("CONSTITUCIÓN DE SOCIEDAD", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a(
                "PODER GENERAL PARA PLEITOS Y COBRANZAS",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "PODER GENERAL PARA ACTOS DE ADMINISTRACIÓN",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "PODER GENERAL PARA ACTOS DE DOMINIO",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("PODER NOTARIAL", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
    DocTypeSpec(
        doctype_id="mx_acta_asamblea",
        label="Acta de Asamblea de Accionistas (shareholders' meeting minutes)",
        country="MX",
        category=Category.corporate,
        issuing_authority="The company itself; protocolised before a notario público when "
        "the resolutions have to be filed with the Registro Público de Comercio",
        applies_to="corporate",
        # NO DECISIVE ANCHOR, deliberately. Minutes are written by the company, so there is
        # no string here that one issuer controls — the vocabulary below is the Ley General
        # de Sociedades Mercantiles' own (arts. 178-206: orden del día, escrutadores, lista
        # de asistencia, acciones representadas), which every Mexican company reuses and no
        # single one owns. Declaring any of it decisive would be exactly the document-class
        # claim that produced confident cross-issuer wrong answers elsewhere in this
        # registry. This doctype can only win by concurrence over the whole cluster, which
        # is the honest strength of the evidence.
        anchors=[
            _a("ASAMBLEA GENERAL ORDINARIA DE ACCIONISTAS"),
            _a("ASAMBLEA GENERAL EXTRAORDINARIA DE ACCIONISTAS"),
            _a("ASAMBLEA GENERAL ANUAL ORDINARIA DE ACCIONISTAS"),
            _a("Lista de Asistencia"),
            _a("Escrutadores"),
            _a("Orden del Día"),
            _a("Acciones representadas"),
            _a("Presidente de la Asamblea"),
            _a("Secretario de la Asamblea"),
            _a("Resoluciones"),
        ],
        confusable_with={
            "mx_acta_constitutiva": "the constitutive instrument creates the company and "
            "carries its own decisive title; ordinary and "
            "extraordinary assemblies of an existing company do not",
            "mx_poder_notarial": "assemblies routinely grant powers, so the poder vocabulary "
            "appears inside minutes — the poder is the standalone "
            "instrument whose whole subject is the grant",
            "mx_informe_comisario": "the comisario's report is read *to* the annual ordinary "
            "assembly, so both name the assembly; only the report "
            "carries the LGSM art. 166 opinion",
        },
        # Deliberately empty. "ACTA CONSTITUTIVA" and "PODER GENERAL PARA PLEITOS Y
        # COBRANZAS" are the obvious candidates and both are wrong: minutes recite the
        # constitutive instrument by name ("según consta en la escritura constitutiva") and
        # very commonly grant exactly those three classical powers. A negative anchor that
        # fires on genuine instances of its own doctype is the mistake that made the
        # mx_cif / mx_rfc_csf pair unable to do anything but abstain.
        negative_anchors=[],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="meeting_date",
                attribute_key="doc.issue_date",
                type="date",
                required=True,
                labels=_labels(
                    ["Fecha de la asamblea", "Fecha", "celebrada el"], ["Date of the meeting"]
                ),
                validator="generic_date",
            ),
            FieldSpec(
                name="meeting_type",
                attribute_key="",
                type="string",
                labels=_labels(["Tipo de asamblea", "Asamblea"], ["Type of meeting"]),
                notes="Ordinaria / extraordinaria / mixta. Kept without an attribute key: it "
                "describes this instrument, not a durable attribute of the company.",
            ),
            FieldSpec(
                name="resolutions",
                attribute_key="",
                type="string",
                multi=True,
                labels=_labels(
                    ["Resoluciones", "Acuerdos", "Orden del Día"], ["Resolutions", "Agenda"]
                ),
                locators=["table", "kv", "label"],
                notes="What the assembly actually decided is the reason a bank asks for the "
                "minutes; surface it verbatim rather than trying to classify it.",
            ),
            FieldSpec(
                name="administradores",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(
                    ["Consejo de administración", "Administrador único", "Consejeros"],
                    ["Directors"],
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="apoderados",
                attribute_key="ownership.authorized_signer",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(["Apoderado", "Delegado especial"], ["Attorney-in-fact"]),
                validator="name",
            ),
            FieldSpec(
                name="accionistas",
                attribute_key="ownership.beneficial_owner",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(
                    ["Accionistas", "Lista de Asistencia", "Socios"], ["Shareholders present"]
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="presidente",
                attribute_key="",
                type="name",
                pii=True,
                labels=_labels(["Presidente de la Asamblea", "Presidente"], ["Chair"]),
                validator="name",
                notes="A meeting officer, not a company officer — the same person may chair "
                "one assembly and hold no role in the company.",
            ),
            FieldSpec(
                name="notario",
                attribute_key="doc.notary",
                type="name",
                pii=True,
                labels=_labels(["Notario público", "Notario", "Fedatario"], ["Notary"]),
                validator="name",
                notes="Only present when the minutes were protocolised; ordinary minutes are "
                "signed by the meeting officers and entered in the libro de actas.",
            ),
        ],
        handling="Minutes name individual shareholders and officers and record their "
        "shareholdings; treat the attendance list as personal data.",
    ),
    DocTypeSpec(
        doctype_id="mx_informe_comisario",
        label="Informe del Comisario (statutory examiner's annual report, LGSM art. 166)",
        country="MX",
        category=Category.corporate,
        issuing_authority="Comisario of the sociedad anónima (an individual or firm elected "
        "by the shareholders under LGSM arts. 164-171)",
        applies_to="corporate",
        anchors=[
            # The statute citation is the decisive evidence, not the report's title. The
            # comisario is created by the Ley General de Sociedades Mercantiles and by
            # nothing else, and art. 166 fracción IV is the provision that obliges the
            # report to exist — a legislature-controlled string in exactly the sense a
            # decisive anchor requires. Punctuation is dropped before matching, so the one
            # spelling below also covers "artículo 166, fracción IV, de la Ley…".
            _a(
                "artículo 166 fracción IV de la Ley General de Sociedades Mercantiles",
                decisive=True,
                controls=Controls.STATUTE_TITLE,
            ),
            #
            # The supporting set is deliberately short, and four candidates were measured
            # out of it rather than argued out. "Comisario" alone, "LEY GENERAL DE SOCIEDADES
            # MERCANTILES" and "A LA ASAMBLEA GENERAL ORDINARIA DE ACCIONISTAS" are broad
            # Spanish corporate vocabulary; because the lexical tier derives its idf from the
            # whole registry, a spec that keeps terms like "general", "sociedad" and
            # "accionistas" in its profile lowers their idf for every doctype that already
            # relied on them. Measured over the 59-document reference corpus, this doctype
            # with those anchors flipped ``corpus/mx/mx_cif.pdf`` from an abstention to a
            # WRONG ``mx_rfc_csf``, without ever being a candidate itself. The statute
            # citation carries the identification; the two phrases the LGSM obliges the
            # report to contain carry the corroboration; the rest was dilution.
            _a("INFORME DEL COMISARIO"),
            _a("Comisario Propietario"),
            _a("veracidad, suficiencia y razonabilidad"),
            _a("políticas y criterios contables"),
        ],
        confusable_with={
            "mx_acta_asamblea": "the report is addressed to the annual ordinary assembly and "
            "is bound with its minutes; only the report gives an "
            "opinion on the board's information",
            "mx_reporte_anual_cnbv": "a listed issuer replaces the comisario with an audit "
            "committee under the Ley del Mercado de Valores, so the "
            "two rarely co-occur — an Anexo N carries the CNBV "
            "taxonomy headings and this does not",
        },
        negative_anchors=[],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="comisario",
                attribute_key="entity.statutory_examiner",
                type="name",
                required=True,
                pii=True,
                labels=_labels(
                    ["Comisario Propietario", "Comisario Suplente"], ["Statutory examiner"]
                ),
                validator="name",
                notes="Usually a licensed contador público acting personally. Their name is "
                "personal data even though the role is corporate. 'C.P.' was dropped as a "
                "label: it abbreviates both contador público and código postal.",
            ),
            FieldSpec(
                name="fiscal_year_end",
                attribute_key="entity.fiscal_year_end",
                type="date",
                required=True,
                labels=_labels(
                    ["por el ejercicio social terminado el", "al 31 de diciembre de"],
                    ["Fiscal year ended"],
                ),
                validator="generic_date",
            ),
            FieldSpec(
                name="opinion",
                attribute_key="",
                type="string",
                labels=_labels(["En mi opinión", "En nuestra opinión"], ["Opinion"]),
                notes="An adverse or qualified comisario opinion is a due-diligence finding in "
                "itself; never collapse it to a boolean.",
            ),
            _issue_date_field(required=True),
            FieldSpec(
                name="signatory_title",
                attribute_key="",
                type="string",
                labels=_labels(["Comisario Propietario", "Comisario Suplente"], ["Title"]),
                notes="Propietario or suplente. A report signed by the suplente is valid but "
                "worth surfacing — it means the elected comisario did not sign.",
            ),
        ],
        handling="Names an individual professional and their signature; pii.",
    ),
    DocTypeSpec(
        doctype_id="mx_imss_alta_patronal",
        label="Alta Patronal ante el IMSS (AFIL-01 / Tarjeta de Identificación Patronal)",
        country="MX",
        category=Category.corporate,
        issuing_authority="Instituto Mexicano del Seguro Social (IMSS)",
        applies_to="corporate",
        anchors=[
            # IMSS form codes and the IMSS-printed card title: strings the institute alone
            # controls. AFIL-01 splits into two words ("AFIL", "01") so it clears the
            # short-decisive-anchor rule without a zone gate, and a form number is the
            # canonical shape of a safe decisive anchor.
            _a(
                "AVISO DE INSCRIPCIÓN PATRONAL O DE MODIFICACIÓN EN SU REGISTRO",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a(
                "TARJETA DE IDENTIFICACIÓN PATRONAL",
                decisive=True,
                controls=Controls.CLASS_NAME_UNCONTESTED,
            ),
            _a("AFIL-01", decisive=True, controls=Controls.FORM_NUMBER),
            _a("INSTITUTO MEXICANO DEL SEGURO SOCIAL"),
            _a("Registro Patronal"),
            _a("INFONAVIT"),
            _a("Prima de Riesgo de Trabajo"),
            _a("Subdelegación"),
            _a("Clase"),
            _a("Actividad económica"),
        ],
        id_patterns=[RFC_PATTERN],
        confusable_with={
            "mx_rfc_csf": "the CSF is the SAT's fiscal-status statement; this is the IMSS "
            "employer registration and carries a registro patronal, not a régimen",
            "mx_opinion_cumplimiento": "the 32-D opinion reports whether SAT obligations are "
            "current; this reports that the employer exists on the "
            "IMSS padrón",
        },
        negative_anchors=[
            "SERVICIO DE ADMINISTRACIÓN TRIBUTARIA",
            "CONSTANCIA DE SITUACIÓN FISCAL",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="registro_patronal",
                attribute_key="id.imss_registro_patronal",
                type="id",
                required=True,
                labels=_labels(
                    ["Registro Patronal", "Número de Registro Patronal"],
                    ["Employer registration number"],
                ),
                notes="Eleven alphanumeric positions. The IMSS has never published how the "
                "positions decompose or whether the trailing digits check, so no pattern "
                "and no validator are asserted — inventing one would reject genuine cards.",
            ),
            _rfc_field(required=False),
            _address_field(
                key="address.registered",
                es=["Domicilio del centro de trabajo", "Domicilio"],
                en=["Workplace address"],
            ),
            FieldSpec(
                name="clase_y_fraccion",
                attribute_key="",
                type="string",
                labels=_labels(["Clase", "Fracción", "Actividad económica"], ["Risk class"]),
                notes="The class/fraction pair fixes the occupational-risk premium; it is a "
                "fact about the workplace, not about the legal entity.",
            ),
            _amount_field(
                "prima_de_riesgo",
                key="",
                es=["Prima de Riesgo de Trabajo", "Prima"],
                en=["Occupational risk premium"],
            ),
            FieldSpec(
                name="subdelegacion",
                attribute_key="doc.issuing_authority",
                type="string",
                labels=_labels(["Subdelegación", "Delegación"], ["IMSS sub-delegation"]),
            ),
            _issue_date_field(required=True),
        ],
    ),
    # ----------------------------------------- listed issuers (BMV / CNBV filings)
    #
    # Three documents that only a Mexican listed issuer produces, and the reason each one
    # has a usable decisive anchor: the CNBV and the BMV mandate the *wording*. The Anexo N
    # and the quarterly are generated from the exchange's XBRL template, so their section
    # headings are literal taxonomy role labels ("[411000-AR] Datos generales - Reporte
    # Anual") that no one but the BMV assigns; the prospectus carries a CNBV legend that the
    # Circular Única de Emisoras prescribes verbatim. Everything the three share — "Clave de
    # Cotización", "Bolsa Mexicana de Valores", "Registro Nacional de Valores", the RNV
    # non-certification legend — stays non-decisive on all three, because it is evidence of
    # "a Mexican securities filing" and not of which one.
    DocTypeSpec(
        doctype_id="mx_reporte_anual_cnbv",
        label="Reporte Anual (CNBV Anexo N — Circular Única de Emisoras)",
        country="MX",
        category=Category.financial,
        issuing_authority="Filed by the emisora with the Comisión Nacional Bancaria y de "
        "Valores and the Bolsa Mexicana de Valores under the Circular Única de Emisoras",
        applies_to="corporate",
        anchors=[
            _a(
                "[411000-AR] Datos generales - Reporte Anual",
                decisive=True,
                controls=Controls.FORM_NUMBER,
            ),
            _a("[412000-N] Portada reporte anual", decisive=True, controls=Controls.FORM_NUMBER),
            _a(
                "Reporte Anual que se presenta de acuerdo con las disposiciones de carácter "
                "general aplicables a las emisoras de valores y a otros participantes del "
                "mercado de valores",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a("[413000-N] Información general"),
            _a("[417000-N] La emisora"),
            _a("[424000-N] Información financiera"),
            _a("[427000-N] Administración"),
            _a("[429000-N] Mercado de capitales"),
            _a("[432000-N] Anexos"),
            _a("Clave de Cotización"),
            _a("Bolsa Mexicana de Valores"),
            _a("Registro Nacional de Valores"),
            _a("Comisión Nacional Bancaria y de Valores"),
            _a(
                "La inscripción en el Registro Nacional de Valores no implica certificación "
                "sobre la bondad de los valores"
            ),
        ],
        confusable_with={
            "mx_reporte_trimestral_bmv": "both come out of the exchange's XBRL template; the "
            "annual carries the -AR / -N taxonomy roles and the "
            "quarterly carries the NIC 34 interim note",
            "mx_prospecto_colocacion": "a prospectus offers securities and carries the CNBV "
            "offering legend; the annual report reports on "
            "securities already registered",
            "mx_informe_comisario": "a listed issuer reports through an audit committee, not "
            "a comisario",
        },
        negative_anchors=[
            # Safe in one direction only, which is why it is the only one here: NIC 34
            # governs *interim* reporting, so this note exists on a quarterly and cannot
            # exist on a report covering a full financial year.
            "[813000] Notas - Información financiera intermedia de conformidad con la NIC 34",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="clave_cotizacion",
                attribute_key="entity.ticker",
                type="id",
                required=True,
                labels=_labels(
                    ["Clave de Cotización", "Clave de cotización", "Clave de pizarra"],
                    ["Ticker", "Trading symbol"],
                ),
                notes="Assigned by the BMV and unique per issuer, but reused after a "
                "delisting — never a durable entity key on its own.",
            ),
            FieldSpec(
                name="exchange",
                attribute_key="entity.exchange",
                type="string",
                labels=_labels(["Bolsa", "Bolsa de valores"], ["Exchange"]),
                notes="BMV or BIVA. Both are Mexican exchanges and an issuer may be listed on "
                "either, so the exchange name is not evidence of the doctype.",
            ),
            FieldSpec(
                name="fiscal_year_end",
                attribute_key="entity.fiscal_year_end",
                type="date",
                required=True,
                labels=_labels(
                    ["Fecha", "Por el ejercicio social terminado el", "Ejercicio"],
                    ["Fiscal year ended"],
                ),
                validator="generic_date",
            ),
            FieldSpec(
                name="period_covered",
                attribute_key="doc.period_covered",
                type="string",
                labels=_labels(["Periodo", "Ejercicio social"], ["Period covered"]),
            ),
            _address_field(
                key="address.registered",
                es=["Domicilio de la emisora", "Domicilio social", "Dirección"],
                en=["Registered address"],
            ),
            FieldSpec(
                name="auditor",
                attribute_key="entity.auditor",
                type="name",
                labels=_labels(
                    ["Auditor externo", "Auditores independientes", "Despacho"],
                    ["Independent auditor"],
                ),
                validator="name",
            ),
            FieldSpec(
                name="shares_outstanding",
                attribute_key="entity.shares_outstanding",
                type="number",
                labels=_labels(
                    ["Títulos Accionarios en Circulación", "Acciones en circulación"],
                    ["Shares outstanding"],
                ),
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="administradores",
                attribute_key="ownership.director",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(
                    ["Consejo de Administración", "Consejeros"], ["Board of directors"]
                ),
                validator="name",
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="signatory",
                attribute_key="ownership.authorized_signer",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(
                    ["Director General", "Director de Finanzas", "Nombre y firma"],
                    ["Chief Executive Officer", "Chief Financial Officer"],
                ),
                validator="name",
            ),
            FieldSpec(
                name="signatory_title",
                attribute_key="",
                type="string",
                multi=True,
                labels=_labels(["Cargo", "Puesto"], ["Title"]),
            ),
        ],
        handling="A public filing, so its corporate content is not confidential — but the "
        "officers and directors it names are still identified individuals.",
    ),
    DocTypeSpec(
        doctype_id="mx_reporte_trimestral_bmv",
        label="Información Financiera Trimestral (BMV quarterly report)",
        country="MX",
        category=Category.financial,
        issuing_authority="Filed by the emisora with the Bolsa Mexicana de Valores and the "
        "Comisión Nacional Bancaria y de Valores",
        applies_to="corporate",
        anchors=[
            # NIC 34 is the IFRS interim-reporting standard; the BMV's template emits this
            # note only on an interim filing, and the bracketed number is the exchange's own
            # taxonomy role. Nothing else in the registry can print it.
            _a("[813000] Notas - Información financiera intermedia de conformidad con la NIC 34"),
            _a("Información Financiera Trimestral"),
            _a("[105000] Comentarios y Análisis de la Administración"),
            _a("[700003] Datos informativos- Estado de resultados 12 meses"),
            _a("Cantidades monetarias expresadas en Unidades"),
            _a("Clave de Cotización"),
            _a("Bolsa Mexicana de Valores"),
            _a("Trimestre"),
            _a("Consolidado"),
        ],
        confusable_with={
            "mx_reporte_anual_cnbv": "the annual report is the Anexo N and carries the -AR / "
            "-N taxonomy roles; the quarterly carries the NIC 34 "
            "interim note and a Trimestre / Año header",
            "mx_estado_cuenta": "a quarterly report is an issuer's own filing; a bank "
            "statement is issued to a customer by a bank",
        },
        negative_anchors=[
            # One-directional, like the annual's: the Anexo N cover roles cannot appear on a
            # quarterly filing, which has no cover page of that kind.
            "[411000-AR] Datos generales - Reporte Anual",
            "[412000-N] Portada reporte anual",
        ],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="clave_cotizacion",
                attribute_key="entity.ticker",
                type="id",
                required=True,
                labels=_labels(
                    ["Clave de Cotización", "Clave de cotización"], ["Ticker", "Trading symbol"]
                ),
                notes="Assigned by the exchange, with no published structure and no check "
                "digit — and reused after a delisting, so it is never a durable entity "
                "key on its own.",
            ),
            FieldSpec(
                name="quarter",
                attribute_key="doc.period_covered",
                type="string",
                required=True,
                labels=_labels(["Trimestre", "Trimestre y Año"], ["Quarter"]),
                notes="Printed as a bare 1-4 next to 'Trimestre:' in the page header, with "
                "the year in a separate 'Año:' box — capture both or the value is "
                "meaningless.",
            ),
            FieldSpec(
                name="fiscal_year",
                attribute_key="entity.fiscal_year_end",
                type="string",
                labels=_labels(["Año", "Ejercicio"], ["Year"]),
            ),
            FieldSpec(
                name="consolidation_basis",
                attribute_key="",
                type="string",
                labels=_labels(["Consolidado", "No Consolidado"], ["Consolidated"]),
                notes="Consolidado / No Consolidado changes what the figures mean; it is a "
                "property of this filing, not of the entity.",
            ),
            _amount_field(
                "total_assets",
                key="",
                es=["Total de activos", "Activos totales"],
                en=["Total assets"],
            ),
            _amount_field(
                "total_revenue",
                key="",
                es=["Ingresos", "Ingresos totales", "Ventas netas"],
                en=["Revenue", "Total revenue"],
            ),
            FieldSpec(
                name="signatory",
                attribute_key="ownership.authorized_signer",
                type="name",
                multi=True,
                pii=True,
                labels=_labels(
                    ["Director General", "Director de Finanzas", "Por:"],
                    ["Chief Executive Officer", "Chief Financial Officer"],
                ),
                validator="name",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="mx_prospecto_colocacion",
        label="Prospecto de Colocación (securities offering prospectus)",
        country="MX",
        category=Category.financial,
        issuing_authority="Emisora and intermediario colocador; authorised for publication "
        "by the Comisión Nacional Bancaria y de Valores",
        applies_to="corporate",
        anchors=[
            # The Circular Única de Emisoras prescribes both legends verbatim and they may
            # not be paraphrased, so the CNBV — one issuer — controls the exact wording.
            # The second one differs from the annual report's only in its closing words
            # ("...contenida en este Prospecto" versus "...en este Reporte Anual"), which is
            # why the shared prefix is declared separately and non-decisively below.
            _a(
                "no podrán ser ofrecidos ni vendidos fuera de los Estados Unidos Mexicanos, "
                "a menos que sea permitido por las leyes de otros países",
                decisive=True,
                controls=Controls.ISSUER_TEMPLATE,
            ),
            _a(
                "ni convalida los actos que, en su caso, hubieren sido realizados en "
                "contravención de las leyes",
            ),
            _a("PROSPECTO DEFINITIVO"),
            _a("PROSPECTO PRELIMINAR"),
            _a(
                "La inscripción en el Registro Nacional de Valores no implica certificación "
                "sobre la bondad de los valores"
            ),
            _a("Registro Nacional de Valores"),
            _a("Comisión Nacional Bancaria y de Valores"),
            _a("Intermediario Colocador"),
            _a("Oferta Pública"),
            _a("Clave de pizarra"),
            _a("Factores de Riesgo"),
            _en("DEFINITIVE PROSPECTUS"),
        ],
        confusable_with={
            "mx_reporte_anual_cnbv": "both carry the RNV non-certification legend; only a "
            "prospectus carries the offering legend and names an "
            "intermediario colocador",
            "mx_reporte_trimestral_bmv": "a prospectus offers securities; the quarterly "
            "reports on an issuer that already has them listed",
        },
        negative_anchors=["[411000-AR] Datos generales - Reporte Anual"],
        fields=[
            _entity_name_field(),
            FieldSpec(
                name="clave_pizarra",
                attribute_key="entity.ticker",
                type="id",
                labels=_labels(
                    ["Clave de pizarra", "Clave de Cotización"], ["Ticker", "Trading symbol"]
                ),
                notes="Assigned by the exchange, with no published structure and no check "
                "digit. A debt programme's clave carries a year suffix (GDINIZ 12) that "
                "the equity clave does not — do not strip it.",
            ),
            FieldSpec(
                name="instrument_type",
                attribute_key="",
                type="string",
                labels=_labels(
                    ["Tipo de valor", "Tipo de instrumento"], ["Type of security"]
                ),
                notes="Certificados bursátiles, acciones, obligaciones subordinadas… — the "
                "instrument, not the issuer, so it does not belong in the merge view.",
            ),
            _amount_field(
                "offering_amount",
                key="",
                es=["Monto total autorizado", "Monto de la oferta", "Monto total de la emisión"],
                en=["Aggregate offering amount"],
            ),
            FieldSpec(
                name="intermediario_colocador",
                attribute_key="",
                type="name",
                multi=True,
                labels=_labels(["Intermediario Colocador", "Agente colocador"], ["Underwriter"]),
                validator="name",
            ),
            FieldSpec(
                name="rnv_registration",
                attribute_key="doc.registration_number",
                type="id",
                labels=_labels(
                    ["Número de inscripción en el Registro Nacional de Valores", "Inscripción"],
                    ["RNV registration number"],
                ),
                notes="RNV numbers are assigned per issue, not per issuer, and the CNBV has "
                "never published their composition — no pattern is asserted.",
            ),
            _address_field(
                key="address.registered",
                es=["Domicilio de la emisora", "Domicilio social"],
                en=["Registered address"],
            ),
            _issue_date_field(required=True),
        ],
        handling="A prospectus is published to the market, so nothing in it is confidential; "
        "the individuals it names are nonetheless identified people.",
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
            _a("ESTADO DE CUENTA", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a("COMISIÓN FEDERAL DE ELECTRICIDAD", decisive=True, controls=Controls.ISSUER_NAME),
            _a("AVISO-RECIBO", decisive=True, controls=Controls.CLASS_NAME_UNCONTESTED),
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
            _a(
                "SISTEMA DE AGUAS DE LA CIUDAD DE MÉXICO",
                decisive=True,
                controls=Controls.ISSUER_NAME,
            ),
            _a("SACMEX", decisive=True, controls=Controls.ISSUER_NAME, zone=Zone.title),
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
            _a("TELÉFONOS DE MÉXICO"),
            _a("TELMEX", zone=Zone.title),
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
            _a("IMPUESTO PREDIAL"),
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
    # -------------------------------------------------------------------- other
    DocTypeSpec(
        doctype_id="mx_aviso_privacidad",
        label="Aviso de Privacidad (LFPDPPP privacy notice)",
        country="MX",
        category=Category.other,
        issuing_authority="The responsable (data controller) itself, under the Ley Federal "
        "de Protección de Datos Personales en Posesión de los Particulares",
        applies_to="both",
        anchors=[
            # A statute title is legislature-controlled, which is the one kind of
            # document-wide string a decisive anchor may rest on. The notice's own heading
            # ("AVISO DE PRIVACIDAD") is a document-class name every controller writes for
            # itself, so it stays non-decisive.
            _a(
                "Ley Federal de Protección de Datos Personales en Posesión de los Particulares",
                decisive=True,
                controls=Controls.STATUTE_TITLE,
            ),
            _a("AVISO DE PRIVACIDAD"),
            _a("derechos ARCO"),
            _a("Acceso, Rectificación, Cancelación y Oposición"),
            _a("Datos personales sensibles"),
            _a("Responsable del tratamiento"),
            _a("Transferencia de datos personales"),
            _a("Finalidades del tratamiento"),
            _a("Departamento de Datos Personales"),
        ],
        confusable_with={
            "mx_acta_constitutiva": "a privacy notice states how a company handles personal "
            "data; the acta states how the company was formed",
        },
        negative_anchors=[],
        fields=[
            FieldSpec(
                name="responsable",
                attribute_key="entity.legal_name",
                type="name",
                required=True,
                labels=_labels(
                    ["Responsable", "Responsable del tratamiento", "Razón social"],
                    ["Data controller"],
                ),
                validator="name",
            ),
            _address_field(
                key="address.registered",
                es=["Domicilio del responsable", "Domicilio"],
                en=["Controller address"],
            ),
            FieldSpec(
                name="finalidades",
                attribute_key="",
                type="string",
                multi=True,
                labels=_labels(
                    ["Finalidades del tratamiento", "Finalidades"], ["Purposes of processing"]
                ),
                locators=["table", "kv", "label"],
            ),
            FieldSpec(
                name="contact_email",
                attribute_key="identity.email",
                type="string",
                labels=_labels(["Correo electrónico", "Contacto"], ["Contact email"]),
                notes="The ARCO contact address. It belongs to a department far more often "
                "than to a person, but it is treated as personal data because sometimes "
                "it does not.",
                pii=True,
            ),
            _issue_date_field(),
            FieldSpec(
                name="last_updated",
                attribute_key="",
                type="date",
                labels=_labels(
                    ["Última actualización", "Fecha de última actualización"], ["Last updated"]
                ),
                validator="generic_date",
                notes="A privacy notice is versioned rather than issued once; the update date "
                "is what tells a reviewer whether it is current.",
            ),
        ],
        notes="Included because Mexican counterparty and vendor due-diligence packs carry it "
        "as a matter of course, not because it is a corporate registration document. "
        "It proves a controller published a notice — nothing about the entity itself.",
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
