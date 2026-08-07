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

**The non-decisive flag alone does not deliver that**, which is the second rule this module
now states explicitly:

    A cross-country doctype declares the words that name the *shape* and nothing else. It
    must not declare a string that a country pack also declares.

Weight is not the only way a spec can win — count is. ``xx_bank_statement`` used to declare
twenty anchors including ``Beginning Balance``, ``Ending Balance``, ``Statement Period``,
``Account Summary`` and ``Routing Number``: five of ``us_bank_statement``'s seven. On a US
statement the generic matched more of its own vocabulary than the specific spec matched of
its own, so the generic outranked it — the exact inversion the paragraph above promises
cannot happen. A string a country pack claims is, by construction, evidence about the
issuer; if it were not, the pack would not have claimed it. So it belongs to the pack, and
the generic keeps only the document-naming noun (``PASSPORT``, ``UTILITY BILL``) plus
structure that no jurisdiction owns (``Debit``, ``Credit``, ``Transaction``, ``IBAN``).

That rule is enforced at import by ``_check_generic_not_greedy`` in
:mod:`dce.registry.loader`, so a future pack cannot silently reintroduce the inversion by
adding a country doctype that overlaps a generic.

Their field sets are deliberately thin. The point of landing here is to get the document to
a reviewer with the two or three facts that make triage possible, not to pretend to know a
schema we have not modelled.

**Two kinds of doctype now live here, and only the first kind is a fallback.**

*Shape fallbacks* — ``xx_utility_bill``, ``xx_bank_statement``, ``xx_passport_generic``,
``xx_photo_id_generic``, ``xx_unknown_form``. Landing on one of these means "we recognised
the shape and failed to identify the issuer". They are anchor-weak on purpose and they
carry ``handling`` text saying they satisfy no requirement on their own.

*Globally-issued instruments* — the LEI certificate, the OECD self-certification, the
Wolfsberg questionnaire, the ISDA Master Agreement, the ISO certificate, the D-U-N-S
record and the rest. These are **not** fallbacks. They live here because their issuer is a
single global body (GLEIF, the OECD, the Wolfsberg Group, ISDA, ISO, Dun & Bradstreet, the
IASB/IAASB) rather than a state, so filing them under any one country would be a lie: an
Indian counterparty and a Mexican one present the identical document. Their anchors are
consequently *stronger* than a shape fallback's, not weaker — the strings below are
controlled by their issuing body in exactly the sense a decisive anchor requires.

They still may not be flagged decisive: ``_check_anchor`` forbids the flag for
``country == "XX"`` and that rule stays, because the flag is worth a 2.0 multiplier and the
one property this module must never lose is that a country doctype outranks a generic on
the same page. What replaces the flag is specificity. "Wolfsberg Group", "ISO 17442" and
"D-U-N-S" are strings that appear on no other document in this registry, so these doctypes
win by concurrence over their own vocabulary and score zero on anything else — which is the
behaviour the decisive flag would have bought, without the ranking hazard.

The same discipline cuts the other way. A document type whose distinguishing strings are
written by whoever prepared it does **not** get a doctype here, however common it is in a
diligence pack: see the note at the end of this module for the ones that were rejected on
that ground.
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
from dce.registry.loader import ATTRIBUTE_KEYS, register_all

#: Attribute keys the globally-issued instruments below need and the base namespace does
#: not have. Contributed with ``setdefault`` and worded identically to the copies in
#: :mod:`dce.registry.mexico`, exactly as the country packs contribute theirs — whichever
#: pack imports first wins, and the two must therefore agree to the character or the merge
#: view would describe the same key two ways depending on import order.
for _key, _description in {
    "id.lei": "Legal Entity Identifier (ISO 17442) issued through GLEIF",
    "id.duns": "Dun & Bradstreet D-U-N-S Number",
    "id.tin": "Taxpayer Identification Number, jurisdiction stated separately",
    "entity.ticker": "Trading symbol under which a class of securities trades",
    "entity.exchange": "Exchange on which a class of securities is registered",
    "entity.jurisdiction": "State / province / country of incorporation or organisation",
    "entity.status": "Registry status of the entity (good standing, active, dissolved)",
    "entity.fiscal_year_end": "Financial year end the report or filing closes on",
    "entity.auditor": "Independent accounting firm that signed the audit report",
    "entity.shares_outstanding": "Shares of a class outstanding as of a stated date",
    "doc.period_covered": "Reporting period a periodic report covers",
}.items():
    ATTRIBUTE_KEYS.setdefault(_key, _description)
del _key, _description

#: LEI — ISO 17442: 18 alphanumerics plus two ISO 7064 MOD 97-10 check digits.
LEI_PATTERN = r"\b[A-Z0-9]{18}\d{2}\b"
#: D-U-N-S — nine digits, printed hyphenated on D&B's own output.
DUNS_PATTERN = r"\b\d{2}-\d{3}-\d{4}\b"

_SPECS: list[DocTypeSpec] = [
    DocTypeSpec(
        doctype_id="xx_utility_bill",
        label="Utility Bill (generic, issuer not modelled)",
        country="XX",
        category=Category.address_proof,
        issuing_authority="Unidentified utility / service provider",
        applies_to="both",
        officially_valid=False,
        # Shape words only. "Service Address", "Billing Period", "Amount Due" and
        # "Meter Reading" were removed: ca_utility_bill, us_utility_bill and the three
        # in_utility_* specs claim them, and a generic that repeats a pack's vocabulary
        # competes with the pack instead of catching what the pack misses. "Statement" and
        # "Account Number" went for the same reason plus a second one — they were shared
        # with xx_bank_statement, so they told the classifier nothing about which of the two
        # generics it was looking at either.
        anchors=[
            Anchor(text="UTILITY BILL"),
            Anchor(text="Bill"),
            Anchor(text="Invoice"),
            Anchor(text="Customer Number"),
            Anchor(text="Consumer Number"),
            Anchor(text="Billing Address"),
            Anchor(text="Total Due"),
            Anchor(text="Due Date"),
            Anchor(text="Previous Balance"),
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
        # Shape words only, and the payment rails belonging to jurisdictions this service
        # does not model. Everything a bank-statement pack claims was removed:
        # "Account Summary"/"Beginning Balance"/"Ending Balance"/"Statement Period" and
        # "Routing Number" (us_bank_statement, us_voided_check), "Opening Balance"/
        # "Closing Balance"/"Withdrawal"/"Deposit" (ca_bank_statement, in_bank_statement,
        # in_bank_passbook). Those five US strings were the whole bug: the generic matched
        # more of us_bank_statement's vocabulary than us_bank_statement did, so a Bank of
        # America statement landed on "issuer not modelled". "Routing Number" is an ABA
        # routing transit number, i.e. the single most issuer-identifying string on a US
        # statement — a doctype whose definition is "I cannot identify the issuer" has no
        # business claiming it.
        # IBAN / SWIFT / BIC / Sort Code stay: they are the rails of jurisdictions that
        # have no pack, which is exactly what this doctype is for.
        anchors=[
            Anchor(text="Transaction"),
            Anchor(text="Description"),
            Anchor(text="Debit"),
            Anchor(text="Credit"),
            Anchor(text="Balance"),
            Anchor(text="IBAN"),
            Anchor(text="SWIFT"),
            Anchor(text="BIC"),
            Anchor(text="Sort Code"),
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
        # The document-naming noun in the three languages this registry covers, plus the
        # TD3 MRZ pattern below. Nothing else — not even "Passport No", which in_passport
        # claims and which the ``passport_number`` field label already contributes.
        #
        # Removed: "Type", "Code", "Authority", "Nationality", "Surname", "Date of birth",
        # "Place of birth", "Date of issue". Those are ICAO 9303 visual-inspection-zone
        # *field labels*, not passport identity — every one of them is printed on
        # identity cards, visas, birth certificates and residence permits, and "Type" and
        # "Code" are simply ordinary English words. With fifteen anchors of which eight were
        # generic-identity vocabulary, this spec scored on any document that merely
        # *discusses* identity: it ranked first on a birth-registration worksheet and on a
        # social-insurance-number application form, both of which say "date of birth" and
        # "place of birth" many times and are not passports. A passport is identified by
        # saying "passport" and by carrying a TD3 machine-readable zone; that is what is
        # left here, and the MRZ is self-validating.
        anchors=[
            Anchor(text="PASSPORT"),
            Anchor(text="PASSEPORT"),
            Anchor(text="PASAPORTE"),
            Anchor(text="Date of expiry"),
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
        # Card-naming nouns and card structure. "Name", "Address", "Sex", "Date of Birth",
        # "Issued" and "Signature" were removed: they are on every identity document and
        # most forms, so they let this spec accumulate score on documents that are neither
        # cards nor identity documents, which is the opposite of a last-resort landing spot.
        anchors=[
            Anchor(text="Identity Card"),
            Anchor(text="ID Card"),
            Anchor(text="Identification"),
            Anchor(text="Card Number"),
            Anchor(text="Card No"),
            Anchor(text="Valid Until"),
            Anchor(text="Expires"),
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
        # Form-ness, and only form-ness. "Signature", "Place", "Date" and "Verification"
        # were removed: they appear on essentially every document in the registry, so they
        # gave the last-resort doctype a floor of score on documents that a country pack was
        # about to identify correctly — it was the second-ranked candidate on a CKYC record
        # and on a partnership registration certificate. What makes a document *only* a form
        # is its filling instructions, not the fact that somebody signed it.
        anchors=[
            Anchor(text="Application Form"),
            Anchor(text="Form"),
            Anchor(text="Declaration"),
            Anchor(text="Please fill"),
            Anchor(text="block letters"),
            Anchor(text="Tick as applicable"),
            Anchor(text="For Office Use Only"),
            Anchor(text="I hereby declare"),
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
    # =======================================================================
    # Globally-issued instruments. Not fallbacks — see the module docstring.
    # =======================================================================
    DocTypeSpec(
        doctype_id="xx_lei_certificate",
        label="Legal Entity Identifier (LEI) Registration Record — GLEIF",
        country="XX",
        category=Category.corporate,
        issuing_authority=(
            "A Local Operating Unit accredited by the Global Legal Entity Identifier "
            "Foundation (GLEIF); the code itself is defined by ISO 17442"
        ),
        applies_to="corporate",
        officially_valid=False,
        # GLEIF's own vocabulary. "Legal Entity Identifier" is the naming noun and is the
        # only string here a country pack could plausibly want; the rest — GLEIF, ISO 17442,
        # Managing LOU, Entity Legal Form Code — are terms of art the Foundation coined and
        # that appear on no national document. If a country pack ever claims one of these,
        # _check_generic_not_greedy will say so at import, and the right fix is the one that
        # check states: the string belongs to the pack, so drop it from here.
        anchors=[
            Anchor(text="Legal Entity Identifier"),
            Anchor(text="GLEIF"),
            Anchor(text="Global Legal Entity Identifier Foundation"),
            Anchor(text="ISO 17442"),
            Anchor(text="Managing LOU"),
            Anchor(text="Local Operating Unit"),
            Anchor(text="Entity Legal Form Code"),
            Anchor(text="Next Renewal Date"),
            Anchor(text="Initial Registration Date"),
            Anchor(text="Registration Status"),
            Anchor(text="Validation Sources"),
        ],
        id_patterns=[LEI_PATTERN],
        confusable_with={
            "in_certificate_incorporation": (
                "an incorporation certificate is issued by a national registrar and "
                "creates the company; an LEI record only identifies a company that "
                "already exists, and never confers legal status"
            ),
        },
        negative_anchors=[],
        handling=(
            "The whole LEI dataset is published openly by GLEIF, so nothing here is "
            "confidential. That cuts both ways: an LEI record is free to obtain and proves "
            "only that somebody registered the entity, never that the entity is in good "
            "standing — never accept it in place of a registrar's certificate."
        ),
        fields=[
            build_field(
                "lei",
                "id.lei",
                kind="id",
                required=True,
                pattern=LEI_PATTERN,
                locators=("label", "kv", "regex"),
                notes="20 characters, the last two being ISO 7064 MOD 97-10 check digits. "
                "The check is not implemented here, so a read is format-valid and not "
                "checksum-verified — say so rather than implying the stronger claim.",
            ),
            build_field(
                "legal_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Legal Name", "Entity Legal Name", "Legal name"]},
            ),
            build_field(
                "registered_address",
                "address.registered",
                kind="address",
                labels={"en": ["Legal Address", "Registered Address", "Headquarters Address"]},
                locators=("kv", "label"),
            ),
            build_field(
                "legal_jurisdiction",
                "entity.jurisdiction",
                labels={"en": ["Legal Jurisdiction", "Jurisdiction", "Country"]},
            ),
            build_field(
                "entity_legal_form",
                "entity.constitution",
                labels={"en": ["Entity Legal Form", "Entity Legal Form Code", "Legal Form"]},
                notes="An ISO 20275 ELF code (e.g. 8888 for 'not yet assigned'), not free "
                "text — resolve it before showing it to a human.",
            ),
            build_field(
                "registration_status",
                "entity.status",
                labels={"en": ["Registration Status", "Entity Status"]},
                notes="ISSUED / LAPSED / RETIRED / ANNULLED. A LAPSED LEI is the common "
                "finding in a diligence pack and it is an adverse one — the entity "
                "stopped paying to keep its record current.",
            ),
            build_field(
                "next_renewal_date",
                "doc.expiry_date",
                kind="date",
                labels={"en": ["Next Renewal Date", "Renewal Date"]},
                notes="No date validator: GLEIF publishes ISO-8601 but LOU-issued PDFs "
                "reformat it into the local convention.",
            ),
            build_field(
                "managing_lou",
                "doc.issuing_authority",
                labels={"en": ["Managing LOU", "LOU", "Issued by"]},
            ),
            build_field(
                "initial_registration_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Initial Registration Date", "Registration Date"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_fatca_crs_self_certification",
        label="FATCA / CRS Tax Residency Self-Certification",
        country="XX",
        category=Category.tax,
        issuing_authority=(
            "Completed by the account holder for a financial institution; the form follows "
            "the OECD Common Reporting Standard and the US FATCA regulations"
        ),
        applies_to="both",
        officially_valid=False,
        # OECD terms of art. Every one of these is defined in the CRS itself, which is why
        # they survive translation into whichever bank's house form is in front of us and
        # why no national pack owns them. The FATCA side is deliberately represented by
        # "GIIN" and "Chapter 4" only: the IRS's own W-8 series already has doctypes
        # (us_w8ben, us_w8bene) and their strings belong to those.
        anchors=[
            Anchor(text="Common Reporting Standard"),
            Anchor(text="Self-Certification"),
            Anchor(text="Controlling Person"),
            Anchor(text="Passive Non-Financial Entity"),
            Anchor(text="Active Non-Financial Entity"),
            Anchor(text="Reportable Jurisdiction"),
            Anchor(text="Automatic Exchange of Information"),
            Anchor(text="Country of Tax Residence"),
            Anchor(text="Tax Identification Number"),
            # "GIIN" was declared here and removed: us_w8bene claims it, and a GIIN is the
            # single most FATCA-specific string on any form, so it is evidence about the
            # IRS's own document rather than about a bank's house self-certification. The
            # ``giin`` field below stays — a CRS form does have a box for it, and a field
            # label costs the W-8BEN-E nothing.
        ],
        id_patterns=[],
        confusable_with={
            "us_w8bene": (
                "the IRS's own form is a statutory instrument with a form number printed on "
                "it; this is a bank's house self-certification covering the same ground, "
                "and the W-8BEN-E must win on any page carrying that form number"
            ),
            "us_w8ben": (
                "the same relationship for an individual account holder — the IRS form wins"
            ),
            "xx_ubo_declaration": (
                "a CRS self-certification names controlling persons for tax-reporting "
                "purposes and a UBO declaration names them for AML purposes; the "
                "thresholds and the definitions genuinely differ, so the two must not be "
                "merged into one answer"
            ),
        },
        negative_anchors=[],
        handling=(
            "Declares an individual's tax residences and taxpayer numbers — among the most "
            "sensitive combinations of personal data this service sees, and the reason "
            "every identifier field below is flagged pii."
        ),
        fields=[
            build_field(
                "account_holder_name",
                "identity.full_name",
                kind="name",
                required=True,
                pii=True,
                labels={"en": ["Account Holder", "Name of Account Holder", "Entity Name"]},
            ),
            build_field(
                "country_of_tax_residence",
                "",
                multi=True,
                required=True,
                labels={"en": ["Country of Tax Residence", "Jurisdiction of Residence"]},
                notes="Multiple residences are normal and are the whole point of the form. "
                "No attribute key: tax residence is a declaration by the subject, not a "
                "fact the registry can attribute to a document issuer.",
            ),
            build_field(
                "tin",
                "id.tin",
                kind="id",
                multi=True,
                pii=True,
                labels={"en": ["TIN", "Taxpayer Identification Number", "Tax Reference Number"]},
                notes="Deliberately unvalidated: a TIN's shape is defined by whichever of "
                "100+ jurisdictions issued it, and rejecting on shape would discard "
                "genuine numbers from every country this service does not model.",
            ),
            build_field(
                "giin",
                "id.giin",
                kind="id",
                labels={"en": ["GIIN", "Global Intermediary Identification Number"]},
                pattern=r"\b[A-Z0-9]{6}\.[A-Z0-9]{5}\.[A-Z]{2}\.\d{3}\b",
                notes="The IRS publishes the 19-character XXXXXX.XXXXX.LL.CCC layout but no "
                "check digit, so the pattern is structure only.",
            ),
            build_field(
                "entity_classification",
                "",
                labels={
                    "en": [
                        "Entity Classification",
                        "Passive Non-Financial Entity",
                        "Active Non-Financial Entity",
                        "Financial Institution",
                    ]
                },
                notes="Passive NFE is the classification that forces the controlling-person "
                "section to be completed; surface it, because an empty controlling-person "
                "section is only correct for the other classifications.",
            ),
            build_field(
                "controlling_persons",
                "ownership.beneficial_owner",
                kind="name",
                multi=True,
                pii=True,
                labels={"en": ["Controlling Person", "Controlling Persons"]},
                locators=("table", "kv", "label"),
            ),
            build_field(
                "signatory",
                "ownership.authorized_signer",
                kind="name",
                pii=True,
                labels={"en": ["Signature", "Signed by", "Print Name"]},
            ),
            build_field(
                "declaration_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Date", "Date of Declaration", "Dated"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_wolfsberg_questionnaire",
        label="Wolfsberg Group CBDDQ / FCCQ Due Diligence Questionnaire",
        country="XX",
        category=Category.corporate,
        issuing_authority=(
            "The Wolfsberg Group, which publishes and versions the questionnaire; completed "
            "by the financial institution answering it"
        ),
        applies_to="corporate",
        officially_valid=False,
        # The Wolfsberg Group names its own instruments and versions them, so the titles and
        # the acronyms are as tightly controlled as any form number. Nothing else in banking
        # prints "CBDDQ".
        anchors=[
            Anchor(text="Wolfsberg Group"),
            Anchor(text="Correspondent Banking Due Diligence Questionnaire"),
            Anchor(text="Financial Crime Compliance Questionnaire"),
            Anchor(text="CBDDQ"),
            Anchor(text="FCCQ"),
            Anchor(text="Entity & Ownership"),
            Anchor(text="AML, CTF & Sanctions Programme"),
            Anchor(text="Anti-Bribery & Corruption"),
            Anchor(text="Declaration Statement"),
        ],
        id_patterns=[],
        confusable_with={
            "in_ckyc_record": (
                "a CKYC record is a national registry's own record of a customer; this is "
                "an institution's self-description and carries no registry's authority"
            ),
            "xx_sanctions_screening_report": (
                "the questionnaire is what an institution says about its own controls; a "
                "screening report is what a vendor found about a counterparty"
            ),
        },
        negative_anchors=[],
        handling=(
            "Describes a bank's financial-crime controls in detail, which is commercially "
            "sensitive and is shared under the Wolfsberg Group's own usage terms. Treat the "
            "whole document as confidential to the responding institution."
        ),
        fields=[
            build_field(
                "financial_institution_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Financial Institution Name", "Legal Name of FI", "FI Name"]},
            ),
            build_field(
                "lei",
                "id.lei",
                kind="id",
                pattern=LEI_PATTERN,
                labels={"en": ["LEI", "Legal Entity Identifier"]},
                locators=("label", "kv", "regex"),
            ),
            build_field(
                "questionnaire_version",
                "",
                labels={"en": ["Version", "CBDDQ V", "Questionnaire Version"]},
                notes="The Wolfsberg Group revises the questionnaire and correspondent banks "
                "reject superseded versions, so the version is the first thing a reviewer "
                "needs.",
            ),
            build_field(
                "country_of_incorporation",
                "entity.jurisdiction",
                labels={"en": ["Country of Incorporation", "Jurisdiction"]},
            ),
            build_field(
                "registered_address",
                "address.registered",
                kind="address",
                labels={"en": ["Registered Address", "Principal Place of Business"]},
                locators=("kv", "label"),
            ),
            build_field(
                "signatory",
                "ownership.authorized_signer",
                kind="name",
                pii=True,
                labels={"en": ["Name", "Signed by", "Declaration Statement"]},
            ),
            build_field(
                "signatory_title",
                "",
                labels={"en": ["Title", "Position", "Role"]},
            ),
            build_field(
                "completion_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Date", "Date of Completion", "Reporting Period"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_isda_master_agreement",
        label="ISDA Master Agreement (with Schedule / Credit Support Annex)",
        country="XX",
        category=Category.financial,
        issuing_authority=(
            "The International Swaps and Derivatives Association publishes the pre-printed "
            "form; the parties execute it and negotiate the Schedule"
        ),
        applies_to="corporate",
        officially_valid=False,
        # The pre-printed form is ISDA's copyrighted text and the parties may not alter it —
        # they amend it in the Schedule instead. That is what makes these strings safe: the
        # body of the agreement reads identically whichever two institutions signed it.
        anchors=[
            Anchor(text="International Swaps and Derivatives Association"),
            Anchor(text="ISDA Master Agreement"),
            Anchor(text="Credit Support Annex"),
            Anchor(text="Schedule to the Master Agreement"),
            Anchor(text="Early Termination Date"),
            Anchor(text="Events of Default"),
            Anchor(text="Termination Events"),
            Anchor(text="Close-out Amount"),
            Anchor(text="Confirmation"),
        ],
        id_patterns=[],
        # No country doctype models a derivatives master agreement, so the rivals named here
        # are the other long executed contracts in the registry. All three are multi-party
        # instruments of recitals, defined terms and signature blocks, and on a page of
        # boilerplate that is most of what the classifier can see.
        confusable_with={
            "us_operating_agreement": (
                "an operating agreement governs one LLC's internal affairs; an ISDA governs "
                "trades between two institutions and names ISDA's published form"
            ),
            "us_trust_agreement": (
                "a trust agreement creates a trust; an ISDA creates no entity at all"
            ),
        },
        negative_anchors=[],
        handling=(
            "A bilateral contract. Its Schedule contains the negotiated credit terms, which "
            "are commercially confidential to both parties — never quote it into a summary "
            "that leaves the requesting institution."
        ),
        fields=[
            build_field(
                "party_a",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Party A", "between"]},
            ),
            build_field(
                "party_b",
                "",
                kind="name",
                required=True,
                validator="name",
                labels={"en": ["Party B", "and"]},
                notes="No attribute key: an agreement has two counterparties and only one of "
                "them is the subject of the file this document was submitted against. "
                "Which one is a question for the caller, not for the registry.",
            ),
            build_field(
                "agreement_form_year",
                "",
                labels={"en": ["2002 ISDA Master Agreement", "1992 ISDA Master Agreement"]},
                notes="1992 and 2002 differ materially on close-out; which form was used is a "
                "credit-risk fact, not a formatting detail.",
            ),
            build_field(
                "agreement_date",
                "doc.issue_date",
                kind="date",
                required=True,
                labels={"en": ["dated as of", "Date", "Agreement Date"]},
            ),
            build_field(
                "governing_law",
                "",
                labels={"en": ["Governing Law", "governed by"]},
                notes="English law or New York law in almost every case, and the choice "
                "decides which close-out mechanics apply.",
            ),
            build_field(
                "signatory",
                "ownership.authorized_signer",
                kind="name",
                multi=True,
                pii=True,
                labels={"en": ["By:", "Name:", "Signature"]},
            ),
            build_field(
                "signatory_title",
                "",
                multi=True,
                labels={"en": ["Title:", "Title"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_sanctions_screening_report",
        label="Sanctions / PEP Screening Report",
        country="XX",
        category=Category.other,
        issuing_authority=(
            "A screening vendor (Dow Jones, LexisNexis, Refinitiv, ComplyAdvantage and "
            "others) or an institution's own screening engine"
        ),
        applies_to="both",
        officially_valid=False,
        # The vendors write their own layouts, so the report itself carries no controlled
        # string — but the *lists it screens against* are named by the authorities that
        # publish them, and every vendor must name them the same way to be intelligible.
        # Those list names are the anchors.
        #
        # "Politically Exposed Person" was a candidate and was dropped: in_ckyc_record
        # already claims it, so declaring it here would be precisely the greedy overlap
        # _check_generic_not_greedy forbids, and a CKYC record is a document this doctype
        # must never outrank.
        anchors=[
            Anchor(text="Specially Designated Nationals"),
            Anchor(text="Consolidated Sanctions List"),
            Anchor(text="OFAC"),
            Anchor(text="Sanctions Screening"),
            Anchor(text="PEP Screening"),
            Anchor(text="Adverse Media"),
            Anchor(text="Watchlist"),
            Anchor(text="Match Score"),
            Anchor(text="False Positive"),
            Anchor(text="No matches found"),
        ],
        id_patterns=[],
        confusable_with={
            "in_ckyc_record": (
                "the CKYC record claims 'Politically Exposed Person' and is a registry's "
                "record of a real customer; this is a vendor's search output, and it must "
                "never outrank the record"
            ),
            "xx_wolfsberg_questionnaire": (
                "the questionnaire is a self-description of controls; this is a result "
                "produced by running a name against published lists"
            ),
        },
        negative_anchors=[],
        handling=(
            "A screening result is an allegation about a named person until a human "
            "adjudicates it, and a false positive that escapes into a customer record is a "
            "defamation risk as well as a compliance one. Never auto-accept a hit, never "
            "merge a hit into an identity view, and treat the whole report as pii."
        ),
        fields=[
            build_field(
                "screened_name",
                "identity.full_name",
                kind="name",
                required=True,
                pii=True,
                labels={"en": ["Search Term", "Name Screened", "Subject", "Query"]},
            ),
            build_field(
                "screening_date",
                "doc.issue_date",
                kind="date",
                required=True,
                labels={"en": ["Date of Screening", "Search Date", "Run Date", "Date"]},
                notes="A screening result is only as good as its date; the lists change "
                "daily, so an undated report is worthless and must go to review.",
            ),
            build_field(
                "screening_result",
                "",
                required=True,
                labels={"en": ["Result", "Status", "Outcome", "No matches found"]},
                notes="No attribute key, deliberately. A screening outcome is a statement "
                "about one run of one vendor's data at one moment; merging it into a "
                "durable customer attribute would turn a snapshot into a standing label.",
            ),
            build_field(
                "lists_searched",
                "",
                multi=True,
                labels={"en": ["Lists Searched", "Sources", "Databases", "Watchlist"]},
                locators=("table", "label", "kv"),
                notes="Which lists were searched is what makes a negative result meaningful; "
                "a report that does not say is not evidence of anything.",
            ),
            build_field(
                "match_count",
                "",
                kind="number",
                labels={"en": ["Matches", "Hits", "Match Count", "Match Score"]},
                locators=("table", "label", "kv"),
            ),
            build_field(
                "vendor",
                "doc.issuing_authority",
                labels={"en": ["Provider", "Vendor", "Generated by", "Powered by"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_ubo_declaration",
        label="Ultimate Beneficial Ownership Declaration",
        country="XX",
        category=Category.corporate,
        issuing_authority="Declared by the entity's officers to the requesting institution",
        applies_to="corporate",
        officially_valid=False,
        # The narrowest anchor set in this module, and on purpose. us_fincen_boir is a real
        # UBO document with a statutory form behind it, and the failure this module exists
        # to prevent is precisely a generic outranking that kind of doctype on its own
        # document. So the FinCEN report's vocabulary — "Beneficial Owner", "BENEFICIAL
        # OWNERSHIP INFORMATION REPORT", "FinCEN Identifier" — is left entirely to it, and
        # what is claimed here is the terminology of a *voluntary* declaration made to a
        # bank: FATF's "ultimate beneficial owner", and the ownership-chain vocabulary that
        # a statutory filing does not use because its form already has boxes for it.
        anchors=[
            Anchor(text="Ultimate Beneficial Owner"),
            Anchor(text="Declaration of Beneficial Ownership"),
            Anchor(text="Ownership Percentage"),
            Anchor(text="Direct Ownership"),
            Anchor(text="Indirect Ownership"),
            Anchor(text="Senior Managing Official"),
            Anchor(text="Nature of Control"),
            Anchor(text="Ownership Chain"),
        ],
        id_patterns=[],
        confusable_with={
            "us_fincen_boir": (
                "the single most important pairing in this module. A BOIR is the statutory "
                "US beneficial-ownership filing, with FinCEN's own form behind it; this is "
                "a voluntary declaration made to a bank. On any page carrying the BOIR's "
                "title the BOIR must win, which is why none of its vocabulary is claimed "
                "here"
            ),
            "us_fincen_boi_cert": (
                "the FinCEN submission confirmation proves a BOIR was filed; a declaration "
                "proves only that somebody wrote one"
            ),
            "xx_fatca_crs_self_certification": (
                "both list controlling persons, but CRS control is a tax-reporting concept "
                "and UBO control is an AML one, and their thresholds differ"
            ),
        },
        negative_anchors=[],
        handling=(
            "Names natural persons, their ownership percentages and usually their dates of "
            "birth and identity-document numbers. A self-declaration is evidence of what "
            "the entity asserts and nothing more — it never substitutes for a registry "
            "extract, and in jurisdictions with a statutory beneficial-ownership filing the "
            "statutory document outranks it."
        ),
        fields=[
            build_field(
                "entity_legal_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Entity Name", "Legal Name", "Company Name"]},
            ),
            build_field(
                "beneficial_owners",
                "ownership.beneficial_owner",
                kind="name",
                multi=True,
                required=True,
                pii=True,
                labels={
                    "en": ["Ultimate Beneficial Owner", "Beneficial Owner Name", "Full Name"]
                },
                locators=("table", "kv", "label"),
            ),
            build_field(
                "ownership_percentages",
                "ownership.share",
                multi=True,
                labels={"en": ["Ownership Percentage", "% Held", "Shareholding"]},
                locators=("table", "kv", "label"),
                notes="Percentages only mean something next to the owner they belong to; a "
                "list of bare numbers is a review item, not an extraction.",
            ),
            build_field(
                "nature_of_control",
                "",
                multi=True,
                labels={"en": ["Nature of Control", "Type of Control", "Basis of Control"]},
                locators=("table", "kv", "label"),
                notes="Control through shares, voting rights, or the right to appoint the "
                "board are different findings and the last one is invisible in a "
                "shareholding table.",
            ),
            build_field(
                "senior_managing_official",
                "ownership.director",
                kind="name",
                multi=True,
                pii=True,
                labels={"en": ["Senior Managing Official", "Senior Manager"]},
                notes="Named when no natural person meets the ownership threshold. That is a "
                "meaningful negative finding about the structure, not a fallback value.",
            ),
            build_field(
                "signatory",
                "ownership.authorized_signer",
                kind="name",
                pii=True,
                labels={"en": ["Signature", "Signed by", "Authorised Signatory"]},
            ),
            build_field(
                "declaration_date",
                "doc.issue_date",
                kind="date",
                required=True,
                labels={"en": ["Date", "Dated", "Date of Declaration"]},
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_audited_financial_statements",
        label="Audited Financial Statements with Independent Auditor's Report (IFRS / ISA)",
        country="XX",
        category=Category.financial,
        issuing_authority=(
            "Prepared by the entity under IFRS Accounting Standards as issued by the IASB "
            "and reported on by an independent auditor under the IAASB's International "
            "Standards on Auditing"
        ),
        applies_to="corporate",
        officially_valid=False,
        # ONE doctype, not two. The audited statements and the auditor's report over them
        # were drafted as separate doctypes and merged after the obvious objection: they are
        # bound into a single PDF in every diligence pack that exists, so two specs would
        # split the evidence on one document and the pair would abstain by construction —
        # a designed-in abstention is not a safe outcome, it is a useless one.
        #
        # What makes the vocabulary usable is that the standard-setters mandate wording:
        # IAS 1.16 requires the explicit and unreserved compliance statement quoted below,
        # and it is not chosen by the preparer.
        #
        # The weight sits on the IFRS side and that is not an accident. "Key Audit Matters"
        # (ISA 701) and "Basis for Opinion" (ISA 700) were declared here and removed —
        # us_auditor_report and in_statutory_auditor_report claim them, and this module's
        # rule is that a string a country pack claims belongs to that pack. Losing them is
        # the right outcome rather than a compromise: those two doctypes model the auditor's
        # report of a named jurisdiction, and this one has to be beaten by them on their own
        # documents. What is left is the statements' own vocabulary, which is what remains
        # when the country pack has taken the report.
        anchors=[
            Anchor(
                text=(
                    "International Financial Reporting Standards as issued by the "
                    "International Accounting Standards Board"
                )
            ),
            Anchor(text="International Accounting Standards Board"),
            Anchor(text="International Standards on Auditing"),
            Anchor(text="International Ethics Standards Board for Accountants"),
            Anchor(text="Consolidated Statement of Financial Position"),
            Anchor(text="Notes to the Consolidated Financial Statements"),
            Anchor(text="Non-controlling interests"),
        ],
        id_patterns=[],
        confusable_with={
            "xx_bank_statement": (
                "a bank statement is issued to a customer by a bank and lists transactions; "
                "financial statements are prepared by the entity about itself"
            ),
            "mx_reporte_anual_cnbv": (
                "a Mexican Anexo N *contains* audited consolidated statements as an annex, "
                "so both fire on the same PDF — the Anexo N's CNBV taxonomy headings are "
                "what make it the more specific answer, and it must win"
            ),
        },
        negative_anchors=[],
        handling=(
            "Financial statements filed publicly are public, but the same document is "
            "routinely supplied privately by unlisted companies under an NDA. This service "
            "cannot tell the two apart from the bytes, so treat the contents as "
            "confidential to the submitting party unless the caller says otherwise."
        ),
        fields=[
            build_field(
                "entity_legal_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Entity Name", "Company Name", "Group"]},
            ),
            build_field(
                "fiscal_year_end",
                "entity.fiscal_year_end",
                kind="date",
                required=True,
                labels={
                    "en": [
                        "for the year ended",
                        "as at",
                        "Financial year ended",
                        "Year ended",
                    ]
                },
                notes="No date validator: the year end is printed in the local convention "
                "and the preparer's jurisdiction is unknown by construction here.",
            ),
            build_field(
                "period_covered",
                "doc.period_covered",
                labels={"en": ["Period", "Reporting Period", "for the year ended"]},
            ),
            build_field(
                "auditor",
                "entity.auditor",
                kind="name",
                required=True,
                labels={"en": ["Independent Auditor", "Auditor", "Chartered Accountants"]},
                notes="The audit firm signs, not an individual, in most jurisdictions — but "
                "in some the engagement partner is named too, and that name is personal "
                "data. Prefer the firm.",
            ),
            build_field(
                "audit_opinion",
                "",
                labels={
                    "en": ["Opinion", "Basis for Opinion", "Qualified Opinion", "Adverse Opinion"]
                },
                notes="Unmodified / qualified / adverse / disclaimer. A modified opinion is "
                "the single most important fact in the document and must never be "
                "flattened into a boolean or dropped.",
            ),
            build_field(
                "key_audit_matters",
                "",
                multi=True,
                labels={"en": ["Key Audit Matters", "Key Audit Matter"]},
                locators=("table", "label", "kv"),
            ),
            build_field(
                "report_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Date", "Dated", "Date of the auditor's report"]},
            ),
            build_field(
                "registered_address",
                "address.registered",
                kind="address",
                labels={"en": ["Registered Office", "Registered Address"]},
                locators=("kv", "label"),
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_certificate_of_insurance",
        label="Certificate of Insurance",
        country="XX",
        category=Category.other,
        issuing_authority="An insurance producer or broker on behalf of the insurer(s)",
        applies_to="both",
        officially_valid=False,
        # The lead anchor is the discriminating clause of ACORD's disclaimer: the sentence a
        # certificate must carry and a policy must not — the certificate telling the reader
        # it is not the policy.
        #
        # It is the *clause*, not the sentence, and that was measured rather than chosen for
        # brevity. The full legend ("THIS CERTIFICATE IS ISSUED AS A MATTER OF INFORMATION
        # ONLY AND CONFERS NO RIGHTS UPON THE CERTIFICATE HOLDER") is twenty words of which
        # fifteen are ordinary English — this, is, issued, as, a, matter, of, information,
        # only, and, no, upon, the. The lexical tier derives its idf from the whole registry,
        # so a spec that keeps that many common terms in its profile lowers their idf for
        # every doctype already relying on them. Measured over the 59-document reference
        # corpus, adding this doctype with the full legend took ``corpus/ca/ca_cra_noa.pdf``
        # from CORRECT to an abstention: the Canadian notice of assessment and
        # in_itr_acknowledgement swapped places in the lexical channel, the two channels
        # stopped agreeing, and the cascade declined. This spec was never a candidate on that
        # document and never scored on it — it degraded it purely by being in the registry,
        # which is precisely the defect the scale-invariance work exists to prevent.
        #
        # "General Liability" went for the same reason ("general" is on eleven other
        # doctypes) and costs nothing: a certificate that names no coverage line is not a
        # certificate this doctype needs to catch.
        anchors=[
            Anchor(text="confers no rights upon the certificate holder"),
            Anchor(text="Certificate of Insurance"),
            Anchor(text="Certificate Holder"),
            Anchor(text="Named Insured"),
            Anchor(text="Insurer(s) Affording Coverage"),
            Anchor(text="Policy Number"),
            Anchor(text="Policy Effective Date"),
            Anchor(text="Limits of Liability"),
            Anchor(text="Insurance Producer"),
        ],
        id_patterns=[],
        # No country pack models an insurance certificate, so the nearest rival is the other
        # third-party attestation in this module: one page, a named subject, a reference
        # number, an issue date and an expiry, issued by an intermediary rather than by the
        # subject itself.
        confusable_with={
            "xx_iso_certificate": (
                "an ISO certificate attests to an audited management system and names a "
                "standard number; this attests to a policy and names an insurer and limits "
                "of liability"
            ),
        },
        negative_anchors=[],
        handling=(
            "A certificate is evidence that a policy existed on the day it was issued and "
            "nothing more — it confers no rights, it is not the policy, and it says so "
            "itself. It must never be accepted as proof that cover is in force today."
        ),
        fields=[
            build_field(
                "insured_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Named Insured", "Insured Name"]},
                notes="The bare label 'Insured' was dropped: on its own it is a one-word "
                "heading that binds the certificate holder just as readily as the insured, "
                "and the two are usually different parties.",
            ),
            build_field(
                "certificate_holder",
                "",
                kind="name",
                validator="name",
                labels={"en": ["Certificate Holder"]},
                notes="The party the certificate was issued *to*, which is usually the "
                "requesting counterparty rather than the subject of the file — no "
                "attribute key, or the two would merge into one entity.",
            ),
            build_field(
                "insurer",
                "",
                kind="name",
                multi=True,
                validator="name",
                labels={"en": ["Insurer", "Insurer(s) Affording Coverage", "Underwriter"]},
            ),
            build_field(
                "policy_number",
                "doc.reference_number",
                kind="id",
                required=True,
                multi=True,
                labels={"en": ["Policy Number", "Policy No"]},
                notes="Every insurer numbers its own policies; no format can be asserted.",
            ),
            build_field(
                "policy_period",
                "doc.period_covered",
                labels={"en": ["Policy Period", "Policy Effective Date", "Policy Expiry Date"]},
            ),
            build_field(
                "coverage_limits",
                "",
                kind="number",
                multi=True,
                labels={"en": ["Limits of Liability", "Each Occurrence", "General Aggregate"]},
                locators=("table", "label", "kv"),
                notes="The bare labels 'Limit' and 'Aggregate' were dropped: they are single "
                "common words, and a certificate prints a dozen of each in its coverage "
                "grid, so neither can bind to one value.",
            ),
            build_field(
                "issue_date",
                "doc.issue_date",
                kind="date",
                required=True,
                labels={"en": ["Date Issued", "Date (MM/DD/YYYY)"]},
                notes="The bare label 'Date' was dropped. A certificate carries an issue "
                "date, an effective date and an expiry date, so 'Date' alone binds the "
                "wrong one as often as the right one — and, because the lexical tier "
                "derives its idf from the whole registry, a class that keeps a term that "
                "common in its profile degrades every doctype that depends on it.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_iso_certificate",
        label="ISO Management System Certificate",
        country="XX",
        category=Category.other,
        issuing_authority=(
            "A certification body accredited under ISO/IEC 17021 by a national "
            "accreditation body that is an IAF signatory; the standard itself is ISO's"
        ),
        applies_to="corporate",
        officially_valid=False,
        # The standard numbers are ISO's and nobody else's, which is what carries this
        # doctype. Certification bodies design their own certificates, so the layout words
        # ("Certificate of Registration", "Scope") vary — the standard number does not.
        anchors=[
            Anchor(text="ISO 9001"),
            Anchor(text="ISO 14001"),
            Anchor(text="ISO 45001"),
            Anchor(text="ISO/IEC 27001"),
            Anchor(text="Management System Certificate"),
            Anchor(text="Scope of Certification"),
            Anchor(text="Certification Body"),
            Anchor(text="Accreditation"),
            Anchor(text="Surveillance Audit"),
        ],
        id_patterns=[],
        confusable_with={
            "xx_certificate_of_insurance": (
                "both are one-page third-party attestations with a holder, a number and an "
                "expiry; only this one names an ISO standard and a scope"
            ),
            "in_gst_certificate": (
                "a GST registration certificate is a tax authority registering the "
                "enterprise; this is a private body auditing one of its management systems "
                "and confers no registration of any kind"
            ),
        },
        negative_anchors=[],
        handling=(
            "Certificates are frequently forged, and the only real check is the "
            "certification body's own register. Treat an ISO certificate as a claim to be "
            "verified, never as verified evidence."
        ),
        fields=[
            build_field(
                "entity_legal_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Company", "Organisation", "This is to certify that"]},
            ),
            build_field(
                "standard",
                "",
                multi=True,
                required=True,
                labels={"en": ["Standard", "ISO 9001", "ISO 14001", "ISO/IEC 27001"]},
                notes="Capture the edition year too — ISO 9001:2008 certificates are long "
                "expired and a certificate naming a withdrawn edition is a finding.",
            ),
            build_field(
                "scope",
                "",
                required=True,
                labels={"en": ["Scope", "Scope of Certification", "Scope of Registration"]},
                locators=("label", "kv", "table"),
                notes="The scope is what the certificate is actually worth: a company-wide "
                "ISO 27001 and one scoped to a single data centre are different claims.",
            ),
            build_field(
                "certificate_number",
                "doc.reference_number",
                kind="id",
                required=True,
                labels={"en": ["Certificate Number", "Certificate No", "Registration Number"]},
                notes="Each certification body numbers its own certificates however it "
                "likes, so there is no ISO-wide format to enforce. The number is only "
                "meaningful together with the body that issued it.",
            ),
            build_field(
                "certification_body",
                "doc.issuing_authority",
                labels={"en": ["Certification Body", "Issued by", "Registrar"]},
            ),
            build_field(
                "site_address",
                "address.registered",
                kind="address",
                labels={"en": ["Address", "Site", "Location"]},
                locators=("kv", "label"),
            ),
            build_field(
                "issue_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Date of Issue", "Original Certification Date", "Issued"]},
            ),
            build_field(
                "expiry_date",
                "doc.expiry_date",
                kind="date",
                required=True,
                labels={"en": ["Expiry Date", "Valid Until", "Certificate Expiry"]},
                notes="Required because an expired ISO certificate is the single most common "
                "finding, and a missing expiry date is itself a reason to review.",
            ),
        ],
    ),
    DocTypeSpec(
        doctype_id="xx_duns_record",
        label="D-U-N-S Business Record (Dun & Bradstreet)",
        country="XX",
        category=Category.corporate,
        issuing_authority="Dun & Bradstreet",
        applies_to="corporate",
        officially_valid=False,
        # A registered trademark and a proprietary scoring vocabulary. Nothing else prints
        # "D-U-N-S" or "PAYDEX", which makes this the least ambiguous doctype in the module.
        anchors=[
            Anchor(text="D-U-N-S"),
            Anchor(text="D-U-N-S Number"),
            Anchor(text="Dun & Bradstreet"),
            Anchor(text="Business Information Report"),
            Anchor(text="PAYDEX"),
            Anchor(text="D&B Rating"),
            Anchor(text="Failure Score"),
        ],
        id_patterns=[DUNS_PATTERN],
        confusable_with={
            "us_certificate_good_standing": (
                "a good-standing certificate is a state registrar's statement that the "
                "entity exists and is current; this is a credit bureau's file on it and "
                "proves nothing about its legal status"
            ),
            "xx_lei_certificate": (
                "both are global identifiers attached to an existing company; the D-U-N-S "
                "is a commercial credit-file key and the LEI a regulatory one, and neither "
                "is evidence of legal existence"
            ),
        },
        negative_anchors=[],
        handling=(
            "A commercial credit file, licensed rather than published. The scores in it are "
            "D&B's opinion and are not facts about the entity — never restate a PAYDEX or "
            "failure score as though the registry had verified it."
        ),
        fields=[
            build_field(
                "duns_number",
                "id.duns",
                kind="id",
                required=True,
                pattern=DUNS_PATTERN,
                labels={"en": ["D-U-N-S Number", "DUNS", "D-U-N-S"]},
                locators=("label", "kv", "regex"),
                notes="Nine digits, printed NN-NNN-NNNN by D&B and often unhyphenated "
                "elsewhere. The pattern matches the hyphenated form only: a bare nine-digit "
                "run matches half the identifiers on earth and would bind anything.",
            ),
            build_field(
                "entity_legal_name",
                "entity.legal_name",
                kind="name",
                required=True,
                labels={"en": ["Business Name", "Legal Name", "Company Name"]},
            ),
            build_field(
                "trade_name",
                "entity.trade_name",
                kind="name",
                labels={"en": ["Trading As", "Trade Style", "Doing Business As"]},
            ),
            build_field(
                "registered_address",
                "address.registered",
                kind="address",
                labels={"en": ["Address", "Registered Address", "Principal Address"]},
                locators=("kv", "label"),
            ),
            build_field(
                "country_of_incorporation",
                "entity.jurisdiction",
                labels={"en": ["Country", "Jurisdiction", "State of Incorporation"]},
            ),
            build_field(
                "year_started",
                "entity.incorporation_date",
                kind="date",
                labels={"en": ["Year Started", "Date Started", "Incorporated"]},
                notes="D&B records the year the business *started trading*, which is not the "
                "incorporation date and often precedes it by years.",
            ),
            build_field(
                "report_date",
                "doc.issue_date",
                kind="date",
                labels={"en": ["Report Date", "Date", "As of"]},
            ),
        ],
    ),
]

# ---------------------------------------------------------------------------
# Deliberately NOT modelled here, and why. Each of these was drafted and dropped;
# the note exists so the next author does not spend the afternoon rediscovering it.
#
# *Group ownership structure chart.* A diagram. The only text on it is company names,
# jurisdictions and percentages, all of which are chosen by whoever drew it — there is no
# issuer-controlled string, in any language, at any point on the page. A doctype anchored on
# "Group Structure" or "Organisation Chart" would fire on any slide deck with a box-and-line
# diagram, which is a confident wrong answer, and abstention is the correct outcome for a
# document this service genuinely cannot recognise from text.
#
# *A standalone IFRS/ISA auditor's report.* Drafted, then merged into
# ``xx_audited_financial_statements``. The report is bound with the statements it reports on
# in every real submission, so two doctypes would split one document's evidence between them
# and both would fall below the margin — an abstention engineered into the registry rather
# than earned by the document.
# ---------------------------------------------------------------------------

register_all(_SPECS)

#: Every cross-country doctype, in declaration order.
SPECS: tuple[DocTypeSpec, ...] = tuple(_SPECS)
