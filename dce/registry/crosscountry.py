"""Cross-country doctype pack — document shapes that occur in every jurisdiction.

These exist so the classifier has somewhere honest to land a document it recognises the
*shape* of but not the *issuer*: a utility bill from a country with no pack yet, a passport
whose issuing state we do not model, a form that is plainly a form and nothing more.

**Every anchor here is non-decisive, and the loader enforces it** (see
``_check_anchor``: ``country == "XX"`` forbids the decisive flag). That is the entire
design constraint of this module. A decisive anchor carries ``fuse_weight_anchor`` (3.0),
so a decisive ``PASSPORT`` on ``xx_passport_generic`` would let the generic spec draw with
``in_passport`` on a genuine Indian passport and push the pair below
``classify_min_margin`` — turning a confident answer into an abstention. Weak anchors mean
a country-specific doctype always outranks the generic one when both fire, and the generic
one still wins when nothing else does.

Their field sets are deliberately thin. The point of landing here is to get the document to
a reviewer with the two or three facts that make triage possible, not to pretend to know a
schema we have not modelled.
"""

from __future__ import annotations

from dce.models import Anchor, Category, DocTypeSpec
from dce.registry.india import (
    LABELS_ACCOUNT_NO,
    LABELS_ADDRESS,
    LABELS_BANK,
    LABELS_BILL_AMOUNT,
    LABELS_BILL_PERIOD,
    LABELS_CONSUMER,
    LABELS_DUE_DATE,
    LABELS_NAME,
    address_field,
    build_field,
    dob_field,
    expiry_field,
    issue_date_field,
    name_field,
    sex_field,
)
from dce.registry.loader import register_all

_SPECS: list[DocTypeSpec] = [
    DocTypeSpec(
        doctype_id="xx_utility_bill",
        label="Utility Bill (generic, issuer not modelled)",
        country="XX",
        category=Category.address_proof,
        issuing_authority="Unidentified utility / service provider",
        applies_to="both",
        officially_valid=False,
        anchors=[
            Anchor(text="UTILITY BILL"),
            Anchor(text="Bill"),
            Anchor(text="Invoice"),
            Anchor(text="Statement"),
            Anchor(text="Account Number"),
            Anchor(text="Customer Number"),
            Anchor(text="Consumer Number"),
            Anchor(text="Service Address"),
            Anchor(text="Billing Address"),
            Anchor(text="Billing Period"),
            Anchor(text="Amount Due"),
            Anchor(text="Total Due"),
            Anchor(text="Due Date"),
            Anchor(text="Previous Balance"),
            Anchor(text="Meter Reading"),
            Anchor(text="Tariff"),
            Anchor(text="Payment Received"),
        ],
        id_patterns=[],
        confusable_with={
            "in_utility_electricity": "an Indian electricity bill names a DISCOM and meters kWh",
            "in_utility_water": "an Indian water bill names a jal board / water supply board",
            "in_utility_gas": "an Indian gas bill names a city gas distributor and meters SCM",
            "in_utility_telephone": (
                "an Indian telephone bill names a telecom operator and prints call/rental charges"
            ),
        },
        negative_anchors=[],
        handling=(
            "A generic landing spot, not an accepted document type. Nothing here has been "
            "validated against an issuer, so it must never satisfy an address requirement on "
            "its own — route it to a reviewer who can identify the issuer."
        ),
        fields=[
            build_field(
                "consumer_number",
                "utility.consumer_number",
                kind="id",
                pii=True,
                labels=LABELS_CONSUMER,
                locators=("label", "kv"),
                notes="No format assumed — the issuer is by definition unknown here.",
            ),
            build_field(
                "customer_name", "identity.full_name", kind="name", pii=True, labels=LABELS_NAME
            ),
            address_field(required=True),
            build_field(
                "service_provider",
                "utility.service_provider",
                labels={"en": ["Provider", "Service Provider", "Company", "Issued by"]},
                notes="Identifying the provider is what promotes this document to a "
                "country-specific doctype; surface it prominently for the reviewer.",
            ),
            build_field(
                "bill_amount",
                "utility.bill_amount",
                kind="number",
                labels=LABELS_BILL_AMOUNT,
                locators=("table", "label", "kv"),
                notes="No currency validator: the amount_inr normaliser would mangle a "
                "non-Indian grouping convention.",
            ),
            build_field("bill_period", "utility.bill_period", labels=LABELS_BILL_PERIOD),
            build_field(
                "bill_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Bill Date", "Invoice Date", "Statement Date", "Date"]},
                notes="Deliberately no date_ddmmyyyy validator — day-first parsing is an "
                "Indian convention and would silently misread a month-first date.",
            ),
            build_field("due_date", "doc.due_date", kind="date", labels=LABELS_DUE_DATE),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_bank_statement",
        label="Bank Statement (generic, issuer not modelled)",
        country="XX",
        category=Category.financial,
        issuing_authority="Unidentified bank / financial institution",
        applies_to="both",
        officially_valid=False,
        anchors=[
            Anchor(text="Statement"),
            Anchor(text="Account Summary"),
            Anchor(text="Account Number"),
            Anchor(text="Statement Period"),
            Anchor(text="Opening Balance"),
            Anchor(text="Closing Balance"),
            Anchor(text="Beginning Balance"),
            Anchor(text="Ending Balance"),
            Anchor(text="Transaction"),
            Anchor(text="Description"),
            Anchor(text="Debit"),
            Anchor(text="Credit"),
            Anchor(text="Withdrawal"),
            Anchor(text="Deposit"),
            Anchor(text="Balance"),
            Anchor(text="IBAN"),
            Anchor(text="SWIFT"),
            Anchor(text="BIC"),
            Anchor(text="Sort Code"),
            Anchor(text="Routing Number"),
        ],
        id_patterns=[],
        confusable_with={
            "in_bank_statement": (
                "an Indian statement carries an IFSC and NEFT/RTGS/UPI/IMPS narrations"
            ),
            "in_bank_passbook": (
                "a passbook is titled PASSBOOK and carries an account-opening block"
            ),
        },
        negative_anchors=[],
        handling=(
            "Transaction-level personal data from an unidentified institution. Treat the "
            "whole document as pii and route it to a reviewer; do not accept it as evidence "
            "of anything until the issuer is identified."
        ),
        fields=[
            build_field(
                "account_number",
                "account.number",
                kind="id",
                required=True,
                pii=True,
                labels=LABELS_ACCOUNT_NO,
                locators=("label", "kv"),
                notes="No validator: account-number formats differ by country, and the Indian "
                "9-18 digit rule would reject an IBAN outright.",
            ),
            build_field("bank_name", "account.bank_name", labels=LABELS_BANK),
            build_field(
                "account_holder_name",
                "identity.full_name",
                kind="name",
                pii=True,
                labels=LABELS_NAME,
            ),
            address_field("address.mailing"),
            build_field(
                "statement_period",
                "doc.reference_number",
                labels={"en": ["Statement Period", "Period", "For the period", "From", "To"]},
            ),
            build_field(
                "closing_balance",
                "account.balance",
                kind="number",
                labels={"en": ["Closing Balance", "Ending Balance", "Balance"]},
                locators=("table", "label", "kv"),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_passport_generic",
        label="Passport (generic, issuing state not modelled)",
        country="XX",
        category=Category.identity,
        issuing_authority="Unidentified passport-issuing state",
        applies_to="individual",
        officially_valid=False,
        anchors=[
            Anchor(text="PASSPORT"),
            Anchor(text="PASSEPORT"),
            Anchor(text="PASAPORTE"),
            Anchor(text="Type"),
            Anchor(text="Code"),
            Anchor(text="Passport No"),
            Anchor(text="Surname"),
            Anchor(text="Given Names"),
            Anchor(text="Nationality"),
            Anchor(text="Date of birth"),
            Anchor(text="Place of birth"),
            Anchor(text="Date of issue"),
            Anchor(text="Date of expiry"),
            Anchor(text="Authority"),
            Anchor(text="Holder's signature"),
        ],
        id_patterns=[r"P[<K][A-Z]{3}"],
        confusable_with={
            "in_passport": (
                "the Indian book prints 'REPUBLIC OF INDIA' / 'भारत गणराज्य' and an MRZ issuing "
                "code of IND"
            ),
        },
        negative_anchors=[],
        handling=(
            "The MRZ is self-validating, so identity fields read from it can be trusted even "
            "when the issuing state is not modelled — but the document still cannot be "
            "treated as an RBI Officially Valid Document, because that status attaches to "
            "documents this service can attribute to a known issuer."
        ),
        fields=[
            build_field(
                "passport_number",
                "id.passport_number",
                kind="id",
                required=True,
                pii=True,
                labels={"en": ["Passport No", "Passport Number", "Document No"]},
                locators=("mrz", "label", "kv"),
                notes="Structure differs by issuing state; the MRZ check digits are the only "
                "trustworthy validation available here.",
            ),
            build_field(
                "surname",
                "identity.surname",
                kind="name",
                pii=True,
                labels={"en": ["Surname", "Family Name"]},
                locators=("mrz", "kv", "label"),
            ),
            build_field(
                "given_names",
                "identity.given_names",
                kind="name",
                pii=True,
                labels={"en": ["Given Names", "Given Name", "First Name"]},
                locators=("mrz", "kv", "label"),
            ),
            dob_field(),
            sex_field(),
            build_field(
                "nationality",
                "identity.nationality",
                labels={"en": ["Nationality"]},
                locators=("mrz", "kv", "label"),
            ),
            issue_date_field(),
            expiry_field(),
            build_field(
                "mrz",
                "",
                pattern=r"P[<K][A-Z]{3}[A-Z0-9<]{36,}",
                validator="mrz_td3",
                locators=("mrz", "regex"),
                notes="ICAO 9303 TD3. When it validates it outranks every printed field, "
                "whichever state issued the book.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_photo_id_generic",
        label="Photo Identity Card (generic, issuer not modelled)",
        country="XX",
        category=Category.identity,
        issuing_authority="Unidentified issuer",
        applies_to="individual",
        officially_valid=False,
        anchors=[
            Anchor(text="Identity Card"),
            Anchor(text="ID Card"),
            Anchor(text="Identification"),
            Anchor(text="Card Number"),
            Anchor(text="Card No"),
            Anchor(text="Name"),
            Anchor(text="Date of Birth"),
            Anchor(text="Sex"),
            Anchor(text="Address"),
            Anchor(text="Valid Until"),
            Anchor(text="Expires"),
            Anchor(text="Issued"),
            Anchor(text="Signature"),
            Anchor(text="Photograph"),
        ],
        id_patterns=[],
        confusable_with={
            "in_voter_epic": (
                "the EPIC names the Election Commission of India and an assembly constituency"
            ),
            "in_driving_licence": (
                "the Indian licence is headed 'DRIVING LICENCE' and lists vehicle classes"
            ),
            "in_aadhaar": "an Aadhaar names the Unique Identification Authority of India",
        },
        negative_anchors=[],
        handling=(
            "A card-shaped document with an unrecognised issuer. It has no regulatory "
            "standing whatsoever — the only correct next step is human identification of "
            "the issuer. Never auto-forward it to a model or accept it as an OVD."
        ),
        fields=[
            build_field(
                "card_number",
                "",
                kind="id",
                pii=True,
                labels={"en": ["Card Number", "Card No", "ID Number", "Identity Number"]},
                locators=("label", "kv"),
                notes="No attribute_key: an unattributed card number must not merge into the "
                "identity view as though it were a known government identifier.",
            ),
            name_field(required=False),
            dob_field(),
            sex_field(),
            address_field(),
            issue_date_field(),
            expiry_field(),
            build_field(
                "issuer",
                "doc.issuing_authority",
                labels={"en": ["Issued by", "Issuing Authority", "Authority"]},
                notes="Identifying the issuer is the whole job of the review this doctype "
                "routes to; surface it first.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_unknown_form",
        label="Unrecognised Form",
        country="XX",
        category=Category.other,
        issuing_authority="",
        applies_to="both",
        officially_valid=False,
        anchors=[
            Anchor(text="Application Form"),
            Anchor(text="Form"),
            Anchor(text="Declaration"),
            Anchor(text="Please fill"),
            Anchor(text="block letters"),
            Anchor(text="Tick as applicable"),
            Anchor(text="For Office Use Only"),
            Anchor(text="Signature"),
            Anchor(text="Place"),
            Anchor(text="Date"),
            Anchor(text="I hereby declare"),
            Anchor(text="Verification"),
            Anchor(text="Enclosures"),
            Anchor(text="Annexure"),
        ],
        id_patterns=[],
        confusable_with={},
        negative_anchors=[],
        handling=(
            "The last stop before UNKNOWN. Landing here means the document is structurally a "
            "form and nothing more was established. It goes to the human queue — it is never "
            "auto-forwarded to a model, which is the invariant this whole service exists to "
            "hold."
        ),
        fields=[
            build_field(
                "title",
                "",
                labels={"en": ["Title", "Subject", "Form Name", "Form No"]},
                locators=("label", "kv"),
                notes="The title-zone text, captured so a reviewer can identify the form "
                "without opening the original.",
            ),
            build_field(
                "subject_name", "identity.full_name", kind="name", pii=True, labels=LABELS_NAME
            ),
            build_field(
                "subject_address",
                "address.residential",
                kind="address",
                pii=True,
                labels=LABELS_ADDRESS,
                locators=("kv", "label"),
            ),
            build_field(
                "document_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Date", "Dated", "Place and Date"]},
                notes="No date validator: with an unknown issuer the day/month order is "
                "unknown too.",
            ),
        ],
    ),
]

register_all(_SPECS)

#: Every cross-country doctype, in declaration order.
SPECS: tuple[DocTypeSpec, ...] = tuple(_SPECS)
