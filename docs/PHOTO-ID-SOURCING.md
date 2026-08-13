# Photo ID sourcing checklist

The corpus test covers blank forms well, because issuers publish them. Photo IDs are the
gap: there is no blank W-9 equivalent for a passport. This is the list of what we still
need, what a *usable* sample looks like, and where issuers publish official specimens.

## The rule for anything you add

**Only specimens with fabricated data.** Never a real person's card — not yours, not a
colleague's, not one from an image search. Every ID below is regulated personal data
(Aadhaar Act, LFPDPPP in Mexico, PIPEDA in Canada, state ID laws in the US), and a real
card in a test corpus is a breach waiting to be found in a git history.

Acceptable sources, in order of preference:

1. **Issuer specimen** — the "what the new card looks like" page most issuers publish. INE,
   USCIS, IRCC and several US DMVs all do.
2. **Synthetic sample** — a card mock-up made for training/awareness material.
3. **Redacted-and-fabricated** — a real layout with every value replaced by fake data. If
   you make these, keep the *layout and printed text* intact: that is what the classifier
   reads. The values only matter for extraction.

Drop files in `corpus/<cc>/` named `<doctype_id>.pdf|png|jpg` and add a line to
`corpus/<cc>/manifest.jsonl` (format in `tools/README-corpus.md`).

## What makes a sample valuable to us

The classifier keys on **printed issuer text**, not on the photo. So a usable sample must
show the card's headers legibly — the values can be nonsense.

| Signal | Why it matters |
|---|---|
| **Issuing-authority header** | This is the *decisive anchor*. `INSTITUTO NACIONAL ELECTORAL`, `UNIQUE IDENTIFICATION AUTHORITY OF INDIA`. One of these alone classifies the document. |
| **MRZ** (the `<<<<` block) | The single best signal in KYC — four check digits make extraction *checksum-verified* rather than guessed. Any MRZ-bearing sample is worth 3 non-MRZ ones. |
| **Bilingual text** | 123 Devanagari and 58 French anchors have never seen a real document. A Hindi/English Aadhaar or an EN/FR Canadian card tests a whole untested path. |
| **Both sides** | Several cards print the authority on the back (Aadhaar, INE, PR card). Front-only samples under-test. |

---

## Read this before sourcing passports

The three passports classify on their **MRZ line, not on any printed word**: the decisive
anchors are literally `P<USA`, `P<CAN`, `P<MEX`. A passport specimen where the MRZ is
cropped off, blurred, or covered by a "SPECIMEN" overprint **will not classify** — it will
fall through to the generic `xx_passport_generic` or abstain. When you pick a passport
sample, check the two `<<<<` lines at the bottom of the data page are sharp and complete.

Indian passports are the exception — `in_passport` anchors on `REPUBLIC OF INDIA` /
`भारत गणराज्य`, so the printed page is enough.

---

## India — 8 photo IDs (all 4 OVDs are here; highest priority)

The registry carries 123 Devanagari anchors that have **never seen a real document**. Any
bilingual Indian specimen is disproportionately valuable.

| Doctype | Card | Must show (decisive anchor) | Where | Pri |
|---|---|---|---|---|
| `in_aadhaar` | Aadhaar / e-Aadhaar | `UNIQUE IDENTIFICATION AUTHORITY OF INDIA` · `भारतीय विशिष्ट पहचान प्राधिकरण` · `AADHAAR` · `आधार` | UIDAI sample e-Aadhaar. **PDF with a text layer** — the easiest real test we can get | ★★★ |
| `in_aadhaar_masked` | Masked e-Aadhaar | `Masked Aadhaar` | Same flow, masked option. Confirms `XXXXXXXX1234` isn't scored as a failed checksum | ★★★ |
| `in_pan` | PAN card | `INCOME TAX DEPARTMENT` · `आयकर विभाग` · `PERMANENT ACCOUNT NUMBER CARD` | ITD / NSDL specimen; e-PAN is a PDF | ★★★ |
| `in_voter_epic` | Voter ID (EPIC) | `ELECTION COMMISSION OF INDIA` · `भारत निर्वाचन आयोग` · `ELECTOR PHOTO IDENTITY CARD` | ECI specimen incl. the newer PVC card | ★★★ |
| `in_passport` | Indian Passport | `REPUBLIC OF INDIA` · `भारत गणराज्य` | Passport Seva specimen data page. 17 fields — our richest schema | ★★★ |
| `in_driving_licence` | Driving Licence | `DRIVING LICENCE` · `चालक अनुज्ञप्ति` | State RTO / Parivahan. **Weakest anchor set on this page** — a generic phrase, no checksum, no national format. Highest value per sample | ★★ |
| `in_nrega_job_card` | MGNREGA Job Card | `MAHATMA GANDHI NATIONAL RURAL EMPLOYMENT GUARANTEE ACT` · `JOB CARD` | State NREGA portals | ★ |
| `in_ration_card` | Ration Card | `RATION CARD` · `राशन कार्ड` | State PDS portals; format varies a lot by state | ★ |

**Aadhaar handling:** the UIDAI masking obligation is encoded in the registry — we persist
only the last 4 digits. A masked specimen is *more* useful to us than a full one, not less.

## United States — 8 IDs

| Doctype | Card | Must show (decisive anchor) | Where | Pri |
|---|---|---|---|---|
| `us_passport` | Passport book | `P<USA` **(MRZ — see the warning above)** | State Dept sample data page | ★★★ |
| `us_green_card` | Permanent Resident Card (I-551) | `PERMANENT RESIDENT CARD` | USCIS card-design specimens. MRZ + 11 fields | ★★★ |
| `us_drivers_license` | State DL | `DRIVER LICENSE` · `DRIVER'S LICENSE` | DMV "your new licence looks like this" pages. **Get 3–4 states** — no national format, highest US volume | ★★★ |
| `us_passport_card` | Passport card | `PASSPORT CARD` · `I<USA` | State Dept specimen | ★★ |
| `us_state_id` | Non-driver ID | `IDENTIFICATION CARD` | Same DMV pages | ★★ |
| `us_ead` | Employment Authorization (I-766) | `EMPLOYMENT AUTHORIZATION CARD` · `I-766` | USCIS specimen | ★★ |
| `us_ssn_card` | Social Security Card | `SOCIAL SECURITY ADMINISTRATION` | SSA specimen. No photo, same sourcing problem | ★★ |
| `us_military_id` | CAC / Uniformed Services | `COMMON ACCESS CARD` · `UNIFORMED SERVICES IDENTIFICATION CARD` | DoD/DMDC specimen | ★ |

**RESOLVED — `us_real_id` vs `us_drivers_license`: merged, and there is nothing left to
source.** This section used to ask for a REAL ID specimen as the test of whether the two
doctypes had to be merged. The specimen arrived (Virginia DMV's AAMVA calibration sheet,
`corpus/us/us_real_id.pdf`) and the answer was yes.

The prediction above was right about the symptom and understated the cause. A REAL ID is not
merely *similar* to a driver's licence — it **is** one. The REAL ID Act of 2005 and 6 CFR
Part 37 set minimum standards for a state-issued licence or ID card; 6 CFR 37.17(n) adds the
star marking and leaves the card's title alone. So the REAL ID set is a strict *subset* of
`us_drivers_license` ∪ `us_state_id`, and no anchor can separate a subset from its own
superset — the superset's issuer prints everything the subset does. The specimen confirmed
even the programme name is shared: the sheet for the **non**-compliant Standard licence also
prints "REAL ID", in the legend explaining AAMVA element `DDA` (`F` = fully compliant,
`N` = non-compliant).

`us_real_id` is therefore gone, and compliance survives as the `real_id_compliant` boolean
field on both `us_drivers_license` and `us_state_id` — which is the fact a KYC reviewer
actually needs. **Do not source a "REAL ID" specimen.** What is still worth sourcing is more
state driver's licences and non-driver IDs (row 3 and row 5 above), a mix of compliant and
non-compliant, so the field has something to extract from.

## Canada — 9 IDs (bilingual EN/FR — exercises 58 untested French anchors)

| Doctype | Card | Must show (decisive anchor) | Where | Pri |
|---|---|---|---|---|
| `ca_passport` | Canadian Passport | `P<CAN` **(MRZ — see the warning above)** | IRCC specimen data page | ★★★ |
| `ca_pr_card` | PR Card | `CARTE DE RÉSIDENT PERMANENT` · `RÉSIDENT PERMANENT` | IRCC card-design specimen. **French-only anchors** — an English-side scan will not classify | ★★★ |
| `ca_drivers_license` | Provincial DL | `DRIVER'S LICENCE` · `PERMIS DE CONDUIRE` | ServiceOntario, SAAQ, ICBC… **Get 2–3 provinces** | ★★★ |
| `ca_citizenship_certificate` | Citizenship Certificate | `CERTIFICATE OF CANADIAN CITIZENSHIP` · `CERTIFICAT DE CITOYENNETÉ CANADIENNE` | IRCC specimen | ★★ |
| `ca_provincial_photo_id` | Non-driver photo ID | `ONTARIO PHOTO CARD` · `ALBERTA IDENTIFICATION CARD` · `MANITOBA IDENTIFICATION CARD` | Provincial sites. **Only 3 provinces are covered** — a BC/QC/NS card will abstain today | ★★ |
| `ca_secure_status_card` | Secure Certificate of Indian Status | `SECURE CERTIFICATE OF INDIAN STATUS` · `CERTIFICAT SÉCURISÉ DE STATUT D'INDIEN` | ISC card design | ★ |
| `ca_refugee_protection_doc` | IMM 1442 | `REFUGEE PROTECTION CLAIMANT DOCUMENT` · `IMM 1442` | IRCC sample | ★ |
| `ca_health_card` | Provincial health card | `ONTARIO HEALTH CARD` · `CARTE D'ASSURANCE MALADIE` · `RÉGIE DE L'ASSURANCE MALADIE DU QUÉBEC` | Provincial health ministries. Several provinces *prohibit* its use as ID — good test that we classify it and the policy layer still refuses it | ★ |
| `ca_nexus` | NEXUS card | `NEXUS` | CBSA/CBP specimen. **One short generic token** — weakest anchor in the whole registry | ★ |

## Mexico — 6 IDs

| Doctype | Card | Must show (decisive anchor) | Where | Pri |
|---|---|---|---|---|
| `mx_ine` | Credencial para Votar | `INSTITUTO NACIONAL ELECTORAL` · `CREDENCIAL PARA VOTAR` · `INSTITUTO FEDERAL ELECTORAL` | **INE publishes specimens of every series (A–H)** — the best-documented issuer here. Get several: the layout changed materially across them, and the old `IFE` anchor is there for the pre-2014 cards | ★★★ |
| `mx_passport` | Pasaporte Mexicano | `P<MEX` · `PASAPORTE` | SRE specimen | ★★★ |
| `mx_tarjeta_residente` | Tarjeta de Residente | `TARJETA DE RESIDENTE PERMANENTE` · `TARJETA DE RESIDENTE TEMPORAL` · `INSTITUTO NACIONAL DE MIGRACIÓN` | INM specimen. Temporal and permanente are separate anchors — either is fine | ★★ |
| `mx_matricula_consular` | Matrícula Consular (MCAS) | `MATRÍCULA CONSULAR DE ALTA SEGURIDAD` | SRE specimen | ★★ |
| `mx_cedula_profesional` | Cédula Profesional | `CÉDULA PROFESIONAL` · `DIRECCIÓN GENERAL DE PROFESIONES` | SEP electronic cédula | ★ |
| `mx_cartilla_militar` | Cartilla Militar | `CARTILLA DEL SERVICIO MILITAR NACIONAL` · `SECRETARÍA DE LA DEFENSA NACIONAL` | SEDENA sample | ★ |

**Accents matter.** Every Mexican anchor above carries diacritics. We fold accents before
matching, but that path has only ever been tested on text I wrote — a real `CÉDULA` /
`MIGRACIÓN` off an OCR engine is the first genuine test of it.

---

## If you can only get a handful

Ranked by what would most reduce our uncertainty:

1. **`in_aadhaar` + `in_aadhaar_masked` (e-Aadhaar PDFs)** — digital PDFs with a text layer,
   bilingual, and they exercise the Devanagari path, the Verhoeff validator and the masking
   rule in one document.
2. **`mx_ine`** — the specimen set is public and the decisive anchor is unambiguous; good
   confirmation that the Spanish accent-folding path works on real text.
3. **Any passport (`in_`/`us_`/`ca_`/`mx_`)** — MRZ is the only path in the system that
   produces checksum-verified extraction, and it is completely untested against a real zone.
4. **2–3 US state driver's licences from different states** — highest-volume US document,
   no national format, so our anchors are the shakiest here.
5. **`us_green_card` or `ca_pr_card`** — MRZ plus a bilingual/AAMVA-style layout.

## What we'll do with them

Drop them in and run:

```bash
python tools/corpus_test.py --country in --verbose
```

The report gives per-document CORRECT/WRONG/ABSTAINED with confidence, margin and the
runners-up, plus the extraction fill-rate. Scanned images (no text layer) currently record
as `needs_ocr` and skip — wiring them through Azure Read/Layout is a small change once we
know how many samples actually need it.
