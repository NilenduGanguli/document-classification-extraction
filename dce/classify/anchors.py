"""L1 — anchors and checksums. The tier that is allowed to be nearly certain.

Two kinds of evidence live here, and they are the two strongest signals in the service:

**Anchors.** The strings an issuer prints on every copy of a document: ``INSTITUTO NACIONAL
ELECTORAL``, ``WAGE AND TAX STATEMENT``, ``CANADA BUSINESS CORPORATIONS ACT``. A
*decisive* anchor is near-proof of the doctype on its own.

**Checksums.** An identifier that matches its registry pattern *and* passes its check digit is
the strongest thing this system can observe. Nobody accidentally writes a Luhn-valid
Canadian SIN, or a TD3 machine-readable zone whose four check digits agree.

The one rule that matters more than any scoring detail:

    **Anchors are matched on tokens, never with ``needle in haystack``.**

The substring implementation this service replaces fired ``DL`` inside "mi**dl**e", ``EIN``
inside "b**ein**g", ``SIN`` inside "u**sin**g", ``SAT`` inside "**Sat**urday" and ``USA``
inside "**usa**ndo"/"ca**usa**". Every one of those is a KYC misclassification caused by a
three-character token and a substring test. Matching is therefore:

1. contiguous **token** n-gram match on the folded form (accents intact) — full credit;
2. the same on the skeleton form (accents stripped, OCR confusions folded) — near-full credit,
   this is what makes Spanish/Portuguese anchors survive accent-blind OCR;
3. ``rapidfuzz.partial_ratio >= 90`` against the skeleton — partial credit, and **only** for
   anchors of at least 8 characters, because a fuzzy match on ``SAT`` is exactly the bug.

Zone restrictions are honoured (an anchor declared ``zone=title`` only counts in the title),
and a match in a heavy zone is worth more than the same match in page furniture.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from typing import Any

from rapidfuzz import fuzz

from dce.config import Settings, get_settings
from dce.models import Anchor, DocTypeSpec, Evidence, LayoutView, Zone
from dce.normalize import NormalizedText, normalize

__all__ = [
    "AnchorHit",
    "AnchorOutcome",
    "ChecksumHit",
    "anchor_scores",
    "checksum_sweep",
    "find_td3",
]

#: A fuzzy match is only allowed for anchors this long. Short tokens ("DL", "EIN", "SIN",
#: "SAT", "USA", "CURP") are matched exactly or not at all — fuzzing them is the original bug.
_FUZZY_MIN_CHARS = 8
#: rapidfuzz partial_ratio floor for an OCR-damaged header.
_FUZZY_THRESHOLD = 90.0

#: Anchors at or below this length additionally require **case** evidence: the document must
#: contain the anchor as an all-caps token. Token matching alone is not enough for them,
#: because a three-letter anchor is frequently an ordinary word in one of the languages the
#: registry covers — Spanish ``sin`` ("without") is the ``SIN`` anchor, French ``des`` and
#: Portuguese ``ele`` collide the same way. An issuer prints ``SIN`` in caps on the card and
#: prose does not, so case is the discriminator, and it is free: we still have the raw text.
_CASE_SENSITIVE_MAX_CHARS = 4

#: Credit multiplier per match strategy.
_QUALITY = {"token": 1.0, "skeleton": 0.95, "fuzzy": 0.8}

#: Zones searched in descending weight order, so an anchor is credited to the heaviest zone
#: it appears in.
_ZONE_ORDER: tuple[Zone, ...] = (Zone.title, Zone.heading, Zone.table, Zone.body, Zone.furniture)

#: Raw-score contributions. A verified check digit is worth more than any single anchor; a
#: merely well-shaped identifier is worth about one good anchor; a pattern hit that nothing
#: accepted is worth almost nothing, because "nine digits appeared" is not evidence.
#:
#: These are the weights of a **corroborated** identifier — one the document either labels or
#: backs with an anchor of the same doctype. An uncorroborated one, however clean its check
#: digit, is scored at ``_CHECKSUM_UNVERIFIED_WEIGHT`` with the bare pattern hits. The raw
#: score is read as evidence in bits (the squash is ``1 - 2^-raw``), and the bits an
#: unlabelled Luhn-valid nine-digit number carries about *what document this is* are close to
#: zero: about one in ten arbitrary nine-digit strings passes Luhn, and ordinary bank
#: statements, invoices and tax forms are full of nine-digit strings.
_CHECKSUM_VERIFIED_WEIGHT = 3.0
_FORMAT_VALID_WEIGHT = 1.0
_CHECKSUM_UNVERIFIED_WEIGHT = 0.25
_MAX_VERIFIED_HITS = 2
_MAX_FORMAT_HITS = 3
_MAX_UNVERIFIED_HITS = 4
_DECISIVE_MULTIPLIER = 2.0
_NEGATIVE_ANCHOR_PENALTY = 1.0
#: Never report certainty from this tier alone.
_SCORE_CEILING = 0.97


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AnchorHit:
    """One anchor of one doctype, found in the document.

    Attributes:
        doctype_id: Owning doctype.
        text: The anchor as declared in the registry.
        zone: Heaviest zone the anchor was found in.
        decisive: Whether the registry marked this anchor as near-proof.
        how: ``"token"`` | ``"skeleton"`` | ``"fuzzy"``.
        weight: Raw-score contribution of this hit.
    """

    doctype_id: str
    text: str
    zone: Zone
    decisive: bool
    how: str
    weight: float


@dataclass(frozen=True)
class ChecksumHit:
    """An identifier that matched a registry pattern, and what validation said about it.

    The distinction between ``level="checksum"`` and ``level="format"`` is the whole value of
    this class. A published check digit is proof; a structural pattern is a shape. A US SSN
    has no check digit at all and an EIN can only have its campus prefix checked — so
    ``123-45-6789`` is a *shape*, and only a Luhn-valid SIN, a RENAPO-consistent CURP or a
    four-check-digit MRZ is *proof*. Only ``verified`` hits can make the cascade skip the
    lexical tier.

    Attributes:
        doctype_id: Owning doctype.
        value: The matched identifier, as found on the page.
        pattern: The registry pattern that found it.
        validator: Name of the validator that accepted it (empty when nothing accepted it).
        level: ``"checksum"`` | ``"format"`` | ``"none"``.
        verified: ``True`` only for a clean, checksum-grade acceptance.
        corroborated: Whether the document *labels* this identifier — one of the declared
            labels for the field that owns the validator appears on the identifier's own line
            or the line above it.

            This is the difference between evidence about a **number** and evidence about a
            **document**, and it is not a nicety. A Canadian SIN is nine digits with a Luhn
            check digit; roughly one in ten arbitrary nine-digit strings passes Luhn, and a
            bank statement, an invoice or a tax form is full of nine-digit strings. So "a
            Luhn-valid nine-digit number appeared" carries about 3.3 bits about *that number*
            and close to zero bits about *what document this is* — the likelihood ratio
            ``P(some 9-digit Luhn-valid number | SIN letter) / P(same | any other document)``
            is near 1. "A Luhn-valid nine-digit number appeared **next to the words Social
            Insurance Number**" is a different observation entirely. A machine-readable zone
            is self-labelling (the ``P<`` record structure plus four agreeing check digits)
            and is therefore always corroborated.
    """

    doctype_id: str
    value: str
    pattern: str
    validator: str = ""
    level: str = "none"
    verified: bool = False
    corroborated: bool = False


@dataclass(frozen=True)
class AnchorOutcome:
    """Everything L1 learned.

    Attributes:
        scores: ``doctype_id -> [0, 0.97]`` squashed anchor+checksum score.
        bits: ``doctype_id -> raw evidence in bits``, the quantity ``scores`` is the squash
            **of**: ``score = min(0.97, 1 - 2^-bits)``. Anchor specificity, match quality, the
            zone multiplier, the decisive multiplier and the checksum weights all *add* here,
            which is what makes this the additive channel and ``scores`` the saturating one.

            **Anything that COMPARES two doctypes must read this, not** ``scores``. The squash
            is monotone, so it preserves the *ranking* — but it does not preserve *differences*
            and it is clipped: every doctype holding 5.06 bits or more reports exactly 0.97.
            Two doctypes at 5.1 and 9.4 bits are 4.3 bits apart and 0.0000 apart on ``scores``.
            A difference taken on ``scores`` therefore measures the ceiling rather than the
            evidence, and it *shrinks as the evidence for both grows* — which is exactly how
            zone weighting, which multiplies bits for the winner and the rival alike, came to
            reduce the measured separation between them. The ceiling itself is deliberate and
            stays: it caps the *certainty* this tier may report on its own. A cap on reported
            certainty is not a scale for comparing candidates.
        hits: Anchor hits per doctype.
        checksums: Checksum hits per doctype (verified and not).
        evidence: Human-readable evidence per doctype.
        coverage: Fraction of a doctype's declared anchors that were observed. Used by the
            cascade's short-circuit, which never pays for the lexical tier and therefore has
            no profile coverage to report.
        muted_decisive: Decisive anchors this payload was unable to *evaluate*, per doctype.
            An entry means: the anchor is declared ``zone=X``, the document carries no zone
            ``X`` at all, and the anchor's text is nevertheless present somewhere in the
            document. That is a claim we failed to hear, not a claim that failed — and the
            difference matters, because a text-layer PDF has no title zone, so a doctype whose
            decisive anchors are all title-gated is silent on this route while an identically
            confusable doctype that gated nothing is heard. See
            :func:`dce.classify.cascade._decisive_short_circuit`.
    """

    scores: Mapping[str, float] = field(default_factory=dict)
    bits: Mapping[str, float] = field(default_factory=dict)
    hits: Mapping[str, tuple[AnchorHit, ...]] = field(default_factory=dict)
    checksums: Mapping[str, tuple[ChecksumHit, ...]] = field(default_factory=dict)
    evidence: Mapping[str, tuple[Evidence, ...]] = field(default_factory=dict)
    coverage: Mapping[str, float] = field(default_factory=dict)
    muted_decisive: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def verified_doctypes(self) -> tuple[str, ...]:
        """Doctypes holding at least one checksum-verified **and corroborated** identifier.

        Corroborated means the document said what the number is: either the identifier sits
        next to one of its declared labels (:attr:`ChecksumHit.corroborated`), or the doctype
        independently matched one of its own anchors. A check digit on an unlabelled number
        found on a document that shows no other sign of being that doctype identifies a
        number, not a document — see :class:`ChecksumHit`.
        """
        return tuple(
            doctype
            for doctype, hits in self.checksums.items()
            if any(h.verified and (h.corroborated or self.hits.get(doctype)) for h in hits)
        )

    def decisive_doctypes(self) -> tuple[str, ...]:
        """Doctypes with at least one decisive anchor hit."""
        return tuple(
            doctype
            for doctype, hits in self.hits.items()
            if any(h.decisive for h in hits)
        )


# ---------------------------------------------------------------------------
# Zone index
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _ZoneIndex:
    """Normalised text per zone, plus one combined view used as a cheap pre-filter."""

    per_zone: Mapping[Zone, NormalizedText]
    combined: NormalizedText
    raw_upper: str
    #: Tokens that appear ALL-CAPS in the document, case preserved. Gates short anchors.
    caps_tokens: frozenset[str]


#: Case-preserving tokenisation, mirroring ``dce.models.tokenize`` without the lower-casing.
_CASED_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _build_zone_index(view: LayoutView) -> _ZoneIndex:
    """Group the payload's text by zone and normalise each group once."""
    buckets: dict[Zone, list[str]] = {zone: [] for zone in Zone}
    for block in view.blocks:
        buckets[block.zone].append(block.text)
    for table in view.tables:
        buckets[Zone.table].extend(c.text for c in table.cells if c.text)
    for kv in view.key_values:
        # A provider key/value pair is label-like text; treat it as body so a stray key
        # cannot outrank a real printed heading.
        buckets[Zone.body].append(f"{kv.key} {kv.value}")

    per_zone = {zone: normalize("\n".join(texts)) for zone, texts in buckets.items()}
    raw = "\n".join("\n".join(t) for t in buckets.values())
    combined = normalize(raw)
    caps = frozenset(
        token for token in _CASED_WORD_RE.findall(raw) if token.isupper() and len(token) > 1
    )
    return _ZoneIndex(
        per_zone=per_zone,
        combined=combined,
        raw_upper=view.text().upper(),
        caps_tokens=caps,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _contains_sequence(
    haystack: Sequence[str], needle: Sequence[str], present: Mapping[str, int]
) -> bool:
    """Whether ``needle`` appears as a contiguous run of tokens inside ``haystack``.

    ``present`` is the haystack's token-count mapping, used as an O(1) rejection test before
    the scan. This matters: a request scores every anchor of every doctype against the whole
    document, and the overwhelming majority of those anchors are simply absent. Rejecting them
    on a dict lookup instead of a list walk is the difference between a linear and a quadratic
    request.
    """
    if not needle or len(needle) > len(haystack):
        return False
    if any(token not in present for token in needle):
        return False
    if len(needle) == 1:
        return True
    first = needle[0]
    span = len(needle)
    target = tuple(needle)
    for i, token in enumerate(haystack):
        if token == first and tuple(haystack[i : i + span]) == target:
            return True
    return False


def _match_in(
    text: NormalizedText, anchor: NormalizedText, *, decisive: bool = False
) -> str | None:
    """Return the strategy that matched ``anchor`` inside ``text``, or ``None``.

    Args:
        text: Normalised haystack.
        anchor: Normalised anchor.
        decisive: When True, fuzzy matching is refused (see body).

    Returns:
        ``"token"``, ``"skeleton"``, ``"fuzzy"`` or ``None``.
    """
    if text.is_empty or anchor.is_empty:
        return None
    if _contains_sequence(text.tokens, anchor.tokens, text.token_counts):
        return "token"
    if _contains_sequence(text.skeleton_tokens, anchor.skeleton_tokens, text.skeleton_counts):
        return "skeleton"
    if decisive:
        # A decisive anchor is a near-proof claim, so it must be SEEN, not approximated.
        # partial_ratio finds the best-aligned substring, which makes it actively wrong
        # here: it scored "Form W-2" against a document reading "Form W-9" (one character
        # apart, and the entire distinction between two different IRS forms), and matched
        # "KYC Identification Number" inside "Taxpayer Identification Number". Both fired
        # a decisive short-circuit for the wrong doctype. Form numbers and issuing headers
        # are exactly the strings where a single character carries the meaning, so fuzzy
        # matching is denied to them. Non-decisive anchors keep it: there, an OCR-damaged
        # header contributing partial credit to a score is useful and harmless.
        return None
    if len(anchor.skeleton) >= _FUZZY_MIN_CHARS:
        # score_cutoff lets rapidfuzz abandon a candidate window as soon as it cannot reach
        # the threshold, which is most windows of most documents.
        score = fuzz.partial_ratio(
            anchor.skeleton, text.skeleton, score_cutoff=_FUZZY_THRESHOLD
        )
        if score >= _FUZZY_THRESHOLD:
            return "fuzzy"
    return None


def anchor_specificity(anchor_text: str) -> float:
    """Weight an anchor by how discriminating it is.

    A three-word issuing-authority header is proof; a two-letter token is a coincidence
    waiting to happen. Same shape as the prior art, kept because it is well calibrated.

    Args:
        anchor_text: The anchor as declared.

    Returns:
        A multiplier in ``[0.15, 1.0]``.
    """
    stripped = anchor_text.strip()
    words = len(stripped.split())
    if words >= 3 or len(stripped) >= 18:
        return 1.0
    if words == 2 or len(stripped) >= 10:
        return 0.6
    if len(stripped) >= 5:
        return 0.35
    return 0.15


def _zone_multiplier(zone: Zone, settings: Settings) -> float:
    """Scale a hit by the zone it was found in, relative to body text."""
    weights = {
        Zone.title: settings.zone_weight_title,
        Zone.heading: settings.zone_weight_heading,
        Zone.body: settings.zone_weight_body,
        Zone.table: settings.zone_weight_table,
        Zone.furniture: settings.zone_weight_furniture,
    }
    body = settings.zone_weight_body or 1.0
    return max(0.4, min(2.0, weights.get(zone, body) / body))


def _case_confirmed(anchor_text: str, index: _ZoneIndex) -> bool:
    """Whether a short anchor also appears ALL-CAPS in the document.

    Short anchors are ordinary words in some language: ``sin`` (es, "without"), ``de``, ``des``,
    ``sat``. Requiring the issuer's printed capitalisation for anchors of four characters or
    fewer removes that entire false-positive class at the cost of nothing real — an issuer
    prints ``SIN``/``DL``/``EIN`` in caps, and these anchors are the weakest in the system
    anyway (specificity 0.15), so a lower-cased OCR dump loses a rounding error, not a
    doctype.
    """
    tokens = [t for t in _CASED_WORD_RE.findall(anchor_text) if t]
    if not tokens:
        return False
    return all(token.upper() in index.caps_tokens for token in tokens)


def _locate(
    anchor: Anchor, index: _ZoneIndex, *, zone_blind: bool = False
) -> tuple[Zone, str] | None:
    """Find the heaviest zone in which ``anchor`` matches, honouring its zone restriction.

    Args:
        anchor: The declared anchor.
        index: The payload's per-zone text.
        zone_blind: Ignore ``anchor.zone``, matching anywhere in the document. Used only by
            the cascade's zone-free second reading, where the question is "is this string in
            the document at all" — a fact about the document — rather than "did the layout
            provider put it where the registry says it lives", which is a fact about the
            provider. Silencing a gated anchor there would answer neither: the zone-free
            reading exists to be *independent* of the labels, and refusing to hear a gated
            anchor is as label-dependent as crediting one, in the opposite direction.
    """
    if len(anchor.text.strip()) <= _CASE_SENSITIVE_MAX_CHARS and not _case_confirmed(
        anchor.text, index
    ):
        return None
    normalized_anchor = normalize(anchor.text)
    if anchor.zone is not None and not zone_blind:
        how = _match_in(
            index.per_zone[anchor.zone], normalized_anchor, decisive=anchor.decisive
        )
        return (anchor.zone, how) if how else None

    # Cheap pre-filter: if it is nowhere in the document, skip the per-zone sweep.
    if _match_in(index.combined, normalized_anchor, decisive=anchor.decisive) is None:
        return None
    for zone in _ZONE_ORDER:
        how = _match_in(index.per_zone[zone], normalized_anchor, decisive=anchor.decisive)
        if how:
            return zone, how
    return None


def _is_muted(anchor: Anchor, index: _ZoneIndex) -> bool:
    """Whether a decisive anchor was *unmeasurable* on this payload rather than absent.

    True when the anchor is gated to a zone the document does not have at all, and the anchor's
    text is present somewhere else in the document. Both halves are required:

    * If the declared zone exists and the anchor is not in it, the anchor genuinely did not
      match — the registry said "only in the title" and the title says otherwise. That is
      evidence, and it must keep counting as evidence.
    * If the text appears nowhere in the document, there is nothing to be uncertain about.

    Only the gap between the two is a muted claim, and it is real: no adapter on the text-layer
    route emits a ``title`` zone (``from_plain_text`` labels every block ``body``, honestly, on
    the grounds that a guessed title would be amplified 3x). So five registry doctypes whose
    decisive anchors are *all* title-gated are structurally inaudible on that route — while
    their ungated confusable peers are heard, and win uncontested. That asymmetry produced a US
    driver licence classified as a Canadian one.

    Ordered so the cheap test runs first: most anchors are ungated, and of the gated ones most
    are on payloads that do have the zone.
    """
    if not anchor.decisive or anchor.zone is None:
        return False
    if not index.per_zone[anchor.zone].is_empty:
        return False
    if len(anchor.text.strip()) <= _CASE_SENSITIVE_MAX_CHARS and not _case_confirmed(
        anchor.text, index
    ):
        return False
    return _match_in(index.combined, normalize(anchor.text), decisive=True) is not None


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _validator_module() -> Any | None:
    """Import ``dce.extract.validate`` if it exists, else ``None``.

    The extraction agent owns that module. This tier degrades to the built-in fallbacks below
    rather than failing to import, so classification never depends on extraction being wired
    up yet.
    """
    try:
        from dce.extract import validate as module
    except Exception:  # noqa: BLE001 - any import failure means "not available"; the built-in
        # fallbacks below cover the checksums, and classification must not fail because the
        # extraction package is half-installed or mid-refactor.
        return None
    return module


def _module_level(module: Any, name: str) -> str:
    """Ask the validator module how strong ``name`` is: checksum, format, or unknown."""
    grade = getattr(module, "verification_level", None)
    if callable(grade):
        try:
            return str(grade(name))
        except Exception:  # noqa: BLE001 - a third-party grading function that raises is a
            # function we cannot trust; treat the name as unknown rather than as valid.
            return "none"
    checksum_names = getattr(module, "CHECKSUM_VALIDATORS", frozenset())
    known = getattr(module, "VALIDATORS", {})
    if name in checksum_names:
        return "checksum"
    return "format" if name in known else "none"


def _module_accepts(module: Any, name: str, value: str) -> tuple[bool, bool]:
    """Run the module's validator.

    Returns:
        ``(accepted, clean)`` — ``clean`` is False when the validator accepted the value but
        flagged a soft error, which is exactly the case that must **not** count as
        checksum-verified.
    """
    dispatch = getattr(module, "validate", None)
    if not callable(dispatch):
        return (False, False)
    try:
        result = dispatch(name, value)
    except Exception:  # noqa: BLE001 - a validator that raises has not accepted the value;
        # an OCR artefact must never be able to crash classification.
        return (False, False)
    ok = bool(getattr(result, "ok", result))
    error = str(getattr(result, "error", "") or "")
    return (ok, ok and not error)


def check_identifier(name: str, value: str) -> tuple[bool, str]:
    """Validate ``value`` under validator ``name``.

    Resolution order is deliberate:

    1. :mod:`dce.extract.validate` when it is importable **and knows the name**. Its
       ``validate()`` treats an unknown name as a *soft pass* — sensible for extraction, where
       a typo must not delete data, but catastrophic here: it would make every unrecognised
       validator name report a verified identifier. So the name is checked against the
       module's own registry first, via ``verification_level``.
    2. The built-in fallbacks below, so the classifier still verifies check digits before the
       extraction module has landed.

    Args:
        name: Validator name from a :class:`~dce.models.FieldSpec`.
        value: Candidate identifier as found on the page.

    Returns:
        ``(accepted, level)`` where level is ``"checksum"``, ``"format"`` or ``"none"``.
        ``accepted`` is only True at ``"checksum"`` level for a clean check-digit pass.
    """
    module = _validator_module()
    if module is not None:
        level = _module_level(module, name)
        if level in ("checksum", "format"):
            accepted, clean = _module_accepts(module, name, value)
            if level == "checksum":
                return (clean, "checksum")
            return (accepted, "format")

    canonical = _FALLBACK_ALIASES.get(name.lower(), name.lower())
    fallback = _FALLBACK_VALIDATORS.get(canonical)
    if fallback is None:
        return (False, "none")
    try:
        accepted = bool(fallback(value))
    except Exception:  # noqa: BLE001 - same rule as above: a raise is a rejection, not a fault.
        accepted = False
    return (accepted, _FALLBACK_LEVELS.get(canonical, "format"))


# -- built-in fallbacks ------------------------------------------------------
# A deliberately small, pure set covering the identifiers whose check digits actually decide a
# doctype, named to match ``dce.extract.validate`` so it is a drop-in. That module always wins
# when it is present; these exist so the classifier is never silently downgraded to "pattern
# matched, unverified" because another module has not landed yet.
#
# The split between checksum-grade and format-grade is copied from reality, not from
# convenience: SIN (Luhn), CURP and the MRZ publish a check digit; SSN, EIN and ITIN do
# not, and pretending otherwise would reject genuine documents.
#: RENAPO check-digit alphabet (index == value).
_CURP_ALPHABET = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"
#: IRS campus prefixes that were never issued.
_INVALID_EIN_PREFIXES = frozenset(
    {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "78", "79", "89"}
)
_RFC_RE = re.compile(r"^[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{2}[0-9A]$")
_MRZ_CHECK_WEIGHTS = (7, 3, 1)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def luhn(value: str) -> bool:
    """Luhn check over a digit string (Canadian SIN's scheme)."""
    digits = _digits(value)
    if len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def sin_luhn(value: str) -> bool:
    """Canadian SIN: 9 digits, Luhn-valid, first digit not 8 (never issued)."""
    digits = _digits(value)
    return len(digits) == 9 and digits[0] != "8" and luhn(digits)


def ssn(value: str) -> bool:
    """US SSN structural validity (the SSA publishes no check digit)."""
    digits = _digits(value)
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in ("000", "666") or area[0] == "9":
        return False
    return group != "00" and serial != "0000"


def ein(value: str) -> bool:
    """US EIN: 9 digits whose 2-digit campus prefix was actually issued."""
    digits = _digits(value)
    return len(digits) == 9 and digits[:2] not in _INVALID_EIN_PREFIXES


def curp(value: str) -> bool:
    """Mexican CURP: 18 characters with a consistent RENAPO check digit."""
    candidate = value.strip().upper()
    if len(candidate) != 18:
        return False
    try:
        total = sum(_CURP_ALPHABET.index(candidate[i]) * (18 - i) for i in range(17))
    except ValueError:
        return False
    return candidate[-1] == str((10 - (total % 10)) % 10)


def rfc(value: str) -> bool:
    """Mexican RFC: structure only in the fallback — the homoclave is unreliable via OCR."""
    return bool(_RFC_RE.match(value.strip().upper()))


def _mrz_char_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    return 0  # '<' filler and anything unexpected


def _mrz_check_digit(chunk: str) -> int:
    """ICAO 9303 7-3-1 weighted modulus-10 check digit."""
    return (
        sum(
            _mrz_char_value(ch) * _MRZ_CHECK_WEIGHTS[i % 3] for i, ch in enumerate(chunk)
        )
        % 10
    )


def mrz_td3(value: str) -> bool:
    """Validate a two-line TD3 machine-readable zone.

    Args:
        value: The two 44-character lines, newline-separated (as :func:`find_td3` returns).

    Returns:
        ``True`` when the document-number, birth-date, expiry-date **and** composite check
        digits all agree. Four independent check digits agreeing is not something OCR noise
        or a coincidence produces.
    """
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if len(lines) != 2 or any(len(line) != 44 for line in lines):
        return False
    line2 = lines[1]
    if not line2[9].isdigit() or not line2[19].isdigit():
        return False
    if not line2[27].isdigit() or not line2[43].isdigit():
        return False
    if _mrz_check_digit(line2[0:9]) != int(line2[9]):
        return False
    if _mrz_check_digit(line2[13:19]) != int(line2[19]):
        return False
    if _mrz_check_digit(line2[21:27]) != int(line2[27]):
        return False
    composite = line2[0:10] + line2[13:20] + line2[21:43]
    return _mrz_check_digit(composite) == int(line2[43])


_FALLBACK_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "sin_luhn": sin_luhn,
    "ssn": ssn,
    "ein": ein,
    "curp": curp,
    "rfc": rfc,
    "mrz_td3": mrz_td3,
}
#: Which fallbacks carry a genuine published check digit. Everything else is shape only.
_FALLBACK_LEVELS: dict[str, str] = {
    "sin_luhn": "checksum",
    "curp": "checksum",
    "mrz_td3": "checksum",
    "ssn": "format",
    "ein": "format",
    "rfc": "format",
}
_FALLBACK_ALIASES: dict[str, str] = {
    "us_ssn": "ssn",
    "us_ein": "ein",
    "ca_sin": "sin_luhn",
    "sin": "sin_luhn",
    "luhn": "sin_luhn",
    "mx_curp": "curp",
    "mx_rfc": "rfc",
    "mrz": "mrz_td3",
    "passport_mrz": "mrz_td3",
    "td3": "mrz_td3",
}

_TD3_LINE_RE = re.compile(r"[A-Z0-9<]{44}")
_TD3_PREFIX_RE = re.compile(r"^P[A-Z<][A-Z<]{3}")


def find_td3(text: str) -> str | None:
    """Locate a passport TD3 zone in an OCR dump.

    Args:
        text: Document text with line structure intact.

    Returns:
        The two lines joined by a newline, or ``None``. Spaces are removed before matching
        because OCR frequently sprinkles them through the filler characters.
    """
    candidates: list[str] = []
    # OCR renders the MRZ filler '<' as a guillemet often enough to be worth folding back.
    unfolded = text.upper().replace("«", "<").replace("‹", "<")  # noqa: RUF001 - deliberate
    for line in unfolded.splitlines():
        squeezed = line.replace(" ", "")
        window = _TD3_LINE_RE.search(squeezed)
        if window is not None:
            candidates.append(window.group(0))
    for first, second in pairwise(candidates):
        if _TD3_PREFIX_RE.match(first):
            return f"{first}\n{second}"
    return None


def _spec_validators(spec: DocTypeSpec) -> tuple[str, ...]:
    """Validator names this doctype declares across its fields."""
    return tuple(dict.fromkeys(f.validator for f in spec.fields if f.validator))


def _validator_labels(spec: DocTypeSpec) -> dict[str, tuple[str, ...]]:
    """``validator name -> the labels the registry says appear next to that identifier``.

    Every language the field declares, flattened: an issuer prints ``Numéro d'assurance
    sociale`` or ``Social Insurance Number`` and either one labels the same number.
    """
    out: dict[str, list[str]] = {}
    for field_spec in spec.fields:
        if not field_spec.validator:
            continue
        bucket = out.setdefault(field_spec.validator, [])
        for labels in field_spec.labels.values():
            bucket.extend(labels)
    return {name: tuple(dict.fromkeys(labels)) for name, labels in out.items()}


def _line_window(text: str, start: int, end: int) -> str:
    """The line containing ``text[start:end]``, plus the line above it.

    A label and its value are printed on one line ("Social Insurance Number: 046 454 286") or
    stacked, label above value — that is the whole layout vocabulary of a form field, and it
    is why the window is defined in *lines* rather than in a tuned character count. There is
    no constant here to calibrate.
    """
    line_start = text.rfind("\n", 0, start) + 1
    previous_start = text.rfind("\n", 0, max(line_start - 1, 0)) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[previous_start:line_end]


def _is_label_anchored(window: str, labels: Sequence[str]) -> bool:
    """Whether any declared label for this identifier appears in ``window``.

    Matched with the same token machinery as anchors, never with ``in`` — the substring test
    is the bug this module exists to have removed, and it is no less a bug when the haystack
    is one line long.
    """
    if not labels:
        return False
    normalized_window = normalize(window)
    return any(_match_in(normalized_window, normalize(label)) is not None for label in labels)


def _wants_mrz(spec: DocTypeSpec) -> bool:
    """Whether this doctype declares a machine-readable zone."""
    if any("P<" in p or "MRZ" in p.upper() for p in spec.id_patterns):
        return True
    return any(
        (f.validator or "").startswith("mrz") or "mrz" in f.locators for f in spec.fields
    )


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str] | None:
    """Compile a registry pattern, tolerating a malformed one."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def checksum_sweep(
    text: str, specs: Iterable[DocTypeSpec]
) -> dict[str, tuple[ChecksumHit, ...]]:
    """Run every doctype's ``id_patterns`` over ``text`` and validate what they find.

    A pattern hit is only *verified* when one of the doctype's declared validators accepts it.
    A doctype that declares patterns but no validators can still produce hits, but they carry
    a fraction of the weight and can never trigger the cascade's short-circuit — "nine digits
    appeared" is not evidence, "nine digits that satisfy Luhn appeared" is.

    Args:
        text: Document text with line structure intact (the MRZ sweep needs the lines).
        specs: The doctype registry (or a subset).

    Returns:
        Mapping of ``doctype_id`` to its hits. Doctypes with no hits are omitted.
    """
    upper = text.upper()
    td3 = find_td3(text)
    out: dict[str, tuple[ChecksumHit, ...]] = {}

    for spec in specs:
        hits: list[ChecksumHit] = []
        seen: set[str] = set()
        validators = _spec_validators(spec)
        labels_by_validator = _validator_labels(spec)

        if td3 is not None and _wants_mrz(spec):
            accepted, level = check_identifier("mrz_td3", td3)
            hits.append(
                ChecksumHit(
                    doctype_id=spec.doctype_id,
                    value=td3.splitlines()[1][0:9].replace("<", ""),
                    pattern="TD3",
                    validator="mrz_td3" if accepted else "",
                    level=level if accepted else "none",
                    verified=accepted and level == "checksum",
                    # A TD3 zone is self-labelling: the ``P<`` record structure IS the label,
                    # and four independent check digits agreeing over it is not a coincidence
                    # a nine-digit Luhn pass is comparable to.
                    corroborated=True,
                )
            )
            seen.add("TD3")

        for pattern in spec.id_patterns:
            compiled = _compiled(pattern)
            if compiled is None:
                continue
            for match in compiled.finditer(upper):
                candidate = (match.group(1) if match.groups() else match.group(0)).strip()
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                window = _line_window(upper, match.start(), match.end())
                hits.append(
                    _best_validation(
                        spec.doctype_id,
                        candidate,
                        pattern,
                        validators,
                        labels_by_validator,
                        window,
                    )
                )
        if hits:
            out[spec.doctype_id] = tuple(hits)
    return out


def _best_validation(
    doctype_id: str,
    candidate: str,
    pattern: str,
    validators: Sequence[str],
    labels_by_validator: Mapping[str, tuple[str, ...]] | None = None,
    window: str = "",
) -> ChecksumHit:
    """Validate one candidate against a doctype's validators, keeping the strongest verdict.

    A doctype may declare several validators across its fields; a candidate is credited with
    the best one that accepted it, and a checksum-grade acceptance always beats a
    format-grade one.

    ``corroborated`` is decided per accepting validator, against that validator's own declared
    labels: an unlabelled number is not evidence about a document type however clean its check
    digit is (see :class:`ChecksumHit`).
    """
    labels = labels_by_validator or {}
    best = ChecksumHit(
        doctype_id=doctype_id, value=candidate, pattern=pattern, level="none", verified=False
    )
    for name in validators:
        accepted, level = check_identifier(name, candidate)
        if not accepted:
            continue
        anchored = _is_label_anchored(window, labels.get(name, ()))
        if level == "checksum":
            return ChecksumHit(
                doctype_id=doctype_id,
                value=candidate,
                pattern=pattern,
                validator=name,
                level="checksum",
                verified=True,
                corroborated=anchored,
            )
        if best.level == "none":
            best = ChecksumHit(
                doctype_id=doctype_id,
                value=candidate,
                pattern=pattern,
                validator=name,
                level=level,
                verified=False,
                corroborated=anchored,
            )
    return best


# ---------------------------------------------------------------------------
# Tier entry point
# ---------------------------------------------------------------------------
def anchor_scores(
    view: LayoutView,
    specs: Iterable[DocTypeSpec],
    *,
    settings: Settings | None = None,
    zone_blind: bool = False,
) -> AnchorOutcome:
    """Score every doctype on anchor and checksum evidence.

    Args:
        view: The layout payload.
        specs: The doctype registry (or a subset).
        settings: Settings override; defaults to :func:`dce.config.get_settings`.
        zone_blind: Evaluate every anchor as if it declared no zone restriction. For the
            cascade's zone-free second reading only — see :func:`_locate`. Nothing is ever
            recorded as ``muted_decisive`` in this mode, because nothing was gated.

    Returns:
        An :class:`AnchorOutcome`. Scores are squashed into ``[0, 0.97]`` so the fusion sees a
        bounded quantity: an avalanche of weak anchors must never out-shout a check digit.
    """
    resolved = settings if settings is not None else get_settings()
    spec_list = list(specs)
    index = _build_zone_index(view)
    checksums = checksum_sweep(index.raw_upper, spec_list)

    scores: dict[str, float] = {}
    bits: dict[str, float] = {}
    hits_by_type: dict[str, tuple[AnchorHit, ...]] = {}
    evidence: dict[str, tuple[Evidence, ...]] = {}
    coverage: dict[str, float] = {}
    muted: dict[str, tuple[str, ...]] = {}

    for spec in spec_list:
        raw = 0.0
        hits: list[AnchorHit] = []
        notes: list[Evidence] = []
        unheard: list[str] = []

        for anchor in spec.anchors:
            located = _locate(anchor, index, zone_blind=zone_blind)
            if located is None:
                if not zone_blind and _is_muted(anchor, index):
                    unheard.append(anchor.text)
                    notes.append(
                        Evidence(
                            tier="anchor",
                            detail=(
                                f"decisive anchor {anchor.text!r} is present in the document "
                                f"but is declared {anchor.zone.value}-only, and this payload "
                                f"has no {anchor.zone.value} zone — the claim could not be "
                                "evaluated, so it scores nothing and is recorded as contested"
                            ),
                            weight=0.0,
                        )
                    )
                continue
            zone, how = located
            weight = (
                anchor_specificity(anchor.text)
                * _QUALITY[how]
                * _zone_multiplier(zone, resolved)
            )
            if anchor.decisive:
                weight *= _DECISIVE_MULTIPLIER
            raw += weight
            hits.append(
                AnchorHit(
                    doctype_id=spec.doctype_id,
                    text=anchor.text,
                    zone=zone,
                    decisive=anchor.decisive,
                    how=how,
                    weight=round(weight, 4),
                )
            )
            notes.append(
                Evidence(
                    tier="anchor",
                    detail=(
                        f"{'decisive ' if anchor.decisive else ''}anchor {anchor.text!r} "
                        f"matched in {zone.value} via {how}"
                    ),
                    weight=round(weight, 4),
                )
            )

        for negative in spec.negative_anchors:
            if _match_in(index.combined, normalize(negative)) in ("token", "skeleton"):
                raw -= _NEGATIVE_ANCHOR_PENALTY
                notes.append(
                    Evidence(
                        tier="anchor",
                        detail=f"negative anchor {negative!r} present",
                        weight=-_NEGATIVE_ANCHOR_PENALTY,
                    )
                )

        spec_checksums = checksums.get(spec.doctype_id, ())
        # An identifier is corroborated when the document labels it, OR when the doctype
        # independently matched one of its own anchors — either way the document has said
        # what it is, and the check digit is then evidence about THIS DOCUMENT rather than
        # about a nine-digit number that happened to satisfy Luhn. Uncorroborated hits fall
        # all the way to the "a number of the right shape appeared" weight; there is no new
        # constant, because that is exactly what an uncorroborated identifier is worth.
        self_identified = bool(hits)

        def corroborated(hit: ChecksumHit, identified: bool = self_identified) -> bool:
            return hit.corroborated or identified

        verified = [h for h in spec_checksums if h.verified and corroborated(h)]
        formatted = [
            h for h in spec_checksums if h.level == "format" and corroborated(h)
        ]
        uncorroborated = [
            h for h in spec_checksums if h.level != "none" and not corroborated(h)
        ]
        unverified = [h for h in spec_checksums if h.level == "none"]
        verified = verified[:_MAX_VERIFIED_HITS]
        formatted = formatted[:_MAX_FORMAT_HITS]
        weak = (uncorroborated + unverified)[:_MAX_UNVERIFIED_HITS]
        raw += _CHECKSUM_VERIFIED_WEIGHT * len(verified)
        raw += _FORMAT_VALID_WEIGHT * len(formatted)
        raw += _CHECKSUM_UNVERIFIED_WEIGHT * len(weak)
        for hit in verified:
            notes.append(
                Evidence(
                    tier="checksum",
                    detail=f"{hit.validator} check digit verified {_redact(hit.value)}",
                    weight=_CHECKSUM_VERIFIED_WEIGHT,
                )
            )
        for hit in formatted:
            notes.append(
                Evidence(
                    tier="checksum",
                    detail=(
                        f"{hit.validator} accepted the shape of {_redact(hit.value)} "
                        "(no published check digit — not proof)"
                    ),
                    weight=_FORMAT_VALID_WEIGHT,
                )
            )
        for hit in uncorroborated[:_MAX_UNVERIFIED_HITS]:
            notes.append(
                Evidence(
                    tier="checksum",
                    detail=(
                        f"{hit.validator} accepted {_redact(hit.value)}, but the document "
                        "neither labels that number nor matches any anchor of this doctype — "
                        "an unlabelled identifier is evidence about a number, not about a "
                        "document type, so it scores as a bare pattern hit"
                    ),
                    weight=_CHECKSUM_UNVERIFIED_WEIGHT,
                )
            )
        for hit in unverified[:_MAX_UNVERIFIED_HITS]:
            notes.append(
                Evidence(
                    tier="checksum",
                    detail=f"pattern {hit.pattern!r} matched but did not validate",
                    weight=_CHECKSUM_UNVERIFIED_WEIGHT,
                )
            )

        # Negative anchors can drive ``raw`` below zero; "less than no evidence" is not a
        # thing, so the channel floors at zero — as it always has, implicitly, through the
        # ``if raw > 0`` below. Made explicit because ``bits`` is now read directly.
        bits[spec.doctype_id] = round(max(raw, 0.0), 6)
        scores[spec.doctype_id] = (
            round(min(_SCORE_CEILING, 1.0 - 0.5**raw), 4) if raw > 0 else 0.0
        )
        coverage[spec.doctype_id] = (
            round(len(hits) / len(spec.anchors), 4) if spec.anchors else 0.0
        )
        if hits:
            hits_by_type[spec.doctype_id] = tuple(hits)
        if notes:
            evidence[spec.doctype_id] = tuple(notes)
        if unheard:
            muted[spec.doctype_id] = tuple(unheard)

    return AnchorOutcome(
        scores=scores,
        bits=bits,
        hits=hits_by_type,
        checksums=checksums,
        evidence=evidence,
        coverage=coverage,
        muted_decisive=muted,
    )


def _redact(value: str) -> str:
    """Mask an identifier for evidence text — this is a KYC audit trail, not a data dump."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * (len(value) - 4)}{value[-4:]}"
