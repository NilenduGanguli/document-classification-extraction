"""HTML and email (EML) — formats whose structure is stated in tags and headers.

Both carry real zone signal and it is exactly the signal the classifier weights: an
``<h1>`` is a title in the same sense Azure's ``paragraphs[].role == "title"`` is, and a
mail ``Subject`` is the one line of an email that names what the message is. Throwing that
away and sending a flat string was the shape of defect class 3.
"""
from __future__ import annotations

import email
import email.message
import email.policy
from html.parser import HTMLParser

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.models import Zone

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
#: Elements whose content is never document text.
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "canvas", "template", "iframe", "object"}
)

#: Elements that set the zone of the text inside them. ``<h1>`` and ``<title>`` are the only
#: promotions to :attr:`Zone.title`: in HTML those two *are* the document's stated title,
#: which is the same standing Azure's ``title`` role has. ``h2``-``h6`` are section headings.
#: ``header``/``footer``/``nav`` are the page furniture that repeats on every page of a site,
#: which is precisely what :attr:`Zone.furniture` is for.
_ZONE_TAGS: dict[str, Zone] = {
    "title": Zone.title,
    "h1": Zone.title,
    "h2": Zone.heading,
    "h3": Zone.heading,
    "h4": Zone.heading,
    "h5": Zone.heading,
    "h6": Zone.heading,
    "header": Zone.furniture,
    "footer": Zone.furniture,
    "nav": Zone.furniture,
}

#: Elements that end the current run of text.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "li", "br", "hr", "section", "article", "aside", "main", "blockquote",
        "pre", "dt", "dd", "figcaption", "address", "form", "fieldset", "legend", "label",
        "option", "h1", "h2", "h3", "h4", "h5", "h6", "title", "header", "footer", "nav",
        "ul", "ol", "dl", "body", "html",
    }
)

_VOID_TAGS = frozenset(
    {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source"}
)


class _HtmlReader(HTMLParser):
    """Streaming HTML -> blocks and tables.

    Written against :mod:`html.parser` rather than a third-party parser on purpose: it is
    stdlib, it never executes anything, and adding an HTML library to a service whose base
    dependency list is deliberately six packages long would need a better reason than
    tolerating slightly worse tag soup.
    """

    def __init__(
        self, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._builder = builder
        self._limits = limits
        self._deadline = deadline
        self._buffer: list[str] = []
        self._zones: list[Zone] = []
        self._skip_depth = 0
        #: Stack of in-progress tables: each is ``(rows, current_row, header_rows)``.
        self._tables: list[tuple[list[list[str]], list[str], list[int]]] = []
        self._ticks = 0

    # -- helpers ------------------------------------------------------------
    @property
    def _zone(self) -> Zone:
        return self._zones[-1] if self._zones else Zone.body

    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % 512 == 0:
            self._deadline.check("html")

    def _flush(self) -> None:
        text = "".join(self._buffer)
        self._buffer.clear()
        if not text.strip():
            return
        if self._tables:
            self._tables[-1][1].append(text)
            return
        self._builder.block(text, zone=self._zone, page=1)

    # -- HTMLParser hooks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tick()
        if self._skip_depth:
            if tag in _SKIP_TAGS and tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self._flush()
            self._skip_depth = 1
            return
        if tag in _BLOCK_TAGS or tag in {"table", "tr", "td", "th"}:
            self._flush()
        if tag == "table":
            if len(self._tables) < 16:            # nesting past this is a broken page
                self._tables.append(([], [], [0]))
            return
        if tag == "tr" and self._tables:
            rows, current, _ = self._tables[-1]
            if current:
                rows.append(list(current))
                current.clear()
            return
        if tag == "th" and self._tables:
            rows, _, header = self._tables[-1]
            if not rows:
                header[0] = 1
            return
        zone = _ZONE_TAGS.get(tag)
        if zone is not None:
            self._zones.append(zone)

    def handle_endtag(self, tag: str) -> None:
        self._tick()
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS or tag in {"table", "tr", "td", "th"}:
            self._flush()
        if tag == "table" and self._tables:
            rows, current, header = self._tables.pop()
            if current:
                rows.append(list(current))
            if rows:
                self._builder.table(rows, page=1, header_rows=header[0])
            return
        if tag in _ZONE_TAGS and self._zones and self._zones[-1] is _ZONE_TAGS[tag]:
            self._zones.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._skip_depth:
            self._flush()

    def handle_data(self, data: str) -> None:
        self._tick()
        if self._skip_depth or not data:
            return
        if self._builder.full:
            return
        self._buffer.append(data)

    def close(self) -> None:
        super().close()
        while self._tables:
            rows, current, header = self._tables.pop()
            if current:
                rows.append(list(current))
            if rows:
                self._builder.table(rows, page=1, header_rows=header[0])
        self._flush()


def parse_html(
    text: str, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> None:
    """Parse HTML into zoned blocks and tables."""
    deadline.check("html")
    builder.page(1)
    reader = _HtmlReader(builder, limits, deadline)
    reader.feed(text)
    reader.close()


# ---------------------------------------------------------------------------
# EML
# ---------------------------------------------------------------------------
#: Headers worth keeping, in the order a reader expects them. Everything else — the dozens
#: of ``Received``, ``X-`` and DKIM lines — is transport noise that would swamp the body.
_KEPT_HEADERS = ("From", "To", "Cc", "Date", "Reply-To")


def _header(message: email.message.Message, name: str) -> str:
    """One header as a plain string, never raising on a malformed value."""
    try:
        value = message.get(name)
    except (ValueError, IndexError, AttributeError):  # malformed header, defensively
        return ""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _part_text(part: email.message.Message) -> str:
    """Decoded text of one MIME part, or empty when it cannot be decoded."""
    try:
        payload = part.get_payload(decode=True)
    except (ValueError, TypeError, AssertionError):  # malformed base64/QP, defensively
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def parse_eml(
    data: bytes, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> None:
    """Parse an RFC 5322 message.

    Zones follow the same logic as everywhere else: the ``Subject`` is what the message
    *says it is*, so it is the title; the envelope headers are furniture, because they
    repeat across every message from the same correspondent and would otherwise look like
    document evidence; the body is body.

    **Attachments are named, not opened.** A PDF attached to an email is a different document
    with a different doctype, and folding its text into the email's would produce one blended
    payload that is neither. The attachment's filename and type are recorded as furniture so
    a caller can see what to submit next.
    """
    deadline.check("eml")
    try:
        message = email.message_from_bytes(data, policy=email.policy.default)
    except (ValueError, TypeError) as exc:
        raise MalformedDocument(f"not a readable RFC 5322 message: {exc}") from exc

    builder.page(1)
    subject = _header(message, "Subject")
    if subject:
        builder.block(subject, zone=Zone.title, page=1, role="emailSubject")
    for name in _KEPT_HEADERS:
        value = _header(message, name)
        if value:
            builder.block(f"{name}: {value}", zone=Zone.furniture, page=1, role="emailHeader")

    html_fallback: list[str] = []
    parts = 0
    for part in message.walk():
        parts += 1
        if parts > limits.max_mime_parts:
            builder.limits_hit.append("max_mime_parts")
            builder.truncated = True
            break
        deadline.check(f"eml.part{parts}")
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and disposition != "inline"):
            builder.block(
                f"Attachment: {filename or 'unnamed'} ({part.get_content_type()})",
                zone=Zone.furniture,
                page=1,
                role="emailAttachment",
            )
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            builder.lines(_part_text(part), zone=Zone.body, page=1)
        elif content_type == "text/html":
            html_fallback.append(_part_text(part))

    # A multipart/alternative message repeats its body in both text and HTML. Prefer the
    # plain part — it is the same words without the markup — and fall back to the HTML only
    # when there was no plain part at all.
    if html_fallback and builder.block_count <= 1 + len(_KEPT_HEADERS):
        parse_html("\n".join(html_fallback), builder, limits, deadline)


__all__ = ["parse_eml", "parse_html"]
