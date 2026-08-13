"""Ingestion settings, kept separate from :mod:`dce.config` on purpose.

``dce.config.Settings`` governs the classifier and the extraction tiers. Ingestion is a
different concern with a different owner — it is about *what a caller may upload* and *how
much of one request's work the process will do* — and folding a dozen parser caps into the
settings object a control reviewer reads for the egress invariant would bury the invariant.

Everything here is read from the environment with the ``DCE_INGEST_`` prefix, e.g.
``DCE_INGEST_LOCAL_OCR_ENABLED=true``.

**Local OCR is off by default and that is the whole design.** See :mod:`dce.ingest`.

**Remote OCR is also off by default, and it is a different kind of off.** Local OCR being on
is an accuracy claim. Remote OCR being on is a *disclosure*: this deployment sends documents
nobody has classified to a third party. The two therefore get two settings rather than one
overloaded flag — calling a network provider "local OCR" would make the word "local" a lie
in the one place an operator is most likely to read it quickly.

**Where that remote endpoint sits is DECLARED, not inferred.** ``ocr.internal.corp`` and
``x.cognitiveservices.azure.com`` are indistinguishable to this process, so
``remote_ocr_trust_boundary`` is how a deployment says which it has, the value is reported
with its provenance, and the code default is the cautious one.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dce.ingest.limits import IngestLimits
from dce.ingest.ocr import NETWORK_ENGINES, is_network_provider

#: The endpoint is outside this deployment's trust boundary. **The code default**, because a
#: deployment that has declared nothing must get the cautious reading rather than the
#: reassuring one: silence is not a statement that the host is internal.
TRUST_BOUNDARY_EXTERNAL = "external"
#: The deployment declares the endpoint is inside its own trust boundary — an on-premises OCR
#: appliance, an Azure service on a private endpoint, a host on the operator's own network.
TRUST_BOUNDARY_ON_PREMISES = "on_premises"
#: The complete set. A third value is a typo, and a typo in this field would otherwise decide
#: how a disclosure reads.
TRUST_BOUNDARIES = frozenset({TRUST_BOUNDARY_EXTERNAL, TRUST_BOUNDARY_ON_PREMISES})


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

    # ---- REMOTE OCR: EGRESS, BEFORE THE DOCTYPE IS KNOWN --------------------
    # Everything in this block is off/empty by default, and a deployment that wants no
    # pre-classification egress gets it by doing nothing at all.
    #
    # Turning it on means: this service will transmit documents that have NOT been classified
    # — other business units' documents — to the endpoint below, so that a third party can
    # read them. That is the exact disclosure dce.egress exists to prevent, and it is
    # permitted here only because the alternative for an image is not "read it locally" on
    # every deployment; it is "refuse to read it at all". Both answers are defensible. Picking
    # the second one silently, on a deployment that never chose it, is not.
    #
    # The invariant setting `dce.config.allow_preclassification_egress` is NOT involved and
    # must stay False: that one lets anything out during classification. This one permits a
    # single named call site (dce.ingest.remote_ocr), guarded by
    # dce.egress.assert_ocr_egress_permitted, and nothing else.

    #: Recognise images and scanned PDFs by sending them to a remote provider. Off.
    #: ``/readyz`` reports a deployment with this on as transmitting unclassified documents,
    #: naming the endpoint host, and says so whether or not anybody asked.
    remote_ocr_enabled: bool = False
    #: ``azure_read`` (Vision Read v3.2) or ``azure_layout`` (Document Intelligence v4.0
    #: ``prebuilt-layout``). Prefer ``azure_layout``: Read predicts no paragraph roles, so a
    #: Read payload can never satisfy a title-gated decisive anchor and classifies with
    #: strictly less evidence — see :func:`dce.adapters.from_azure_read`.
    remote_ocr_provider: str = "azure_layout"
    #: Base URL of the Azure Document Intelligence resource, e.g.
    #: ``https://<resource>.cognitiveservices.azure.com``. Empty by default: with no endpoint
    #: there is nowhere for a document to go, so the absence of configuration is itself a
    #: control. Deliberately a *separate* setting from ``dce.config.azure_di_endpoint``, which
    #: configures the post-classification T2/T3 tiers — sending a document before its type is
    #: known and sending one after are two different authorisations, and one variable
    #: granting both would let enabling T2 quietly enable this.
    azure_di_endpoint: str = ""
    #: Azure DI key. Inject it from a secret store at runtime; never bake it into an image.
    azure_di_key: str = ""
    #: Pinned, not "latest": a newer API version changes field names under a signed-off
    #: pipeline.
    azure_di_api_version: str = "2024-11-30"
    #: The DI model to analyse with. ``prebuilt-layout`` is the only one this path maps.
    azure_di_model: str = "prebuilt-layout"
    #: Base URL of the Azure AI Vision resource for Read v3.2, when
    #: ``remote_ocr_provider=azure_read``.
    azure_read_endpoint: str = ""
    azure_read_key: str = ""
    #: The version segment of Read's URL path (``/vision/{v}/read/analyze``). Read is frozen
    #: at v3.2; it is configurable so a resource behind a gateway that rewrites the path can
    #: still be pointed at, not so that a newer version can be guessed at.
    azure_read_api_version: str = "v3.2"

    # ---- WHERE THAT ENDPOINT SITS: A DECLARATION, NOT AN INFERENCE ----------
    #: ``external`` (default) or ``on_premises`` — where the deployment says the remote OCR
    #: endpoint sits relative to its own trust boundary.
    #:
    #: **This has to be declared because it cannot be derived.** ``ocr.internal.corp`` and
    #: ``x.cognitiveservices.azure.com`` are the same operation to this process: resolve a
    #: name, open a socket, put a document on it. A hostname is not evidence of ownership —
    #: a private DNS zone can point ``internal`` at anything, and a private endpoint can put
    #: a vendor domain inside a VPC. Any code here that guessed from the string would be
    #: asserting something it does not know, in the one field an auditor reads for exactly
    #: that fact.
    #:
    #: So the deployment states it, and the statement is *attributable*: ``/readyz`` reports
    #: the value, that it came from this variable, and that the service did not verify it.
    #: Declaring ``on_premises`` changes how the disclosure READS — from "documents are
    #: transmitted to a third party" to "documents go to a host the operator declares is
    #: theirs" — and changes nothing about what the process DOES. The bytes still leave this
    #: process over a socket before the doctype is known, ``ocr.network`` is still true, and
    #: ``egress.preclassification_ocr`` is still true. This setting cannot be used to make a
    #: transmission stop being reported; only to say whose network it lands on.
    #:
    #: The default is the cautious reading on purpose. A deployment that forgets to declare
    #: gets "external", never the reassuring answer by omission.
    remote_ocr_trust_boundary: str = TRUST_BOUNDARY_EXTERNAL

    #: Wall clock for one remote analyse, submit and polling together. Bounded on purpose: a
    #: caller is holding the request open, and the fallback — ``needs_ocr``, routed to a human
    #: — is where an unreadable document was going anyway.
    remote_ocr_timeout_seconds: float = 30.0
    #: Delay between polls of the ``Operation-Location`` URL.
    remote_ocr_poll_interval_seconds: float = 0.5
    #: Hard cap on polls, independent of the clock. Two bounds rather than one because a
    #: provider that answers instantly with a non-terminal status would otherwise be polled as
    #: fast as the loop runs for the whole timeout.
    remote_ocr_max_polls: int = 60

    # ---- Caps ---------------------------------------------------------------
    max_bytes: int = 32 * 1024 * 1024
    max_seconds: float = 20.0
    max_pages: int = 200
    max_ocr_pages: int = 10
    ocr_dpi: int = 300

    @model_validator(mode="after")
    def _check(self) -> IngestSettings:
        """Refuse the two configurations that cannot mean anything.

        Loud rather than silent, and only for mistakes that *can only* be mistakes:

        * **Both providers on.** There is no sensible precedence. Preferring local would make
          an operator who deliberately enabled a remote recogniser get the local one silently;
          preferring remote would put documents on the wire on a deployment that had asked for
          in-process recognition. Neither is a default anyone should discover from a metric.
        * **A remote provider id that is not a remote provider.** ``rapidocr`` here is not a
          typo worth guessing about — the field selects which third party receives documents.
        * **A trust boundary that is not one of the two values.** Anything unrecognised would
          have to fall back to something, and both fallbacks are wrong: to ``external`` and a
          deployment that declared ``on-premises`` with a hyphen gets an alarming page it
          thought it had answered; to ``on_premises`` and a typo silently produces the
          reassuring reading. Refusing is the only option that cannot mislead.

        A *missing endpoint or key* deliberately does not raise. That is usually a secret that
        has not landed yet, and taking the whole service down for it would trade degraded
        ingestion for a total outage of classification, which still works. It surfaces through
        :meth:`remote_ocr_problem`, on ``/readyz``, and as a ``needs_ocr`` outcome.

        Raises:
            ValueError: On either configuration above.
        """
        if self.local_ocr_enabled and self.remote_ocr_enabled:
            raise ValueError(
                "local_ocr_enabled and remote_ocr_enabled are both true. Choose one "
                "recogniser: local keeps every document in this process, remote transmits "
                "unclassified documents to a third party. There is no precedence between "
                "them that would not silently override one of the two decisions."
            )
        if self.remote_ocr_enabled and not is_network_provider(self.remote_ocr_provider):
            raise ValueError(
                f"remote_ocr_provider={self.remote_ocr_provider!r} is not a network OCR "
                f"provider; supported: {', '.join(sorted(NETWORK_ENGINES))}"
            )
        if self.remote_ocr_trust_boundary.strip().lower() not in TRUST_BOUNDARIES:
            raise ValueError(
                f"remote_ocr_trust_boundary={self.remote_ocr_trust_boundary!r} is not a trust "
                f"boundary; supported: {', '.join(sorted(TRUST_BOUNDARIES))}. This field says "
                "where the remote OCR endpoint sits relative to this deployment's boundary; a "
                "value that is not one of the two cannot be guessed at, because both guesses "
                "misreport a disclosure."
            )
        return self

    def active_provider(self) -> str:
        """The recogniser that would actually run here, by name, or ``"none"``.

        One name, because :meth:`_check` has already refused the configuration where two could
        be. This is what a caller's ``ocr_provider`` pin is compared against — see
        :class:`dce.ingest.pipeline.IngestOptions` — and what ``/readyz`` marks as the
        available provider.

        Returns:
            ``rapidocr`` | ``tesseract`` | ``azure_read`` | ``azure_layout`` | ``none``.
        """
        if self.remote_ocr_enabled:
            return self.remote_ocr_provider.strip().lower()
        if self.local_ocr_enabled:
            return self.local_ocr_engine.strip().lower()
        return "none"

    def remote_ocr_endpoint(self) -> str:
        """The endpoint the configured remote provider would send documents to.

        Returns:
            The base URL, or ``""`` when remote OCR is off or unconfigured.
        """
        if not self.remote_ocr_enabled:
            return ""
        if self.remote_ocr_provider == "azure_read":
            return self.azure_read_endpoint.strip()
        return self.azure_di_endpoint.strip()

    def remote_ocr_endpoint_host(self) -> str:
        """Host of :meth:`remote_ocr_endpoint` — what ``/readyz`` shows an operator.

        The host, not the whole URL: it is the part that answers "who receives our customers'
        unclassified documents", and it cannot carry a key in a query string.
        """
        endpoint = self.remote_ocr_endpoint()
        if not endpoint:
            return ""
        parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
        return parsed.hostname or ""

    def trust_boundary(self) -> str:
        """The declared boundary, normalised: ``external`` or ``on_premises``.

        Normalised here rather than at every call site so that ``On_Premises`` in a compose
        file and ``on_premises`` in a Helm chart cannot report differently.
        """
        return self.remote_ocr_trust_boundary.strip().lower()

    def trust_boundary_declared(self) -> bool:
        """Whether an operator *set* the boundary, as opposed to inheriting the default.

        Reported because "we chose external" and "nobody said" are different claims about the
        same value, and only one of them is a decision. An auditor reading ``external`` is
        entitled to know which they are looking at.
        """
        return "remote_ocr_trust_boundary" in self.model_fields_set

    def trust_boundary_attribution(self) -> str:
        """Who says where the endpoint sits, and that this service did not check.

        This is the whole point of the field. The value alone would be a bare assertion in the
        service's own voice; the attribution keeps it a claim with an owner, so a page that
        stops shouting is still a page an auditor can hold somebody to.

        Returns:
            One sentence, or ``""`` when no remote provider is configured and the question
            therefore does not arise.
        """
        if not self.remote_ocr_enabled:
            return ""
        host = self.remote_ocr_endpoint_host() or "the configured endpoint"
        if self.trust_boundary() == TRUST_BOUNDARY_ON_PREMISES:
            return (
                f"this deployment declares {host} is inside its own trust boundary "
                "(DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY=on_premises). That is the operator's "
                "declaration about their own network; this service has not verified it and "
                "cannot — a hostname is not evidence. What it does verify: the document does "
                "leave this process, to that host, before its doctype is known."
            )
        if self.trust_boundary_declared():
            return (
                f"this deployment declares {host} is outside its own trust boundary "
                "(DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY=external)."
            )
        return (
            f"no trust boundary has been declared for {host}, so it is reported as outside "
            "this deployment's boundary — DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY defaults to "
            "'external' precisely so that saying nothing cannot produce the reassuring answer."
        )

    def remote_ocr_problem(self) -> str:
        """Why the enabled remote provider cannot work, or ``""`` when it can.

        Reported rather than raised, for the reason given in :meth:`_check`.
        """
        if not self.remote_ocr_enabled:
            return ""
        if not self.remote_ocr_endpoint():
            provider = self.remote_ocr_provider
            variable = (
                "DCE_INGEST_AZURE_READ_ENDPOINT"
                if provider == "azure_read"
                else "DCE_INGEST_AZURE_DI_ENDPOINT"
            )
            return (
                f"remote_ocr_enabled=true with provider {provider!r} but {variable} is empty; "
                "images and scanned PDFs will return needs_ocr"
            )
        return ""

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


__all__ = [
    "TRUST_BOUNDARIES",
    "TRUST_BOUNDARY_EXTERNAL",
    "TRUST_BOUNDARY_ON_PREMISES",
    "IngestSettings",
    "get_ingest_settings",
]
