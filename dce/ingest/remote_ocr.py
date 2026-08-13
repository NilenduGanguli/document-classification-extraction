"""Remote OCR: **egress, before the doctype is known**, and therefore off by default.

This is provider (B). It is the honest other half of a trade-off :mod:`dce.ingest` states
plainly: an image carries no text, classifying one requires recognition, and recognition
happens either on this host or on somebody else's. There is no third option. A deployment
that cannot install a local engine and cannot accept ``needs_ocr`` has to send the document
somewhere, and this module is how — deliberately, visibly, and never by accident.

--------------------------------------------------------------------------------
WHAT USING THIS COSTS, STATED BEFORE HOW IT WORKS
--------------------------------------------------------------------------------
Turning ``DCE_INGEST_REMOTE_OCR_ENABLED`` on means this service transmits documents that
**have not been classified** — another business unit's documents, whose type nobody knows yet
— to Microsoft. That is the disclosure :mod:`dce.egress` exists to prevent. Four things make
it a decision rather than a slip:

1. It is off by default, and the base install has no HTTP client at all
   (``pip install '.[azure-ocr]'``), so the default build *cannot* do this.
2. It is a **separate** setting from ``local_ocr_enabled``. Calling a network provider "local
   OCR" would make the word "local" a lie in the place an operator reads fastest.
3. Every request — submit and each poll — passes
   :func:`dce.egress.assert_ocr_egress_permitted`, a positive check that names the provider
   and the endpoint. ``assert_no_egress`` would be silent here (ingestion runs before the
   classification scope is entered), and relying on that silence would be a bypass.
4. ``/readyz`` reports the deployment as transmitting unclassified documents and names the
   endpoint host, whether or not anybody asked.

**The zero-egress alternatives, in preference order**, both of which keep the invariant
intact: the caller-supplied path (an upstream service does the OCR and posts the result to
``/classify`` as ``azure_analyze_result`` / ``azure_read_result`` / ``des_ocr``), and a local
engine (:mod:`dce.ingest.ocr`).

--------------------------------------------------------------------------------
PROTOCOL
--------------------------------------------------------------------------------
Both providers are Azure's asynchronous shape, and the implementation is the one already
proven against these services in the Document Enrichment Service (``des/ocr/azure_read.py``,
``des/ocr/azure_layout.py``) rather than a second invention:

* **Read v3.2** — ``POST {endpoint}/vision/v3.2/read/analyze`` with the bytes as the body,
  ``202`` plus an ``Operation-Location`` header, then ``GET`` that URL until ``status`` is
  terminal. Maps through :func:`dce.adapters.from_azure_read`.
* **Document Intelligence v4.0** — ``POST
  {endpoint}/documentintelligence/documentModels/{model}:analyze?api-version=…``, same 202 +
  ``Operation-Location`` + poll. Maps through :func:`dce.adapters.from_azure_layout`.

Polling is bounded twice — by a wall clock *and* by a poll count — because a provider that
answers instantly with a non-terminal status would otherwise be hammered for the whole
timeout. The ingestion :class:`~dce.ingest.limits.Deadline` bounds it a third time, so a
remote call can never outlive the request budget the caller was promised.

**The whole document goes in one call**, not one call per rasterised page. Both services
accept PDFs and images natively, so this needs no PDF renderer, and — the point that matters
— the payload that comes back is byte-for-byte the shape a caller would have posted on path
(A). The same adapter maps it. Provider (B) is therefore *exactly* provider (A) with the
network call made here instead of there, which is the only property that makes the two
comparable to a reviewer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from dce.adapters import from_azure_layout, from_azure_read
from dce.egress import assert_ocr_egress_permitted
from dce.ingest.detect import MediaType
from dce.ingest.errors import EngineUnavailable, IngestTimeout, MalformedDocument
from dce.ingest.limits import Deadline
from dce.models import LayoutView

#: Terminal job statuses. Anything else means "keep polling".
_TERMINAL = {"succeeded", "failed"}

#: MIME type sent with the document. Azure decides the parser from this, so a wrong value is
#: a failed analyse rather than a wrong answer.
_CONTENT_TYPES: dict[MediaType, str] = {
    MediaType.pdf: "application/pdf",
    MediaType.jpeg: "image/jpeg",
    MediaType.png: "image/png",
    MediaType.tiff: "image/tiff",
    MediaType.bmp: "image/bmp",
    MediaType.heic: "image/heic",
    MediaType.webp: "image/webp",
    MediaType.gif: "image/gif",
}


def content_type_for(media_type: MediaType) -> str:
    """MIME type to submit ``media_type`` under; ``application/octet-stream`` if unmapped."""
    return _CONTENT_TYPES.get(media_type, "application/octet-stream")


def _require_httpx():
    """Import ``httpx``, or explain that the optional extra was not installed.

    Lazy on purpose. The base dependency set contains no HTTP client — see ``pyproject.toml``
    — so importing this module must not fail on a default build, and a default build must not
    acquire an HTTP client merely because someone imported :mod:`dce.ingest`.
    """
    try:
        import importlib

        return importlib.import_module("httpx")
    except ImportError as exc:  # pragma: no cover - exercised by the extras matrix, not CI
        raise EngineUnavailable(
            "remote OCR needs 'httpx', which is not installed. The base install deliberately "
            "ships no HTTP client: a build that cannot open a socket cannot leak a document. "
            "Install the optional extra deliberately: pip install '.[azure-ocr]'."
        ) from exc


@dataclass(frozen=True)
class RemoteOcrConfig:
    """Everything one remote provider needs, resolved from :class:`IngestSettings`."""

    provider: str
    endpoint: str
    key: str
    api_version: str
    model: str = ""
    timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.5
    max_polls: int = 60

    @property
    def host(self) -> str:
        """Endpoint host — what an operator is shown, and what a log line names."""
        parsed = urlsplit(self.endpoint if "//" in self.endpoint else f"//{self.endpoint}")
        return parsed.hostname or ""


class RemoteOcrProvider(Protocol):
    """What a network recogniser must offer. Mirrors the local protocol, plus the flag."""

    name: str
    #: Always True. The pipeline branches on this, not on the provider's name.
    network: bool
    endpoint: str

    def recognize(self, data: bytes, *, media_type: MediaType, deadline: Deadline) -> LayoutView:
        """Send one whole document out, and return the layout the provider read back."""
        ...


class _AzureAsyncProvider:
    """Shared 202 + ``Operation-Location`` + bounded-poll client for both Azure products."""

    network = True

    def __init__(self, config: RemoteOcrConfig, *, enabled: bool) -> None:
        self._config = config
        #: Carried, not read from a global: the guard must be told what the *deployment*
        #: decided, by the code that resolved the deployment's settings, so that constructing
        #: a provider in a test can never accidentally inherit a permission.
        self._enabled = enabled
        self.name = config.provider
        self.endpoint = config.endpoint

    # -- the guard ----------------------------------------------------------
    def _permit(self) -> None:
        assert_ocr_egress_permitted(self.name, self.endpoint, enabled=self._enabled)

    # -- per-product ---------------------------------------------------------
    def _analyze_url(self) -> str:
        raise NotImplementedError

    def _analyze_params(self) -> dict[str, str]:
        return {}

    def _submit_content_type(self, media_type: MediaType) -> str:
        return content_type_for(media_type)

    def _to_layout(self, job: dict[str, Any]) -> LayoutView:
        raise NotImplementedError

    # -- transport -----------------------------------------------------------
    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self._config.key:
            headers["Ocp-Apim-Subscription-Key"] = self._config.key
        return headers

    def _budget(self, deadline: Deadline) -> float:
        """Seconds this analyse may take: the provider's own cap, or the request's if shorter.

        The ingestion deadline wins when it is tighter, so a remote call can never outlive the
        budget a caller was promised — a long poll is not a licence to hold a request open.
        """
        return max(0.0, min(float(self._config.timeout_seconds), deadline.remaining))

    def recognize(
        self, data: bytes, *, media_type: MediaType, deadline: Deadline
    ) -> LayoutView:
        """Submit the whole document, poll to a terminal status, and adapt the result.

        Args:
            data: The entire document — both products accept PDFs and images natively, so
                nothing is rasterised here and no PDF engine is needed.
            media_type: Decides the ``Content-Type`` header.
            deadline: The ingestion budget; bounds the polling alongside the provider's own
                timeout and poll cap.

        Returns:
            The provider-neutral view, produced by the **same adapter** a caller-supplied
            payload would go through.

        Raises:
            EgressViolation: When this deployment has not permitted remote OCR, or when the
                call is made from inside a classification scope.
            EngineUnavailable: No HTTP client installed, a transport failure, a non-202
                submit, or a 202 with no ``Operation-Location``.
            IngestTimeout: The job did not reach a terminal status within the budget or the
                poll cap.
            MalformedDocument: The provider reported the document could not be analysed.
        """
        httpx = _require_httpx()
        budget = self._budget(deadline)
        if budget <= 0:
            raise IngestTimeout(
                f"no time left for remote OCR at {self._config.host!r}; the ingestion "
                "deadline was already spent before the document could be submitted"
            )
        started = time.monotonic()
        try:
            with httpx.Client(timeout=budget) as client:
                operation_url = self._submit(client, data, media_type)
                job = self._poll(client, operation_url, started, budget)
        except httpx.HTTPError as exc:
            # The message deliberately names the endpoint and the exception type only. A
            # transport error can carry a request body, and this one's body is a customer's
            # unclassified document.
            raise EngineUnavailable(
                f"remote OCR provider {self.name!r} at {self._config.host!r} failed: "
                f"{type(exc).__name__}"
            ) from exc

        status = str(job.get("status") or "").lower()
        if status != "succeeded":
            raise MalformedDocument(
                f"remote OCR provider {self.name!r} ended with status {status!r}; the "
                "document could not be analysed"
            )
        return self._to_layout(job)

    def _submit(self, client: Any, data: bytes, media_type: MediaType) -> str:
        self._permit()
        response = client.post(
            self._analyze_url(),
            content=data,
            params=self._analyze_params() or None,
            headers=self._headers(content_type=self._submit_content_type(media_type)),
        )
        if response.status_code != 202:
            raise EngineUnavailable(
                f"remote OCR provider {self.name!r} answered the analyse request with HTTP "
                f"{response.status_code} rather than 202"
            )
        operation_url = response.headers.get("Operation-Location", "")
        if not operation_url:
            raise EngineUnavailable(
                f"remote OCR provider {self.name!r} returned 202 with no Operation-Location "
                "header; there is nothing to poll"
            )
        return operation_url

    def _poll(self, client: Any, operation_url: str, started: float, budget: float) -> dict:
        """Poll to a terminal status under two independent bounds."""
        for _ in range(max(1, self._config.max_polls)):
            elapsed = time.monotonic() - started
            if elapsed > budget:
                break
            time.sleep(min(self._config.poll_interval_seconds, max(0.0, budget - elapsed)))
            self._permit()
            response = client.get(operation_url, headers=self._headers())
            response.raise_for_status()
            job = response.json()
            if not isinstance(job, dict):
                raise MalformedDocument(
                    f"remote OCR provider {self.name!r} returned a non-object job document"
                )
            if str(job.get("status") or "").lower() in _TERMINAL:
                return job
        raise IngestTimeout(
            f"remote OCR provider {self.name!r} at {self._config.host!r} did not finish "
            f"within {self._config.timeout_seconds:g}s / {self._config.max_polls} polls; the "
            "document was not classified rather than the request being held open"
        )


class AzureReadProvider(_AzureAsyncProvider):
    """Azure AI Vision **Read v3.2** — lines and words, no roles. See :func:`from_azure_read`."""

    def _analyze_url(self) -> str:
        version = self._config.api_version.strip("/") or "v3.2"
        return f"{self._config.endpoint.rstrip('/')}/vision/{version}/read/analyze"

    def _submit_content_type(self, media_type: MediaType) -> str:
        # Read v3.2 takes the binary as an opaque stream; it sniffs the format itself.
        return "application/octet-stream"

    def _to_layout(self, job: dict[str, Any]) -> LayoutView:
        return from_azure_read(job)


class AzureLayoutProvider(_AzureAsyncProvider):
    """Azure AI Document Intelligence v4.0 ``prebuilt-layout`` — roles, tables, marks."""

    def _analyze_url(self) -> str:
        model = self._config.model or "prebuilt-layout"
        return (
            f"{self._config.endpoint.rstrip('/')}"
            f"/documentintelligence/documentModels/{model}:analyze"
        )

    def _analyze_params(self) -> dict[str, str]:
        return {"api-version": self._config.api_version}

    def _to_layout(self, job: dict[str, Any]) -> LayoutView:
        return from_azure_layout(job)


#: The closed allowlist of network providers, keyed the same way
#: :data:`dce.ingest.ocr.NETWORK_ENGINES` is. Extending it is a code change and a review.
_CONSTRUCTORS = {
    "azure_read": AzureReadProvider,
    "azure_layout": AzureLayoutProvider,
}


def load_remote_provider(config: RemoteOcrConfig, *, enabled: bool) -> RemoteOcrProvider:
    """Construct the named network provider.

    Args:
        config: Endpoint, key, versions and the polling bounds.
        enabled: Whether the deployment permitted remote OCR. Passed through to
            :func:`dce.egress.assert_ocr_egress_permitted` on every request; constructing a
            provider with ``enabled=False`` is legal and useless, which is the intent — the
            permission is checked where the bytes leave, not where the object is made.

    Raises:
        EngineUnavailable: ``config.provider`` is not a network provider, the endpoint is
            empty (with nowhere to send a document there is nothing to construct), or the
            HTTP client is not installed. The pipeline treats all three as "this deployment
            cannot recognise images" and returns ``needs_ocr``, exactly as it does for a
            local engine whose extra is missing — a half-configured recogniser must degrade
            the same way whichever kind it is.
    """
    constructor = _CONSTRUCTORS.get((config.provider or "").strip().lower())
    if constructor is None:
        raise EngineUnavailable(
            f"unknown remote OCR provider {config.provider!r}; supported: "
            f"{', '.join(sorted(_CONSTRUCTORS))}"
        )
    if not config.endpoint.strip():
        raise EngineUnavailable(
            f"remote OCR provider {config.provider!r} has no endpoint configured; with "
            "nowhere to send a document there is nothing to construct"
        )
    # Checked here as well as at the first request, so a missing extra presents as
    # `needs_ocr` with a reason rather than as a 503 on the request that happened to be an
    # image. Importing httpx is not egress; it is the thing that would make egress possible.
    _require_httpx()
    return constructor(config, enabled=enabled)


__all__ = [
    "AzureLayoutProvider",
    "AzureReadProvider",
    "RemoteOcrConfig",
    "RemoteOcrProvider",
    "content_type_for",
    "load_remote_provider",
]
