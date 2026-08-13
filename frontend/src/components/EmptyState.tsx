/**
 * An empty state.
 *
 * Half the empty states in this console are *good news* — a review queue with nothing in it, a
 * blank form with no values to pull, a deployment with no cost-bearing tier enabled. So this
 * component has no built-in tone: it says what is not there and, when it matters, why that is
 * fine. Do not decorate it with a warning.
 */
import type { ReactNode } from 'react';

export interface EmptyStateProps {
  /** One line: what is not here. */
  title: string;
  /** Optional second line: why that is (or is not) expected, and what to do next. */
  body?: ReactNode;
  /** A single glyph. Keep it quiet. */
  icon?: string;
  /** A button or link. */
  action?: ReactNode;
}

export function EmptyState({ title, body, icon = '—', action }: EmptyStateProps) {
  return (
    <div className="state">
      <div className="state-icon" aria-hidden="true">
        {icon}
      </div>
      <div className="state-title">{title}</div>
      {body && <div className="state-body muted">{body}</div>}
      {action && <div className="state-actions">{action}</div>}
    </div>
  );
}

/**
 * The specific empty state for a form that legitimately has no values in it.
 *
 * A blank application form is not a failed extraction: the fields exist in the schema, the
 * document simply has nothing written in them. Saying so explicitly stops a reviewer from
 * hunting for a bug that is not there.
 */
export function EmptyByDesign({ fieldCount }: { fieldCount: number }) {
  return (
    <EmptyState
      icon="▢"
      title="no values on this document"
      body={
        <>
          All {fieldCount} schema {fieldCount === 1 ? 'field is' : 'fields are'} empty. On a blank
          or unfilled form that is the correct result, not a failed extraction — the fields are
          defined, the document just has nothing written in them.
        </>
      }
    />
  );
}
