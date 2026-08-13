/**
 * Typed client for the DCE API.
 *
 * Every function here binds to a path and a pair of models transcribed from the running
 * service's `/openapi.json` (see `types.ts`). Nothing in this file talks to anything other
 * than this service's own origin: the console is served by the same process it calls, and a
 * console for a no-egress service must not be the thing that egresses.
 *
 * ## Errors
 * Everything non-2xx throws `ApiError`. Pages should not need to touch `fetch` or read status
 * codes: `ApiError` already carries the interesting distinctions.
 *
 *   err.status        HTTP status
 *   err.detail        the parsed `detail` from FastAPI (string, object or ValidationError[])
 *   err.needsOcr      NeedsOcrDetail when this is the 422 "there was nothing to read"
 *   err.message       one human sentence, safe to render
 *
 * The three states worth branching on, in order of how often they happen:
 *   1. `err.needsOcr` — an image with local OCR off. Show the reason + remedy, offer nothing
 *      that pretends to have read it.
 *   2. `err.status === 401` — an API key is configured on this deployment and the console has
 *      the wrong one (or none). See `setApiKey`.
 *   3. `err.status === 503` — an engine is not importable. `/readyz` says which.
 *
 * ## Abstention is not an error
 * `POST /classify` and `POST /process` return **200** when the cascade abstains.
 * `Classification.abstained === true`, `reason` names the gate that failed, and the document
 * is routed to a human. Do not render that as a failure. See `isAbstention`.
 */

import type {
  Category,
  Classification,
  DocTypeListResponse,
  DocTypeSpec,
  DocumentRequest,
  ExtractRequest,
  ExtractionResult,
  HealthResponse,
  InduceRequest,
  NeedsOcrDetail,
  ProcessResponse,
  ReadinessResponse,
  ReviewDecision,
  ReviewItem,
  ReviewListResponse,
  ReviewStatus,
  SchemaResponse,
  ValidationErrorItem,
} from './types';

/** Same-origin in the container; the vite dev server proxies these to :8200. */
const API = '/api/v1';

/* ------------------------------------------------------------- api key */

const API_KEY_STORAGE = 'dce.apiKey';

/**
 * The X-API-Key this console sends, if the deployment configured one.
 *
 * Kept in localStorage because there is nowhere else to put it in a static SPA, and it is a
 * *service* key the operator already holds — not a user credential the console mints. When
 * `API_KEY` is unset on the server the gate is off and this is unused.
 */
export function getApiKey(): string {
  try {
    return window.localStorage.getItem(API_KEY_STORAGE) ?? '';
  } catch {
    return '';
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) window.localStorage.setItem(API_KEY_STORAGE, key);
    else window.localStorage.removeItem(API_KEY_STORAGE);
  } catch {
    /* private browsing; the key just will not persist */
  }
}

/* -------------------------------------------------------------- errors */

/** A non-2xx response, with the distinctions the UI actually branches on. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  /** Set exactly when this is the structured 422 from the ingest path. */
  readonly needsOcr?: NeedsOcrDetail;
  /** Set when FastAPI rejected the request body (422 with a ValidationError list). */
  readonly validation?: ValidationErrorItem[];

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    if (isNeedsOcrDetail(detail)) this.needsOcr = detail;
    if (Array.isArray(detail) && detail.every(isValidationErrorItem)) {
      this.validation = detail as ValidationErrorItem[];
    }
  }
}

function isNeedsOcrDetail(d: unknown): d is NeedsOcrDetail {
  return (
    typeof d === 'object' &&
    d !== null &&
    (d as { status?: unknown }).status === 'needs_ocr' &&
    typeof (d as { reason?: unknown }).reason === 'string'
  );
}

function isValidationErrorItem(d: unknown): d is ValidationErrorItem {
  return (
    typeof d === 'object' &&
    d !== null &&
    Array.isArray((d as { loc?: unknown }).loc) &&
    typeof (d as { msg?: unknown }).msg === 'string'
  );
}

/** One renderable sentence from whatever FastAPI put in `detail`. */
function messageFor(status: number, detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail) return detail;
  if (isNeedsOcrDetail(detail)) return detail.reason || 'this file carries no text to read';
  if (Array.isArray(detail) && detail.length && isValidationErrorItem(detail[0])) {
    const first = detail[0] as ValidationErrorItem;
    return `${first.loc.join('.')}: ${first.msg}`;
  }
  if (typeof detail === 'object' && detail !== null) {
    const d = detail as { detail?: unknown; error?: unknown };
    if (typeof d.detail === 'string' && d.detail) return d.detail;
    if (typeof d.error === 'string' && d.error) return d.error;
  }
  return fallback || `HTTP ${status}`;
}

/* --------------------------------------------------------------- fetch */

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
}

function withQuery(path: string, query?: RequestOptions['query']): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * Parse the `X-Document-Source` header into the same shape `/process` puts in its body.
 *
 * `/classify` and `/extract` report which adapter read the payload — and whether this service
 * dialled out to obtain it — **only** on this header; `/process` reports it as a `source`
 * object. That asymmetry is invisible to an operator but not to the console: without this, the
 * "How the text was read" panel falls back to echoing the console's own request on two of the
 * three endpoints, and a request is not an answer.
 *
 * The header is `<provider>` or `<provider>; remote=<host>`. Nothing here is inferred — every
 * field comes out of the string the service sent.
 */
function sourceFromHeader(header: string | null): Record<string, unknown> | null {
  const raw = (header ?? '').trim();
  if (!raw) return null;
  const [provider, ...rest] = raw.split(';').map((part) => part.trim());
  if (!provider) return null;
  const remoteTag = rest.find((part) => part.toLowerCase().startsWith('remote='));
  const host = remoteTag ? remoteTag.slice(remoteTag.indexOf('=') + 1).trim() : '';
  return {
    provider,
    remote: Boolean(remoteTag),
    endpoint_host: host === 'yes' ? '' : host,
  };
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const key = getApiKey();
  if (key) headers['X-API-Key'] = key;

  let response: Response;
  try {
    response = await fetch(withQuery(path, opts.query), {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: opts.signal,
      // The console and the API are the same origin. Say so.
      credentials: 'same-origin',
      referrerPolicy: 'no-referrer',
    });
  } catch (cause) {
    // A dropped connection is not an HTTP status. Status 0 marks "we never got a reply".
    throw new ApiError(0, null, `cannot reach the service: ${(cause as Error).message}`);
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }

  if (!response.ok) {
    const detail =
      typeof parsed === 'object' && parsed !== null && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    throw new ApiError(response.status, detail, messageFor(response.status, detail, response.statusText));
  }

  // Normalise the header-only provenance onto the body, so every endpoint answers "what read
  // this document" the same way. Never overwrites a `source` the service sent itself.
  if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
    const body = parsed as Record<string, unknown>;
    if (body.source === undefined) {
      const source = sourceFromHeader(response.headers.get('X-Document-Source'));
      if (source) body.source = source;
    }
  }
  return parsed as T;
}

/* ------------------------------------------------- document construction */

/** Strip the `data:...;base64,` prefix a FileReader data URL carries. */
function stripDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(',');
  return comma === -1 ? dataUrl : dataUrl.slice(comma + 1);
}

/** base64 of a File's bytes, done in the browser. The file is never uploaded anywhere else. */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`could not read ${file.name}`));
    reader.onload = () => resolve(stripDataUrl(String(reader.result ?? '')));
    reader.readAsDataURL(file);
  });
}

/**
 * Build a `DocumentRequest` from a picked file.
 *
 * `content_base64` alone is *carried, not read* — it exists so the paid Azure tiers can send
 * the original file after a doctype is accepted. Passing `ingest` (which this always does) is
 * what asks the service to parse the bytes **in this process** into a layout, which is the
 * only path that works on a default, no-egress deployment.
 *
 * `local_ocr` may be set to `false` to decline local OCR for this one request. It cannot be
 * set to `true` to enable an engine the operator has not enabled — that is an operator
 * decision, not a caller flag — so this helper never sends `local_ocr: true`.
 *
 * `ingestOcr` carries the same asymmetry one step further. It comes from
 * `ocr.ingestFieldsFor()`, whose type cannot express `local_ocr: true` at all, and it may name a
 * remote provider — which is a *request* the service is free to refuse, not a grant. Callers
 * must only name a provider `/readyz` reported for this deployment; see `src/ocr.ts`.
 */
export async function documentRequestFromFile(
  file: File,
  opts: { docId?: string; ingestOcr?: { local_ocr?: false; ocr_provider?: string } } = {},
): Promise<DocumentRequest> {
  return {
    doc_id: opts.docId ?? file.name,
    content_base64: await fileToBase64(file),
    ingest: {
      filename: file.name,
      ...(opts.ingestOcr ?? {}),
    },
  };
}

/** Build a `DocumentRequest` from text somebody pasted. The simplest, always-supported path. */
export function documentRequestFromText(text: string, docId = 'pasted'): DocumentRequest {
  return { doc_id: docId, text };
}

/* ----------------------------------------------------- classify / extract */

/** `POST /api/v1/classify`. 200 even when it abstains — check `abstained`. */
export function classify(body: DocumentRequest, signal?: AbortSignal): Promise<Classification> {
  return request<Classification>(`${API}/classify`, { method: 'POST', body, signal });
}

/**
 * `POST /api/v1/extract`. Omit `doctype_id` to have the document classified first; an
 * abstention returns an empty result flagged for review rather than a guessed doctype.
 */
export function extract(body: ExtractRequest, signal?: AbortSignal): Promise<ExtractionResult> {
  return request<ExtractionResult>(`${API}/extract`, { method: 'POST', body, signal });
}

/** `POST /api/v1/process` — classify + extract + tier ledger + review routing, one call. */
export function process(body: DocumentRequest, signal?: AbortSignal): Promise<ProcessResponse> {
  return request<ProcessResponse>(`${API}/process`, { method: 'POST', body, signal });
}

/* --------------------------------------------------------------- registry */

/** `GET /api/v1/doctypes` — 182 of them on a stock deployment. Both filters are optional. */
export function listDocTypes(
  filters: { country?: string; category?: Category } = {},
  signal?: AbortSignal,
): Promise<DocTypeListResponse> {
  return request<DocTypeListResponse>(`${API}/doctypes`, { query: filters, signal });
}

/** `GET /api/v1/doctypes/{id}` — the full spec: anchors, negatives, confusables, fields. */
export function getDocType(doctypeId: string, signal?: AbortSignal): Promise<DocTypeSpec> {
  return request<DocTypeSpec>(`${API}/doctypes/${encodeURIComponent(doctypeId)}`, { signal });
}

/** `GET /api/v1/schemas/{id}` — the active field schema for a doctype. */
export function getSchema(doctypeId: string, signal?: AbortSignal): Promise<SchemaResponse> {
  return request<SchemaResponse>(`${API}/schemas/${encodeURIComponent(doctypeId)}`, { signal });
}

/**
 * `POST /api/v1/schemas/induce` — draft a schema from samples.
 *
 * The result comes back with `active: false`. It is a **draft**: nothing in the registry
 * changed, and nothing will classify differently because you called this.
 */
export function induceSchema(body: InduceRequest, signal?: AbortSignal): Promise<SchemaResponse> {
  return request<SchemaResponse>(`${API}/schemas/induce`, { method: 'POST', body, signal });
}

/* ----------------------------------------------------------------- review */

/**
 * `GET /api/v1/review`.
 *
 * `status` defaults to `pending` on the server. Pass `'all'` (or `''`) for every item —
 * the route treats both as "no filter".
 */
export function listReview(
  filters: { status?: ReviewStatus | 'all' | ''; doctype?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<ReviewListResponse> {
  return request<ReviewListResponse>(`${API}/review`, { query: filters, signal });
}

/** The three decisions a human can record. One item is one FIELD of one document. */
export type ReviewAction = 'approve' | 'reject' | 'correct';

/**
 * `POST /api/v1/review/{id}/{approve|reject|correct}`.
 *
 * Two things the UI must get right:
 *  - **A 200 does not mean the item closed.** On a double-entry item (PII + checksum-backed)
 *    the first decision comes back still `pending`, with the first reviewer named in
 *    `approvals`. Check `status`, not the HTTP code.
 *  - **409 is a real outcome, not a bug.** Already decided, the same reviewer trying to be
 *    both halves of a double entry, or two independent keyings that disagreed — in the last
 *    case *both* entries are discarded and the item is back to square one. `ApiError.message`
 *    carries the queue's own sentence, which says what the reviewer must do next; render it.
 *
 * `correct` requires a non-empty `value`; an empty correction is an approval and the server
 * returns 400 telling you to send it as one.
 */
export function decideReview(
  itemId: string,
  action: ReviewAction,
  body: ReviewDecision,
  signal?: AbortSignal,
): Promise<ReviewItem> {
  return request<ReviewItem>(`${API}/review/${encodeURIComponent(itemId)}/${action}`, {
    method: 'POST',
    body,
    signal,
  });
}

export const approveReview = (id: string, body: ReviewDecision, signal?: AbortSignal) =>
  decideReview(id, 'approve', body, signal);
export const rejectReview = (id: string, body: ReviewDecision, signal?: AbortSignal) =>
  decideReview(id, 'reject', body, signal);
export const correctReview = (id: string, body: ReviewDecision, signal?: AbortSignal) =>
  decideReview(id, 'correct', body, signal);

/* ----------------------------------------------------------- system probes */

/**
 * `GET /readyz` — NOT under /api/v1, and not behind the API key.
 *
 * It answers 503 when the service is not ready (a missing engine, or the egress invariant
 * switched off) and the **body is still the full `ReadinessResponse`**. That body is the whole
 * point of the posture page, so this reads it on both codes instead of throwing.
 */
export async function readiness(signal?: AbortSignal): Promise<ReadinessResponse> {
  let response: Response;
  try {
    response = await fetch('/readyz', { headers: { Accept: 'application/json' }, signal });
  } catch (cause) {
    throw new ApiError(0, null, `cannot reach the service: ${(cause as Error).message}`);
  }
  const text = await response.text();
  try {
    return JSON.parse(text) as ReadinessResponse;
  } catch {
    throw new ApiError(response.status, text, 'readiness returned something that is not JSON');
  }
}

/** `GET /health` — liveness only. (There is no `/healthz`; the route is `/health`.) */
export function health(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/health', { signal });
}

/** `GET /metrics` — Prometheus exposition, as text. */
export async function metrics(signal?: AbortSignal): Promise<string> {
  const response = await fetch('/metrics', { signal });
  return response.text();
}

/* -------------------------------------------------------------- predicates */

/**
 * True when the cascade declined to decide. This is a **feature**: four gates
 * (evidence, margin over the runner-up, coverage of the doctype's vocabulary, support) must
 * all hold, and when one does not the document goes to a human instead of to a guess.
 * Render it as a routed decision, never as a failure.
 */
export function isAbstention(c: Classification | null | undefined): boolean {
  return Boolean(c && (c.abstained || c.doctype_id === 'unknown'));
}

/** True when this error is the honest "there was nothing to read" 422. */
export function isNeedsOcr(err: unknown): err is ApiError & { needsOcr: NeedsOcrDetail } {
  return err instanceof ApiError && err.needsOcr !== undefined;
}

/** Narrowing helper so pages can `catch (e)` without an `any`. */
export function asApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : new ApiError(0, null, String((err as Error)?.message ?? err));
}
