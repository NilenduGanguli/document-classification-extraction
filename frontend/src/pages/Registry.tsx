/**
 * /registry — what this service can recognise, and how.
 *
 * ## What this page is for
 * 182 doctypes across 5 jurisdictions. The naive version of this page is a searchable list, and
 * it is not enough: the reviewer's real question is not "is there an entry for a PAN card" but
 * **"what would make the classifier pick this one, and what would make it pick the one next to
 * it?"** That answer lives in the spec — `anchors` (some `decisive`), `negative_anchors`,
 * `confusable_with` and `id_patterns`. Those four are the page; the list is how you reach them.
 *
 * ## The one design decision
 * DECISIVE vs SUPPORTING is the single most important fact on a spec, so the two groups are
 * given different *shapes*, not different badges: decisive anchors are full-width rows with a
 * solid accept-coloured edge, supporting anchors are quiet inline chips. The rule behind the
 * distinction — a decisive anchor must be a string ONE ISSUER controls, never a document-class
 * name — is stated on screen, next to the anchors, every time. It is the rule that keeps the
 * registry honest, and a spec author reading this page is exactly who needs to see it.
 *
 * ## Where the sentences come from
 * Every claim this page makes about classifier behaviour was read out of the service, not
 * assumed:
 *   - decisive = "near-proof", worth a 2.0 multiplier in the concurrence score, and muted by
 *     the audibility guard when another doctype claims the same string
 *     (`dce/classify/cascade.py`);
 *   - a zone-gated anchor is only audible in that zone (`dce/classify/anchors.py`);
 *   - a negative anchor subtracts from the doctype's anchor score (`anchors.py`);
 *   - an `id_patterns` hit is only *verified* when one of the doctype's own field validators
 *     accepts it — "nine digits appeared" is not evidence (`anchors.checksum_sweep`), which is
 *     why the patterns panel cross-references the validators the fields declare.
 * No threshold is quoted: `/readyz` reports posture, not cutoffs.
 *
 * ## Data
 *   `api.listDocTypes({country, category})` — both filters are the SERVER's (exact category,
 *      case-insensitive country). The unfiltered list is fetched once as the facet index, so
 *      the counts next to each facet are real counts and not a guess.
 *   `api.getDocType(id)`  — the spec.
 *   `api.getSchema(id)`   — the active schema: `schema_version`, `source`, `active`.
 *   `api.induceSchema(…)` — a DRAFT. `active: false`, the registry did not change.
 *
 * Search is in-browser over the rows the API returned (there is no search parameter on the
 * endpoint) and says so on screen.
 *
 * ## PII
 * `FieldSpec.pii` here is a *schema* fact — the registry saying "this slot will hold a
 * person's identifier". There is no value on this page to mask, so it is badged, not masked;
 * `<PiiValue>` belongs where a value is rendered.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';

import * as api from '../api';
import type { ApiError } from '../api';
import {
  Badge,
  CountryTag,
  DocTypeBadge,
  EmptyState,
  ErrorState,
  Fact,
  JsonView,
  Loading,
  PageHead,
  Panel,
} from '../components';
import { CATEGORIES } from '../types';
import type {
  Anchor,
  Category,
  DocTypeSpec,
  DocTypeSummary,
  DocumentRequest,
  FieldSpec,
  SchemaResponse,
} from '../types';
import type { PageProps } from './contract';
import './Registry.css';

/* ------------------------------------------------------------------ copy */

/** The rule. Stated on the page because it is what a decisive anchor MEANS. */
const DECISIVE_RULE = (
  <>
    A <strong>decisive</strong> anchor is near-proof on its own: an issuing authority's name, a
    masthead, a form number — a string <strong>exactly one issuer controls</strong>. A
    document-class name is never one. Every bank on earth prints “BANK STATEMENT”, so it can
    support a decision and can never carry one. Supporting anchors have to accumulate; no single
    one of them decides.
  </>
);

const ZONE_RULE = (
  <>
    A <strong>zone-gated</strong> anchor is only audible in that zone. “PASSPORT” across the top
    of a page is the document; “passport” in a sentence halfway down is a mention of one. The
    same string outside its zone scores nothing — which is why a payload with no title/heading
    structure can mute a decisive claim entirely and send the document to a human instead.
  </>
);

const NO_DECISIVE_NOTE = (
  <>
    <strong>No decisive anchor.</strong> Nothing on this document's face is controlled by one
    issuer alone, so no single string can carry the decision: it is reachable only by
    accumulation — several supporting anchors agreeing — and it leans on its negative anchors to
    stay out of its neighbours' way. Expect it to abstain more readily than a doctype with a
    masthead.
  </>
);

/* --------------------------------------------------------------- helpers */

const CATEGORY_LABEL: Record<Category, string> = {
  identity: 'identity',
  address_proof: 'address proof',
  tax: 'tax',
  corporate: 'corporate',
  financial: 'financial',
  other: 'other',
};

const LANG_NAME: Record<string, string> = {
  en: 'English',
  hi: 'Hindi',
  es: 'Spanish',
  fr: 'French',
};

function langTitle(lang: string): string {
  return LANG_NAME[lang] ? `${LANG_NAME[lang]} (${lang})` : lang;
}

/** `/registry` never shows a value, but ids and labels still go through the same nav helper. */
function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/* ------------------------------------------------------------- the page */

export default function Registry({ readiness }: PageProps) {
  const [params, setParams] = useSearchParams();

  const q = params.get('q') ?? '';
  const country = params.get('country') ?? '';
  const rawCategory = params.get('category') ?? '';
  const category = (CATEGORIES as readonly string[]).includes(rawCategory)
    ? (rawCategory as Category)
    : '';
  const selected = params.get('doctype') ?? '';

  /** One place that writes the URL, so every filter is linkable and the back button works. */
  const patch = useCallback(
    (next: Record<string, string>, replace = false) => {
      setParams(
        (prev) => {
          const merged = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(next)) {
            if (value) merged.set(key, value);
            else merged.delete(key);
          }
          return merged;
        },
        { replace },
      );
    },
    [setParams],
  );

  /* ---- the facet index: the whole registry, fetched once, unfiltered ---- */

  const [index, setIndex] = useState<DocTypeSummary[] | null>(null);
  const [indexError, setIndexError] = useState<ApiError | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    api
      .listDocTypes({}, ctl.signal)
      .then((res) => setIndex(res.doctypes))
      .catch((err: unknown) => {
        if (!ctl.signal.aborted && !isAbortError(err)) setIndexError(api.asApiError(err));
      });
    return () => ctl.abort();
  }, []);

  /* ---- the rows on screen: the SERVER's answer for the current filters ---- */

  const filtered = Boolean(country || category);
  const [rows, setRows] = useState<DocTypeSummary[] | null>(null);
  const [rowsError, setRowsError] = useState<ApiError | null>(null);
  const [rowsLoading, setRowsLoading] = useState(false);

  useEffect(() => {
    if (filtered) return;
    setRows(index);
    setRowsError(null);
    setRowsLoading(false);
  }, [filtered, index]);

  useEffect(() => {
    if (!filtered) return;
    const ctl = new AbortController();
    setRowsLoading(true);
    api
      .listDocTypes({ country: country || undefined, category: category || undefined }, ctl.signal)
      .then((res) => {
        setRows(res.doctypes);
        setRowsError(null);
      })
      .catch((err: unknown) => {
        if (ctl.signal.aborted || isAbortError(err)) return;
        setRows([]);
        setRowsError(api.asApiError(err));
      })
      .finally(() => {
        if (!ctl.signal.aborted) setRowsLoading(false);
      });
    return () => ctl.abort();
  }, [filtered, country, category]);

  /* ---------------------------- facet counts ---------------------------- */

  /**
   * Standard faceted counting: each facet is counted against the OTHER facet's filter, so the
   * number on a button is what you would get by pressing it. Counted from the index, never from
   * the visible rows — a count that changed as you typed in the search box would be a lie about
   * the registry.
   */
  const facets = useMemo(() => {
    const all = index ?? [];
    const countries = new Map<string, number>();
    for (const d of all) {
      if (category && d.category !== category) continue;
      countries.set(d.country, (countries.get(d.country) ?? 0) + 1);
    }
    const categories = new Map<string, number>();
    for (const d of all) {
      if (country && d.country !== country) continue;
      categories.set(d.category, (categories.get(d.category) ?? 0) + 1);
    }
    // A filter you have applied must never vanish from the bar: CA + "other" is an empty
    // intersection, and dropping the CA button would leave the reader unable to see — or
    // undo — half of the filter that emptied the list.
    if (country && !countries.has(country)) countries.set(country, 0);
    return {
      countries: [...countries.entries()].sort((a, b) => a[0].localeCompare(b[0])),
      categories: CATEGORIES.map((c) => [c, categories.get(c) ?? 0] as const),
      total: all.length,
    };
  }, [index, country, category]);

  const byId = useMemo(() => {
    const map = new Map<string, DocTypeSummary>();
    for (const d of index ?? []) map.set(d.doctype_id, d);
    return map;
  }, [index]);

  /* -------------------------------- search ------------------------------- */

  const visible = useMemo(() => {
    const source = rows ?? [];
    const needle = q.trim().toLowerCase();
    if (!needle) return source;
    const terms = needle.split(/\s+/);
    return source.filter((d) => {
      const hay = `${d.doctype_id} ${d.label} ${d.issuing_authority} ${d.category} ${d.country} ${d.applies_to} ${d.fields.join(' ')}`.toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
  }, [rows, q]);

  /* -------------------------------- detail ------------------------------- */

  const [spec, setSpec] = useState<DocTypeSpec | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [specError, setSpecError] = useState<ApiError | null>(null);
  const [schemaError, setSchemaError] = useState<ApiError | null>(null);
  const [specLoading, setSpecLoading] = useState(false);
  const detailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setSpec(null);
    setSchema(null);
    setSpecError(null);
    setSchemaError(null);
    if (!selected) {
      setSpecLoading(false);
      return;
    }
    const ctl = new AbortController();
    setSpecLoading(true);
    Promise.allSettled([
      api.getDocType(selected, ctl.signal),
      api.getSchema(selected, ctl.signal),
    ]).then(([gotSpec, gotSchema]) => {
      if (ctl.signal.aborted) return;
      if (gotSpec.status === 'fulfilled') setSpec(gotSpec.value);
      else if (!isAbortError(gotSpec.reason)) setSpecError(api.asApiError(gotSpec.reason));
      if (gotSchema.status === 'fulfilled') setSchema(gotSchema.value);
      else if (!isAbortError(gotSchema.reason)) setSchemaError(api.asApiError(gotSchema.reason));
      setSpecLoading(false);
    });
    return () => ctl.abort();
  }, [selected]);

  // When the layout has stacked, selecting from the rail must not leave the reader looking at
  // an unchanged list with the answer somewhere below the fold.
  useEffect(() => {
    if (!selected || !detailRef.current) return;
    if (window.matchMedia('(min-width: 1081px)').matches) return;
    detailRef.current.scrollIntoView({ block: 'start' });
  }, [selected, spec]);

  const select = (id: string) => patch({ doctype: id });
  const clearFilters = () => patch({ country: '', category: '', q: '' });

  const registryEmpty = readiness !== null && !readiness.registry.loaded;

  /* -------------------------------- render ------------------------------- */

  return (
    <main className="page">
      <PageHead
        title="Registry"
        lede="Every document type this deployment can recognise — the anchors that identify it, the anchors that rule it out, and what it is confusable with."
        actions={
          index ? (
            <span className="muted tabular">
              {facets.total} doctypes
              {readiness ? ` · ${readiness.registry.countries.join(' ')}` : ''}
            </span>
          ) : null
        }
      />

      {registryEmpty && (
        <div style={{ marginBottom: 'var(--s-4)' }}>
          <ErrorState
            tone="warn"
            title="the registry did not load"
            body="/readyz reports registry.loaded = false. Nothing can be classified in this state; the posture page has the component detail."
          />
        </div>
      )}

      <div className="stack">
        <Panel
          title="Find a doctype"
          actions={
            <span className="faint tabular">
              {rows === null ? '—' : `${visible.length} of ${facets.total || rows.length}`}
            </span>
          }
          stack
        >
          <div className="row" style={{ gap: 'var(--s-3)' }}>
            <input
              type="search"
              value={q}
              placeholder="id, label, issuing authority, field name…"
              aria-label="search the registry"
              onChange={(e) => patch({ q: e.target.value }, true)}
              style={{ flex: '1 1 320px', minWidth: 0 }}
            />
            {(country || category || q) && (
              <button className="btn btn-sm" onClick={clearFilters}>
                clear
              </button>
            )}
          </div>

          <FacetBar
            facets={facets}
            country={country}
            category={category}
            onCountry={(c) => patch({ country: c === country ? '' : c })}
            onCategory={(c) => patch({ category: c === category ? '' : c })}
          />

          <p className="faint" style={{ fontSize: 'var(--t-xs)', margin: 0 }}>
            Country and category are the API's own filters (<span className="mono">
              GET /api/v1/doctypes
            </span>
            ); the counts are over the whole registry, so the number on a button is what pressing
            it returns. The search box filters those rows in this browser — the endpoint has no
            search parameter.
          </p>
        </Panel>

        {indexError && <ErrorState title="could not read the registry" error={indexError} />}

        <div className={`reg-layout ${selected ? 'split' : ''}`}>
          <Panel
            className={selected ? 'reg-rail-panel' : ''}
            title={selected ? 'Doctypes' : 'The registry'}
            flush={!selected}
            actions={
              rowsLoading ? <span className="faint">loading…</span> : undefined
            }
          >
            {rows === null && !indexError && <Loading label="reading the registry…" />}
            {rowsError && <ErrorState title="that filter failed" error={rowsError} />}
            {rows !== null && visible.length === 0 && !rowsError && (
              <EmptyState
                icon="▤"
                title="nothing matches"
                body={
                  <>
                    {describeFilters(q, country, category)} A registry with no{' '}
                    {country || category ? 'entry for that combination' : 'match'} is a fact about
                    this deployment, not an error — the registry is compiled into the image.
                  </>
                }
                action={
                  <button className="btn" onClick={clearFilters}>
                    clear filters
                  </button>
                }
              />
            )}
            {rows !== null &&
              visible.length > 0 &&
              (selected ? (
                <DocTypeRail rows={visible} selected={selected} onSelect={select} />
              ) : (
                <div className="scroll-x">
                  <DocTypeTable rows={visible} onSelect={select} />
                </div>
              ))}
          </Panel>

          {selected && (
            <div className="stack" ref={detailRef}>
              {specLoading && (
                <Panel>
                  <Loading label={`reading ${selected}…`} />
                </Panel>
              )}
              {!specLoading && specError && (
                <Panel title={<span className="mono">{selected}</span>}>
                  {specError.status === 404 ? (
                    <ErrorState
                      tone="warn"
                      title="not in this deployment's registry"
                      body={
                        <>
                          The service answered 404 for <span className="mono">{selected}</span>.
                          That is different from “no such document type exists”: the registry is
                          compiled into the image, so a doctype another deployment has may simply
                          not be in this one. Nothing here can classify it.
                        </>
                      }
                      facts={[['response', <span className="mono">{specError.message}</span>]]}
                      action={
                        <button className="btn" onClick={() => patch({ doctype: '' })}>
                          back to the list
                        </button>
                      }
                    />
                  ) : (
                    <ErrorState error={specError} />
                  )}
                </Panel>
              )}
              {!specLoading && spec && (
                <SpecDetail
                  spec={spec}
                  schema={schema}
                  schemaError={schemaError}
                  byId={byId}
                  onClose={() => patch({ doctype: '' })}
                />
              )}
            </div>
          )}
        </div>

        <InducePanel prefill={spec} />
      </div>
    </main>
  );
}

function describeFilters(q: string, country: string, category: string): string {
  const bits: string[] = [];
  if (q.trim()) bits.push(`search “${q.trim()}”`);
  if (country) bits.push(`country ${country}`);
  if (category) bits.push(`category ${category}`);
  return bits.length ? `No doctype matches ${bits.join(', ')}.` : 'No doctype matches.';
}

/* --------------------------------------------------------------- facets */

interface FacetData {
  countries: Array<[string, number]>;
  categories: ReadonlyArray<readonly [Category, number]>;
  total: number;
}

function FacetBar({
  facets,
  country,
  category,
  onCountry,
  onCategory,
}: {
  facets: FacetData;
  country: string;
  category: string;
  onCountry: (c: string) => void;
  onCategory: (c: Category) => void;
}) {
  return (
    <div className="reg-facets">
      <div className="reg-facet-group">
        <span className="label">country</span>
        {facets.countries.length === 0 && <span className="faint">—</span>}
        {facets.countries.map(([code, n]) => (
          <button
            key={code}
            className={`reg-facet ${country === code ? 'on' : ''}`}
            aria-pressed={country === code}
            disabled={n === 0 && country !== code}
            onClick={() => onCountry(code)}
            title={code === 'XX' ? 'not jurisdiction-specific' : code}
          >
            {code}
            <span className="n">{n}</span>
          </button>
        ))}
      </div>
      <div className="reg-facet-group">
        <span className="label">category</span>
        {facets.categories.map(([cat, n]) => (
          <button
            key={cat}
            className={`reg-facet ${category === cat ? 'on' : ''}`}
            aria-pressed={category === cat}
            disabled={n === 0 && category !== cat}
            onClick={() => onCategory(cat)}
          >
            {CATEGORY_LABEL[cat]}
            <span className="n">{n}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- lists */

function DocTypeTable({
  rows,
  onSelect,
}: {
  rows: DocTypeSummary[];
  onSelect: (id: string) => void;
}) {
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>doctype</th>
          <th>country</th>
          <th>issuing authority</th>
          <th>applies to</th>
          <th className="tabular">anchors</th>
          <th className="tabular">fields</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.doctype_id} style={{ cursor: 'pointer' }} onClick={() => onSelect(d.doctype_id)}>
            <td>
              <div className="stack" style={{ gap: '2px', alignItems: 'flex-start' }}>
                <DocTypeBadge doctypeId={d.doctype_id} label={d.label} category={d.category} />
                <span className="faint" style={{ fontSize: 'var(--t-xs)' }}>
                  {CATEGORY_LABEL[d.category]}
                  {d.officially_valid ? ' · officially valid' : ''}
                </span>
              </div>
            </td>
            <td>
              <CountryTag country={d.country} />
            </td>
            <td className="muted">{d.issuing_authority || '—'}</td>
            <td className="muted nowrap">{d.applies_to}</td>
            <td className="tabular">{d.anchors}</td>
            <td className="tabular">{d.fields.length}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DocTypeRail({
  rows,
  selected,
  onSelect,
}: {
  rows: DocTypeSummary[];
  selected: string;
  onSelect: (id: string) => void;
}) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const activeRef = useRef<HTMLButtonElement | null>(null);

  // A deep link lands on a doctype the rail has scrolled past. Showing the entry in place —
  // with its neighbours around it — is half the value of keeping the list on screen at all.
  useEffect(() => {
    const box = boxRef.current;
    const item = activeRef.current;
    if (!box || !item) return;
    const boxRect = box.getBoundingClientRect();
    const itemRect = item.getBoundingClientRect();
    if (itemRect.top >= boxRect.top && itemRect.bottom <= boxRect.bottom) return;
    box.scrollTop += itemRect.top - boxRect.top - (box.clientHeight - itemRect.height) / 2;
  }, [selected, rows]);

  return (
    <div className="reg-rail-scroll" ref={boxRef}>
      <div className="reg-rail">
        {rows.map((d) => (
          <button
            key={d.doctype_id}
            ref={d.doctype_id === selected ? activeRef : undefined}
            className={`reg-rail-item cat-${d.category} ${d.doctype_id === selected ? 'on' : ''}`}
            aria-current={d.doctype_id === selected}
            onClick={() => onSelect(d.doctype_id)}
          >
            <span className="txt">
              <span className="lbl">{d.label}</span>
              <span className="rid">{d.doctype_id}</span>
            </span>
            <span className="cc">{d.country}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- detail */

function SpecDetail({
  spec,
  schema,
  schemaError,
  byId,
  onClose,
}: {
  spec: DocTypeSpec;
  schema: SchemaResponse | null;
  schemaError: ApiError | null;
  byId: Map<string, DocTypeSummary>;
  onClose: () => void;
}) {
  const decisive = spec.anchors.filter((a) => a.decisive);
  const supporting = spec.anchors.filter((a) => !a.decisive);
  const zoned = spec.anchors.filter((a) => a.zone);
  const fields = schema?.fields.length ? schema.fields : spec.fields;
  const fieldsFromSchema = Boolean(schema?.fields.length);
  const validators = [...new Set(fields.map((f) => f.validator).filter(Boolean))] as string[];
  const confusables = Object.entries(spec.confusable_with);

  return (
    <>
      <Panel
        title={
          <span className="reg-title-row">
            <span>{spec.label}</span>
            <span className="rid">{spec.doctype_id}</span>
          </span>
        }
        actions={
          <button className="btn btn-ghost btn-sm" onClick={onClose} title="back to the full list">
            close
          </button>
        }
        stack
      >
        <div className="reg-kv">
          <Fact label="country">
            <CountryTag country={spec.country} />
          </Fact>
          <Fact label="category">
            <Badge tone="neutral">{CATEGORY_LABEL[spec.category]}</Badge>
          </Fact>
          <Fact label="applies to">{spec.applies_to}</Fact>
          <Fact label="officially valid">
            {spec.officially_valid ? (
              <Badge
                tone="accept"
                title="an officially valid document for KYC purposes on this jurisdiction's rules"
              >
                OVD
              </Badge>
            ) : (
              <span className="muted">no</span>
            )}
          </Fact>
        </div>
        <Fact label="issuing authority">{spec.issuing_authority || '—'}</Fact>

        <div className="row faint" style={{ fontSize: 'var(--t-xs)', gap: 'var(--s-3)' }}>
          {schema && (
            <>
              <span>
                schema <span className="mono">{schema.schema_version}</span>
              </span>
              <span>
                source <span className="mono">{schema.source}</span>
              </span>
              <span>{schema.active ? 'active' : 'NOT ACTIVE'}</span>
            </>
          )}
          {schemaError && (
            <span title={schemaError.message}>
              the active schema could not be read ({schemaError.message}) — the field list below
              is the registry spec
            </span>
          )}
        </div>
      </Panel>

      {spec.handling && (
        <Panel title="Handling">
          <div className="reg-note pii">{spec.handling}</div>
        </Panel>
      )}

      <Panel title="Anchors — what makes the classifier pick this doctype" stack>
        <div className="reg-note accept">{DECISIVE_RULE}</div>

        <div>
          <div className="reg-group-head">
            <h3>Decisive</h3>
            <span className="n">{decisive.length}</span>
          </div>
          {decisive.length > 0 ? (
            <ul className="reg-decisive">
              {decisive.map((a, i) => (
                <AnchorRow key={`d-${i}`} anchor={a} decisive />
              ))}
            </ul>
          ) : (
            <div className="reg-note abstain">{NO_DECISIVE_NOTE}</div>
          )}
          {decisive.length > 0 && (
            <p className="faint" style={{ fontSize: 'var(--t-xs)', marginTop: 'var(--s-2)' }}>
              Any one of these is near-proof and can carry the decision alone — unless another
              doctype in the registry claims the same string, in which case the service mutes the
              short-circuit and makes the anchors argue it out.
            </p>
          )}
        </div>

        <div>
          <div className="reg-group-head">
            <h3>Supporting</h3>
            <span className="n">{supporting.length}</span>
          </div>
          {supporting.length > 0 ? (
            <ul className="reg-supporting">
              {supporting.map((a, i) => (
                <AnchorRow key={`s-${i}`} anchor={a} />
              ))}
            </ul>
          ) : (
            <p className="faint" style={{ margin: 0 }}>
              none — this spec rests entirely on its decisive anchors.
            </p>
          )}
        </div>

        {zoned.length > 0 && <div className="reg-note">{ZONE_RULE}</div>}
      </Panel>

      <Panel title="Negative anchors — what rules this doctype out" stack>
        {spec.negative_anchors.length > 0 ? (
          <>
            <ul className="reg-negatives">
              {spec.negative_anchors.map((neg, i) => (
                <li key={`${neg}-${i}`}>{neg}</li>
              ))}
            </ul>
            <p className="faint" style={{ fontSize: 'var(--t-xs)', margin: 0 }}>
              Seeing any one of these on the page subtracts from this doctype's anchor score. They
              are how a spec stays out of its neighbours' way — usually the exact mastheads of the
              doctypes listed as confusable below.
            </p>
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            None declared. Nothing on the page can argue this doctype <em>down</em>; it is
            separated from its neighbours by its own anchors alone.
          </p>
        )}
      </Panel>

      <Panel title="Identifier patterns" stack>
        {spec.id_patterns.length > 0 ? (
          <>
            <ul className="reg-patterns">
              {spec.id_patterns.map((p) => (
                <li key={p} className="reg-pattern">
                  {p}
                </li>
              ))}
            </ul>
            <div className="reg-note">
              A pattern hit is only <strong>verified</strong> when one of this doctype's own field
              validators accepts what it found — “nine digits appeared” is not evidence, “nine
              digits that satisfy the checksum appeared” is. This doctype declares{' '}
              {validators.length > 0 ? (
                <>
                  {validators.map((v, i) => (
                    <span key={v}>
                      {i > 0 ? ', ' : ''}
                      <span className="mono">{v}</span>
                    </span>
                  ))}
                  .
                </>
              ) : (
                <>
                  <strong>no validator at all</strong>, so a match here carries a fraction of the
                  weight and can never short-circuit the cascade.
                </>
              )}
            </div>
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            None. This doctype carries no identifier the service can pattern-match and check — it
            is recognised by its anchors.
          </p>
        )}
      </Panel>

      <Panel title="Confusable with" stack>
        {confusables.length > 0 ? (
          <>
            <ul className="reg-confusables">
              {confusables.map(([otherId, why]) => {
                const other = byId.get(otherId);
                return (
                  <li key={otherId}>
                    <DocTypeBadge
                      doctypeId={otherId}
                      label={other?.label}
                      category={other?.category}
                      link
                    />
                    <span className="why">{why}</span>
                  </li>
                );
              })}
            </ul>
            <p className="faint" style={{ fontSize: 'var(--t-xs)', margin: 0 }}>
              Each sentence is the registry's own answer to “how would a human tell these apart”.
              It is also the sentence a reviewer should be able to quote when defending an
              accept — and the cluster the classifier consults before letting a decisive anchor
              short-circuit anything.
            </p>
          </>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            Nothing declared. This doctype has no near neighbour in the registry.
          </p>
        )}
      </Panel>

      <Panel
        title="Fields"
        actions={
          <span className="faint">
            {fields.length} · {fieldsFromSchema ? 'active schema' : 'registry spec'}
          </span>
        }
        stack
      >
        <FieldTable fields={fields} />
        <p className="faint" style={{ fontSize: 'var(--t-xs)', margin: 0 }}>
          <span className="mono">pii</span> here is a schema fact — the registry saying this slot
          will hold a person's identifier — not a value. Extracted values wear it as a mask.
          Locators are the strategies allowed to fill the field, in preference order.
        </p>
      </Panel>

      <Panel title="Raw" stack>
        <JsonView value={spec} title={`GET /api/v1/doctypes/${spec.doctype_id}`} collapsed />
        {schema && (
          <JsonView value={schema} title={`GET /api/v1/schemas/${spec.doctype_id}`} collapsed />
        )}
      </Panel>
    </>
  );
}

function AnchorRow({ anchor, decisive = false }: { anchor: Anchor; decisive?: boolean }) {
  return (
    <li>
      {decisive && (
        <span className="mark" aria-hidden="true">
          ◆
        </span>
      )}
      <span className="txt">{anchor.text}</span>
      <span className="reg-lang" title={langTitle(anchor.lang)}>
        {anchor.lang}
      </span>
      {anchor.zone && (
        <span
          className="reg-zone"
          title={`zone gate — this string only counts when it lands in the ${anchor.zone} zone; anywhere else it scores nothing`}
        >
          ▲ {anchor.zone} only
        </span>
      )}
    </li>
  );
}

/* --------------------------------------------------------------- fields */

function FieldTable({ fields, draft = false }: { fields: FieldSpec[]; draft?: boolean }) {
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());

  if (fields.length === 0) {
    return (
      <EmptyState
        icon="▢"
        title={draft ? 'the draft found no fields' : 'no fields declared'}
        body={
          draft
            ? 'Induction reads provider key/value pairs and table headers. Samples with neither — plain text, for instance — have nothing for it to name, so an empty draft here is the honest answer rather than a failure.'
            : 'This doctype is recognised but nothing is extracted from it.'
        }
      />
    );
  }

  const toggle = (name: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  return (
    <div className="scroll-x">
      <table className="grid">
        <thead>
          <tr>
            <th className="reg-expand" />
            <th>field</th>
            <th>type</th>
            <th>required</th>
            <th>pii</th>
            <th>validator</th>
            <th>locators</th>
            <th>pattern</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => {
            const extra = Boolean(f.notes) || Object.keys(f.labels ?? {}).length > 0;
            const isOpen = open.has(f.name);
            return (
              <FieldRows
                key={f.name}
                field={f}
                extra={extra}
                isOpen={isOpen}
                onToggle={() => toggle(f.name)}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FieldRows({
  field,
  extra,
  isOpen,
  onToggle,
}: {
  field: FieldSpec;
  extra: boolean;
  isOpen: boolean;
  onToggle: () => void;
}): ReactNode {
  return (
    <>
      <tr>
        <td className="reg-expand">
          {extra && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={onToggle}
              aria-expanded={isOpen}
              aria-label={`${isOpen ? 'hide' : 'show'} labels and notes for ${field.name}`}
            >
              {isOpen ? '▾' : '▸'}
            </button>
          )}
        </td>
        <td>
          <span className="reg-fieldname">{field.name}</span>
          {field.attribute_key ? (
            <span className="reg-attr">{field.attribute_key}</span>
          ) : (
            <span className="reg-attr">no attribute key</span>
          )}
        </td>
        <td className="muted nowrap">
          {field.type}
          {field.multi && (
            <>
              {' '}
              <Badge tone="neutral" title="this field can hold more than one value">
                multi
              </Badge>
            </>
          )}
        </td>
        <td>
          {field.required ? <Badge tone="accent">required</Badge> : <span className="faint">—</span>}
        </td>
        <td>
          {field.pii ? (
            <Badge tone="pii" title="the registry marks this slot as personally identifying; values are masked wherever they are rendered">
              pii
            </Badge>
          ) : (
            <span className="faint">—</span>
          )}
        </td>
        <td className="mono" style={{ fontSize: 'var(--t-xs)' }}>
          {field.validator || <span className="faint">none</span>}
        </td>
        <td>
          <span className="reg-locators">
            {field.locators.map((l) => (
              <span key={l} className="reg-locator">
                {l}
              </span>
            ))}
          </span>
        </td>
        <td className="reg-pat-cell">{field.pattern || <span className="faint">—</span>}</td>
      </tr>
      {extra && isOpen && (
        <tr className="reg-detail-row">
          <td />
          <td colSpan={7}>
            <div className="stack" style={{ gap: 'var(--s-2)' }}>
              {Object.entries(field.labels ?? {}).map(([lang, syns]) => (
                <div key={lang} className="reg-labels">
                  <span className="reg-lang" title={langTitle(lang)}>
                    {lang}
                  </span>
                  {syns.map((s) => (
                    <span key={s} className="syn">
                      {s}
                    </span>
                  ))}
                </div>
              ))}
              {field.notes && <div className="muted">{field.notes}</div>}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/* ------------------------------------------------------- schema induction */

type SampleKind = 'text' | 'layout' | 'azure' | 'des';

const SAMPLE_KINDS: Array<[SampleKind, string]> = [
  ['layout', 'LayoutView JSON'],
  ['azure', 'Azure analyze result JSON'],
  ['des', 'DES OCR JSON'],
  ['text', 'plain text'],
];

interface SampleDraft {
  key: number;
  kind: SampleKind;
  body: string;
}

let nextSampleKey = 1;

function newSample(kind: SampleKind = 'layout'): SampleDraft {
  return { key: nextSampleKey++, kind, body: '' };
}

/**
 * Draft a schema from samples.
 *
 * Two things this panel must not let anybody misunderstand:
 *
 *  1. **The result is a draft.** `active: false`, `source: induced`. The registry did not
 *     change, no deployment classifies differently, and nothing extracts by it — the resolver
 *     refuses an inactive schema outright.
 *  2. **Induction only names what the document named.** It reads provider key/value pairs and
 *     table column headers, keeps the candidates present in at least `min_support` of the
 *     samples, and guesses a type from the values. It does not know what is required, what is
 *     PII, or which validator applies — every one of those comes back empty, which is precisely
 *     why a human has to finish it.
 */
function InducePanel({ prefill }: { prefill: DocTypeSpec | null }) {
  const [open, setOpen] = useState(false);
  const [doctypeId, setDoctypeId] = useState('');
  const [label, setLabel] = useState('');
  const [country, setCountry] = useState('');
  const [minSupport, setMinSupport] = useState(0.5);
  const [samples, setSamples] = useState<SampleDraft[]>(() => [newSample()]);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<SchemaResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [badJson, setBadJson] = useState<Record<number, string>>({});

  const usePrefill = () => {
    if (!prefill) return;
    setDoctypeId(prefill.doctype_id);
    setLabel(prefill.label);
    setCountry(prefill.country);
  };

  const patchSample = (key: number, next: Partial<SampleDraft>) =>
    setSamples((prev) => prev.map((s) => (s.key === key ? { ...s, ...next } : s)));

  const addFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const built = await Promise.all([...files].map((f) => api.documentRequestFromFile(f)));
    setSamples((prev) => [
      ...prev.filter((s) => s.body.trim() !== ''),
      ...built.map((req, i) => ({
        key: nextSampleKey++,
        kind: 'layout' as SampleKind,
        body: JSON.stringify({ __file: files[i].name, request: req }),
      })),
    ]);
  };

  const build = (): DocumentRequest[] | null => {
    const bad: Record<number, string> = {};
    const out: DocumentRequest[] = [];
    samples.forEach((s, i) => {
      const body = s.body.trim();
      if (!body) return;
      // A file the picker turned into a DocumentRequest is carried whole.
      if (body.startsWith('{"__file"')) {
        try {
          out.push((JSON.parse(body) as { request: DocumentRequest }).request);
        } catch {
          bad[s.key] = 'this file could not be read back';
        }
        return;
      }
      if (s.kind === 'text') {
        out.push({ doc_id: `sample-${i + 1}`, text: body });
        return;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(body);
      } catch (err) {
        bad[s.key] = `not JSON: ${(err as Error).message}`;
        return;
      }
      const doc: DocumentRequest = { doc_id: `sample-${i + 1}` };
      if (s.kind === 'layout') doc.layout = parsed as DocumentRequest['layout'];
      else if (s.kind === 'azure') doc.azure_analyze_result = parsed as Record<string, unknown>;
      else doc.des_ocr = parsed as Record<string, unknown>;
      out.push(doc);
    });
    setBadJson(bad);
    if (Object.keys(bad).length > 0) return null;
    return out;
  };

  const submit = async () => {
    const built = build();
    if (!built || built.length === 0) return;
    setBusy(true);
    setError(null);
    setDraft(null);
    try {
      const res = await api.induceSchema({
        doctype_id: doctypeId.trim(),
        label: label.trim() || undefined,
        country: country.trim() || undefined,
        samples: built,
        min_support: minSupport,
      });
      setDraft(res);
    } catch (err) {
      setError(api.asApiError(err));
    } finally {
      setBusy(false);
    }
  };

  const fileCount = samples.filter((s) => s.body.startsWith('{"__file"')).length;

  return (
    <Panel
      title="Draft a schema from samples"
      actions={
        <button className="btn btn-sm" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? 'hide' : 'open'}
        </button>
      }
      stack
    >
      {!open ? (
        <p className="muted" style={{ margin: 0 }}>
          <span className="mono">POST /api/v1/schemas/induce</span> proposes field names from
          sample documents when you are adding a doctype. It always returns a{' '}
          <strong>draft</strong> — the registry does not change and nothing classifies differently
          because you ran it.
        </p>
      ) : (
        <>
          <div className="reg-note warn">
            The result is <strong>inactive</strong> (<span className="mono">active: false</span>,{' '}
            <span className="mono">source: induced</span>). Nothing in the registry changes, no
            document is classified differently, and the extractor refuses an inactive schema
            outright. Induction reads <strong>provider key/value pairs and table headers</strong>{' '}
            only — it names what the document named. It cannot know what is required, what is PII
            or which validator applies, so those come back empty and a human has to finish the
            job.
          </div>

          <div className="row" style={{ gap: 'var(--s-3)', alignItems: 'flex-end' }}>
            <label className="stack" style={{ gap: '2px' }}>
              <span className="label">doctype id</span>
              <input
                type="text"
                value={doctypeId}
                placeholder="in_new_doctype"
                onChange={(e) => setDoctypeId(e.target.value)}
              />
            </label>
            <label className="stack" style={{ gap: '2px' }}>
              <span className="label">label</span>
              <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} />
            </label>
            <label className="stack" style={{ gap: '2px' }}>
              <span className="label">country</span>
              <input
                type="text"
                value={country}
                placeholder="IN"
                size={4}
                onChange={(e) => setCountry(e.target.value)}
                style={{ width: 72 }}
              />
            </label>
            <label className="stack" style={{ gap: '2px' }}>
              <span className="label">min support</span>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={minSupport}
                onChange={(e) => setMinSupport(Number(e.target.value))}
                style={{ width: 88 }}
                title="fraction of samples a candidate field must appear in"
              />
            </label>
            {prefill && (
              <button className="btn btn-sm" onClick={usePrefill}>
                use {prefill.doctype_id}
              </button>
            )}
          </div>

          <div className="stack" style={{ gap: 'var(--s-2)' }}>
            {samples.map((s, i) => {
              const isFile = s.body.startsWith('{"__file"');
              return (
                <div key={s.key} className="reg-sample">
                  <div className="row">
                    <span className="label">sample {i + 1}</span>
                    {!isFile && (
                      <select
                        value={s.kind}
                        aria-label={`payload kind for sample ${i + 1}`}
                        onChange={(e) =>
                          patchSample(s.key, { kind: e.target.value as SampleKind })
                        }
                      >
                        {SAMPLE_KINDS.map(([value, text]) => (
                          <option key={value} value={value}>
                            {text}
                          </option>
                        ))}
                      </select>
                    )}
                    {isFile && (
                      <span className="muted mono">
                        {(JSON.parse(s.body) as { __file: string }).__file} — parsed by the service
                      </span>
                    )}
                    <span className="spacer" style={{ marginLeft: 'auto' }} />
                    {samples.length > 1 && (
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => setSamples((prev) => prev.filter((x) => x.key !== s.key))}
                      >
                        remove
                      </button>
                    )}
                  </div>
                  {!isFile && (
                    <textarea
                      value={s.body}
                      spellCheck={false}
                      placeholder={
                        s.kind === 'text'
                          ? 'plain text has no key/value pairs and no table headers — this will draft nothing'
                          : 'paste the JSON payload'
                      }
                      onChange={(e) => patchSample(s.key, { body: e.target.value })}
                    />
                  )}
                  {s.kind === 'text' && !isFile && s.body.trim() !== '' && (
                    <span className="faint" style={{ fontSize: 'var(--t-xs)' }}>
                      A plain-text sample carries no key/value pairs and no table headers, so it
                      contributes no candidate fields. It is accepted, and it will draft nothing.
                    </span>
                  )}
                  {badJson[s.key] && <span className="bad">{badJson[s.key]}</span>}
                </div>
              );
            })}
          </div>

          <div className="row">
            <button className="btn btn-sm" onClick={() => setSamples((prev) => [...prev, newSample()])}>
              add a sample
            </button>
            <label className="btn btn-sm" style={{ cursor: 'pointer' }}>
              add files
              <input
                type="file"
                multiple
                style={{ display: 'none' }}
                onChange={(e) => {
                  void addFiles(e.target.files);
                  e.target.value = '';
                }}
              />
            </label>
            {fileCount > 0 && (
              <span className="faint">
                {fileCount} file{fileCount === 1 ? '' : 's'} will be sent to this service and
                parsed in-process
              </span>
            )}
            <span style={{ marginLeft: 'auto' }} />
            <button
              className="btn btn-sm btn-primary"
              disabled={busy || !doctypeId.trim() || samples.every((s) => !s.body.trim())}
              onClick={() => void submit()}
            >
              {busy ? 'drafting…' : 'draft a schema'}
            </button>
          </div>

          {error && <ErrorState title="induction failed" error={error} />}

          {draft && (
            <div className="reg-draft">
              <div className="reg-draft-banner">
                <Badge tone="warn">DRAFT</Badge>
                <span>
                  {draft.fields.length} field{draft.fields.length === 1 ? '' : 's'} proposed for{' '}
                  <span className="mono">{draft.doctype_id}</span> from {draft.sample_count} sample
                  {draft.sample_count === 1 ? '' : 's'}
                </span>
              </div>
              <div className="reg-draft-body">
                <span className="mono">active: {String(draft.active)}</span> ·{' '}
                <span className="mono">source: {draft.source}</span> ·{' '}
                <span className="mono">version: {draft.schema_version}</span>
                <br />
                {draft.notes}
                <br />
                Nothing was written. To make any of this real, put it in the registry and deploy —
                and note that the draft marks{' '}
                {draft.fields.filter((f) => f.pii).length === 0 ? 'nothing' : 'something'} as PII
                and declares{' '}
                {draft.fields.filter((f) => f.validator).length === 0 ? 'no' : 'some'} validators.
                It does not know which of these is a person's identifier. You do.
              </div>
            </div>
          )}
          {draft && <FieldTable fields={draft.fields} draft />}
          {draft && <JsonView value={draft} title="POST /api/v1/schemas/induce" collapsed />}
        </>
      )}
    </Panel>
  );
}
