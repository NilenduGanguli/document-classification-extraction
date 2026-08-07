"""India doctype pack — 36 document types, as data.

This is the knowledge base the service runs on. Everything here is a
:class:`~dce.models.DocTypeSpec`: no logic, no I/O, no imports beyond the models and the
registry loader. It is deliberately verbose — the anchors *are* the product.

Three rules were followed throughout, and they matter more than the volume:

**Bilingual anchors.** Most Indian government documents are printed in English and Hindi
together. An OCR read of an Aadhaar letter contains ``भारतीय विशिष्ट पहचान प्राधिकरण`` as
reliably as it contains ``UNIQUE IDENTIFICATION AUTHORITY OF INDIA``, and on a poor scan the
Devanagari header often survives when the English one does not. Anchors are declared in both.

**Decisive means decisive.** A decisive anchor carries ``fuse_weight_anchor`` (3.0) on its
own, so only issuing-authority headers and form numbers get the flag. Shared page furniture
that appears on every government document — ``GOVERNMENT OF INDIA`` / ``भारत सरकार`` — is
present as a *non-decisive* anchor on many specs, because it is real lexical evidence, and
decisive on none, because it separates nothing.

**No invented regexes.** Where an identifier has a published check digit (Aadhaar/Verhoeff,
GSTIN/mod-36, MRZ) the field names a validator that must enforce it. Where the format varies
by state or the check algorithm is not published (driving licence, EPIC legacy series, ration
card, NREGA job card, PPO, PAN's 10th character) the spec says so in ``notes`` and validates
structure only or not at all. A wrong regex silently rejects genuine documents, which for a
KYC gate means turning away real customers — strictly worse than passing the value through
to a human.
"""

from __future__ import annotations

from dce.models import Anchor, Category, DocTypeSpec, FieldSpec, Zone
from dce.registry.loader import RegistryError, register_all

# ---------------------------------------------------------------------------
# The RBI "Officially Valid Document" set.
#
# Master Direction on KYC lists exactly six OVDs: passport, driving licence, proof of
# possession of Aadhaar number, Voter's Identity Card issued by the Election Commission of
# India, job card issued by NREGA duly signed by an officer of the State Government, and
# the letter issued by the National Population Register containing name and address.
#
# Two things this set deliberately does NOT contain, because they are the two most common
# mistakes in a KYC registry:
#   * PAN — mandatory for tax, but never an OVD.
#   * Ration card — was withdrawn from the list; it is not an OVD today.
#
# Masked Aadhaar is included: the OVD is "proof of possession of an Aadhaar number", and the
# masked download is precisely the form a regulated entity is meant to accept and retain.
#
# Utility bills, registered lease agreements, employer allotment letters and pension payment
# orders are "deemed OVDs" for a limited address-update purpose only. They are marked
# officially_valid=False, with the nuance recorded in each spec's handling note.
# ---------------------------------------------------------------------------
IN_OVD_DOCTYPES: frozenset[str] = frozenset(
    {
        "in_passport",
        "in_driving_licence",
        "in_aadhaar",
        "in_aadhaar_masked",
        "in_voter_epic",
        "in_nrega_job_card",
        "in_npr_letter",
    }
)

#: The UIDAI handling obligation, attached verbatim to both Aadhaar variants.
AADHAAR_HANDLING = (
    "UIDAI MASKING OBLIGATION. The Aadhaar number is regulated by the Aadhaar (Targeted "
    "Delivery of Financial and Other Subsidies, Benefits and Services) Act, 2016 and the "
    "Aadhaar (Sharing of Information) Regulations, 2016. The full 12-digit number must not "
    "be displayed, published, printed or stored in the ordinary course: redact all but the "
    "last four digits at every boundary — UI, API response, export, audit log, error "
    "message and classification/extraction trace. Retain the full number only where a "
    "specific statutory authorisation exists for that use, and never as a convenience copy. "
    "The aadhaar_number field is pii=True; treat a leak of it as a reportable incident, not "
    "a data-quality issue. Prefer id.aadhaar_last4 or the Virtual ID for any downstream "
    "matching."
)

# ---------------------------------------------------------------------------
# Shared label vocabularies. Indian forms label the same field a dozen ways; the
# label-anchored locator fuzzy-matches these, so breadth here is cheap and accuracy is not.
# ---------------------------------------------------------------------------
_L_NAME = {
    "en": [
        "Name",
        "Full Name",
        "Name of Holder",
        "Holder Name",
        "Applicant Name",
        "Name of Applicant",
    ],
    "hi": ["नाम", "पूरा नाम", "धारक का नाम"],
}
_L_FATHER = {
    "en": ["Father's Name", "Fathers Name", "Father Name", "S/O", "Son of", "D/O", "Daughter of"],
    "hi": ["पिता का नाम", "पिता"],
}
_L_MOTHER = {
    "en": ["Mother's Name", "Mother Name", "Name of Mother"],
    "hi": ["माता का नाम", "माता"],
}
_L_SPOUSE = {
    "en": ["Spouse's Name", "Husband's Name", "Wife's Name", "W/O", "H/O", "Spouse Name"],
    "hi": ["पति का नाम", "पत्नी का नाम"],
}
_L_DOB = {
    "en": ["Date of Birth", "DOB", "D.O.B.", "Birth Date", "Date Of Birth"],
    "hi": ["जन्म तिथि", "जन्म की तारीख", "जन्म दिनांक"],
}
_L_SEX = {"en": ["Gender", "Sex", "MALE", "FEMALE"], "hi": ["लिंग", "पुरुष", "महिला"]}
_L_ADDRESS = {
    "en": ["Address", "Residential Address", "Permanent Address", "Present Address", "Addr"],
    "hi": ["पता", "निवास का पता", "स्थायी पता"],
}
_L_PIN = {"en": ["PIN Code", "PIN", "Pincode", "Pin Code", "Postal Code"], "hi": ["पिन कोड"]}
_L_ISSUE = {
    "en": ["Date of Issue", "Issue Date", "Issued on", "DOI", "Date of Issuance"],
    "hi": ["जारी करने की तिथि", "निर्गम तिथि"],
}
_L_EXPIRY = {
    "en": ["Date of Expiry", "Valid Till", "Valid Upto", "Valid Up to", "Expiry Date", "DOE"],
    "hi": ["वैधता तिथि", "वैध तिथि तक"],
}
_L_ACCOUNT_NO = {
    "en": ["Account Number", "Account No", "A/C No", "A/c Number", "Acct No"],
    "hi": ["खाता संख्या", "खाता क्रमांक"],
}
_L_IFSC = {"en": ["IFSC", "IFSC Code", "IFS Code"], "hi": ["आईएफएससी"]}
_L_BANK = {"en": ["Bank", "Bank Name", "Name of Bank"], "hi": ["बैंक", "बैंक का नाम"]}
_L_BRANCH = {"en": ["Branch", "Branch Name", "Branch Address"], "hi": ["शाखा"]}
_L_PAN = {"en": ["PAN", "PAN No", "Permanent Account Number", "PAN of the Deductee"], "hi": ["पैन"]}
_L_AADHAAR = {
    "en": ["Aadhaar", "Aadhaar Number", "Aadhaar No", "UID", "UID No"],
    "hi": ["आधार", "आधार संख्या"],
}
_L_CONSUMER = {
    "en": [
        "Consumer Number",
        "Consumer No",
        "Consumer ID",
        "Connection Number",
        "CA Number",
        "Service Number",
        "K Number",
        "Account ID",
    ],
    "hi": ["उपभोक्ता संख्या", "उपभोक्ता क्रमांक"],
}
_L_BILL_AMOUNT = {
    "en": ["Amount Payable", "Bill Amount", "Total Amount", "Net Payable", "Current Bill Amount"],
    "hi": ["देय राशि", "कुल राशि"],
}
_L_DUE_DATE = {
    "en": ["Due Date", "Pay by Date", "Last Date of Payment"],
    "hi": ["देय तिथि", "अंतिम तिथि"],
}
_L_BILL_PERIOD = {
    "en": ["Bill Period", "Billing Period", "Bill Month", "Period", "Reading Period"],
    "hi": ["बिल अवधि", "बिल माह"],
}
_L_ENTITY = {
    "en": [
        "Legal Name",
        "Legal Name of Business",
        "Name of Company",
        "Company Name",
        "Name of the Company",
        "Name of Firm",
        "Name of the Firm",
    ],
    "hi": ["कंपनी का नाम", "फर्म का नाम"],
}
_L_REG_OFFICE = {
    "en": [
        "Registered Office",
        "Address of Registered Office",
        "Principal Place of Business",
        "Registered Address",
    ],
    "hi": ["पंजीकृत कार्यालय"],
}
_L_CERT_NO = {
    "en": [
        "Certificate Number",
        "Certificate No",
        "Registration Number",
        "Registration No",
        "Serial Number",
        "Sl. No",
        "Reference Number",
    ],
    "hi": ["प्रमाण पत्र संख्या", "पंजीकरण संख्या", "क्रमांक"],
}


def _f(
    name: str,
    attribute_key: str = "",
    *,
    kind: str = "string",
    required: bool = False,
    pii: bool = False,
    multi: bool = False,
    labels: dict[str, list[str]] | None = None,
    pattern: str | None = None,
    validator: str | None = None,
    locators: tuple[str, ...] = ("kv", "label"),
    notes: str = "",
) -> FieldSpec:
    """Build a :class:`~dce.models.FieldSpec` with the pack's defaults.

    Args:
        name: snake_case field name, unique within its doctype.
        attribute_key: Canonical dotted key from ``loader.ATTRIBUTE_KEYS``; empty for
            fields that are genuinely doc-local and do not belong in the merge view.
        kind: ``FieldSpec.type`` — spelled ``kind`` here to avoid shadowing ``type``.
        required: Whether absence should surface in ``missing_required``.
        pii: Whether the value must be masked at every boundary.
        multi: Whether several concurrent values are legitimate.
        labels: Per-language label strings for the label-anchored locator.
        pattern: Value-shape regex, used to reject a wrong binding.
        validator: Name declared in ``loader.VALIDATOR_CONTRACT``. Left unset on a
            ``name`` or ``address`` field, the matching generic validator is applied —
            see below.
        locators: Locator hints in priority order.
        notes: Anything a reviewer needs to know — especially format uncertainty.

    Returns:
        The constructed FieldSpec.
    """
    # Name and address fields get their generic validator by default. This is not
    # convenience: the failure it prevents is specific and common. The label-anchored
    # locator searches right-of and below a label, so on a form where a date sits to the
    # right of "Address" it will happily bind "12/03/1999" to the address field, and a
    # bare digit run to a name field. The `name` and `address` validators exist precisely
    # to reject those two bindings, and a doctype pack with sixty name fields will forget
    # to ask for them one field at a time. Pass an explicit validator to override.
    if validator is None:
        validator = {"name": "name", "address": "address"}.get(kind)
    return FieldSpec(
        name=name,
        attribute_key=attribute_key,
        type=kind,
        required=required,
        pii=pii,
        multi=multi,
        labels=dict(labels or {}),
        pattern=pattern,
        validator=validator,
        locators=list(locators),
        notes=notes,
    )


def _name_field(*, required: bool = True) -> FieldSpec:
    """The holder's name — present on almost every individual document."""
    return _f(
        "name", "identity.full_name", kind="name", required=required, pii=True, labels=_L_NAME
    )


def _father_field() -> FieldSpec:
    """Father's / guardian's name. Near-universal on Indian identity documents, and often
    the only disambiguator between two people with the same name and date of birth."""
    return _f("father_name", "identity.father_name", kind="name", pii=True, labels=_L_FATHER)


def _dob_field(*, required: bool = False) -> FieldSpec:
    return _f(
        "date_of_birth",
        "identity.date_of_birth",
        kind="date",
        required=required,
        pii=True,
        labels=_L_DOB,
        validator="generic_date",
    )


def _sex_field() -> FieldSpec:
    return _f(
        "gender",
        "identity.sex",
        labels=_L_SEX,
        pattern=r"(?i)^(m|f|t|male|female|transgender|पुरुष|महिला|अन्य)$",
        notes="Indian forms use MALE/FEMALE/TRANSGENDER and the Hindi पुरुष/महिला; "
        "'T'/'TRANSGENDER' is a valid third value on Aadhaar and passports.",
    )


def _address_field(
    attribute_key: str = "address.residential", *, required: bool = False
) -> FieldSpec:
    return _f(
        "address",
        attribute_key,
        kind="address",
        required=required,
        pii=True,
        labels=_L_ADDRESS,
        locators=("kv", "label", "regex"),
    )


def _pincode_field() -> FieldSpec:
    return _f(
        "pincode",
        "address.postal_code",
        labels=_L_PIN,
        pattern=r"\b[1-9]\d{5}\b",
        locators=("label", "regex", "kv"),
    )


def _issue_date_field(*, required: bool = False) -> FieldSpec:
    return _f(
        "issue_date",
        "doc.issue_date",
        kind="date",
        required=required,
        labels=_L_ISSUE,
        validator="generic_date",
    )


def _expiry_field(*, required: bool = False) -> FieldSpec:
    return _f(
        "expiry_date",
        "doc.expiry_date",
        kind="date",
        required=required,
        labels=_L_EXPIRY,
        validator="generic_date",
    )


#: Page furniture shared by most Government of India documents. Never decisive — it is on
#: everything from an Aadhaar letter to a caste certificate — but it is real evidence that
#: the document is Indian and government-issued, so the lexical tier should see it.
_GOI_FURNITURE = (
    Anchor(text="GOVERNMENT OF INDIA"),
    Anchor(text="भारत सरकार", lang="hi"),
)


_SPECS: list[DocTypeSpec] = []


# ===========================================================================
# Identity — the RBI OVD core
# ===========================================================================
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_aadhaar",
            label="Aadhaar Card / e-Aadhaar (full number)",
            country="IN",
            category=Category.identity,
            issuing_authority="Unique Identification Authority of India (UIDAI)",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(text="UNIQUE IDENTIFICATION AUTHORITY OF INDIA", decisive=True),
                Anchor(text="भारतीय विशिष्ट पहचान प्राधिकरण", lang="hi", decisive=True),
                Anchor(text="AADHAAR", decisive=True, zone=Zone.title),
                Anchor(text="आधार", lang="hi", decisive=True, zone=Zone.title),
                Anchor(text="आधार", lang="hi"),
                Anchor(text="Aadhaar"),
                Anchor(text="मेरा आधार, मेरी पहचान", lang="hi"),
                Anchor(text="आम आदमी का अधिकार", lang="hi"),
                Anchor(text="Aadhaar is a proof of identity, not of citizenship"),
                Anchor(text="आधार पहचान का प्रमाण है, नागरिकता का नहीं", lang="hi"),
                Anchor(text="Aadhaar is valid throughout the country"),
                Anchor(text="Enrolment No"),
                Anchor(text="नामांकन संख्या", lang="hi"),
                Anchor(text="Virtual ID"),
                Anchor(text="Year of Birth"),
                Anchor(text="जन्म वर्ष", lang="hi"),
                Anchor(text="uidai.gov.in"),
                Anchor(text="help@uidai.gov.in"),
                Anchor(text="Download Date"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"(?<!\d)(?<!\d )[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\s?\d)"],
            confusable_with={
                "in_aadhaar_masked": (
                    "the masked download prints XXXX XXXX before the last four digits; if any "
                    "masked-number pattern is present this is in_aadhaar_masked"
                ),
                "in_form60": (
                    "Form 60 quotes an Aadhaar number as a field value but is headed "
                    "'FORM NO. 60' and cites rule 114B"
                ),
                "in_ckyc_record": (
                    "a CKYC record also carries an Aadhaar number; it is headed "
                    "'CENTRAL KYC REGISTRY' and carries a 14-digit KIN"
                ),
            },
            negative_anchors=[
                "Masked Aadhaar",
                "FORM NO. 60",
                "rule 114B",
                "CENTRAL KYC REGISTRY",
                "PERMANENT ACCOUNT NUMBER CARD",
            ],
            handling=AADHAAR_HANDLING,
            fields=[
                _f(
                    "aadhaar_number",
                    "id.aadhaar",
                    kind="id",
                    required=True,
                    pii=True,
                    labels=_L_AADHAAR,
                    pattern=r"(?<!\d)(?<!\d )[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\s?\d)",
                    validator="verhoeff_aadhaar",
                    locators=("regex", "label", "kv"),
                    notes="12 digits, first digit 2-9, Verhoeff check digit. MUST be masked to "
                    "the last four digits everywhere except a statutorily authorised store.",
                ),
                _name_field(),
                _dob_field(),
                _f(
                    "year_of_birth",
                    "identity.year_of_birth",
                    pii=True,
                    labels={"en": ["Year of Birth", "YOB"], "hi": ["जन्म वर्ष"]},
                    pattern=r"\b(19|20)\d{2}\b",
                    notes="Aadhaar prints a bare year instead of a full date when the enrolee "
                    "could not evidence one. Both forms are legitimate; never synthesise a "
                    "01/01 date from a year.",
                ),
                _sex_field(),
                _f(
                    "guardian_name",
                    "identity.guardian_name",
                    kind="name",
                    pii=True,
                    labels={
                        "en": ["S/O", "D/O", "C/O", "W/O", "Care of"],
                        "hi": ["पिता", "पति", "द्वारा"],
                    },
                    notes="e-Aadhaar prints the relation inline in the address block as "
                    "S/O, D/O, W/O or C/O rather than as a separate labelled field.",
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "vid",
                    "id.aadhaar_vid",
                    kind="id",
                    pii=True,
                    labels={"en": ["VID", "Virtual ID"], "hi": ["वर्चुअल आईडी"]},
                    pattern=r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b",
                    locators=("label", "regex", "kv"),
                    notes="16 digits. Believed to carry a Verhoeff check digit like the Aadhaar "
                    "number itself, but UIDAI does not publish the VID algorithm — no "
                    "validator is named here rather than risk rejecting genuine VIDs.",
                ),
                _f(
                    "enrolment_number",
                    "id.aadhaar_enrolment",
                    kind="id",
                    pii=True,
                    labels={"en": ["Enrolment No", "Enrollment No", "EID"], "hi": ["नामांकन संख्या"]},
                    locators=("label", "kv", "regex"),
                    notes="Printed on the acknowledgement slip as a 14-digit enrolment id "
                    "followed by a date-time stamp (1234/56789/01234 + DD/MM/YYYY HH:MM:SS). "
                    "Layout varies between the slip and the e-Aadhaar footer; not regexed here.",
                ),
                _f(
                    "mobile",
                    "identity.mobile",
                    pii=True,
                    pattern=r"(?:\+?91[\s-]?)?[6-9]\d{9}",
                    labels={"en": ["Mobile", "Mobile No", "Registered Mobile"], "hi": ["मोबाइल"]},
                ),
                _issue_date_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_aadhaar_masked",
            label="Masked Aadhaar / masked e-Aadhaar (last 4 digits only)",
            country="IN",
            category=Category.identity,
            issuing_authority="Unique Identification Authority of India (UIDAI)",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                # in_aadhaar and in_aadhaar_masked are the same document with one field
                # redacted, so the only honest discriminators are this spec's masking
                # marker, in_aadhaar's matching negative anchor, and — decisively — whether
                # a Verhoeff-valid 12-digit number is present at all. The issuing-authority
                # headers are shared and are therefore NOT decisive here: making them
                # decisive was tried and changes nothing, because dce.classify.anchors
                # saturates both doctypes at its confidence ceiling (see the note on
                # id_patterns below).
                Anchor(text="Masked Aadhaar", decisive=True),
                Anchor(text="UNIQUE IDENTIFICATION AUTHORITY OF INDIA"),
                Anchor(text="भारतीय विशिष्ट पहचान प्राधिकरण", lang="hi"),
                Anchor(text="AADHAAR", zone=Zone.title),
                Anchor(text="आधार", lang="hi", zone=Zone.title),
                Anchor(text="आधार", lang="hi"),
                Anchor(text="मेरा आधार, मेरी पहचान", lang="hi"),
                Anchor(text="Aadhaar is a proof of identity, not of citizenship"),
                Anchor(text="आधार पहचान का प्रमाण है, नागरिकता का नहीं", lang="hi"),
                Anchor(text="Virtual ID"),
                Anchor(text="Year of Birth"),
                Anchor(text="uidai.gov.in"),
                Anchor(text="Download Date"),
                *_GOI_FURNITURE,
            ],
            # KNOWN LIMITATION, recorded here because it is a property of the pair and not
            # of either spec alone. These masking patterns carry no check digit, so
            # dce.classify.anchors scores them as "matched but unverified" (0.25) while a
            # real Aadhaar scores "checksum verified" (3.0). That asymmetry ought to settle
            # the pair outright. It currently does not: anchor_scores saturates both
            # doctypes at its confidence ceiling (0.97) before fusion, so the checksum term
            # is invisible and the near-identical lexical profiles decide by noise. The pair
            # therefore abstains to the human queue — safe, and the designed behaviour for
            # an unresolved call, but not the intended one. No amount of anchor tuning in
            # this pack fixes it; the ceiling has to stop saturating.
            id_patterns=[
                r"\b[Xx]{4}\s?[Xx]{4}\s?\d{4}\b",
                r"\b[Xx]{8}\s?\d{4}\b",
            ],
            confusable_with={
                "in_aadhaar": (
                    "the unmasked card prints all 12 digits and they satisfy the Verhoeff "
                    "check; presence of an XXXX XXXX NNNN pattern, and absence of any "
                    "checksum-valid 12-digit number, is what makes this the masked variant"
                ),
            },
            negative_anchors=[],
            handling=AADHAAR_HANDLING
            + " This variant is already masked at source and is the preferred form to "
            "collect and retain.",
            fields=[
                _f(
                    "aadhaar_last4",
                    "id.aadhaar_last4",
                    kind="id",
                    required=True,
                    pii=True,
                    labels=_L_AADHAAR,
                    pattern=r"(?:[Xx]{4}\s?[Xx]{4}|[Xx]{8})\s?(\d{4})",
                    locators=("regex", "label", "kv"),
                    notes="Only the trailing four digits are recoverable, and that is the "
                    "point — do not attempt to reconstruct or look up the full number.",
                ),
                _f(
                    "masked_aadhaar_number",
                    "",
                    labels=_L_AADHAAR,
                    pattern=r"\b(?:[Xx]{4}\s?[Xx]{4}|[Xx]{8})\s?\d{4}\b",
                    locators=("regex", "label"),
                    notes="The masked string exactly as printed, kept for display parity with "
                    "the source document. Deliberately has no attribute_key: it must not "
                    "merge into an identity view as if it were an identifier.",
                ),
                _name_field(),
                _dob_field(),
                _f(
                    "year_of_birth",
                    "identity.year_of_birth",
                    pii=True,
                    labels={"en": ["Year of Birth", "YOB"], "hi": ["जन्म वर्ष"]},
                    pattern=r"\b(19|20)\d{2}\b",
                ),
                _sex_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "vid",
                    "id.aadhaar_vid",
                    kind="id",
                    pii=True,
                    labels={"en": ["VID", "Virtual ID"], "hi": ["वर्चुअल आईडी"]},
                    pattern=r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b",
                    locators=("label", "regex", "kv"),
                    notes="The VID is not masked on the masked download; it is the "
                    "UIDAI-sanctioned substitute for the Aadhaar number in downstream matching.",
                ),
                _issue_date_field(),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_pan",
            label="PAN Card (Permanent Account Number)",
            country="IN",
            category=Category.identity,
            issuing_authority="Income Tax Department, Government of India",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="PERMANENT ACCOUNT NUMBER CARD", decisive=True),
                Anchor(text="स्थायी लेखा संख्या कार्ड", lang="hi", decisive=True),
                # Both spellings of the issuer's name were decisive here, gated to the title
                # zone. The Income Tax Department issues four doctypes in this registry —
                # in_pan, in_form16, in_form60 and in_itr_acknowledgement — and prints its
                # name at the head of all of them, so the string proves the *issuer*, never
                # the document. in_form16 already claimed "आयकर विभाग" non-decisively, which
                # is what made the asymmetry visible. Demoted to the plain non-decisive
                # entries below; "PERMANENT ACCOUNT NUMBER CARD" is the string only a PAN card
                # bears, and it stays decisive.
                Anchor(text="INCOME TAX DEPARTMENT"),
                Anchor(text="आयकर विभाग", lang="hi"),
                Anchor(text="PERMANENT ACCOUNT NUMBER"),
                Anchor(text="स्थायी लेखा संख्या", lang="hi"),
                Anchor(text="GOVT. OF INDIA"),
                Anchor(text="Signature"),
                Anchor(text="हस्ताक्षर", lang="hi"),
                Anchor(text="पिता का नाम", lang="hi"),
                Anchor(text="जन्म की तारीख", lang="hi"),
                Anchor(text="incometax.gov.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[A-Z]{5}\d{4}[A-Z]\b"],
            confusable_with={
                "in_form60": (
                    "Form 60 is the declaration filed *instead of* a PAN and is headed 'FORM NO. "
                    "60'"
                ),
                "in_form16": (
                    "Form 16 quotes a PAN but is headed 'FORM NO. 16' and names a TAN and a "
                    "deductor"
                ),
                "in_itr_acknowledgement": (
                    "ITR-V quotes a PAN but is headed 'INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT'"
                ),
                "in_certificate_incorporation": (
                    "a certificate of incorporation prints the company's PAN on its face; it "
                    "is headed 'CERTIFICATE OF INCORPORATION' and carries a 21-character CIN"
                ),
                "in_gst_certificate": (
                    "a GST certificate embeds the entity PAN inside its GSTIN; it is headed "
                    "'FORM GST REG-06'"
                ),
            },
            # A PAN card is a card. Every document below merely *quotes* a PAN, and each of
            # them contains a PAN-shaped string that would otherwise pull the PAN doctype up
            # on a document it has no business winning. This list is the difference between
            # "contains a PAN" and "is a PAN card".
            negative_anchors=[
                "FORM NO. 60",
                "FORM NO. 16",
                "INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT",
                "TAN of the Deductor",
                "CERTIFICATE OF INCORPORATION",
                "MINISTRY OF CORPORATE AFFAIRS",
                "Corporate Identity Number",
                "FORM GST REG-06",
                "STATEMENT OF ACCOUNT",
            ],
            handling=(
                "PAN is NOT an RBI Officially Valid Document — it evidences tax identity, not "
                "identity or address for KYC. Do not let a PAN alone satisfy an OVD requirement."
            ),
            fields=[
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    required=True,
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("regex", "label", "kv"),
                    notes="Structure only. The 4th character encodes holder type (P individual, "
                    "C company, H HUF, F firm, A AOP, T trust, B BOI, L local authority, "
                    "J artificial juridical person, G government) and the 5th is the first "
                    "letter of the surname or entity name. The 10th character is a check "
                    "character, but the Income Tax Department has never published the "
                    "algorithm — do not compute or enforce one.",
                ),
                _name_field(),
                _father_field(),
                _dob_field(),
                _f(
                    "holder_type",
                    "entity.constitution",
                    labels={"en": ["Status", "Constitution", "Category of Holder"]},
                    locators=("kv", "label"),
                    notes="Derivable from the 4th character of the PAN; prefer the derived "
                    "value over an OCR read of the printed status when the two disagree.",
                ),
                _issue_date_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_passport",
            label="Indian Passport",
            country="IN",
            category=Category.identity,
            issuing_authority="Ministry of External Affairs, Government of India",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(text="REPUBLIC OF INDIA", decisive=True),
                Anchor(text="भारत गणराज्य", lang="hi", decisive=True),
                Anchor(text="PASSPORT"),
                Anchor(text="पासपोर्ट", lang="hi"),
                Anchor(text="Ministry of External Affairs"),
                # "Type", "Surname", "Nationality" and "Place of Birth" were removed. They
                # are ICAO 9303 visual-inspection-zone labels, printed identically on every
                # state's passport, so they cannot contribute evidence that *this* passport
                # is Indian — which is the only question this spec exists to answer. What
                # they did contribute was score on documents that merely discuss identity
                # documents: this spec ranked first on a birth-registration worksheet and on
                # a Canadian social-insurance-number application form, neither of which is a
                # passport, purely on "type", "place of birth" and "nationality". "Type" is
                # additionally a four-letter ordinary English word, the token class this
                # registry has been burned by before.
                Anchor(text="Country Code"),
                Anchor(text="Passport No"),
                Anchor(text="Given Name"),
                Anchor(text="Place of Issue"),
                Anchor(text="जन्म स्थान", lang="hi"),
                Anchor(text="जारी करने का स्थान", lang="hi"),
                Anchor(text="File No"),
                Anchor(text="Old Passport No"),
                Anchor(text="INDIAN"),
                Anchor(text="passportindia.gov.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[
                r"\bP<IND[A-Z<]{2,}",
                r"\b[A-PR-WYa-pr-wy][0-9]{7}\b",
            ],
            confusable_with={
                "xx_passport_generic": (
                    "the Indian book prints 'REPUBLIC OF INDIA' / 'भारत गणराज्य' and an MRZ issuing"
                    " code of IND"
                ),
                "in_voter_epic": (
                    "both are photo IDs; the passport carries a two-line MRZ and 'REPUBLIC OF "
                    "INDIA'"
                ),
            },
            negative_anchors=["ELECTION COMMISSION OF INDIA", "DRIVING LICENCE"],
            handling=(
                "An RBI Officially Valid Document for both identity and address. The address "
                "page is a separate scan from the data page — a passport submitted as address "
                "proof without the back page does not satisfy the address requirement."
            ),
            fields=[
                _f(
                    "passport_number",
                    "id.passport_number",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Passport No", "Passport Number", "Passport No."],
                        "hi": ["पासपोर्ट संख्या"],
                    },
                    pattern=r"\b[A-Z][0-9]{7}\b",
                    validator="in_passport",
                    locators=("mrz", "label", "kv", "regex"),
                    notes="Ordinary passports are one letter plus seven digits. The MRZ is the "
                    "authoritative read when both are present — it carries check digits and "
                    "the printed line does not.",
                ),
                _f(
                    "surname",
                    "identity.surname",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={"en": ["Surname", "Family Name"], "hi": ["उपनाम"]},
                    locators=("mrz", "kv", "label"),
                ),
                _f(
                    "given_names",
                    "identity.given_names",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Given Name", "Given Names", "First Name"],
                        "hi": ["दिया गया नाम"],
                    },
                    locators=("mrz", "kv", "label"),
                ),
                _dob_field(required=True),
                _sex_field(),
                _f(
                    "nationality",
                    "identity.nationality",
                    labels={"en": ["Nationality"], "hi": ["राष्ट्रीयता"]},
                    locators=("mrz", "kv", "label"),
                ),
                _f(
                    "place_of_birth",
                    "identity.place_of_birth",
                    pii=True,
                    labels={"en": ["Place of Birth"], "hi": ["जन्म स्थान"]},
                ),
                _f(
                    "place_of_issue",
                    "doc.place_of_issue",
                    labels={"en": ["Place of Issue"], "hi": ["जारी करने का स्थान"]},
                ),
                _issue_date_field(required=True),
                _expiry_field(required=True),
                _father_field(),
                _f("mother_name", "identity.mother_name", kind="name", pii=True, labels=_L_MOTHER),
                _f("spouse_name", "identity.spouse_name", kind="name", pii=True, labels=_L_SPOUSE),
                _address_field(),
                _pincode_field(),
                _f(
                    "mrz",
                    "",
                    pattern=r"P<IND[A-Z0-9<]{39}",
                    validator="mrz_td3",
                    locators=("mrz", "regex"),
                    notes="ICAO 9303 TD3: two 44-character lines with per-field and composite "
                    "check digits. When the MRZ validates it outranks every printed field.",
                ),
                _f(
                    "file_number",
                    "doc.reference_number",
                    labels={"en": ["File No", "File Number"]},
                    notes=(
                        "Printed on the last page. Format is issuing-office specific; not regexed."
                    ),
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_voter_epic",
            label="Voter ID / Elector Photo Identity Card (EPIC)",
            country="IN",
            category=Category.identity,
            issuing_authority="Election Commission of India",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(text="ELECTION COMMISSION OF INDIA", decisive=True),
                Anchor(text="भारत निर्वाचन आयोग", lang="hi", decisive=True),
                Anchor(text="ELECTOR PHOTO IDENTITY CARD", decisive=True),
                Anchor(text="निर्वाचक फोटो पहचान पत्र", lang="hi", decisive=True),
                Anchor(text="मतदाता पहचान पत्र", lang="hi"),
                Anchor(text="Elector's Name"),
                Anchor(text="निर्वाचक का नाम", lang="hi"),
                Anchor(text="Assembly Constituency"),
                Anchor(text="विधान सभा निर्वाचन क्षेत्र", lang="hi"),
                Anchor(text="Parliamentary Constituency"),
                Anchor(text="Electoral Registration Officer"),
                Anchor(text="Part No"),
                Anchor(text="Serial No"),
                Anchor(text="Age as on"),
                Anchor(text="Facsimile Signature"),
                Anchor(text="eci.gov.in"),
                Anchor(text="nvsp.in"),
            ],
            id_patterns=[r"\b[A-Z]{3}\d{7}\b"],
            confusable_with={
                "in_driving_licence": (
                    "both are state-issued photo cards; the EPIC names the Election Commission and "
                    "an assembly constituency"
                ),
                "xx_photo_id_generic": (
                    "the EPIC carries 'ELECTION COMMISSION OF INDIA' / 'भारत निर्वाचन आयोग'"
                ),
            },
            negative_anchors=[
                "DRIVING LICENCE",
                "TRANSPORT DEPARTMENT",
                "UNIQUE IDENTIFICATION AUTHORITY OF INDIA",
            ],
            handling="An RBI Officially Valid Document for identity and address.",
            fields=[
                _f(
                    "epic_number",
                    "id.voter_epic",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["EPIC No", "EPIC Number", "IDENTITY CARD NUMBER", "Card No"],
                        "hi": ["पहचान पत्र संख्या"],
                    },
                    pattern=r"\b[A-Z]{3}\d{7}\b",
                    validator="epic_voter",
                    locators=("regex", "label", "kv"),
                    notes="Current series is three letters (the state/AC functional code) plus "
                    "seven digits. Several older state-issued series used slash-separated "
                    "numeric layouts and are still in circulation — the validator must not "
                    "hard-reject them, only decline to assert the modern format.",
                ),
                _name_field(),
                _father_field(),
                _f(
                    "relation_name",
                    "identity.spouse_name",
                    kind="name",
                    pii=True,
                    labels={
                        "en": [
                            "Husband's Name",
                            "Mother's Name",
                            "Relation's Name",
                            "Relation Name",
                        ],
                        "hi": ["पति का नाम", "माता का नाम", "संबंधी का नाम"],
                    },
                    notes=(
                        "The EPIC prints one relation line whose type varies (father, mother, "
                        "husband). Bind the printed relation type alongside the name; do not "
                        "assume father."
                    ),
                ),
                _dob_field(),
                _f(
                    "age",
                    "identity.age",
                    kind="number",
                    labels={"en": ["Age", "Age as on"], "hi": ["आयु"]},
                    pattern=r"\b\d{1,3}\b",
                    notes="Older cards print an age as of a stated qualifying date instead of a "
                    "date of birth. Never convert an age to a birth date.",
                ),
                _sex_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "assembly_constituency",
                    "",
                    labels={
                        "en": ["Assembly Constituency", "AC No"],
                        "hi": ["विधान सभा निर्वाचन क्षेत्र"],
                    },
                ),
                _issue_date_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_driving_licence",
            label="Indian Driving Licence",
            country="IN",
            category=Category.identity,
            issuing_authority="State Transport Department / Regional Transport Office (RTO)",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(text="DRIVING LICENCE", decisive=True),
                Anchor(text="DRIVING LICENSE", decisive=True),
                Anchor(text="चालक अनुज्ञप्ति", lang="hi", decisive=True),
                Anchor(text="ड्राइविंग लाइसेंस", lang="hi", decisive=True),
                Anchor(text="AUTHORISATION TO DRIVE FOLLOWING CLASS OF VEHICLES", decisive=True),
                Anchor(text="THE UNION OF INDIA"),
                Anchor(text="भारत संघ", lang="hi"),
                Anchor(text="TRANSPORT DEPARTMENT"),
                Anchor(text="परिवहन विभाग", lang="hi"),
                Anchor(text="Motor Vehicles Act"),
                Anchor(text="Class of Vehicle"),
                Anchor(text="COV"),
                Anchor(text="LMV"),
                Anchor(text="MCWG"),
                Anchor(text="Issuing Authority"),
                Anchor(text="Blood Group"),
                Anchor(text="रक्त समूह", lang="hi"),
                Anchor(text="Valid Till"),
                Anchor(text="Non-Transport"),
                Anchor(text="parivahan.gov.in"),
                Anchor(text="sarathi"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[A-Z]{2}[-\s]?\d{2}[-\s]?\d{4}[-\s]?\d{7}\b", r"\b[A-Z]{2}\d{13}\b"],
            confusable_with={
                "in_voter_epic": (
                    "both are state-issued photo cards; the licence names a class of vehicle and an"
                    " RTO"
                ),
                "xx_photo_id_generic": (
                    "the licence carries 'DRIVING LICENCE' and a vehicle class table"
                ),
            },
            negative_anchors=[
                "ELECTION COMMISSION OF INDIA",
                "REGISTRATION CERTIFICATE OF VEHICLE",
            ],
            handling=(
                "An RBI Officially Valid Document for identity and address. An expired licence "
                "is not an OVD — check expiry_date, do not just check that the field parsed."
            ),
            fields=[
                _f(
                    "licence_number",
                    "id.driving_licence",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["DL No", "Licence No", "License No", "DL Number", "Licence Number"],
                        "hi": ["अनुज्ञप्ति संख्या", "लाइसेंस संख्या"],
                    },
                    validator="in_dl",
                    locators=("label", "kv", "regex"),
                    notes="Two-letter state code plus two-digit RTO code, then a state-specific "
                    "serial — most commonly a four-digit year and a seven-digit serial, but "
                    "the layout and the separators genuinely differ between states. Only the "
                    "state+RTO prefix and the total digit count are safe to enforce.",
                ),
                _name_field(),
                _father_field(),
                _dob_field(),
                _f(
                    "blood_group",
                    "",
                    labels={"en": ["Blood Group", "BG"], "hi": ["रक्त समूह"]},
                    pattern=r"(?i)^(A|B|AB|O)[+-]?(POS|NEG|POSITIVE|NEGATIVE)?$",
                ),
                _address_field(required=True),
                _pincode_field(),
                _issue_date_field(),
                _expiry_field(required=True),
                _f(
                    "vehicle_classes",
                    "",
                    multi=True,
                    labels={
                        "en": ["Class of Vehicle", "COV", "Authorisation"],
                        "hi": ["वाहन का वर्ग"],
                    },
                    locators=("table", "label", "kv"),
                    notes="Printed as a table of class codes (LMV, MCWG, TRANS, HGMV) with their "
                    "own issue dates; read from the table, not the surrounding prose.",
                ),
                _f(
                    "issuing_rto",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Issuing Authority", "RTO", "Issued By"],
                        "hi": ["जारीकर्ता प्राधिकारी"],
                    },
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_nrega_job_card",
            label="MGNREGA Job Card",
            country="IN",
            category=Category.identity,
            issuing_authority="Ministry of Rural Development / State Government (Gram Panchayat)",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(
                    text="MAHATMA GANDHI NATIONAL RURAL EMPLOYMENT GUARANTEE ACT", decisive=True
                ),
                Anchor(
                    text="महात्मा गांधी राष्ट्रीय ग्रामीण रोजगार गारंटी अधिनियम", lang="hi", decisive=True
                ),
                Anchor(text="NATIONAL RURAL EMPLOYMENT GUARANTEE ACT", decisive=True),
                Anchor(text="JOB CARD", decisive=True, zone=Zone.title),
                Anchor(text="जॉब कार्ड", lang="hi", decisive=True, zone=Zone.title),
                Anchor(text="JOB CARD"),
                Anchor(text="जॉब कार्ड", lang="hi"),
                Anchor(text="MGNREGA"),
                Anchor(text="NREGA"),
                Anchor(text="Ministry of Rural Development"),
                Anchor(text="ग्रामीण विकास मंत्रालय", lang="hi"),
                Anchor(text="Gram Panchayat"),
                Anchor(text="ग्राम पंचायत", lang="hi"),
                Anchor(text="Head of Household"),
                Anchor(text="Name of Household Head"),
                Anchor(text="Employment Demanded"),
                Anchor(text="Days Worked"),
                Anchor(text="Muster Roll"),
                Anchor(text="Block"),
                Anchor(text="nrega.nic.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[],
            confusable_with={
                "in_ration_card": (
                    "both list household members with a village address; the job card names "
                    "the MGNREGA Act and records days of employment, the ration card names "
                    "the food & civil supplies department and a fair price shop"
                ),
            },
            negative_anchors=["Public Distribution System", "Fair Price Shop", "राशन कार्ड"],
            handling=(
                "An RBI Officially Valid Document ONLY when duly signed by an officer of the "
                "State Government — an unsigned job card does not satisfy the OVD requirement. "
                "Check for the signature/attestation block before accepting it as one."
            ),
            fields=[
                _f(
                    "job_card_number",
                    "id.nrega_job_card",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Job Card No", "Job Card Number", "Registration No"],
                        "hi": ["जॉब कार्ड संख्या", "पंजीकरण संख्या"],
                    },
                    locators=("label", "kv", "table"),
                    notes="No national format exists. States compose it from state, district, "
                    "block, panchayat and household codes with varying separators "
                    "(e.g. 'XX-02-003-004-001/123'). No regex is declared here on purpose.",
                ),
                _f(
                    "household_head",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Name of Household Head", "Head of Household", "Applicant Name"],
                        "hi": ["परिवार के मुखिया का नाम"],
                    },
                ),
                _father_field(),
                _f(
                    "category",
                    "identity.category",
                    labels={"en": ["Category", "Caste Category"], "hi": ["श्रेणी", "जाति"]},
                    pattern=r"(?i)^(SC|ST|OBC|GEN|GENERAL|OTHERS)$",
                ),
                _f(
                    "village",
                    "address.village",
                    labels={
                        "en": ["Village", "Gram Panchayat", "Panchayat"],
                        "hi": ["गाँव", "ग्राम पंचायत"],
                    },
                ),
                _f(
                    "district",
                    "address.district",
                    labels={"en": ["District", "Block"], "hi": ["जिला", "प्रखंड"]},
                ),
                _f("state", "address.state", labels={"en": ["State"], "hi": ["राज्य"]}),
                _address_field(required=True),
                _f(
                    "registration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={
                        "en": ["Date of Registration", "Registration Date"],
                        "hi": ["पंजीकरण तिथि"],
                    },
                    validator="generic_date",
                ),
                _f(
                    "bank_account_number",
                    "account.number",
                    kind="id",
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                ),
                _f(
                    "household_members",
                    "",
                    multi=True,
                    labels={
                        "en": ["Name of Applicant", "Members", "Adult Members"],
                        "hi": ["सदस्य"],
                    },
                    locators=("table", "label"),
                    notes="Adult members are listed in a table with age and gender columns.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_npr_letter",
            label="National Population Register (NPR) Letter",
            country="IN",
            category=Category.identity,
            issuing_authority="Registrar General & Census Commissioner, India",
            applies_to="individual",
            officially_valid=True,
            anchors=[
                Anchor(text="NATIONAL POPULATION REGISTER", decisive=True),
                Anchor(text="राष्ट्रीय जनसंख्या रजिस्टर", lang="hi", decisive=True),
                Anchor(text="REGISTRAR GENERAL AND CENSUS COMMISSIONER", decisive=True),
                Anchor(text="REGISTRAR GENERAL & CENSUS COMMISSIONER", decisive=True),
                Anchor(text="भारत के महापंजीयक", lang="hi"),
                Anchor(text="Office of the Registrar General"),
                Anchor(text="Ministry of Home Affairs"),
                Anchor(text="गृह मंत्रालय", lang="hi"),
                Anchor(text="Usual Resident"),
                Anchor(text="NPR Survey"),
                Anchor(text="Household Number"),
                Anchor(text="Resident Identity"),
                Anchor(text="censusindia.gov.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[],
            confusable_with={
                "in_aadhaar": (
                    "both are UIDAI-adjacent resident records; the NPR letter names the "
                    "Registrar General & Census Commissioner and carries no 12-digit Aadhaar "
                    "number of its own"
                ),
            },
            negative_anchors=[
                "UNIQUE IDENTIFICATION AUTHORITY OF INDIA",
                "भारतीय विशिष्ट पहचान प्राधिकरण",
            ],
            handling=(
                "An RBI Officially Valid Document only when the letter actually contains BOTH "
                "name and address — an NPR acknowledgement slip without an address does not "
                "qualify. Verify the address field parsed before treating it as an OVD."
            ),
            fields=[
                _name_field(),
                _father_field(),
                _dob_field(),
                _sex_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "household_number",
                    "doc.reference_number",
                    labels={
                        "en": ["Household Number", "Household No", "NPR Number", "Schedule No"],
                        "hi": ["परिवार संख्या"],
                    },
                    notes="The NPR letter's own reference. No published national format; "
                    "structure is not enforced.",
                ),
                _f("district", "address.district", labels={"en": ["District"], "hi": ["जिला"]}),
                _f("state", "address.state", labels={"en": ["State"], "hi": ["राज्य"]}),
                _issue_date_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_form60",
            label="Form 60 — declaration by a person without a PAN",
            country="IN",
            category=Category.tax,
            issuing_authority="Income Tax Department (self-declaration by the customer)",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM NO. 60", decisive=True),
                Anchor(text="FORM NO 60", decisive=True),
                Anchor(
                    text=(
                        "Form of declaration to be filed by a person who does not have a "
                        "permanent account number"
                    ),
                    decisive=True,
                ),
                Anchor(text="second proviso to rule 114B"),
                Anchor(text="rule 114B"),
                Anchor(text="Income-tax Rules, 1962"),
                Anchor(text="आयकर नियम", lang="hi"),
                Anchor(text="Amount of transaction"),
                Anchor(text="Reasons for not having PAN"),
                Anchor(text="applied for PAN"),
                Anchor(text="Estimated total income"),
                Anchor(text="Verification"),
                Anchor(text="आयकर विभाग", lang="hi"),
            ],
            id_patterns=[],
            confusable_with={
                "in_pan": (
                    "Form 60 is filed *instead of* a PAN — it is headed 'FORM NO. 60' and asks why "
                    "the declarant has no PAN"
                ),
                "in_aadhaar": "Form 60 quotes an Aadhaar number as one of its declared fields",
            },
            negative_anchors=["PERMANENT ACCOUNT NUMBER CARD", "स्थायी लेखा संख्या कार्ड"],
            handling=(
                "A self-declaration, not an issued document: nothing on it is independently "
                "verified. Never treat a Form 60 field as checksum- or issuer-verified, and "
                "never let it stand in for an OVD."
            ),
            fields=[
                _f(
                    "first_name",
                    "identity.given_names",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={"en": ["First Name", "Middle Name"], "hi": ["प्रथम नाम"]},
                ),
                _f(
                    "surname",
                    "identity.surname",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={"en": ["Surname", "Last Name"], "hi": ["उपनाम"]},
                ),
                _dob_field(required=True),
                _father_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "aadhaar_number",
                    "id.aadhaar",
                    kind="id",
                    pii=True,
                    labels=_L_AADHAAR,
                    pattern=r"(?<!\d)(?<!\d )[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\s?\d)",
                    validator="verhoeff_aadhaar",
                    locators=("label", "kv", "regex"),
                    notes="Form 60 asks for the Aadhaar number where one exists. The UIDAI "
                    "masking obligation applies to this field exactly as on an Aadhaar card.",
                ),
                _f(
                    "transaction_amount",
                    "income.amount",
                    kind="number",
                    labels={
                        "en": ["Amount of transaction", "Transaction Amount"],
                        "hi": ["लेन-देन की राशि"],
                    },
                    validator="amount",
                ),
                _f(
                    "estimated_total_income",
                    "income.total_income",
                    kind="number",
                    labels={"en": ["Estimated total income", "Estimated Total Income"]},
                    validator="amount",
                ),
                _f(
                    "pan_applied_acknowledgement",
                    "doc.reference_number",
                    labels={
                        "en": [
                            "Acknowledgement Number",
                            "Acknowledgement No",
                            "Date of application",
                        ]
                    },
                    notes="Present only when the declarant has applied for a PAN.",
                ),
                _f(
                    "declaration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date", "Place", "Dated"], "hi": ["दिनांक"]},
                    validator="generic_date",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_ckyc_record",
            label="CKYC Record / Central KYC Registry Application Form",
            country="IN",
            category=Category.identity,
            issuing_authority="Central Registry of Securitisation Asset Reconstruction and "
            "Security Interest of India (CERSAI)",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="CENTRAL KYC REGISTRY", decisive=True),
                Anchor(text="KYC Identification Number", decisive=True),
                Anchor(text="CKYCR"),
                Anchor(text="CKYC"),
                Anchor(text="CERSAI"),
                Anchor(text="Know Your Customer"),
                Anchor(text="KYC Application Form"),
                Anchor(text="Application Type"),
                Anchor(text="Proof of Identity"),
                Anchor(text="Proof of Address"),
                Anchor(text="Officially Valid Document"),
                Anchor(text="Related Person Details"),
                Anchor(text="Personal Details"),
                Anchor(text="Deemed OVD"),
                Anchor(text="Politically Exposed Person"),
                Anchor(text="ckycindia.in"),
            ],
            id_patterns=[r"\b\d{14}\b"],
            confusable_with={
                "in_aadhaar": (
                    "the CKYC record quotes an Aadhaar number as one field; it is headed 'CENTRAL "
                    "KYC REGISTRY' and carries a 14-digit KIN"
                ),
                "in_form60": (
                    "both are KYC paperwork; the CKYC form carries a KIN and an OVD checklist"
                ),
            },
            negative_anchors=["FORM NO. 60"],
            handling=(
                "A CKYC record is an aggregation of other documents' data, not an issued "
                "identity document. Fields read from it inherit the verification status of "
                "whatever OVD backed them — treat everything here as unverified unless the "
                "underlying OVD is also on file."
            ),
            fields=[
                _f(
                    "kin",
                    "id.ckyc_kin",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={"en": ["KYC Identification Number", "KIN", "KIN Number"]},
                    pattern=r"\b\d{14}\b",
                    locators=("label", "kv", "regex"),
                ),
                _name_field(),
                _father_field(),
                _f("mother_name", "identity.mother_name", kind="name", pii=True, labels=_L_MOTHER),
                _dob_field(),
                _sex_field(),
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "aadhaar_number",
                    "id.aadhaar",
                    kind="id",
                    pii=True,
                    labels=_L_AADHAAR,
                    pattern=r"(?<!\d)(?<!\d )[2-9]\d{3}\s?\d{4}\s?\d{4}(?!\s?\d)",
                    validator="verhoeff_aadhaar",
                    locators=("label", "kv", "regex"),
                    notes="Masking obligation applies here as on the card itself.",
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "mobile",
                    "identity.mobile",
                    pii=True,
                    pattern=r"(?:\+?91[\s-]?)?[6-9]\d{9}",
                    labels={"en": ["Mobile", "Mobile Number"], "hi": ["मोबाइल"]},
                ),
                _f(
                    "email",
                    "identity.email",
                    pii=True,
                    labels={"en": ["Email", "Email ID", "E-mail"]},
                ),
                _f(
                    "pep_status",
                    "",
                    kind="bool",
                    labels={"en": ["Politically Exposed Person", "PEP", "Related to PEP"]},
                    locators=("mark", "kv", "label"),
                    notes="Answered by a tick box on the printed form — read the selection mark, "
                    "not the surrounding text.",
                ),
                _f(
                    "proof_of_identity_type",
                    "",
                    labels={"en": ["Proof of Identity", "POI", "Identity Proof Submitted"]},
                    locators=("mark", "kv", "label"),
                ),
                _f(
                    "proof_of_address_type",
                    "",
                    labels={"en": ["Proof of Address", "POA", "Address Proof Submitted"]},
                    locators=("mark", "kv", "label"),
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_bank_passbook",
            label="Bank Passbook",
            country="IN",
            category=Category.financial,
            issuing_authority="Scheduled commercial bank / co-operative bank / post office",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="PASSBOOK", decisive=True, zone=Zone.title),
                Anchor(text="पासबुक", lang="hi", decisive=True, zone=Zone.title),
                Anchor(text="PASS BOOK", decisive=True, zone=Zone.title),
                Anchor(text="PASSBOOK"),
                Anchor(text="पासबुक", lang="hi"),
                Anchor(text="Customer ID"),
                Anchor(text="CIF No"),
                Anchor(text="खाता संख्या", lang="hi"),
                Anchor(text="Account Number"),
                Anchor(text="IFSC"),
                Anchor(text="MICR"),
                Anchor(text="Branch"),
                Anchor(text="शाखा", lang="hi"),
                Anchor(text="Nomination"),
                Anchor(text="Mode of Operation"),
                Anchor(text="Savings Bank Account"),
                Anchor(text="Date of Opening"),
                Anchor(text="Withdrawal"),
                Anchor(text="Deposit"),
            ],
            id_patterns=[r"\b[A-Z]{4}0[A-Z0-9]{6}\b"],
            confusable_with={
                "in_bank_statement": (
                    "a passbook is titled PASSBOOK and carries the account-opening block; a "
                    "statement is titled 'STATEMENT OF ACCOUNT' and covers a stated period"
                ),
                "xx_bank_statement": "the Indian passbook carries an IFSC and usually an MICR line",
            },
            negative_anchors=["STATEMENT OF ACCOUNT", "Statement Period"],
            handling=(
                "A passbook with the bank's stamp and entries is accepted as address proof by "
                "many regulated entities, but it is NOT an RBI Officially Valid Document. "
                "Account numbers are pii — mask them in logs and exports."
            ),
            fields=[
                _f(
                    "account_number",
                    "account.number",
                    kind="id",
                    required=True,
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                    locators=("label", "kv", "regex"),
                    notes="9-18 digits depending on the bank. No cross-bank checksum exists — "
                    "each bank has its own internal scheme, so length and charset are all "
                    "that can be enforced.",
                ),
                _f(
                    "ifsc",
                    "account.ifsc",
                    kind="id",
                    required=True,
                    labels=_L_IFSC,
                    pattern=r"\b[A-Z]{4}0[A-Z0-9]{6}\b",
                    locators=("regex", "label", "kv"),
                ),
                _f(
                    "micr",
                    "account.micr",
                    kind="id",
                    labels={"en": ["MICR", "MICR Code"]},
                    pattern=r"\b\d{9}\b",
                    locators=("label", "kv", "regex"),
                ),
                _f("bank_name", "account.bank_name", labels=_L_BANK),
                _f("branch", "account.branch", labels=_L_BRANCH),
                _f(
                    "customer_id",
                    "account.customer_id",
                    pii=True,
                    labels={"en": ["Customer ID", "CIF No", "CIF Number", "Cust ID"]},
                    notes="Bank-internal identifier; no common format across banks.",
                ),
                _f(
                    "account_type",
                    "account.type",
                    labels={"en": ["Account Type", "Scheme", "Type of Account", "Product"]},
                ),
                _name_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "opening_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of Opening", "Account Opening Date", "Opened On"]},
                    validator="generic_date",
                ),
                _f(
                    "balance",
                    "account.balance",
                    kind="number",
                    labels={
                        "en": ["Balance", "Closing Balance", "Available Balance"],
                        "hi": ["शेष"],
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_bank_statement",
            label="Bank Account Statement",
            country="IN",
            category=Category.financial,
            issuing_authority="Scheduled commercial bank / co-operative bank",
            applies_to="both",
            officially_valid=False,
            anchors=[
                # The two English titles were decisive at zone=title. Both are document-class
                # names that every English-speaking bank chooses independently — the registry
                # itself has ca_bank_statement and mx_estado_cuenta claiming
                # "Account Statement" — so neither was ever near-proof of an *Indian* bank
                # statement. Demoted to the plain entries below.
                #
                # The Hindi title stays decisive, and the difference is not arbitrary: the
                # English string identifies a document class, the Hindi string identifies a
                # document class *and* the jurisdiction that prints in that script, which is
                # what a decisive anchor is allowed to assert. This is the same reasoning
                # ca_pr_card uses for its French anchors. What made that fragile was never the
                # language — it was that losing the French line let another jurisdiction's
                # generic English claim win by default, and the loader check plus the
                # cascade's contested-claim rule are what close that.
                Anchor(text="खाता विवरण", lang="hi", decisive=True, zone=Zone.title),
                Anchor(text="STATEMENT OF ACCOUNT"),
                Anchor(text="ACCOUNT STATEMENT"),
                Anchor(text="खाता विवरण", lang="hi"),
                Anchor(text="Statement Period"),
                Anchor(text="Opening Balance"),
                Anchor(text="Closing Balance"),
                Anchor(text="Value Date"),
                Anchor(text="Narration"),
                Anchor(text="Particulars"),
                Anchor(text="Cheque No"),
                Anchor(text="Withdrawal"),
                Anchor(text="Deposit"),
                Anchor(text="NEFT"),
                Anchor(text="RTGS"),
                Anchor(text="IMPS"),
                Anchor(text="This is a computer generated statement"),
            ],
            id_patterns=[r"\b[A-Z]{4}0[A-Z0-9]{6}\b"],
            confusable_with={
                "in_bank_passbook": (
                    "the statement is titled 'STATEMENT OF ACCOUNT' and states a period; the "
                    "passbook is titled PASSBOOK"
                ),
                "xx_bank_statement": (
                    "the Indian statement carries an IFSC and NEFT/RTGS/UPI/IMPS narrations"
                ),
            },
            negative_anchors=["PASSBOOK", "पासबुक"],
            handling=(
                "Bank statements are transaction-level personal data. Account numbers and "
                "counterparty names are pii; never write a statement's transaction rows into "
                "an application log."
            ),
            fields=[
                _f(
                    "account_number",
                    "account.number",
                    kind="id",
                    required=True,
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "ifsc",
                    "account.ifsc",
                    kind="id",
                    labels=_L_IFSC,
                    pattern=r"\b[A-Z]{4}0[A-Z0-9]{6}\b",
                    locators=("regex", "label", "kv"),
                ),
                _f("bank_name", "account.bank_name", labels=_L_BANK),
                _f("branch", "account.branch", labels=_L_BRANCH),
                _f(
                    "account_type",
                    "account.type",
                    labels={"en": ["Account Type", "Scheme", "Product"]},
                ),
                _name_field(),
                _address_field("address.mailing", required=True),
                _pincode_field(),
                _f(
                    "statement_period",
                    "doc.reference_number",
                    labels={"en": ["Statement Period", "Period", "For the period", "From", "To"]},
                    notes="Usually printed as a from/to pair in the header block.",
                ),
                _f(
                    "opening_balance",
                    "account.balance",
                    kind="number",
                    labels={"en": ["Opening Balance", "Balance B/F", "Brought Forward"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "closing_balance",
                    "account.balance",
                    kind="number",
                    labels={"en": ["Closing Balance", "Balance C/F", "Carried Forward"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_cancelled_cheque",
            label="Cancelled Cheque",
            country="IN",
            category=Category.financial,
            issuing_authority="Scheduled commercial bank / co-operative bank",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="VALID FOR THREE MONTHS FROM DATE OF ISSUE", decisive=True),
                Anchor(text="PAYABLE AT PAR", decisive=True),
                Anchor(text="OR BEARER", decisive=True),
                Anchor(text="CTS-2010", decisive=True),
                Anchor(text="CANCELLED"),
                Anchor(text="रद्द", lang="hi"),
                Anchor(text="Please sign above"),
                Anchor(text="Rupees"),
                Anchor(text="रुपये", lang="hi"),
                Anchor(text="A/c No"),
                Anchor(text="IFSC"),
                Anchor(text="MICR"),
                Anchor(text="Account Payee"),
                Anchor(text="Authorised Signatory"),
            ],
            id_patterns=[r"\b[A-Z]{4}0[A-Z0-9]{6}\b"],
            confusable_with={
                "in_bank_passbook": (
                    "a cheque leaf carries the cheque-specific 'OR BEARER' / 'PAYABLE AT PAR' "
                    "furniture and a MICR band"
                ),
            },
            negative_anchors=["STATEMENT OF ACCOUNT", "PASSBOOK"],
            handling=(
                "Collected purely to bind an account number and IFSC to a customer. A cheque "
                "leaf that is not visibly cancelled is a live negotiable instrument — route it "
                "to human review rather than storing it as routine KYC evidence."
            ),
            fields=[
                _f(
                    "account_number",
                    "account.number",
                    kind="id",
                    required=True,
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "ifsc",
                    "account.ifsc",
                    kind="id",
                    required=True,
                    labels=_L_IFSC,
                    pattern=r"\b[A-Z]{4}0[A-Z0-9]{6}\b",
                    locators=("regex", "label", "kv"),
                ),
                _f(
                    "micr",
                    "account.micr",
                    kind="id",
                    labels={"en": ["MICR", "MICR Code"]},
                    pattern=r"\b\d{9}\b",
                    locators=("regex", "label", "kv"),
                    notes="Read from the MICR band at the foot of the leaf: 3-digit city, "
                    "3-digit bank, 3-digit branch.",
                ),
                _f(
                    "cheque_number",
                    "account.cheque_number",
                    kind="id",
                    labels={"en": ["Cheque No", "Cheque Number"]},
                    pattern=r"\b\d{6}\b",
                    locators=("regex", "label"),
                    notes="Six digits under CTS-2010, printed leftmost in the MICR band.",
                ),
                _f(
                    "account_holder_name",
                    "identity.full_name",
                    kind="name",
                    pii=True,
                    labels=_L_NAME,
                    notes="Pre-printed above the signature line on most personal cheque books; "
                    "absent on unpersonalised leaves.",
                ),
                _f("bank_name", "account.bank_name", labels=_L_BANK),
                _f("branch", "account.branch", labels=_L_BRANCH),
                _f(
                    "is_cancelled",
                    "",
                    kind="bool",
                    labels={"en": ["CANCELLED"], "hi": ["रद्द"]},
                    locators=("regex", "label"),
                    notes="Presence of a CANCELLED overprint. Absence is a review signal, not "
                    "an extraction failure.",
                ),
            ],
        ),
    ]
)


def _utility_fields(extra: list[FieldSpec] | None = None) -> list[FieldSpec]:
    """The field set every Indian utility bill shares.

    Args:
        extra: Utility-specific fields appended after the common ones.

    Returns:
        Common consumer/address/amount fields plus ``extra``.
    """
    return [
        _f(
            "consumer_number",
            "utility.consumer_number",
            kind="id",
            required=True,
            pii=True,
            labels=_L_CONSUMER,
            locators=("label", "kv", "regex"),
            notes="Every operator uses its own scheme (numeric, alphanumeric, "
            "hyphenated, 8-14 characters). No pattern is declared: a regex here "
            "would reject genuine bills from whichever DISCOM was not sampled.",
        ),
        _f(
            "consumer_name",
            "identity.full_name",
            kind="name",
            required=True,
            pii=True,
            labels={
                "en": ["Consumer Name", "Customer Name", "Name", "Billed To", "Name of Consumer"],
                "hi": ["उपभोक्ता का नाम", "नाम"],
            },
        ),
        _address_field(required=True),
        _pincode_field(),
        _f(
            "service_provider",
            "utility.service_provider",
            labels={
                "en": ["Distribution Company", "Licensee", "Service Provider", "Board"],
                "hi": ["वितरण कंपनी"],
            },
            notes="Usually only present as a masthead/logo line rather than a labelled "
            "field — read it from the title zone when the label lookup misses.",
        ),
        _f(
            "bill_amount",
            "utility.bill_amount",
            kind="number",
            labels=_L_BILL_AMOUNT,
            validator="amount",
            locators=("table", "label", "kv"),
        ),
        _f("bill_period", "utility.bill_period", labels=_L_BILL_PERIOD),
        _f(
            "bill_date",
            "doc.issue_date",
            kind="date",
            labels={"en": ["Bill Date", "Date of Bill", "Invoice Date"], "hi": ["बिल तिथि"]},
            validator="generic_date",
        ),
        _f("due_date", "doc.due_date", kind="date", labels=_L_DUE_DATE, validator="generic_date"),
        *(extra or []),
    ]


#: Every utility bill is confusable with every other and with the generic cross-country
#: bill. Declared once and reused so the four specs cannot drift apart.
_UTILITY_CONFUSABLE = {
    "xx_utility_bill": (
        "the Indian bill names a specific Indian operator and prints amounts in ₹/Rs"
    ),
}

_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_utility_electricity",
            label="Electricity Bill",
            country="IN",
            category=Category.address_proof,
            issuing_authority="State electricity distribution company (DISCOM) / electricity board",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="ELECTRICITY BILL", decisive=True),
                Anchor(text="विद्युत बिल", lang="hi", decisive=True),
                Anchor(text="बिजली बिल", lang="hi", decisive=True),
                Anchor(text="ENERGY BILL", decisive=True),
                Anchor(text="Units Consumed"),
                Anchor(text="यूनिट खपत", lang="hi"),
                Anchor(text="Meter Reading"),
                Anchor(text="मीटर रीडिंग", lang="hi"),
                Anchor(text="Sanctioned Load"),
                Anchor(text="Connected Load"),
                Anchor(text="Energy Charges"),
                Anchor(text="Fixed Charges"),
                Anchor(text="Fuel Surcharge"),
                Anchor(text="Electricity Duty"),
                Anchor(text="kWh"),
                Anchor(text="Tariff Category"),
                Anchor(text="Previous Reading"),
                Anchor(text="Present Reading"),
                Anchor(text="Electricity Board"),
                Anchor(text="Power Distribution"),
                Anchor(text="MSEDCL"),
                Anchor(text="BSES"),
                Anchor(text="Tata Power"),
                Anchor(text="BESCOM"),
                Anchor(text="TNEB"),
                Anchor(text="UPPCL"),
                Anchor(text="PSPCL"),
                Anchor(text="WBSEDCL"),
                Anchor(text="KSEB"),
                Anchor(text="TSSPDCL"),
                Anchor(text="CESC"),
            ],
            id_patterns=[],
            confusable_with={
                **_UTILITY_CONFUSABLE,
                "in_utility_water": (
                    "the electricity bill meters kWh and prints energy/fixed charges"
                ),
                "in_utility_gas": "the electricity bill meters kWh, the gas bill meters SCM",
            },
            negative_anchors=["Sewerage Charges", "Piped Natural Gas", "Call Charges", "Broadband"],
            handling=(
                "NOT an Officially Valid Document. Under the RBI KYC Master Direction a "
                "utility bill is a 'deemed OVD' for a limited address-update purpose only, and "
                "only if it is not more than two months old — check bill_date against the "
                "submission date before relying on it."
            ),
            fields=_utility_fields(
                [
                    _f(
                        "units_consumed",
                        "utility.units_consumed",
                        kind="number",
                        labels={
                            "en": ["Units Consumed", "Consumption", "Units", "kWh Consumed"],
                            "hi": ["खपत", "यूनिट"],
                        },
                        locators=("table", "label", "kv"),
                    ),
                    _f(
                        "sanctioned_load",
                        "",
                        labels={"en": ["Sanctioned Load", "Connected Load", "Contract Demand"]},
                    ),
                    _f(
                        "meter_number",
                        "",
                        labels={
                            "en": ["Meter No", "Meter Number", "Meter Sl No"],
                            "hi": ["मीटर संख्या"],
                        },
                    ),
                    _f(
                        "tariff_category",
                        "",
                        labels={"en": ["Tariff", "Tariff Category", "Category", "Connection Type"]},
                    ),
                ]
            ),
        ),
        DocTypeSpec(
            doctype_id="in_utility_water",
            label="Water Bill",
            country="IN",
            category=Category.address_proof,
            issuing_authority="Municipal water board / water supply and sewerage board",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="WATER BILL", decisive=True),
                Anchor(text="जल बिल", lang="hi", decisive=True),
                Anchor(text="जल बोर्ड", lang="hi", decisive=True),
                Anchor(text="WATER SUPPLY AND SEWERAGE BOARD", decisive=True),
                Anchor(text="Water Charges"),
                Anchor(text="जल शुल्क", lang="hi"),
                Anchor(text="Sewerage Charges"),
                Anchor(text="Water Supply"),
                Anchor(text="Kilolitre"),
                Anchor(text="KL"),
                Anchor(text="Meter Reading"),
                Anchor(text="Delhi Jal Board"),
                Anchor(text="BWSSB"),
                Anchor(text="Chennai Metro Water"),
                Anchor(text="Municipal Corporation"),
                Anchor(text="नगर निगम", lang="hi"),
                Anchor(text="Water Tax"),
            ],
            id_patterns=[],
            confusable_with={
                **_UTILITY_CONFUSABLE,
                "in_utility_electricity": (
                    "the water bill meters kilolitres and prints sewerage charges"
                ),
                "in_property_tax_receipt": (
                    "both come from a municipal body; the water bill meters consumption, the tax "
                    "receipt assesses a property"
                ),
            },
            negative_anchors=["Energy Charges", "kWh", "Piped Natural Gas", "Broadband"],
            handling=(
                "NOT an Officially Valid Document. Deemed-OVD status for address update "
                "requires the bill to be not more than two months old."
            ),
            fields=_utility_fields(
                [
                    _f(
                        "units_consumed",
                        "utility.units_consumed",
                        kind="number",
                        labels={
                            "en": ["Consumption", "Units Consumed", "KL Consumed", "Kilolitres"],
                            "hi": ["खपत"],
                        },
                        locators=("table", "label", "kv"),
                    ),
                    _f(
                        "sewerage_charges",
                        "",
                        kind="number",
                        labels={"en": ["Sewerage Charges", "Sewerage Cess"]},
                        validator="amount",
                    ),
                    _f(
                        "meter_number",
                        "",
                        labels={"en": ["Meter No", "Meter Number"], "hi": ["मीटर संख्या"]},
                    ),
                ]
            ),
        ),
        DocTypeSpec(
            doctype_id="in_utility_gas",
            label="Gas Bill (piped natural gas) / LPG connection document",
            country="IN",
            category=Category.address_proof,
            issuing_authority="City gas distribution company / LPG oil marketing company",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="PIPED NATURAL GAS", decisive=True),
                Anchor(text="GAS BILL", decisive=True),
                Anchor(text="पाइप्ड प्राकृतिक गैस", lang="hi", decisive=True),
                Anchor(text="PNG"),
                Anchor(text="SCM"),
                Anchor(text="Standard Cubic Metre"),
                Anchor(text="Gas Consumption"),
                Anchor(text="Indraprastha Gas"),
                Anchor(text="Mahanagar Gas"),
                Anchor(text="Gujarat Gas"),
                Anchor(text="Adani Total Gas"),
                Anchor(text="LPG"),
                Anchor(text="रसोई गैस", lang="hi"),
                Anchor(text="Indane"),
                Anchor(text="HP Gas"),
                Anchor(text="Bharatgas"),
                Anchor(text="Subscription Voucher"),
                Anchor(text="Refill"),
                Anchor(text="Distributor"),
                Anchor(text="Meter Reading"),
            ],
            id_patterns=[],
            confusable_with={
                **_UTILITY_CONFUSABLE,
                "in_utility_electricity": (
                    "the gas bill meters SCM, the electricity bill meters kWh"
                ),
            },
            negative_anchors=["Energy Charges", "kWh", "Sewerage Charges", "Call Charges"],
            handling=(
                "NOT an Officially Valid Document; deemed-OVD only, and only within two months "
                "of issue. Note that an LPG subscription voucher is a different artefact from a "
                "piped-gas bill and carries no consumption data — both land here, so check "
                "which fields actually populated before treating it as a periodic bill."
            ),
            fields=_utility_fields(
                [
                    _f(
                        "units_consumed",
                        "utility.units_consumed",
                        kind="number",
                        labels={
                            "en": ["Consumption", "SCM Consumed", "Units Consumed", "Gas Consumed"]
                        },
                        locators=("table", "label", "kv"),
                        notes="Piped gas only; an LPG voucher has no consumption figure.",
                    ),
                    _f(
                        "connection_type",
                        "",
                        labels={"en": ["Connection Type", "Category", "Domestic", "Commercial"]},
                    ),
                    _f(
                        "distributor",
                        "",
                        labels={"en": ["Distributor", "Distributor Name", "Agency", "Dealer"]},
                        notes="LPG documents name a dealer rather than a metered licensee.",
                    ),
                ]
            ),
        ),
        DocTypeSpec(
            doctype_id="in_utility_telephone",
            label="Telephone / Landline / Broadband Bill",
            country="IN",
            category=Category.address_proof,
            issuing_authority="Telecom service provider",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="TELEPHONE BILL", decisive=True),
                Anchor(text="LANDLINE BILL", decisive=True),
                Anchor(text="दूरभाष बिल", lang="hi", decisive=True),
                Anchor(text="BROADBAND BILL", decisive=True),
                Anchor(text="Call Charges"),
                Anchor(text="Monthly Rental"),
                Anchor(text="Tariff Plan"),
                Anchor(text="Plan Name"),
                Anchor(text="Broadband"),
                Anchor(text="Landline"),
                Anchor(text="Postpaid"),
                Anchor(text="Usage Charges"),
                Anchor(text="Data Usage"),
                Anchor(text="BSNL"),
                Anchor(text="MTNL"),
                Anchor(text="Airtel"),
                Anchor(text="Vodafone Idea"),
                Anchor(text="Jio"),
                Anchor(text="Relationship Number"),
                Anchor(text="Bill Number"),
            ],
            id_patterns=[],
            confusable_with={
                **_UTILITY_CONFUSABLE,
                "in_utility_electricity": (
                    "the telephone bill prints call/rental charges and no metered consumption"
                ),
            },
            negative_anchors=["Energy Charges", "kWh", "Sewerage Charges", "Piped Natural Gas"],
            handling=(
                "NOT an Officially Valid Document. A telephone or post-paid mobile bill is a "
                "deemed OVD for address update only, and only when not more than two months "
                "old. A pre-paid mobile document is not accepted at all."
            ),
            fields=_utility_fields(
                [
                    _f(
                        "telephone_number",
                        "identity.mobile",
                        pii=True,
                        labels={
                            "en": [
                                "Telephone No",
                                "Phone Number",
                                "Mobile Number",
                                "Landline Number",
                                "Service Number",
                            ],
                            "hi": ["दूरभाष संख्या"],
                        },
                        notes="May be a 10-digit mobile or an STD-code landline; the "
                        "indian_mobile validator is deliberately NOT applied because it "
                        "would reject every landline.",
                    ),
                    _f("plan_name", "", labels={"en": ["Tariff Plan", "Plan Name", "Plan"]}),
                    _f(
                        "monthly_rental",
                        "",
                        kind="number",
                        labels={"en": ["Monthly Rental", "Rental", "Fixed Charges"]},
                        validator="amount",
                    ),
                ]
            ),
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_rent_agreement",
            label="Rent / Lease / Leave-and-Licence Agreement",
            country="IN",
            category=Category.address_proof,
            issuing_authority="Executed between private parties; registered with the "
            "Sub-Registrar where the term requires registration",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="RENT AGREEMENT", decisive=True),
                Anchor(text="LEAVE AND LICENCE AGREEMENT", decisive=True),
                Anchor(text="LEAVE AND LICENSE AGREEMENT", decisive=True),
                Anchor(text="LEASE DEED", decisive=True),
                Anchor(text="DEED OF LEASE", decisive=True),
                Anchor(text="किरायानामा", lang="hi", decisive=True),
                Anchor(text="किराया अनुबंध", lang="hi", decisive=True),
                Anchor(text="LESSOR"),
                Anchor(text="LESSEE"),
                Anchor(text="LICENSOR"),
                Anchor(text="LICENSEE"),
                Anchor(text="Monthly Rent"),
                Anchor(text="मासिक किराया", lang="hi"),
                Anchor(text="Security Deposit"),
                Anchor(text="Schedule of Property"),
                Anchor(text="Tenure"),
                Anchor(text="INDIAN NON JUDICIAL"),
                Anchor(text="e-Stamp Certificate"),
                Anchor(text="Stock Holding Corporation of India"),
                Anchor(text="Stamp Duty"),
                Anchor(text="Sub-Registrar"),
                Anchor(text="WITNESSETH"),
                Anchor(text="PARTY OF THE FIRST PART"),
            ],
            id_patterns=[],
            confusable_with={
                "in_property_tax_receipt": (
                    "the agreement is a contract between parties; the receipt is a municipal "
                    "demand/payment record"
                ),
                "in_employer_allotment_letter": (
                    "an allotment letter is issued unilaterally by an employer, not executed "
                    "between a lessor and a lessee"
                ),
            },
            negative_anchors=["PROPERTY TAX", "संपत्ति कर", "LETTER OF ALLOTMENT OF ACCOMMODATION"],
            handling=(
                "NOT an Officially Valid Document. Only a REGISTERED lease or leave-and-licence "
                "agreement is a deemed OVD for address; an unregistered agreement on stamp "
                "paper is not. Look for the Sub-Registrar registration block before treating "
                "it as address evidence."
            ),
            fields=[
                _f(
                    "landlord_name",
                    "tenancy.landlord_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": [
                            "Lessor",
                            "Landlord",
                            "Licensor",
                            "Owner",
                            "Party of the First Part",
                        ],
                        "hi": ["मकान मालिक"],
                    },
                ),
                _f(
                    "tenant_name",
                    "tenancy.tenant_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Lessee", "Tenant", "Licensee", "Party of the Second Part"],
                        "hi": ["किरायेदार"],
                    },
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "monthly_rent",
                    "tenancy.monthly_rent",
                    kind="number",
                    labels={
                        "en": ["Monthly Rent", "Rent", "Licence Fee", "Monthly Licence Fee"],
                        "hi": ["मासिक किराया"],
                    },
                    validator="amount",
                ),
                _f(
                    "security_deposit",
                    "",
                    kind="number",
                    labels={"en": ["Security Deposit", "Deposit", "Interest Free Deposit"]},
                    validator="amount",
                ),
                _f(
                    "term",
                    "tenancy.term",
                    labels={
                        "en": ["Term", "Tenure", "Period of Lease", "Duration"],
                        "hi": ["अवधि"],
                    },
                    notes="Commonly '11 months' in India — the term is chosen to stay under the "
                    "compulsory-registration threshold, which is exactly why an unregistered "
                    "agreement is so common and why registration must be checked.",
                ),
                _f(
                    "commencement_date",
                    "doc.issue_date",
                    kind="date",
                    labels={
                        "en": [
                            "Date of Commencement",
                            "With effect from",
                            "Commencing from",
                            "Agreement Date",
                        ]
                    },
                    validator="generic_date",
                ),
                _f(
                    "registration_number",
                    "doc.registration_number",
                    labels={
                        "en": [
                            "Registration No",
                            "Document No",
                            "Serial No",
                            "e-Stamp Certificate No",
                        ],
                        "hi": ["पंजीकरण संख्या"],
                    },
                    notes="Registration numbering is state-specific; no pattern is enforced.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_property_tax_receipt",
            label="Property Tax Receipt / Municipal Tax Demand",
            country="IN",
            category=Category.address_proof,
            issuing_authority="Municipal corporation / municipality / gram panchayat",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="PROPERTY TAX", decisive=True),
                Anchor(text="संपत्ति कर", lang="hi", decisive=True),
                Anchor(text="HOUSE TAX", decisive=True),
                Anchor(text="गृह कर", lang="hi", decisive=True),
                Anchor(text="Municipal Corporation"),
                Anchor(text="नगर निगम", lang="hi"),
                Anchor(text="नगर पालिका", lang="hi"),
                Anchor(text="Property ID"),
                Anchor(text="Khata Number"),
                Anchor(text="Assessment Number"),
                Anchor(text="Ward No"),
                Anchor(text="वार्ड", lang="hi"),
                Anchor(text="Zone"),
                Anchor(text="Annual Rateable Value"),
                Anchor(text="Tax Paid"),
                Anchor(text="Receipt No"),
                Anchor(text="रसीद", lang="hi"),
                Anchor(text="Assessment Year"),
                Anchor(text="Built Up Area"),
            ],
            id_patterns=[],
            confusable_with={
                "in_rent_agreement": (
                    "the receipt is a municipal payment record naming an assessed property, not a "
                    "contract"
                ),
                "in_utility_water": (
                    "both are municipal; the tax receipt assesses a property and meters nothing"
                ),
            },
            negative_anchors=["LESSOR", "LESSEE", "Water Charges", "Energy Charges"],
            handling=(
                "NOT an Officially Valid Document. Evidences ownership/occupation of an "
                "address; the named assessee is the property owner, who is not necessarily "
                "the customer standing in front of you."
            ),
            fields=[
                _f(
                    "property_id",
                    "id.property_id",
                    kind="id",
                    required=True,
                    labels={
                        "en": [
                            "Property ID",
                            "Property No",
                            "Khata Number",
                            "Assessment Number",
                            "UPIC",
                            "PID",
                        ],
                        "hi": ["संपत्ति संख्या"],
                    },
                    notes="Every municipal body numbers properties differently (Khata, PID, "
                    "UPIC, assessment number). No pattern is enforced.",
                ),
                _f(
                    "owner_name",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Owner Name", "Assessee", "Name of Owner", "Name of Assessee"],
                        "hi": ["स्वामी का नाम"],
                    },
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "assessment_year",
                    "doc.assessment_year",
                    pattern=r"\b(?:19|20)\d{2}\s*[-/]\s*\d{2}\b",
                    labels={
                        "en": ["Assessment Year", "Financial Year", "Year"],
                        "hi": ["निर्धारण वर्ष"],
                    },
                ),
                _f(
                    "tax_amount",
                    "",
                    kind="number",
                    labels={
                        "en": ["Tax Paid", "Amount Paid", "Total Tax", "Net Payable"],
                        "hi": ["कर राशि"],
                    },
                    validator="amount",
                ),
                _f(
                    "receipt_number",
                    "doc.reference_number",
                    labels={
                        "en": ["Receipt No", "Receipt Number", "Transaction ID"],
                        "hi": ["रसीद संख्या"],
                    },
                ),
                _f(
                    "payment_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Payment Date", "Date of Payment", "Receipt Date", "Date"]},
                    validator="generic_date",
                ),
                _f(
                    "municipal_body",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Municipal Corporation", "Municipality", "Urban Local Body"],
                        "hi": ["नगर निगम"],
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_employer_allotment_letter",
            label="Employer Letter of Allotment of Accommodation",
            country="IN",
            category=Category.address_proof,
            issuing_authority="Central/State government department, statutory or regulatory "
            "body, PSU, scheduled commercial bank, financial institution or listed company",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="LETTER OF ALLOTMENT OF ACCOMMODATION", decisive=True),
                Anchor(text="ALLOTMENT OF ACCOMMODATION", decisive=True),
                Anchor(text="ALLOTMENT LETTER", decisive=True),
                Anchor(text="आवास आवंटन पत्र", lang="hi", decisive=True),
                Anchor(text="Allottee"),
                Anchor(text="Estate Office"),
                Anchor(text="Quarter No"),
                Anchor(text="Type of Accommodation"),
                Anchor(text="Company Leased Accommodation"),
                Anchor(text="Staff Quarters"),
                Anchor(text="Licence Fee"),
                Anchor(text="House Rent Allowance"),
                Anchor(text="Employee Code"),
                Anchor(text="Designation"),
                Anchor(text="Date of Occupation"),
            ],
            id_patterns=[],
            confusable_with={
                "in_rent_agreement": (
                    "an allotment letter is issued unilaterally by the employer; a rent agreement "
                    "is executed between a lessor and a lessee"
                ),
            },
            negative_anchors=["LESSOR", "LESSEE", "LEAVE AND LICENCE AGREEMENT"],
            handling=(
                "NOT an Officially Valid Document. It is a deemed OVD for address only when "
                "the issuer falls within the RBI's named categories — government departments, "
                "statutory/regulatory bodies, PSUs, scheduled commercial banks, financial "
                "institutions and listed companies. A letter from an unlisted private employer "
                "does not qualify: verify the issuer, do not just verify the letter."
            ),
            fields=[
                _name_field(),
                _f(
                    "employee_code",
                    "",
                    pii=True,
                    labels={"en": ["Employee Code", "Employee No", "Staff No", "EMP ID"]},
                ),
                _f(
                    "designation",
                    "",
                    labels={"en": ["Designation", "Post", "Grade"], "hi": ["पदनाम"]},
                ),
                _f(
                    "employer",
                    "income.employer",
                    required=True,
                    labels={
                        "en": ["Employer", "Organisation", "Department", "Company"],
                        "hi": ["नियोक्ता"],
                    },
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "quarter_number",
                    "",
                    labels={"en": ["Quarter No", "Flat No", "Accommodation No", "Type"]},
                ),
                _f(
                    "allotment_date",
                    "doc.issue_date",
                    kind="date",
                    labels={
                        "en": ["Date of Allotment", "Allotment Date", "Date of Occupation", "Date"]
                    },
                    validator="generic_date",
                ),
                _f(
                    "licence_fee",
                    "",
                    kind="number",
                    labels={"en": ["Licence Fee", "Rent", "Monthly Recovery"]},
                    validator="amount",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_pension_payment_order",
            label="Pension Payment Order (PPO)",
            country="IN",
            category=Category.financial,
            issuing_authority="Central Pension Accounting Office / Accountant General / "
            "EPFO / PSU pension authority",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="PENSION PAYMENT ORDER", decisive=True),
                Anchor(text="पेंशन भुगतान आदेश", lang="hi", decisive=True),
                Anchor(text="CENTRAL PENSION ACCOUNTING OFFICE", decisive=True),
                Anchor(text="PPO No"),
                Anchor(text="Basic Pension"),
                Anchor(text="मूल पेंशन", lang="hi"),
                Anchor(text="Family Pension"),
                Anchor(text="Commuted Value"),
                Anchor(text="Commutation"),
                Anchor(text="Pension Disbursing Authority"),
                Anchor(text="Date of Retirement"),
                Anchor(text="सेवानिवृत्ति", lang="hi"),
                Anchor(text="Dearness Relief"),
                Anchor(text="Accountant General"),
                Anchor(text="Qualifying Service"),
                Anchor(text="Pensioner"),
                Anchor(text="EPFO"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[],
            confusable_with={
                "in_salary_slip": (
                    "a PPO sanctions a pension once; a salary slip reports one month's payroll"
                ),
            },
            negative_anchors=["Gross Earnings", "Provident Fund Contribution", "SALARY SLIP"],
            handling=(
                "NOT an Officially Valid Document. A PPO issued to a retired government or PSU "
                "employee is a deemed OVD for address ONLY if it actually contains an address — "
                "many do not. Verify the address field populated before relying on it."
            ),
            fields=[
                _f(
                    "ppo_number",
                    "id.ppo_number",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["PPO No", "PPO Number", "Pension Payment Order No"],
                        "hi": ["पीपीओ संख्या"],
                    },
                    locators=("label", "kv", "regex"),
                    notes="CPAO PPO numbers are commonly 12 digits, but Accountant General, "
                    "EPFO and PSU authorities each issue their own layouts, some alphanumeric. "
                    "No pattern is enforced — a 12-digit regex would reject state pensioners.",
                ),
                _name_field(),
                _dob_field(),
                _address_field(),
                _pincode_field(),
                _f(
                    "basic_pension",
                    "income.pension_amount",
                    kind="number",
                    labels={
                        "en": ["Basic Pension", "Pension Amount", "Monthly Pension"],
                        "hi": ["मूल पेंशन"],
                    },
                    validator="amount",
                ),
                _f(
                    "family_pension",
                    "",
                    kind="number",
                    labels={"en": ["Family Pension", "Enhanced Family Pension"]},
                    validator="amount",
                ),
                _f(
                    "retirement_date",
                    "",
                    kind="date",
                    labels={
                        "en": ["Date of Retirement", "Date of Superannuation", "Retired On"],
                        "hi": ["सेवानिवृत्ति तिथि"],
                    },
                    validator="generic_date",
                ),
                _f(
                    "bank_account_number",
                    "account.number",
                    kind="id",
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                ),
                _f(
                    "disbursing_authority",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Pension Disbursing Authority", "Paying Branch", "Disbursing Bank"]
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_ration_card",
            label="Ration Card (Public Distribution System)",
            country="IN",
            category=Category.identity,
            issuing_authority="State Department of Food, Civil Supplies and Consumer Affairs",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="RATION CARD", decisive=True),
                Anchor(text="राशन कार्ड", lang="hi", decisive=True),
                Anchor(text="Food and Civil Supplies"),
                Anchor(text="खाद्य एवं नागरिक आपूर्ति", lang="hi"),
                Anchor(text="Public Distribution System"),
                Anchor(text="सार्वजनिक वितरण प्रणाली", lang="hi"),
                Anchor(text="Fair Price Shop"),
                Anchor(text="उचित मूल्य की दुकान", lang="hi"),
                Anchor(text="National Food Security Act"),
                Anchor(text="राष्ट्रीय खाद्य सुरक्षा अधिनियम", lang="hi"),
                Anchor(text="Antyodaya Anna Yojana"),
                Anchor(text="AAY"),
                Anchor(text="Priority Household"),
                Anchor(text="PHH"),
                Anchor(text="Head of Family"),
                Anchor(text="परिवार के मुखिया", lang="hi"),
                Anchor(text="Unit"),
                Anchor(text="FPS Code"),
            ],
            id_patterns=[],
            confusable_with={
                "in_nrega_job_card": (
                    "both list household members; the ration card names the food & civil "
                    "supplies department and a fair price shop, the job card names the MGNREGA Act"
                ),
            },
            negative_anchors=[
                "MAHATMA GANDHI NATIONAL RURAL EMPLOYMENT GUARANTEE ACT",
                "JOB CARD",
                "जॉब कार्ड",
            ],
            handling=(
                "NOT an Officially Valid Document. The ration card was withdrawn from the RBI "
                "OVD list and must never be accepted in place of one, however commonly it is "
                "offered as address proof."
            ),
            fields=[
                _f(
                    "ration_card_number",
                    "id.ration_card",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Ration Card No", "Ration Card Number", "RC No", "Card No"],
                        "hi": ["राशन कार्ड संख्या"],
                    },
                    locators=("label", "kv", "regex"),
                    notes="Each state issues its own numbering (length 9-14, sometimes with a "
                    "state alpha prefix). No pattern is enforced.",
                ),
                _f(
                    "head_of_family",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Head of Family", "Name of Head", "Card Holder"],
                        "hi": ["परिवार के मुखिया का नाम"],
                    },
                ),
                _father_field(),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "card_category",
                    "",
                    labels={"en": ["Category", "Card Type", "Scheme"], "hi": ["श्रेणी"]},
                    pattern=r"(?i)^(AAY|PHH|APL|BPL|NFSA|PRIORITY|ANTYODAYA)$",
                    notes="AAY (Antyodaya), PHH (priority household), and the legacy APL/BPL "
                    "labels are all still printed depending on state and vintage.",
                ),
                _f(
                    "member_count",
                    "",
                    kind="number",
                    labels={"en": ["No of Members", "Units", "Total Members"], "hi": ["सदस्य संख्या"]},
                ),
                _f(
                    "household_members",
                    "",
                    multi=True,
                    labels={"en": ["Name of Member", "Members", "Family Members"], "hi": ["सदस्य"]},
                    locators=("table", "label"),
                    notes="Listed in a table with age, gender and relation columns.",
                ),
                _f(
                    "fps_code",
                    "",
                    labels={
                        "en": ["FPS Code", "Fair Price Shop", "Shop No", "Dealer"],
                        "hi": ["उचित मूल्य की दुकान"],
                    },
                ),
                _issue_date_field(),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_gst_certificate",
            label="GST Registration Certificate (Form GST REG-06)",
            country="IN",
            category=Category.tax,
            issuing_authority="Goods and Services Tax Network / Central Board of Indirect "
            "Taxes and Customs / State tax authority",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM GST REG-06", decisive=True),
                Anchor(text="GOODS AND SERVICES TAX IDENTIFICATION NUMBER", decisive=True),
                Anchor(text="वस्तु एवं सेवा कर पहचान संख्या", lang="hi", decisive=True),
                Anchor(text="Registration Certificate"),
                Anchor(text="पंजीकरण प्रमाणपत्र", lang="hi"),
                Anchor(text="GSTIN"),
                Anchor(text="Goods and Services Tax"),
                Anchor(text="वस्तु एवं सेवा कर", lang="hi"),
                Anchor(text="Legal Name"),
                Anchor(text="Trade Name"),
                Anchor(text="Constitution of Business"),
                Anchor(text="Principal Place of Business"),
                Anchor(text="Additional Places of Business"),
                Anchor(text="Date of Liability"),
                Anchor(text="Date of Validity"),
                Anchor(text="Type of Registration"),
                Anchor(text="Particulars of Approving Authority"),
                Anchor(text="Centre Jurisdiction"),
                Anchor(text="State Jurisdiction"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"],
            confusable_with={
                "in_certificate_incorporation": (
                    "the GST certificate carries a 15-character GSTIN and names a jurisdiction; the"
                    " incorporation certificate carries a 21-character CIN"
                ),
            },
            negative_anchors=["CERTIFICATE OF INCORPORATION", "MINISTRY OF CORPORATE AFFAIRS"],
            handling=(
                "Evidences tax registration and a principal place of business. It is not an "
                "OVD for the individuals behind the entity — a GST certificate never "
                "substitutes for KYC of the promoters or signatories."
            ),
            fields=[
                _f(
                    "gstin",
                    "id.gstin",
                    kind="id",
                    required=True,
                    labels={
                        "en": [
                            "GSTIN",
                            "Registration Number",
                            "GSTIN/UIN",
                            "Goods and Services Tax Identification Number",
                        ]
                    },
                    pattern=r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b",
                    validator="gstin",
                    locators=("regex", "label", "kv"),
                    notes="15 characters: 2-digit state code, the entity's 10-character PAN, a "
                    "1-character entity code, a literal 'Z', and a mod-36 check character. "
                    "The check character algorithm IS published — enforce it.",
                ),
                _f("legal_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "trade_name",
                    "entity.trade_name",
                    labels={"en": ["Trade Name", "Trade Name, if any"]},
                ),
                _f(
                    "constitution",
                    "entity.constitution",
                    labels={"en": ["Constitution of Business", "Constitution", "Type of Business"]},
                ),
                _f(
                    "principal_place_of_business",
                    "address.registered",
                    kind="address",
                    required=True,
                    labels={
                        "en": [
                            "Address of Principal Place of Business",
                            "Principal Place of Business",
                        ]
                    },
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("regex", "label", "kv"),
                    notes="Characters 3-12 of the GSTIN are the entity PAN; prefer the derived "
                    "value when the printed PAN and the GSTIN disagree.",
                ),
                _f(
                    "liability_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of Liability", "Date of Registration", "Valid From"]},
                    validator="generic_date",
                ),
                _f(
                    "validity_date",
                    "doc.expiry_date",
                    kind="date",
                    labels={"en": ["Date of Validity", "Period of Validity", "Valid Upto"]},
                    validator="generic_date",
                    notes="Blank on a regular registration; populated only for casual and "
                    "non-resident taxable persons.",
                ),
                _f(
                    "registration_type",
                    "",
                    labels={"en": ["Type of Registration", "Taxpayer Type"]},
                ),
                _f(
                    "jurisdiction",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Centre Jurisdiction", "State Jurisdiction", "Jurisdictional Office"]
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_itr_acknowledgement",
            label="Income Tax Return Acknowledgement (ITR-V)",
            country="IN",
            category=Category.tax,
            issuing_authority="Income Tax Department, Centralised Processing Centre",
            applies_to="both",
            officially_valid=False,
            anchors=[
                Anchor(text="INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT", decisive=True),
                Anchor(text="INDIAN INCOME TAX RETURN VERIFICATION FORM", decisive=True),
                Anchor(text="e-Filing Acknowledgement Number", decisive=True),
                Anchor(text="ITR-V"),
                Anchor(text="Acknowledgement Number"),
                Anchor(text="Assessment Year"),
                Anchor(text="निर्धारण वर्ष", lang="hi"),
                Anchor(text="Gross Total Income"),
                Anchor(text="Total Income"),
                Anchor(text="Filed u/s"),
                Anchor(text="Date of Filing"),
                Anchor(text="Current Year loss"),
                Anchor(text="Net Tax Payable"),
                Anchor(text="Total Tax Paid"),
                Anchor(text="Refund"),
                Anchor(text="Centralised Processing Centre"),
                Anchor(text="incometax.gov.in"),
                Anchor(text="आयकर विभाग", lang="hi"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[A-Z]{5}\d{4}[A-Z]\b", r"\b\d{15}\b"],
            confusable_with={
                "in_form16": (
                    "Form 16 is issued by an employer against a TAN; ITR-V is the taxpayer's own "
                    "filed return"
                ),
                "in_pan": (
                    "ITR-V quotes a PAN but is headed 'INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT'"
                ),
            },
            negative_anchors=[
                "FORM NO. 16",
                "TAN of the Deductor",
                "PERMANENT ACCOUNT NUMBER CARD",
            ],
            handling=(
                "Widely used as income evidence. Note it is an acknowledgement of what the "
                "taxpayer FILED, not an assessment of what the department accepted — do not "
                "describe an ITR-V figure as verified income."
            ),
            fields=[
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    required=True,
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("regex", "label", "kv"),
                ),
                _name_field(),
                _address_field(required=False),
                _pincode_field(),
                _f(
                    "assessment_year",
                    "doc.assessment_year",
                    required=True,
                    pattern=r"\b(?:19|20)\d{2}\s*[-/]\s*\d{2}\b",
                    labels={"en": ["Assessment Year", "A.Y.", "AY"], "hi": ["निर्धारण वर्ष"]},
                ),
                _f(
                    "acknowledgement_number",
                    "doc.reference_number",
                    kind="id",
                    required=True,
                    labels={
                        "en": [
                            "Acknowledgement Number",
                            "Acknowledgement No",
                            "e-Filing Acknowledgement Number",
                        ]
                    },
                    pattern=r"\b\d{15}\b",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "filing_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of Filing", "Date of filing", "Filed on", "Date"]},
                    validator="generic_date",
                ),
                _f(
                    "gross_total_income",
                    "income.total_income",
                    kind="number",
                    labels={"en": ["Gross Total Income", "Gross Total income"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "total_income",
                    "income.total_income",
                    kind="number",
                    labels={"en": ["Total Income", "Taxable Total Income"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "tax_payable",
                    "",
                    kind="number",
                    labels={"en": ["Net Tax Payable", "Total Tax Payable", "Tax Payable"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "itr_form_type",
                    "",
                    labels={"en": ["Form Number", "ITR Form", "Form No"]},
                    pattern=r"(?i)^ITR[\s-]?[1-7][A-Z]?$",
                ),
                _f(
                    "status",
                    "entity.constitution",
                    labels={"en": ["Status", "Assessee Status"]},
                    notes="Individual / HUF / Firm / Company — determines whether the "
                    "downstream subject is a person or an entity.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_form16",
            label="Form 16 — TDS certificate on salary",
            country="IN",
            category=Category.tax,
            issuing_authority="Employer (deductor), generated via TRACES",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM NO. 16", decisive=True),
                Anchor(text="FORM NO 16", decisive=True),
                Anchor(
                    text="Certificate under Section 203 of the Income-tax Act, 1961 for tax "
                    "deducted at source on salary",
                    decisive=True,
                ),
                Anchor(text="TAN of the Deductor", decisive=True),
                Anchor(text="TRACES"),
                Anchor(text="PART A"),
                Anchor(text="PART B"),
                Anchor(text="Name and address of the Employer"),
                Anchor(text="Name and address of the Employee"),
                Anchor(text="Deductor"),
                Anchor(text="Deductee"),
                Anchor(text="Quarterly Statements of TDS"),
                Anchor(text="Summary of tax deducted at source"),
                Anchor(text="Gross Salary"),
                Anchor(text="Chapter VI-A"),
                Anchor(text="Deductions under Chapter VI-A"),
                Anchor(text="Assessment Year"),
                Anchor(text="Certificate Number"),
                Anchor(text="Period with the Employer"),
                Anchor(text="आयकर विभाग", lang="hi"),
            ],
            id_patterns=[r"\b[A-Z]{4}\d{5}[A-Z]\b", r"\b[A-Z]{5}\d{4}[A-Z]\b"],
            confusable_with={
                "in_itr_acknowledgement": (
                    "Form 16 is issued by an employer against a TAN; ITR-V is the taxpayer's own "
                    "filed return"
                ),
                "in_salary_slip": (
                    "Form 16 is an annual statutory TDS certificate; a salary slip is a monthly "
                    "payroll document"
                ),
            },
            negative_anchors=["INDIAN INCOME TAX RETURN ACKNOWLEDGEMENT", "SALARY SLIP", "Net Pay"],
            handling=(
                "The strongest routinely available evidence of salaried income, because it is "
                "reconciled against TDS actually deposited. Contains both employee and "
                "employer identifiers — treat the whole document as pii."
            ),
            fields=[
                _f(
                    "employee_pan",
                    "id.pan",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["PAN of the Employee", "PAN of the Deductee", "PAN"],
                        "hi": ["पैन"],
                    },
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "employer_tan",
                    "id.tan",
                    kind="id",
                    required=True,
                    labels={"en": ["TAN of the Deductor", "TAN"]},
                    pattern=r"\b[A-Z]{4}\d{5}[A-Z]\b",
                    locators=("label", "kv", "regex"),
                    notes="Ten characters: 4 letters, 5 digits, 1 letter. Structure only — no "
                    "published check digit.",
                ),
                _f(
                    "employer_pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels={"en": ["PAN of the Deductor", "PAN of the Employer"]},
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "employee_name",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": [
                            "Name of the Employee",
                            "Name and address of the Employee",
                            "Employee Name",
                        ]
                    },
                ),
                _f(
                    "employer_name",
                    "income.employer",
                    required=True,
                    labels={
                        "en": [
                            "Name of the Employer",
                            "Name and address of the Employer",
                            "Deductor",
                        ]
                    },
                ),
                _address_field("address.residential"),
                _pincode_field(),
                _f(
                    "assessment_year",
                    "doc.assessment_year",
                    required=True,
                    pattern=r"\b(?:19|20)\d{2}\s*[-/]\s*\d{2}\b",
                    labels={"en": ["Assessment Year", "A.Y.", "AY"]},
                ),
                _f(
                    "certificate_number",
                    "doc.reference_number",
                    labels={"en": ["Certificate Number", "Certificate No"]},
                    notes="TRACES certificate numbers are a short alphanumeric string; no "
                    "published format, so structure is not enforced.",
                ),
                _f(
                    "gross_salary",
                    "income.gross_salary",
                    kind="number",
                    labels={
                        "en": ["Gross Salary", "Total amount of salary", "Salary as per provisions"]
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "total_income",
                    "income.total_income",
                    kind="number",
                    labels={"en": ["Total Income", "Total taxable income"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "tax_deducted",
                    "income.tax_deducted",
                    kind="number",
                    labels={
                        "en": [
                            "Total tax deducted",
                            "Tax deducted at source",
                            "Amount of tax deducted",
                        ]
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "employment_period",
                    "",
                    labels={"en": ["Period with the Employer", "Period", "From", "To"]},
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_salary_slip",
            label="Salary Slip / Pay Slip / Wage Slip",
            country="IN",
            category=Category.financial,
            issuing_authority="Employer payroll",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="SALARY SLIP", decisive=True),
                Anchor(text="PAY SLIP", decisive=True),
                Anchor(text="PAYSLIP", decisive=True, zone=Zone.title),
                Anchor(text="वेतन पर्ची", lang="hi", decisive=True),
                Anchor(text="SALARY STATEMENT", decisive=True),
                # The statutory variant. India's *prescribed* wage document is not called a
                # salary slip: rule 78(1)(b) of the Contract Labour (Regulation and
                # Abolition) Central Rules, 1971 requires a "Wage Slip" in Form XIX, and the
                # state rule sets reproduce it verbatim — Form XIX in Andhra Pradesh /
                # Telangana, Karnataka, Maharashtra and Gujarat, Form 15 under Haryana's
                # rule 77(1)(b). The *form number* varies by state; the *title* "Wage Slip"
                # does not, which is why the title is the anchor and the number is not.
                # Rule 26(2) of the Minimum Wages (Central) Rules, 1950 prescribes the same
                # title for its Form XI. Without this, every contract-labour and
                # daily-wage pay document in India is invisible to this doctype.
                Anchor(text="WAGE SLIP", decisive=True),
                # Form XIX's column headings, which the prescribed form prints whether or
                # not it has been filled in. They are wage-period vocabulary that no other
                # registered doctype uses: a corporate payslip says "Basic"/"HRA", a Form
                # XIX says "gross wages payable" and "net amount of wages paid".
                Anchor(text="Gross wages payable"),
                Anchor(text="Net amount of wages paid"),
                Anchor(text="Rate of daily wages"),
                Anchor(text="Amount of overtime wages"),
                Anchor(text="piece rate"),
                Anchor(text="Wage Period"),
                Anchor(text="Earnings"),
                Anchor(text="Deductions"),
                Anchor(text="Basic"),
                Anchor(text="HRA"),
                Anchor(text="House Rent Allowance"),
                Anchor(text="Special Allowance"),
                Anchor(text="Conveyance"),
                Anchor(text="Provident Fund"),
                Anchor(text="भविष्य निधि", lang="hi"),
                Anchor(text="Professional Tax"),
                Anchor(text="Net Pay"),
                Anchor(text="Gross Earnings"),
                Anchor(text="Total Deductions"),
                Anchor(text="UAN"),
                Anchor(text="ESI"),
                Anchor(text="Employee Code"),
                Anchor(text="Days Paid"),
                Anchor(text="LOP"),
                Anchor(text="Pay Period"),
                Anchor(text="CTC"),
            ],
            id_patterns=[r"\b\d{12}\b"],
            confusable_with={
                "in_form16": (
                    "a salary slip covers one month and shows net pay; Form 16 is the annual "
                    "statutory TDS certificate"
                ),
                "in_pension_payment_order": (
                    "a slip reports one month's payroll; a PPO sanctions a pension once"
                ),
            },
            negative_anchors=["FORM NO. 16", "PENSION PAYMENT ORDER", "TAN of the Deductor"],
            handling=(
                "An employer-generated document with no issuer verification — the easiest "
                "income document to forge. Never treat a salary slip figure as verified "
                "income; corroborate against Form 16 or bank credits."
            ),
            fields=[
                _f(
                    "employee_name",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Employee Name", "Name", "Name of Employee"],
                        "hi": ["कर्मचारी का नाम"],
                    },
                ),
                _f(
                    "employee_code",
                    "",
                    pii=True,
                    labels={
                        "en": ["Employee Code", "Employee No", "Emp ID", "Employee ID", "Token No"]
                    },
                ),
                _f(
                    "employer",
                    "income.employer",
                    required=True,
                    labels={"en": ["Company", "Employer", "Organisation", "Company Name"]},
                    notes="Usually only in the masthead rather than a labelled field — read "
                    "from the title zone when the label lookup misses.",
                ),
                _f(
                    "designation",
                    "",
                    labels={"en": ["Designation", "Grade", "Department", "Position"]},
                ),
                _f(
                    "pay_period",
                    "utility.bill_period",
                    labels={
                        "en": ["Pay Period", "Month", "Salary Month", "For the month of", "Period"],
                        "hi": ["माह"],
                    },
                ),
                _f(
                    "uan",
                    "id.uan",
                    kind="id",
                    pii=True,
                    labels={"en": ["UAN", "UAN No", "Universal Account Number"]},
                    pattern=r"\b\d{12}\b",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "gross_earnings",
                    "income.gross_salary",
                    kind="number",
                    required=True,
                    labels={
                        "en": ["Gross Earnings", "Gross Salary", "Total Earnings", "Gross Pay"],
                        "hi": ["कुल आय"],
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Sits in the earnings/deductions table, not in a key-value pair — the "
                    "table locator is the one that works on most payroll templates.",
                ),
                _f(
                    "net_pay",
                    "income.net_pay",
                    kind="number",
                    required=True,
                    labels={
                        "en": ["Net Pay", "Net Salary", "Take Home", "Net Amount Payable"],
                        "hi": ["शुद्ध वेतन"],
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "total_deductions",
                    "",
                    kind="number",
                    labels={"en": ["Total Deductions", "Deductions", "Total Deduction"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "bank_account_number",
                    "account.number",
                    kind="id",
                    pii=True,
                    pattern=r"\b\d{9,18}\b",
                    labels=_L_ACCOUNT_NO,
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_certificate_incorporation",
            label="Certificate of Incorporation (company)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Registrar of Companies, Ministry of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "CERTIFICATE OF INCORPORATION" was decisive. It is the title a company
                # registrar in India, England, Delaware and Ontario each chose independently,
                # and both us_articles_incorporation and ca_articles_incorporation_provincial
                # claim the string here. Demoted. "MINISTRY OF CORPORATE AFFAIRS" is the
                # actual Indian signature and stays decisive; it is shared only with
                # in_llp_incorporation, which is a declared, mutual, same-issuer overlap.
                Anchor(text="CERTIFICATE OF INCORPORATION"),
                Anchor(text="MINISTRY OF CORPORATE AFFAIRS", decisive=True),
                Anchor(text="कॉर्पोरेट कार्य मंत्रालय", lang="hi", decisive=True),
                Anchor(text="Corporate Identity Number", decisive=True),
                Anchor(text="Registrar of Companies"),
                Anchor(text="कंपनी रजिस्ट्रार", lang="hi"),
                Anchor(text="Companies Act, 2013"),
                Anchor(text="Form No. INC-11"),
                Anchor(text="CIN"),
                Anchor(text="Permanent Account Number (PAN) of the company"),
                Anchor(text="TAN of the company"),
                Anchor(text="incorporated on"),
                Anchor(text="Private Limited"),
                Anchor(text="Public Limited"),
                Anchor(text="Central Registration Centre"),
                Anchor(text="mca.gov.in"),
                Anchor(text="Digitally signed"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_llp_incorporation": (
                    "both are MCA incorporation certificates; the company certificate carries "
                    "a 21-character CIN and cites the Companies Act 2013, the LLP certificate "
                    "carries an LLPIN and cites the LLP Act 2008"
                ),
                "in_moa": (
                    "the certificate is the registrar's one-page attestation; the memorandum is the"
                    " constitutional document it attests"
                ),
            },
            negative_anchors=[
                "Limited Liability Partnership Act, 2008",
                "LLP Identification Number",
                "LLPIN",
                "MEMORANDUM OF ASSOCIATION",
                "ARTICLES OF ASSOCIATION",
                # Added with the MCA e-form doctypes (MGT-7, AOC-4, DIR-12, PAS-3, SH-7,
                # CHG-1, INC-20A). Those forms all print "Corporate identity number (CIN) of
                # company", inside which this doctype's decisive anchor "Corporate Identity
                # Number" matches as a token n-gram — so without these, every MCA e-form
                # carries a decisive claim for a certificate of incorporation. Both strings
                # are MCA e-form furniture and appear on no certificate, so this is evidence
                # added, never evidence removed.
                "Refer the instruction kit for filing the form",
                "Global location number",
            ],
            handling=(
                "Establishes the legal existence of the entity, not the identity of anyone "
                "behind it. Directors and beneficial owners still need their own OVDs."
            ),
            fields=[
                _f(
                    "cin",
                    "id.cin",
                    kind="id",
                    required=True,
                    labels={"en": ["Corporate Identity Number", "CIN", "CIN Number"]},
                    pattern=r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b",
                    locators=("regex", "label", "kv"),
                    notes="21 characters: listing status (L listed / U unlisted), 5-digit "
                    "industry code, 2-letter state, 4-digit year, 3-letter ownership code, "
                    "6-digit registration number. Structure only; no check digit exists.",
                ),
                _f("company_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "incorporation_date",
                    "entity.incorporation_date",
                    kind="date",
                    required=True,
                    labels={
                        "en": ["Date of Incorporation", "incorporated on", "Dated"],
                        "hi": ["निगमन तिथि"],
                    },
                    validator="generic_date",
                ),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                    notes="Certificates issued since the SPICe+ regime print the company PAN "
                    "and TAN on the face of the certificate; older ones do not.",
                ),
                _f(
                    "tan",
                    "id.tan",
                    kind="id",
                    labels={"en": ["TAN", "TAN of the company"]},
                    pattern=r"\b[A-Z]{4}\d{5}[A-Z]\b",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "registrar",
                    "doc.issuing_authority",
                    labels={"en": ["Registrar of Companies", "RoC", "Issued by"]},
                ),
                _f(
                    "constitution",
                    "entity.constitution",
                    labels={"en": ["Type of Company", "Class of Company", "Category"]},
                    notes="Derivable from the CIN ownership code; prefer the derived value.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_llp_incorporation",
            label="LLP Certificate of Incorporation",
            country="IN",
            category=Category.corporate,
            issuing_authority="Registrar of Companies, Ministry of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # Demoted for the same reason as in_certificate_incorporation: a company
                # registrar's title, not one jurisdiction's string. The LLP-specific decisive
                # anchors below ("Limited Liability Partnership Act, 2008", "LLP
                # Identification Number") are what separate this from a company certificate,
                # and they are unaffected.
                Anchor(text="CERTIFICATE OF INCORPORATION"),
                Anchor(text="MINISTRY OF CORPORATE AFFAIRS", decisive=True),
                Anchor(text="कॉर्पोरेट कार्य मंत्रालय", lang="hi", decisive=True),
                Anchor(text="Limited Liability Partnership Act, 2008", decisive=True),
                Anchor(text="LLP Identification Number", decisive=True),
                Anchor(text="LLPIN"),
                Anchor(text="Limited Liability Partnership"),
                Anchor(text="Form No. FiLLiP"),
                Anchor(text="Designated Partner"),
                Anchor(text="DPIN"),
                Anchor(text="Registrar of Companies"),
                Anchor(text="mca.gov.in"),
                Anchor(text="Digitally signed"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[A-Z]{3}-\d{4}\b"],
            confusable_with={
                "in_certificate_incorporation": (
                    "both are MCA incorporation certificates; the LLP certificate cites the "
                    "LLP Act 2008 and carries an LLPIN, the company certificate cites the "
                    "Companies Act 2013 and carries a CIN"
                ),
                "in_partnership_deed": (
                    "an LLP is a body corporate registered with the MCA; a partnership firm is "
                    "constituted by deed under the 1932 Act"
                ),
            },
            negative_anchors=[
                "Corporate Identity Number",
                "Companies Act, 2013",
                "INDIAN PARTNERSHIP ACT",
            ],
            handling=(
                "Establishes the LLP's legal existence. Designated partners still require "
                "their own OVDs; an LLPIN is not identity evidence for any individual."
            ),
            fields=[
                _f(
                    "llpin",
                    "id.llpin",
                    kind="id",
                    required=True,
                    labels={"en": ["LLPIN", "LLP Identification Number", "LLP Identity Number"]},
                    pattern=r"\b[A-Z]{3}-\d{4}\b",
                    locators=("regex", "label", "kv"),
                    notes="Three letters, a hyphen, four digits (e.g. AAA-1234). Structure only.",
                ),
                _f("llp_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "incorporation_date",
                    "entity.incorporation_date",
                    kind="date",
                    required=True,
                    labels={"en": ["Date of Incorporation", "incorporated on", "Dated"]},
                    validator="generic_date",
                ),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "designated_partners",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Designated Partner", "Designated Partners", "Partners"]},
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "pan",
                    "id.pan",
                    kind="id",
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                ),
                _f(
                    "registrar",
                    "doc.issuing_authority",
                    labels={"en": ["Registrar of Companies", "RoC", "Issued by"]},
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_moa",
            label="Memorandum of Association",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed with the Registrar of Companies (constitutional document "
            "of the company)",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="MEMORANDUM OF ASSOCIATION", decisive=True),
                Anchor(text="The name of the Company is", decisive=True),
                Anchor(
                    text="The objects to be pursued by the Company on its incorporation are",
                    decisive=True,
                ),
                Anchor(text="The Companies Act, 2013"),
                Anchor(text="COMPANY LIMITED BY SHARES"),
                Anchor(
                    text="The registered office of the Company will be situated in the State of"
                ),
                Anchor(text="The liability of the member"),
                Anchor(text="Authorised Share Capital"),
                Anchor(text="divided into"),
                Anchor(text="equity shares of"),
                Anchor(text="SUBSCRIBERS"),
                Anchor(text="Names, addresses, descriptions and occupations of subscribers"),
                Anchor(text="Number of shares taken by each subscriber"),
                Anchor(text="WITNESS"),
                Anchor(text="Form No. INC-33"),
                Anchor(text="Table A"),
            ],
            id_patterns=[],
            confusable_with={
                "in_aoa": (
                    "the memorandum sets out name, state, objects, liability and capital; the "
                    "articles set out the internal governance rules and cite Table F"
                ),
                "in_certificate_incorporation": (
                    "the memorandum is the constitutional document; the certificate is the "
                    "registrar's attestation of it"
                ),
            },
            negative_anchors=["ARTICLES OF ASSOCIATION", "Table F", "CERTIFICATE OF INCORPORATION"],
            handling=(
                "MOA and AOA are almost always scanned and filed as one PDF. Expect a merged "
                "document and rely on the per-page classification rather than a single "
                "whole-document verdict."
            ),
            fields=[
                _f("company_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "registered_state",
                    "address.state",
                    labels={"en": ["State of", "situated in the State of", "Registered State"]},
                ),
                _f(
                    "objects",
                    "entity.objects",
                    labels={"en": ["Objects", "The objects to be pursued", "Main Objects"]},
                    locators=("label", "kv"),
                    notes="Free prose spanning several numbered clauses; extraction is a "
                    "best-effort capture of the objects clause, not a structured parse.",
                ),
                _f(
                    "authorised_capital",
                    "entity.authorised_capital",
                    kind="number",
                    labels={
                        "en": [
                            "Authorised Share Capital",
                            "The Authorised Share Capital",
                            "Share Capital",
                        ]
                    },
                    validator="amount",
                    locators=("label", "kv", "table"),
                ),
                _f(
                    "subscribers",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={
                        "en": [
                            "Subscribers",
                            "Names, addresses, descriptions and occupations of subscribers",
                        ]
                    },
                    locators=("table", "label"),
                    notes="The subscriber table is the original shareholder list and is the "
                    "starting point for beneficial-ownership work — read it from the table.",
                ),
                _f(
                    "shares_subscribed",
                    "ownership.share",
                    multi=True,
                    labels={"en": ["Number of shares taken by each subscriber", "No. of shares"]},
                    locators=("table",),
                ),
                _f(
                    "execution_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Dated", "Date", "this day of"]},
                    validator="generic_date",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_aoa",
            label="Articles of Association",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed with the Registrar of Companies (internal governance "
            "rules of the company)",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="ARTICLES OF ASSOCIATION", decisive=True),
                Anchor(text="Table F", decisive=True),
                Anchor(text="The regulations contained in Table F", decisive=True),
                Anchor(text="The Companies Act, 2013"),
                Anchor(text="COMPANY LIMITED BY SHARES"),
                Anchor(text="Interpretation"),
                Anchor(text="Share Capital and Variation of Rights"),
                Anchor(text="Lien"),
                Anchor(text="Calls on Shares"),
                Anchor(text="Transfer of Shares"),
                Anchor(text="Transmission of Shares"),
                Anchor(text="General Meetings"),
                Anchor(text="Proceedings at General Meetings"),
                Anchor(text="Board of Directors"),
                Anchor(text="Proceedings of the Board"),
                Anchor(text="Winding Up"),
                Anchor(text="Indemnity"),
                Anchor(text="Form No. INC-34"),
            ],
            id_patterns=[],
            confusable_with={
                "in_moa": (
                    "the articles set out internal governance and cite Table F; the memorandum "
                    "sets out name, state, objects, liability and capital"
                ),
            },
            negative_anchors=[
                "MEMORANDUM OF ASSOCIATION",
                "The objects to be pursued by the Company on its incorporation are",
                "Table A",
            ],
            handling=(
                "Usually bound with the MOA in a single scan. The share-transfer and board "
                "articles are what a control analysis actually needs; the rest is boilerplate "
                "from Table F."
            ),
            fields=[
                _f("company_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "directors",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Board of Directors", "First Directors", "Directors"]},
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "subscribers",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Subscribers", "Names of subscribers"]},
                    locators=("table", "label"),
                ),
                _f(
                    "execution_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Dated", "Date", "this day of"]},
                    validator="generic_date",
                ),
                _f(
                    "adopts_table_f",
                    "",
                    kind="bool",
                    labels={"en": ["Table F", "The regulations contained in Table F"]},
                    locators=("regex", "label"),
                    notes="Whether the company adopted the statutory Table F regulations "
                    "wholesale. If it did not, the articles are bespoke and a human has to "
                    "read the control provisions.",
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_partnership_deed",
            label="Partnership Deed",
            country="IN",
            category=Category.corporate,
            issuing_authority="Executed between the partners on stamp paper; not issued by "
            "any authority",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="PARTNERSHIP DEED", decisive=True),
                Anchor(text="DEED OF PARTNERSHIP", decisive=True),
                Anchor(text="साझेदारी विलेख", lang="hi", decisive=True),
                Anchor(text="Indian Partnership Act, 1932"),
                Anchor(text="profit sharing ratio"),
                Anchor(text="Profit and Loss Sharing Ratio"),
                Anchor(text="Capital Contribution"),
                Anchor(text="PARTY OF THE FIRST PART"),
                Anchor(text="PARTY OF THE SECOND PART"),
                Anchor(text="hereinafter called the Partners"),
                Anchor(text="the firm shall"),
                Anchor(text="Name of the Firm"),
                Anchor(text="Place of Business"),
                Anchor(text="Dissolution"),
                Anchor(text="INDIAN NON JUDICIAL"),
                Anchor(text="Stamp Duty"),
                Anchor(text="WITNESSETH"),
            ],
            id_patterns=[],
            confusable_with={
                "in_partnership_reg_cert": (
                    "the deed is the contract between the partners; the registration "
                    "certificate is the Registrar of Firms' acknowledgement of it"
                ),
                "in_llp_incorporation": (
                    "a partnership firm is constituted by deed under the 1932 Act; an LLP is a body"
                    " corporate incorporated by the MCA"
                ),
            },
            negative_anchors=[
                "CERTIFICATE OF REGISTRATION OF FIRM",
                "REGISTRAR OF FIRMS",
                "Limited Liability Partnership Act, 2008",
            ],
            handling=(
                "A private contract with no issuer verification. Registration of a partnership "
                "firm is optional in India, so the absence of a registration certificate is "
                "normal and is not by itself a red flag — but nothing in the deed is "
                "independently verified either."
            ),
            fields=[
                _f(
                    "firm_name",
                    "entity.legal_name",
                    required=True,
                    labels={
                        "en": ["Name of the Firm", "Firm Name", "the firm shall be known as"],
                        "hi": ["फर्म का नाम"],
                    },
                ),
                _f(
                    "partners",
                    "ownership.partner",
                    kind="name",
                    multi=True,
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Partners", "Name of Partner", "Party of the First Part"],
                        "hi": ["साझेदार"],
                    },
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "profit_sharing_ratio",
                    "ownership.share",
                    multi=True,
                    labels={
                        "en": ["Profit Sharing Ratio", "Profit and Loss Sharing Ratio", "Share"]
                    },
                    locators=("table", "label", "kv"),
                    notes="The ratio is the control signal for beneficial-ownership analysis; "
                    "read it per partner, not as one string.",
                ),
                _f(
                    "capital_contribution",
                    "entity.paid_up_capital",
                    kind="number",
                    multi=True,
                    labels={"en": ["Capital Contribution", "Capital", "Contribution"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "place_of_business",
                    "address.registered",
                    kind="address",
                    labels={"en": ["Place of Business", "Principal Place of Business", "Office"]},
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "commencement_date",
                    "entity.incorporation_date",
                    kind="date",
                    labels={"en": ["Date of Commencement", "with effect from", "Dated", "Date"]},
                    validator="generic_date",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_partnership_reg_cert",
            label="Certificate of Registration of Firm (Registrar of Firms)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Registrar of Firms of the State",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="CERTIFICATE OF REGISTRATION OF FIRM", decisive=True),
                Anchor(text="REGISTRAR OF FIRMS", decisive=True),
                Anchor(text="फर्म पंजीकरण प्रमाण पत्र", lang="hi", decisive=True),
                Anchor(text="Indian Partnership Act, 1932"),
                Anchor(text="Section 59"),
                Anchor(text="Form A"),
                Anchor(text="Firm Registration Number"),
                Anchor(text="Registration No"),
                Anchor(text="Date of Registration"),
                Anchor(text="Name of the Firm"),
                Anchor(text="Principal Place of Business"),
                Anchor(text="Names of Partners"),
            ],
            id_patterns=[],
            confusable_with={
                "in_partnership_deed": (
                    "the certificate is the Registrar of Firms' acknowledgement; the deed is "
                    "the underlying contract between the partners"
                ),
            },
            negative_anchors=["PARTNERSHIP DEED", "DEED OF PARTNERSHIP", "WITNESSETH"],
            handling=(
                "Confirms the firm is on the Register of Firms. It attests registration, not "
                "the current partner list — a firm's partners can change without the "
                "certificate being reissued, so corroborate against the current deed."
            ),
            fields=[
                _f(
                    "firm_registration_number",
                    "id.firm_registration_number",
                    kind="id",
                    required=True,
                    labels={
                        "en": [
                            "Firm Registration Number",
                            "Registration No",
                            "Registration Number",
                        ],
                        "hi": ["पंजीकरण संख्या"],
                    },
                    notes="Numbering is state-specific (often a serial plus a year, sometimes "
                    "with a district code). No pattern is enforced.",
                ),
                _f(
                    "firm_name",
                    "entity.legal_name",
                    required=True,
                    labels={"en": ["Name of the Firm", "Firm Name"], "hi": ["फर्म का नाम"]},
                ),
                _f(
                    "partners",
                    "ownership.partner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Names of Partners", "Partners", "Partner"]},
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "place_of_business",
                    "address.registered",
                    kind="address",
                    labels={"en": ["Principal Place of Business", "Place of Business", "Address"]},
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "registration_date",
                    "entity.incorporation_date",
                    kind="date",
                    labels={"en": ["Date of Registration", "Registered on", "Dated"]},
                    validator="generic_date",
                ),
                _f(
                    "registrar_office",
                    "doc.issuing_authority",
                    labels={"en": ["Registrar of Firms", "Office of the Registrar", "Issued by"]},
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_board_resolution",
            label="Board Resolution / Certified True Copy",
            country="IN",
            category=Category.corporate,
            issuing_authority="Board of Directors of the company, certified by a director or "
            "company secretary",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="CERTIFIED TRUE COPY OF THE RESOLUTION", decisive=True),
                Anchor(text="BOARD RESOLUTION", decisive=True),
                Anchor(text="RESOLVED FURTHER THAT", decisive=True),
                Anchor(text="CERTIFIED TRUE COPY", decisive=True),
                Anchor(text="RESOLVED THAT"),
                Anchor(text="meeting of the Board of Directors"),
                Anchor(text="duly convened"),
                Anchor(text="held at the registered office"),
                Anchor(text="Authorised Signatory"),
                Anchor(text="अधिकृत हस्ताक्षरकर्ता", lang="hi"),
                Anchor(text="specimen signature"),
                Anchor(text="be and is hereby authorised"),
                Anchor(text="Chairman"),
                Anchor(text="Quorum"),
                Anchor(text="For and on behalf of"),
                Anchor(text="DIN"),
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_aoa": (
                    "the articles are the standing governance rules; a resolution is one dated "
                    "decision taken under them"
                ),
                "in_certificate_incorporation": (
                    "a resolution names the company and its CIN but records a board decision, not "
                    "the registrar's attestation"
                ),
            },
            negative_anchors=["CERTIFICATE OF INCORPORATION", "ARTICLES OF ASSOCIATION", "Table F"],
            handling=(
                "The document that says WHO may act for the entity. The authorised-signatory "
                "list is the load-bearing output — each named signatory needs their own OVD "
                "before they can transact."
            ),
            fields=[
                _f("company_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "cin",
                    "id.cin",
                    kind="id",
                    labels={"en": ["CIN", "Corporate Identity Number"]},
                    pattern=r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b",
                    locators=("regex", "label", "kv"),
                ),
                _f(
                    "meeting_date",
                    "doc.issue_date",
                    kind="date",
                    required=True,
                    labels={"en": ["held on", "Date of Meeting", "Dated", "Date"]},
                    validator="generic_date",
                ),
                _f(
                    "authorised_signatories",
                    "ownership.authorized_signer",
                    kind="name",
                    multi=True,
                    required=True,
                    pii=True,
                    labels={
                        "en": [
                            "Authorised Signatory",
                            "Authorized Signatory",
                            "be and is hereby authorised",
                            "Name of Signatory",
                        ]
                    },
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "directors_present",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Directors Present", "Present", "Members Present"]},
                    locators=("table", "label"),
                ),
                _f(
                    "resolution_text",
                    "",
                    labels={"en": ["RESOLVED THAT", "RESOLVED FURTHER THAT", "Resolution"]},
                    locators=("label", "kv"),
                    notes="Free prose. Captured verbatim for the reviewer; not parsed into "
                    "structured powers.",
                ),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _f(
                    "certified_by",
                    "",
                    labels={
                        "en": [
                            "Certified by",
                            "Company Secretary",
                            "Director",
                            "For and on behalf of",
                        ]
                    },
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_birth_certificate",
            label="Birth Certificate",
            country="IN",
            category=Category.identity,
            issuing_authority="Registrar of Births and Deaths (municipal corporation, "
            "municipality or gram panchayat)",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="BIRTH CERTIFICATE", decisive=True),
                Anchor(text="CERTIFICATE OF BIRTH", decisive=True),
                Anchor(text="जन्म प्रमाण पत्र", lang="hi", decisive=True),
                Anchor(text="REGISTRATION OF BIRTHS AND DEATHS ACT", decisive=True),
                Anchor(text="जन्म एवं मृत्यु पंजीकरण अधिनियम", lang="hi", decisive=True),
                Anchor(text="Registrar of Births and Deaths"),
                Anchor(text="Date of Registration"),
                Anchor(text="पंजीकरण तिथि", lang="hi"),
                Anchor(text="Registration No"),
                Anchor(text="Place of Birth"),
                Anchor(text="जन्म स्थान", lang="hi"),
                Anchor(text="Name of Mother"),
                Anchor(text="Name of Father"),
                Anchor(text="Permanent Address of Parents"),
                Anchor(text="issued in pursuance of section 12"),
                Anchor(text="Municipal Corporation"),
                Anchor(text="नगर निगम", lang="hi"),
                Anchor(text="Sex"),
            ],
            id_patterns=[],
            confusable_with={
                "in_marriage_certificate": (
                    "the birth certificate registers a birth under the RBD Act 1969; the marriage "
                    "certificate registers a marriage"
                ),
                "in_caste_certificate": (
                    "both are state-issued certificates about a person; the birth certificate cites"
                    " the Registration of Births and Deaths Act"
                ),
            },
            negative_anchors=[
                "MARRIAGE CERTIFICATE",
                "विवाह प्रमाण पत्र",
                "CASTE CERTIFICATE",
                "जाति प्रमाण पत्र",
                "Scheduled Caste",
            ],
            handling=(
                "NOT an Officially Valid Document. Evidences date and place of birth and "
                "parentage; it carries no photograph and no current address, so it can never "
                "stand alone as identity or address evidence."
            ),
            fields=[
                _name_field(),
                _dob_field(required=True),
                _sex_field(),
                _f(
                    "place_of_birth",
                    "identity.place_of_birth",
                    required=True,
                    pii=True,
                    labels={"en": ["Place of Birth", "Hospital", "Place"], "hi": ["जन्म स्थान"]},
                ),
                _father_field(),
                _f("mother_name", "identity.mother_name", kind="name", pii=True, labels=_L_MOTHER),
                _address_field(required=False),
                _pincode_field(),
                _f(
                    "registration_number",
                    "doc.registration_number",
                    required=True,
                    labels={
                        "en": ["Registration No", "Registration Number", "Certificate No"],
                        "hi": ["पंजीकरण संख्या"],
                    },
                    notes=(
                        "Numbering is per registrar and per year; no national format. Not regexed."
                    ),
                ),
                _f(
                    "registration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={
                        "en": ["Date of Registration", "Registered on", "Date of Issue"],
                        "hi": ["पंजीकरण तिथि"],
                    },
                    validator="generic_date",
                ),
                _f(
                    "registrar",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Registrar", "Registrar of Births and Deaths", "Issuing Authority"]
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_marriage_certificate",
            label="Marriage Certificate",
            country="IN",
            category=Category.identity,
            issuing_authority="Registrar of Marriages (State), under the Hindu Marriage Act "
            "1955 or the Special Marriage Act 1954",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="MARRIAGE CERTIFICATE", decisive=True),
                Anchor(text="CERTIFICATE OF MARRIAGE", decisive=True),
                Anchor(text="विवाह प्रमाण पत्र", lang="hi", decisive=True),
                Anchor(text="Hindu Marriage Act, 1955", decisive=True),
                Anchor(text="Special Marriage Act, 1954", decisive=True),
                Anchor(text="Registrar of Marriages"),
                Anchor(text="विवाह अधिकारी", lang="hi"),
                Anchor(text="solemnized"),
                Anchor(text="solemnised"),
                Anchor(text="Bridegroom"),
                Anchor(text="Bride"),
                Anchor(text="वर", lang="hi"),
                Anchor(text="वधू", lang="hi"),
                Anchor(text="Date of Marriage"),
                Anchor(text="विवाह की तिथि", lang="hi"),
                Anchor(text="Place of Marriage"),
                Anchor(text="Witness"),
                Anchor(text="registered under section"),
            ],
            id_patterns=[],
            confusable_with={
                "in_birth_certificate": (
                    "the marriage certificate registers a marriage; the birth certificate cites the"
                    " Registration of Births and Deaths Act"
                ),
            },
            negative_anchors=["BIRTH CERTIFICATE", "जन्म प्रमाण पत्र", "CASTE CERTIFICATE"],
            handling=(
                "NOT an Officially Valid Document. Its KYC use is evidencing a name change "
                "after marriage — link the pre- and post-marriage names rather than treating "
                "it as identity evidence in its own right."
            ),
            fields=[
                _f(
                    "husband_name",
                    "identity.full_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Bridegroom", "Name of Bridegroom", "Husband", "Groom"],
                        "hi": ["वर का नाम", "पति का नाम"],
                    },
                ),
                _f(
                    "wife_name",
                    "identity.spouse_name",
                    kind="name",
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Bride", "Name of Bride", "Wife"],
                        "hi": ["वधू का नाम", "पत्नी का नाम"],
                    },
                ),
                _f(
                    "marriage_date",
                    "doc.issue_date",
                    kind="date",
                    required=True,
                    labels={
                        "en": ["Date of Marriage", "Solemnized on", "Married on"],
                        "hi": ["विवाह की तिथि"],
                    },
                    validator="generic_date",
                ),
                _f(
                    "place_of_marriage",
                    "",
                    labels={
                        "en": ["Place of Marriage", "Solemnized at", "Place"],
                        "hi": ["विवाह स्थान"],
                    },
                ),
                _address_field(required=False),
                _pincode_field(),
                _f(
                    "registration_number",
                    "doc.registration_number",
                    labels={
                        "en": ["Registration No", "Certificate No", "Serial No"],
                        "hi": ["पंजीकरण संख्या"],
                    },
                    notes="State-specific numbering; not regexed.",
                ),
                _f(
                    "registration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of Registration", "Registered on"], "hi": ["पंजीकरण तिथि"]},
                    validator="generic_date",
                ),
                _f(
                    "registrar",
                    "doc.issuing_authority",
                    labels={
                        "en": ["Registrar of Marriages", "Marriage Officer", "Issuing Authority"]
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_caste_certificate",
            label="Caste / Community Certificate (SC / ST / OBC)",
            country="IN",
            category=Category.other,
            issuing_authority="Revenue authority of the State — Tahsildar, Sub-Divisional "
            "Magistrate or District Magistrate",
            applies_to="individual",
            officially_valid=False,
            anchors=[
                Anchor(text="CASTE CERTIFICATE", decisive=True),
                Anchor(text="जाति प्रमाण पत्र", lang="hi", decisive=True),
                Anchor(text="COMMUNITY CERTIFICATE", decisive=True),
                Anchor(text="Scheduled Caste"),
                Anchor(text="अनुसूचित जाति", lang="hi"),
                Anchor(text="Scheduled Tribe"),
                Anchor(text="अनुसूचित जनजाति", lang="hi"),
                Anchor(text="Other Backward Class"),
                Anchor(text="अन्य पिछड़ा वर्ग", lang="hi"),
                Anchor(text="Non-Creamy Layer"),
                Anchor(text="Creamy Layer"),
                Anchor(text="Tahsildar"),
                Anchor(text="तहसीलदार", lang="hi"),
                Anchor(text="Sub-Divisional Magistrate"),
                Anchor(text="District Magistrate"),
                Anchor(text="Revenue Department"),
                Anchor(text="Constitution (Scheduled Castes) Order"),
                Anchor(text="ordinarily resides"),
                Anchor(text="belongs to"),
            ],
            id_patterns=[],
            confusable_with={
                "in_birth_certificate": (
                    "both are state-issued certificates about a person; the caste certificate "
                    "states a community and cites the SC/ST/OBC orders"
                ),
            },
            negative_anchors=["BIRTH CERTIFICATE", "MARRIAGE CERTIFICATE", "जन्म प्रमाण पत्र"],
            handling=(
                "NOT an Officially Valid Document. Caste and community are sensitive personal "
                "data under Indian privacy norms: collect only where a specific entitlement "
                "requires it, restrict access, and never propagate the category into a general "
                "customer profile. Do not use it as identity or address evidence."
            ),
            fields=[
                _name_field(),
                _father_field(),
                _f(
                    "category",
                    "identity.category",
                    required=True,
                    labels={
                        "en": ["Category", "Caste", "Community", "belongs to"],
                        "hi": ["श्रेणी", "जाति"],
                    },
                    pattern=(
                        r"(?i)^(SC|ST|OBC|Scheduled Caste|Scheduled Tribe|Other Backward"
                        r" Class)$"
                    ),
                    notes="Sensitive personal data — pii is set on the name fields; treat this "
                    "value with the same care even though it is not a direct identifier.",
                ),
                _f(
                    "caste_name",
                    "",
                    pii=True,
                    labels={"en": ["Caste", "Name of Caste", "Community"], "hi": ["जाति का नाम"]},
                    notes="The specific caste/community name, distinct from the SC/ST/OBC "
                    "bucket. Sensitive; collect only when the entitlement requires it.",
                ),
                _address_field(required=True),
                _pincode_field(),
                _f(
                    "district",
                    "address.district",
                    labels={"en": ["District", "Tehsil", "Taluka"], "hi": ["जिला", "तहसील"]},
                ),
                _f("state", "address.state", labels={"en": ["State"], "hi": ["राज्य"]}),
                _f("certificate_number", "doc.reference_number", labels=_L_CERT_NO),
                _issue_date_field(),
                _f(
                    "issuing_officer",
                    "doc.issuing_authority",
                    labels={
                        "en": [
                            "Tahsildar",
                            "Sub-Divisional Magistrate",
                            "Issuing Authority",
                            "Signature of Issuing Authority",
                        ],
                        "hi": ["तहसीलदार"],
                    },
                ),
            ],
        ),
    ]
)
# ===========================================================================
# Listed-issuer compliance and the corporate due-diligence pack
#
# Everything below this line is a document a company files *about itself* with a regulator —
# the MCA, SEBI, the RBI, the DGFT — or a report a professional signs *about* the company.
# They are the substance of an Indian corporate due-diligence pack, and they are the part of
# this registry where the "decisive anchor" rule bites hardest, because their titles are
# ordinary English nouns.
#
# **The rule that shaped every spec here.** ``ANNUAL REPORT``, ``PROSPECTUS``, ``BALANCE
# SHEET``, ``CORPORATE GOVERNANCE REPORT``, ``SECRETARIAL AUDIT REPORT``, ``INDEPENDENT
# AUDITOR'S REPORT`` are names every issuer in every jurisdiction picks independently. Not one
# of them is decisive below. What *is* decisive is the string the regulator wrote and the
# filer may not change:
#
#   * an MCA e-form number and the rule that mandates it — ``FORM NO. AOC-4``,
#     ``[Pursuant to section 137 of the Companies Act, 2013 and sub-rule (1) of Rule 12 of
#     Companies (Accounts) Rules, 2014]``;
#   * a SEBI-prescribed format title carrying its own regulation number — ``Shareholding
#     Pattern under Regulation 31 of SEBI (Listing Obligations and Disclosure Requirements)
#     Regulations, 2015``;
#   * an ICDR-mandated cover-page sentence — ``Please read Section 32 of the Companies Act,
#     2013``;
#   * a scheme identifier one Indian ministry coined — ``Udyam Registration Number``,
#     ``Importer-Exporter Code``, ``Form FC-GPR``.
#
# Each of those was read off a real filing, not recalled: AOC-4 and CHG-1 from the MCA e-form
# PDFs, MGT-7 from a filed annual return, the shareholding pattern from CDSL's own Reg-31
# filing, the corporate-governance format from Bharat Dynamics' quarterly report, MR-3 from
# IDBI Bank's FY2020-21 secretarial audit, the DRHP cover text from three unrelated issuers'
# draft red herring prospectuses. Where the reading contradicted the plan, the plan lost — see
# the note on ``in_sebi_registration_certificate`` that is *not* in this file.
#
# **Two anchors that look decisive and are not.** ``SECURITIES AND EXCHANGE BOARD OF INDIA``
# heads six doctypes below: it proves the regulator, not the document, exactly like
# ``INCOME TAX DEPARTMENT`` upstream. And ``Issue of Capital and Disclosure Requirements`` was
# the obvious decisive anchor for an offer document until the IDBI MR-3 was read — a
# secretarial audit report enumerates *every* SEBI regulation the company is subject to, ICDR
# among them. Both are present below, on several specs, decisive on none.
#
# **Attribute keys this section wanted and did not have.** ``loader.ATTRIBUTE_KEYS`` is shared
# and is not edited from here. Six fields therefore carry an empty ``attribute_key`` and stay
# doc-local: reporting period / financial year, ticker-and-exchange (scrip code), auditor firm
# name, DIN, ISIN and shares outstanding. Each says so in its ``notes``. They are reported as a
# loader change request rather than smuggled into a near-miss key — filing a fiscal year under
# ``doc.assessment_year`` would put a company's reporting period in the same bucket as an
# income-tax assessment year, and the merge view would silently conflate them.
# ===========================================================================
_L_LISTED_ENTITY = {
    "en": [
        "Name of Listed Entity",
        "Name of the Listed Entity",
        "Name of the Company",
        "Name of Company",
        "Name of the Issuer",
    ],
    "hi": ["सूचीबद्ध कंपनी का नाम"],
}
_L_FINANCIAL_YEAR = {
    "en": [
        "Financial Year",
        "Financial year to which financial statements relates",
        "For the financial year ended",
        "FOR THE FINANCIAL YEAR ENDED",
        "Year ended",
        "Period",
    ],
    "hi": ["वित्तीय वर्ष"],
}
_L_SRN = {
    "en": ["SRN", "Service Request Number", "SRN of Form", "Challan Number"],
}
_L_DIN = {
    "en": ["DIN", "DIN/PAN", "Director Identification Number", "DIN of the director"],
}
_L_SCRIP = {
    "en": ["Scrip Code", "Scrip Code/Name of Scrip/Class of Security", "Symbol", "Stock Code"],
}


def _company_name_field(*, required: bool = True) -> FieldSpec:
    """The filer's legal name. Every document in this section names one company."""
    return _f("company_name", "entity.legal_name", required=required, labels=_L_ENTITY)


def _cin_field(*, required: bool = False) -> FieldSpec:
    """The CIN, as printed on an MCA e-form or a SEBI filing.

    Deliberately a copy of the shape used by :data:`in_certificate_incorporation` rather than a
    reference to it: the same identifier, the same 21-character structure, the same absence of
    a check digit. The listing status is the first character — ``L`` for a listed company,
    ``U`` for an unlisted one — which is the single most useful bit on a due-diligence pack and
    is why the pattern keeps the character class explicit instead of collapsing it to ``[A-Z]``.
    """
    return _f(
        "cin",
        "id.cin",
        kind="id",
        required=required,
        labels={
            "en": [
                "Corporate Identity Number",
                "CIN",
                "Corporate identity number (CIN) of company",
                "CIN of the company",
            ],
            "hi": ["कॉर्पोरेट पहचान संख्या"],
        },
        pattern=r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b",
        locators=("regex", "label", "kv"),
        notes="21 characters: listing status (L listed / U unlisted), 5-digit industry code, "
        "2-letter state, 4-digit year, 3-letter ownership code, 6-digit registration "
        "number. Structure only; no check digit exists.",
    )


def _srn_field() -> FieldSpec:
    """MCA Service Request Number — the receipt for a filing, and the thing a reviewer
    re-queries the portal with."""
    return _f(
        "srn",
        "doc.reference_number",
        kind="id",
        labels=_L_SRN,
        locators=("label", "kv"),
        notes="MCA allots SRNs from several series (letter plus digits, length varies by "
        "vintage and by portal generation). No pattern is enforced: a regex tight enough "
        "to be useful would reject the older series outright.",
    )


def _financial_year_field(*, required: bool = False) -> FieldSpec:
    """The reporting period. Doc-local — see the section note on attribute keys."""
    return _f(
        "financial_year",
        "",
        required=required,
        labels=_L_FINANCIAL_YEAR,
        notes="The Indian financial year runs 1 April to 31 March and is printed as "
        "'2023-24', as '31st March 2024', or as a From/To date pair. Left doc-local: "
        "loader.ATTRIBUTE_KEYS has no reporting-period key, and doc.assessment_year is an "
        "income-tax concept, not a company's fiscal year.",
    )


def _din_field(name: str = "director_din", *, multi: bool = False) -> FieldSpec:
    """Director Identification Number. Personal, so it is flagged pii."""
    return _f(
        name,
        "",
        kind="id",
        multi=multi,
        pii=True,
        labels=_L_DIN,
        pattern=r"\b\d{8}\b",
        locators=("table", "label", "kv"),
        notes="8 digits, allotted to a natural person by the MCA and reused across every "
        "company they sit on — which is what makes it a directorship-graph key and also "
        "why it is personal data. No published check digit. Doc-local: ATTRIBUTE_KEYS has "
        "ownership.director for the name but no key for the DIN itself.",
    )


#: Furniture printed on every MCA e-form. Never decisive — an e-form header is shared by all
#: fifty-odd forms in the MCA catalogue — but strong evidence that the page is an MCA filing
#: rather than a certificate or a board minute, which is precisely the confusion these seven
#: doctypes have to survive.
_MCA_EFORM_FURNITURE = (
    Anchor(text="Refer the instruction kit for filing the form"),
    Anchor(text="Global location number (GLN) of company"),
    Anchor(text="Registrar of Companies"),
    Anchor(text="Companies Act, 2013"),
    Anchor(text="All fields marked in * are to be mandatorily filled"),
    Anchor(text="Pre-Fill"),
    Anchor(text="Digital Signature Certificate"),
)

#: What separates one MCA e-form from another is its form number, so every one of these seven
#: specs declares the other six's numbers as evidence *against* itself. Two forms are never
#: the same page, and a filing bundle that concatenates them is a page-type problem, not a
#: doctype problem.
_MCA_EFORM_NUMBERS = (
    "FORM NO. MGT-7",
    "FORM NO. AOC-4",
    "FORM NO. DIR-12",
    "FORM NO. PAS-3",
    "FORM NO. SH-7",
    "FORM NO. CHG-1",
    "FORM NO. INC-20A",
)


def _other_eform_numbers(mine: str) -> list[str]:
    """The six MCA form numbers that are not ``mine``, for ``negative_anchors``."""
    return [n for n in _MCA_EFORM_NUMBERS if n != mine]


_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_mca_mgt7_annual_return",
            label="MCA Form MGT-7 / MGT-7A Annual Return",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the company with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # The form number is the whole case for decisiveness. MCA prints it three
                # ways depending on portal generation ("FORM NO. MGT-7", "Form MGT-7",
                # "eForm MGT-7") and token n-gram matching treats those as different
                # strings, so all three are declared. MGT-7A is the abridged return for
                # OPCs and small companies — the same document under a different threshold,
                # so it lives here rather than in a doctype of its own.
                Anchor(text="FORM NO. MGT-7", decisive=True),
                Anchor(text="Form MGT-7", decisive=True),
                Anchor(text="FORM NO. MGT-7A", decisive=True),
                Anchor(text="Form MGT-7A", decisive=True),
                Anchor(text="eForm MGT-7"),
                Anchor(text="Companies (Management and Administration) Rules, 2014", decisive=True),
                Anchor(text="sub-Section(1) of section 92 of the Companies Act"),
                # The bare string "Annual Return" is printed on MGT-7, directly under the
                # form number, and this spec would ordinarily claim it. It is deliberately
                # NOT declared, because ``ca_annual_return`` currently declares the same
                # string DECISIVE and the registry's cross-jurisdiction rule then refuses the
                # whole build. Declaring a true claim here would be correct and would take
                # every other pack down with it; the actual defect is on the Canadian side —
                # "ANNUAL RETURN" is a document-class name that a registrar in Ottawa, Delhi
                # and Companies House each chose independently, which is precisely the
                # decisive-anchor rule this registry enforces everywhere else. Reported
                # rather than worked around in the other pack's file. The Hindi title below
                # is uncontested and carries the same evidence for an Indian filing.
                Anchor(text="वार्षिक विवरणी", lang="hi"),
                Anchor(text="REGISTRATION AND OTHER DETAILS"),
                Anchor(text="PRINCIPAL BUSINESS ACTIVITIES OF THE COMPANY"),
                Anchor(text="SHARE CAPITAL, DEBENTURES AND OTHER SECURITIES OF THE COMPANY"),
                Anchor(text="Whether shares listed on recognized Stock Exchange(s)"),
                Anchor(text="Turnover and net worth of the company"),
                Anchor(text="MEETINGS OF MEMBERS"),
                Anchor(text="Number of promoters, members, debenture holders"),
                Anchor(text="Registrar and Transfer Agents"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_aoc4_financial_statements": (
                    "both are the company's annual MCA filings for the same financial year; "
                    "MGT-7 is the annual return under section 92 and carries the member and "
                    "shareholding registers, AOC-4 is the financial statements under section "
                    "137 and carries the balance sheet and profit-and-loss segments"
                ),
                "in_certificate_incorporation": (
                    "the annual return recites the CIN and company name that the certificate "
                    "conferred; the certificate is the registrar's one-off attestation and "
                    "carries no financial year"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. MGT-7"),
                "CERTIFICATE OF INCORPORATION",
                "INDEPENDENT AUDITOR'S REPORT",
            ],
            handling=(
                "The single most useful document in an Indian corporate DD pack: it is the "
                "company's own signed statement of who its members, promoters, directors and "
                "KMP were on the last day of the financial year. It is a snapshot as at the "
                "financial-year end, never as at today — a director who resigned in April will "
                "still be listed in the return filed that November. Treat every officer named "
                "here as a lead to verify against DIR-12, not as current fact."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _financial_year_field(required=True),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "listed_status",
                    "",
                    kind="bool",
                    labels={
                        "en": [
                            "Whether shares listed on recognized Stock Exchange(s)",
                            "Whether listed company",
                        ]
                    },
                    notes="Yes/No on the face of the form, and corroborated by the CIN's first "
                    "character (L = listed). Prefer the printed answer; report the "
                    "disagreement rather than silently picking one.",
                ),
                _f(
                    "authorised_capital",
                    "entity.authorised_capital",
                    kind="number",
                    labels={"en": ["Authorised capital", "Authorised Capital (in Rs.)"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "paid_up_capital",
                    "entity.paid_up_capital",
                    kind="number",
                    labels={"en": ["Paid up capital", "Subscribed capital", "Paid-up Capital"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "directors",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={
                        "en": [
                            "Name of the director",
                            "Directors",
                            "Director",
                            "Key managerial personnel",
                        ]
                    },
                    locators=("table", "label", "kv"),
                ),
                _din_field(multi=True),
                _f(
                    "promoters",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Promoters", "Name of the promoter", "Promoter"]},
                    locators=("table", "label"),
                    notes="MGT-7's promoter list is the closest thing an Indian filing gives to "
                    "a declared beneficial-ownership statement, but it is not one: control "
                    "through a chain of bodies corporate does not appear here. Corroborate "
                    "against the significant-beneficial-owner register before relying on it.",
                ),
                _f(
                    "registrar_transfer_agent",
                    "",
                    labels={
                        "en": [
                            "Registrar and Transfer Agents",
                            "Name of RTA",
                            "Registrar and Share Transfer Agent",
                        ]
                    },
                    notes="Doc-local. Useful for tracing the share register, which is held by "
                    "the RTA rather than by the company.",
                ),
                _f(
                    "agm_date",
                    "",
                    kind="date",
                    labels={"en": ["Date of AGM", "date of annual general meeting", "AGM held on"]},
                    validator="generic_date",
                    notes="Doc-local: this is the date of a corporate event, not the date the "
                    "document was issued, and conflating the two in doc.issue_date would "
                    "make an annual return look like it was issued at its AGM.",
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_aoc4_financial_statements",
            label="MCA Form AOC-4 (financial statements filed with the Registrar)",
            country="IN",
            category=Category.financial,
            issuing_authority="Filed by the company with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # Read verbatim off the MCA e-form: "FORM NO. AOC-4 / Form for filing
                # financial statement and other documents with the Registrar / [Pursuant to
                # section 137 of the Companies Act, 2013 and sub-rule (1) of Rule 12 of
                # Companies (Accounts) Rules, 2014]". The long title line is decisive too:
                # it is MCA's sentence, not the filer's, and it survives an OCR read that
                # loses the small-print form number in the corner.
                Anchor(text="FORM NO. AOC-4", decisive=True),
                Anchor(text="Form AOC-4", decisive=True),
                Anchor(
                    text="Form for filing financial statement and other documents with the "
                    "Registrar",
                    decisive=True,
                ),
                Anchor(text="Companies (Accounts) Rules, 2014", decisive=True),
                Anchor(text="AOC-4 XBRL"),
                Anchor(text="AOC-4 CFS"),
                Anchor(text="section 137 of the Companies Act"),
                Anchor(text="SEGMENT- I: INFORMATION AND PARTICULARS IN RESPECT OF BALANCE SHEET"),
                Anchor(text="INFORMATION AND PARTICULARS IN RESPECT OF PROFIT AND LOSS ACCOUNT"),
                Anchor(text="Whether annual general meeting (AGM) held"),
                Anchor(text="Date of Board of Directors' meeting in which financial statements "
                       "are approved"),
                Anchor(text="Date of signing of reports on the financial statements by the "
                       "auditors"),
                Anchor(text="Figures appearing in the e-Form should be entered in Absolute "
                       "Rupees only"),
                Anchor(text="Whether consolidated financial statements required"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_mgt7_annual_return": (
                    "both are the company's annual MCA filings for the same financial year; "
                    "AOC-4 is the financial statements under section 137 and carries the "
                    "balance sheet and profit-and-loss segments, MGT-7 is the annual return "
                    "under section 92 and carries the member and shareholding registers"
                ),
                "in_statutory_auditor_report": (
                    "the auditor's report is an attachment to AOC-4 and is signed by the "
                    "auditor; AOC-4 itself is the company's e-form, signed by a director, and "
                    "prints the MCA form header"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. AOC-4"),
                "CERTIFICATE OF INCORPORATION",
            ],
            handling=(
                "Carries the filed financial statements, so the numbers here are the ones the "
                "company stood behind at the Registrar — stronger than a management account "
                "and weaker than an audited signed set, because AOC-4 is a transcription of "
                "the statements into MCA's fields. Where the e-form and the attached audited "
                "statements disagree, the attachment governs; surface both."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _financial_year_field(required=True),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _f(
                    "board_approval_date",
                    "",
                    kind="date",
                    labels={
                        "en": [
                            "Date of Board of Directors' meeting in which financial statements "
                            "are approved",
                            "Date of board meeting",
                        ]
                    },
                    validator="generic_date",
                    notes="Doc-local corporate-event date; see the note on MGT-7's agm_date.",
                ),
                _f(
                    "auditor_signing_date",
                    "",
                    kind="date",
                    labels={
                        "en": [
                            "Date of signing of reports on the financial statements by the "
                            "auditors",
                            "Date of auditor's report",
                        ]
                    },
                    validator="generic_date",
                    notes="Doc-local. The gap between this and the board approval date is a "
                    "standard DD red flag when it is negative.",
                ),
                _f(
                    "agm_date",
                    "",
                    kind="date",
                    labels={"en": ["date of AGM", "Whether annual general meeting (AGM) held"]},
                    validator="generic_date",
                    notes="Doc-local corporate-event date.",
                ),
                _f(
                    "auditor_name",
                    "",
                    kind="name",
                    pii=True,
                    labels={
                        "en": [
                            "Name of the auditor",
                            "Statutory Auditor",
                            "Auditors",
                            "Name of the audit firm",
                        ]
                    },
                    notes="Doc-local: ATTRIBUTE_KEYS has no auditor key. Flagged pii because a "
                    "sole practitioner's name is an individual's name; a firm name is not, "
                    "and the field cannot tell them apart before extraction.",
                ),
                _f(
                    "authorised_capital",
                    "entity.authorised_capital",
                    kind="number",
                    labels={
                        "en": [
                            "Authorised capital of the company as on the date of filing",
                            "Authorised capital",
                        ]
                    },
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "paid_up_capital",
                    "entity.paid_up_capital",
                    kind="number",
                    labels={"en": ["Paid up capital", "Subscribed capital"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "turnover",
                    "",
                    kind="number",
                    labels={"en": ["Turnover", "Revenue from operations", "Total revenue"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local: income.amount is a natural person's income key and would "
                    "put a company's revenue in a KYC income bucket.",
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_dir12",
            label="MCA Form DIR-12 (appointment or cessation of directors and KMP)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the company with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM NO. DIR-12", decisive=True),
                Anchor(text="Form DIR-12", decisive=True),
                Anchor(
                    text="Particulars of appointment of directors and the key managerial "
                    "personnel and the changes among them",
                    decisive=True,
                ),
                Anchor(
                    text="Companies (Appointment and Qualification of Directors) Rules, 2014",
                    decisive=True,
                ),
                Anchor(text="section 170(2) of the Companies Act"),
                Anchor(text="Number of directors or key managerial personnel"),
                Anchor(text="Designation at the time of appointment"),
                Anchor(text="Date of appointment"),
                Anchor(text="Date of cessation"),
                Anchor(text="Reason for cessation"),
                Anchor(text="Category of the director"),
                Anchor(text="Director Identification Number"),
                Anchor(text="Managing Director"),
                Anchor(text="Whole-time director"),
                Anchor(text="Company Secretary"),
                Anchor(text="Chief Financial Officer"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_board_resolution": (
                    "the board resolution is the internal decision to appoint; DIR-12 is the "
                    "statutory intimation of that decision to the Registrar and prints an MCA "
                    "form header"
                ),
                "in_mca_mgt7_annual_return": (
                    "the annual return lists the board as at the financial-year end; DIR-12 "
                    "records one dated change to it and names a reason for cessation"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. DIR-12"),
                "CERTIFIED TRUE COPY OF THE RESOLUTION",
            ],
            handling=(
                "The authoritative record of who joined or left the board and when — the "
                "document that resolves a stale MGT-7. Every person named is an individual: "
                "names, DINs, designations and dates of cessation are personal data, and a "
                "cessation reason ('disqualification', 'removal') is adverse personal data. "
                "Mask on export unless the reviewer's purpose covers officer screening."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _f(
                    "officer_names",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    required=True,
                    pii=True,
                    labels={
                        "en": [
                            "Name of the director",
                            "Name",
                            "Name of the key managerial personnel",
                        ]
                    },
                    locators=("table", "label", "kv"),
                ),
                _din_field("officer_din", multi=True),
                _f(
                    "designation",
                    "",
                    multi=True,
                    labels={
                        "en": [
                            "Designation",
                            "Designation at the time of appointment",
                            "Category of the director",
                        ]
                    },
                    locators=("table", "label", "kv"),
                    notes="Doc-local. Managing Director / Whole-time director / Independent / "
                    "Nominee / Company Secretary / CFO — the distinction that decides whether "
                    "a signature binds the company.",
                ),
                _f(
                    "appointment_date",
                    "",
                    kind="date",
                    multi=True,
                    labels={"en": ["Date of appointment", "Date of appointment or change"]},
                    validator="generic_date",
                    locators=("table", "label", "kv"),
                    notes="Doc-local corporate-event date.",
                ),
                _f(
                    "cessation_date",
                    "",
                    kind="date",
                    multi=True,
                    labels={"en": ["Date of cessation", "Date of change"]},
                    validator="generic_date",
                    locators=("table", "label", "kv"),
                    notes="Doc-local corporate-event date.",
                ),
                _f(
                    "cessation_reason",
                    "",
                    multi=True,
                    pii=True,
                    labels={"en": ["Reason for cessation", "Reason for change"]},
                    locators=("table", "label"),
                    notes="Adverse personal data when it reads 'disqualification' or "
                    "'removal'. pii=True is not optional here.",
                ),
                _f(
                    "officer_pan",
                    "id.pan",
                    kind="id",
                    multi=True,
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("table", "label", "regex"),
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_pas3",
            label="MCA Form PAS-3 (return of allotment of securities)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the company with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "Return of Allotment" is the form's title and is NOT decisive: allotment is
                # shared UK-descended company-law vocabulary and a registrar in any
                # Commonwealth jurisdiction could print it. The form number and the Indian
                # rule citation are what one issuer controls.
                Anchor(text="FORM NO. PAS-3", decisive=True),
                Anchor(text="Form PAS-3", decisive=True),
                Anchor(
                    text="Companies (Prospectus and Allotment of Securities) Rules, 2014",
                    decisive=True,
                ),
                Anchor(text="Return of Allotment"),
                Anchor(text="section 39(4) and 42(9) of the Companies Act"),
                Anchor(text="Allotment of securities"),
                Anchor(text="Date of allotment"),
                Anchor(text="Number of securities allotted"),
                Anchor(text="Nominal amount per security"),
                Anchor(text="Premium amount per security"),
                Anchor(text="Whether securities were allotted for consideration other than cash"),
                Anchor(text="Preferential allotment"),
                Anchor(text="Private placement"),
                Anchor(text="Rights issue"),
                Anchor(text="Bonus issue"),
                Anchor(text="List of allottees"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_sh7": (
                    "SH-7 alters the authorised capital ceiling; PAS-3 issues shares within it "
                    "and names the allottees"
                ),
                "in_fema_fcgpr": (
                    "PAS-3 reports the allotment to the Registrar under the Companies Act; "
                    "FC-GPR reports the same allotment to the RBI under FEMA when the allottee "
                    "is resident outside India"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. PAS-3"),
                "Notice to Registrar of any alteration of share capital",
            ],
            handling=(
                "The cap-table event record. The allottee list is the reason the document "
                "exists in a DD pack and it names natural persons as often as bodies "
                "corporate — treat every allottee name as pii until the reviewer has "
                "classified it. Reconcile against SH-7 (was there headroom?) and against "
                "FC-GPR (was any allottee non-resident?)."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _f(
                    "allotment_date",
                    "doc.issue_date",
                    kind="date",
                    required=True,
                    labels={"en": ["Date of allotment", "Date of the allotment"]},
                    validator="generic_date",
                ),
                _f(
                    "securities_allotted",
                    "",
                    kind="number",
                    labels={
                        "en": [
                            "Number of securities allotted",
                            "Number of shares allotted",
                            "No. of securities",
                        ]
                    },
                    locators=("table", "label", "kv"),
                    notes="Doc-local: ATTRIBUTE_KEYS has no shares-outstanding key. This is a "
                    "delta, not a total — never merge it with a share count from another "
                    "document.",
                ),
                _f(
                    "nominal_value",
                    "",
                    kind="number",
                    labels={"en": ["Nominal amount per security", "Face value", "Nominal value"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local.",
                ),
                _f(
                    "premium",
                    "",
                    kind="number",
                    labels={"en": ["Premium amount per security", "Securities premium"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local.",
                ),
                _f(
                    "allottees",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["List of allottees", "Name of the allottee", "Allottee"]},
                    locators=("table", "label"),
                ),
                _f(
                    "allotment_type",
                    "",
                    labels={
                        "en": [
                            "Preferential allotment",
                            "Private placement",
                            "Rights issue",
                            "Bonus issue",
                            "Type of allotment",
                        ]
                    },
                    locators=("mark", "label", "kv"),
                    notes="Doc-local, and usually a tick box rather than a value — hence the "
                    "mark locator first.",
                ),
                _f(
                    "paid_up_capital",
                    "entity.paid_up_capital",
                    kind="number",
                    labels={"en": ["Paid up capital", "Paid-up capital after allotment"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_sh7",
            label="MCA Form SH-7 (notice of alteration of share capital)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the company with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM NO. SH-7", decisive=True),
                Anchor(text="Form SH-7", decisive=True),
                Anchor(
                    text="Notice to Registrar of any alteration of share capital", decisive=True
                ),
                Anchor(
                    text="Companies (Share Capital and Debentures) Rules, 2014", decisive=True
                ),
                Anchor(text="section 64(1) of the Companies Act"),
                Anchor(text="Increase in authorised capital"),
                Anchor(text="Consolidation or division of shares"),
                Anchor(text="Redemption of redeemable preference shares"),
                Anchor(text="Particulars of alteration"),
                Anchor(text="Original authorised capital"),
                Anchor(text="Revised authorised capital"),
                Anchor(text="Stamp duty paid"),
                Anchor(text="Date of the resolution"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_pas3": (
                    "SH-7 raises the authorised capital ceiling and names no shareholder; "
                    "PAS-3 issues shares within that ceiling and lists the allottees"
                ),
                "in_moa": (
                    "the capital clause SH-7 alters lives in the memorandum; the memorandum is "
                    "the constitutional document, SH-7 is one dated notice of a change to it"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. SH-7"),
                "Return of Allotment",
                "List of allottees",
            ],
            handling=(
                "Names no individual — an alteration of the authorised capital is a company "
                "fact, not a person fact — so this is one of the few documents in the DD pack "
                "with no pii at all. The value it carries is the authorised-capital ceiling as "
                "at a date, which is what makes a later allotment lawful or not."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _f(
                    "resolution_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of the resolution", "Date of resolution", "Dated"]},
                    validator="generic_date",
                ),
                _f(
                    "alteration_type",
                    "",
                    labels={
                        "en": [
                            "Particulars of alteration",
                            "Increase in authorised capital",
                            "Consolidation or division of shares",
                            "Type of alteration",
                        ]
                    },
                    locators=("mark", "label", "kv"),
                    notes="Doc-local; usually a tick box.",
                ),
                _f(
                    "authorised_capital_before",
                    "",
                    kind="number",
                    labels={"en": ["Original authorised capital", "Existing authorised capital"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local: entity.authorised_capital is reserved for the value that "
                    "should merge into the entity view, which is the revised figure.",
                ),
                _f(
                    "authorised_capital_after",
                    "entity.authorised_capital",
                    kind="number",
                    labels={"en": ["Revised authorised capital", "Authorised capital after"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "stamp_duty",
                    "",
                    kind="number",
                    labels={"en": ["Stamp duty paid", "Stamp duty"]},
                    validator="amount",
                    locators=("label", "kv"),
                    notes="Doc-local.",
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_chg1",
            label="MCA Form CHG-1 (registration of creation or modification of charge)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed with the Registrar of Companies, Ministry of Corporate "
            "Affairs, by the company or the charge holder",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # Header read verbatim off the MCA e-form, including the SARFAESI clause that
                # MCA folded into the title in the 2015 revision.
                Anchor(text="FORM NO. CHG-1", decisive=True),
                Anchor(text="Form CHG-1", decisive=True),
                Anchor(
                    text="Application for registration of creation, modification of charge",
                    decisive=True,
                ),
                # MCA's own e-form prints "Rules 2014" without the comma while every
                # secondary source prints "Rules, 2014". Anchor matching is on tokens, so one
                # declaration covers both spellings — declaring the second would be a
                # duplicate claim, not extra coverage.
                Anchor(text="Companies (Registration of Charges) Rules 2014", decisive=True),
                Anchor(text="other than those related to debentures"),
                Anchor(text="Securitization and Reconstruction of Financial Assets and "
                       "Enforcement of Securities Interest Act, 2002"),
                Anchor(text="SARFAESI"),
                Anchor(text="Asset Reconstruction Company"),
                Anchor(text="Creation of charge"),
                Anchor(text="Modification of charge"),
                Anchor(text="Date of the instrument creating or modifying the charge"),
                Anchor(text="Amount secured by the charge"),
                Anchor(text="Particulars of the property or asset(s) charged"),
                Anchor(text="Charge holder"),
                Anchor(text="Rate of interest"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_pas3": (
                    "CHG-1 registers security over the company's assets in favour of a lender; "
                    "PAS-3 issues securities in the company to an investor"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. CHG-1"),
                "Return of Allotment",
            ],
            handling=(
                "The encumbrance record — in a lending or acquisition DD this is the document "
                "the whole exercise turns on. A CHG-1 on file does NOT mean the charge is "
                "still live: satisfaction is filed separately on CHG-4, and an unsatisfied "
                "CHG-1 from 2011 is routine. Never report a charge as subsisting from CHG-1 "
                "alone; the index of charges is the authority."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _f(
                    "charge_holder",
                    "",
                    kind="name",
                    labels={
                        "en": ["Charge holder", "Name of the charge holder", "Name of the bank"]
                    },
                    notes="Doc-local. Usually a bank or an ARC; occasionally a natural person, "
                    "which is why the generic name validator applies.",
                ),
                _f(
                    "instrument_date",
                    "doc.issue_date",
                    kind="date",
                    required=True,
                    labels={
                        "en": [
                            "Date of the instrument creating or modifying the charge",
                            "Date of instrument",
                        ]
                    },
                    validator="generic_date",
                ),
                _f(
                    "amount_secured",
                    "",
                    kind="number",
                    required=True,
                    labels={"en": ["Amount secured by the charge", "Amount secured"]},
                    validator="amount",
                    locators=("label", "kv", "table"),
                    notes="Doc-local.",
                ),
                _f(
                    "charge_type",
                    "",
                    labels={
                        "en": [
                            "Creation of charge",
                            "Modification of charge",
                            "This form is for registration of",
                        ]
                    },
                    locators=("mark", "label", "kv"),
                    notes="Doc-local; a tick box on the face of the form.",
                ),
                _f(
                    "property_charged",
                    "",
                    labels={
                        "en": [
                            "Particulars of the property or asset(s) charged",
                            "Short particulars of the property",
                        ]
                    },
                    locators=("label", "kv", "table"),
                    notes="Doc-local free prose. Captured verbatim for the reviewer; not parsed "
                    "into an asset schedule.",
                ),
                _f(
                    "rate_of_interest",
                    "",
                    labels={"en": ["Rate of interest", "Interest rate"]},
                    notes="Doc-local.",
                ),
                _srn_field(),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_mca_inc20a",
            label="MCA Form INC-20A (declaration for commencement of business)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by a director with the Registrar of Companies, Ministry "
            "of Corporate Affairs",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                Anchor(text="FORM NO. INC-20A", decisive=True),
                Anchor(text="Form INC-20A", decisive=True),
                Anchor(text="Declaration for commencement of business", decisive=True),
                Anchor(text="Rule 23A of the Companies (Incorporation) Rules, 2014", decisive=True),
                Anchor(text="section 10A(1)(a) of the Companies Act"),
                Anchor(text="section 10A of the Companies Act"),
                Anchor(text="every subscriber to the memorandum has paid the value of the shares"),
                Anchor(text="Date of incorporation"),
                Anchor(text="Amount of paid-up share capital"),
                Anchor(text="Whether company is required to obtain registration or approval "
                       "from any sectoral regulator"),
                *_MCA_EFORM_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_certificate_incorporation": (
                    "the certificate is the registrar's attestation that the company exists; "
                    "INC-20A is the director's later declaration that subscription money was "
                    "actually received, without which the company may not trade"
                ),
            },
            negative_anchors=[
                *_other_eform_numbers("FORM NO. INC-20A"),
                "CERTIFICATE OF INCORPORATION",
            ],
            handling=(
                "Small and often overlooked, and it answers a question a certificate of "
                "incorporation cannot: whether the company is permitted to trade at all. A "
                "company incorporated with share capital that never filed INC-20A within 180 "
                "days cannot lawfully borrow or commence business — that is an onboarding "
                "blocker, not a paperwork gap."
            ),
            fields=[
                _cin_field(required=True),
                _company_name_field(),
                _f(
                    "incorporation_date",
                    "entity.incorporation_date",
                    kind="date",
                    labels={"en": ["Date of incorporation", "Date of Incorporation"]},
                    validator="generic_date",
                ),
                _f(
                    "declaration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date of declaration", "Dated", "Date"]},
                    validator="generic_date",
                ),
                _f(
                    "paid_up_capital",
                    "entity.paid_up_capital",
                    kind="number",
                    labels={
                        "en": [
                            "Amount of paid-up share capital",
                            "Paid up capital",
                            "Subscription money received",
                        ]
                    },
                    validator="amount",
                    locators=("label", "kv", "table"),
                ),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _f(
                    "declaring_director",
                    "ownership.director",
                    kind="name",
                    pii=True,
                    labels={"en": ["Name of the director", "Declared by", "Director"]},
                ),
                _din_field("declaring_director_din"),
                _srn_field(),
            ],
        ),
    ]
)
#: The SEBI regulator header and the LODR citation, on every listed-entity filing below.
#:
#: Decisive on none of them, and that is the point. ``SECURITIES AND EXCHANGE BOARD OF INDIA``
#: is an *issuer* name that heads six doctypes in this file — it proves the regulator, exactly
#: as ``INCOME TAX DEPARTMENT`` proves the department and not the form. ``Listing Obligations
#: and Disclosure Requirements`` is worse: a secretarial audit report enumerates every SEBI
#: regulation the company is subject to, so LODR, ICDR, SAST and PIT all appear on a document
#: that is none of them. What separates these filings is the *regulation number* SEBI printed
#: in the format's own title line, and that is what each spec declares decisive.
_SEBI_FURNITURE = (
    Anchor(text="SECURITIES AND EXCHANGE BOARD OF INDIA"),
    Anchor(text="भारतीय प्रतिभूति और विनिमय बोर्ड", lang="hi"),
    Anchor(text="Listing Obligations and Disclosure Requirements"),
    Anchor(text="Listing Regulations"),
    Anchor(text="BSE Limited"),
    Anchor(text="National Stock Exchange of India Limited"),
)


def _listed_entity_name_field() -> FieldSpec:
    return _f(
        "listed_entity_name", "entity.legal_name", required=True, labels=_L_LISTED_ENTITY
    )


def _scrip_code_field() -> FieldSpec:
    return _f(
        "scrip_code",
        "",
        kind="id",
        labels=_L_SCRIP,
        locators=("label", "kv", "table"),
        notes="BSE prints a numeric scrip code, NSE a symbol, and a filing addressed to both "
        "prints both — so no pattern is enforced and the field is multi-valued in "
        "practice even though the format gives it one box. Doc-local: ATTRIBUTE_KEYS has "
        "no ticker/exchange key.",
    )


def _quarter_field() -> FieldSpec:
    return _f(
        "period_end",
        "",
        kind="date",
        labels={
            "en": [
                "Quarter ending",
                "Quarter ended",
                "as on",
                "For the quarter ended",
                "report for Quarter ending",
            ]
        },
        validator="generic_date",
        notes="Doc-local reporting period; see the section note on attribute keys.",
    )


_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_shareholding_pattern",
            label="Shareholding Pattern (SEBI LODR Regulation 31)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the listed entity with the stock exchanges under SEBI "
            "(LODR) Regulations, 2015",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # Read verbatim off CDSL's own Reg-31 filing. Every one of the decisive
                # strings below is SEBI's prescribed format text, not the filer's prose: the
                # title line carries its own regulation number, the table titles are fixed by
                # the circular, and "calculated as per SCRR, 1957" cites the Securities
                # Contracts (Regulation) Rules — an Indian instrument no other regulator
                # names. Five unrelated issuers' filings carry the title line character for
                # character, which is what a prescribed format looks like from outside.
                Anchor(text="Shareholding Pattern under Regulation 31 of SEBI", decisive=True),
                Anchor(
                    text="Summary Statement holding of specified securities", decisive=True
                ),
                Anchor(
                    text="Share Holding Pattern Filed under: Reg. 31(1)(a)/Reg. 31(1)(b)/"
                    "Reg.31(1)(c)",
                    decisive=True,
                ),
                Anchor(text="calculated as per SCRR, 1957"),
                Anchor(text="format of holding of specified securities"),
                Anchor(text="Statement showing shareholding pattern of the Promoter and "
                       "Promoter Group"),
                Anchor(text="Statement showing shareholding pattern of the Public shareholder"),
                Anchor(text="Non Promoter - Non Public"),
                Anchor(text="Shares Underlying DRs"),
                Anchor(text="Shares Held By Employee Trust"),
                Anchor(text="Promoter & Promoter Group"),
                Anchor(text="Category of shareholder"),
                Anchor(text="No. of fully paid up equity shares held"),
                Anchor(text="Number of Shares pledged or otherwise encumbered"),
                Anchor(text="Number of Locked in shares"),
                Anchor(text="Number of equity shares held in dematerialised form"),
                Anchor(text="Whether the Listed Entity has issued any partly paid up shares"),
                Anchor(text="Name of Listed Entity"),
                *_SEBI_FURNITURE,
            ],
            id_patterns=[r"\bINE[A-Z0-9]{9}\b"],
            confusable_with={
                "in_mca_mgt7_annual_return": (
                    "both tabulate who holds the shares; the shareholding pattern is SEBI's "
                    "quarterly format for a listed entity and categorises holders as promoter "
                    "or public, the annual return is MCA's yearly form and lists members"
                ),
            },
            negative_anchors=[
                "FORM NO. MGT-7",
                "COMPLIANCE REPORT ON CORPORATE GOVERNANCE",
                "BUSINESS RESPONSIBILITY AND SUSTAINABILITY REPORT",
            ],
            handling=(
                "The promoter/public split and the pledge column are the two things this "
                "document exists to disclose, and both are material to a credit or "
                "acquisition view. Shareholder *names* appear only above the 1% disclosure "
                "threshold, so absence of a name is not absence of a holder. Individual "
                "shareholders are named alongside their PANs in the promoter table — that "
                "combination is personal data at its most identifying, and the PAN column is "
                "flagged pii accordingly."
            ),
            fields=[
                _listed_entity_name_field(),
                _scrip_code_field(),
                _quarter_field(),
                _f(
                    "isin",
                    "",
                    kind="id",
                    labels={"en": ["ISIN", "Class of Security", "ISIN Number"]},
                    pattern=r"\bINE[A-Z0-9]{9}\b",
                    locators=("regex", "label", "kv"),
                    notes="Indian ISINs begin INE (companies) or IN9/INF (other issuers). The "
                    "ISO 6166 check digit is a Luhn over the alphanumeric expansion and is "
                    "NOT enforced here: no validator for it is declared in "
                    "loader.VALIDATOR_CONTRACT, and inventing one in a pack would put an "
                    "unverified value behind a 'checksum_verified' label. Structure only. "
                    "Doc-local: ATTRIBUTE_KEYS has no securities-identifier key.",
                ),
                _f(
                    "promoter_holding_pct",
                    "ownership.share",
                    kind="number",
                    labels={
                        "en": [
                            "Promoter & Promoter Group",
                            "Total Shareholding Of Promoter And Promoter Group",
                            "Shareholding as a % of total no. of shares",
                        ]
                    },
                    locators=("table", "label", "kv"),
                    notes="Read from row (A) of Table I. The percentage is 'as per SCRR, 1957', "
                    "which excludes shares underlying depository receipts — do not compare "
                    "it against a percentage computed from a raw share count.",
                ),
                _f(
                    "public_holding_pct",
                    "",
                    kind="number",
                    labels={"en": ["Public", "Public shareholder"]},
                    locators=("table", "label", "kv"),
                    notes="Doc-local; row (B) of Table I.",
                ),
                _f(
                    "total_shares",
                    "",
                    kind="number",
                    labels={
                        "en": ["Total nos. shares held", "No. of fully paid up equity shares held"]
                    },
                    locators=("table", "label"),
                    notes="Doc-local: ATTRIBUTE_KEYS has no shares-outstanding key.",
                ),
                _f(
                    "shares_pledged",
                    "",
                    kind="number",
                    labels={
                        "en": [
                            "Number of Shares pledged or otherwise encumbered",
                            "No. of Shares pledged",
                        ]
                    },
                    locators=("table", "label"),
                    notes="Doc-local, and the single most load-bearing number on the page for "
                    "a credit view: promoter pledging is the standard early signal of "
                    "distress at the holding-company level.",
                ),
                _f(
                    "promoter_names",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={"en": ["Name of the Shareholders", "Promoter", "Name"]},
                    locators=("table", "label"),
                ),
                _f(
                    "shareholder_pan",
                    "id.pan",
                    kind="id",
                    multi=True,
                    pii=True,
                    labels=_L_PAN,
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("table", "regex", "label"),
                    notes="The format prints the PAN of every named shareholder. A name and a "
                    "PAN together identify a natural person exactly; mask at every boundary.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_corporate_governance_report",
            label="Compliance Report on Corporate Governance (SEBI LODR Regulation 27(2))",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed quarterly by the listed entity with the stock exchanges "
            "under SEBI (LODR) Regulations, 2015",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "COMPLIANCE REPORT ON CORPORATE GOVERNANCE" is printed at the head of the
                # SEBI format and is NOT decisive: a compliance report on corporate governance
                # is a title a listed-company regulator in Singapore, Johannesburg or Kuala
                # Lumpur could and does use. The line underneath it — SEBI's own regulation
                # number — is what one regulator controls, and that is the decisive claim.
                # Observed verbatim on Bharat Dynamics' Q2 FY22 filing:
                # "Regulation 27(2) of Securities and Exchange Board of India (Listing
                # Obligations and Disclosure Requirements) Regulations, 2015".
                Anchor(
                    text="Regulation 27(2) of Securities and Exchange Board of India",
                    decisive=True,
                ),
                Anchor(text="Regulation 27(2) of SEBI", decisive=True),
                Anchor(text="COMPLIANCE REPORT ON CORPORATE GOVERNANCE"),
                Anchor(text="Composition of Board of Directors"),
                Anchor(text="Composition of Committees"),
                Anchor(text="Stakeholders Relationship Committee"),
                Anchor(text="Nomination and Remuneration Committee"),
                Anchor(text="Risk Management Committee"),
                Anchor(text="Meeting of Board of Directors"),
                Anchor(text="Whether requirement of Quorum met"),
                Anchor(text="Maximum gap between any two consecutive"),
                Anchor(text="Whether regular chairperson appointed"),
                Anchor(text="Whether Permanent chairperson appointed"),
                Anchor(text="Whether Chairperson is related to managing director or CEO"),
                Anchor(text="Number of Independent Directors present"),
                Anchor(text="Category of directorship"),
                Anchor(text="Name of Listed Entity"),
                Anchor(text="Quarter ending"),
                Anchor(text="Affirmations"),
                *_SEBI_FURNITURE,
            ],
            id_patterns=[],
            confusable_with={
                "in_secretarial_audit_mr3": (
                    "both report on governance compliance; the CG report is the company's own "
                    "quarterly return in SEBI's Regulation 27(2) format, MR-3 is an annual "
                    "opinion signed by a practising company secretary under MCA's form number"
                ),
            },
            negative_anchors=[
                "FORM NO. MR-3",
                "Shareholding Pattern under Regulation 31 of SEBI",
                "BUSINESS RESPONSIBILITY AND SUSTAINABILITY REPORT",
            ],
            handling=(
                "The board composition table is the fastest route to a current, dated officer "
                "list for a listed entity, and every row of it is personal data: name, DIN, "
                "date of birth, date of appointment and date of cessation. Note the report is "
                "as at a quarter end, and that a 'No' in the chairperson or quorum columns is "
                "a declared non-compliance the reviewer should read rather than aggregate."
            ),
            fields=[
                _listed_entity_name_field(),
                _scrip_code_field(),
                _quarter_field(),
                _f(
                    "director_names",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    required=True,
                    pii=True,
                    labels={
                        "en": ["Name of the Director", "Name of Director", "Title", "Name"]
                    },
                    locators=("table", "label", "kv"),
                ),
                _din_field(multi=True),
                _f(
                    "director_category",
                    "",
                    multi=True,
                    labels={
                        "en": [
                            "Category",
                            "Category of directorship",
                            "Chairperson/Executive/Non-Executive/Independent/Nominee",
                        ]
                    },
                    locators=("table", "label"),
                    notes="Doc-local. Whether a board has the independent directors the "
                    "regulation requires is answered here and nowhere else in the pack.",
                ),
                _f(
                    "director_date_of_birth",
                    "identity.date_of_birth",
                    kind="date",
                    multi=True,
                    pii=True,
                    labels={"en": ["Date of Birth", "Date of birth"]},
                    validator="generic_date",
                    locators=("table", "label"),
                ),
                _f(
                    "board_meeting_dates",
                    "",
                    kind="date",
                    multi=True,
                    labels={
                        "en": [
                            "Date(s) of Meeting",
                            "Date(s) of Meeting (if any) in the relevant quarter",
                        ]
                    },
                    validator="generic_date",
                    locators=("table", "label"),
                    notes="Doc-local corporate-event dates.",
                ),
                _f(
                    "chairperson_appointed",
                    "",
                    kind="bool",
                    labels={
                        "en": [
                            "Whether regular chairperson appointed",
                            "Whether Permanent chairperson appointed",
                        ]
                    },
                    locators=("table", "label", "kv"),
                    notes="Doc-local. A declared 'No' is a compliance exception, not missing "
                    "data — surface it rather than treating it as an empty field.",
                ),
                _f(
                    "compliance_officer",
                    "ownership.authorized_signer",
                    kind="name",
                    pii=True,
                    labels={
                        "en": ["Compliance Officer", "Company Secretary", "Name of signatory"]
                    },
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_brsr",
            label="Business Responsibility and Sustainability Report (SEBI BRSR)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Prepared by the listed entity in SEBI's prescribed BRSR format "
            "under LODR Regulation 34(2)(f)",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # BRSR is a name SEBI coined in circular SEBI/HO/CFD/CMD-2/P/CIR/2021/562. It
                # is not the generic "sustainability report" — no other regulator prescribes a
                # format under that name, and the nine principles it reports against are the
                # MCA's National Guidelines on Responsible Business Conduct, an Indian
                # instrument. Both are therefore decisive. The generic ESG vocabulary below
                # (GRI, TCFD, Scope 1 and 2 emissions) is not: it belongs to every
                # sustainability report in the world.
                Anchor(
                    text="BUSINESS RESPONSIBILITY AND SUSTAINABILITY REPORT", decisive=True
                ),
                Anchor(
                    text="National Guidelines on Responsible Business Conduct", decisive=True
                ),
                Anchor(text="BRSR Core"),
                Anchor(text="NGRBC"),
                Anchor(text="SECTION A: GENERAL DISCLOSURES"),
                Anchor(text="SECTION B: MANAGEMENT AND PROCESS DISCLOSURES"),
                Anchor(text="SECTION C: PRINCIPLE WISE PERFORMANCE DISCLOSURE"),
                Anchor(text="Details of the listed entity"),
                Anchor(text="Paid-up Capital"),
                Anchor(text="Turnover (in Rs.)"),
                Anchor(text="Name of the National Industrial Classification"),
                Anchor(text="Principle 1"),
                Anchor(text="Businesses should conduct and govern themselves with integrity"),
                Anchor(text="Reasonable Assurance"),
                Anchor(text="Global Reporting Initiative"),
                Anchor(text="Task Force on Climate-related Financial Disclosures"),
                *_SEBI_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={},
            negative_anchors=[
                "COMPLIANCE REPORT ON CORPORATE GOVERNANCE",
                "Shareholding Pattern under Regulation 31 of SEBI",
                "FORM NO. MR-3",
            ],
            handling=(
                "Almost always an annexure inside an annual report rather than a standalone "
                "PDF, so expect it as a page-type on a merged document. It carries employee "
                "headcount broken down by gender and disability, complaint counts under the "
                "POSH Act, and grievance data — aggregate figures, but figures about people, "
                "and small denominators in a single-location entity can re-identify. Do not "
                "treat BRSR numbers as audited: only the BRSR Core subset carries assurance, "
                "and the assurance provider is named separately."
            ),
            fields=[
                _company_name_field(),
                _cin_field(),
                _financial_year_field(),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _scrip_code_field(),
                _f(
                    "turnover",
                    "",
                    kind="number",
                    labels={"en": ["Turnover (in Rs.)", "Turnover", "Revenue"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local; see the note on AOC-4's turnover field.",
                ),
                _f(
                    "paid_up_capital",
                    "entity.paid_up_capital",
                    kind="number",
                    labels={"en": ["Paid-up Capital", "Paid up capital"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "assurance_provider",
                    "",
                    kind="name",
                    labels={
                        "en": [
                            "Name of the assurance provider",
                            "Assurance provider",
                            "Type of assurance obtained",
                        ]
                    },
                    notes="Doc-local. Present only for BRSR Core; its absence is meaningful "
                    "and should be reported as 'not assured', never as 'not found'.",
                ),
                _f(
                    "contact_email",
                    "identity.email",
                    labels={"en": ["E-mail", "Email", "Contact details"]},
                    pattern=r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
                    locators=("label", "kv", "regex"),
                    notes="A named officer's work address as often as a functional mailbox; "
                    "the field cannot tell them apart, so it inherits identity.email.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_offer_document",
            label="Offer Document — DRHP / RHP / Prospectus (SEBI ICDR)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the issuer and the book running lead managers with "
            "SEBI, the stock exchanges and the Registrar of Companies",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # ONE doctype for all three stages, deliberately. A DRHP becomes an RHP
                # becomes a Prospectus by amendment: the same document, the same
                # thousand-page structure, differing in the word "DRAFT" and in whether the
                # price band has been filled in. Modelling them as three doctypes would put
                # three decisive claimants on one cover page — "RED HERRING PROSPECTUS" is a
                # token subsequence of "DRAFT RED HERRING PROSPECTUS" and would match inside
                # it — and the cascade would correctly refuse to conclude anything. The stage
                # is extracted as a field instead, which is where a three-way distinction
                # belongs.
                #
                # The decisive strings are SEBI ICDR Schedule VI cover-page text, verified on
                # three unrelated issuers' filings. "PROSPECTUS" and "RED HERRING PROSPECTUS"
                # are not among them: those are document-class names used from Mumbai to New
                # York, and a US registration statement claiming the same string would make
                # this doctype a cross-jurisdiction hazard.
                Anchor(text="Please read Section 32 of the Companies Act, 2013", decisive=True),
                Anchor(
                    text="Please read Section 26 and 32 of the Companies Act, 2013",
                    decisive=True,
                ),
                Anchor(
                    text="This Draft Red Herring Prospectus will be updated upon filing with "
                    "the RoC",
                    decisive=True,
                ),
                Anchor(text="DRAFT RED HERRING PROSPECTUS"),
                Anchor(text="RED HERRING PROSPECTUS"),
                Anchor(text="BOOK RUNNING LEAD MANAGER"),
                Anchor(text="REGISTRAR TO THE OFFER"),
                Anchor(text="REGISTRAR TO THE ISSUE"),
                Anchor(text="RISKS IN RELATION TO THE FIRST ISSUE"),
                Anchor(text="RISKS IN RELATION TO THE FIRST OFFER"),
                Anchor(text="100% Book Built Offer"),
                Anchor(text="Book Built Issue"),
                Anchor(text="Price Band"),
                Anchor(text="Issue of Capital and Disclosure Requirements"),
                Anchor(text="GENERAL RISKS"),
                Anchor(text="OFFER FOR SALE"),
                Anchor(text="Anchor Investor"),
                Anchor(text="Qualified Institutional Buyers"),
                Anchor(text="Basis of Allotment"),
                Anchor(text="Objects of the Offer"),
                Anchor(text="Promoters of our Company"),
                *_SEBI_FURNITURE,
            ],
            id_patterns=[
                r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b",
                r"\bINE[A-Z0-9]{9}\b",
            ],
            confusable_with={},
            negative_anchors=[
                "FORM NO. MGT-7",
                "COMPLIANCE REPORT ON CORPORATE GOVERNANCE",
                "Summary Statement holding of specified securities",
            ],
            handling=(
                "The most information-dense document in the pack and the one most likely to "
                "arrive as several hundred pages. Two cautions for anything downstream. "
                "First, a DRHP is a DRAFT: nothing in it — price, size, dates, even the "
                "decision to proceed — is binding, and the promoter and litigation "
                "disclosures are as at the draft date. Second, the document reproduces "
                "directors' and promoters' addresses, dates of birth, PANs, passport numbers "
                "and DINs in the capital-structure and management sections, so extraction "
                "over an offer document touches more personal data than any identity document "
                "in this registry. Extract the stage first and let the reviewer decide."
            ),
            fields=[
                _company_name_field(),
                _cin_field(),
                _f(
                    "offer_document_stage",
                    "",
                    labels={
                        "en": [
                            "DRAFT RED HERRING PROSPECTUS",
                            "RED HERRING PROSPECTUS",
                            "PROSPECTUS",
                        ]
                    },
                    locators=("label", "kv"),
                    notes="Doc-local, and the field the whole doctype turns on: draft / red "
                    "herring / final. Read from the cover title, which is the only place "
                    "the stage is stated unambiguously — the presence of a price band is "
                    "corroboration, not proof.",
                ),
                _f(
                    "document_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Dated", "Date", "Dated:"]},
                    validator="generic_date",
                ),
                _f(
                    "registered_office",
                    "entity.registered_office",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _f(
                    "book_running_lead_managers",
                    "",
                    multi=True,
                    labels={
                        "en": [
                            "BOOK RUNNING LEAD MANAGER",
                            "BOOK RUNNING LEAD MANAGERS",
                            "Lead Manager",
                        ]
                    },
                    locators=("label", "table", "kv"),
                    notes="Doc-local. Institutions, not individuals.",
                ),
                _f(
                    "registrar_to_offer",
                    "",
                    labels={
                        "en": ["REGISTRAR TO THE OFFER", "REGISTRAR TO THE ISSUE", "Registrar"]
                    },
                    locators=("label", "kv"),
                    notes="Doc-local.",
                ),
                _f(
                    "promoters",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={
                        "en": ["Promoters of our Company", "Our Promoters", "Promoter"]
                    },
                    locators=("label", "table", "kv"),
                ),
                _f(
                    "isin",
                    "",
                    kind="id",
                    labels={"en": ["ISIN", "ISIN Number"]},
                    pattern=r"\bINE[A-Z0-9]{9}\b",
                    locators=("regex", "label", "kv"),
                    notes="Doc-local; see the ISIN note on in_shareholding_pattern. Present "
                    "only once the securities have been admitted, so absent from most DRHPs.",
                ),
                _f(
                    "face_value",
                    "",
                    kind="number",
                    labels={"en": ["The face value of the Equity Shares is", "Face Value"]},
                    validator="amount",
                    locators=("label", "kv"),
                    notes="Doc-local.",
                ),
                _f(
                    "price_band",
                    "",
                    labels={"en": ["Price Band", "Offer Price", "Issue Price"]},
                    locators=("label", "kv"),
                    notes="Doc-local. Blank or '[•]' on a DRHP by construction — an empty "
                    "value here is evidence about the stage, not a extraction failure.",
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_statutory_auditor_report",
            label="Statutory Auditor's Report (including the CARO annexure)",
            country="IN",
            category=Category.financial,
            issuing_authority="Signed by the company's statutory auditor, a firm of Chartered "
            "Accountants registered with the ICAI",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "INDEPENDENT AUDITOR'S REPORT", "Basis for Opinion" and "Key Audit Matters"
                # are ISA/IAASB headings printed on an audit report in every country that
                # adopted the international standards. Not decisive, any of them. What is
                # Indian and issuer-controlled is the Order and the Rules the report is
                # required to cite: CARO is an MCA order made under section 143(11), and
                # Rule 11 of the Companies (Audit and Auditors) Rules is the source of the
                # "Other Legal and Regulatory Requirements" paragraph.
                Anchor(text="Companies (Auditor's Report) Order, 2020", decisive=True),
                Anchor(text="Companies (Auditor's Report) Order, 2016", decisive=True),
                Anchor(text="Companies (Audit and Auditors) Rules, 2014", decisive=True),
                Anchor(text="INDEPENDENT AUDITOR'S REPORT"),
                Anchor(text="Report on the Audit of the Standalone Financial Statements"),
                Anchor(text="Report on the Audit of the Consolidated Financial Statements"),
                Anchor(text="Report on Other Legal and Regulatory Requirements"),
                Anchor(text="Basis for Opinion"),
                Anchor(text="Key Audit Matters"),
                Anchor(text="Standards on Auditing specified under section 143(10)"),
                Anchor(text="Institute of Chartered Accountants of India"),
                Anchor(text="Chartered Accountants"),
                Anchor(text="Firm Registration No"),
                Anchor(text="UDIN"),
                Anchor(text="Membership No"),
                Anchor(
                    text="internal financial controls with reference to financial statements"
                ),
                Anchor(text="Annexure A to the Independent Auditor's Report"),
                Anchor(text="Property, Plant and Equipment"),
                Anchor(text="in our opinion and to the best of our information"),
                Anchor(text="true and fair view"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_aoc4_financial_statements": (
                    "the auditor's report is signed by the auditor and cites CARO; AOC-4 is "
                    "the company's own MCA e-form and prints the MCA form header"
                ),
                "in_secretarial_audit_mr3": (
                    "both are professional opinions annexed to the same annual report; the "
                    "auditor's report is signed by Chartered Accountants on the financial "
                    "statements, MR-3 is signed by a Company Secretary on statutory compliance"
                ),
            },
            negative_anchors=[
                "FORM NO. MR-3",
                "SECRETARIAL AUDIT REPORT",
                "FORM NO. AOC-4",
            ],
            handling=(
                "The CARO annexure is deliberately part of this doctype rather than a doctype "
                "of its own: it is an annexure to the report, printed immediately after it, "
                "under a heading that refers back to it. Splitting them would guarantee two "
                "doctypes competing on one continuous document. The report's value in DD is "
                "in its exceptions — a modified opinion, an emphasis of matter, a CARO clause "
                "answered adversely — so a reviewer needs the opinion paragraph verbatim, not "
                "a boolean. Extract, do not summarise."
            ),
            fields=[
                _company_name_field(),
                _cin_field(),
                _financial_year_field(),
                _f(
                    "auditor_firm",
                    "",
                    labels={
                        "en": [
                            "Chartered Accountants",
                            "For and on behalf of",
                            "Name of the audit firm",
                        ]
                    },
                    notes="Doc-local: ATTRIBUTE_KEYS has no auditor key. The firm, not the "
                    "signing partner — the partner is captured separately below.",
                ),
                _f(
                    "firm_registration_number",
                    "doc.registration_number",
                    kind="id",
                    labels={
                        "en": ["Firm Registration No", "FRN", "Firm's Registration Number"]
                    },
                    locators=("label", "kv"),
                    notes="ICAI firm registration number, printed as digits plus a regional "
                    "suffix letter (e.g. 301003E). Structure varies; no pattern enforced.",
                ),
                _f(
                    "signing_partner",
                    "ownership.authorized_signer",
                    kind="name",
                    pii=True,
                    labels={"en": ["Partner", "Membership No", "Signed by"]},
                    notes="A natural person, personally liable for the opinion. pii.",
                ),
                _f(
                    "udin",
                    "",
                    kind="id",
                    labels={"en": ["UDIN", "Unique Document Identification Number"]},
                    pattern=r"\b\d{18}\b",
                    locators=("label", "regex", "kv"),
                    notes="18 digits: 6-digit ICAI membership number, 2-digit year, 2-digit "
                    "month, 8-digit serial. Verifiable only against the ICAI portal, which "
                    "this service must not call — the egress invariant applies to "
                    "verification as much as to classification. Structure only. Doc-local.",
                ),
                _f(
                    "opinion_type",
                    "",
                    labels={
                        "en": [
                            "Opinion",
                            "Qualified Opinion",
                            "Adverse Opinion",
                            "Disclaimer of Opinion",
                            "Basis for Opinion",
                        ]
                    },
                    locators=("label", "kv"),
                    notes="Doc-local, and the field a reviewer reads first. An unmodified "
                    "opinion prints the bare heading 'Opinion'; anything else prints its "
                    "own qualifier, so the presence of a qualifier IS the finding.",
                ),
                _f(
                    "report_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date", "Dated", "Place and Date"]},
                    validator="generic_date",
                ),
                _f(
                    "place_of_signature",
                    "doc.place_of_issue",
                    labels={"en": ["Place", "Place of signature"]},
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_secretarial_audit_mr3",
            label="Secretarial Audit Report (Form MR-3)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Signed by a Company Secretary in practice, in MCA's Form MR-3 "
            "under section 204(1) of the Companies Act, 2013",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # Header verified verbatim on IDBI Bank's FY2020-21 report: "FORM NO. MR-3 /
                # SECRETARIAL AUDIT REPORT / FOR THE FINANCIAL YEAR ENDED 31ST MARCH 2021 /
                # [Pursuant to Section 204(1) of the Companies Act, 2013 and Rule No. 9 of the
                # Companies (Appointment and Remuneration of Managerial Personnel) Rules,
                # 2014]". "SECRETARIAL AUDIT REPORT" is left non-decisive even though
                # secretarial audit is an Indian institution with no direct analogue: the form
                # number and the rule citation already carry the doctype, and a title made of
                # three ordinary English words does not need to be the thing that proves it.
                Anchor(text="FORM NO. MR-3", decisive=True),
                Anchor(text="Form MR-3", decisive=True),
                Anchor(
                    text="Companies (Appointment and Remuneration of Managerial Personnel) "
                    "Rules, 2014",
                    decisive=True,
                ),
                Anchor(text="SECRETARIAL AUDIT REPORT"),
                Anchor(text="Section 204(1) of the Companies Act"),
                Anchor(text="Company Secretary in Practice"),
                Anchor(text="Institute of Company Secretaries of India"),
                Anchor(text="Certificate of Practice"),
                Anchor(text="CP No"),
                Anchor(text="We have conducted the Secretarial Audit of the compliance of "
                       "applicable statutory provisions"),
                Anchor(text="adherence to good corporate practices"),
                Anchor(text="books, papers, minute books, forms and returns filed"),
                Anchor(text="The Depositories Act, 1996"),
                Anchor(text="Secretarial Standards"),
                Anchor(text="Substantial Acquisition of Shares and Takeovers"),
                Anchor(text="Prohibition of Insider Trading"),
                Anchor(text="Issue of Capital and Disclosure Requirements"),
                Anchor(text="Listing Obligations and Disclosure Requirements"),
                Anchor(text="UDIN"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_statutory_auditor_report": (
                    "both are professional opinions annexed to the same annual report; MR-3 is "
                    "signed by a Company Secretary on statutory compliance and prints an MCA "
                    "form number, the auditor's report is signed by Chartered Accountants on "
                    "the financial statements and cites CARO"
                ),
                "in_corporate_governance_report": (
                    "MR-3 is an annual opinion by an outside professional under MCA's form "
                    "number; the CG report is the company's own quarterly return in SEBI's "
                    "Regulation 27(2) format"
                ),
            },
            negative_anchors=[
                "Companies (Auditor's Report) Order, 2020",
                "INDEPENDENT AUDITOR'S REPORT",
                "COMPLIANCE REPORT ON CORPORATE GOVERNANCE",
            ],
            handling=(
                "MR-3 recites, by name, every SEBI regulation and every other statute the "
                "company is subject to. That recital is why this doctype exists and also why "
                "the SEBI regulation names are non-decisive throughout this section: a "
                "document that lists ICDR, LODR, SAST and PIT is not an ICDR, LODR, SAST or "
                "PIT filing. As with the auditor's report, the value is in the qualifications "
                "— an MR-3 with observations is a finding, and the observation text must reach "
                "the reviewer intact."
            ),
            fields=[
                _company_name_field(),
                _cin_field(),
                _financial_year_field(required=True),
                _f(
                    "secretarial_auditor",
                    "",
                    kind="name",
                    pii=True,
                    labels={
                        "en": [
                            "Company Secretary in Practice",
                            "Practising Company Secretary",
                            "Name of the Company Secretary",
                        ]
                    },
                    notes="Doc-local. A practising company secretary signs in their own name, "
                    "so this is a natural person even when a firm name also appears.",
                ),
                _f(
                    "membership_number",
                    "doc.registration_number",
                    kind="id",
                    labels={"en": ["FCS No", "ACS No", "Membership No", "CP No"]},
                    locators=("label", "kv"),
                    notes="ICSI membership (FCS/ACS) and certificate-of-practice numbers. "
                    "Digits, length varies by vintage; no pattern enforced.",
                ),
                _f(
                    "udin",
                    "",
                    kind="id",
                    labels={"en": ["UDIN", "Unique Document Identification Number"]},
                    locators=("label", "kv"),
                    notes="ICSI's UDIN uses a different scheme from ICAI's 18-digit number, so "
                    "no pattern is shared with in_statutory_auditor_report. Doc-local.",
                ),
                _f(
                    "observations",
                    "",
                    labels={
                        "en": [
                            "Observations",
                            "Qualification",
                            "We further report that",
                            "subject to the following",
                        ]
                    },
                    locators=("label", "kv"),
                    notes="Doc-local free prose, captured verbatim. Not parsed into findings — "
                    "a summarised compliance qualification is a compliance qualification "
                    "that got lost.",
                ),
                _f(
                    "report_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["Date", "Dated", "Place and Date"]},
                    validator="generic_date",
                ),
                _f(
                    "place_of_signature",
                    "doc.place_of_issue",
                    labels={"en": ["Place", "Place of signature"]},
                ),
            ],
        ),
    ]
)
_SPECS.extend(
    [
        DocTypeSpec(
            doctype_id="in_fema_fcgpr",
            label="RBI Form FC-GPR (reporting of foreign investment in an Indian company)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Filed by the Indian company with the Reserve Bank of India "
            "through its AD Category-I bank on the FIRMS portal",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "FC-GPR" is an RBI-coined form code and expands to a phrase — "Foreign
                # Currency-Gross Provisional Return" — that exists nowhere else. Both are
                # decisive. The FEMA instruments are supporting rather than decisive because
                # a secretarial audit report and a statutory auditor's report both recite
                # FEMA by name.
                Anchor(text="Form FC-GPR", decisive=True),
                # RBI prints the expansion with a hyphen, secondary sources with a space.
                # Anchor matching splits on both, so one declaration covers the pair.
                Anchor(text="Foreign Currency-Gross Provisional Return", decisive=True),
                Anchor(text="Foreign Exchange Management Act, 1999"),
                Anchor(text="Foreign Exchange Management (Non-debt Instruments) Rules"),
                Anchor(text="FIRMS"),
                Anchor(text="Reserve Bank of India"),
                Anchor(text="भारतीय रिज़र्व बैंक", lang="hi"),
                Anchor(text="Authorised Dealer Category-I bank"),
                Anchor(text="AD Reference Number"),
                Anchor(text="Unique Identification Number"),
                Anchor(text="Capital instruments"),
                Anchor(text="Date of issue of capital instruments"),
                Anchor(text="Total amount of inflow"),
                Anchor(text="Foreign Inward Remittance Certificate"),
                Anchor(text="Fair value of the capital instruments"),
                Anchor(text="Pricing guidelines"),
                Anchor(text="Nature of the investing entity"),
            ],
            id_patterns=[r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b"],
            confusable_with={
                "in_mca_pas3": (
                    "FC-GPR reports the allotment to the RBI under FEMA because the allottee "
                    "is resident outside India; PAS-3 reports the same allotment to the "
                    "Registrar under the Companies Act and prints an MCA form header"
                ),
            },
            negative_anchors=[
                "FORM NO. PAS-3",
                "Return of Allotment",
                "Registrar of Companies",
            ],
            handling=(
                "The document that answers 'is this company foreign-owned, by whom, and was "
                "it reported on time'. The 30-day filing deadline has no extension mechanism, "
                "so a late FC-GPR is a FEMA contravention requiring compounding — a date "
                "discrepancy between the allotment date and the filing date is a finding, not "
                "a data-quality issue. Names the foreign investor and, where that investor is "
                "a natural person, their country of residence: personal data."
            ),
            fields=[
                _company_name_field(),
                _cin_field(),
                _f(
                    "issue_date",
                    "doc.issue_date",
                    kind="date",
                    required=True,
                    labels={
                        "en": ["Date of issue of capital instruments", "Date of allotment"]
                    },
                    validator="generic_date",
                ),
                _f(
                    "investor_name",
                    "ownership.beneficial_owner",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={
                        "en": ["Name of the investor", "Investor", "Name of the investing entity"]
                    },
                    locators=("table", "label", "kv"),
                ),
                _f(
                    "investor_country",
                    "",
                    multi=True,
                    labels={"en": ["Country of the investor", "Country", "Country of residence"]},
                    locators=("table", "label", "kv"),
                    notes="Doc-local. ATTRIBUTE_KEYS has identity.nationality for a natural "
                    "person; an investing entity's country of incorporation is a different "
                    "fact and must not merge into it.",
                ),
                _f(
                    "amount_of_inflow",
                    "",
                    kind="number",
                    labels={"en": ["Total amount of inflow", "Amount of consideration"]},
                    validator="amount",
                    locators=("table", "label", "kv"),
                    notes="Doc-local.",
                ),
                _f(
                    "ad_bank",
                    "account.bank_name",
                    labels={
                        "en": [
                            "Authorised Dealer Category-I bank",
                            "AD Bank",
                            "Name of the AD bank",
                        ]
                    },
                ),
                _f(
                    "uin",
                    "doc.reference_number",
                    kind="id",
                    labels={
                        "en": ["Unique Identification Number", "UIN", "AD Reference Number"]
                    },
                    locators=("label", "kv"),
                    notes="RBI allots the UIN on acknowledgement. Format has changed with each "
                    "generation of the reporting portal; no pattern enforced.",
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_iec_certificate",
            label="Importer-Exporter Code (IEC) certificate",
            country="IN",
            category=Category.corporate,
            issuing_authority="Directorate General of Foreign Trade, Ministry of Commerce and "
            "Industry",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "Importer-Exporter Code" is the term of art of the Indian Foreign Trade
                # Policy; the equivalent identifiers elsewhere are called something else
                # entirely (EORI in the EU, an importer number at US CBP), so the phrase is
                # controlled by one issuer. "DIRECTORATE GENERAL OF FOREIGN TRADE" is NOT
                # decisive: DGFT issues advance authorisations, RoDTEP scrips and RCMCs under
                # the same header, so it proves the issuer and not the document.
                Anchor(text="Importer-Exporter Code", decisive=True),
                Anchor(text="Importer Exporter Code", decisive=True),
                Anchor(text="आयातक-निर्यातक कोड", lang="hi", decisive=True),
                Anchor(text="DIRECTORATE GENERAL OF FOREIGN TRADE"),
                Anchor(text="विदेश व्यापार महानिदेशालय", lang="hi"),
                Anchor(text="Ministry of Commerce and Industry"),
                Anchor(text="Foreign Trade (Development and Regulation) Act, 1992"),
                Anchor(text="Foreign Trade Policy"),
                Anchor(text="IEC Details"),
                Anchor(text="Nature of Concern/Firm"),
                Anchor(text="Date of Issue"),
                Anchor(text="Branch Code"),
                Anchor(text="dgft.gov.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\b[A-Z]{5}\d{4}[A-Z]\b"],
            confusable_with={
                "in_gst_certificate": (
                    "both are PAN-derived business registrations printed as a one-page "
                    "certificate; the IEC is DGFT's foreign-trade code and equals the PAN "
                    "itself, the GST certificate carries a 15-character GSTIN with a state "
                    "code prefix and a check character"
                ),
            },
            negative_anchors=[
                "Goods and Services Tax Identification Number",
                "UDYAM REGISTRATION CERTIFICATE",
                "CERTIFICATE OF INCORPORATION",
            ],
            handling=(
                "Since the 2017 alignment the IEC *is* the entity's PAN — same ten "
                "characters. That is a trap for anything downstream: extracting the IEC and "
                "the PAN as two independent identifiers and finding they agree is not "
                "corroboration, it is the same fact counted twice. The IEC is also the one "
                "document here that answers whether the entity may lawfully import or export "
                "at all, and it must be revalidated annually or it is deactivated — an IEC "
                "with no current-year update is not evidence of an active trader."
            ),
            fields=[
                _f(
                    "iec",
                    "id.pan",
                    kind="id",
                    required=True,
                    pii=True,
                    labels={"en": ["IEC", "IEC Number", "Importer-Exporter Code", "IEC Code"]},
                    pattern=r"\b[A-Z]{5}\d{4}[A-Z]\b",
                    validator="pan",
                    locators=("label", "kv", "regex"),
                    notes="Ten characters, and since 2017 identical to the entity's PAN — "
                    "hence id.pan and the pan validator rather than an IEC-specific key. "
                    "Older IECs issued before the alignment are ten digits and will fail "
                    "this pattern; that is a legacy document, not a bad read.",
                ),
                _f("firm_name", "entity.legal_name", required=True, labels=_L_ENTITY),
                _f(
                    "nature_of_concern",
                    "entity.constitution",
                    labels={
                        "en": ["Nature of Concern/Firm", "Nature of Concern", "Type of Firm"]
                    },
                ),
                _f(
                    "registered_address",
                    "address.registered",
                    kind="address",
                    labels=_L_REG_OFFICE,
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _issue_date_field(),
                _f(
                    "branch_code",
                    "",
                    labels={"en": ["Branch Code", "Branch"]},
                    notes="Doc-local. A multi-branch IEC lists each site with its own code.",
                ),
                _f(
                    "directors",
                    "ownership.director",
                    kind="name",
                    multi=True,
                    pii=True,
                    labels={
                        "en": [
                            "Details of Proprietor/Partner/Director/Karta/Managing Trustee",
                            "Proprietor",
                            "Partner",
                            "Director",
                        ]
                    },
                    locators=("table", "label", "kv"),
                ),
            ],
        ),
        DocTypeSpec(
            doctype_id="in_udyam_certificate",
            label="Udyam Registration Certificate (MSME)",
            country="IN",
            category=Category.corporate,
            issuing_authority="Ministry of Micro, Small and Medium Enterprises, Government of "
            "India (Udyam Registration portal)",
            applies_to="corporate",
            officially_valid=False,
            anchors=[
                # "Udyam" is a name the MSME Ministry coined in its 26 June 2020 notification
                # for a scheme that exists only in India, and the certificate and the number
                # both carry it. Decisive on the strength of the coinage, not of the English
                # words around it.
                Anchor(text="UDYAM REGISTRATION CERTIFICATE", decisive=True),
                Anchor(text="Udyam Registration Number", decisive=True),
                Anchor(text="उद्यम रजिस्ट्रेशन प्रमाणपत्र", lang="hi", decisive=True),
                Anchor(text="MINISTRY OF MICRO, SMALL AND MEDIUM ENTERPRISES"),
                Anchor(text="सूक्ष्म, लघु और मध्यम उद्यम मंत्रालय", lang="hi"),
                Anchor(text="Micro, Small and Medium Enterprises Development Act, 2006"),
                Anchor(text="TYPE OF ENTERPRISE"),
                Anchor(text="MAJOR ACTIVITY"),
                Anchor(text="SOCIAL CATEGORY OF ENTREPRENEUR"),
                Anchor(text="NAME OF ENTERPRISE"),
                Anchor(text="DATE OF UDYAM REGISTRATION"),
                Anchor(text="DATE OF INCORPORATION / REGISTRATION OF ENTERPRISE"),
                Anchor(text="NATIONAL INDUSTRY CLASSIFICATION CODE"),
                Anchor(text="udyamregistration.gov.in"),
                *_GOI_FURNITURE,
            ],
            id_patterns=[r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b"],
            confusable_with={
                "in_iec_certificate": (
                    "both are one-page Government of India business registrations; Udyam is "
                    "the MSME classification and carries a UDYAM-XX-00-0000000 number, the "
                    "IEC is DGFT's foreign-trade code and equals the entity's PAN"
                ),
            },
            negative_anchors=[
                "Importer-Exporter Code",
                "Goods and Services Tax Identification Number",
                "CERTIFICATE OF INCORPORATION",
            ],
            handling=(
                "The enterprise classification — micro, small or medium — is the point of the "
                "document, and it is self-declared against turnover and investment thresholds "
                "the enterprise reports itself. It carries real legal consequences (MSME "
                "payment terms, priority-sector lending), so it is worth extracting, but it "
                "is not third-party-verified data and must not be presented as if it were. "
                "Note the certificate names an entrepreneur's social category, which is "
                "sensitive personal data under Indian law even on a business registration."
            ),
            fields=[
                _f(
                    "udyam_number",
                    "doc.registration_number",
                    kind="id",
                    required=True,
                    labels={
                        "en": [
                            "Udyam Registration Number",
                            "UDYAM REGISTRATION NUMBER",
                            "URN",
                        ]
                    },
                    pattern=r"\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b",
                    locators=("regex", "label", "kv"),
                    notes="UDYAM-<2-letter state>-<2-digit district>-<7-digit serial>. "
                    "Structure only; there is no published check digit.",
                ),
                _f(
                    "enterprise_name",
                    "entity.legal_name",
                    required=True,
                    labels={
                        "en": ["NAME OF ENTERPRISE", "Name of Enterprise"],
                        "hi": ["उद्यम का नाम"],
                    },
                ),
                _f(
                    "enterprise_type",
                    "entity.constitution",
                    labels={
                        "en": ["TYPE OF ENTERPRISE", "Type of Enterprise", "Micro", "Small"]
                    },
                    notes="Micro / Small / Medium. Self-declared against the investment and "
                    "turnover thresholds of the MSMED Act as amended in 2020.",
                ),
                _f(
                    "major_activity",
                    "",
                    labels={"en": ["MAJOR ACTIVITY", "Major Activity"]},
                    notes="Doc-local. Manufacturing or Services.",
                ),
                _f(
                    "social_category",
                    "identity.category",
                    pii=True,
                    labels={
                        "en": ["SOCIAL CATEGORY OF ENTREPRENEUR", "Social Category"]
                    },
                    notes="SC / ST / OBC / General. Sensitive personal data about a named "
                    "individual, printed on a business registration — mask it by default "
                    "and never use it as a business attribute.",
                ),
                _f(
                    "registered_address",
                    "address.registered",
                    kind="address",
                    labels={
                        "en": [
                            "OFFICIAL ADDRESS OF ENTERPRISE",
                            "Address",
                            "Location of Plant",
                        ]
                    },
                    locators=("kv", "label", "regex"),
                ),
                _pincode_field(),
                _f(
                    "registration_date",
                    "doc.issue_date",
                    kind="date",
                    labels={"en": ["DATE OF UDYAM REGISTRATION", "Date of Udyam Registration"]},
                    validator="generic_date",
                ),
                _f(
                    "incorporation_date",
                    "entity.incorporation_date",
                    kind="date",
                    labels={
                        "en": [
                            "DATE OF INCORPORATION / REGISTRATION OF ENTERPRISE",
                            "Date of Incorporation",
                        ]
                    },
                    validator="generic_date",
                ),
                _f(
                    "nic_code",
                    "",
                    labels={
                        "en": ["NATIONAL INDUSTRY CLASSIFICATION CODE", "NIC Code", "NIC 2 Digit"]
                    },
                    locators=("table", "label", "kv"),
                    notes="Doc-local.",
                ),
            ],
        ),
    ]
)
# <<SPECS-INSERTION-POINT>>


register_all(_SPECS)


def _assert_ovd_flags() -> None:
    """Fail at import if the officially-valid flags drift from :data:`IN_OVD_DOCTYPES`.

    The OVD set is a regulatory fact, not a tag: flagging a non-OVD as officially valid
    would let the service tell a business unit that a ration card satisfies KYC. Checking it
    here means the drift is caught at container start, not in an audit.

    Raises:
        RegistryError: If the flagged set and :data:`IN_OVD_DOCTYPES` disagree.
    """
    flagged = {s.doctype_id for s in _SPECS if s.officially_valid}
    if flagged != IN_OVD_DOCTYPES:
        raise RegistryError(
            "India pack OVD drift: officially_valid is set on "
            f"{sorted(flagged)} but IN_OVD_DOCTYPES declares {sorted(IN_OVD_DOCTYPES)}"
        )


_assert_ovd_flags()

#: Every India doctype, in declaration order.
SPECS: tuple[DocTypeSpec, ...] = tuple(_SPECS)

# ---------------------------------------------------------------------------
# Public aliases for the field builders and label vocabularies.
#
# The cross-country pack reuses these rather than forking its own copies. One definition
# per concept is what stops an Aadhaar ``name`` field and a generic ``name`` field drifting
# into two different shapes with two different attribute keys — which is exactly how a
# merge view starts showing the same person twice.
# ---------------------------------------------------------------------------
build_field = _f
name_field = _name_field
father_field = _father_field
dob_field = _dob_field
sex_field = _sex_field
address_field = _address_field
pincode_field = _pincode_field
issue_date_field = _issue_date_field
expiry_field = _expiry_field

LABELS_NAME = _L_NAME
LABELS_ADDRESS = _L_ADDRESS
LABELS_ACCOUNT_NO = _L_ACCOUNT_NO
LABELS_BANK = _L_BANK
LABELS_BILL_AMOUNT = _L_BILL_AMOUNT
LABELS_BILL_PERIOD = _L_BILL_PERIOD
LABELS_CONSUMER = _L_CONSUMER
LABELS_DUE_DATE = _L_DUE_DATE
