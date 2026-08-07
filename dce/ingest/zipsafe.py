"""A ZIP reader that assumes the archive is hostile.

DOCX, XLSX, PPTX and ODT are ZIP containers. Accepting them means accepting the zip-bomb
class of attack, so every read in this package goes through :class:`SafeArchive` and nothing
calls :mod:`zipfile` directly.

Four guards, because the obvious one is not sufficient on its own:

1. **Entry count, before the archive is opened at all.** ``zipfile.ZipFile`` materialises one
   ``ZipInfo`` per central-directory record at construction time, so an archive claiming ten
   million members costs memory before any of our code runs. The count is read straight out
   of the end-of-central-directory record first and refused there.
2. **Declared compression ratio**, per member, from the central directory — free, and it
   catches the classic 42 KB → 4.5 GB shape before a single byte is inflated.
3. **A bounded read**, per member, that stops inflating at the cap. The declared sizes are
   attacker-controlled; the only number that cannot lie is how many bytes actually came out.
4. **A whole-archive decompression budget**, so a thousand members each just under the
   per-member cap cannot add up to a memory exhaustion.

Path traversal (``../``) is irrelevant here — nothing is ever written to disk — but member
names are still normalised before comparison so ``word/document.xml`` cannot be smuggled past
a lookup as ``word/./document.xml``.
"""
from __future__ import annotations

import io
import posixpath
import struct
import zipfile
from types import TracebackType

from dce.ingest.errors import ArchiveBomb, MalformedDocument
from dce.ingest.limits import Deadline, IngestLimits

_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_MIN_SIZE = 22
#: The ZIP comment is a uint16 length, so the EOCD starts at most 65535 + 22 bytes from the end.
_EOCD_SEARCH_WINDOW = 0xFFFF + _EOCD_MIN_SIZE
_ZIP64_SENTINEL = 0xFFFF


def _declared_entry_count(data: bytes) -> int | None:
    """Entry count from the end-of-central-directory record, without parsing the archive.

    Args:
        data: The whole archive.

    Returns:
        The number of members the archive claims to hold, or ``None`` when the record could
        not be found or uses the ZIP64 sentinel (in which case the caller falls through to
        the ordinary guards rather than trusting a number it could not read).
    """
    window = data[-_EOCD_SEARCH_WINDOW:] if len(data) > _EOCD_SEARCH_WINDOW else data
    index = window.rfind(_EOCD_SIGNATURE)
    if index < 0 or len(window) - index < _EOCD_MIN_SIZE:
        return None
    total = struct.unpack_from("<H", window, index + 10)[0]
    return None if total == _ZIP64_SENTINEL else int(total)


def normalize_member(name: str) -> str:
    """Canonical form of an archive member name, for lookups."""
    return posixpath.normpath(name.replace("\\", "/")).lstrip("./")


class SafeArchive:
    """A ZIP archive with every read bounded. Use via :func:`open_archive`."""

    def __init__(self, zf: zipfile.ZipFile, limits: IngestLimits, deadline: Deadline) -> None:
        self._zf = zf
        self._limits = limits
        self._deadline = deadline
        self._spent = 0
        self._index: dict[str, zipfile.ZipInfo] = {}
        for info in zf.infolist():
            self._index.setdefault(normalize_member(info.filename), info)

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> SafeArchive:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    # -- inspection ---------------------------------------------------------
    def names(self) -> list[str]:
        """Normalised member names, in central-directory order."""
        return list(self._index)

    def has(self, name: str) -> bool:
        return normalize_member(name) in self._index

    def match(self, prefix: str, suffix: str = "") -> list[str]:
        """Members under ``prefix`` ending in ``suffix``, sorted for determinism."""
        prefix = normalize_member(prefix) if prefix else ""
        return sorted(
            name
            for name in self._index
            if name.startswith(prefix) and name.endswith(suffix)
        )

    # -- reading ------------------------------------------------------------
    def read(self, name: str, *, max_bytes: int | None = None) -> bytes:
        """Decompress one member, refusing to inflate past the caps.

        Args:
            name: Member name; normalised before lookup.
            max_bytes: Per-member ceiling, defaulting to ``max_archive_entry_bytes``.

        Returns:
            The member's bytes.

        Raises:
            MalformedDocument: The member is absent or its stream is broken.
            ArchiveBomb: The member's declared ratio, its actual size, or the archive's
                running total crossed a cap.
        """
        key = normalize_member(name)
        info = self._index.get(key)
        if info is None:
            raise MalformedDocument(f"archive member {name!r} is missing")
        self._deadline.check(f"zip.read:{key}")

        cap = self._limits.max_archive_entry_bytes if max_bytes is None else max_bytes
        cap = min(cap, self._limits.max_archive_entry_bytes)

        # Guard 2: the declared numbers. Free, and wrong only in the direction that costs the
        # attacker — a bomb has to declare its expansion to make the decompressor perform it.
        if info.file_size > cap:
            raise ArchiveBomb(
                f"archive member {key!r} declares {info.file_size} bytes, over the "
                f"{cap}-byte per-member cap"
            )
        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > self._limits.max_compression_ratio:
                raise ArchiveBomb(
                    f"archive member {key!r} declares a {ratio:.0f}x compression ratio, over "
                    f"the {self._limits.max_compression_ratio:.0f}x cap — this is a zip bomb"
                )

        # Guard 3 + 4: what actually comes out, and what the archive has cost so far.
        remaining_budget = self._limits.max_archive_uncompressed_bytes - self._spent
        if remaining_budget <= 0:
            raise ArchiveBomb(
                f"archive already decompressed {self._spent} bytes, at the "
                f"{self._limits.max_archive_uncompressed_bytes}-byte whole-archive cap"
            )
        ceiling = min(cap, remaining_budget)

        chunks: list[bytes] = []
        total = 0
        try:
            with self._zf.open(info, "r") as handle:
                while True:
                    self._deadline.check(f"zip.inflate:{key}")
                    chunk = handle.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > ceiling:
                        raise ArchiveBomb(
                            f"archive member {key!r} inflated past {ceiling} bytes; the "
                            "declared size was a lie and the read was aborted"
                        )
                    chunks.append(chunk)
        except (zipfile.BadZipFile, EOFError, OSError) as exc:
            raise MalformedDocument(f"archive member {key!r} is unreadable: {exc}") from exc
        self._spent += total
        return b"".join(chunks)

    def read_text(self, name: str, *, max_bytes: int | None = None) -> str:
        """:meth:`read`, decoded as UTF-8 with replacement.

        XML parts in OOXML/ODF are UTF-8 by specification. Replacement rather than strict
        because one bad byte in a 200 KB sheet should cost that character, not the document.
        """
        return self.read(name, max_bytes=max_bytes).decode("utf-8", "replace")


def open_archive(data: bytes, limits: IngestLimits, deadline: Deadline) -> SafeArchive:
    """Open ``data`` as a ZIP with all four guards armed.

    Raises:
        MalformedDocument: The bytes are not a readable ZIP.
        ArchiveBomb: The archive declares more members than ``max_archive_entries``.
    """
    declared = _declared_entry_count(data)
    if declared is not None and declared > limits.max_archive_entries:
        # Guard 1: refuse before ZipFile allocates one object per member.
        raise ArchiveBomb(
            f"archive declares {declared} members, over the {limits.max_archive_entries} cap"
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise MalformedDocument(f"not a readable ZIP archive: {exc}") from exc
    if len(zf.infolist()) > limits.max_archive_entries:
        zf.close()
        raise ArchiveBomb(
            f"archive holds {len(zf.infolist())} members, over the "
            f"{limits.max_archive_entries} cap"
        )
    return SafeArchive(zf, limits, deadline)


__all__ = ["SafeArchive", "normalize_member", "open_archive"]
