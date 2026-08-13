/**
 * Error and not-error states.
 *
 * This console has three things that look superficially like failures and are not:
 *
 *  - **Abstention** (`Classification.abstained`). Four gates were checked, one did not hold,
 *    and the document was routed to a human instead of guessed at. HTTP 200. Blue, never red.
 *  - **`needs_ocr`** (HTTP 422 from the ingest path). The bytes carry no text — an image, a
 *    scanned PDF — and this deployment either has local OCR off or was told to skip it. The
 *    service did not misread anything; there was nothing to read, and it will not ship an
 *    unclassified document to somebody else's OCR to find out.
 *  - **An empty field on a blank form.** See `EmptyByDesign` in `EmptyState.tsx`.
 *
 * Only `ErrorState` — a real, red one — is for the service failing to answer.
 */
import type { ReactNode } from 'react';
import { ApiError, isNeedsOcr } from '../api';
import type { Classification } from '../types';

type Tone = 'danger' | 'warn' | 'abstain';

export interface ErrorStateProps {
  title?: string;
  /** An `ApiError`, any Error, or a plain message. */
  error?: unknown;
  /** Extra explanation. */
  body?: ReactNode;
  /** key/value rows under the message — status, media type, whatever the caller has. */
  facts?: Array<[string, ReactNode]>;
  tone?: Tone;
  action?: ReactNode;
}

function defaultTitle(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return 'the service did not answer';
    if (error.status === 401) return 'this deployment wants an API key';
    if (error.status === 404) return 'not found';
    if (error.status === 409) return 'the queue refused this decision';
    if (error.status === 422) return 'the request was rejected';
    if (error.status === 501) return 'this queue cannot do that';
    if (error.status === 503) return 'a component is not available';
    return `request failed (${error.status})`;
  }
  return 'something went wrong';
}

export function ErrorState({ title, error, body, facts, tone = 'danger', action }: ErrorStateProps) {
  const message =
    error instanceof Error ? error.message : typeof error === 'string' ? error : undefined;
  return (
    <div className={`state state-error tone-${tone}`} role={tone === 'danger' ? 'alert' : undefined}>
      <div className="state-title">{title ?? defaultTitle(error)}</div>
      {message && <div className="state-body">{message}</div>}
      {body && <div className="state-body muted">{body}</div>}
      {facts && facts.length > 0 && (
        <dl>
          {facts.map(([k, v]) => (
            <div key={k} style={{ display: 'contents' }}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {action && <div className="state-actions">{action}</div>}
    </div>
  );
}

/**
 * The `needs_ocr` 422, rendered as the honest answer it is.
 *
 * `ocr_available` is the fact that matters: `false` distinguishes "this deployment has local
 * OCR switched off (or the extra is not installed), so we chose not to read it" from
 * "we cannot". Neither is a bug, and the remedy the server supplies is the next step.
 */
export function NeedsOcrState({ error, action }: { error: ApiError; action?: ReactNode }) {
  if (!isNeedsOcr(error)) return <ErrorState error={error} />;
  const d = error.needsOcr;
  return (
    <ErrorState
      tone="warn"
      title="nothing to read in this file"
      error={undefined}
      body={
        <>
          {d.reason} This is not a failed classification: no classifier ran, because the bytes
          carry no text. An unclassified document is not sent to a cloud OCR to find out — that
          is the disclosure this service exists to prevent.
        </>
      }
      facts={[
        ['media type', <span className="mono">{d.media_type}</span>],
        ['detected by', <span className="mono">{d.detected_by || '—'}</span>],
        ['pages', d.page_count || '—'],
        [
          'local OCR',
          d.ocr_available ? 'available on this deployment' : 'not available for this request',
        ],
        ...(d.remedy ? ([['what to do', d.remedy]] as Array<[string, ReactNode]>) : []),
      ]}
      action={action}
    />
  );
}

/**
 * An abstention, rendered as a routed decision.
 *
 * This is deliberately NOT an `ErrorState` in the red sense: the tone is `abstain`, the
 * language is "declined to decide", and the reason is quoted verbatim because it names the
 * gate that did not hold. Pages should follow this with the gate meters, which show *how far*
 * off it was.
 */
export function AbstentionNotice({
  classification,
  action,
}: {
  classification: Classification;
  action?: ReactNode;
}) {
  const top = classification.runners_up[0];
  return (
    <ErrorState
      tone="abstain"
      title="declined to decide — routed to a human"
      body={
        <>
          Four gates have to hold together before a doctype is accepted. One did not, so this
          document went to the review queue instead of being labelled. Nothing downstream has
          been told a doctype.
        </>
      }
      facts={[
        ['reason', <span className="mono">{classification.reason || 'not stated'}</span>],
        ...(top
          ? ([
              [
                'closest doctype',
                <>
                  <span className="mono">{top[0]}</span>{' '}
                  <span className="faint tabular">{top[1].toFixed(3)}</span>
                </>,
              ],
            ] as Array<[string, ReactNode]>)
          : []),
        ['margin', <span className="tabular">{classification.margin.toFixed(3)}</span>],
        ['coverage', <span className="tabular">{classification.coverage.toFixed(3)}</span>],
      ]}
      action={action}
    />
  );
}
