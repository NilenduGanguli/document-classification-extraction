/**
 * The gate meter — the single most important component in this console.
 *
 * DCE accepts a doctype only when FOUR gates hold together: evidence (the winner's absolute
 * score), margin over the runner-up, coverage of the doctype's vocabulary, and support. When
 * one does not hold the service abstains and the document goes to a human.
 *
 * A bare number cannot express that. A meter without its threshold is a number without a
 * decision, so `Meter` draws the threshold as a tick on the track and colours the fill by
 * whether the value cleared it. A reviewer should be able to point at the tick and say "this
 * is what would have had to change".
 *
 * THRESHOLDS ARE NOT ON THE WIRE. `/readyz` reports posture, not the classifier's cutoffs, so
 * `threshold` is optional everywhere: pass it when you know it (from the deployment's
 * `CLASSIFY_MIN_*` settings, or parsed out of `Classification.reason`), and the meter renders
 * honestly as "no gate shown" when you do not. Never invent one.
 */
import type { Classification } from '../types';

export interface MeterProps {
  /** Short gate name: `evidence`, `margin`, `coverage`, `support`. */
  name: string;
  /** 0..1. Values outside are clamped for the bar but shown verbatim. */
  value: number;
  /** The cutoff this value had to clear. Omit when it is genuinely unknown. */
  threshold?: number;
  /** One line under the meter — the place for "what would have changed this". */
  note?: string;
  /** Override the pass/fail derivation (e.g. the server said which gate failed). */
  status?: 'pass' | 'fail' | 'unknown';
  /** Formatter for the right-hand readout. Defaults to 2dp. */
  format?: (value: number) => string;
}

const clamp01 = (n: number): number => (n < 0 ? 0 : n > 1 ? 1 : n);
const fmt2 = (n: number): string => n.toFixed(2);

/** Percent formatter, for coverage-like gates that read better as a share. */
export const asPercent = (n: number): string => `${Math.round(clamp01(n) * 100)}%`;

export function Meter({ name, value, threshold, note, status, format = fmt2 }: MeterProps) {
  const derived: 'pass' | 'fail' | 'unknown' =
    status ?? (threshold === undefined ? 'unknown' : value >= threshold ? 'pass' : 'fail');
  const pct = clamp01(value) * 100;

  return (
    <div className={`meter ${derived}`}>
      <div className="meter-name">{name}</div>
      <div
        className="meter-track"
        role="meter"
        aria-label={name}
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuetext={
          threshold === undefined
            ? `${format(value)}, no gate shown`
            : `${format(value)} against a gate of ${format(threshold)} — ${derived}`
        }
      >
        <div className="meter-fill" style={{ width: `${pct}%` }} />
        {threshold !== undefined && (
          <div
            className="meter-threshold"
            style={{ left: `${clamp01(threshold) * 100}%` }}
            title={`gate: ${format(threshold)}`}
          />
        )}
      </div>
      <div className="meter-value tabular">{format(value)}</div>
      {note && <div className="meter-note">{note}</div>}
    </div>
  );
}

/** The cutoffs a deployment runs with. All optional; pass what you actually know. */
export interface GateThresholds {
  /** Not a configured floor any more — see `dce/config.py`. Usually left undefined. */
  confidence?: number;
  /** `CLASSIFY_MIN_MARGIN`, default 0.04. */
  margin?: number;
  /** `CLASSIFY_MIN_COVERAGE`, default 0.20. */
  coverage?: number;
  /** `CLASSIFY_MIN_SUPPORT`, default 0.30. */
  support?: number;
}

export interface GateMetersProps {
  classification: Classification;
  thresholds?: GateThresholds;
  /**
   * The support gate's value. NOT carried on `Classification`, so pass it only if you have
   * genuinely obtained it (it is sometimes quoted inside `reason`). Omitted = not rendered,
   * which is the honest default.
   */
  support?: number;
}

/**
 * The gate panel: every gate the decision turned on, in the order the cascade checks them.
 *
 * Renders `confidence`, `margin` and `coverage` from the response, plus `support` when the
 * caller supplies it. A gate named in `Classification.reason` is forced to `fail` even when a
 * threshold was not supplied, because the server already told us it did not hold.
 */
export function GateMeters({ classification, thresholds = {}, support }: GateMetersProps) {
  const reason = classification.reason.toLowerCase();
  const failed = (gate: string): 'fail' | undefined =>
    classification.abstained && reason.includes(gate) ? 'fail' : undefined;

  return (
    <div className="gates">
      <Meter
        name="evidence"
        value={classification.confidence}
        threshold={thresholds.confidence}
        status={failed('probab') ?? failed('evidence') ?? failed('confidence')}
        note="the winner's own score, on an absolute [0, 1] channel"
      />
      <Meter
        name="margin"
        value={classification.margin}
        threshold={thresholds.margin}
        status={failed('margin')}
        note={
          // On an ABSTENTION `runners_up[0]` is the DECLINED CANDIDATE ITSELF — the service is
          // saying "this came closest and still did not clear the gates", not "this is what the
          // winner beat". Naming it here renders "lead over in_form60" on the very panel that
          // explains why `in_form60` was refused. Only an accept has a true runner-up.
          classification.abstained
            ? 'lead the closest doctype held over the next one — and it still did not clear'
            : classification.runners_up.length
              ? `lead over ${classification.runners_up[0][0]}`
              : 'lead over the runner-up — nothing else scored'
        }
      />
      <Meter
        name="coverage"
        value={classification.coverage}
        threshold={thresholds.coverage}
        status={failed('coverage')}
        format={asPercent}
        note="how much of this doctype's vocabulary the document actually contained"
      />
      {support !== undefined && (
        <Meter
          name="support"
          value={support}
          threshold={thresholds.support}
          status={failed('support')}
          format={asPercent}
          note="how much of the document the winning doctype accounts for"
        />
      )}
    </div>
  );
}
