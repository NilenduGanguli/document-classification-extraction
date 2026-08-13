/**
 * A doctype, shown the way the registry thinks about it: id, human label, category.
 *
 * `unknown` gets its own treatment — the abstain tone, not a neutral grey and never a danger
 * red. "We did not decide, a human will" is a legitimate outcome of this service, and the
 * badge is the first place a reviewer sees it.
 */
import { Link } from 'react-router-dom';
import type { Category } from '../types';

export interface DocTypeBadgeProps {
  doctypeId: string;
  label?: string;
  category?: Category;
  /** Bigger type, for the headline of a decision panel. */
  size?: 'sm' | 'lg';
  /** Link through to the registry entry. Off by default so tables stay quiet. */
  link?: boolean;
  title?: string;
}

export function DocTypeBadge({
  doctypeId,
  label,
  category = 'other',
  size = 'sm',
  link = false,
  title,
}: DocTypeBadgeProps) {
  const unknown = !doctypeId || doctypeId === 'unknown';
  const body = (
    <span
      className={`doctype cat-${category} ${unknown ? 'unknown' : ''} ${size === 'lg' ? 'lg' : ''}`}
      title={title ?? (unknown ? 'no doctype was accepted — routed to review' : doctypeId)}
    >
      <span className="lbl">{unknown ? 'unknown' : label || doctypeId}</span>
      {!unknown && label && label !== doctypeId && <span className="id">{doctypeId}</span>}
    </span>
  );
  if (!link || unknown) return body;
  return (
    <Link to={`/registry?doctype=${encodeURIComponent(doctypeId)}`} style={{ color: 'inherit' }}>
      {body}
    </Link>
  );
}
