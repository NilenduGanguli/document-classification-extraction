/**
 * An ISO-3166 alpha-2 country tag.
 *
 * The flag is composed from Unicode regional-indicator code points, so it is text — there is
 * no image, no sprite sheet and no request. Platforms that do not draw flag emoji simply show
 * the letters, which is why the code is ALWAYS rendered next to it rather than replaced by it.
 *
 * `XX` is the registry's country-agnostic marker (a doctype that is not tied to a jurisdiction)
 * and gets a globe rather than a nonsense flag.
 */

const REGIONAL_INDICATOR_A = 0x1f1e6;
const LETTER_A = 'A'.charCodeAt(0);

function flagFor(code: string): string {
  const cc = code.trim().toUpperCase();
  if (cc.length !== 2 || cc === 'XX' || !/^[A-Z]{2}$/.test(cc)) return '\u{1F310}'; // globe
  return String.fromCodePoint(
    ...[...cc].map((ch) => REGIONAL_INDICATOR_A + (ch.charCodeAt(0) - LETTER_A)),
  );
}

export interface CountryTagProps {
  /** Two-letter code. `XX` means "not jurisdiction-specific". */
  country: string;
  /** Hide the glyph and show only the code — for dense tables. */
  bare?: boolean;
}

export function CountryTag({ country, bare = false }: CountryTagProps) {
  const cc = (country || 'XX').toUpperCase();
  const label = cc === 'XX' ? 'not jurisdiction-specific' : cc;
  return (
    <span className="country" title={label}>
      {!bare && (
        <span className="flag" aria-hidden="true">
          {flagFor(cc)}
        </span>
      )}
      <span>{cc}</span>
    </span>
  );
}
