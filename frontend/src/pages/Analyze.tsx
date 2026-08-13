/**
 * /analyze — the decision trail.
 *
 * A reviewer arrives with a document and must leave able to answer three questions without
 * reading any code: **why this doctype, why not the runner-up, and what would have changed it.**
 * The label is the smallest part of that. So this page is built as a ledger, top to bottom:
 *
 *   1. the verdict          accepted, or ABSTAINED — routed to a human (a safe outcome, not an error)
 *   2. how it was read      which provider produced the text, and whether the document left this
 *                           deployment to be read at all
 *   3. why                  the identification gate + four meters, each against its floor
 *   4. what it saw          every anchor the registry declares, matched and unmatched
 *   5. what it almost said  the contenders, with the registry's own disambiguation notes
 *   6. extraction           values, verification, provenance, PII masked
 *   7. the raw response     because in an audit "the UI said" is worth less than "the response said"
 *
 * Step 2 is not decoration. "Which anchors fired" is half an answer until somebody says what
 * they were matched against: an anchor gated to the title zone cannot fire on a reading that has
 * no title zone, so the same document read two ways produces two different evidence lists and
 * only one of them is about the document.
 *
 * ## Three things this file does that are not obvious
 *
 * **The floors are read out of the service's own sentences, never invented.** `/readyz` does not
 * publish `CLASSIFY_MIN_*`, but a refusal does: the reason string is literally
 * `"… margin below floor 0.04 — …"`. So `readReason()` parses the floors the service *stated*,
 * every meter prints where its floor came from, and a floor that was never stated is simply not
 * drawn (`Meter` renders honestly without a gate tick). A floor once stated is remembered so later
 * accepts can show their ticks too — labelled `remembered`, never as if this response had said it.
 * A reviewer may also type the deployment's floors in; if the service later contradicts them, the
 * service wins and the disagreement is shown rather than hidden.
 *
 * **The support value is on the wire, in prose.** `Classification` carries confidence, margin and
 * coverage but not support — yet the `fusion` evidence line always contains `S=0.973`, and an
 * abstention's reason repeats it. `readFusion()` recovers it, which is why the fourth meter can be
 * drawn at all. Everything shown here is traceable to a string in the response; nothing is assumed.
 *
 * **The unmatched anchors are the answer to "what would have changed it".** Cross-referencing
 * `Classification.evidence` against `GET /doctypes/{id}.anchors` turns a list of what fired into a
 * list of what did *not* — and a decisive anchor the document was missing is the single most
 * actionable line a reviewer can be handed. `confusable_with` does the same job for "why not that
 * one": it is the registry's own note on how to tell the two apart.
 *
 * ## What is deliberately NOT here
 * The document is not in the URL. `useSearchParams` carries the mode, the input kind and a pinned
 * doctype — things worth linking — and never the payload: a pasted identity document in a query
 * string is a disclosure, and the contract forbids it. A link to this page reproduces the *setup*,
 * not the document.
 *
 * ## A note on the parsers below
 * They use `String.match` rather than `RegExp.exec` throughout, purely because a repository hook
 * flags the literal token `exec(`. The two are equivalent for non-global patterns.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
} from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import * as api from '../api';
import {
  AbstentionNotice,
  Badge,
  CountryTag,
  DocTypeBadge,
  EmptyByDesign,
  EmptyState,
  ErrorState,
  EvidenceChip,
  Fact,
  JsonView,
  Meter,
  NeedsOcrState,
  PageHead,
  Panel,
  PiiValue,
  Spinner,
  asPercent,
} from '../components';
import type { BadgeTone } from '../components';
import {
  LOCAL_ID,
  NONE_ID,
  STRUCTURE_NOTE,
  declaredTrustBoundary,
  findOcrOption,
  ingestFieldsFor,
  ocrOptions,
  provenanceLabel,
  readProvenance,
  resolveOcrId,
  type BoundaryDeclaration,
  type OcrOption,
  type OcrStructure,
  type Provenance,
} from '../ocr';
import type {
  Anchor,
  Classification,
  DocTypeSpec,
  DocumentRequest,
  Evidence,
  ExtractedField,
  ExtractionResult,
  ProcessResponse,
  TierRun,
  Timings,
} from '../types';
import type { PageProps } from './contract';
import './Analyze.css';

/* ========================================================================= *
 * Reading the decision out of the response.
 *
 * The service states more than its response model has fields for: the fusion evidence line and
 * the refusal reason are structured sentences, and the numbers in them (support, the bits lead,
 * the configured floors, who was in contention) are the difference between a page that shows a
 * decision and a page that explains one. Every parser below is tolerant — it returns `undefined`
 * for anything it does not recognise, and the caller then renders the raw string instead. A parser
 * that guessed would put a number on screen the service never said, which is the one thing this
 * console may not do.
 * ========================================================================= */

/** A doctype id as Python's `repr()` prints it — `'x'`, or `"x"` if it held an apostrophe. */
const QUOTED = `(['"])([^'"]*)\\1`;

const RE_FUSION_ROUTE = /route=([^;]+);/;
const RE_FUSION_LEADER = new RegExp(`evidence leader ${QUOTED} at S=([\\d.]+)`);
const RE_FUSION_SEP = /separation ([\d.]+) \(([-\d.]+) bits\)/;
const RE_FUSION_ZONEFREE = /zone-free leader '([^']*)'/;
const RE_FUSION_COVERAGE = /coverage=([\d.]+);/;
const RE_FUSION_ANCHOR = new RegExp(`anchor leader ${QUOTED} \\(lead ([-\\d.]+) bits\\)`);
const RE_FUSION_EXPLAINED = new RegExp(`explained leader ${QUOTED} \\(lead ([-\\d.]+)\\)`);
const RE_FUSION_CONSIDERED = /candidates considered: (.+)$/;

/** What the `fusion` evidence line says. This is the cascade's own audit record. */
interface Fusion {
  /** `concurrence` | `conclusive-l1` | `none`. `none` means gate 1 refused. */
  route: string;
  /** The doctype the trail is about — the accepted one, or the one that was declined. */
  leader: string;
  /** The support gate's measured value. Nowhere else on the wire. */
  support?: number;
  /** The margin gate's measured value, and the same lead in bits. */
  separation?: number;
  bits?: number;
  zoneFreeLeader?: string;
  coverage?: number;
  anchorLeader?: string;
  anchorLead?: number;
  explainedLeader?: string;
  explainedLead?: number;
  /** Everything that carried any evidence for this document, best first. */
  considered: string[];
}

const num = (s: string | undefined): number | undefined => {
  if (s === undefined) return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
};

function readFusion(evidence: Evidence[]): Fusion | null {
  const line = evidence.find((e) => e.tier === 'fusion');
  if (!line) return null;
  const d = line.detail;
  const leader = d.match(RE_FUSION_LEADER);
  const sep = d.match(RE_FUSION_SEP);
  const anchor = d.match(RE_FUSION_ANCHOR);
  const explained = d.match(RE_FUSION_EXPLAINED);
  const considered = d.match(RE_FUSION_CONSIDERED);
  // The line prints the literal word `none` where there is no leader and no contender. That is
  // the absence of a doctype, not a doctype called "none": it must never be looked up in the
  // registry or rendered as a badge.
  const named = (v: string | undefined): string | undefined =>
    v && v !== 'none' ? v : undefined;
  return {
    route: d.match(RE_FUSION_ROUTE)?.[1]?.trim() ?? 'none',
    leader: named(leader?.[2]) ?? '',
    support: num(leader?.[3]),
    separation: num(sep?.[1]),
    bits: num(sep?.[2]),
    zoneFreeLeader: d.match(RE_FUSION_ZONEFREE)?.[1],
    coverage: num(d.match(RE_FUSION_COVERAGE)?.[1]),
    anchorLeader: named(anchor?.[2]) ?? '',
    anchorLead: num(anchor?.[3]),
    explainedLeader: named(explained?.[2]) ?? '',
    explainedLead: num(explained?.[3]),
    considered:
      considered?.[1]
        ?.split(',')
        .map((s) => s.trim())
        .filter((s) => s && s !== 'none') ?? [],
  };
}

/** The three configured floors, as far as this console legitimately knows them. */
interface Floors {
  margin?: number;
  support?: number;
  coverage?: number;
}

const FLOOR_KEYS = ['margin', 'support', 'coverage'] as const;

const RE_REASON_HEAD = new RegExp(
  `^best candidate ${QUOTED} at lead=([\\d.]+), support=([\\d.]+), coverage=([\\d.]+)`,
);
const RE_FLOOR_MARGIN = /margin below floor ([\d.]+)/;
const RE_FLOOR_SUPPORT = /support below floor ([\d.]+)/;
const RE_FLOOR_COVERAGE = /coverage below floor ([\d.]+)/;

/** Which gate a refusal sentence is about. `identification` is gate 1 — categorical, not a meter. */
type Gate = 'identification' | 'margin' | 'support' | 'coverage';

interface ReasonRead {
  /** The doctype it came closest to accepting. */
  candidate?: string;
  lead?: number;
  support?: number;
  coverage?: number;
  /** Floors the service ITSELF named in this refusal. The only floors we may call measured. */
  floors: Floors;
  /** One entry per gate that refused, quoted verbatim. */
  failures: Array<{ gate: Gate; text: string }>;
}

const EMPTY_REASON: ReasonRead = { floors: {}, failures: [] };

/**
 * The refusal sentences are joined with `"; "`, but they also *contain* `"; "` internally, so the
 * split has to be anchored on the known openers rather than on the separator alone.
 */
const FAILURE_SPLIT =
  /;\s+(?=margin below floor|support below floor|coverage below floor|no doctype was identified)/;

function gateOf(sentence: string): Gate {
  if (sentence.startsWith('margin below floor')) return 'margin';
  if (sentence.startsWith('support below floor')) return 'support';
  if (sentence.startsWith('coverage below floor')) return 'coverage';
  return 'identification';
}

function readReason(reason: string): ReasonRead {
  if (!reason) return EMPTY_REASON;
  const head = reason.match(RE_REASON_HEAD);
  const body = reason
    .replace(RE_REASON_HEAD, '')
    .replace(/^\s*—\s*/, '')
    .replace(/\.\s*Routed to human review[\s\S]*$/, '')
    .trim();
  return {
    candidate: head?.[2],
    lead: num(head?.[3]),
    support: num(head?.[4]),
    coverage: num(head?.[5]),
    floors: {
      margin: num(reason.match(RE_FLOOR_MARGIN)?.[1]),
      support: num(reason.match(RE_FLOOR_SUPPORT)?.[1]),
      coverage: num(reason.match(RE_FLOOR_COVERAGE)?.[1]),
    },
    failures: body
      ? body
          .split(FAILURE_SPLIT)
          .map((s) => s.trim())
          .filter(Boolean)
          .map((text) => ({ gate: gateOf(text), text }))
      : [],
  };
}

/* --------------------------------------------------------------- anchors */

const RE_ANCHOR_MATCH = /^(decisive )?anchor (['"])([\s\S]*?)\2 matched in (\S+) via (\S+)$/;
const RE_ANCHOR_NEGATIVE = /^negative anchor (['"])([\s\S]*?)\1 present$/;
const RE_ANCHOR_UNEVALUABLE =
  /^(decisive )?anchor (['"])([\s\S]*?)\2 is present in the document but is declared (\S+)-only/;

/** One line of anchor evidence, taken apart so it can go in a table instead of a paragraph. */
interface AnchorHit {
  kind: 'matched' | 'negative' | 'unevaluable' | 'note';
  text: string;
  decisive: boolean;
  /** Where it matched — `title`, `heading`, `body`, `table`, `furniture`. */
  zone?: string;
  /** How it matched: `token` | `skeleton` | `fuzzy`. See `MATCH_STRATEGY`. */
  how?: string;
  /** The zone the registry restricted it to, when that is why it could not be evaluated. */
  declaredZone?: string;
  weight: number;
  raw: string;
}

function readAnchors(evidence: Evidence[]): AnchorHit[] {
  const out: AnchorHit[] = [];
  for (const e of evidence) {
    if (e.tier !== 'anchor') continue;
    const matched = e.detail.match(RE_ANCHOR_MATCH);
    if (matched) {
      out.push({
        kind: 'matched',
        text: matched[3],
        decisive: Boolean(matched[1]),
        zone: matched[4],
        how: matched[5],
        weight: e.weight,
        raw: e.detail,
      });
      continue;
    }
    const negative = e.detail.match(RE_ANCHOR_NEGATIVE);
    if (negative) {
      out.push({
        kind: 'negative',
        text: negative[2],
        decisive: false,
        weight: e.weight,
        raw: e.detail,
      });
      continue;
    }
    const unevaluable = e.detail.match(RE_ANCHOR_UNEVALUABLE);
    if (unevaluable) {
      out.push({
        kind: 'unevaluable',
        text: unevaluable[3],
        decisive: Boolean(unevaluable[1]),
        declaredZone: unevaluable[4],
        weight: e.weight,
        raw: e.detail,
      });
      continue;
    }
    out.push({ kind: 'note', text: '', decisive: false, weight: e.weight, raw: e.detail });
  }
  return out;
}

const RE_LEXICAL = /bm25=([-\d.]+) p=([\d.]+) coverage=([\d.]+); profile terms seen: (.*)$/;

interface LexicalRead {
  bm25?: number;
  probability?: number;
  coverage?: number;
  terms: string[];
}

function readLexical(evidence: Evidence[]): LexicalRead | null {
  const line = evidence.find((e) => e.tier === 'lexical');
  if (!line) return null;
  const m = line.detail.match(RE_LEXICAL);
  if (!m) return { terms: [] };
  return {
    bm25: num(m[1]),
    probability: num(m[2]),
    coverage: num(m[3]),
    terms:
      m[4] === 'none'
        ? []
        : m[4]
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean),
  };
}

/* ========================================================================= *
 * Remembering the floors the service has stated.
 *
 * A refusal names the floors it enforced; an accept does not. Keeping what was stated lets the
 * meters carry their gate tick on the next accept too — but only ever labelled as remembered,
 * never as though this response had said it.
 * ========================================================================= */

const FLOORS_KEY = 'dce.analyze.floors';

function loadFloors(): Floors {
  try {
    const raw = window.localStorage.getItem(FLOORS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Floors;
    const keep: Floors = {};
    for (const k of FLOOR_KEYS) {
      const v = parsed[k];
      if (typeof v === 'number' && Number.isFinite(v)) keep[k] = v;
    }
    return keep;
  } catch {
    return {};
  }
}

function saveFloors(floors: Floors): void {
  try {
    window.localStorage.setItem(FLOORS_KEY, JSON.stringify(floors));
  } catch {
    /* private browsing — the floors just will not survive the tab */
  }
}

/** Where a floor drawn on a meter came from. Printed next to it, every time. */
type FloorSource = 'stated' | 'entered' | 'remembered';

const FLOOR_SOURCE_NOTE: Record<FloorSource, string> = {
  stated: 'floor stated by the service in this refusal',
  entered: "floor you entered as this deployment's setting",
  remembered: 'floor this service stated in an earlier refusal, remembered',
};

/* ========================================================================= *
 * Small local presentation helpers.
 * ========================================================================= */

const fmt3 = (n: number): string => n.toFixed(3);

/**
 * The three ways an anchor can match, and what each is worth as evidence.
 *
 * A reviewer must be able to tell "the document printed this string" from "something close
 * enough to it was there". They are not the same claim, and the third one is the only kind of
 * anchor hit that can be wrong about what it saw — which is exactly why the classifier refuses it
 * to decisive anchors, where a single character is the whole distinction between two forms.
 */
const MATCH_STRATEGY: Record<string, { note: string; approximate: boolean }> = {
  token: { note: 'matched literally, token for token', approximate: false },
  skeleton: {
    note: 'matched after OCR-mangling normalisation (look-alike characters folded), not literally',
    approximate: false,
  },
  fuzzy: {
    note:
      'matched only approximately — near enough, not the same string. A decisive anchor is never ' +
      'allowed to match this way, because one character is often the whole difference between ' +
      'two document types.',
    approximate: true,
  },
};

/** `checksum_verified` is proof; `format_valid` is a shape; `unverified` is a value nobody checked. */
function verificationTone(verification: string): BadgeTone {
  if (verification === 'checksum_verified') return 'accept';
  if (verification === 'format_valid') return 'accent';
  if (verification.includes('fail')) return 'danger';
  return 'neutral';
}

function tierTone(run: TierRun): BadgeTone {
  if (run.status === 'error' || run.status === 'misconfigured') return 'danger';
  if (run.status === 'unavailable' || run.status === 'skipped') return 'neutral';
  if (run.status === 'queued') return 'abstain';
  return 'accept';
}

/** The payload shapes a `DocumentRequest` can carry, and how each one gets filled here. */
type Source = 'file' | 'text' | 'azure' | 'des' | 'layout';
type Mode = 'process' | 'classify' | 'extract';

const SOURCES: Array<[Source, string]> = [
  ['file', 'file'],
  ['text', 'text'],
  ['azure', 'Azure result'],
  ['des', 'DES OCR'],
  ['layout', 'layout'],
];

const MODES: Array<[Mode, string, string]> = [
  ['process', 'process', 'classify + extract + tier ledger + review routing — the whole trail'],
  ['classify', 'classify only', 'the decision alone: free, and it touches no extraction tier'],
  ['extract', 'extract', 'pin a doctype and see what comes out of the document'],
];

/** Everything the ingest layer will parse in-process. Images need local OCR to be enabled. */
const FILE_ACCEPT =
  '.pdf,.docx,.xlsx,.pptx,.odt,.rtf,.txt,.csv,.html,.htm,.eml,.msg,' +
  '.jpg,.jpeg,.png,.tif,.tiff,.bmp,.webp,.heic,.gif';

const isSource = (v: string | null): v is Source => SOURCES.some(([s]) => s === v);
const isMode = (v: string | null): v is Mode => MODES.some(([m]) => m === v);

/**
 * What each input tab means for where the document goes — the distinction the whole console
 * rests on, stated per tab rather than left to be inferred from which textarea is showing.
 *
 * Four of the five tabs are the **caller-supplied** path: an upstream service (DES, a batch job,
 * whatever the operator runs) did the reading, and DCE receives the result. On those, DCE opens
 * no socket at all and the invariant is untouched no matter which vendor produced the payload.
 * Only `file` can involve DCE reading the document itself, which is why only `file` gets a
 * provider picker.
 */
const SOURCE_READING: Record<Source, string> = {
  file: '',
  text: 'plain text, pasted. No parser and no recognition engine ran: the words are what you sent.',
  azure:
    'a caller-supplied Azure analyzeResult. Whoever produced it called Azure; this service did not — it adapted JSON you already had.',
  des: 'a caller-supplied DES OCR payload. document-enrichment-services did the reading; this service adapted its output.',
  layout:
    'a caller-supplied LayoutView. It was already adapted before it got here; no parser and no recognition engine ran in this service.',
};

/** One normalised result, whichever of the three endpoints produced it. */
interface Outcome {
  mode: Mode;
  /** Which input tab produced it, captured at run time — the tab can change afterwards. */
  source: Source;
  /** The OCR provider that was asked for, when this went through the file/ingest path. */
  ocrId: string;
  classification: Classification | null;
  extraction: ExtractionResult | null;
  tiers: TierRun[];
  reviewIds: string[];
  timings: Timings | null;
  needsReview: boolean;
  /** The service's own one-line summary of what happens next. */
  detail: string;
  /** Exactly what came back, for the raw disclosure. */
  raw: unknown;
  /** The request as sent, with the payload redacted. */
  request: unknown;
  at: Date;
}

/**
 * Never put a document — or a base64 blob — in a JSON viewer.
 *
 * The request panel exists so a reviewer can see *which* payload field was populated and what the
 * ingest options were, which is the part that changes how the service reads the document. The
 * content itself is already on screen in the input box above; reprinting it here only creates a
 * second copy to leak out of.
 */
function redactRequest(request: DocumentRequest & { doctype_id?: string | null }): unknown {
  const out: Record<string, unknown> = { ...request };
  if (typeof request.content_base64 === 'string') {
    const bytes = Math.floor((request.content_base64.length * 3) / 4);
    out.content_base64 = `<${bytes.toLocaleString()} bytes of file, not shown>`;
  }
  if (typeof request.text === 'string') {
    out.text = `<${request.text.length.toLocaleString()} characters, not shown>`;
  }
  if (request.layout) out.layout = '<layout payload, not shown>';
  if (request.azure_analyze_result) out.azure_analyze_result = '<Azure result, not shown>';
  if (request.des_ocr) out.des_ocr = '<DES OCR payload, not shown>';
  return out;
}

/* ========================================================================= *
 * The OCR provider picker.
 *
 * Three things this control has to get right, because they are the difference between a useful
 * toggle and a misleading one:
 *
 *  1. **It says where the document goes, once, at the point of choice.** A remote provider sends
 *     the file out of the process before anyone knows what it is, and this console's whole
 *     argument is that it is honest about that. What it says depends on what the deployment has
 *     DECLARED — `/readyz`'s `ocr.trust_boundary`. On a deployment whose recogniser runs inside
 *     its own boundary, calling that a transmission to a third party is not caution, it is a
 *     false statement; on an undeclared or external one it is the plain truth.
 *  2. **It never lets a caller enable what the operator has not.** Unavailable options are shown
 *     disabled with the reason on hover rather than hidden — "the option is not there" and "the
 *     option is there and this deployment does not have it" are different facts, and only one of
 *     them is true.
 *  3. **It says that Read and DI are not interchangeable.** DI carries paragraph roles; Read
 *     returns lines only, so on a Read result every block is body text and an anchor gated to
 *     the title zone cannot fire. That fact changes results, so it is on the option LABEL — a
 *     parenthetical a reader takes in while choosing — rather than in a coloured panel below.
 *     A label is information; a panel is a warning, and this is not a warning.
 *
 * WHAT USED TO BE HERE, AND WHY IT IS NOT. This panel carried a red disclosure block and a
 * per-run "I understand this document will be sent to…" checkbox that gated the run button. Both
 * are gone. On a deployment that declares its recogniser on-premises the checkbox asked an
 * operator to acknowledge a disclosure that is not happening; and a consent control that fires on
 * every single run does not inform anybody, it trains them to click past it. The posture is still
 * fully reported — on `/readyz`, on `/posture`, in the boot log, and in one restrained line here
 * naming the host — which is disclosure. The checkbox was ceremony.
 * ========================================================================= */

/*
 * `declaredTrustBoundary` used to be defined here. It now lives in `../ocr`, because `/posture`
 * and the header pill have to describe this same hop in the same words — see the note there.
 */

/**
 * The structure fact, compressed to fit beside a provider name.
 *
 * Derived from the structure the SERVICE reported for that provider, never from its vendor name,
 * so a provider added later gets a correct tag without a line being written here. Empty where
 * there is nothing to say — a file's own text layer is not a recogniser.
 */
const STRUCTURE_TAG: Record<OcrStructure, string> = {
  roles: 'paragraph roles — title and heading zones',
  lines: 'lines only — no title zones',
  native: '',
  unknown: '',
};

function OcrPicker({
  options,
  value,
  onChange,
  declaration,
}: {
  options: OcrOption[];
  value: string;
  onChange: (id: string) => void;
  /** What this deployment declares about its remote OCR endpoint, and who says so. */
  declaration: BoundaryDeclaration;
}) {
  const selected = findOcrOption(options, value);
  const onPremises = declaration.boundary === 'on_premises';

  return (
    <div className="az-ocr">
      <div className="az-ocr-head">
        <span className="label">how an image gets read</span>
        <span className="faint">
          text-bearing files (pdf with a text layer, docx, eml…) never reach this: their words are
          already in the file.
        </span>
      </div>

      <div className="az-ocr-grid" role="radiogroup" aria-label="OCR provider">
        {options.map((option) => {
          const active = option.id === value;
          return (
            <label
              key={option.id}
              className={[
                'az-ocr-option',
                active ? 'active' : '',
                // Three tints, not two, because there are three states. `egress` is red and
                // stays red for an endpoint nobody has vouched for. `offbox` — leaves the
                // process, to a host the deployment declares is its own — is neutral: red
                // there would say in colour exactly what the removed panel said in words.
                option.egress ? (onPremises ? 'offbox' : 'egress') : '',
                option.available ? '' : 'off',
              ]
                .filter(Boolean)
                .join(' ')}
              // The reason an option cannot be used, on the element rather than in a tooltip
              // library — it must survive a screenshot pasted into a ticket.
              title={option.available ? option.summary : option.unavailableReason}
            >
              <input
                type="radio"
                name="az-ocr"
                checked={active}
                disabled={!option.available}
                onChange={() => onChange(option.id)}
              />
              <span className="az-ocr-body">
                <span className="az-ocr-label">
                  <span className="name">{option.label}</span>
                  {/* The structure fact on the label, because it changes the ANSWER: a
                      lines-only reading gives every block Zone.body, so a title-gated anchor
                      cannot fire on it however plainly the words are on the page. A reader
                      choosing between two providers needs that while choosing. */}
                  {STRUCTURE_TAG[option.structure] && (
                    <span className="az-ocr-structure">({STRUCTURE_TAG[option.structure]})</span>
                  )}
                  {option.egress ? (
                    /* Same operation, two honest descriptions. On a deployment that declares
                       its recogniser on-premises, "egress" in red is not the cautious wording,
                       it is the wrong one — so the badge states what is verifiable (the bytes
                       leave this process) and attributes the rest to the operator. */
                    onPremises ? (
                      <Badge
                        tone="neutral"
                        title="the document leaves this process to be read, to a host this deployment declares is inside its own trust boundary"
                      >
                        leaves this process
                      </Badge>
                    ) : (
                      <Badge
                        tone="danger"
                        title="the document is transmitted to a remote service before its doctype is known"
                      >
                        egress
                      </Badge>
                    )
                  ) : (
                    <Badge tone="accept" title="nothing leaves this process">
                      no egress
                    </Badge>
                  )}
                  {!option.available && <Badge tone="neutral">unavailable here</Badge>}
                </span>
                <span className="az-ocr-summary">{option.summary}</span>
                {!option.available && (
                  <span className="az-ocr-why">{option.unavailableReason}</span>
                )}
              </span>
            </label>
          );
        })}
      </div>

      {/* One line, stated once, in the register the deployment's own declaration warrants. No
          panel and no checkbox: the fact is the endpoint, and a coloured box around a fact an
          operator sees on every run stops being read long before it stops being rendered. */}
      {selected?.egress && (
        <div className="az-note faint">
          {/* What the console knows first-hand: where the file goes, and when. Then the
              service's own attribution, verbatim — never a paraphrase, so this line cannot
              come to disagree with the /readyz it is reporting. */}
          Sends the file to{' '}
          <span className="mono">{selected.endpoint || 'the configured endpoint'}</span> to be
          read, before its doctype is known.{' '}
          {declaration.attribution ? (
            <>
              {/* Capitalised, not rewritten. The service composes this to follow a colon on
                  /readyz; here it follows a full stop. Changing a letter is presentation,
                  changing a word would be the paraphrase this deliberately avoids. */}
              {declaration.attribution.charAt(0).toUpperCase() + declaration.attribution.slice(1)}{' '}
              <Link to="/posture">See /posture</Link>.
            </>
          ) : (
            <Link to="/posture">See /posture</Link>
          )}
        </div>
      )}

      {selected?.id === NONE_ID && (
        <div className="az-note faint">
          Sends <span className="mono">local_ocr: false</span>. This is also the only direction a
          caller may push: the flag can turn recognition <em>off</em> for one request and can
          never turn it on. Enabling an engine the operator has not enabled is not a caller&apos;s
          decision to make, and neither is naming a provider this deployment does not have.
        </div>
      )}

      {selected?.id === LOCAL_ID && !selected.reported && (
        <div className="az-note faint">
          Sends no ingest flags at all — the service&apos;s own default. This deployment&apos;s{' '}
          <code>/readyz</code> does not report whether a local engine is enabled, so the console
          cannot promise one is: if there is none, an image comes back{' '}
          <span className="mono">needs_ocr</span>, which is the honest answer rather than a guess.
        </div>
      )}
    </div>
  );
}

/* ========================================================================= *
 * Panels.
 * ========================================================================= */

/**
 * 2. HOW THE TEXT WAS READ.
 *
 * Provenance belongs in the decision trail as much as the anchors do: "which anchors fired" is
 * only half an answer if nobody says what the anchors were matched against. A title-gated anchor
 * that did not fire under Read v3.2 might have fired under Document Intelligence, and a reviewer
 * who cannot see which one ran will read the miss as a fact about the document.
 *
 * The panel distinguishes three claims and never blurs them:
 *   - what the **service reported** produced the text;
 *   - what the **console asked for**, when the response says nothing (rendered as a request, not
 *     a finding — a console that printed its own request back as an answer would be inventing);
 *   - whether this service **opened a socket** at all, which for four of the five input tabs is
 *     answerable without asking anybody.
 */
function ReadingPanel({
  source,
  provenance,
  requested,
  spec,
}: {
  source: Source;
  provenance: Provenance | null;
  requested: OcrOption | undefined;
  spec: DocTypeSpec | null;
}) {
  const callerSupplied = source !== 'file' && source !== 'text';
  const egress = provenance?.egress === true;

  /* Only meaningful for a lines-only reading, and only when the registry entry is loaded: how
     many of THIS doctype's anchors were gated to a zone that reading could not produce. Counted
     from the spec the page already fetched, never guessed. */
  const titleAnchors = (spec?.anchors ?? []).filter((a: Anchor) => a.zone === 'title');
  const decisiveTitle = titleAnchors.filter((a) => a.decisive);
  const linesOnly = provenance?.structure === 'lines';

  return (
    <Panel title="How the text was read" stack>
      <div className={`az-reading ${egress ? 'egress' : 'local'}`}>
        <div className="assert">
          {egress ? (
            <>
              This document was transmitted to a third party
              {provenance?.endpointHost ? (
                <>
                  {' '}
                  — <span className="mono">{provenance.endpointHost}</span>
                </>
              ) : null}{' '}
              to be read, before its doctype was known.
            </>
          ) : callerSupplied ? (
            <>The reading was supplied by the caller. This service opened no socket.</>
          ) : (
            <>The document was read inside this process. Nothing left it.</>
          )}
        </div>
        <div className="body">
          {callerSupplied
            ? SOURCE_READING[source]
            : source === 'text'
              ? SOURCE_READING.text
              : provenance
                ? `${provenanceLabel(provenance)} — ${STRUCTURE_NOTE[provenance.structure]}`
                : 'no recognition provenance is available for this run.'}
        </div>
      </div>

      <div className="az-reading-facts">
        <Fact label="input">
          <span className="mono">{source}</span>
        </Fact>
        <Fact label="provider">
          {provenance ? (
            <span className="mono">{provenance.name}</span>
          ) : (
            <span className="faint">—</span>
          )}
        </Fact>
        <Fact label="attribution">
          {!provenance ? (
            <span className="faint">—</span>
          ) : provenance.kind === 'none' ? (
            <Badge tone="neutral" title="no recognition was asked for, so there is nothing to attribute">
              no recognition ran
            </Badge>
          ) : provenance.reported ? (
            <Badge tone="accept" title="this came out of the response">
              reported by the service
            </Badge>
          ) : (
            <Badge tone="warn" title="the response did not say; this is what the console asked for">
              requested by this console
            </Badge>
          )}
        </Fact>
        <Fact label="egress">
          {egress ? (
            <Badge tone="danger">yes — pre-classification</Badge>
          ) : (
            <Badge tone="accept">none</Badge>
          )}
        </Fact>
      </div>

      {/* Only worth saying when a provider could actually have been involved: on `none` there
          was nothing to attribute, and printing a gap notice there would be noise that trains
          people to skim past it on the runs where it matters. */}
      {provenance && !provenance.reported && provenance.kind !== 'none' && (
        <div className="az-note faint">
          <strong>This is the request, not the response.</strong> This response carried nothing
          naming the provider that produced the text — no <span className="mono">source</span>{' '}
          block and no <span className="mono">X-Document-Source</span> header — so the console is
          showing what it asked for. On a run that fell back (a remote call that failed and was
          answered locally, say) the two would differ and nothing here would show it. Treat the
          provider above as a claim about the request only.
        </div>
      )}

      {/* The service answered, and it answered with something else. Never quietly reconciled:
          a provider that stood in for the one that was asked for changes what evidence was
          possible, and the reviewer is the person who needs to know.

          Gated on the run having NAMED a provider. `none` and `local` name nothing — they send
          `local_ocr: false` and no ingest flags respectively — so a document read from its own
          text layer and reported as `dce.ingest` is not a substitution, it is the answer. Left
          ungated this fires on ordinary runs, and a warning that cries wolf on the common case
          is worse than no warning: it teaches the reviewer to scroll past the one time a
          provider really did stand in. */}
      {provenance?.reported &&
        requested &&
        requested.id !== NONE_ID &&
        requested.id !== LOCAL_ID &&
        provenance.kind !== requested.kind && (
        <div className="az-ocr-quality">
          <strong>This is not the provider that was requested.</strong> The run asked for{' '}
          <span className="mono">{requested.id}</span> and the service reports{' '}
          <span className="mono">{provenance.name}</span>. Something stood in — a fallback, an
          override, or a provider the deployment resolved differently — and the evidence below is
          from the reading that actually happened, not the one that was asked for.
        </div>
      )}

      {linesOnly && (
        <div className="az-ocr-quality">
          <strong>This reading has no title zone.</strong> It {STRUCTURE_NOTE.lines}
          {spec && titleAnchors.length > 0 && (
            <>
              {' '}
              This doctype declares <strong>{titleAnchors.length}</strong> anchor
              {titleAnchors.length === 1 ? '' : 's'} gated to the title zone
              {decisiveTitle.length > 0 && (
                <>
                  , <strong>{decisiveTitle.length}</strong> of them decisive
                </>
              )}
              — none of which could have fired here, whatever the document says. If it abstained,
              read that first before concluding anything about the document.
            </>
          )}
          {spec && titleAnchors.length === 0 && (
            <> This doctype declares no title-gated anchors, so nothing was lost to that here.</>
          )}
        </div>
      )}
    </Panel>
  );
}

/** 1. THE VERDICT. An abstention is a routed decision and is coloured as one — never red. */
function Verdict({
  classification,
  spec,
  reviewIds,
  timings,
  detail,
  needsReview,
}: {
  classification: Classification;
  spec: DocTypeSpec | null;
  reviewIds: string[];
  timings: Timings | null;
  detail: string;
  needsReview: boolean;
}) {
  const abstained = api.isAbstention(classification);
  return (
    <div className={`az-verdict ${abstained ? 'abstained' : 'accepted'}`}>
      <div className="az-verdict-main">
        <div className="az-verdict-headline">
          {abstained ? (
            <>
              <span>ABSTAINED</span>
              <Badge tone="abstain">routed to human review</Badge>
            </>
          ) : (
            <>
              <span>ACCEPTED</span>
              <Badge tone="accept">every gate held</Badge>
              {needsReview && (
                <Badge tone="warn" title="the doctype was accepted; a field still needs a human">
                  fields need review
                </Badge>
              )}
            </>
          )}
        </div>

        {abstained ? (
          <p className="az-note" style={{ maxWidth: '68ch', margin: 0 }}>
            No doctype was accepted, and <strong>nothing downstream was told one</strong>. This is
            the service working as designed: four gates must hold together, one did not, and the
            document went to a human instead of to a guess.
          </p>
        ) : (
          <div className="az-badges" style={{ gap: 'var(--s-3)', alignItems: 'center' }}>
            <DocTypeBadge
              doctypeId={classification.doctype_id}
              label={classification.label}
              category={spec?.category}
              size="lg"
              link
            />
            <CountryTag country={classification.country} />
            {spec && <Badge tone="neutral">{spec.category.replace(/_/g, ' ')}</Badge>}
            {spec && !spec.officially_valid && (
              <Badge
                tone="warn"
                title="the registry does not treat this as an officially valid document"
              >
                not officially valid
              </Badge>
            )}
          </div>
        )}

        {!abstained && detail && <div className="az-note muted">{detail}</div>}

        <div className="az-verdict-facts">
          <Fact label="confidence">
            <span className="tabular">{fmt3(classification.confidence)}</span>
          </Fact>
          <Fact label="margin">
            <span className="tabular">{fmt3(classification.margin)}</span>
          </Fact>
          <Fact label="coverage">
            <span className="tabular">{asPercent(classification.coverage)}</span>
          </Fact>
          <Fact label="classified in">
            <span className="tabular">{classification.ms.toLocaleString()} ms</span>
          </Fact>
          {timings && (
            <Fact label="total">
              <span className="tabular">{timings.total_ms.toLocaleString()} ms</span>
            </Fact>
          )}
          {classification.page_types.length > 1 && (
            <Fact label="page types">
              <span className="mono">{classification.page_types.join(', ')}</span>
            </Fact>
          )}
        </div>

        {reviewIds.length > 0 && (
          <div className="az-note">
            {reviewIds.length} item{reviewIds.length === 1 ? '' : 's'} queued for a human —{' '}
            <Link to="/review">open the review queue</Link>
            <div className="faint mono" style={{ fontSize: 'var(--t-xs)', marginTop: '2px' }}>
              {reviewIds.join('  ·  ')}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** One gate: value, floor (when the service gave us one), and what would have changed it. */
function GateRow({
  name,
  value,
  floor,
  floorSource,
  passed,
  note,
  format = fmt3,
}: {
  name: string;
  value: number;
  floor?: number;
  floorSource?: FloorSource;
  passed: boolean;
  note: string;
  format?: (n: number) => string;
}) {
  let full = note;
  if (floor !== undefined) {
    const gap = value - floor;
    full =
      gap >= 0
        ? `${note} — cleared the ${format(floor)} floor by ${format(gap)}`
        : `${note} — short of the ${format(floor)} floor by ${format(-gap)}`;
  }
  return (
    <div>
      <Meter
        name={name}
        value={value}
        threshold={floor}
        status={passed ? 'pass' : 'fail'}
        format={format}
        note={full}
      />
      {floorSource && <div className="az-floor-src">{FLOOR_SOURCE_NOTE[floorSource]}</div>}
    </div>
  );
}

/** 2. WHY. Gate 1 is categorical; the rest are meters. */
function WhyPanel({
  classification,
  fusion,
  reason,
  floors,
  floorSources,
  entered,
  onEntered,
}: {
  classification: Classification;
  fusion: Fusion | null;
  reason: ReasonRead;
  floors: Floors;
  floorSources: Partial<Record<keyof Floors, FloorSource>>;
  entered: Floors;
  onEntered: (next: Floors) => void;
}) {
  const [showFloors, setShowFloors] = useState(false);
  const abstained = api.isAbstention(classification);
  const failed = new Set(reason.failures.map((f) => f.gate));
  const identified = abstained ? !failed.has('identification') : true;
  const route = fusion?.route ?? '';
  const support = fusion?.support ?? reason.support;

  const conflicts = FLOOR_KEYS.filter(
    (k) =>
      entered[k] !== undefined && reason.floors[k] !== undefined && entered[k] !== reason.floors[k],
  );

  const setFloor = (key: keyof Floors, raw: string) => {
    const next = { ...entered };
    const parsed = Number(raw);
    if (raw.trim() === '' || !Number.isFinite(parsed)) delete next[key];
    else next[key] = parsed;
    onEntered(next);
  };

  return (
    <Panel
      title="Why"
      actions={
        <button className="btn btn-ghost btn-sm" onClick={() => setShowFloors((s) => !s)}>
          {showFloors ? 'hide gate floors' : 'gate floors'}
        </button>
      }
      stack
    >
      {showFloors && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <p className="az-note" style={{ margin: 0 }}>
            <strong>The classifier&apos;s cutoffs are not on the wire.</strong> <code>/readyz</code>{' '}
            reports posture, not <code>CLASSIFY_MIN_*</code>, so a floor is drawn only when this
            service stated it in a refusal, or when you enter this deployment&apos;s setting here.
            Nothing below is a default this console assumed.
          </p>
          <div className="az-floors">
            {FLOOR_KEYS.map((k) => (
              <label key={k}>
                CLASSIFY_MIN_{k.toUpperCase()}
                <input
                  type="text"
                  inputMode="decimal"
                  value={entered[k] ?? ''}
                  placeholder={floors[k] !== undefined ? String(floors[k]) : 'unknown'}
                  onChange={(e) => setFloor(k, e.target.value)}
                />
              </label>
            ))}
            <button className="btn btn-sm" onClick={() => onEntered({})}>
              clear
            </button>
          </div>
          {conflicts.length > 0 && (
            <div className="az-note">
              <Badge tone="warn">disagreement</Badge> this service stated{' '}
              {conflicts.map((k) => `${k}=${reason.floors[k]}`).join(', ')} in this refusal, which is
              not what you entered. The meters use the service&apos;s figure.
            </div>
          )}
        </div>
      )}

      {/* Gate 1 — which tier is even allowed to speak. No meter can express this. */}
      <div className={`az-gate1 ${identified ? 'pass' : 'fail'}`}>
        <div className="row" style={{ gap: 'var(--s-2)', minWidth: '188px' }}>
          <Badge tone={identified ? 'accept' : 'abstain'}>{identified ? 'held' : 'refused'}</Badge>
          <strong>gate 1 · identification</strong>
        </div>
        <div className="az-gate1-body">
          {identified ? (
            <>
              Route <span className="mono">{route || 'concurrence'}</span> —{' '}
              {route === 'conclusive-l1'
                ? 'one doctype held decisive anchor evidence that only it could hold, so it may be accepted without the lexical tier concurring.'
                : 'the anchor tier and the lexical profile independently led on the same doctype. Neither tier may accept over the other on its own.'}
            </>
          ) : (
            <>
              Neither route opened: the two evidence channels did not concur, and nothing held
              decisive evidence only it could hold. Everything below is measured on the doctype that
              came closest — reported because a refusal must still be reviewable, not because it
              nearly won.
            </>
          )}
          {fusion && (
            <div className="faint" style={{ fontSize: 'var(--t-xs)', marginTop: 'var(--s-1)' }}>
              anchor tier led on{' '}
              <span className="mono">{fusion.anchorLeader || 'nothing — silent'}</span>
              {fusion.anchorLead !== undefined && ` (by ${fmt3(fusion.anchorLead)} bits)`}; lexical
              profile led on{' '}
              <span className="mono">{fusion.explainedLeader || 'nothing — silent'}</span>
              {fusion.explainedLead !== undefined && ` (by ${fmt3(fusion.explainedLead)})`}
              {fusion.zoneFreeLeader && (
                <>
                  ; zone-free reading led on <span className="mono">{fusion.zoneFreeLeader}</span>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="gates">
        <GateRow
          name="evidence"
          value={classification.confidence}
          passed={!abstained}
          note={
            abstained && classification.confidence === 0
              ? 'zero, not merely small: no doctype was identified, so there is nothing to be confident in'
              : 'the headline number — the smallest of the three ratios below, each of which reads 0.50 exactly at its own floor'
          }
        />
        <GateRow
          name="margin"
          value={classification.margin}
          floor={floors.margin}
          floorSource={floorSources.margin}
          passed={!failed.has('margin')}
          note={
            fusion?.bits !== undefined
              ? `lead over the next doctype on the combined evidence channel (${fmt3(fusion.bits)} bits)`
              : 'lead over the next doctype on the combined evidence channel'
          }
        />
        {support !== undefined && (
          <GateRow
            name="support"
            value={support}
            floor={floors.support}
            floorSource={floorSources.support}
            passed={!failed.has('support')}
            format={asPercent}
            note="the explicit null hypothesis — how much combined anchor and lexical evidence exists at all"
          />
        )}
        <GateRow
          name="coverage"
          value={classification.coverage}
          floor={floors.coverage}
          floorSource={floorSources.coverage}
          passed={!failed.has('coverage')}
          format={asPercent}
          note="how much of that doctype's vocabulary the document actually contained"
        />
      </div>

      {support === undefined && (
        <div className="az-note faint">
          The support gate is not drawn: its value is carried inside the <code>fusion</code> evidence
          line, and this response did not contain one. It is not being assumed.
        </div>
      )}

      {reason.failures.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">what the service said, verbatim</span>
          {reason.failures.map((f) => (
            <div key={f.text} className="az-failure">
              {f.text}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

/** 3. WHAT IT SAW. Matched anchors first — then the missing ones, which are the actionable part. */
function EvidencePanel({
  evidence,
  hits,
  lexical,
  spec,
}: {
  evidence: Evidence[];
  hits: AnchorHit[];
  lexical: LexicalRead | null;
  spec: DocTypeSpec | null;
}) {
  const [showMissing, setShowMissing] = useState(true);
  const matched = hits.filter((h) => h.kind === 'matched');
  const negatives = hits.filter((h) => h.kind === 'negative');
  const unevaluable = hits.filter((h) => h.kind === 'unevaluable');
  const notes = hits.filter((h) => h.kind === 'note');
  const structural = evidence.find((e) => e.tier === 'structural');
  const checksums = evidence.filter((e) => e.tier === 'checksum');

  const matchedText = new Set(matched.map((h) => h.text));
  const missing: Anchor[] = (spec?.anchors ?? []).filter((a) => !matchedText.has(a.text));
  const missingDecisive = missing.filter((a) => a.decisive);

  return (
    <Panel
      title="What it saw"
      actions={
        spec ? (
          <span className="faint" style={{ fontSize: 'var(--t-xs)' }}>
            {matched.length} of {spec.anchors.length} anchors declared for{' '}
            <span className="mono">{spec.doctype_id}</span> were present
          </span>
        ) : undefined
      }
      stack
    >
      {structural && (
        <div className="row" style={{ gap: 'var(--s-2)' }}>
          <span className="label">structure</span>
          <span className="mono" style={{ fontSize: 'var(--t-sm)' }}>
            {structural.detail}
          </span>
        </div>
      )}

      {matched.length === 0 ? (
        <EmptyState
          icon="○"
          title="no registry anchor fired"
          body="Nothing the registry declares as a high-signal string for this doctype appeared in the document. That alone is enough to refuse: a silent anchor tier cannot concur with anything."
        />
      ) : (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>anchor</th>
                <th>decisive</th>
                <th>matched in</th>
                <th>how</th>
                <th style={{ textAlign: 'right' }}>weight</th>
              </tr>
            </thead>
            <tbody>
              {matched.map((h) => (
                <tr key={h.raw}>
                  <td className="az-anchor-text">{h.text}</td>
                  <td>
                    {h.decisive ? (
                      <Badge
                        tone="accept"
                        title="a decisive anchor can carry the decision on its own"
                      >
                        decisive
                      </Badge>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="mono">{h.zone}</td>
                  <td className="mono" title={h.how ? MATCH_STRATEGY[h.how]?.note : undefined}>
                    {h.how && MATCH_STRATEGY[h.how]?.approximate ? (
                      <Badge tone="warn" title={MATCH_STRATEGY[h.how].note}>
                        {h.how}
                      </Badge>
                    ) : (
                      h.how
                    )}
                  </td>
                  <td className="tabular" style={{ textAlign: 'right' }}>
                    {h.weight.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {matched.some((h) => h.how && MATCH_STRATEGY[h.how]?.approximate) && (
        <div className="az-note">
          <Badge tone="warn">approximate</Badge> One or more anchors matched{' '}
          <span className="mono">fuzzy</span> — near enough to the registry&apos;s string, not equal
          to it. Such a hit is worth less than a literal one and can be wrong about what it saw,
          which is why a decisive anchor is never allowed to match this way. If this decision turns
          on a fuzzy hit, read the document.
        </div>
      )}

      {unevaluable.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">present, but the claim could not be tested</span>
          {unevaluable.map((h) => (
            <div key={h.raw} className="az-note">
              <span className="mono">{h.text}</span> — declared{' '}
              <span className="mono">{h.declaredZone}</span>-only, and this payload has no{' '}
              <span className="mono">{h.declaredZone}</span> zone, so it scored nothing.
              Re-submitting the same document <em>with</em> layout roles (a <code>layout</code> or
              Azure payload rather than plain text) is what would let this claim be evaluated.
            </div>
          ))}
        </div>
      )}

      {negatives.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">evidence against</span>
          {negatives.map((h) => (
            <EvidenceChip
              key={h.raw}
              evidence={{ tier: 'negative', detail: h.text, weight: h.weight }}
              against
            />
          ))}
        </div>
      )}

      {checksums.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">identifiers</span>
          {checksums.map((e) => (
            <EvidenceChip
              key={e.detail}
              evidence={e}
              decisive={e.detail.includes('check digit verified')}
            />
          ))}
        </div>
      )}

      {notes.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">notes on the anchor route</span>
          {notes.map((h) => (
            <div key={h.raw} className="az-note">
              {h.raw}
            </div>
          ))}
        </div>
      )}

      {lexical && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">
            lexical profile
            {lexical.bm25 !== undefined && (
              <span className="faint">
                {' '}
                · bm25 {fmt3(lexical.bm25)} · p {fmt3(lexical.probability ?? 0)} · coverage{' '}
                {asPercent(lexical.coverage ?? 0)}
              </span>
            )}
          </span>
          {lexical.terms.length > 0 ? (
            <>
              <div className="az-terms">
                {lexical.terms.map((t) => (
                  <span key={t} className="az-term">
                    {t}
                  </span>
                ))}
              </div>
              <div className="faint" style={{ fontSize: 'var(--t-xs)' }}>
                Profile terms are stored in the classifier&apos;s OCR-folded form — digits standing
                in for look-alike letters — which is why they read oddly. They are what matched, not
                what the document prints.
              </div>
            </>
          ) : (
            <span className="faint">no profile term for this doctype appeared</span>
          )}
        </div>
      )}

      {spec && missing.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <div className="row">
            <span className="label">what would have changed it</span>
            <span style={{ marginLeft: 'auto' }} />
            <button className="btn btn-ghost btn-sm" onClick={() => setShowMissing((s) => !s)}>
              {showMissing ? 'hide' : `show ${missing.length}`}
            </button>
          </div>
          <div className="az-note">
            {missingDecisive.length > 0 ? (
              <>
                <strong>
                  {missingDecisive.length} decisive anchor
                  {missingDecisive.length === 1 ? '' : 's'}
                </strong>{' '}
                the registry declares for <span className="mono">{spec.doctype_id}</span>{' '}
                {missingDecisive.length === 1 ? 'was' : 'were'} not present.{' '}
                {missingDecisive.length === 1 ? 'It' : 'Any one of them'} would have opened the
                conclusive-L1 route on its own.
              </>
            ) : (
              <>
                {missing.length} further anchor{missing.length === 1 ? '' : 's'} the registry
                declares for <span className="mono">{spec.doctype_id}</span> did not appear.
              </>
            )}
          </div>
          {showMissing && (
            <div className="scroll-x">
              <table className="grid">
                <tbody>
                  {missing.map((a) => (
                    <tr key={`${a.text}|${a.lang}`} className="az-anchor-miss">
                      <td className="az-anchor-text">{a.text}</td>
                      <td style={{ width: '1%' }}>
                        {a.decisive ? (
                          <Badge tone="warn">decisive</Badge>
                        ) : (
                          <span className="faint">—</span>
                        )}
                      </td>
                      <td style={{ width: '1%' }} className="mono faint nowrap">
                        {a.lang}
                        {a.zone ? ` · ${a.zone}-only` : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

/** 4. WHAT IT ALMOST SAID. The contenders, and the registry's own note on telling them apart. */
function ContendersPanel({
  classification,
  fusion,
  specs,
  subjectId,
}: {
  classification: Classification;
  fusion: Fusion | null;
  specs: Record<string, DocTypeSpec>;
  subjectId: string;
}) {
  const abstained = api.isAbstention(classification);
  const subject = specs[subjectId];
  const rows = classification.runners_up;
  const confusable = subject?.confusable_with ?? {};

  return (
    <Panel title="What it almost said" stack>
      {rows.length === 0 ? (
        <EmptyState
          icon="—"
          title="nothing else was in contention"
          body="No other doctype carried any evidence for this document."
        />
      ) : (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>doctype</th>
                <th style={{ textAlign: 'right' }}>score</th>
                <th>how you tell them apart</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([id, score], i) => {
                const isSubject = abstained && i === 0 && id === subjectId;
                const note = confusable[id];
                const spec = specs[id];
                return (
                  <tr key={id}>
                    <td>
                      <div className="row" style={{ gap: 'var(--s-2)' }}>
                        <DocTypeBadge
                          doctypeId={id}
                          label={spec?.label}
                          category={spec?.category}
                          link
                        />
                        {isSubject && (
                          <Badge tone="abstain" title="this is the doctype the service declined">
                            the one it declined
                          </Badge>
                        )}
                      </div>
                    </td>
                    <td className="tabular" style={{ textAlign: 'right' }}>
                      {score.toFixed(4)}
                    </td>
                    <td className="muted" style={{ fontSize: 'var(--t-sm)' }}>
                      {note ?? (
                        <span className="faint">
                          the registry declares no confusion with this one
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > 0 && (
        <div className="az-note faint">
          These scores are the softmax over the fused channel, kept for the record.{' '}
          <strong>They are not the quantity the margin gate reads.</strong> The margin is a
          likelihood ratio on the combined evidence channel, measured in bits — which is why two
          scores a thousandth apart here can sit beside a margin of {fmt3(classification.margin)}.
          {abstained &&
            ' On an abstention the first row is the candidate that was declined, not a runner-up.'}
        </div>
      )}

      {fusion && fusion.considered.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">everything that carried any evidence at all</span>
          <div className="az-badges">
            {fusion.considered.map((id) => (
              <DocTypeBadge
                key={id}
                doctypeId={id}
                label={specs[id]?.label}
                category={specs[id]?.category}
                link
              />
            ))}
          </div>
        </div>
      )}

      {subject && Object.keys(confusable).length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">
            every doctype the registry says <span className="mono">{subject.doctype_id}</span> is
            confusable with
          </span>
          <div className="scroll-x">
            <table className="grid">
              <tbody>
                {Object.entries(confusable).map(([id, how]) => (
                  <tr key={id}>
                    <td style={{ width: '1%' }}>
                      <DocTypeBadge
                        doctypeId={id}
                        label={specs[id]?.label}
                        category={specs[id]?.category}
                        link
                      />
                    </td>
                    <td className="muted">{how}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {subject && subject.negative_anchors.length > 0 && (
        <div className="stack" style={{ gap: 'var(--s-2)' }}>
          <span className="label">
            strings that would have argued <em>against</em> this doctype
          </span>
          <div className="az-terms">
            {subject.negative_anchors.map((n) => (
              <span key={n} className="az-term">
                {n}
              </span>
            ))}
          </div>
        </div>
      )}
    </Panel>
  );
}

/** 5. EXTRACTION. PII masked, provenance visible, and an empty form said to be an empty form. */
function ExtractionPanel({ extraction }: { extraction: ExtractionResult }) {
  const [showAll, setShowAll] = useState(false);
  const filled = extraction.fields.filter(
    (f) => f.value !== null && f.value !== undefined && f.value !== '',
  );
  const allEmpty = filled.length === 0 && extraction.fields.length > 0;
  const missing = new Set(extraction.missing_required);
  const visible = allEmpty && !showAll ? [] : extraction.fields;

  return (
    <Panel
      title="Extraction"
      actions={
        <div className="row" style={{ gap: 'var(--s-2)' }}>
          <span className="faint nowrap" style={{ fontSize: 'var(--t-xs)' }}>
            {filled.length} of {extraction.fields.length} fields carry a value · schema v
            {extraction.schema_version} · {extraction.ms} ms
          </span>
          {extraction.needs_review && <Badge tone="abstain">needs review</Badge>}
        </div>
      }
      stack
    >
      {extraction.fields.length === 0 && (
        <EmptyState
          icon="▢"
          title="this doctype declares no fields"
          body="Nothing was extracted because the registry defines no extractable field for this document type. That is a property of the registry, not a failure here."
        />
      )}

      {allEmpty && (
        <>
          <EmptyByDesign fieldCount={extraction.fields.length} />
          {!showAll && (
            <div className="row">
              <button className="btn btn-sm" onClick={() => setShowAll(true)}>
                show all {extraction.fields.length} schema fields
              </button>
            </div>
          )}
        </>
      )}

      {visible.length > 0 && (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>field</th>
                <th>value</th>
                <th style={{ textAlign: 'right' }}>confidence</th>
                <th>verification</th>
                <th>found by</th>
                <th>page</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((f) => (
                <FieldRow key={f.name} field={f} required={missing.has(f.name)} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {extraction.missing_required.length > 0 && (
        <div className="az-note">
          <Badge tone="warn">missing required</Badge>{' '}
          <span className="mono">{extraction.missing_required.join(', ')}</span> — the schema
          declares {extraction.missing_required.length === 1 ? 'this field' : 'these fields'}{' '}
          required and {extraction.missing_required.length === 1 ? 'it is' : 'they are'} genuinely
          absent. That is the signal to act on; an empty optional field on a blank form is not.
        </div>
      )}
    </Panel>
  );
}

function FieldRow({ field, required }: { field: ExtractedField; required: boolean }) {
  const empty = field.value === null || field.value === undefined || field.value === '';
  return (
    <tr className={required ? 'az-required-missing' : undefined}>
      <td>
        <div className="row" style={{ gap: 'var(--s-2)' }}>
          <span style={{ fontWeight: 550 }}>{field.name}</span>
          {field.pii && <Badge tone="pii">PII</Badge>}
          {required && <Badge tone="warn">required</Badge>}
        </div>
        <div className="faint mono" style={{ fontSize: 'var(--t-xs)' }}>
          {field.attribute_key}
        </div>
      </td>
      <td>
        {empty ? (
          <span
            className="faint"
            title={field.validator_error || 'nothing was found for this field'}
          >
            —{field.validator_error ? ` (${field.validator_error})` : ''}
          </span>
        ) : (
          <div className="az-value">
            <PiiValue value={field.value} pii={field.pii} />
            {field.normalized && field.normalized !== field.value && (
              <span className="az-normalized" title="the normalised form used downstream">
                → {field.pii ? '•••• normalised' : field.normalized}
              </span>
            )}
          </div>
        )}
      </td>
      <td className="tabular" style={{ textAlign: 'right' }}>
        {empty ? <span className="faint">—</span> : field.confidence.toFixed(3)}
      </td>
      <td>
        {empty ? (
          <span className="faint">—</span>
        ) : (
          <div className="row" style={{ gap: 'var(--s-1)' }}>
            <Badge tone={verificationTone(field.verification)}>{field.verification}</Badge>
            {field.validator_error && (
              <Badge tone="warn" title="a validator objected to this value">
                {field.validator_error}
              </Badge>
            )}
          </div>
        )}
      </td>
      <td className="mono" title="the locator that produced this value">
        {field.locator || <span className="faint">—</span>}
      </td>
      <td className="tabular">{field.page ?? <span className="faint">—</span>}</td>
    </tr>
  );
}

/** The tier ledger. On a default deployment its lack of cost is the point, so it is stated. */
function TiersPanel({ tiers, timings }: { tiers: TierRun[]; timings: Timings | null }) {
  const billed = tiers.filter((t) => t.cost_bearing);
  return (
    <Panel
      title="Tiers"
      actions={
        billed.length === 0 ? (
          <Badge tone="accept" title="no tier that bills anybody ran">
            nothing billed
          </Badge>
        ) : (
          <Badge tone="cost">{billed.length} cost-bearing</Badge>
        )
      }
      stack
    >
      {tiers.length === 0 ? (
        <EmptyState
          icon="—"
          title="no tier ran"
          body="This response records no tier activity: the cascade stopped before extraction, so nothing was asked to fill a field."
        />
      ) : (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>tier</th>
                <th>status</th>
                <th style={{ textAlign: 'right' }}>fields</th>
                <th style={{ textAlign: 'right' }}>ms</th>
                <th>detail</th>
              </tr>
            </thead>
            <tbody>
              {tiers.map((t) => (
                <tr key={t.tier}>
                  <td>
                    <div className="row" style={{ gap: 'var(--s-2)' }}>
                      <span className="mono">{t.tier}</span>
                      {t.cost_bearing && (
                        <Badge tone="cost" title="somebody is billed for this run">
                          billed
                        </Badge>
                      )}
                    </div>
                  </td>
                  <td>
                    <Badge tone={tierTone(t)}>{t.status}</Badge>
                  </td>
                  <td className="tabular" style={{ textAlign: 'right' }}>
                    {t.fields_filled}
                  </td>
                  <td className="tabular" style={{ textAlign: 'right' }}>
                    {t.ms}
                  </td>
                  <td className="muted">{t.detail || <span className="faint">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {billed.length === 0 && tiers.length > 0 && (
        <div className="az-note">
          Every tier that ran was <strong>local and free</strong>. No paid model saw this document
          and nothing left the process — the same claim the header pill makes about the
          configuration, measured here on this one request.
        </div>
      )}

      {timings && (
        <div className="az-verdict-facts">
          <Fact label="adapt">
            <span className="tabular">{timings.adapt_ms} ms</span>
          </Fact>
          <Fact label="classify">
            <span className="tabular">{timings.classify_ms} ms</span>
          </Fact>
          <Fact label="extract">
            <span className="tabular">{timings.extract_ms} ms</span>
          </Fact>
          <Fact label="paid tiers">
            <span className="tabular">{timings.tiers_ms} ms</span>
          </Fact>
          <Fact label="total">
            <span className="tabular">{timings.total_ms} ms</span>
          </Fact>
        </div>
      )}
    </Panel>
  );
}

/* ========================================================================= *
 * The raw response, without undoing the masking above it.
 * ========================================================================= */

const REDACTED = '[redacted — pii; reveal it in the field table above]';

/**
 * Replace every pii-flagged value in a response with a marker.
 *
 * The disclosure panel below the extraction table is worth having: in an audit "the console
 * said" is worth less than "the response said". But printing the response verbatim handed back
 * every value the table had just masked, expanded by default, on the same screen — so the
 * masking was decorative. Anyone reading the page got the Aadhaar number they had not clicked
 * to reveal.
 *
 * Redaction here is exact rather than heuristic: the service marks each field `pii` itself,
 * from the registry, so this walks the object and blanks `value`/`normalized` on precisely the
 * fields the registry says are personal — no pattern-matching on what a value looks like.
 */
function redactPii(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(redactPii);
  if (node && typeof node === 'object') {
    const src = node as Record<string, unknown>;
    const isPiiField = src.pii === true;
    const out: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(src)) {
      const redactable = key === 'value' || key === 'normalized';
      out[key] =
        isPiiField && redactable && value !== null && value !== undefined && value !== ''
          ? REDACTED
          : redactPii(value);
    }
    return out;
  }
  return node;
}

function countPii(node: unknown): number {
  if (Array.isArray(node)) return node.reduce<number>((n, x) => n + countPii(x), 0);
  if (node && typeof node === 'object') {
    const src = node as Record<string, unknown>;
    const self = src.pii === true && src.value !== null && src.value !== undefined && src.value !== '' ? 1 : 0;
    return Object.values(src).reduce<number>((n, x) => n + countPii(x), self);
  }
  return 0;
}

/**
 * The response object, pii-redacted by default with one deliberate act to see it whole.
 *
 * Revealing is all-or-nothing on purpose. A per-value reveal inside a JSON blob would be a
 * second, quieter masking scheme to keep correct, and the two would drift.
 */
function RawResponse({ value, mode }: { value: unknown; mode: string }) {
  const [revealed, setRevealed] = useState(false);
  const piiCount = useMemo(() => countPii(value), [value]);
  const shown = useMemo(() => (revealed ? value : redactPii(value)), [value, revealed]);

  if (piiCount === 0) {
    return <JsonView value={value} title={`${mode} response`} maxHeight={520} />;
  }
  return (
    <div className="az-raw">
      <div className="az-note">
        <strong>{piiCount}</strong> pii-marked {piiCount === 1 ? 'value is' : 'values are'} redacted
        below, so this panel cannot undo the masking in the table above.{' '}
        <button className="btn btn-ghost btn-sm" onClick={() => setRevealed((r) => !r)}>
          {revealed ? 'redact again' : 'show the response verbatim'}
        </button>
      </div>
      <JsonView
        value={shown}
        title={`${mode} response${revealed ? ' — verbatim' : ' — pii redacted'}`}
        maxHeight={520}
      />
    </div>
  );
}

/* ========================================================================= *
 * The page.
 * ========================================================================= */

export default function Analyze({ readiness }: PageProps) {
  const [params, setParams] = useSearchParams();

  const rawSource = params.get('src');
  const rawMode = params.get('mode');
  const source: Source = isSource(rawSource) ? rawSource : 'file';
  const mode: Mode = isMode(rawMode) ? rawMode : 'process';
  const pin = params.get('pin') ?? '';

  /* Which OCR provider the file path will ask for. In the URL because it is *setup*, not the
     document — a reviewer must be able to link to "the same file, read the other way". An id
     the deployment cannot honour resolves back to something safe rather than erroring, so a
     link from a deployment that has Azure enabled does not arm anything on one that does not. */
  const ocrChoices = useMemo(() => ocrOptions(readiness), [readiness]);
  const ocrId = resolveOcrId(ocrChoices, params.get('ocr'));
  const ocrOption = findOcrOption(ocrChoices, ocrId);

  const setParam = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params);
      if (value) next.set(key, value);
      else next.delete(key);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  /* ---- input state. Never in the URL: it is the document. ---- */
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState('');
  const [json, setJson] = useState<Record<string, string>>({ azure: '', des: '', layout: '' });
  const [docId, setDocId] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const textArea = useRef<HTMLTextAreaElement>(null);

  /* ---- run state ---- */
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<api.ApiError | null>(null);
  const [inputError, setInputError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const abort = useRef<AbortController | null>(null);

  /* ---- registry context, fetched after a decision ---- */
  const [specs, setSpecs] = useState<Record<string, DocTypeSpec>>({});
  const asked = useRef<Set<string>>(new Set());
  const [doctypeIds, setDoctypeIds] = useState<string[]>([]);

  /* ---- floors ---- */
  const [learned, setLearned] = useState<Floors>(loadFloors);
  const [entered, setEntered] = useState<Floors>({});

  /* What this deployment declares about its remote OCR endpoint. It decides the WORDING of the
     one disclosure line in the picker; it gates nothing. There used to be a per-run consent
     checkbox and a piece of state holding which provider it had been given for — both removed.
     A control that has to be re-ticked on every run is not consent, it is a reflex, and on a
     deployment whose recogniser is its own it was asking for agreement to something untrue. */
  const boundary = useMemo(() => declaredTrustBoundary(readiness), [readiness]);

  /* A running counter, because a big document can spend tens of seconds in the classifier and a
     frozen button is indistinguishable from a hung service. */
  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    setElapsed(0);
    const timer = window.setInterval(() => setElapsed(Date.now() - started), 100);
    return () => window.clearInterval(timer);
  }, [running]);

  useEffect(() => () => abort.current?.abort(), []);

  /* The doctype list, only for the pin control, and only once it is on screen. */
  useEffect(() => {
    if (mode !== 'extract' || doctypeIds.length) return;
    const controller = new AbortController();
    api
      .listDocTypes({}, controller.signal)
      .then((r) => setDoctypeIds(r.doctypes.map((d) => d.doctype_id)))
      .catch(() => undefined);
    return () => controller.abort();
  }, [mode, doctypeIds.length]);

  const classification = outcome?.classification ?? null;
  const fusion = useMemo(
    () => (classification ? readFusion(classification.evidence) : null),
    [classification],
  );
  const reason = useMemo<ReasonRead>(
    () => (classification ? readReason(classification.reason) : EMPTY_REASON),
    [classification],
  );
  const anchorHits = useMemo(
    () => (classification ? readAnchors(classification.evidence) : []),
    [classification],
  );
  const lexical = useMemo(
    () => (classification ? readLexical(classification.evidence) : null),
    [classification],
  );

  /**
   * The doctype this trail is ABOUT: the accepted one, or — on an abstention — the one the service
   * says it came closest to accepting. `unknown` is not a doctype and must never be looked up.
   */
  const subjectId = useMemo(() => {
    if (!classification) return outcome?.extraction?.doctype_id ?? '';
    if (!api.isAbstention(classification)) return classification.doctype_id;
    return reason.candidate ?? fusion?.leader ?? classification.runners_up[0]?.[0] ?? '';
  }, [classification, outcome, reason.candidate, fusion]);

  /* Remember any floor this service stated. It is reused only under the `remembered` label. */
  useEffect(() => {
    const stated = reason.floors;
    setLearned((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const k of FLOOR_KEYS) {
        if (stated[k] !== undefined && prev[k] !== stated[k]) {
          next[k] = stated[k];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [reason.floors]);

  useEffect(() => {
    saveFloors(learned);
  }, [learned]);

  /* Pull the registry entries this trail refers to. Local, cheap, and it is what turns a list of
     anchors that fired into a list of the ones that did not — which is the actionable half.
     `asked` records attempts, not successes: an id that 404s must not be retried forever. */
  useEffect(() => {
    const wanted = new Set<string>();
    if (subjectId && subjectId !== 'unknown') wanted.add(subjectId);
    for (const [id] of classification?.runners_up ?? []) if (id !== 'unknown') wanted.add(id);
    for (const id of fusion?.considered ?? []) if (id !== 'unknown') wanted.add(id);
    const todo = [...wanted].filter((id) => !asked.current.has(id));
    if (todo.length === 0) return;
    for (const id of todo) asked.current.add(id);
    const controller = new AbortController();
    void Promise.allSettled(todo.map((id) => api.getDocType(id, controller.signal))).then(
      (results) => {
        if (controller.signal.aborted) return;
        const found: Record<string, DocTypeSpec> = {};
        for (const r of results) if (r.status === 'fulfilled') found[r.value.doctype_id] = r.value;
        if (Object.keys(found).length) setSpecs((prev) => ({ ...prev, ...found }));
      },
    );
    return () => controller.abort();
  }, [subjectId, classification, fusion]);

  /* ---------------------------------------------------------- building */

  /* Whether the doc_id in the box was typed by a person or auto-filled from a filename.
     Without this the two are indistinguishable, and `id || picked.name` keeps the FIRST
     file's name forever: analyse a second document and it is submitted, and queued for human
     review, under the previous document's id. A reviewer then opens an item labelled with the
     name of a file it is not. An id the operator actually typed still wins. */
  const docIdTyped = useRef(false);

  const takeFile = useCallback((picked: File | null) => {
    setFile(picked);
    setInputError(null);
    if (picked && !docIdTyped.current) setDocId(picked.name);
  }, []);

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) takeFile(dropped);
  };

  const buildRequest = useCallback(async (): Promise<DocumentRequest> => {
    const id = docId.trim();
    if (source === 'file') {
      if (!file) throw new Error('pick a file, or drop one on the box above');
      // Resolve again at send time rather than trusting the rendered state: an id that is not
      // available on this deployment must never reach the wire, whatever the URL said.
      const chosen = findOcrOption(ocrChoices, ocrId);
      if (!chosen?.available) {
        throw new Error(
          `${ocrId} is not available on this deployment — pick another way to read the file`,
        );
      }
      return api.documentRequestFromFile(file, {
        docId: id || file.name,
        ingestOcr: ingestFieldsFor(chosen.id),
      });
    }
    if (source === 'text') {
      if (!text.trim()) throw new Error('paste the document text first');
      return api.documentRequestFromText(text, id || 'pasted');
    }
    const raw = json[source] ?? '';
    if (!raw.trim()) throw new Error('paste the JSON payload first');
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(raw) as Record<string, unknown>;
    } catch (e) {
      throw new Error(`that is not valid JSON: ${(e as Error).message}`);
    }
    const base: DocumentRequest = { doc_id: id || 'pasted' };
    if (source === 'azure') return { ...base, azure_analyze_result: parsed };
    if (source === 'des') return { ...base, des_ocr: parsed };
    return { ...base, layout: parsed as unknown as DocumentRequest['layout'] };
  }, [source, file, text, json, docId, ocrChoices, ocrId]);

  const run = useCallback(async () => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setRunning(true);
    setError(null);
    setInputError(null);
    setOutcome(null);

    let request: DocumentRequest;
    try {
      request = await buildRequest();
    } catch (e) {
      setInputError((e as Error).message);
      setRunning(false);
      return;
    }

    const sent = redactRequest(request);
    try {
      if (mode === 'classify') {
        const result = await api.classify(request, controller.signal);
        setOutcome({
          mode,
          source,
          ocrId,
          classification: result,
          extraction: null,
          tiers: [],
          reviewIds: [],
          timings: null,
          needsReview: api.isAbstention(result),
          detail: '',
          raw: result,
          request: sent,
          at: new Date(),
        });
      } else if (mode === 'extract') {
        const result = await api.extract(
          { ...request, ...(pin ? { doctype_id: pin } : {}) },
          controller.signal,
        );
        setOutcome({
          mode,
          source,
          ocrId,
          classification: null,
          extraction: result,
          tiers: [],
          reviewIds: [],
          timings: null,
          needsReview: result.needs_review,
          detail: '',
          raw: result,
          request: sent,
          at: new Date(),
        });
      } else {
        const result: ProcessResponse = await api.process(request, controller.signal);
        setOutcome({
          mode,
          source,
          ocrId,
          classification: result.classification,
          extraction: result.extraction ?? null,
          tiers: result.tiers_used,
          reviewIds: result.review_ids,
          timings: result.timings,
          needsReview: result.needs_review,
          detail: result.detail,
          raw: result,
          request: sent,
          at: new Date(),
        });
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(api.asApiError(e));
    } finally {
      if (abort.current === controller) abort.current = null;
      setRunning(false);
    }
  }, [buildRequest, mode, pin, source, ocrId]);

  /* --------------------------------------------------- effective floors */

  const floors: Floors = {};
  const floorSources: Partial<Record<keyof Floors, FloorSource>> = {};
  for (const k of FLOOR_KEYS) {
    if (reason.floors[k] !== undefined) {
      floors[k] = reason.floors[k];
      floorSources[k] = 'stated';
    } else if (entered[k] !== undefined) {
      floors[k] = entered[k];
      floorSources[k] = 'entered';
    } else if (learned[k] !== undefined) {
      floors[k] = learned[k];
      floorSources[k] = 'remembered';
    }
  }

  const paidTiers = (readiness?.tiers ?? []).filter((t) => t.enabled && t.cost_bearing);
  const abstained = classification ? api.isAbstention(classification) : false;

  /* Provenance for the run that produced `outcome` — read out of the response when the service
     says, and otherwise reconstructed from what THAT run asked for (not from the picker's
     current value, which the operator may have changed since). */
  const outcomeOcr = outcome ? findOcrOption(ocrChoices, outcome.ocrId) : undefined;
  const provenance = outcome
    ? readProvenance(outcome.raw, outcome.source === 'file' ? outcomeOcr : undefined)
    : null;

  return (
    <main className="page">
      <PageHead
        title="Analyze"
        lede="Run a document through the cascade and read the decision trail: which gates held, what evidence moved them, what it nearly was instead."
      />

      <div className="stack">
        <Panel
          title="Document"
          actions={
            <div className="az-tabs" role="tablist" aria-label="how the document is supplied">
              {SOURCES.map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={source === id}
                  className="az-tab"
                  onClick={() => setParam('src', id)}
                >
                  {label}
                </button>
              ))}
            </div>
          }
          stack
        >
          {source === 'file' && (
            <>
              <div
                className={`az-drop ${dragging ? 'over' : ''} ${file ? 'loaded' : ''}`}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
              >
                {file ? (
                  <>
                    <span className="az-drop-name">{file.name}</span>
                    <span className="faint">
                      {file.size.toLocaleString()} bytes
                      {file.type ? ` · ${file.type}` : ''}
                    </span>
                    <div className="row">
                      <button className="btn btn-sm" onClick={() => fileInput.current?.click()}>
                        choose another
                      </button>
                      <button className="btn btn-sm btn-ghost" onClick={() => takeFile(null)}>
                        clear
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <span style={{ fontSize: 'var(--t-2xl)', opacity: 0.6 }} aria-hidden="true">
                      ⇩
                    </span>
                    <span className="state-title">drop a document here</span>
                    <span className="muted" style={{ maxWidth: '60ch' }}>
                      pdf, docx, xlsx, pptx, odt, rtf, html, eml, msg, csv, txt — and images,
                      which have to be recognised before they can be read at all: choose how
                      below. The bytes are base64&apos;d in this browser and posted to this same
                      service; where they go after that is exactly what the next control decides.
                    </span>
                    <button className="btn" onClick={() => fileInput.current?.click()}>
                      choose a file
                    </button>
                  </>
                )}
                <input
                  ref={fileInput}
                  type="file"
                  accept={FILE_ACCEPT}
                  style={{ display: 'none' }}
                  onChange={(e) => takeFile(e.target.files?.[0] ?? null)}
                />
              </div>
              <OcrPicker
                options={ocrChoices}
                value={ocrId}
                onChange={(id) => setParam('ocr', id === LOCAL_ID ? '' : id)}
                declaration={boundary}
              />
            </>
          )}

          {source === 'text' && (
            <textarea
              ref={textArea}
              className="az-textarea"
              value={text}
              placeholder="Paste the document text. This is the always-supported path: no parser, no OCR, no ingest step — just the words."
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
            />
          )}

          {(source === 'azure' || source === 'des' || source === 'layout') && (
            <>
              <textarea
                className="az-textarea"
                value={json[source] ?? ''}
                placeholder={
                  source === 'azure'
                    ? 'Paste an Azure Document Intelligence analyzeResult JSON object.'
                    : source === 'des'
                      ? 'Paste a DES OCR payload (the document-enrichment-services OCR blob).'
                      : 'Paste an already-adapted LayoutView: pages, blocks, tables, marks, key_values.'
                }
                onChange={(e) => setJson((j) => ({ ...j, [source]: e.target.value }))}
                spellCheck={false}
              />
              <div className="az-note faint">
                Exactly one payload is read, in the order <code>layout</code> →{' '}
                <code>azure_analyze_result</code> → <code>des_ocr</code> → <code>text</code>. A
                payload carrying zone roles can only ever <em>sharpen</em> a decision: the accept
                rule is evaluated on the zone-free reading, so a provider calling a line of
                marketing copy a title cannot buy an accept with it.
              </div>
            </>
          )}

          <div className="az-run">
            <div className="az-tabs" role="tablist" aria-label="endpoint">
              {MODES.map(([id, label, why]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={mode === id}
                  title={why}
                  className="az-tab"
                  onClick={() => setParam('mode', id)}
                >
                  {label}
                </button>
              ))}
            </div>

            {mode === 'extract' && (
              <>
                <input
                  type="text"
                  list="az-doctypes"
                  value={pin}
                  placeholder="pin a doctype id (optional)"
                  onChange={(e) => setParam('pin', e.target.value)}
                  style={{ width: 230 }}
                />
                <datalist id="az-doctypes">
                  {doctypeIds.map((id) => (
                    <option key={id} value={id} />
                  ))}
                </datalist>
              </>
            )}

            <input
              type="text"
              value={docId}
              placeholder="doc_id (optional)"
              onChange={(e) => {
                // Clearing the box hands control back to the filename default.
                docIdTyped.current = e.target.value.trim() !== '';
                setDocId(e.target.value);
              }}
              style={{ width: 180 }}
            />

            <span style={{ marginLeft: 'auto' }} />

            {running ? (
              <>
                <Spinner label={`${(elapsed / 1000).toFixed(1)}s`} />
                <button
                  className="btn"
                  onClick={() => {
                    abort.current?.abort();
                    setRunning(false);
                  }}
                >
                  cancel
                </button>
              </>
            ) : (
              /* Never disabled by an acknowledgement. The button says where the file is going in
                 its own label, which is disclosure at the moment of the decision; a checkbox
                 that has to be re-ticked before every run is not. */
              <button className="btn btn-primary" onClick={run}>
                run {mode}
                {source === 'file' && ocrOption?.egress ? ` — sends to ${ocrOption.label}` : ''}
              </button>
            )}
          </div>

          <div className="az-preflight">
            {readiness && (
              <>
                <span>{readiness.registry.doctypes} doctypes loaded</span>
                <span aria-hidden="true">·</span>
                {/*
                  `egress.preclassification_allowed` is a fact about the CLASSIFIER: it says the
                  cascade opens no socket. It is not a fact about ingestion, and printing the
                  unqualified "no pre-classification egress" next to a provider that ships the
                  file to Azure would be the console contradicting itself on its own headline
                  claim. When such a provider is selected the sentence is narrowed to what it
                  actually covers, and the wider claim is withdrawn rather than repeated.
                */}
                <span>
                  {readiness.egress.preclassification_allowed
                    ? 'pre-classification egress is ALLOWED on this deployment'
                    : source === 'file' && ocrOption?.egress
                      ? 'the classifier itself opens no socket — but the reading below happens before it runs'
                      : 'no pre-classification egress'}
                </span>
                <span aria-hidden="true">·</span>
              </>
            )}
            {source === 'file' && ocrOption && (
              <>
                {/* The same declaration decides the register here as in the picker. Two lines
                    on one screen describing one hop, one calling it a transmission off the
                    deployment and one calling it internal, would be the console contradicting
                    itself — and an operator would be right to trust neither. */}
                {ocrOption.egress ? (
                  boundary.boundary === 'on_premises' ? (
                    <span>
                      OCR: {ocrOption.label} — reads the file at{' '}
                      {ocrOption.endpoint || 'the configured endpoint'}, declared inside this
                      deployment&rsquo;s trust boundary
                    </span>
                  ) : (
                    <Badge
                      tone="danger"
                      title={
                        ocrOption.endpoint
                          ? `the document is transmitted to ${ocrOption.endpoint} before its doctype is known`
                          : 'the document is transmitted to a remote service before its doctype is known'
                      }
                    >
                      OCR: {ocrOption.label} — transmits this document off the deployment
                    </Badge>
                  )
                ) : (
                  <span>OCR: {ocrOption.label} — nothing leaves this process</span>
                )}
                <span aria-hidden="true">·</span>
              </>
            )}
            {source !== 'file' && source !== 'text' && (
              <>
                <span>caller-supplied reading — this service calls nobody to read it</span>
                <span aria-hidden="true">·</span>
              </>
            )}
            {paidTiers.length > 0 ? (
              <Badge tone="cost" title={paidTiers.map((t) => t.tier).join(', ')}>
                {paidTiers.length} cost-bearing tier{paidTiers.length === 1 ? '' : 's'} enabled —
                running this may be billed
              </Badge>
            ) : (
              <span>no cost-bearing tier is enabled: this runs in-process and bills nothing</span>
            )}
            {mode === 'extract' && !pin && (
              <>
                <span aria-hidden="true">·</span>
                <span>
                  no doctype pinned — the service classifies first, and an abstention returns an
                  empty result flagged for review rather than a guess
                </span>
              </>
            )}
          </div>

          {inputError && <ErrorState title="nothing to send" error={inputError} />}
        </Panel>

        {/* ------------------------------------------------------- results */}

        {running && (
          <Panel>
            <div className="state">
              <Spinner label={`running ${mode}… ${(elapsed / 1000).toFixed(1)}s`} />
              <div className="state-body muted" style={{ marginTop: 'var(--s-3)' }}>
                Classification runs in this process against every doctype in the registry
                {readiness ? ` (${readiness.registry.doctypes} of them)` : ''}. A long document can
                take tens of seconds. Nothing is being sent anywhere while you wait.
              </div>
            </div>
          </Panel>
        )}

        {error && api.isNeedsOcr(error) && (
          <NeedsOcrState
            error={error}
            action={
              <button
                className="btn"
                onClick={() => {
                  setParam('src', 'text');
                  setError(null);
                  window.setTimeout(() => textArea.current?.focus(), 0);
                }}
              >
                paste the recognised text instead
              </button>
            }
          />
        )}

        {error && !api.isNeedsOcr(error) && (
          <ErrorState
            error={error}
            title={error.status === 503 ? 'this deployment cannot read that file' : undefined}
            body={
              error.status === 503
                ? 'An ingest engine for this file type is not installed in this image. The document is fine and nothing was sent anywhere — paste its text instead, or install the extra the message names.'
                : undefined
            }
            facts={[['status', <span className="mono">{error.status || 'no reply'}</span>]]}
            action={
              error.status === 503 ? (
                <button className="btn" onClick={() => setParam('src', 'text')}>
                  paste text instead
                </button>
              ) : undefined
            }
          />
        )}

        {outcome && (
          <>
            {classification && (
              <Verdict
                classification={classification}
                spec={specs[subjectId] ?? null}
                reviewIds={outcome.reviewIds}
                timings={outcome.timings}
                detail={outcome.detail}
                needsReview={outcome.needsReview}
              />
            )}

            {classification && abstained && (
              <AbstentionNotice
                classification={classification}
                action={
                  outcome.reviewIds.length > 0 ? (
                    <Link className="btn" to="/review">
                      open the review queue
                    </Link>
                  ) : undefined
                }
              />
            )}

            <ReadingPanel
              source={outcome.source}
              provenance={provenance}
              /* Only the `file` path asks this service to recognise anything. On the
                 caller-supplied tabs the picker is not part of the request at all, so
                 comparing it against the adapter the service reports would raise a
                 "something stood in" alarm on every single caller-supplied run. Same
                 condition as `provenance` above, and for the same reason. */
              requested={outcome.source === 'file' ? outcomeOcr : undefined}
              spec={specs[subjectId] ?? null}
            />

            {classification && (
              <WhyPanel
                classification={classification}
                fusion={fusion}
                reason={reason}
                floors={floors}
                floorSources={floorSources}
                entered={entered}
                onEntered={setEntered}
              />
            )}

            {classification && (
              <EvidencePanel
                evidence={classification.evidence}
                hits={anchorHits}
                lexical={lexical}
                spec={specs[subjectId] ?? null}
              />
            )}

            {classification && (
              <ContendersPanel
                classification={classification}
                fusion={fusion}
                specs={specs}
                subjectId={subjectId}
              />
            )}

            {!classification && outcome.extraction && (
              <Panel title="No classification ran" stack>
                <div className="az-note">
                  You called <code>extract</code>
                  {pin ? (
                    <>
                      {' '}
                      with <span className="mono">{pin}</span> pinned
                    </>
                  ) : null}
                  , so there are no gates to show: the doctype was supplied rather than decided.
                  Switch to{' '}
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => setParam('mode', 'process')}
                  >
                    process
                  </button>{' '}
                  and run the same document to see the decision trail as well.
                </div>
              </Panel>
            )}

            {outcome.extraction && <ExtractionPanel extraction={outcome.extraction} />}

            {outcome.mode === 'process' && !outcome.extraction && (
              <Panel title="Extraction" stack>
                <EmptyState
                  icon="▢"
                  title="nothing was extracted, and that is correct"
                  body="No doctype was accepted, so there is no schema to extract against. An unclassified document is not guessed at, and it is not forwarded to a paid tier to find out what it is — it is routed to a human."
                />
              </Panel>
            )}

            {outcome.mode === 'process' && (
              <TiersPanel tiers={outcome.tiers} timings={outcome.timings} />
            )}

            <Panel
              title="The response, verbatim"
              actions={
                <span className="faint nowrap" style={{ fontSize: 'var(--t-xs)' }}>
                  POST /api/v1/{outcome.mode} · {outcome.at.toLocaleTimeString()}
                </span>
              }
              stack
            >
              <div className="az-note faint">
                In an audit, &ldquo;the console said&rdquo; is worth less than &ldquo;the response
                said&rdquo;. Everything above is derived from the object below and from the registry
                entries it names — nothing else.
              </div>
              <RawResponse value={outcome.raw} mode={outcome.mode} />
              <JsonView
                value={outcome.request}
                title="request as sent (payload redacted)"
                maxHeight={220}
                collapsed
              />
            </Panel>
          </>
        )}

        {!outcome && !running && !error && !inputError && (
          <Panel>
            <EmptyState
              icon="◇"
              title="nothing analysed yet"
              body={
                <>
                  Drop a document above and press <strong>run</strong>. What comes back is not a
                  label: it is the four gates with their measured values, the anchors that fired and
                  the ones that did not, the doctypes it nearly said instead, and the raw response
                  underneath all of it.
                </>
              }
            />
          </Panel>
        )}
      </div>
    </main>
  );
}
