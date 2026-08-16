/**
 * Wire types for the DCE API.
 *
 * DERIVED FROM THE RUNNING SERVICE. Every interface below is a transcription of a schema in
 * `components.schemas` of `http://localhost:8200/openapi.json`, fetched from the container on
 * :8200 — not from memory of the Pydantic models. If you change a model in `dce/api/routes.py`
 * or `dce/models.py`, re-fetch and re-derive:
 *
 *     curl -s http://localhost:8200/openapi.json | python3 -m json.tool
 *
 * Conventions used in the transcription:
 *  - a Pydantic field with a default is REQUIRED on the wire (the server always emits it), so
 *    it is non-optional here; a field typed `X | None` with no default is `X | null` and
 *    optional.
 *  - `additionalProperties: true` objects become `Record<string, unknown>`.
 *  - fixed-length numeric arrays (bbox, runners_up) keep their tuple shape where the schema
 *    pins the length.
 */

/* ------------------------------------------------------------------ enums */

/** `Zone` — where on the page a piece of text sits. Drives lexical weighting. */
export type Zone = 'title' | 'heading' | 'body' | 'table' | 'furniture';
export const ZONES: readonly Zone[] = ['title', 'heading', 'body', 'table', 'furniture'];

/** `Category` — the registry's coarse grouping of a doctype. */
export type Category =
  | 'identity'
  | 'address_proof'
  | 'tax'
  | 'corporate'
  | 'financial'
  | 'other';
export const CATEGORIES: readonly Category[] = [
  'identity',
  'address_proof',
  'tax',
  'corporate',
  'financial',
  'other',
];

/**
 * `TierRun.status`. Not an enum in the schema (plain `str`), but the server only produces
 * these five. Treat an unknown value as informational rather than as an error.
 */
export type TierRunStatus = 'ran' | 'error' | 'unavailable' | 'misconfigured' | 'skipped' | 'queued';

/** `ReviewItem.status`. Also a plain `str` on the wire; the memory/file queues emit these. */
export type ReviewStatus = 'pending' | 'approved' | 'rejected' | 'corrected';

/** `ExtractedField.verification`. Plain `str`; `unverified` is the default. */
export type Verification = string;

/** A bbox is `[x, y, w, h]` in the page's own units (`PageInfo.unit`). */
export type BBox = number[];

/* ------------------------------------------------------- layout (input side) */

/** `PageInfo` */
export interface PageInfo {
  page: number;
  width: number;
  height: number;
  /** e.g. `pixel`, `inch`. */
  unit: string;
  angle: number;
}

/** `TextBlock` — one paragraph/line of text with its zone and geometry. */
export interface TextBlock {
  text: string;
  zone: Zone;
  page: number;
  bbox?: BBox | null;
  role?: string | null;
}

/** `Cell` */
export interface Cell {
  row: number;
  col: number;
  row_span: number;
  col_span: number;
  text: string;
  is_header: boolean;
  bbox?: BBox | null;
}

/** `Table` */
export interface Table {
  table_id: string;
  page: number;
  row_count: number;
  col_count: number;
  cells: Cell[];
  bbox?: BBox | null;
}

/** `Mark` — a checkbox / radio. On a KYC form, which box is ticked is often the answer. */
export interface Mark {
  state: string;
  page: number;
  bbox?: BBox | null;
}

/** `KeyValue` — a provider-detected key/value pair (Azure `features=keyValuePairs`). */
export interface KeyValue {
  key: string;
  value: string;
  page: number;
  key_bbox?: BBox | null;
  value_bbox?: BBox | null;
  confidence?: number | null;
}

/** `LayoutView` — everything the classifier and extractors are allowed to see. */
export interface LayoutView {
  doc_id: string;
  pages: PageInfo[];
  blocks: TextBlock[];
  tables: Table[];
  marks: Mark[];
  key_values: KeyValue[];
  languages: string[];
  raw: Record<string, unknown>;
}

/* --------------------------------------------------------------- requests */

/**
 * `IngestOptions` — per-request ingestion options.
 *
 * `local_ocr` can turn local OCR OFF for one request but can never turn it ON where the
 * operator has not enabled it. `filename` is a hint for plain-text subtypes only; it can never
 * choose a binary parser.
 */
export interface IngestOptions {
  filename?: string | null;
  local_ocr?: boolean | null;
  /**
   * Which recognition provider to read an image with, by the service's own name for it.
   *
   * NOT on `/openapi.json` at the time of writing — this is the field the service-side provider
   * work is adding, and the console sends it only for a provider `/readyz` has told us exists
   * here (see `src/ocr.ts`). It is a *request*, never a grant: naming a remote provider on a
   * deployment that has not enabled one must be refused server-side, exactly as `local_ocr: true`
   * is, and the console renders that refusal rather than working around it.
   */
  ocr_provider?: string | null;
  /**
   * How the document should be READ, before anything classifies it.
   *
   * `auto` is what every caller got before this existed: the text layer where the file has one,
   * recognition where it does not. `lexical` takes the text layer only, so a scan comes back
   * `needs_ocr` even where a recogniser is willing. `optical` recognises the page even when a
   * text layer is sitting right there.
   *
   * The last is the one worth having. A PDF with a text layer can be read both ways and the two
   * readings are not the same document: the text layer has no paragraph roles, so a zone-gated
   * anchor cannot fire on it, while Document Intelligence supplies roles and it can. Same bytes,
   * different evidence, sometimes a different doctype — and the only way to see that is to run
   * both.
   */
  read_channel?: 'auto' | 'lexical' | 'optical';
}

/**
 * `DocumentRequest` — one document, in whichever form the caller has it.
 *
 * Exactly one payload field is used, in this order: `layout` (already adapted),
 * `azure_analyze_result`, `des_ocr`, `text`.
 *
 * THE RAW-FILE FIELD IS `content_base64` (base64 of the original bytes, no data: prefix), and
 * on its own it is *carried, not read* — it exists so the paid Azure tiers can send the file
 * after a doctype is accepted. To have the bytes parsed in this process into a layout you must
 * also send `ingest` (even `ingest: {}`). See `api.documentRequestFromFile`.
 */
export interface DocumentRequest {
  doc_id?: string;
  layout?: LayoutView | null;
  text?: string | null;
  azure_analyze_result?: Record<string, unknown> | null;
  des_ocr?: Record<string, unknown> | null;
  content_base64?: string | null;
  ingest?: IngestOptions | null;
}

/** `ExtractRequest` — a document plus an optional doctype pin. */
export interface ExtractRequest extends DocumentRequest {
  /** Omit to have the document classified first; an abstention returns an empty result. */
  doctype_id?: string | null;
  schema_version?: string | null;
}

/** `InduceRequest` — sample documents to draft a schema from. */
export interface InduceRequest {
  doctype_id: string;
  label?: string;
  country?: string;
  /** minItems: 1. */
  samples: DocumentRequest[];
  /** 0..1, default 0.5. Fraction of samples a candidate field must appear in. */
  min_support?: number;
}

/**
 * `ReviewDecision` — a human's decision on one queue item, which is one FIELD of one document.
 *
 * `reviewer` is required and has no default: an unattributed decision in a KYC system is not a
 * decision, and blind double entry is meaningless without two distinct identities.
 */
export interface ReviewDecision {
  reviewer: string;
  note?: string;
  /** Required (non-empty) for `correct`; ignored by approve/reject. */
  value?: string;
}

/* -------------------------------------------------------------- responses */

/** `Evidence` — why the classifier believed something. Always populated. */
export interface Evidence {
  tier: string;
  detail: string;
  weight: number;
}

/** `[doctype_id, score]`. */
export type RunnerUp = [string, number];

/** `Classification` — the decision, and everything needed to defend it. */
export interface Classification {
  doctype_id: string;
  label: string;
  country: string;
  confidence: number;
  margin: number;
  coverage: number;
  /** True when the cascade declined to decide. A routed abstention, NOT an error. */
  abstained: boolean;
  /** Which gate failed and why, when `abstained`. Empty on an accept. */
  reason: string;
  evidence: Evidence[];
  runners_up: RunnerUp[];
  page_types: string[];
  ms: number;
}

/** `ExtractedField` */
export interface ExtractedField {
  name: string;
  attribute_key: string;
  value?: string | null;
  normalized?: string | null;
  confidence: number;
  /** e.g. `unverified`, `checksum_ok`, `checksum_failed`, `pattern_ok`. */
  verification: Verification;
  /** Provenance: which tier/strategy produced the value. */
  locator: string;
  page?: number | null;
  bbox?: BBox | null;
  /** Mask by default in any UI. */
  pii: boolean;
  validator_error: string;
}

/** `ExtractionResult` */
export interface ExtractionResult {
  doctype_id: string;
  schema_version: string;
  fields: ExtractedField[];
  missing_required: string[];
  needs_review: boolean;
  ms: number;
}

/** `Timings` — server-side, milliseconds. Mirrored on the `X-Elapsed-Ms` header. */
export interface Timings {
  total_ms: number;
  adapt_ms: number;
  classify_ms: number;
  extract_ms: number;
  /** Time in the paid tiers (T2-T4). Zero on a default deployment. */
  tiers_ms: number;
}

/** `TierRun` — what one extraction tier did. Absent tiers spent nothing. */
export interface TierRun {
  tier: string;
  status: TierRunStatus;
  fields_filled: number;
  fields: string[];
  ms: number;
  /** True when somebody gets billed. An `error` after the call still counts. */
  cost_bearing: boolean;
  detail: string;
}

/** `ProcessResponse` — classification plus extraction, or classification alone. */
export interface ProcessResponse {
  classification: Classification;
  extraction?: ExtractionResult | null;
  needs_review: boolean;
  detail: string;
  tiers_used: TierRun[];
  review_ids: string[];
  timings: Timings;
}

/* ------------------------------------------------------------- segmenting */

/** Why one split was proposed, so a segmentation can be argued with rather than trusted. */
export interface BoundaryEvidence {
  /** First page of the new document, 1-based. */
  page: number;
  /** `adequacy` | `geometry` | `first_page_anchor`. */
  signal: string;
  detail: string;
}

/** One document found inside an upload. */
export interface DocumentSegment {
  start_page: number;
  end_page: number;
  page_count: number;
  /** The classification of *these pages alone*, from classifying the span whole. */
  classification: Classification;
  /**
   * Present on `/process/segments`; null when the span abstained — nothing is extracted from
   * a document nobody has identified.
   */
  extraction?: ExtractionResult | null;
  needs_review: boolean;
  /** What ran for THIS document. Per segment, because a bundle's tiers are per document:
   *  one segment can abstain and run nothing while its neighbour extracts seven fields. */
  tiers_used: TierRun[];
}

/**
 * What an upload turned out to contain.
 *
 * `segments` always holds at least one entry: a file with no boundary evidence comes back as
 * one segment covering every page. That uniformity is the point — nothing in the console has
 * to branch on whether the user happened to pick a bundle.
 */
/**
 * How one page was read.
 *
 * "Is the view right?" has to be answerable before "is the classifier right?", and a page
 * that contributed nothing to a classification is otherwise indistinguishable from a page
 * that genuinely held nothing.
 */
export interface PageRead {
  page: number;
  width: number;
  height: number;
  alnum_chars: number;
  /** `null` means nothing measured it — not the same as `false`. */
  text_adequate: boolean | null;
  image_fraction: number;
}

export interface SegmentsResponse {
  segments: DocumentSegment[];
  /** The plain answer to "is this a bundle?", so it need not be inferred from a list length. */
  segmented: boolean;
  boundaries: BoundaryEvidence[];
  pages: PageRead[];
  page_count: number;
  ms: number;
}

/* --------------------------------------------------------------- registry */

/** `Anchor` — a high-signal string that appears in this doctype's OCR dump. */
export interface Anchor {
  text: string;
  lang: string;
  /** A decisive anchor can carry the decision on its own. */
  decisive: boolean;
  zone?: Zone | null;
}

/** `FieldSpec` — one extractable field on a document type. */
export interface FieldSpec {
  name: string;
  attribute_key: string;
  type: string;
  required: boolean;
  pii: boolean;
  multi: boolean;
  /** lang code -> label synonyms. */
  labels: Record<string, string[]>;
  pattern?: string | null;
  validator?: string | null;
  locators: string[];
  notes: string;
}

/** `DocTypeSummary` — one registry entry, as listed by `GET /api/v1/doctypes`. */
export interface DocTypeSummary {
  doctype_id: string;
  label: string;
  country: string;
  category: Category;
  issuing_authority: string;
  applies_to: string;
  officially_valid: boolean;
  /** Count, not the anchors themselves. */
  anchors: number;
  /** Field names only. */
  fields: string[];
}

/** `DocTypeSpec` — how to recognise a doctype, and what to pull out of it. */
export interface DocTypeSpec {
  doctype_id: string;
  label: string;
  country: string;
  category: Category;
  issuing_authority: string;
  applies_to: string;
  officially_valid: boolean;
  anchors: Anchor[];
  id_patterns: string[];
  /** other doctype_id -> how to tell them apart. */
  confusable_with: Record<string, string>;
  negative_anchors: string[];
  fields: FieldSpec[];
  handling: string;
}

/** `DocTypeListResponse` */
export interface DocTypeListResponse {
  count: number;
  doctypes: DocTypeSummary[];
  timings: Timings;
}

/** `SchemaResponse` — `active` is false for a freshly induced draft. */
export interface SchemaResponse {
  doctype_id: string;
  schema_version: string;
  active: boolean;
  /** e.g. `registry`, `induced`. */
  source: string;
  label: string;
  country: string;
  fields: FieldSpec[];
  sample_count: number;
  notes: string;
  timings: Timings;
}

/* ----------------------------------------------------------------- review */

/**
 * `ReviewItem` — one FIELD of one document waiting for (or already seen by) a human.
 *
 * The model is `extra="allow"` on the server, so unknown keys are passed through; the index
 * signature preserves them rather than dropping whatever the queue added last week.
 */
export interface ReviewItem {
  id: string;
  doc_id: string;
  doctype_id: string;
  field_name: string;
  value?: string | null;
  confidence: number;
  status: ReviewStatus;
  reason: string;
  page?: number | null;
  bbox?: BBox | null;
  pii: boolean;
  /** Reviewers who have signed. Length may be < `required_approvals`. */
  approvals: string[];
  /** 2 on a PII + checksum-backed field (blind double entry), else 1. */
  required_approvals: number;
  corrected_value?: string | null;
  decision_note: string;
  created_at: string;
  decided_at?: string | null;
  reviewer: string;
  [extra: string]: unknown;
}

/** `ReviewListResponse` — `depth` is the queue's total, when it can report one. */
export interface ReviewListResponse {
  count: number;
  items: ReviewItem[];
  depth?: number | null;
  timings: Timings;
}

/* ------------------------------------------------------- system / posture */

/** `RegistryStatus` */
export interface RegistryStatus {
  loaded: boolean;
  doctypes: number;
  countries: string[];
}

/** `EgressStatus` — the invariant, reported so an operator need not read the config. */
export interface EgressStatus {
  preclassification_allowed: boolean;
  enforced: boolean;
  note: string;
}

/** `TierStatus` — one extraction tier as configured on this deployment. */
export interface TierStatus {
  tier: string;
  enabled: boolean;
  cost_bearing: boolean;
  /** Non-empty when the tier is half-configured (flag without endpoint, or no HTTP client). */
  problem: string;
  summary: string;
}

/** `ComponentState` — health of a single component as last reported. */
export interface ComponentState {
  ok: boolean;
  detail: string;
  extra: Record<string, unknown>;
}

/**
 * `bert` on `/readyz` is an open dict on the wire. These are the keys the service emits today;
 * the index signature keeps anything it adds later.
 */
export interface BertStatus {
  enabled?: boolean;
  loaded?: boolean;
  model_dir?: string;
  device?: string;
  [extra: string]: unknown;
}

/**
 * `ReadinessResponse` — `GET /readyz`. 200 when ready, 503 when not.
 *
 * `ocr` is **not on the wire today**. It is where the service-side provider work is expected to
 * report which recognition providers this deployment has, and it is typed as `unknown` on
 * purpose: `src/ocr.ts` parses it tolerantly (it also looks in `components.ocr.extra` and
 * `components.ingest.extra`), so the console adapts to whichever shape lands instead of failing
 * to compile against it. Nothing reads this field directly — go through `readOcrPosture()`.
 */
export interface ReadinessResponse {
  ready: boolean;
  service: string;
  version: string;
  registry: RegistryStatus;
  bert: BertStatus;
  egress: EgressStatus;
  tiers: TierStatus[];
  components: Record<string, ComponentState>;
  degraded: string[];
  ocr?: unknown;
}

/** `GET /health` — liveness only. Never touches an engine. */
export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  [extra: string]: string;
}

/* ----------------------------------------------------------------- errors */

/** `ValidationError` inside FastAPI's 422 body. */
export interface ValidationErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

/**
 * The body of the 422 the ingest path returns when the bytes carry no text — an image, a
 * TIFF, a scanned PDF — and this deployment could not (or was told not to) read it locally.
 *
 * This is NOT a failure and NOT an abstention: nothing was misread, there was simply nothing
 * to read. `ocr_available` distinguishes "we chose not to" from "we cannot".
 * Shape: `IngestResult.as_detail()` in `dce/ingest/result.py`, delivered as `detail`.
 */
export interface NeedsOcrDetail {
  status: 'needs_ocr' | string;
  /** e.g. `image/jpeg`, `application/pdf`. */
  media_type: string;
  /** How the media type was decided: `magic` | `container` | `text-sniff`… */
  detected_by: string;
  page_count: number;
  /** Why OCR is needed, in one sentence. */
  reason: string;
  /** What the caller can do about it. */
  remedy: string;
  ocr_available: boolean;
}

/** The body of the 400 an ingest failure returns (`dce.ingest.errors`). */
export interface IngestErrorDetail {
  error: string;
  detail: string;
}
