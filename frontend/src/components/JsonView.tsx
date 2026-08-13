/**
 * A JSON / code viewer with a copy button.
 *
 * Highlighting tokenises the *serialised* string into React elements — never into an HTML
 * string, and never through `dangerouslySetInnerHTML`. That is not fussiness: the values in
 * this viewer are OCR'd text from an unclassified document supplied by whoever sent it, which
 * is the textbook definition of untrusted input. React escapes text children, so a document
 * containing `<img onerror=…>` renders as those characters.
 *
 * No highlighter library, no worker, nothing fetched — consistent with the rest of the bundle.
 *
 * This is the console's escape hatch: whatever a panel summarises, the raw response is one
 * click away. In an audit, "the UI said" is worth less than "the response said".
 */
import { Fragment, useMemo, useState, type ReactNode } from 'react';

const TOKEN =
  /("(?:\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g;

function classFor(match: string): string {
  if (match.startsWith('"')) return /:\s*$/.test(match) ? 'tok-key' : 'tok-str';
  if (match === 'true' || match === 'false') return 'tok-bool';
  if (match === 'null') return 'tok-null';
  return 'tok-num';
}

/** Split the source into plain text and classified spans. Text children stay text. */
function tokenise(json: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const m of json.matchAll(TOKEN)) {
    const at = m.index ?? 0;
    if (at > last) out.push(<Fragment key={key++}>{json.slice(last, at)}</Fragment>);
    out.push(
      <span key={key++} className={classFor(m[0])}>
        {m[0]}
      </span>,
    );
    last = at + m[0].length;
  }
  if (last < json.length) out.push(<Fragment key={key++}>{json.slice(last)}</Fragment>);
  return out;
}

export interface JsonViewProps {
  /** Any JSON-serialisable value, or a pre-formatted string (rendered verbatim). */
  value: unknown;
  /** Shown on the left of the header bar. */
  title?: string;
  /** Max height of the scroll area. Defaults to 420px. */
  maxHeight?: number | string;
  /** Start collapsed; the header stays, the body is hidden until asked for. */
  collapsed?: boolean;
  /** Turn highlighting off — for Prometheus text or anything that is not JSON. */
  plain?: boolean;
}

const toCss = (v: number | string): string => (typeof v === 'number' ? `${v}px` : v);

export function JsonView({
  value,
  title = 'response',
  maxHeight = 420,
  collapsed = false,
  plain = false,
}: JsonViewProps) {
  const [open, setOpen] = useState(!collapsed);
  const [copied, setCopied] = useState(false);

  const text = useMemo(
    () => (typeof value === 'string' ? value : JSON.stringify(value, null, 2)),
    [value],
  );
  // Highlighting a megabyte of OCR is not worth the frame; over the cap it renders plain.
  const tooBig = text.length > 250_000;
  const nodes = useMemo(
    () => (plain || tooBig ? null : tokenise(text)),
    [text, plain, tooBig],
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked; the text is selectable anyway */
    }
  };

  return (
    <div className="jsonview">
      <div className="jsonview-head">
        <span className="mono">{title}</span>
        <span className="faint">{text.length.toLocaleString()} chars</span>
        <span className="spacer" />
        <button className="btn btn-ghost btn-sm" onClick={() => setOpen((o) => !o)}>
          {open ? 'hide' : 'show'}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={copy}>
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      {open && (
        <pre style={{ ['--json-max' as string]: toCss(maxHeight) }}>{nodes ?? text}</pre>
      )}
    </div>
  );
}
