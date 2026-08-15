/**
 * /posture — what this deployment is allowed to do.
 *
 * ## What this page is for
 * Every claim the other three pages make rests on facts this page shows. "The document did not
 * leave" is only true if `egress.preclassification_allowed` is false **and** no remote OCR
 * provider is configured — reading happens upstream of classification, so a deployment that
 * transmits a document in order to recognise it has disclosed it one step before the invariant
 * above ever applies. "This cost nothing" is only true if no cost-bearing tier is enabled. This
 * page is where an auditor is pointed, and where an operator finds out that somebody changed
 * something.
 *
 * It is a *statement of posture*, not a dashboard. There are no sparklines here.
 *
 * ## How it is built
 *
 * The page renders `props.readiness` — the shell's copy, refreshed every 30s. It does not poll.
 * Two deliberate additions to that:
 *
 *  1. **A one-off `api.readiness()` while the shell's copy is `null`.** `null` means either "not
 *     yet" or "the service never answered", and this is the one page where the difference is the
 *     story: an unreachable service must render as an error with the actual reason, not as a
 *     spinner forever. The local probe exists only to get that error object; the moment the shell
 *     has an answer, the shell's copy wins.
 *  2. **`/metrics`, read once on mount and on refresh.** `/readyz` reports what this deployment
 *     is *configured* to do; `dce_preclassification_egress_blocked_total` reports what the
 *     running process has actually *refused*. That counter is the only runtime corroboration of
 *     the invariant anywhere on the wire, and it is qualified honestly on the page: it counts
 *     refusals, so zero means nothing tried, and it resets when the process restarts.
 *
 * Nothing here invents a number. Every value on the page is a field of `/readyz`, a sample from
 * `/metrics`, or a count of those. Where a fact is *not* on the wire — the classifier's
 * thresholds, a fingerprint of the ruleset — the page says so rather than filling the gap.
 *
 * ## States
 *  - `readiness === null` → loading, then `<ErrorState/>` once the local probe has failed.
 *  - `ready === false` with a full body → NOT an error. Render it and let `degraded[]` explain.
 *    The readiness badge goes warn, never red: a service that says why it is not ready is
 *    /readyz working.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';

import {
  asApiError,
  health as fetchHealth,
  metrics as fetchMetrics,
  readiness as fetchReadiness,
} from '../api';
import {
  Badge,
  CountryTag,
  ErrorState,
  Fact,
  JsonView,
  Loading,
  PageHead,
  Panel,
} from '../components';
import {
  declaredTrustBoundary,
  providerEgress,
  readOcrPosture,
  readsRemotely,
  type OcrPosture,
} from '../ocr';
import type {
  BertStatus,
  ComponentState,
  HealthResponse,
  ReadinessResponse,
  TierStatus,
} from '../types';
import type { PosturePageProps } from './contract';
import './Posture.css';

/* ------------------------------------------------------------------ prom */

/**
 * A minimal Prometheus text parser.
 *
 * Local to this page on purpose: it is the only place in the console that reads `/metrics`, and
 * the shared layer has no parser (nor should it grow one for a single caller). It handles the
 * subset this service emits — `name{label="v",...} value` — and nothing else. A malformed line
 * is skipped rather than guessed at.
 */
interface PromSample {
  labels: Record<string, string>;
  value: number;
}

const LABEL_RE = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"/g;

function promSamples(text: string, metric: string): PromSample[] {
  const out: PromSample[] = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.startsWith(metric)) continue;
    // `dce_foo_total` must not match a line for `dce_foo_total_sum`.
    const rest = line.slice(metric.length);
    if (rest[0] !== '{' && rest[0] !== ' ') continue;

    const labels: Record<string, string> = {};
    let tail = rest;
    if (rest[0] === '{') {
      const close = rest.indexOf('}');
      if (close === -1) continue;
      for (const m of rest.slice(1, close).matchAll(LABEL_RE)) labels[m[1]] = m[2];
      tail = rest.slice(close + 1);
    }
    const value = Number.parseFloat(tail.trim().split(/\s+/)[0] ?? '');
    if (Number.isFinite(value)) out.push({ labels, value });
  }
  return out;
}

const sum = (samples: PromSample[]): number => samples.reduce((n, s) => n + s.value, 0);

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const d = Math.floor(seconds / 86_400);
  const h = Math.floor((seconds % 86_400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${Math.floor(seconds)}s`;
}

const clock = (d: Date): string =>
  d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/* ---------------------------------------------------------------- pieces */

/** One-line verdict above the evidence for it. */
function Finding({
  tone = 'neutral',
  glyph,
  children,
}: {
  tone?: 'neutral' | 'ok' | 'warn' | 'cost' | 'danger' | 'abstain';
  glyph?: string;
  children: ReactNode;
}) {
  return (
    <div className={`pos-finding ${tone === 'neutral' ? '' : tone}`}>
      {glyph && (
        <span className="glyph" aria-hidden="true">
          {glyph}
        </span>
      )}
      <div className="text">{children}</div>
    </div>
  );
}

/** yes/no where `yes` is the healthy answer. Anything where "no" is also fine uses a plain Badge. */
const YesNo = ({ value }: { value: boolean }) => (
  <Badge tone={value ? 'accept' : 'warn'}>{value ? 'yes' : 'no'}</Badge>
);

/** Render a `ComponentState.extra` bag without pretending to know its shape. */
function ExtraBag({ extra }: { extra: Record<string, unknown> }) {
  const entries = Object.entries(extra ?? {});
  if (!entries.length) return <span className="faint">—</span>;
  return (
    <span>
      {entries.map(([k, v]) => (
        <span className="pos-kv" key={k}>
          <span>{k}</span>
          <span className="tabular">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
        </span>
      ))}
    </span>
  );
}

/* --------------------------------------------------------- the invariant */

function Invariant({
  posture,
  metricsText,
}: {
  posture: ReadinessResponse;
  metricsText: string | null;
}) {
  const allowed = posture.egress.preclassification_allowed;
  const enforced = posture.egress.enforced;

  /* The guard covers the CLASSIFIER. Reading happens before it, so a remote OCR provider can
     disclose a document while `preclassification_allowed` is still false — both statements
     true, and the headline below false. Rather than print a green assertion an auditor could
     quote and be wrong, the assertion is narrowed here and the panel points at the OCR panel. */
  const readRemotely = readsRemotely(posture);
  // `components` is an open bag on the wire: a deployment that stops reporting a component must
  // read as "not reported", not as `false`.
  const egressComponent: ComponentState | undefined = posture.components.egress;
  const componentOk = egressComponent?.ok;

  const blocked = metricsText ? promSamples(metricsText, 'dce_preclassification_egress_blocked_total') : null;
  const startedAt = metricsText ? promSamples(metricsText, 'process_start_time_seconds')[0]?.value : undefined;
  const uptime = startedAt === undefined ? null : Date.now() / 1000 - startedAt;

  return (
    <Panel title="The invariant" stack>
      <div className={`pos-invariant ${allowed || readRemotely ? 'alarm' : 'ok'}`}>
        <div className="assert">
          {allowed
            ? 'Pre-classification egress is ALLOWED on this deployment.'
            : readRemotely
              ? 'The classifier calls nobody — but documents are sent away to be READ before it runs.'
              : 'No document leaves this process before its doctype is known.'}
        </div>
        {!allowed && readRemotely && (
          <div className="body">
            <strong>Read the two facts together, not separately.</strong> The guard below is a
            fact about the cascade: no vendor SDK, no HTTP call and no embedding API between
            receiving a document and deciding what it is. It says nothing about how the document
            was turned into text in the first place, and this deployment has a remote OCR provider
            available — so an unclassified document can leave this process one step
            <em> before</em> the guard applies. <b>Do not quote the counter below as evidence that
            nothing left.</b> See <b>How documents are read</b>, immediately after this panel —
            it names the host and reports what this deployment declares about it.
          </div>
        )}
        <div className="body">
          {allowed ? (
            <>
              Unclassified document content may be sent to a remote service — an OCR, a vendor
              SDK, an embedding API — <em>before</em> anything has established what the document
              is. That is the one disclosure this service exists to prevent, and it is not a
              default: somebody set it deliberately. <code>/readyz</code> answers <b>503</b> for
              as long as it is on, so a load balancer will not send traffic here.
            </>
          ) : (
            <>
              Classification runs entirely inside this process. The paid tiers cannot be reached
              until a doctype has been accepted, so a document that the service declines to
              classify is never transmitted{' '}
              {readRemotely ? (
                <>
                  <em>to an extraction tier</em> — though it may already have been transmitted to
                  the OCR provider that read it.
                </>
              ) : (
                <>
                  anywhere at all — an abstention is the safe outcome, not a half-completed one.
                </>
              )}
            </>
          )}
        </div>
        <div className="quote">{posture.egress.note}</div>
        {allowed && (
          <div className="body">
            <b>What to do:</b> unset <code>ALLOW_PRECLASSIFICATION_EGRESS</code> on this
            deployment and restart it, then confirm this panel reads green again. Anything
            processed while it was on should be treated as having been disclosed.
          </div>
        )}
      </div>

      <div className="pos-facts">
        <Fact label="preclassification_allowed">
          <Badge tone={allowed ? 'danger' : 'accept'}>{String(allowed)}</Badge>
        </Fact>
        <Fact label="enforced">
          <Badge tone={enforced ? 'accept' : 'danger'}>{String(enforced)}</Badge>
        </Fact>
        <Fact label="components.egress">
          {componentOk === undefined ? (
            <span className="faint">not reported</span>
          ) : (
            <Badge tone={componentOk ? 'accept' : 'danger'}>{componentOk ? 'ok' : 'not ok'}</Badge>
          )}
        </Fact>
        <Fact label="refusals recorded">
          {blocked === null ? (
            <span className="faint">/metrics not read</span>
          ) : blocked.length === 0 ? (
            <span className="muted tabular">no sample</span>
          ) : (
            <span className="tabular">{sum(blocked).toLocaleString()}</span>
          )}
        </Fact>
        <Fact label="process uptime">
          {uptime === null ? <span className="faint">—</span> : <span className="tabular">{formatDuration(uptime)}</span>}
        </Fact>
      </div>

      {allowed !== !enforced && (
        <Finding tone="warn" glyph="!">
          <strong>These two fields disagree.</strong> <code>preclassification_allowed</code> is{' '}
          <code>{String(allowed)}</code> but <code>enforced</code> is <code>{String(enforced)}</code>
          . They are derived from the same setting, so this should not be possible — treat the
          posture as unknown and read the raw <code>/readyz</code> below before relying on it.
        </Finding>
      )}

      <Finding glyph="i">
        <strong>What the refusal counter is and is not.</strong>{' '}
        <code className="mono">dce_preclassification_egress_blocked_total</code> counts network
        calls the guard <em>refused</em> because the document was not classified yet
        {blocked !== null && blocked.length === 0 ? (
          <>
            {' '}
            — it currently has no sample at all, which means nothing in this process has attempted
            one
          </>
        ) : null}
        . A zero is therefore not evidence of compliance; it is the absence of an attempt. The
        counter also resets when the process restarts
        {uptime !== null ? <> — this one has been up {formatDuration(uptime)}</> : null}, so it
        says nothing about any earlier process. The configuration above, not this number, is the
        claim.
      </Finding>
    </Panel>
  );
}

/* ------------------------------------------------- the known limitation */

/**
 * The gap, named.
 *
 * Verified against this repo rather than assumed: `/readyz` carries `version` (the package
 * version) and nothing content-addressed; `Classification` has no ruleset field; the only
 * `_fingerprint` in the codebase is a private in-memory cache key in `dce/classify/profiles.py`
 * and never reaches the wire. A compliance console that implies traceability it does not have is
 * worse than one that states the gap, so this panel is not collapsible and not a footnote.
 */
function Limitation({ posture }: { posture: ReadinessResponse }) {
  return (
    <Panel title="What this page cannot prove" className="pos-caveat" stack>
      <Finding tone="warn" glyph="⚠">
        <strong>There is no ruleset fingerprint.</strong> Nothing on this page is
        content-addressed, so a decision stored yesterday cannot be tied to the rules that
        produced it.
      </Finding>
      <ul>
        <li>
          <code>/readyz</code> reports <b>posture</b>, not <b>content</b>. It carries a doctype{' '}
          <em>count</em> ({posture.registry.doctypes}) but no hash of those doctypes, and it does
          not carry the classifier's gates (<code>CLASSIFY_MIN_MARGIN</code>,{' '}
          <code>CLASSIFY_MIN_COVERAGE</code>, <code>CLASSIFY_MIN_SUPPORT</code>) at all. Two
          deployments from the same image running different thresholds return byte-identical{' '}
          <code>/readyz</code>.
        </li>
        <li>
          <code>version</code> is <code className="mono">{posture.version}</code> — the Python
          package version. It does not change when an anchor is edited, a doctype is added or a
          threshold is moved, so it is not a build identifier and must not be quoted as one.
        </li>
        <li>
          A <code>Classification</code> has no rules-version field. The response carries the
          decision, the evidence and the runners-up, but nothing that identifies the ruleset that
          produced them — so a stored decision cannot be replayed against, or attributed to, a
          known configuration.
        </li>
        <li>
          <b>What this means for an audit.</b> You can demonstrate what this deployment is doing{' '}
          <em>right now</em>, from this page and the raw response below. You cannot demonstrate,
          from the service's own output, that it was doing the same thing when an older decision
          was made. Today the only link is out-of-band — the container image digest, the
          deployment record, and whatever your configuration management retains.
        </li>
        <li>
          <b>What would close it.</b> A digest over the loaded registry plus the effective
          thresholds, reported on <code>/readyz</code> and echoed on every{' '}
          <code>Classification</code>. Until that exists, this console does not claim it.
        </li>
      </ul>
    </Panel>
  );
}

/* --------------------------------------------------------- tier ledger */

function TierLedger({ tiers, components }: { tiers: TierStatus[]; components: Record<string, ComponentState> }) {
  const live = tiers.filter((t) => t.enabled);
  const billing = tiers.filter((t) => t.enabled && t.cost_bearing);
  const broken = tiers.filter((t) => t.problem);

  const tierComponent: ComponentState | undefined = components.tiers;
  const tierExtra: Record<string, unknown> = tierComponent?.extra ?? {};
  const egressEnabled = Array.isArray(tierExtra.enabled)
    ? (tierExtra.enabled as unknown[]).map(String)
    : null;
  const queueBackend = typeof tierExtra.review_queue_backend === 'string' ? tierExtra.review_queue_backend : null;

  return (
    <Panel
      title="The tier ledger"
      actions={
        <span className="muted" style={{ fontSize: 'var(--t-xs)' }}>
          {live.length} of {tiers.length} enabled · {billing.length} that can bill
        </span>
      }
      stack
    >
      {billing.length === 0 ? (
        <Finding tone="ok" glyph="✓">
          <strong>No cost-bearing tier is enabled.</strong> Extraction on this deployment is local
          only: {live.map((t) => t.tier).join(' and ') || 'nothing'}. Nothing here can bill anyone,
          and nothing here sends a document to a third party. This is the default posture — the
          absence is the finding, not an empty table.
        </Finding>
      ) : (
        <Finding tone="cost" glyph="$">
          <strong>
            {billing.length} cost-bearing {billing.length === 1 ? 'tier is' : 'tiers are'} enabled:{' '}
            {billing.map((t) => t.tier).join(', ')}.
          </strong>{' '}
          These run only after a doctype has been accepted, but when they run they transmit
          document content to a remote service and they bill per page, per field or per token.
        </Finding>
      )}

      {broken.length > 0 && (
        <Finding tone="warn" glyph="⚠">
          <strong>
            {broken.length} {broken.length === 1 ? 'tier is' : 'tiers are'} half-configured.
          </strong>{' '}
          A tier that is switched on but cannot run is the dangerous state: the operator believes
          they have a capability and the fields it was meant to fill come back empty. This does
          not make the service unready — classification is unaffected — so nothing else will
          complain about it.
        </Finding>
      )}

      <div className="scroll-x">
        <table className="grid">
          <thead>
            <tr>
              <th>tier</th>
              <th>status</th>
              <th>billing</th>
              <th>what it does</th>
            </tr>
          </thead>
          <tbody>
            {tiers.map((t) => (
              <tr key={t.tier} className={t.enabled ? 'pos-tier-live' : 'pos-tier-off'}>
                <td className="mono nowrap">{t.tier}</td>
                <td>
                  {t.enabled ? <Badge tone="accept">enabled</Badge> : <Badge tone="neutral">off</Badge>}
                </td>
                <td>
                  {t.cost_bearing ? (
                    <Badge tone="cost" title="this tier bills somebody when it runs">
                      billed
                    </Badge>
                  ) : (
                    <Badge tone="neutral">free</Badge>
                  )}
                </td>
                <td>
                  <span className="pos-summary">{t.summary}</span>
                  {t.problem && (
                    <div style={{ marginTop: 'var(--s-1)' }}>
                      <Badge tone="warn">problem</Badge>{' '}
                      <span className="mono" style={{ fontSize: 'var(--t-xs)' }}>
                        {t.problem}
                      </span>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="pos-facts">
        <Fact label="egress tiers enabled">
          {egressEnabled === null ? (
            <span className="faint">not reported</span>
          ) : egressEnabled.length === 0 ? (
            <Badge tone="accept">none</Badge>
          ) : (
            <span className="mono">{egressEnabled.join(', ')}</span>
          )}
        </Fact>
        <Fact label="review queue backend">
          {queueBackend ? <span className="mono">{queueBackend}</span> : <span className="faint">not reported</span>}
        </Fact>
      </div>

      {queueBackend === 'memory' && (
        <Finding tone="warn" glyph="⚠">
          <strong>The review queue is in memory.</strong> Every human decision — who approved
          what, who corrected what, and both halves of a double-entry keying — lives in this
          process and is gone when it restarts. That is fine for a demonstration and is not fine
          for an audit trail: on this backend the queue cannot evidence a decision that was made
          before the last restart.
        </Finding>
      )}
    </Panel>
  );
}

/* ------------------------------------------------------ how documents are read */

/**
 * The OCR posture — the second question an auditor asks, right after the invariant.
 *
 * "No document leaves this process before its doctype is known" is a claim about
 * classification. Reading is *upstream* of classification, and a deployment that has been given
 * a remote OCR provider transmits the document to a third party in order to find out what it is
 * — which is the same disclosure, arriving one step earlier. It cannot be inferred from the
 * `egress` block or from the tier ledger, so it gets its own panel.
 *
 * Three postures, three different findings, and the third is the one that matters:
 *
 *   nothing reported   `/readyz` says nothing about OCR. The page says so rather than reading
 *                      silence as "no remote provider", which would be exactly the wrong way to
 *                      resolve an unknown on a compliance page.
 *   local only         recognition happens in-process, or not at all.
 *   remote configured  named, with its endpoint, in the alarm register — because the operator
 *                      has enabled the transmission of unclassified documents to a third party
 *                      and this page is where somebody finds that out.
 */
function OcrReading({ posture }: { posture: ReadinessResponse }) {
  const ocr: OcrPosture = readOcrPosture(posture);
  const remote = ocr.providers.filter(providerEgress);
  const liveRemote = remote.filter((p) => p.available);
  const declaration = declaredTrustBoundary(posture);

  return (
    <Panel
      title="How documents are read"
      actions={
        <span className="muted" style={{ fontSize: 'var(--t-xs)' }}>
          upstream of classification
        </span>
      }
      stack
    >
      {ocr.reported && ocr.textLayerPolicy ? (
        <Finding tone={ocr.textLayerPolicy === 'trust' ? 'warn' : 'neutral'} glyph="i">
          <strong>
            A PDF&rsquo;s own text layer is{' '}
            {ocr.textLayerPolicy === 'always_ocr'
              ? 'never read here'
              : ocr.textLayerPolicy === 'trust'
                ? 'taken at face value here'
                : 'checked page by page here'}{' '}
            (<code className="mono">{ocr.textLayerPolicy}</code>).
          </strong>{' '}
          {ocr.textLayerAttribution}{' '}
          <em>
            This decides whether a document is recognised at all, before any provider below is
            chosen: a page judged adequate never reaches them.
          </em>
        </Finding>
      ) : null}
      {!ocr.reported ? (
        <Finding tone="warn" glyph="?">
          <strong>This deployment does not report its OCR posture.</strong> <code>/readyz</code>{' '}
          carries no OCR block, so this page cannot tell you whether a recognition provider is
          configured here or which one. Read that as <em>unknown</em>, not as <em>none</em>: a
          console that resolved silence into a clean bill of health would be the least trustworthy
          thing on this page. The <code>/analyze</code> picker takes the same position — it will
          not offer a remote provider this service has not named.
        </Finding>
      ) : liveRemote.length > 0 ? (
        <Finding tone={declaration.boundary === 'on_premises' ? 'warn' : 'danger'} glyph="!">
          <strong>
            This deployment sends unclassified documents to{' '}
            {liveRemote.map((p) => p.endpoint || p.name).join(', ')} to be read.
          </strong>{' '}
          A document sent to a remote service to be <em>read</em> has left this process before
          anything established what it is or whose it is — the same disclosure the invariant above
          prevents, one step earlier in the pipeline. This is not a tuning knob and it is not a
          default: somebody enabled it deliberately.{' '}
          {declaration.attribution ? (
            <>
              {declaration.attribution.charAt(0).toUpperCase() + declaration.attribution.slice(1)}
            </>
          ) : declaration.boundary === 'on_premises' ? (
            <>
              This deployment declares that host is inside its own trust boundary — the operator’s
              declaration, not a fact this service verified.
            </>
          ) : (
            <>
              No trust boundary has been declared for that host, so every document read this way
              should be treated as having been seen by a third party.
            </>
          )}
        </Finding>
      ) : ocr.localEnabled === true ? (
        <Finding tone="ok" glyph="✓">
          <strong>Recognition is local.</strong> Images and scanned PDFs are read by{' '}
          <code>{ocr.localEngine || 'the configured local engine'}</code>, on this host. No remote
          provider is available here, so no document is transmitted anywhere to be read.
        </Finding>
      ) : (
        <Finding tone="ok" glyph="✓">
          <strong>Nothing recognises anything here.</strong> No local engine is enabled and no
          remote provider is available, so a document with no text layer comes back{' '}
          <code>needs_ocr</code> — the honest answer. An image is never guessed at and never sent
          away to be identified.
        </Finding>
      )}

      <div className="pos-facts">
        <Fact label="local OCR enabled">
          {ocr.localEnabled === null ? (
            <span className="faint">not reported</span>
          ) : (
            <Badge tone={ocr.localEnabled ? 'accent' : 'neutral'}>{String(ocr.localEnabled)}</Badge>
          )}
        </Fact>
        <Fact label="local engine">
          {ocr.localEngine ? (
            <span className="mono">{ocr.localEngine}</span>
          ) : (
            <span className="faint">not reported</span>
          )}
        </Fact>
        <Fact label="remote providers available">
          {!ocr.reported ? (
            <span className="faint">not reported</span>
          ) : liveRemote.length === 0 ? (
            <Badge tone="accept">none</Badge>
          ) : (
            <Badge tone={declaration.boundary === 'on_premises' ? 'warn' : 'danger'}>
              {liveRemote.length}
            </Badge>
          )}
        </Fact>
        {/*
          A first-class row rather than a line an auditor has to find in the raw JSON below.
          It is rendered only when a remote provider is actually live: on a deployment that
          reads nothing remotely the question does not arise, and printing `on_premises` there
          would be answering a question nobody asked with a reassuring word.
        */}
        {liveRemote.length > 0 && (
          <Fact label="declared trust boundary">
            <Badge tone={declaration.boundary === 'on_premises' ? 'warn' : 'danger'}>
              {declaration.boundary}
            </Badge>{' '}
            <span className="faint">operator declaration — not verified here</span>
          </Fact>
        )}
      </div>

      {ocr.providers.length > 0 && (
        <div className="scroll-x">
          <table className="grid">
            <thead>
              <tr>
                <th>provider</th>
                <th>state</th>
                <th>egress</th>
                <th>endpoint</th>
                <th>note</th>
              </tr>
            </thead>
            <tbody>
              {ocr.providers.map((p) => {
                const egress = providerEgress(p);
                return (
                  <tr key={p.name} className={p.available ? 'pos-tier-live' : 'pos-tier-off'}>
                    <td className="mono nowrap">{p.name}</td>
                    <td>
                      {p.available ? (
                        <Badge tone="accept">available</Badge>
                      ) : (
                        <Badge tone="neutral">off</Badge>
                      )}
                    </td>
                    <td>
                      {egress ? (
                        <Badge tone="danger" title="the document is transmitted before it is classified">
                          transmits
                        </Badge>
                      ) : (
                        <Badge tone="accept">local</Badge>
                      )}
                    </td>
                    <td className="mono" style={{ fontSize: 'var(--t-xs)', wordBreak: 'break-all' }}>
                      {p.endpoint || <span className="faint">—</span>}
                    </td>
                    <td>{p.reason ? p.reason : <span className="faint">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <Finding glyph="i">
        <strong>There are two ways to get an image classified, and only one of them is this.</strong>{' '}
        On the <em>caller-supplied</em> path an upstream service does the recognition and posts the
        result to <code>/classify</code> as <code>azure_analyze_result</code> or{' '}
        <code>des_ocr</code>; this service opens no socket and nothing on this panel applies. The
        providers above are the other path — this service doing the reading itself, and therefore
        this service doing the transmitting. Whatever a vendor's name appears in a request payload,
        it is this table that says whether <em>this</em> deployment called anyone.
      </Finding>
    </Panel>
  );
}

/* -------------------------------------------------- registry + components */

function RegistryAndEngines({ posture }: { posture: ReadinessResponse }) {
  const components = Object.entries(posture.components);
  const degraded = posture.degraded ?? [];

  return (
    <Panel title="Registry and engines" stack>
      {degraded.length === 0 ? (
        <Finding tone="ok" glyph="✓">
          Nothing is degraded. Every component below reported <code>ok</code>.
        </Finding>
      ) : (
        <Finding tone="warn" glyph="⚠">
          <strong>Degraded: {degraded.join(', ')}.</strong> The service still answers; these
          components are named here precisely so nobody has to infer what stopped working from a
          drop in a graph.
        </Finding>
      )}

      <div className="pos-facts">
        <Fact label="registry loaded">
          <YesNo value={posture.registry.loaded} />
        </Fact>
        <Fact label="doctypes">
          <span className="tabular" style={{ fontSize: 'var(--t-md)', fontWeight: 650 }}>
            {posture.registry.doctypes.toLocaleString()}
          </span>
        </Fact>
        <Fact label={`countries (${posture.registry.countries.length})`}>
          <span className="row" style={{ gap: 'var(--s-1)' }}>
            {posture.registry.countries.length === 0 ? (
              <span className="faint">none</span>
            ) : (
              posture.registry.countries.map((c) => <CountryTag key={c} country={c} />)
            )}
          </span>
        </Fact>
      </div>

      <div className="scroll-x">
        <table className="grid">
          <thead>
            <tr>
              <th>component</th>
              <th>state</th>
              <th>detail</th>
              <th>extra</th>
            </tr>
          </thead>
          <tbody>
            {components.map(([name, state]) => (
              <tr key={name}>
                <td className="mono nowrap">{name}</td>
                <td>
                  {state.ok ? <Badge tone="accept">ok</Badge> : <Badge tone="warn">not ok</Badge>}
                </td>
                <td>{state.detail ? state.detail : <span className="faint">—</span>}</td>
                <td>
                  <ExtraBag extra={state.extra} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/* ------------------------------------------------------------ local BERT */

function Bert({ bert }: { bert: BertStatus }) {
  const enabled = bert.enabled === true;
  const loaded = bert.loaded === true;
  const known = new Set(['enabled', 'loaded', 'model_dir', 'device']);
  const extra = Object.entries(bert).filter(([k]) => !known.has(k));

  return (
    <Panel title="The local BERT tier" stack>
      {enabled && !loaded ? (
        <Finding tone="warn" glyph="⚠">
          <strong>Enabled, but not loaded.</strong> The cascade is running without the tier
          somebody switched on — quietly, because a missing local model is not a readiness
          failure. Classification still works and still abstains correctly; it is simply less
          accurate than the operator intended. This is an accuracy question, not an egress one:
          BERT here is a local model directory, and enabling it sends nothing anywhere.
        </Finding>
      ) : enabled ? (
        <Finding tone="ok" glyph="✓">
          <strong>Loaded and in the cascade.</strong> It runs in this process from a local model
          directory — no model download, no inference API. Enabling it changes accuracy, not
          egress.
        </Finding>
      ) : (
        <Finding glyph="—">
          <strong>Off.</strong> The structural, anchor and lexical tiers carry classification on
          their own, and the four gates apply to their output exactly as they would with BERT in
          the mix. Local either way — this is an accuracy choice, not an egress one.
        </Finding>
      )}

      <div className="pos-facts">
        <Fact label="enabled">
          {/* Off is a legitimate posture here, so this is neutral — not a red "no". */}
          <Badge tone={enabled ? 'accent' : 'neutral'}>{String(enabled)}</Badge>
        </Fact>
        <Fact label="loaded">
          {enabled ? <YesNo value={loaded} /> : <span className="faint">n/a</span>}
        </Fact>
        <Fact label="device">
          <span className="mono">{typeof bert.device === 'string' ? bert.device : '—'}</span>
        </Fact>
        <Fact label="model_dir">
          <span className="mono" style={{ wordBreak: 'break-all', fontSize: 'var(--t-xs)' }}>
            {typeof bert.model_dir === 'string' ? bert.model_dir : '—'}
          </span>
        </Fact>
        {extra.map(([k, v]) => (
          <Fact key={k} label={k}>
            <span className="mono">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
          </Fact>
        ))}
      </div>
    </Panel>
  );
}

/* --------------------------------------------------------- raw evidence */

type EvidenceTab = 'readyz' | 'health' | 'metrics';
const TABS: EvidenceTab[] = ['readyz', 'health', 'metrics'];
const isTab = (v: string | null): v is EvidenceTab => TABS.includes(v as EvidenceTab);

function RawEvidence({
  posture,
  healthBody,
  metricsText,
  tab,
  onTab,
}: {
  posture: ReadinessResponse;
  healthBody: HealthResponse | null;
  metricsText: string | null;
  tab: EvidenceTab;
  onTab: (t: EvidenceTab) => void;
}) {
  return (
    <Panel
      title="Raw evidence"
      actions={
        <div className="pos-tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t}
              role="tab"
              aria-selected={tab === t}
              className={`pos-tab ${tab === t ? 'active' : ''}`}
              onClick={() => onTab(t)}
            >
              /{t}
            </button>
          ))}
        </div>
      }
      stack
    >
      <p className="muted" style={{ maxWidth: '82ch' }}>
        In an audit, "the console said" is worth less than "the service said". Everything above is
        a rendering of these three responses and nothing else. The tab is in the URL, so a link to
        this page points at exactly what was being looked at.
      </p>
      {tab === 'readyz' && <JsonView value={posture} title="GET /readyz" maxHeight={480} />}
      {tab === 'health' &&
        (healthBody ? (
          <JsonView value={healthBody} title="GET /health" maxHeight={200} />
        ) : (
          <p className="faint">not read.</p>
        ))}
      {tab === 'metrics' &&
        (metricsText ? (
          <JsonView value={metricsText} title="GET /metrics" plain maxHeight={480} />
        ) : (
          <p className="faint">not read.</p>
        ))}
    </Panel>
  );
}

/* ------------------------------------------------------------------ page */

export default function Posture({ readiness, onRefresh }: PosturePageProps) {
  const [params, setParams] = useSearchParams();
  const raw = params.get('evidence');
  const tab: EvidenceTab = isTab(raw) ? raw : 'readyz';

  const setTab = useCallback(
    (t: EvidenceTab) => {
      const next = new URLSearchParams(params);
      if (t === 'readyz') next.delete('evidence');
      else next.set('evidence', t);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  /* The local probe: only ever used while the shell's copy is null. */
  const [probe, setProbe] = useState<{ body: ReadinessResponse | null; error: unknown | null }>({
    body: null,
    error: null,
  });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (readiness) return;
    let live = true;
    fetchReadiness()
      .then((body) => live && setProbe({ body, error: null }))
      .catch((err) => live && setProbe({ body: null, error: asApiError(err) }));
    return () => {
      live = false;
    };
  }, [readiness, attempt]);

  /* /metrics and /health: read once, and again when the operator asks for a refresh. */
  const [metricsText, setMetricsText] = useState<string | null>(null);
  const [healthBody, setHealthBody] = useState<HealthResponse | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    fetchMetrics()
      .then((text) => live && setMetricsText(text))
      .catch(() => live && setMetricsText(null));
    fetchHealth()
      .then((body) => live && setHealthBody(body))
      .catch(() => live && setHealthBody(null));
    return () => {
      live = false;
    };
  }, [reload]);

  const posture = readiness ?? probe.body;

  /* When this page last saw a change. An auditor's first question is "as of when". */
  const [observedAt, setObservedAt] = useState<Date | null>(null);
  useEffect(() => {
    if (posture) setObservedAt(new Date());
  }, [posture]);

  const refresh = useCallback(() => {
    onRefresh();
    setAttempt((n) => n + 1);
    setReload((n) => n + 1);
  }, [onRefresh]);

  const head = useMemo(
    () => (
      <PageHead
        title="Posture"
        lede="What this deployment is allowed to do: the pre-classification egress invariant, how documents get read in the first place, which tiers can spend money, what is loaded, and what is degraded. Every number here is a field of a response, quoted below."
        actions={
          <button className="btn btn-sm" onClick={refresh}>
            refresh
          </button>
        }
      />
    ),
    [refresh],
  );

  if (!posture) {
    return (
      <main className="page">
        {head}
        {probe.error ? (
          <ErrorState
            title="this service did not answer"
            error={probe.error}
            body={
              <>
                <code>/readyz</code> could not be read, so this page can assert nothing at all —
                not the egress invariant, not the tier ledger, not the registry. An unanswered
                probe is not a green light: treat the posture as unknown until it answers.
              </>
            }
            action={
              <button className="btn" onClick={refresh}>
                try again
              </button>
            }
          />
        ) : (
          <Loading label="reading /readyz…" />
        )}
      </main>
    );
  }

  return (
    <main className="page">
      {head}

      <div className="posture-ident">
        <span className="mono">{posture.service}</span>
        <span>
          version <span className="mono">{posture.version}</span>
        </span>
        <Badge
          tone={posture.ready ? 'accept' : 'warn'}
          title={
            posture.ready
              ? '/readyz answered 200'
              : '/readyz answered 503 and said why — see degraded below'
          }
        >
          {posture.ready ? 'ready' : 'not ready'}
        </Badge>
        {observedAt && <span className="faint">observed at {clock(observedAt)}</span>}
      </div>

      <div className="posture-sections">
        {!posture.ready && (
          <Finding tone="abstain" glyph="i">
            <strong>Not ready is not broken.</strong> <code>/readyz</code> answered <b>503</b> and
            returned this entire body to say which component is unhappy — that is the probe doing
            its job. A load balancer will stop sending traffic here; nothing has been misreported.
          </Finding>
        )}

        <Invariant posture={posture} metricsText={metricsText} />
        <OcrReading posture={posture} />
        <Limitation posture={posture} />
        <TierLedger tiers={posture.tiers} components={posture.components} />
        <RegistryAndEngines posture={posture} />
        <Bert bert={posture.bert} />
        <RawEvidence
          posture={posture}
          healthBody={healthBody}
          metricsText={metricsText}
          tab={tab}
          onTab={setTab}
        />
      </div>
    </main>
  );
}
