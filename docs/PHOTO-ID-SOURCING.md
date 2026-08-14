# Photo ID sourcing — what to gather, and what makes a specimen usable

Blank forms are easy: issuers publish them. Photo IDs are the gap, because there is no blank
W-9 equivalent for a passport. This is what to collect, in priority order, and what each
specimen has to show to be worth having.

**Scope: US, Canada, Mexico.** Indian doctypes were removed from the registry; the Indian
entries that used to be here live on the `archive/india-doctypes` branch.

---

## The rule for anything you add

**Only specimens with fabricated data.** Never a real person's card — not yours, not a
colleague's, not one from an image search. Every ID below is regulated personal data (state
ID laws in the US, PIPEDA in Canada, LFPDPPP in Mexico), and a real card in a test corpus is
a breach waiting to be found in a git history. Three files were already deleted from this
repo for carrying real individuals' identifiers.

Acceptable sources, best first:

1. **Issuer specimen** — the "what the new card looks like" page most issuers publish. USCIS,
   IRCC, INE and several US DMVs all do.
2. **Synthetic sample** — a card mock-up made for training or awareness material.
3. **Redacted-and-fabricated** — a real layout with every value replaced. If you make these,
   keep the *printed layout text* intact: that is what the classifier reads. The values only
   matter for extraction.

Drop files in `corpus/<cc>/` named `<doctype_id>.pdf|png|jpg` and add one line to
`corpus/<cc>/manifest.jsonl` (format in `tools/README-corpus.md`).

---

## What makes a specimen valuable

The classifier reads **printed issuer text**, not the photograph. So the values can be
nonsense, but the card's own wording has to be legible.

| Signal | Why it matters |
|---|---|
| **Issuing-authority header** | The decisive anchor. `INSTITUTO NACIONAL ELECTORAL` alone classifies a card. |
| **MRZ** (the `<<<<` block) | The best signal in KYC — check digits make extraction *checksum-verified* rather than guessed. One MRZ specimen is worth three without. |
| **Bilingual EN/FR text** | 58 French anchors have barely been exercised. A Canadian card tests a whole path. |
| **Both sides** | Several cards print the authority on the back (INE, PR card). Front-only under-tests. |

### Read this before sourcing passports

The three passports classify on their **MRZ line, not on any printed word** — the decisive
anchors are literally `P<USA`, `P<CAN`, `P<MEX`. A specimen whose MRZ is cropped, blurred, or
covered by a SPECIMEN overprint **will not classify**; it falls through to
`xx_passport_generic` or abstains. Check the two `<<<<` lines at the bottom of the data page
are sharp and complete before keeping a passport sample.

---

## Priority 1 — get these first

Ranked by what most reduces uncertainty, not by how easy they are.

| # | Doctype | Card | Must show | Where |
|---|---|---|---|---|
| 1 | `mx_ine` | Credencial para Votar | `INSTITUTO NACIONAL ELECTORAL` · `CREDENCIAL PARA VOTAR` | **INE publishes specimens of every series A–H** — the best-documented issuer here. Get several: the layout changed materially across series, and the old `INSTITUTO FEDERAL ELECTORAL` anchor covers pre-2014 cards |
| 2 | `us_drivers_license` | State DL | `DRIVER LICENSE` · `DRIVER'S LICENSE` | DMV "your new licence looks like this" pages. **Get 3–4 different states.** No national format, highest US volume, and the weakest anchors of any high-volume US document |
| 3 | `ca_pr_card` | PR Card | `CARTE DE RÉSIDENT PERMANENT` · `RÉSIDENT PERMANENT` | IRCC card-design specimen. **French-only anchors — an English-side scan will not classify.** Also carries an MRZ |
| 4 | `us_passport` | Passport book | `P<USA` (MRZ) | State Dept sample data page |
| 5 | `us_green_card` | Permanent Resident Card (I-551) | `PERMANENT RESIDENT CARD` | USCIS card-design specimen. MRZ + 11 fields |
| 6 | `ca_passport` | Canadian Passport | `P<CAN` (MRZ) | IRCC specimen data page |
| 7 | `mx_passport` | Pasaporte Mexicano | `P<MEX` · `PASAPORTE` | SRE specimen |

**Why this order.** `mx_ine` because the specimen set is public and complete, so it is the
cheapest real win. US driver's licences because they are the highest-volume US KYC document
and there is no national format, which makes our anchors shakiest exactly where volume is
highest. `ca_pr_card` because its anchors are French-only and that path is almost untested —
this specimen would tell us whether 58 French anchors work at all.

## Priority 2 — worth having

| Doctype | Card | Must show | Where |
|---|---|---|---|
| `us_state_id` | Non-driver ID | `IDENTIFICATION CARD` | Same DMV pages as the licence |
| `ca_drivers_license` | Provincial DL | `DRIVER'S LICENCE` · `PERMIS DE CONDUIRE` | ServiceOntario, SAAQ, ICBC — **2–3 provinces** |
| `us_passport_card` | Passport card | `PASSPORT CARD` · `I<USA` | State Dept specimen |
| `us_ead` | Employment Authorization (I-766) | `EMPLOYMENT AUTHORIZATION CARD` · `I-766` | USCIS specimen |
| `mx_tarjeta_residente` | Tarjeta de Residente | `TARJETA DE RESIDENTE PERMANENTE` / `TEMPORAL` · `INSTITUTO NACIONAL DE MIGRACIÓN` | INM specimen. Either temporal or permanente is fine |
| `mx_matricula_consular` | Matrícula Consular | `MATRÍCULA CONSULAR DE ALTA SEGURIDAD` | SRE specimen |
| `ca_citizenship_certificate` | Citizenship Certificate | `CERTIFICATE OF CANADIAN CITIZENSHIP` · `CERTIFICAT DE CITOYENNETÉ CANADIENNE` | IRCC specimen |
| `us_ssn_card` | Social Security Card | `SOCIAL SECURITY ADMINISTRATION` | SSA specimen. No photo, same sourcing problem |

## Priority 3 — completeness

`ca_provincial_photo_id` (`ONTARIO PHOTO CARD` / `ALBERTA IDENTIFICATION CARD` /
`MANITOBA IDENTIFICATION CARD` — **only these three provinces are covered; a BC or QC card
abstains today**) · `ca_secure_status_card` · `ca_nexus` (anchor is the single token `NEXUS`,
the weakest in the registry) · `ca_health_card` (several provinces prohibit its use as ID —
a good test that we classify it and the policy layer still refuses it) ·
`ca_refugee_protection_doc` (`IMM 1442`) · `us_military_id` · `mx_cedula_profesional` ·
`mx_cartilla_militar`.

---

## Two things not to bother with

**Do not source a "REAL ID" specimen.** `us_real_id` was merged into `us_drivers_license`. The
REAL ID Act (6 CFR Part 37) sets standards for a state licence or ID card and creates no
separate credential — so the REAL ID set is a strict *subset* of licence ∪ ID card, and no
anchor can separate a subset from its own superset. Compliance is now the `real_id_compliant`
field. What *is* worth sourcing is more state licences and non-driver IDs, **a mix of
compliant and non-compliant**, so that field has something to extract from.

**Scanned images are fine now.** Earlier this list warned that image-only specimens could not
be scored. The OCR path works: images go through the recogniser and classify. Two caveats
worth knowing — OCR of a photo ID is often too degraded for the lexical channel to corroborate
the anchors, so a scan may abstain where a text-layer PDF would classify; and a PDF **with a
text layer still beats an image** every time. Prefer one where the issuer offers it.

---

## What happens when you drop them in

```bash
cd /Users/neelu/document-classification-extraction && python tools/corpus_test.py --ingest
```

Per document you get CORRECT / WRONG / ABSTAINED, with confidence, margin, coverage and the
runners-up. Two things to read rather than skim:

- **An abstention is not a failure.** It routes to a human, which is the designed safe
  outcome. What matters is *why* — the report names the gate that refused.
- **OCR'd documents are counted on their own line** (`service_ingest_ocr`), because a wrong
  answer on a recognised page may be the recogniser's fault rather than the classifier's.

For a document that abstains, this shows which channel disagreed with which:

```bash
cd /Users/neelu/document-classification-extraction && python tools/channel_probe.py us/us_passport.jpg mx/mx_ine.pdf
```
