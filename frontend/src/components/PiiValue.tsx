/**
 * A value that may be personally identifying.
 *
 * `ExtractedField.pii` and `ReviewItem.pii` are set by the registry, not guessed here. When
 * they are set the value ships MASKED and revealing it is an act the reviewer takes — a click
 * that is visible to whoever is standing behind them. A console that renders a stranger's
 * Aadhaar number by default has quietly turned every reviewer's screen into a disclosure.
 *
 * Masking keeps the last few characters, which is what makes a value checkable against a
 * source document without putting the whole identifier on screen.
 *
 * **Except where the tail is the disclosure.** A fixed trailing window is the right shape for a
 * long opaque identifier — the last 4 of an Aadhaar or a card number narrow nothing on their own.
 * It is the wrong shape for a value whose *end* is its most identifying part:
 *
 *   ``14/03/1990`` masked to a 4-character tail is ``••••••1990`` — the birth year in full,
 *   with no reveal click, from a field the console is claiming to have masked.
 *
 * So a date is masked whole. The rule is: reveal a tail only when the tail is the least
 * informative part of the value, and when it is not, reveal nothing. A masking scheme that
 * discloses the discriminating half while hiding the harmless half is worse than no masking,
 * because it is displayed under a label that says the value is protected.
 */
import { useState } from 'react';

export interface PiiValueProps {
  value?: string | null;
  /** False renders the value plainly — most fields are not PII. */
  pii?: boolean;
  /** Trailing characters left visible when masked. Ignored for date-shaped values. */
  tail?: number;
  /** What to show when there is no value at all. */
  placeholder?: string;
}

/** Values whose trailing characters are the identifying part, so no tail may be shown. */
const _DATE_LIKE =
  /^\s*(\d{1,4}[-/. ]\d{1,2}[-/. ]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{2,4})\s*$/;

function mask(value: string, tail: number): string {
  const trimmed = value.trim();
  // A year is the whole disclosure in a date of birth; never leave it showing.
  if (_DATE_LIKE.test(trimmed)) return '•'.repeat(Math.min(Math.max(trimmed.length, 6), 12));
  if (trimmed.length <= tail) return '•'.repeat(Math.max(trimmed.length, 4));
  return '•'.repeat(Math.min(trimmed.length - tail, 12)) + trimmed.slice(-tail);
}

export function PiiValue({ value, pii = false, tail = 4, placeholder = '—' }: PiiValueProps) {
  const [revealed, setRevealed] = useState(false);
  if (value === null || value === undefined || value === '') {
    return <span className="faint">{placeholder}</span>;
  }
  if (!pii) return <span className="mono">{value}</span>;
  return (
    <span className="pii-value">
      {revealed ? (
        <span className="revealed">{value}</span>
      ) : (
        <span className="masked" title="masked — this field is marked PII in the registry">
          {mask(value, tail)}
        </span>
      )}
      <button
        className="btn btn-ghost btn-sm"
        onClick={() => setRevealed((r) => !r)}
        aria-label={revealed ? 'hide this value' : 'reveal this value'}
      >
        {revealed ? 'hide' : 'reveal'}
      </button>
    </span>
  );
}
