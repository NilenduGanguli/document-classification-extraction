/**
 * One piece of evidence: which tier said it, what it saw, and how much it counted for.
 *
 * `Classification.evidence` is always populated — an unexplainable classification is not
 * auditable, and this is a KYC system. The chip keeps the tier visually pinned to the left so
 * a column of them reads as a ledger rather than as prose.
 */
import type { Evidence } from '../types';

export interface EvidenceChipProps {
  evidence: Evidence;
  /** Mark a decisive anchor / the piece that carried the decision. */
  decisive?: boolean;
  /** Evidence that argued against (a negative anchor, a confusable). Renders in the warn tone. */
  against?: boolean;
  title?: string;
}

const fmtWeight = (w: number): string => (w > 0 ? `+${w.toFixed(2)}` : w.toFixed(2));

export function EvidenceChip({ evidence, decisive, against, title }: EvidenceChipProps) {
  const tone = decisive ? 'decisive' : against || evidence.weight < 0 ? 'against' : '';
  return (
    <span
      className={`evidence ${tone}`}
      title={title ?? `${evidence.tier}: ${evidence.detail}`}
    >
      <span className="tier">{evidence.tier}</span>
      <span className="detail">{evidence.detail}</span>
      {evidence.weight !== 0 && <span className="weight tabular">{fmtWeight(evidence.weight)}</span>}
    </span>
  );
}

/** A column of evidence chips. Empty renders nothing — the caller owns the empty state. */
export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return null;
  return (
    <div className="stack" style={{ gap: 'var(--s-2)', alignItems: 'flex-start' }}>
      {evidence.map((e, i) => (
        <EvidenceChip key={`${e.tier}-${i}`} evidence={e} />
      ))}
    </div>
  );
}
