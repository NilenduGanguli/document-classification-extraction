/** Layout primitives. Small on purpose — four people are writing pages against these. */
import type { ReactNode } from 'react';

export interface PanelProps {
  title?: ReactNode;
  /** Pushed to the right of the title bar: filters, buttons, counts. */
  actions?: ReactNode;
  children: ReactNode;
  /** Drop the inner padding when the body is a full-bleed table. */
  flush?: boolean;
  /**
   * Lay the body out as a vertical stack with the standard gap. Use it whenever the panel has
   * more than one child — it is the difference between four pages that space things the same
   * way and four pages that each pick a margin.
   */
  stack?: boolean;
  className?: string;
}

export function Panel({
  title,
  actions,
  children,
  flush = false,
  stack = false,
  className = '',
}: PanelProps) {
  const body = [flush ? '' : 'panel-body', stack ? 'stack' : ''].filter(Boolean).join(' ');
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          {actions && (
            <>
              <span className="spacer" />
              {actions}
            </>
          )}
        </header>
      )}
      <div className={body}>{children}</div>
    </section>
  );
}

/** A page header: title, one-line lede, optional right-hand controls. */
export function PageHead({
  title,
  lede,
  actions,
}: {
  title: ReactNode;
  lede?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-head">
      <div className="row" style={{ alignItems: 'flex-start' }}>
        <h1>{title}</h1>
        {actions && (
          <>
            <span style={{ marginLeft: 'auto' }} />
            {actions}
          </>
        )}
      </div>
      {lede && <p className="lede">{lede}</p>}
    </header>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row" style={{ gap: 'var(--s-2)' }}>
      <span className="spinner" aria-hidden="true" />
      {label && <span className="muted">{label}</span>}
    </span>
  );
}

/** Full-width loading placeholder, for a panel body that has nothing yet. */
export function Loading({ label = 'working…' }: { label?: string }) {
  return (
    <div className="state">
      <Spinner label={label} />
    </div>
  );
}

export type BadgeTone =
  | 'neutral'
  | 'accept'
  | 'abstain'
  | 'warn'
  | 'danger'
  | 'cost'
  | 'pii'
  | 'accent';

/** The one badge. Tone carries the meaning — see the colour rules in `theme.css`. */
export function Badge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

/** A labelled value in a definition-list-ish row. */
export function Fact({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="stack" style={{ gap: '2px' }}>
      <span className="label">{label}</span>
      <span>{children}</span>
    </div>
  );
}
