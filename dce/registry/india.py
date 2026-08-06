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
                Anchor(text="INCOME TAX DEPARTMENT", decisive=True, zone=Zone.title),
                Anchor(text="आयकर विभाग", lang="hi", decisive=True, zone=Zone.title),
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
                Anchor(text="Type"),
                Anchor(text="Country Code"),
                Anchor(text="Passport No"),
                Anchor(text="Given Name"),
                Anchor(text="Surname"),
                Anchor(text="Nationality"),
                Anchor(text="Place of Birth"),
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
                Anchor(text="STATEMENT OF ACCOUNT", decisive=True, zone=Zone.title),
                Anchor(text="ACCOUNT STATEMENT", decisive=True, zone=Zone.title),
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
            label="Salary Slip / Pay Slip",
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
                Anchor(text="CERTIFICATE OF INCORPORATION", decisive=True),
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
                Anchor(text="CERTIFICATE OF INCORPORATION", decisive=True),
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
