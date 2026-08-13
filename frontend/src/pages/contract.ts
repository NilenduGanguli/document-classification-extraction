/**
 * THE PAGE CONTRACT.
 *
 * Four pages are written independently against this file. It is the whole interface between
 * the shell and a page, and it is deliberately tiny: a page owns everything inside `<main>`
 * and nothing outside it.
 *
 * ## Rules every page must keep
 *
 * 1. **Default-export a component taking `PageProps`.** The shell renders it inside an error
 *    boundary at its route; do not add a second router.
 * 2. **Render exactly one `<main className="page">`** as the root element, starting with a
 *    `<PageHead>`. The shell supplies the nav, the theme, the toast host and nothing else.
 * 3. **Talk to the service only through `src/api.ts`.** No `fetch`, no new URL constants, no
 *    third-party client. Nothing may load from another origin — that is the product's whole
 *    argument, and a font or a CDN script in one page breaks it for the whole console.
 * 4. **Use `src/components` for anything shared.** If you need a new shared thing, add it to
 *    the barrel. Four private "confidence bars" is the failure this contract prevents.
 * 5. **Handle the honest states.** Every page that calls the API must handle, distinctly:
 *      - a loading state (`<Loading/>`);
 *      - a real failure (`<ErrorState/>`, red);
 *      - `isNeedsOcr(err)` → `<NeedsOcrState/>` (warn, never red — nothing was misread);
 *      - `isAbstention(classification)` → `<AbstentionNotice/>` (blue, never red — this is
 *        the service working as designed);
 *      - empty-by-design (`<EmptyState/>` / `<EmptyByDesign/>`), which is often good news.
 * 6. **Mask PII.** Any value from a field with `pii: true` goes through `<PiiValue pii/>`.
 *    Never log one, never put one in a URL, never send one anywhere.
 * 7. **Own your own URL state.** Use `useSearchParams` for filters and selections so a
 *    reviewer can paste a link to exactly what they are looking at — an audit trail that
 *    cannot be linked to is half an audit trail. Do not put a PII value in a query string.
 * 8. **Never claim a threshold you were not given.** `/readyz` reports posture, not the
 *    classifier's cutoffs. `<Meter>` renders honestly without one.
 */
import type { ReadinessResponse } from '../types';

/** What every page receives. */
export interface PageProps {
  /**
   * The last successful `GET /readyz`, refreshed by the shell every 30s, or `null` when the
   * service has not answered yet (or at all).
   *
   * Pages may read it for context — `registry.doctypes`, `registry.countries`, `tiers`,
   * `egress`, `degraded` — but must not block on it: `null` means "not known yet", not
   * "broken". Never re-poll it yourself; the shell owns that.
   */
  readiness: ReadinessResponse | null;
}

/** The posture page also gets a way to force the shell's readiness poll. */
export interface PosturePageProps extends PageProps {
  /** Re-fetch `/readyz` now and update the whole shell, including the header pill. */
  onRefresh: () => void;
}
