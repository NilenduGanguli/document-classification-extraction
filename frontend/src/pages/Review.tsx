/**
 * /review — the human queue.
 *
 * This is where an abstention lands, and it is the reason abstaining is a feature rather than a
 * shrug. Every item here is **one FIELD of one document**, not a whole document. The list is
 * grouped by document anyway, because a field name means nothing without the document it came
 * from — but the counts, the decisions and the double-entry ledger are all per field.
 *
 * Four things this page refuses to get wrong:
 *
 *  1. `reviewer` is required, has no default, and is shown next to every button. An
 *     unattributed decision in a KYC system is not a decision.
 *  2. A 200 does not mean the item closed. Everything branches on `ReviewItem.status`, never on
 *     the HTTP code, and `approvals.length of required_approvals` is on every row.
 *  3. 409 is an outcome. The queue's own sentence is rendered verbatim, and the double-entry
 *     mismatch — where BOTH entries are discarded — says so in as many words, because a
 *     reviewer who thinks their entry survived will not re-key it.
 *  4. Blind double entry means blind. A second reviewer never sees the first entry.
 *
 * And one thing about the backend: `review_queue_backend=memory` loses every pending item and
 * every decision on restart. `/readyz` reports it, so this page says it out loud rather than
 * letting somebody discover it by losing a day of work.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import * as api from '../api';
import { ApiError, asApiError } from '../api';
import type { ReviewAction } from '../api';
import {
  Badge,
  DocTypeBadge,
  EmptyState,
  ErrorState,
  Loading,
  Meter,
  PageHead,
  Panel,
  PiiValue,
  Spinner,
  useToast,
} from '../components';
import type { BadgeTone } from '../components';
import type { DocTypeSummary, ReviewItem, ReviewListResponse, ReviewStatus } from '../types';

import './Review.css';
import type { PageProps } from './contract';

/* ------------------------------------------------------ reviewer identity */

/**
 * Who is signing.
 *
 * Kept in localStorage for the session and NEVER in the URL: the whole point of the deep links
 * on this page is that a reviewer can send somebody a link to an item, and a link that carries
 * an identity invites the next person to act under it.
 */
const REVIEWER_KEY = 'dce.reviewer';

function readReviewer(): string {
  try {
    return window.localStorage.getItem(REVIEWER_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeReviewer(value: string): void {
  try {
    if (value) window.localStorage.setItem(REVIEWER_KEY, value);
    else window.localStorage.removeItem(REVIEWER_KEY);
  } catch {
    /* private browsing; the identity just will not persist across a reload */
  }
}

/** The signing identity, shown permanently in the page head and next to every action. */
function ReviewerControl({
  reviewer,
  onChange,
}: {
  reviewer: string;
  onChange: (value: string) => void;
}) {
  const [editing, setEditing] = useState(!reviewer);
  const [draft, setDraft] = useState(reviewer);

  useEffect(() => {
    setDraft(reviewer);
  }, [reviewer]);

  if (!editing) {
    return (
      <span className="row" style={{ gap: 'var(--s-2)' }}>
        <span className="label">signing as</span>
        <span className="mono">{reviewer}</span>
        <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>
          change
        </button>
      </span>
    );
  }
  return (
    <form
      className="row"
      style={{ gap: 'var(--s-2)' }}
      onSubmit={(e) => {
        e.preventDefault();
        const next = draft.trim();
        if (!next) return;
        onChange(next);
        setEditing(false);
      }}
    >
      <label className="label" htmlFor="dce-reviewer">
        signing as
      </label>
      <input
        id="dce-reviewer"
        type="text"
        value={draft}
        autoComplete="off"
        placeholder="your identity"
        onChange={(e) => setDraft(e.target.value)}
        style={{ width: 180 }}
      />
      <button className="btn btn-sm btn-primary" type="submit" disabled={!draft.trim()}>
        set
      </button>
      {reviewer && (
        <button className="btn btn-ghost btn-sm" type="button" onClick={() => setEditing(false)}>
          cancel
        </button>
      )}
    </form>
  );
}

/* -------------------------------------------------------------- vocabulary */

/** `field_name` on an item that is about the whole document — `dce.review.DOCUMENT_FIELD`. */
const DOCUMENT_FIELD = '__document__';

/**
 * The queue's reason codes, expanded into the sentence a reviewer needs.
 *
 * The prefix before the colon is machine-readable and stable (`dce.review.REASON_*`); the rest
 * is the queue's own prose and is always rendered verbatim next to this.
 */
const REASONS: Record<string, { label: string; tone: BadgeTone; explain: string }> = {
  classification_abstained: {
    label: 'classifier abstained',
    tone: 'abstain',
    explain:
      'Four gates have to hold together before a doctype is accepted. One did not, so nothing ' +
      'was extracted and nothing was sent to any remote tier. Deciding what this document is, ' +
      'is the question.',
  },
  missing_required: {
    label: 'required field missing',
    tone: 'warn',
    explain:
      'The schema requires this field and no candidate was found on the document. That is not ' +
      'always a bug — a blank or unfilled form legitimately has nothing here — so the queue ' +
      'asks rather than guesses.',
  },
  below_confidence_threshold: {
    label: 'below the confidence floor',
    tone: 'warn',
    explain:
      'A value was found, but under this deployment’s accept floor. It is shown as extracted; ' +
      'check it against the document before approving it.',
  },
  validator_error: {
    label: 'validator complained',
    tone: 'warn',
    explain:
      'A value was found and the field’s validator rejected it — a checksum that does not ' +
      'compute, or a pattern that does not match. Treat the extracted value as suspect.',
  },
};

function splitReason(reason: string): { code: string; text: string } {
  const at = reason.indexOf(':');
  if (at === -1) return { code: '', text: reason };
  const code = reason.slice(0, at).trim();
  if (!(code in REASONS)) return { code: '', text: reason };
  return { code, text: reason.slice(at + 1).trim() };
}

/**
 * How a status reads.
 *
 * `pending` is the abstain blue, not a warning: an item sitting here is the service having
 * done the right thing. `rejected` is deliberately quiet — it is a closed item with nothing
 * recorded, which is the safe direction, not a failure.
 */
const STATUS_TONE: Record<string, BadgeTone> = {
  pending: 'abstain',
  approved: 'accept',
  corrected: 'accept',
  rejected: 'neutral',
};

const STATUS_FILTERS: Array<[string, string]> = [
  ['pending', 'pending'],
  ['approved', 'approved'],
  ['corrected', 'corrected'],
  ['rejected', 'rejected'],
  ['all', 'all statuses'],
];

/** Relative age of an item, coarse on purpose — "how long has this been waiting". */
function age(iso: string): string {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return '';
  const seconds = Math.max(0, (Date.now() - at) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86_400)}d`;
}

function stamp(iso: string | null | undefined): string {
  if (!iso) return '—';
  const at = Date.parse(iso);
  return Number.isFinite(at) ? new Date(at).toLocaleString() : iso;
}

const sameReviewer = (a: string, b: string): boolean =>
  a.trim().toLowerCase() === b.trim().toLowerCase() && a.trim() !== '';

/**
 * The signature ledger, as pips.
 *
 * On a double-entry item the unfilled pip is the whole message: somebody has already signed and
 * the item is STILL open. That has to be legible before anyone clicks, not after.
 */
function Approvals({ item }: { item: ReviewItem }) {
  const need = Math.max(1, item.required_approvals);
  const have = Math.min(item.approvals.length, need);
  const done = have >= need;
  const title =
    need > 1
      ? `${have} of ${need} independent approvals — this field is PII and checksum-backed, so it needs two different people`
      : `${have} of ${need} approvals`;
  if (need === 1 && have === 0) {
    return (
      <span className="faint tabular" title={title}>
        0/1
      </span>
    );
  }
  return (
    <span className="row" style={{ gap: 'var(--s-1)' }} title={title}>
      <span className={`rev-pips ${done ? 'done' : ''}`} aria-hidden="true">
        {Array.from({ length: need }, (_, i) => (
          <span key={i} className={`rev-pip ${i < have ? 'filled' : ''}`} />
        ))}
      </span>
      <span className="faint tabular">
        {have}/{need}
      </span>
    </span>
  );
}

/* ------------------------------------------------------------- doctype pick */

/**
 * Searchable against `GET /doctypes`.
 *
 * Only offered on a `__document__` item, because that is the only item where the value under
 * review IS a doctype. On a field item the value is whatever the document says, and a doctype
 * picker there would be an invitation to key the wrong thing.
 */
function DocTypePicker({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [all, setAll] = useState<DocTypeSummary[] | null>(null);
  const [failed, setFailed] = useState<ApiError | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    api
      .listDocTypes({}, ctl.signal)
      .then((r) => setAll(r.doctypes))
      .catch((e) => {
        if (!ctl.signal.aborted) setFailed(asApiError(e));
      });
    return () => ctl.abort();
  }, []);

  const query = value.trim().toLowerCase();
  const hits = useMemo(() => {
    if (!all) return [];
    if (!query) return all.slice(0, 8);
    return all
      .filter(
        (d) =>
          d.doctype_id.toLowerCase().includes(query) || d.label.toLowerCase().includes(query),
      )
      .slice(0, 8);
  }, [all, query]);

  const known = all?.some((d) => d.doctype_id === value.trim()) ?? true;

  return (
    <div className="rev-doctype-search stack" style={{ gap: 'var(--s-2)' }}>
      <input
        type="text"
        className="rev-field-input"
        value={value}
        disabled={disabled}
        autoComplete="off"
        spellCheck={false}
        placeholder="search 182 doctypes by id or label…"
        onChange={(e) => onChange(e.target.value)}
        aria-label="corrected doctype"
      />
      {failed && (
        <div className="rev-hint">
          the registry did not answer ({failed.message}) — you can still type a doctype id
        </div>
      )}
      {!all && !failed && <Spinner label="loading the registry…" />}
      {hits.length > 0 && (
        <div className="rev-doctype-hits" role="listbox" aria-label="matching doctypes">
          {hits.map((d) => (
            <button
              key={d.doctype_id}
              type="button"
              role="option"
              aria-selected={d.doctype_id === value.trim()}
              className={`rev-doctype-hit ${d.doctype_id === value.trim() ? 'active' : ''}`}
              disabled={disabled}
              onClick={() => onChange(d.doctype_id)}
            >
              <span>{d.label || d.doctype_id}</span>
              <span className="hit-id">{d.doctype_id}</span>
            </button>
          ))}
        </div>
      )}
      {value.trim() && !known && (
        <div className="rev-hint">
          <span className="mono">{value.trim()}</span> is not a doctype id in this registry. The
          queue will store exactly what you type, but nothing downstream will match it.
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- decide */

interface DecideProps {
  item: ReviewItem;
  reviewer: string;
  /** True when the queue is in-process memory — a 404 then probably means a restart. */
  volatileQueue: boolean;
  /** The server's updated item, plus which action produced it. */
  onDecided: (updated: ReviewItem, action: ReviewAction) => void;
  /** The queue may have mutated underneath us (a mismatch discards both entries). Re-read it. */
  onStale: () => void;
}

/**
 * The three decisions, and every refusal rendered as the outcome it is.
 *
 * Mounted with `key={item.id}` by the caller, so the typed value never survives a change of
 * item — carrying a keyed identifier from one field to the next is exactly the accident
 * double entry exists to catch.
 */
function Decide({ item, reviewer, volatileQueue, onDecided, onStale }: DecideProps) {
  const [value, setValue] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState<ReviewAction | null>(null);
  const [refusal, setRefusal] = useState<ApiError | null>(null);

  const pending = item.status === 'pending';
  const signed = item.approvals.some((a) => sameReviewer(a, reviewer));
  const doubleEntry = item.required_approvals > 1;
  const isDocument = item.field_name === DOCUMENT_FIELD;
  const who = reviewer.trim();

  const run = async (action: ReviewAction) => {
    setBusy(action);
    setRefusal(null);
    try {
      const updated = await api.decideReview(item.id, action, {
        reviewer: who,
        note: note.trim() || undefined,
        value: action === 'correct' ? value.trim() : undefined,
      });
      setValue('');
      setNote('');
      onDecided(updated, action);
    } catch (caught) {
      const err = asApiError(caught);
      setRefusal(err);
      // A 409 means the queue understood us and moved on without us — including the mismatch
      // case, where it just cleared BOTH entries. Whatever we are showing is now stale.
      if (err.status === 409 || err.status === 404) onStale();
    } finally {
      setBusy(null);
    }
  };

  if (!pending) {
    return (
      <div className="stack" style={{ gap: 'var(--s-3)' }}>
        <div className="muted">
          This item is closed. Decisions are made once — the queue will refuse a second one, and
          that refusal is the audit trail working.
        </div>
        <dl className="rev-kv">
          <dt>decided by</dt>
          <dd className="mono">{item.reviewer || '—'}</dd>
          <dt>at</dt>
          <dd>{stamp(item.decided_at)}</dd>
          {item.decision_note && (
            <>
              <dt>note</dt>
              <dd>{item.decision_note}</dd>
            </>
          )}
        </dl>
      </div>
    );
  }

  if (!who) {
    return (
      <ErrorState
        tone="warn"
        title="set your identity first"
        body={
          <>
            Every decision is recorded against the person who made it, and blind double entry is
            meaningless without two distinct identities. Use <em>signing as</em> at the top of
            this page.
          </>
        }
      />
    );
  }

  return (
    <div className="stack" style={{ gap: 'var(--s-3)' }}>
      {doubleEntry && (
        <div className="rev-blind-notice">
          <strong>Four eyes.</strong> This field is both PII and backed by a real check digit, so
          it takes <strong>two independent decisions from two different people</strong>. Your
          decision will be recorded and the item will stay <em>pending</em> until somebody else
          signs it too. The rule is enforced by the queue, not by this page.
        </div>
      )}

      {signed && (
        <div className="rev-blind-notice">
          You have already signed this item as <span className="mono">{who}</span>. The second
          decision must come from somebody else — two signatures from one pair of eyes is the
          failure this control exists to prevent. You can still <em>reject</em> it.
        </div>
      )}

      <div className="row" style={{ gap: 'var(--s-2)' }}>
        <span className="label">signing as</span>
        <span className="mono">{who}</span>
      </div>

      <label className="stack" style={{ gap: 'var(--s-1)' }}>
        <span className="label">note (optional, kept on the item)</span>
        <textarea
          rows={2}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="why — read by whoever picks this document up next"
        />
      </label>

      <div className="rev-actions">
        <button
          className="btn btn-primary"
          disabled={busy !== null || signed}
          onClick={() => run('approve')}
          title={
            signed
              ? 'you have already signed this item'
              : 'accept the extracted value as it stands'
          }
        >
          {busy === 'approve' ? <Spinner /> : null} approve
        </button>
        <button
          className="btn"
          disabled={busy !== null}
          onClick={() => run('reject')}
          title="refuse the value — nothing is recorded, and one reviewer is enough"
        >
          {busy === 'reject' ? <Spinner /> : null} reject
        </button>
      </div>
      <div className="rev-hint">
        {doubleEntry
          ? 'Rejection takes one reviewer even on a double-entry item: it puts nothing into the record, so it is the safe direction.'
          : 'Approving accepts the extracted value as it stands. Rejecting puts nothing into the record — it is the safe direction, and it sends the field back for another look.'}
      </div>

      <hr style={{ border: 'none', borderTop: '1px solid var(--border)', margin: 0 }} />

      <div className="stack" style={{ gap: 'var(--s-2)' }}>
        <span className="label">
          {isDocument ? 'correct — what is this document?' : `correct — ${item.field_name}`}
        </span>
        {isDocument ? (
          <>
            <div className="rev-hint">
              This item is the whole document, not a field: the classifier declined to place it.
              The value you enter is the <strong>doctype id</strong> it should have been.
            </div>
            <DocTypePicker value={value} onChange={setValue} disabled={busy !== null || signed} />
          </>
        ) : (
          <>
            <input
              type="text"
              className="rev-field-input mono"
              value={value}
              disabled={busy !== null || signed}
              autoComplete="off"
              spellCheck={false}
              placeholder="the value as it appears on the document"
              aria-label={`corrected value for ${item.field_name}`}
              onChange={(e) => setValue(e.target.value)}
            />
            {item.pii && (
              <div className="rev-hint">
                This field is marked PII in the registry. What you type stays in this form — it is
                never put in the URL and never logged by the service, which records the item id
                and your identity only.
              </div>
            )}
          </>
        )}
        {doubleEntry && (
          <div className="rev-hint">
            Read the value off the document and type it yourself. Do not copy it from anywhere
            else on this screen — an independent second keying is the only thing that catches a
            typo in a checksummed identifier.
          </div>
        )}
        <div className="rev-actions">
          <button
            className="btn btn-primary"
            disabled={busy !== null || signed || !value.trim()}
            onClick={() => run('correct')}
            title={
              !value.trim()
                ? 'a correction needs a value — an empty one is an approval, and the queue says so'
                : 'replace the extracted value with this one'
            }
          >
            {busy === 'correct' ? <Spinner /> : null} submit correction
          </button>
        </div>
        {!value.trim() && (
          <div className="rev-hint">
            An empty correction is an approval; the queue returns 400 rather than pretending
            otherwise, so this button stays off until there is a value.
          </div>
        )}
      </div>

      {refusal && <Refusal error={refusal} volatileQueue={volatileQueue} />}
    </div>
  );
}

/**
 * A refusal from the queue, rendered as an outcome.
 *
 * None of these is a bug and none of them is red. The queue's own sentence is the thing the
 * reviewer has to read — it says what to do next — so it is always shown verbatim.
 */
function Refusal({ error, volatileQueue }: { error: ApiError; volatileQueue: boolean }) {
  const message = error.message;

  if (error.status === 409 && /mismatch/i.test(message)) {
    return (
      <ErrorState
        tone="warn"
        title="the two entries disagreed — BOTH were discarded"
        error={error}
        body={
          <>
            This is the control firing, not a fault. Two people keyed this value independently and
            got different answers, so the queue threw away <strong>both</strong> — including the
            one you just submitted. Nothing has been recorded and the item is back to square one
            with zero signatures. Read the value off the document again and re-enter it.
          </>
        }
      />
    );
  }

  if (error.status === 409) {
    return (
      <ErrorState
        tone="warn"
        title="the queue refused this decision"
        error={error}
        body="The queue understood the request and declined it. Its sentence above says what has to happen instead."
      />
    );
  }

  if (error.status === 400) {
    return (
      <ErrorState
        tone="warn"
        title="that is not a correction"
        error={error}
        body="A correction has to carry a value. If the extracted value is right, approve it; if it is wrong and there is nothing to put in its place, reject it."
      />
    );
  }

  if (error.status === 404) {
    return (
      <ErrorState
        tone="warn"
        title="this item is no longer in the queue"
        error={error}
        body={
          volatileQueue
            ? 'This deployment keeps the queue in memory, so a service restart discards every item and every decision. That is the most likely explanation.'
            : 'Somebody may have cleared it, or the queue was replaced underneath this page.'
        }
      />
    );
  }

  if (error.status === 501) {
    return (
      <ErrorState
        tone="warn"
        title="this queue cannot do that"
        error={error}
        body="The queue module installed here does not implement this transition. That is a deployment fact, not a mistake you made."
      />
    );
  }

  if (error.status === 503) {
    return (
      <ErrorState
        tone="warn"
        title="no review queue is installed"
        error={error}
        action={<Link to="/posture">see what this deployment has</Link>}
      />
    );
  }

  return <ErrorState error={error} />;
}

/* ------------------------------------------------------------- the detail */

function Detail({
  item,
  reviewer,
  volatileQueue,
  onDecided,
  onStale,
}: DecideProps) {
  const { code, text } = splitReason(item.reason);
  const meaning = code ? REASONS[code] : undefined;
  const isDocument = item.field_name === DOCUMENT_FIELD;
  const signed = item.approvals.some((a) => sameReviewer(a, reviewer));

  /**
   * Blind means blind. On a pending double-entry item, somebody else's keyed value is withheld
   * from anyone who has not yet keyed their own — not masked-with-a-reveal-button, withheld.
   * A reveal button here would destroy the only check that catches a typo in a checksummed
   * identifier, and it would be clicked.
   */
  const withholdFirstEntry =
    item.required_approvals > 1 && item.status === 'pending' && item.approvals.length > 0 && !signed;

  return (
    <>
      <Panel
        title={
          <span className="row" style={{ gap: 'var(--s-2)' }}>
            <span className="mono">{isDocument ? 'the whole document' : item.field_name}</span>
            <Badge tone={STATUS_TONE[item.status] ?? 'neutral'}>{item.status}</Badge>
          </span>
        }
        actions={<Approvals item={item} />}
        stack
      >
        {meaning && (
          <div className="stack" style={{ gap: 'var(--s-2)' }}>
            <div className="row" style={{ gap: 'var(--s-2)' }}>
              <Badge tone={meaning.tone}>{meaning.label}</Badge>
              <span className="faint mono" style={{ fontSize: 'var(--t-xs)' }}>
                {code}
              </span>
            </div>
            <div className="muted">{meaning.explain}</div>
          </div>
        )}
        {text && <div className="mono" style={{ fontSize: 'var(--t-sm)' }}>{text}</div>}

        <dl className="rev-kv">
          <dt>document</dt>
          <dd className="mono">{item.doc_id || '—'}</dd>

          <dt>doctype</dt>
          <dd>
            <DocTypeBadge doctypeId={item.doctype_id} link />
          </dd>

          <dt>item id</dt>
          <dd className="mono">{item.id}</dd>

          <dt>extracted</dt>
          <dd>
            {item.value === null || item.value === undefined || item.value === '' ? (
              <span className="faint">
                nothing was extracted{' '}
                {code === 'missing_required'
                  ? '— no candidate was found on the document'
                  : code === 'classification_abstained'
                    ? '— the document was never placed, so no schema applied'
                    : ''}
              </span>
            ) : (
              <PiiValue value={item.value} pii={item.pii} />
            )}
          </dd>

          {!isDocument && (
            <>
              <dt>confidence</dt>
              <dd>
                {item.value ? (
                  <Meter
                    name=""
                    value={item.confidence}
                    status={code === 'below_confidence_threshold' ? 'fail' : undefined}
                    note="the extractor’s own confidence. This deployment’s accept floor is not reported on the wire, so no gate is drawn."
                  />
                ) : (
                  <span className="faint tabular">
                    {item.confidence.toFixed(2)} — nothing was found to be confident about
                  </span>
                )}
              </dd>
            </>
          )}

          <dt>where</dt>
          <dd>
            {item.page || item.bbox ? (
              <span className="mono" style={{ fontSize: 'var(--t-sm)' }}>
                {item.page ? `page ${item.page}` : 'page not recorded'}
                {item.bbox ? ` · [${item.bbox.map((n) => Math.round(n)).join(', ')}]` : ''}
              </span>
            ) : (
              <span className="faint">
                not located on a page — check the document itself
              </span>
            )}
          </dd>

          <dt>waiting</dt>
          <dd>
            <span className="tabular">{age(item.created_at)}</span>{' '}
            <span className="faint">since {stamp(item.created_at)}</span>
          </dd>

          <dt>signatures</dt>
          <dd>
            {item.approvals.length ? (
              <span className="mono">{item.approvals.join(', ')}</span>
            ) : (
              <span className="faint">none yet</span>
            )}
            {item.required_approvals > 1 && (
              <>
                {' '}
                <Badge tone="pii" title="PII + a real check digit — this is why it needs two people">
                  double entry
                </Badge>
              </>
            )}
          </dd>

          <dt>correction</dt>
          <dd>
            {withholdFirstEntry ? (
              <span className="faint">
                withheld — see below
              </span>
            ) : item.corrected_value ? (
              <PiiValue value={item.corrected_value} pii={item.pii} />
            ) : (
              <span className="faint">none</span>
            )}
          </dd>

          {/*
            The queue writes its own trail here, and the one that matters is the double-entry
            mismatch: the item comes back PENDING with a note naming who disagreed and no value
            at all. Without this row that history is invisible and the next reviewer cannot tell
            a fresh item from one two people have already failed to agree on.
          */}
          {item.status === 'pending' && item.decision_note && (
            <>
              <dt>queue note</dt>
              <dd>
                {item.decision_note}
                {/mismatch/i.test(item.decision_note) && (
                  <div className="rev-hint">
                    Both entries were discarded and the item was reopened with no value. It has to
                    be keyed again, twice, by two different people.
                  </div>
                )}
              </dd>
            </>
          )}

          {item.pii && (
            <>
              <dt>pii</dt>
              <dd>
                <Badge tone="pii">masked by default</Badge>
              </dd>
            </>
          )}
        </dl>

        {withholdFirstEntry && (
          <div className="rev-blind-notice">
            <strong>A value has already been keyed for this item, and it is not shown.</strong>{' '}
            That is what blind double entry means: if you could see the first entry you would key
            the same thing, and the control would catch nothing. Read the value off the document
            and enter it independently below. If the two do not match, both are discarded and
            neither person&rsquo;s answer is trusted.
          </div>
        )}
      </Panel>

      <Panel title="decide" stack>
        <Decide
          item={item}
          reviewer={reviewer}
          volatileQueue={volatileQueue}
          onDecided={onDecided}
          onStale={onStale}
        />
      </Panel>
    </>
  );
}

/* ------------------------------------------------------------------ page */

export default function Review({ readiness }: PageProps) {
  const [params, setParams] = useSearchParams();
  const { push } = useToast();

  const status = params.get('status') || 'pending';
  const doctype = params.get('doctype') || '';
  const selectedId = params.get('item') || '';
  const limit = Math.max(1, Number(params.get('limit')) || 100);

  const [reviewer, setReviewer] = useState(readReviewer);
  const [list, setList] = useState<ReviewListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [nonce, setNonce] = useState(0);
  /** The last item the server handed back, so a decided item does not vanish mid-read. */
  const [decided, setDecided] = useState<ReviewItem | null>(null);

  /** Doctypes seen in any response this session — the filter's options survive filtering. */
  const seenDoctypes = useRef<Set<string>>(new Set());

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  const setParam = useCallback(
    (key: string, value: string) => {
      setParams((prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        // Changing a filter can only invalidate a selection made under the old one. Raising the
        // limit cannot — it strictly widens the page — so the selection survives that.
        if (key === 'status' || key === 'doctype') next.delete('item');
        return next;
      });
    },
    [setParams],
  );

  useEffect(() => {
    const ctl = new AbortController();
    setLoading(true);
    api
      .listReview(
        {
          status: status === 'all' ? 'all' : (status as ReviewStatus),
          doctype: doctype || undefined,
          limit,
        },
        ctl.signal,
      )
      .then((response) => {
        if (ctl.signal.aborted) return;
        for (const it of response.items) if (it.doctype_id) seenDoctypes.current.add(it.doctype_id);
        setList(response);
        setError(null);
      })
      .catch((caught) => {
        if (ctl.signal.aborted) return;
        setError(asApiError(caught));
        setList(null);
      })
      .finally(() => {
        if (!ctl.signal.aborted) setLoading(false);
      });
    return () => ctl.abort();
  }, [status, doctype, limit, nonce]);

  const onDecided = useCallback(
    (updated: ReviewItem, action: ReviewAction) => {
      setDecided(updated);
      reload();
      // Branch on the item's status, never on the HTTP code: a 200 on a double-entry item means
      // "your decision was recorded", not "this item is closed".
      if (updated.status === 'pending') {
        const need = Math.max(1, updated.required_approvals);
        push(
          `recorded — ${updated.approvals.length} of ${need}. This item is still pending; the next ` +
            `${action === 'correct' ? 'entry' : 'signature'} must come from somebody else.`,
          'warn',
        );
      } else {
        push(`${updated.status} by ${updated.reviewer || reviewer}`, 'accept');
      }
    },
    [push, reload, reviewer],
  );

  const items = list?.items ?? [];
  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) ?? (decided?.id === selectedId ? decided : null),
    [items, selectedId, decided],
  );

  /** One group per document. An item is still one field; the document is the context. */
  const groups = useMemo(() => {
    const byDoc = new Map<string, ReviewItem[]>();
    for (const item of items) {
      const key = item.doc_id || '(no document id)';
      const bucket = byDoc.get(key);
      if (bucket) bucket.push(item);
      else byDoc.set(key, [item]);
    }
    return [...byDoc.entries()];
  }, [items]);

  const doubleEntryOnPage = items.filter((i) => i.required_approvals > 1).length;
  const partiallySigned = items.filter(
    (i) => i.status === 'pending' && i.approvals.length > 0,
  ).length;

  const backendRaw = readiness?.components?.tiers?.extra?.['review_queue_backend'];
  const backend = typeof backendRaw === 'string' ? backendRaw : null;
  const volatileQueue = backend === 'memory';
  const queueProblem = readiness?.tiers?.find((t) => t.tier === 't5_review')?.problem || '';

  const doctypeOptions = useMemo(() => {
    const set = new Set(seenDoctypes.current);
    if (doctype) set.add(doctype);
    return [...set].sort();
  }, [doctype, list]);

  const depth = list?.depth ?? null;
  const capped = list !== null && list.count >= limit;

  /*
   * The doctype filter is applied to the PAGE, not to the queue.
   *
   * `ReviewPort.list` (dce/api/routes.py) asks the store for `limit` items and only then drops
   * the ones whose doctype does not match, so `?doctype=X&limit=25` searches the oldest 25
   * items and nothing else. Verified against the running service: doctype=ca_t1_general returns
   * 0 at limit=25 and 5 at limit=500, with the same queue.
   *
   * A console that renders that 0 as "nothing is waiting" would be telling a reviewer the
   * backlog is clear when it is not, which is the single worst thing this page could do. So
   * whenever the queue is deeper than the page, a doctype-filtered result is reported as what
   * it is — a search of the first N items — and never as an authoritative empty.
   */
  const filterMayBeTruncated = Boolean(doctype) && depth !== null && depth > limit;
  const LIMITS = [25, 100, 250, 500];
  const enoughLimit = LIMITS.find((n) => depth !== null && n >= depth) ?? LIMITS[LIMITS.length - 1];

  return (
    <main className="page">
      <PageHead
        title="Review"
        lede="Every field the service declined to answer on its own. One item is one field of one document; some need two independent people."
        actions={<ReviewerControl reviewer={reviewer} onChange={(v) => { setReviewer(v); writeReviewer(v); }} />}
      />

      <div className="stack">
        {/*
          Durability, before anything else. A reviewer must not find out that the queue is in
          memory by losing a day of decisions to a restart.
        */}
        {volatileQueue && (
          <ErrorState
            tone="warn"
            title="this queue is held in memory — a restart loses everything"
            body={
              <>
                <span className="mono">review_queue_backend=memory</span> keeps the queue inside
                this process: a restart, a redeploy or a second instance loses every pending item
                and every decision recorded here. Fine for a demo, not for a control you have to
                evidence.
              </>
            }
            action={<Link to="/posture">see the deployment&rsquo;s posture</Link>}
          />
        )}
        {backend && !volatileQueue && (
          <div className="muted" style={{ fontSize: 'var(--t-sm)' }}>
            Queue backend: <span className="mono">{backend}</span>
            {queueProblem ? ` — ${queueProblem}` : ' — decisions survive a restart.'}
          </div>
        )}

        <Panel
          className="queue-panel"
          title="Queue"
          actions={
            <div className="review-filters">
              <label className="label" htmlFor="rev-status">
                status
              </label>
              <select
                id="rev-status"
                value={status}
                onChange={(e) => setParam('status', e.target.value)}
              >
                {STATUS_FILTERS.map(([v, label]) => (
                  <option key={v} value={v}>
                    {label}
                  </option>
                ))}
              </select>

              <label className="label" htmlFor="rev-doctype">
                doctype
              </label>
              <select
                id="rev-doctype"
                value={doctype}
                onChange={(e) => setParam('doctype', e.target.value)}
              >
                <option value="">any</option>
                {doctypeOptions.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>

              <label className="label" htmlFor="rev-limit">
                limit
              </label>
              <select
                id="rev-limit"
                value={String(limit)}
                onChange={(e) => setParam('limit', e.target.value)}
              >
                {LIMITS.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>

              <button className="btn btn-ghost btn-sm" onClick={reload} disabled={loading}>
                {loading ? <Spinner /> : null} refresh
              </button>
            </div>
          }
          stack
        >
          {/*
            `count` is the page you asked for. `depth` is the queue's own count of PENDING items,
            whatever the filter — they are different numbers and conflating them would understate
            a backlog the moment somebody filters.
          */}
          <div className="row" style={{ gap: 'var(--s-4)' }}>
            <span>
              <span className="label">showing</span>{' '}
              <span className="tabular" style={{ fontWeight: 600 }}>
                {list?.count ?? 0}
              </span>
            </span>
            <span>
              <span className="label">pending queue-wide</span>{' '}
              <span className="tabular" style={{ fontWeight: 600 }}>
                {list?.depth ?? '—'}
              </span>
            </span>
            {doubleEntryOnPage > 0 && (
              <Badge tone="pii" title="PII + a real check digit: two different people must sign">
                {doubleEntryOnPage} {doubleEntryOnPage === 1 ? 'needs' : 'need'} two people
              </Badge>
            )}
            {partiallySigned > 0 && (
              <Badge tone="abstain" title="one signature recorded, still open">
                {partiallySigned} half-signed
              </Badge>
            )}
            <span className="spacer" style={{ marginLeft: 'auto' }} />
            <span className="rev-legend">
              <span>oldest first</span>
              <span>one item = one field</span>
            </span>
          </div>

          {capped && !filterMayBeTruncated && (
            <div className="rev-hint">
              This page is capped at {limit} items and came back full, so there are probably more.
              Raise the limit, or filter by doctype.
            </div>
          )}

          {filterMayBeTruncated && (
            <div className="rev-blind-notice">
              <strong>This doctype filter searched only the first {limit} items.</strong> The
              service reads {limit} items from the queue and filters them afterwards, so with{' '}
              {depth} pending, a doctype with items further down the queue shows up here as
              nothing at all. Treat this list as a search, not as the doctype&rsquo;s backlog.{' '}
              <button className="btn btn-sm" onClick={() => setParam('limit', String(enoughLimit))}>
                search {enoughLimit} instead
              </button>
            </div>
          )}
        </Panel>

        {error ? (
          error.status === 503 ? (
            <ErrorState
              tone="warn"
              title="no review queue is installed on this deployment"
              error={error}
              body={
                <>
                  That is a posture fact, not a failure: the service still classifies and extracts,
                  it simply has nowhere to route the things it declines to answer. Everything that
                  would have been queued is reported in each response&rsquo;s{' '}
                  <span className="mono">needs_review</span> instead.
                  {queueProblem ? ` /readyz says: ${queueProblem}` : ''}
                </>
              }
              action={<Link to="/posture">see the tier ledger</Link>}
            />
          ) : (
            <ErrorState
              error={error}
              body="The queue could not be read. Nothing has been decided and nothing was lost."
              action={
                <button className="btn" onClick={reload}>
                  try again
                </button>
              }
            />
          )
        ) : loading && !list ? (
          <Panel>
            <Loading label="reading the queue…" />
          </Panel>
        ) : items.length === 0 ? (
          <Panel>
            {/* An empty pending queue is good news. It gets no icon of alarm and no warn tone. */}
            {filterMayBeTruncated ? (
              /*
                NOT "nothing is waiting". The service only looked at the first `limit` items
                before applying the doctype filter, so this empty means "not in the part of the
                queue we read" — and saying otherwise would report a clear backlog that is not.
              */
              <EmptyState
                icon="◔"
                title={`nothing for ${doctype} in the first ${limit} items`}
                body={
                  <>
                    This is not an empty backlog. The service filters by doctype only after
                    reading {limit} items, and {depth} are pending — so items for this doctype
                    further down the queue are simply not in what was read.
                  </>
                }
                action={
                  <button className="btn" onClick={() => setParam('limit', String(enoughLimit))}>
                    search {enoughLimit} items instead
                  </button>
                }
              />
            ) : status === 'pending' && !doctype ? (
              <EmptyState
                icon="✓"
                title="nothing is waiting for a human"
                body="Every field the service declined to answer on its own has been decided. New items appear here on their own whenever a document abstains, a required field comes back empty, a value lands under the confidence floor, or a validator complains."
              />
            ) : (
              <EmptyState
                icon="—"
                title={`no ${status === 'all' ? '' : `${status} `}items${doctype ? ` for ${doctype}` : ''}`}
                body="Nothing matches this filter."
                action={
                  <button
                    className="btn"
                    onClick={() =>
                      setParams((prev) => {
                        const next = new URLSearchParams(prev);
                        next.delete('doctype');
                        next.delete('status');
                        next.delete('item');
                        return next;
                      })
                    }
                  >
                    clear filters
                  </button>
                }
              />
            )}
          </Panel>
        ) : (
          <div className="review-layout">
            <Panel flush>
              {groups.map(([docId, docItems]) => (
                <div className="review-doc" key={docId}>
                  <div className="review-doc-head">
                    <span className="doc-id" title={docId}>
                      {docId}
                    </span>
                    <DocTypeBadge doctypeId={docItems[0].doctype_id} link />
                    <span className="spacer" style={{ marginLeft: 'auto' }} />
                    <span className="faint" style={{ fontSize: 'var(--t-xs)' }}>
                      {docItems.length} {docItems.length === 1 ? 'item' : 'items'}
                    </span>
                  </div>
                  {docItems.map((item) => {
                    const { code } = splitReason(item.reason);
                    const meaning = code ? REASONS[code] : undefined;
                    const isDoc = item.field_name === DOCUMENT_FIELD;
                    return (
                      <button
                        key={item.id}
                        className={`review-row ${item.id === selectedId ? 'selected' : ''}`}
                        onClick={() => setParam('item', item.id)}
                        aria-current={item.id === selectedId}
                      >
                        <span className="field" title={item.field_name}>
                          {isDoc ? 'the whole document' : item.field_name}
                        </span>
                        <span className="right">
                          {item.pii && <Badge tone="pii">pii</Badge>}
                          <Approvals item={item} />
                          <Badge tone={STATUS_TONE[item.status] ?? 'neutral'}>{item.status}</Badge>
                          <span className="rev-age">{age(item.created_at)}</span>
                        </span>
                        <span className="why">
                          {meaning ? meaning.label : item.reason || 'no reason recorded'}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ))}
            </Panel>

            <div className="review-detail-col">
              {selected ? (
                <Detail
                  key={selected.id}
                  item={selected}
                  reviewer={reviewer}
                  volatileQueue={volatileQueue}
                  onDecided={onDecided}
                  onStale={reload}
                />
              ) : (
                <Panel>
                  <EmptyState
                    icon="◔"
                    title={selectedId ? 'that item is not on this page' : 'pick an item'}
                    body={
                      selectedId ? (
                        <>
                          <span className="mono">{selectedId}</span> is not in the current filter.
                          It may have been decided, or it may be past the {limit}-item limit — try{' '}
                          <em>all statuses</em>, or a higher limit.
                        </>
                      ) : (
                        'Each row is one field of one document. Selecting one shows what the classifier said, why the field is here, and the three decisions you can record against it.'
                      )
                    }
                  />
                </Panel>
              )}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
