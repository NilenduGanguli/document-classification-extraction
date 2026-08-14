/**
 * The OCR provider catalogue — the one place in the console that knows what reading a
 * document costs.
 *
 * ## Why this file exists at all
 *
 * `/analyze` lets an operator choose how an image gets turned into text, and `/posture` has to
 * report that choice to an auditor. Both need the same three facts about every provider, and
 * they must never disagree:
 *
 *   1. **does choosing it transmit the document to a third party** — before anything knows what
 *      the document is;
 *   2. **is it actually available on this deployment** — and if not, why not;
 *   3. **what structure does it return** — because that decides which anchors can fire.
 *
 * ## The rule this file enforces
 *
 * **The service names the providers; this console only supplies the prose.** The option list is
 * built from what `/readyz` reports, not from a list hard-coded here. A provider this console
 * has never heard of still appears, with its wire name and whatever the service said about it.
 * A provider this console *has* heard of but the service did **not** report is offered only when
 * it costs nothing — an unreported remote provider is disabled, because shipping an unclassified
 * document to an endpoint the service never told us about would be a guess, and a guess is not
 * an acceptable basis for a disclosure.
 *
 * That is also the reconciliation seam. When the service grows a provider, or renames one, or
 * moves the block on `/readyz`, the change lands in `readOcrPosture()` and `kindOf()` below and
 * nowhere else.
 *
 * ## What this file must never do
 *
 * Send `local_ocr: true`. Whether an unclassified document may be run through a recognition
 * engine is an operator decision; a caller flag that could switch one on would make the default
 * meaningless. See `ingestFieldsFor()` — the mapping is deliberately one-way.
 */
import type { ReadinessResponse } from './types';

/* ------------------------------------------------------------------ kinds */

/**
 * What a provider *is*, independent of what anyone called it.
 *
 * `kindOf()` maps a wire name onto one of these, and everything the console says about a
 * provider — the badge, the warning, the prose — is keyed off the kind rather than off the
 * string. A name the service invents tomorrow lands on `other` and is still rendered honestly.
 */
export type OcrKind = 'none' | 'native' | 'local' | 'azure_read' | 'azure_di' | 'other';

/** What geometry/structure a provider hands back. This is not cosmetic — see `STRUCTURE_NOTE`. */
export type OcrStructure = 'roles' | 'lines' | 'native' | 'unknown';

/**
 * Wire name → kind, by substring rather than by an alias table.
 *
 * DES calls the same two services `read` and `layout`; their engine strings are
 * `azure-read-v3.2` and `azure-layout-v4.0`; DCE's own ingest reports `local_ocr`, `rapidocr`,
 * `tesseract` and `native_text`. A closed alias list would have to be edited every time one of
 * those is spelled differently, and the failure mode of missing an entry is a *remote* provider
 * silently rendering as an unremarkable local one. Ordered substring rules degrade the other
 * way: an unrecognised name reads as `other`, which the console treats as "assume nothing".
 */
export function kindOf(name: string): OcrKind {
  const n = (name ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  if (!n || n === 'none' || n === 'null') return 'none';
  // `plain-text` is the adapter for text a caller pasted: no recognition ran on it at all,
  // which is exactly what `native` means here.
  if (n.includes('native') || n.includes('text_layer') || n.includes('plain')) return 'native';
  if (n.includes('rapid') || n.includes('tesser') || n.includes('local')) return 'local';
  // Order matters: "azure_di" and "prebuilt_layout" must not fall through to the Read rule,
  // and "azure-read-v3.2" must not be caught by a loose "di" test.
  if (n.includes('layout') || n.includes('prebuilt') || n.includes('intelligence')) return 'azure_di';
  if (n === 'di' || n.startsWith('di_') || n.includes('azure_di') || n.includes('_di_')) {
    return 'azure_di';
  }
  if (n.includes('read') || n.includes('vision')) return 'azure_read';
  return 'other';
}

/** True when running this provider puts the document on somebody else's server. */
export function isEgressKind(kind: OcrKind): boolean {
  return kind === 'azure_read' || kind === 'azure_di';
}

/* -------------------------------------------------------------- the prose */

interface Prose {
  label: string;
  /** One line, shown under the label in the picker. */
  summary: string;
  structure: OcrStructure;
  egress: boolean;
}

const PROSE: Record<OcrKind, Prose> = {
  none: {
    label: 'none',
    summary:
      'do not recognise anything. A document with a text layer still reads; an image comes back needs_ocr with its reason.',
    structure: 'native',
    egress: false,
  },
  native: {
    label: 'the file’s own text',
    summary: 'the publisher’s text layer — no recognition ran at all.',
    structure: 'native',
    egress: false,
  },
  local: {
    label: 'local engine',
    summary:
      'the in-process engine this deployment has enabled. Lower accuracy than a cloud service, and it opens no socket.',
    structure: 'lines',
    egress: false,
  },
  azure_read: {
    label: 'Azure AI Vision Read v3.2',
    summary: 'a remote recognition service. Lines and words only — no paragraph roles.',
    structure: 'lines',
    egress: true,
  },
  azure_di: {
    label: 'Azure Document Intelligence (prebuilt-layout)',
    summary: 'a remote analysis service. Returns paragraph roles, tables and selection marks.',
    structure: 'roles',
    egress: true,
  },
  other: {
    label: 'provider',
    summary: 'reported by this deployment; this console has no description for it.',
    structure: 'unknown',
    egress: false,
  },
};

/**
 * What each structure level means for the decision, in one sentence.
 *
 * This is the difference between a useful toggle and a misleading one. Zone-gated anchors are
 * evaluated against `Zone.title`; a provider that returns only lines gives every block
 * `Zone.body`, so those anchors cannot match no matter what the document says. Two providers
 * that both "did OCR" therefore produce different evidence, and a reviewer comparing them is
 * entitled to know which one could not have found the anchor.
 */
export const STRUCTURE_NOTE: Record<OcrStructure, string> = {
  roles: 'carries paragraph roles, so title and heading zones exist and zone-gated anchors can fire.',
  lines:
    'returns lines only. Every block is body text, so no title zone exists and any anchor gated to the title zone cannot fire on this reading — however clearly the words are on the page.',
  native: 'the layout comes from the file itself, not from a recognition engine.',
  unknown: 'this console does not know what structure it returns, so it will not claim either way.',
};

/* ------------------------------------------------------------- the option */

/** One selectable way of reading the document, as the picker renders it. */
export interface OcrOption {
  /** The value in the URL, and the wire name when one is sent. `none` and `local` send neither. */
  id: string;
  kind: OcrKind;
  label: string;
  summary: string;
  structure: OcrStructure;
  /** Choosing this transmits the document to a third party before its doctype is known. */
  egress: boolean;
  /** Where it goes, when the service says. Empty when it did not. */
  endpoint: string;
  available: boolean;
  /** Why it cannot be used here. Never empty when `available` is false. */
  unavailableReason: string;
  /**
   * True when `/readyz` reported this option's availability, false when the console inferred it.
   * The picker says which, because "we checked" and "we assumed" are different claims.
   */
  reported: boolean;
}

/* --------------------------------------------------- reading /readyz */

/** One provider entry as the service reported it, after tolerant parsing. */
export interface ReportedProvider {
  name: string;
  available: boolean;
  egress: boolean | null;
  endpoint: string;
  reason: string;
  structure: OcrStructure | null;
}

/** The OCR posture of a deployment, as far as `/readyz` discloses it. */
export interface OcrPosture {
  /** False when this deployment's `/readyz` says nothing about OCR at all. */
  reported: boolean;
  /** `null` means "not reported", which is not the same as `false`. */
  localEnabled: boolean | null;
  /** The engine name the operator configured, when reported. */
  localEngine: string;
  providers: ReportedProvider[];
  /** Whatever block this was read out of, for the raw disclosure on /posture. */
  raw: unknown;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const bag = (v: unknown): Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

function firstString(source: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) if (str(source[k])) return str(source[k]);
  return '';
}

function firstBool(source: Record<string, unknown>, keys: string[]): boolean | null {
  for (const k of keys) if (typeof source[k] === 'boolean') return source[k] as boolean;
  return null;
}

function readStructure(source: Record<string, unknown>): OcrStructure | null {
  const raw = firstString(source, ['structure', 'granularity', 'returns']).toLowerCase();
  if (!raw) {
    // A provider that advertises paragraph roles has structure whatever it calls it.
    const roles = firstBool(source, ['paragraph_roles', 'roles', 'has_structure']);
    if (roles === true) return 'roles';
    if (roles === false) return 'lines';
    return null;
  }
  if (raw.includes('role') || raw.includes('paragraph') || raw.includes('layout')) return 'roles';
  if (raw.includes('line') || raw.includes('word')) return 'lines';
  if (raw.includes('native')) return 'native';
  return null;
}

/** Parse one entry of whatever the service listed under `providers`. */
function readProvider(value: unknown, fallbackName: string): ReportedProvider | null {
  if (typeof value === 'string') {
    // A bare name in a list means "this provider exists here" and nothing else.
    return value
      ? { name: value, available: true, egress: null, endpoint: '', reason: '', structure: null }
      : null;
  }
  const source = bag(value);
  const name = firstString(source, ['name', 'provider', 'id', 'engine']) || fallbackName;
  if (!name) return null;
  // `available` is the question. A service that only says `enabled` is answering a near-enough
  // one; a service that says neither has not told us it is usable, so it is not.
  const available =
    firstBool(source, ['available', 'ready', 'usable', 'enabled', 'configured']) ?? false;
  return {
    name,
    available,
    egress: firstBool(source, ['egress', 'remote', 'transmits', 'network', 'cost_bearing']),
    endpoint: firstString(source, ['endpoint', 'url', 'base_url', 'host']),
    reason: firstString(source, ['reason', 'problem', 'detail', 'why', 'unavailable_reason']),
    structure: readStructure(source),
  };
}

function readProviderList(value: unknown): ReportedProvider[] {
  const out: ReportedProvider[] = [];
  if (Array.isArray(value)) {
    for (const entry of value) {
      const parsed = readProvider(entry, '');
      if (parsed) out.push(parsed);
    }
  } else if (typeof value === 'object' && value !== null) {
    for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
      const parsed = readProvider(entry, key);
      if (parsed) out.push(parsed);
    }
  }
  return out;
}

/**
 * Find the OCR block on a readiness body, wherever this deployment puts it.
 *
 * `/readyz` has no `ocr` field today. Rather than wait for one shape, this looks in the three
 * places a service reasonably would — a top-level `ocr` (or `ingest`) block, and the `extra`
 * bag of an `ocr`/`ingest` component — and returns the first that carries anything. If none
 * does, `reported` is false and every caller treats OCR posture as *unknown*, which is the only
 * honest reading and is emphatically not the same as "no remote provider is configured".
 */
export function readOcrPosture(readiness: ReadinessResponse | null): OcrPosture {
  const empty: OcrPosture = {
    reported: false,
    localEnabled: null,
    localEngine: '',
    providers: [],
    raw: null,
  };
  if (!readiness) return empty;

  const record = readiness as unknown as Record<string, unknown>;
  const components = bag(record.components);
  const candidates: unknown[] = [
    record.ocr,
    record.ingest,
    bag(components.ocr).extra,
    bag(components.ingest).extra,
  ];

  for (const candidate of candidates) {
    const source = bag(candidate);
    if (!Object.keys(source).length) continue;

    const providers = readProviderList(source.providers ?? source.engines ?? source.remote);
    const local = bag(source.local ?? source.local_ocr);
    const localEnabled =
      firstBool(local, ['enabled', 'available']) ??
      firstBool(source, ['local_ocr_enabled', 'local_enabled', 'local_ocr', 'ocr_available']);
    const localEngine =
      firstString(local, ['engine', 'name', 'provider']) ||
      firstString(source, ['local_ocr_engine', 'engine']);

    // A block that mentions none of the three tells us nothing; keep looking.
    if (!providers.length && localEnabled === null && !localEngine) continue;
    return {
      reported: true,
      localEnabled,
      localEngine,
      providers,
      raw: candidate,
    };
  }
  return empty;
}

/**
 * Does running this provider transmit the document?
 *
 * The service's own `egress` flag wins when it sets one. When it does not, the answer is
 * derived from the kind — and the derivation is deliberately asymmetric: a name that resolves to
 * a known remote service is remote, and a name this console does not recognise is *not* assumed
 * to be local. `other` returning false is safe only because an unrecognised provider is never
 * offered in the picker unless the service listed it, and the service listing it without an
 * `egress` flag is the case an operator should be told about rather than guessed at.
 */
export function providerEgress(p: ReportedProvider): boolean {
  return p.egress ?? isEgressKind(kindOf(p.name));
}

/**
 * True when this deployment can send a document away to be read.
 *
 * The one predicate behind the header pill, the invariant panel and the OCR panel — they must
 * never disagree, and three copies of `p.available && (p.egress ?? …)` in three files is how
 * that stops being true.
 */
export function readsRemotely(readiness: ReadinessResponse | null): boolean {
  return readOcrPosture(readiness).providers.some((p) => p.available && providerEgress(p));
}

/* ------------------------------------------------------- building options */

/** The two options that exist on every deployment, whatever `/readyz` says. */
export const NONE_ID = 'none';
export const LOCAL_ID = 'local';

const NOT_REPORTED_REMOTE =
  'this deployment’s /readyz does not report an OCR provider list, so the console cannot confirm ' +
  'this provider is configured here. It will not transmit a document to a remote service on a ' +
  'guess — enable the provider service-side and it will appear here.';

/**
 * The options to offer, in the order they should be shown.
 *
 * Nothing is ever hidden. An option an operator cannot use is rendered disabled, with the reason
 * on hover, because "the option is not there" and "the option is there and this deployment does
 * not have it" lead an operator to two different next steps and only one of them is the truth.
 */
export function ocrOptions(readiness: ReadinessResponse | null): OcrOption[] {
  const posture = readOcrPosture(readiness);

  const none: OcrOption = {
    id: NONE_ID,
    kind: 'none',
    label: PROSE.none.label,
    summary: PROSE.none.summary,
    structure: 'native',
    egress: false,
    endpoint: '',
    available: true,
    unavailableReason: '',
    reported: true,
  };

  const localReported = posture.localEnabled !== null;
  const local: OcrOption = {
    id: LOCAL_ID,
    kind: 'local',
    label: posture.localEngine ? `local engine — ${posture.localEngine}` : PROSE.local.label,
    summary: PROSE.local.summary,
    structure: 'lines',
    egress: false,
    endpoint: '',
    available: posture.localEnabled !== false,
    unavailableReason:
      posture.localEnabled === false
        ? 'no local OCR engine is enabled on this deployment. It is an operator setting, and a ' +
          'caller cannot turn it on: an image will come back needs_ocr instead.'
        : '',
    reported: localReported,
  };

  // Everything the service actually named, in its own words.
  const reported: OcrOption[] = posture.providers
    .filter((p) => kindOf(p.name) !== 'local' && kindOf(p.name) !== 'native')
    .map((p) => {
      const kind = kindOf(p.name);
      const prose = PROSE[kind];
      return {
        id: p.name,
        kind,
        label: kind === 'other' ? p.name : prose.label,
        summary: prose.summary,
        structure: p.structure ?? prose.structure,
        egress: p.egress ?? prose.egress,
        endpoint: p.endpoint,
        available: p.available,
        unavailableReason: p.available
          ? ''
          : p.reason || 'this deployment reports the provider as not available.',
        reported: true,
      };
    });

  // The two remote providers this console knows how to describe, when the service did not list
  // them. Present, disabled, and explicit about why — an operator should be able to see that the
  // capability exists and that this deployment has not been given it.
  const knownRemote: Array<[string, OcrKind]> = [
    ['azure_read', 'azure_read'],
    ['azure_di', 'azure_di'],
  ];
  const placeholders: OcrOption[] = knownRemote
    .filter(([, kind]) => !reported.some((o) => o.kind === kind))
    .map(([id, kind]) => ({
      id,
      kind,
      label: PROSE[kind].label,
      summary: PROSE[kind].summary,
      structure: PROSE[kind].structure,
      egress: true,
      endpoint: '',
      available: false,
      unavailableReason: posture.reported
        ? 'this deployment reports an OCR provider list and this provider is not on it.'
        : NOT_REPORTED_REMOTE,
      reported: posture.reported,
    }));

  return [none, local, ...reported, ...placeholders];
}

/** The option matching an id, or `undefined`. Ids come from the URL and cannot be trusted. */
export function findOcrOption(options: OcrOption[], id: string | null): OcrOption | undefined {
  return options.find((o) => o.id === id);
}

/**
 * The id to use given whatever the URL said.
 *
 * Falls back to the option that reproduces the service's own default — `local`, which sends no
 * ingest flags at all — and to `none` when the deployment has no local engine. An unusable id in
 * a link resolves to something safe rather than to an error.
 */
export function resolveOcrId(options: OcrOption[], wanted: string | null): string {
  const match = findOcrOption(options, wanted);
  if (match && match.available) return match.id;
  const local = findOcrOption(options, LOCAL_ID);
  return local?.available ? LOCAL_ID : NONE_ID;
}

/* ------------------------------------------------------- shaping the request */

/** The `ingest` fields a choice implies. `local_ocr: true` is deliberately unreachable. */
export interface IngestOcrFields {
  local_ocr?: false;
  ocr_provider?: string;
  read_channel?: ReadChannel;
}

/**
 * How the document gets turned into text at all — the choice *above* which recogniser.
 *
 * `auto` is the service's own behaviour and what every caller got before this existed. The other
 * two force a reading the file would not otherwise have had, and `optical` is the one worth
 * having: a PDF with a text layer can be read both ways, and the two readings are not the same
 * document. The text layer carries no paragraph roles, so a zone-gated anchor cannot fire on it;
 * Document Intelligence supplies roles and it can. Running both on one file is how an operator
 * sees that on their own documents instead of taking it on trust.
 */
export type ReadChannel = 'auto' | 'lexical' | 'optical';

/**
 * Turn a picker choice into ingest options.
 *
 *   `none`   → `local_ocr: false`. The one thing a caller has always been allowed to do:
 *              turn recognition *off* for this request.
 *   `local`  → nothing at all. This is the service's own default, and sending `local_ocr: true`
 *              to "make sure" would be a caller enabling an engine the operator did not.
 *   anything → `ocr_provider: <the service's own name for it>`. The service is still the one
 *   else       that decides whether that provider may run; this only asks.
 */
export function ingestFieldsFor(id: string, channel: ReadChannel = 'auto'): IngestOcrFields {
  // `lexical` is a refusal, and it outranks the provider choice: a caller asking for the text
  // layer and nothing else has said something about THIS document, so sending a provider pin
  // alongside it would be asking the service to honour two contradictory instructions.
  if (channel === 'lexical') return { local_ocr: false, read_channel: 'lexical' };
  const chan = channel === 'auto' ? {} : { read_channel: channel };
  if (id === NONE_ID) return { local_ocr: false, ...chan };
  if (id === LOCAL_ID) return { ...chan };
  return { ocr_provider: id, ...chan };
}

/* --------------------------------------------------- reading the provenance */

/** Which provider produced the text that was classified, as far as a response discloses it. */
export interface Provenance {
  /** The name the service used, verbatim. Empty when it did not say. */
  name: string;
  kind: OcrKind;
  structure: OcrStructure;
  egress: boolean;
  /** True when this came out of the response rather than out of what the console asked for. */
  reported: boolean;
  /** Host the document was sent to, when the service named one. */
  endpointHost: string;
}

const PROVENANCE_KEYS = [
  'ocr_provider',
  'ocr_engine',
  'text_source',
  'provider',
  'engine',
  'source',
];

/** A provenance string plus, when the service gave them, the facts that qualify it. */
interface ProvenanceNode {
  name: string;
  /** The service's own answer to "did this document leave the process". `null` = not stated. */
  remote: boolean | null;
  endpointHost: string;
}

const NOTHING: ProvenanceNode = { name: '', remote: null, endpointHost: '' };

/** Depth-limited hunt for a provenance string anywhere the service might have put one. */
function findProvenanceName(node: unknown, depth = 0): ProvenanceNode {
  if (depth > 3 || typeof node !== 'object' || node === null || Array.isArray(node)) {
    return NOTHING;
  }
  const source = node as Record<string, unknown>;
  for (const key of PROVENANCE_KEYS) {
    const value = source[key];
    // `native_text` and `none` are real answers: they say no recognition ran.
    if (typeof value === 'string' && value) {
      return {
        name: value,
        // Read from the same object the name came out of — these three fields are one
        // statement and must not be assembled from different levels of the response.
        remote: typeof source.remote === 'boolean' ? source.remote : null,
        endpointHost: str(source.endpoint_host),
      };
    }
  }
  // `source` is where the service actually reports this — as a body block on `/process`, and
  // as the parsed `X-Document-Source` header on `/classify` and `/extract` (see `api.ts`). It
  // is listed here as well as in PROVENANCE_KEYS because at this level it is an *object*, and
  // the loop above only accepts a string.
  for (const key of ['source', 'ingest', 'provenance', 'classification', 'extraction', 'document']) {
    const found = findProvenanceName(source[key], depth + 1);
    if (found.name) return found;
  }
  return NOTHING;
}

/**
 * What produced the text, preferring what the service said over what the console asked for.
 *
 * A console that printed its own request back as if it were the answer would be putting a claim
 * on screen the service never made — the one thing this console may not do. So when the response
 * carries no provenance the caller gets `reported: false` and must render it as the request it
 * was, not as a finding.
 */
export function readProvenance(raw: unknown, requested: OcrOption | undefined): Provenance | null {
  const found = findProvenanceName(raw);
  if (found.name) {
    const kind = kindOf(found.name);
    return {
      name: found.name,
      kind,
      structure: PROSE[kind].structure,
      // **The service's own flag wins, and this is not a preference — it is correctness.**
      // The adapter name is the same on both paths: a caller-supplied `analyzeResult` and a
      // document this service posted to Azure itself are both mapped by, and both reported
      // as, `azure-prebuilt-layout`. Only `remote` separates them. Deriving egress from the
      // name instead would accuse the zero-egress caller-supplied path — the one the service
      // recommends — of transmitting documents it never touched, and a compliance panel that
      // cries wolf on the safe path is worse than no panel.
      egress: found.remote ?? isEgressKind(kind),
      reported: true,
      endpointHost: found.endpointHost,
    };
  }
  if (!requested) return null;
  return {
    name: requested.id,
    kind: requested.kind,
    structure: requested.structure,
    egress: requested.egress,
    reported: false,
    endpointHost: requested.endpoint,
  };
}

/** The human label for a provenance string, falling back to the string itself. */
export function provenanceLabel(p: Provenance): string {
  return p.kind === 'other' ? p.name : PROSE[p.kind].label;
}

/* ------------------------------------------------- the declared trust boundary */

/**
 * What the deployment declares about its remote OCR endpoint. `external` is the safe reading.
 *
 * The code cannot tell `ocr.internal.corp` from `x.cognitiveservices.azure.com`: same resolve,
 * same socket, same bytes. So the deployment states it, and every surface in this console reads
 * that statement from here rather than forming its own opinion about a hostname.
 */
export type TrustBoundary = 'external' | 'on_premises';

/** The declared boundary and the service's own sentence explaining who declared it. */
export interface BoundaryDeclaration {
  boundary: TrustBoundary;
  /**
   * `ocr.trust_boundary_attribution` verbatim, or `''`.
   *
   * Rendered rather than paraphrased, deliberately. The service already knows things this
   * console does not — whether the value was set or inherited, and that it was never verified —
   * and a second sentence written here would be a second place for the two to drift apart. A
   * console that told an operator "nothing was declared" about an endpoint the operator had
   * explicitly declared external would be wrong in exactly the register it is asking to be
   * trusted in.
   */
  attribution: string;
}

/**
 * The declaration, read tolerantly off `/readyz`.
 *
 * `on_premises` only when the service says exactly that. Anything else — a deployment that does
 * not report the field, an older build, a shape this console does not recognise — reads as
 * `external`, which is the same asymmetry the service itself uses: silence must never produce
 * the reassuring answer.
 *
 * This lives beside the provider catalogue rather than in one page because all three surfaces
 * that describe the hop — the `/analyze` picker, the `/posture` reading panel and the header
 * pill on every screen — must describe it the same way. They did not: `/posture` went on calling
 * an operator's own appliance a "third party" while the same page rendered the service's note
 * saying the operator had declared it inside their boundary. One screen, two opposite accounts
 * of one socket. A shared reader is what makes that unrepresentable rather than merely fixed.
 */
export function declaredTrustBoundary(readiness: ReadinessResponse | null): BoundaryDeclaration {
  const raw = readOcrPosture(readiness).raw;
  const source = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const attribution = source.trust_boundary_attribution;
  return {
    boundary: source.trust_boundary === 'on_premises' ? 'on_premises' : 'external',
    attribution: typeof attribution === 'string' ? attribution : '',
  };
}
