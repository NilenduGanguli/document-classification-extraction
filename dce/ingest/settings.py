"""Ingestion settings, kept separate from :mod:`dce.config` on purpose.

``dce.config.Settings`` governs the classifier and the extraction tiers. Ingestion is a
different concern with a different owner — it is about *what a caller may upload* and *how
much of one request's work the process will do* — and folding a dozen parser caps into the
settings object a control reviewer reads for the egress invariant would bury the invariant.

Everything here is read from the environment with the ``DCE_INGEST_`` prefix, e.g.
``DCE_INGEST_LOCAL_OCR_ENABLED=true``.

**Local OCR is off by default and that is the whole design.** See :mod:`dce.ingest`.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from dce.ingest.limits import IngestLimits


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="dce_ingest_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LOCAL OCR: OFF, AND OPTIONAL ---------------------------------------
    #: Turn on in-process OCR for images and scanned PDFs. Off by default so the standard
    #: build has no OCR dependency at all and an image gets the honest ``needs_ocr`` answer
    #: rather than a guess. Turning it on never introduces egress — the engines below run
    #: locally — but it does introduce an accuracy claim, which is why it is a decision.
    local_ocr_enabled: bool = False
    #: ``rapidocr`` (ONNX, genuinely in-process) or ``tesseract`` (a local subprocess).
    local_ocr_engine: str = "rapidocr"
    #: Tesseract language packs, e.g. ``eng+hin``. Ignored by RapidOCR.
    local_ocr_languages: str = "eng"

    # ---- Caps ---------------------------------------------------------------
    max_bytes: int = 32 * 1024 * 1024
    max_seconds: float = 20.0
    max_pages: int = 200
    max_ocr_pages: int = 10
    ocr_dpi: int = 300

    def limits(self) -> IngestLimits:
        """The :class:`~dce.ingest.limits.IngestLimits` this deployment is configured for."""
        return IngestLimits(
            max_bytes=self.max_bytes,
            max_seconds=self.max_seconds,
            max_pages=self.max_pages,
            max_ocr_pages=self.max_ocr_pages,
            ocr_dpi=self.ocr_dpi,
        )


@lru_cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()


__all__ = ["IngestSettings", "get_ingest_settings"]
