"""Outlook ``.msg`` — a Compound File Binary container, read with the standard library.

A ``.msg`` is an OLE2/CFB file: a FAT-based filesystem inside a single file, in which each
MAPI property is its own stream named ``__substg1.0_<tag><type>``. There is no stdlib reader
for it, so there is one here — about two hundred lines, versus a dependency that would have
to be audited by anyone reviewing what runs in the same process as unclassified customer
documents.

It reads the container defensively, because the container is attacker-controlled: every
sector chain is walked with a visited set (a chain that points at itself is the cheapest
possible hang), every chain has a sector-count ceiling, and the header's own numbers are
sanity-checked against the actual file length rather than trusted.

Only the handful of properties that say what a message *is* are read — subject, sender,
recipients, body, attachment names. The rest of a MAPI property set is transport and client
state.
"""
from __future__ import annotations

import struct

from dce.ingest.builder import LayoutBuilder
from dce.ingest.errors import MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits
from dce.models import Zone

CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_DIFSECT = 0xFFFFFFFC
_MAXREGSECT = 0xFFFFFFFA

_DIR_ENTRY_SIZE = 128
_STREAM_TYPE = 2
_ROOT_TYPE = 5

#: Ceiling on sectors walked in one chain — a bound on work, independent of what the header
#: claims. 2^22 sectors at 512 bytes is 2 GB, already past ``max_bytes``.
_MAX_CHAIN = 1 << 22


class CompoundFile:
    """A read-only view of a CFB container."""

    def __init__(self, data: bytes) -> None:
        if not data.startswith(CFB_SIGNATURE):
            raise MalformedDocument("not an OLE2/CFB compound file")
        if len(data) < 512:
            raise MalformedDocument("compound file is shorter than its own header")
        self._data = data

        sector_shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
        if sector_shift not in (9, 12) or mini_shift != 6:
            raise MalformedDocument(
                f"unsupported CFB geometry (sector shift {sector_shift}, mini {mini_shift})"
            )
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift
        (
            num_fat_sectors,
            self._first_dir_sector,
            _transaction,
            self._mini_cutoff,
            self._first_minifat,
            num_minifat_sectors,
            self._first_difat,
            num_difat_sectors,
        ) = struct.unpack_from("<IIIIIIII", data, 0x2C)

        self._sector_count = max(0, (len(data) - self.sector_size) // self.sector_size)
        self._fat = self._read_fat(num_fat_sectors, num_difat_sectors)
        self._minifat = self._read_chain_values(self._first_minifat, num_minifat_sectors)
        self._entries = self._read_directory()
        self._mini_stream = self._read_mini_stream()

    # -- raw sector access --------------------------------------------------
    def _sector(self, index: int) -> bytes:
        if index < 0 or index >= self._sector_count:
            raise MalformedDocument(f"compound file references sector {index}, past the file")
        start = (index + 1) * self.sector_size
        return self._data[start : start + self.sector_size]

    def _chain(self, start: int) -> list[int]:
        """Sector numbers of one FAT chain, refusing loops and runaway lengths."""
        out: list[int] = []
        seen: set[int] = set()
        current = start
        while current <= _MAXREGSECT:
            if current in seen:
                raise MalformedDocument("FAT chain loops back on itself")
            if len(out) >= _MAX_CHAIN:
                raise MalformedDocument("FAT chain is implausibly long")
            seen.add(current)
            out.append(current)
            if current >= len(self._fat):
                raise MalformedDocument("FAT chain runs past the allocation table")
            current = self._fat[current]
        return out

    # -- allocation tables --------------------------------------------------
    def _read_fat(self, num_fat_sectors: int, num_difat_sectors: int) -> list[int]:
        per_sector = self.sector_size // 4
        difat: list[int] = list(struct.unpack_from("<109I", self._data, 0x4C))

        sector = self._first_difat
        walked = 0
        seen: set[int] = set()
        while sector <= _MAXREGSECT and walked <= num_difat_sectors + 1 and walked < _MAX_CHAIN:
            if sector in seen:
                raise MalformedDocument("DIFAT chain loops back on itself")
            seen.add(sector)
            block = self._sector(sector)
            values = struct.unpack_from(f"<{per_sector}I", block, 0)
            difat.extend(values[:-1])
            sector = values[-1]
            walked += 1

        fat: list[int] = []
        for index, fat_sector in enumerate(difat):
            if index >= num_fat_sectors or fat_sector > _MAXREGSECT:
                break
            block = self._sector(fat_sector)
            fat.extend(struct.unpack_from(f"<{per_sector}I", block, 0))
        if not fat:
            raise MalformedDocument("compound file has no allocation table")
        return fat

    def _read_chain_values(self, start: int, expected_sectors: int) -> list[int]:
        """A chain of sectors read as a uint32 array — used for the MiniFAT."""
        if start > _MAXREGSECT or expected_sectors == 0:
            return []
        per_sector = self.sector_size // 4
        values: list[int] = []
        for index, sector in enumerate(self._chain(start)):
            if index >= expected_sectors:
                break
            values.extend(struct.unpack_from(f"<{per_sector}I", self._sector(sector), 0))
        return values

    def _read_directory(self) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for sector in self._chain(self._first_dir_sector):
            block = self._sector(sector)
            for offset in range(0, len(block) - _DIR_ENTRY_SIZE + 1, _DIR_ENTRY_SIZE):
                raw = block[offset : offset + _DIR_ENTRY_SIZE]
                name_len = struct.unpack_from("<H", raw, 64)[0]
                if not 2 <= name_len <= 64:
                    continue
                name = raw[: name_len - 2].decode("utf-16-le", "replace")
                obj_type = raw[66]
                if obj_type not in (_STREAM_TYPE, _ROOT_TYPE) or not name:
                    continue
                start, size_low, size_high = struct.unpack_from("<III", raw, 0x74)
                size = size_low | (size_high << 32)
                entries.append(
                    {"name": name, "type": obj_type, "start": start, "size": size}
                )
                if len(entries) > 1 << 18:
                    raise MalformedDocument("compound file declares an absurd directory")
        if not entries:
            raise MalformedDocument("compound file has no directory entries")
        return entries

    def _read_mini_stream(self) -> bytes:
        root = next((e for e in self._entries if e["type"] == _ROOT_TYPE), None)
        if root is None:
            return b""
        return self._read_fat_stream(int(root["start"]), int(root["size"]))

    # -- streams ------------------------------------------------------------
    def _read_fat_stream(self, start: int, size: int) -> bytes:
        if size <= 0 or start > _MAXREGSECT:
            return b""
        size = min(size, len(self._data))
        chunks: list[bytes] = []
        total = 0
        for sector in self._chain(start):
            chunks.append(self._sector(sector))
            total += self.sector_size
            if total >= size:
                break
        return b"".join(chunks)[:size]

    def _read_mini_chain(self, start: int, size: int) -> bytes:
        if size <= 0 or start > _MAXREGSECT:
            return b""
        chunks: list[bytes] = []
        total = 0
        current = start
        seen: set[int] = set()
        while current <= _MAXREGSECT and total < size:
            if current in seen or len(seen) >= _MAX_CHAIN:
                raise MalformedDocument("mini-FAT chain loops back on itself")
            seen.add(current)
            offset = current * self.mini_sector_size
            chunks.append(self._mini_stream[offset : offset + self.mini_sector_size])
            total += self.mini_sector_size
            if current >= len(self._minifat):
                break
            current = self._minifat[current]
        return b"".join(chunks)[:size]

    def streams(self) -> list[str]:
        """Names of every stream in the container, in directory order."""
        return [str(e["name"]) for e in self._entries if e["type"] == _STREAM_TYPE]

    def read(self, name: str, *, max_bytes: int) -> bytes:
        """Read one stream by name, truncated to ``max_bytes``."""
        for entry in self._entries:
            if entry["type"] != _STREAM_TYPE or entry["name"] != name:
                continue
            size = min(int(entry["size"]), max_bytes)
            start = int(entry["start"])
            if int(entry["size"]) < self._mini_cutoff:
                return self._read_mini_chain(start, size)
            return self._read_fat_stream(start, size)
        return b""


# ---------------------------------------------------------------------------
# MAPI properties
# ---------------------------------------------------------------------------
_SUBSTG = "__substg1.0_"

#: MAPI property tags this reader cares about. The full set runs to thousands.
PID_SUBJECT = "0037"
PID_BODY = "1000"
PID_BODY_HTML = "1013"
PID_SENDER_NAME = "0C1A"
PID_SENDER_EMAIL = "0C1F"
PID_DISPLAY_TO = "0E04"
PID_DISPLAY_CC = "0E03"
PID_ATTACH_FILENAME = "3707"
PID_ATTACH_LONG_FILENAME = "3704"

_UNICODE_TYPE = "001F"
_ASCII_TYPE = "001E"


def _property(cfb: CompoundFile, streams: dict[str, str], tag: str, *, max_bytes: int) -> str:
    """One string property, preferring the Unicode variant over the 8-bit one."""
    for type_code, encoding in ((_UNICODE_TYPE, "utf-16-le"), (_ASCII_TYPE, "cp1252")):
        name = streams.get(f"{tag}{type_code}".upper())
        if name is None:
            continue
        raw = cfb.read(name, max_bytes=max_bytes)
        if raw:
            return raw.decode(encoding, "replace")
    return ""


def parse_msg(
    data: bytes, builder: LayoutBuilder, limits: IngestLimits, deadline: Deadline
) -> None:
    """Read an Outlook ``.msg`` into zoned blocks.

    The zone mapping mirrors :func:`dce.ingest.markup.parse_eml` exactly, because the two are
    the same document in two containers: subject to :attr:`~dce.models.Zone.title`, envelope
    to :attr:`~dce.models.Zone.furniture`, body to :attr:`~dce.models.Zone.body`, attachment
    names listed but never opened.
    """
    deadline.check("msg")
    cfb = CompoundFile(data)

    #: ``{TAGTYPE: full stream name}`` — the substorage prefix stripped and upper-cased, so
    #: the lookup does not depend on the casing a particular Outlook version wrote.
    streams: dict[str, str] = {}
    attachments: list[str] = []
    for name in cfb.streams():
        if not name.startswith(_SUBSTG):
            continue
        key = name[len(_SUBSTG) :].upper()
        streams.setdefault(key, name)

    builder.page(1)
    cap = min(limits.max_chars, limits.max_bytes)

    subject = _property(cfb, streams, PID_SUBJECT, max_bytes=limits.max_block_chars * 2)
    if subject:
        builder.block(subject, zone=Zone.title, page=1, role="emailSubject")

    sender = _property(cfb, streams, PID_SENDER_NAME, max_bytes=4096)
    sender_email = _property(cfb, streams, PID_SENDER_EMAIL, max_bytes=4096)
    if sender or sender_email:
        builder.block(
            f"From: {sender} {sender_email}".strip(),
            zone=Zone.furniture,
            page=1,
            role="emailHeader",
        )
    for tag, label in ((PID_DISPLAY_TO, "To"), (PID_DISPLAY_CC, "Cc")):
        value = _property(cfb, streams, tag, max_bytes=8192)
        if value:
            builder.block(f"{label}: {value}", zone=Zone.furniture, page=1, role="emailHeader")

    # Attachment names live in per-attachment storages we do not walk; the flat directory
    # still exposes their filename streams, which is all we report.
    for name in cfb.streams():
        if not name.startswith(_SUBSTG):
            continue
        key = name[len(_SUBSTG) :].upper()
        if key[:4] in (PID_ATTACH_LONG_FILENAME, PID_ATTACH_FILENAME) and key[4:] in (
            _UNICODE_TYPE,
            _ASCII_TYPE,
        ):
            encoding = "utf-16-le" if key[4:] == _UNICODE_TYPE else "cp1252"
            filename = cfb.read(name, max_bytes=1024).decode(encoding, "replace").strip()
            if filename and filename not in attachments:
                attachments.append(filename)
    for filename in attachments[:64]:
        builder.block(
            f"Attachment: {filename}", zone=Zone.furniture, page=1, role="emailAttachment"
        )

    body = _property(cfb, streams, PID_BODY, max_bytes=cap)
    if body.strip():
        builder.lines(body, zone=Zone.body, page=1)
        return

    html = _property(cfb, streams, PID_BODY_HTML, max_bytes=cap)
    if not html:
        raw_html = cfb.read(streams.get(f"{PID_BODY_HTML}0102", ""), max_bytes=cap)
        html = raw_html.decode("utf-8", "replace") if raw_html else ""
    if html.strip():
        from dce.ingest.markup import parse_html

        parse_html(html, builder, limits, deadline)


__all__ = ["CFB_SIGNATURE", "CompoundFile", "parse_msg"]
