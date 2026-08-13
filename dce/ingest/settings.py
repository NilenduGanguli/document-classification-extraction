"""Ingestion settings, kept separate from :mod:`dce.config` on purpose.

``dce.config.Settings`` governs the classifier and the extraction tiers. Ingestion is a
different concern with a different owner — it is about *what a caller may upload* and *how
much of one request's work the process will do* — and folding a dozen parser caps into the
settings object a control reviewer reads for the egress invariant would bury the invariant.

Everything here is read from the environment with the ``DCE_INGEST_`` prefix, e.g.
``DCE_INGEST_LOCAL_OCR_ENABLED=true``.

**Local OCR is off by default and that is the whole design.** See :mod:`dce.ingest`.

**The OCR service providers are also off by default, and they are a different kind of off.**
Local OCR being on is an accuracy claim. An OCR service being on is an architectural fact
about how this deployment reads an image: the document is handed to another process, over a
call, before anybody knows what it is. The two therefore get two settings rather than one
overloaded flag — calling a service provider "local OCR" would make the word "local" a lie in
the one place an operator is most likely to read it quickly.

**Whose network that endpoint is on is DECLARED, not inferred.** ``ocr.internal.corp`` and
``x.cognitiveservices.azure.com`` are indistinguishable to this process, so
``ocr_service_trust_boundary`` is how a deployment says which it has, the value is reported
with its provenance, and the code default is the cautious one.

--------------------------------------------------------------------------------
THE OLD NAMES STILL WORK
--------------------------------------------------------------------------------
These settings were once called ``remote_ocr_*`` (``DCE_INGEST_REMOTE_OCR_*``), language that
described a disclosure rather than an architecture. Every one of them is still accepted as an
alias, so an existing deployment does not break on upgrade; :data:`LEGACY_ENV_ALIASES` maps
old to new and :func:`legacy_env_aliases_in_use` is what the boot log reads to name the ones
still in play.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dce.ingest.limits import IngestLimits
from dce.ingest.ocr import ENGINES, SERVICE_ENGINES, is_service_provider

#: The endpoint is outside this deployment's trust boundary. **The code default**, because a
#: deployment that has declared nothing must get the cautious reading rather than the
#: reassuring one: silence is not a statement that the host is internal.
TRUST_BOUNDARY_EXTERNAL = "external"
#: The deployment declares the endpoint is inside its own network — an OCR pod in the
#: operator's own cluster, an Azure service on a private endpoint, a host on their own LAN.
TRUST_BOUNDARY_ON_PREMISES = "on_premises"
#: The complete set. A third value is a typo, and a typo in this field would otherwise decide
#: how the posture reads.
TRUST_BOUNDARIES = frozenset({TRUST_BOUNDARY_EXTERNAL, TRUST_BOUNDARY_ON_PREMISES})

#: Old environment variable → the name it is now called. Read by
#: :func:`legacy_env_aliases_in_use`; every one of these is still honoured.
LEGACY_ENV_ALIASES: dict[str, str] = {
    "DCE_INGEST_REMOTE_OCR_ENABLED": "DCE_INGEST_OCR_SERVICE_ENABLED",
    "DCE_INGEST_REMOTE_OCR_PROVIDER": "DCE_INGEST_OCR_SERVICE_PROVIDER",
    "DCE_INGEST_REMOTE_OCR_TRUST_BOUNDARY": "DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY",
    "DCE_INGEST_REMOTE_OCR_TIMEOUT_SECONDS": "DCE_INGEST_OCR_SERVICE_TIMEOUT_SECONDS",
    "DCE_INGEST_REMOTE_OCR_POLL_INTERVAL_SECONDS": (
        "DCE_INGEST_OCR_SERVICE_POLL_INTERVAL_SECONDS"
    ),
    "DCE_INGEST_REMOTE_OCR_MAX_POLLS": "DCE_INGEST_OCR_SERVICE_MAX_POLLS",
}


def _aliases(name: str, legacy: str) -> AliasChoices:
    """Env names for one renamed field: the new one first, the old one still accepted.

    Both carry the ``DCE_INGEST_`` prefix explicitly, because declaring a ``validation_alias``
    turns off the automatic prefixing that the rest of the model relies on.
    """
    return AliasChoices(f"dce_ingest_{name}", f"dce_ingest_{legacy}")


def legacy_env_aliases_in_use(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Deprecated ``DCE_INGEST_REMOTE_OCR_*`` variables set in this environment.

    Returns:
        ``{old_name: new_name}`` for each legacy variable actually present, so a boot log can
        name what to rename. Empty on a deployment using the current names.
    """
    env = os.environ if environ is None else environ
    present = {key.upper() for key in env}
    return {old: new for old, new in LEGACY_ENV_ALIASES.items() if old in present}


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="dce_ingest_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # The renamed fields carry an explicit validation_alias, which would otherwise make
        # them settable *only* by alias. Keeping the field name valid keeps every call site
        # and test that constructs IngestSettings(...) directly working.
        populate_by_name=True,
    )

    # ---- LOCAL OCR: OFF, AND OPTIONAL ---------------------------------------
    #: Turn on in-process OCR for images and scanned PDFs. Off by default so the standard
    #: build has no OCR dependency at all and an image gets the honest ``needs_ocr`` answer
    #: rather than a guess. Turning it on adds no call to another host — the engines below run
    #: in this process — but it does introduce an accuracy claim, which is why it is a decision.
    local_ocr_enabled: bool = False
    #: ``rapidocr`` (ONNX, genuinely in-process) or ``tesseract`` (a local subprocess).
    local_ocr_engine: str = "rapidocr"
    #: Tesseract language packs, e.g. ``eng+hin``. Ignored by RapidOCR.
    local_ocr_languages: str = "eng"

    # ---- OCR SERVICE: A CALL TO ANOTHER HOST, BEFORE THE DOCTYPE IS KNOWN ---
    # Everything in this block is off/empty by default, and a deployment that wants no call at
    # all before classification gets it by doing nothing.
    #
    # Turning it on means: this service hands documents that have NOT been classified to the
    # endpoint configured below, to be read. Whose network that endpoint is on is declared in
    # `ocr_service_trust_boundary` and is not something this code can work out.
    #
    # The invariant setting `dce.config.allow_preclassification_egress` is NOT involved and
    # must stay False: that one lets anything out during classification, and it is what governs
    # the paid post-classification tiers. This one permits a single named call site
    # (dce.ingest.ocr_service), guarded by dce.egress.assert_ocr_egress_permitted, and nothing
    # else.

    #: Recognise images and scanned PDFs by calling a configured OCR service. Off by default.
    #: ``/readyz`` reports the provider and endpoint host of a deployment that has this on,
    #: whether or not anybody asked.
    ocr_service_enabled: bool = Field(
        default=False, validation_alias=_aliases("ocr_service_enabled", "remote_ocr_enabled")
    )
    #: The service provider used when a request does not name one: ``azure_read`` (Vision Read
    #: v3.2) or ``azure_layout`` (Document Intelligence v4.0 ``prebuilt-layout``). Prefer
    #: ``azure_layout``: Read predicts no paragraph roles, so a Read payload can never satisfy
    #: a title-gated decisive anchor and classifies with strictly less evidence — see
    #: :func:`dce.adapters.from_azure_read`.
    ocr_service_provider: str = Field(
        default="azure_layout",
        validation_alias=_aliases("ocr_service_provider", "remote_ocr_provider"),
    )
    #: Which recogniser runs when a request names none, on a deployment that has configured
    #: **both** an in-process engine and an OCR service. Required in that case and empty
    #: otherwise: there is no defensible precedence between the two, so the deployment states
    #: one rather than discovering the code's preference from a metric.
    ocr_default_provider: str = ""
    #: Base URL of the Azure Document Intelligence resource, e.g.
    #: ``https://<resource>.cognitiveservices.azure.com``, or an in-network service that speaks
    #: the same protocol. Empty by default: with no endpoint there is nowhere for a document to
    #: go, so the absence of configuration is itself a control. Deliberately a *separate*
    #: setting from ``dce.config.azure_di_endpoint``, which configures the post-classification
    #: T2/T3 tiers — reading a document before its type is known and sending it to a paid
    #: vendor tier after are two different authorisations, and one variable granting both would
    #: let enabling T2 quietly enable this.
    azure_di_endpoint: str = ""
    #: Azure DI key. Inject it from a secret store at runtime; never bake it into an image.
    azure_di_key: str = ""
    #: Pinned, not "latest": a newer API version changes field names under a signed-off
    #: pipeline.
    azure_di_api_version: str = "2024-11-30"
    #: The DI model to analyse with. ``prebuilt-layout`` is the only one this path maps.
    azure_di_model: str = "prebuilt-layout"
    #: Base URL of the Azure AI Vision resource for Read v3.2. Configuring it makes
    #: ``azure_read`` selectable alongside ``azure_layout``.
    azure_read_endpoint: str = ""
    azure_read_key: str = ""
    #: The version segment of Read's URL path (``/vision/{v}/read/analyze``). Read is frozen
    #: at v3.2; it is configurable so a resource behind a gateway that rewrites the path can
    #: still be pointed at, not so that a newer version can be guessed at.
    azure_read_api_version: str = "v3.2"

    # ---- WHERE THAT ENDPOINT SITS: A DECLARATION, NOT AN INFERENCE ----------
    #: ``external`` (default) or ``on_premises`` — where the deployment says the OCR service
    #: endpoint sits relative to its own network.
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
    #: Declaring ``on_premises`` changes how the posture READS — from "documents are
    #: transmitted outside this deployment's boundary" to "this deployment reads images via a
    #: service on its own network" — and changes nothing about what the process DOES. The
    #: document is still handed to another process, still named on ``/readyz``, and
    #: ``ocr.network`` and ``egress.preclassification_ocr`` are still true, because those are
    #: statements about *this* process, not about who owns the far end.
    #:
    #: The default is the cautious reading on purpose. A deployment that forgets to declare
    #: gets "external", never the reassuring answer by omission.
    ocr_service_trust_boundary: str = Field(
        default=TRUST_BOUNDARY_EXTERNAL,
        validation_alias=_aliases(
            "ocr_service_trust_boundary", "remote_ocr_trust_boundary"
        ),
    )

    #: Wall clock for one analyse, submit and polling together. Bounded on purpose: a caller is
    #: holding the request open, and the fallback — ``needs_ocr``, routed to a human — is where
    #: an unreadable document was going anyway.
    ocr_service_timeout_seconds: float = Field(
        default=30.0,
        validation_alias=_aliases(
            "ocr_service_timeout_seconds", "remote_ocr_timeout_seconds"
        ),
    )
    #: Delay between polls of the ``Operation-Location`` URL.
    ocr_service_poll_interval_seconds: float = Field(
        default=0.5,
        validation_alias=_aliases(
            "ocr_service_poll_interval_seconds", "remote_ocr_poll_interval_seconds"
        ),
    )
    #: Hard cap on polls, independent of the clock. Two bounds rather than one because a
    #: provider that answers instantly with a non-terminal status would otherwise be polled as
    #: fast as the loop runs for the whole timeout.
    ocr_service_max_polls: int = Field(
        default=60,
        validation_alias=_aliases("ocr_service_max_polls", "remote_ocr_max_polls"),
    )

    # ---- Caps ---------------------------------------------------------------
    max_bytes: int = 32 * 1024 * 1024
    max_seconds: float = 20.0
    max_pages: int = 200
    max_ocr_pages: int = 10
    ocr_dpi: int = 300

    @model_validator(mode="after")
    def _check(self) -> IngestSettings:
        """Refuse the configurations that cannot mean anything.

        Loud rather than silent, and only for mistakes that *can only* be mistakes:

        * **Both kinds of recogniser on, with no default named.** There is no sensible
          precedence. Preferring the in-process engine would make an operator who deliberately
          configured a service get the local one silently; preferring the service would send
          documents to another host on a deployment that had asked for in-process recognition.
          Neither is a default anyone should discover from a metric — so a deployment that
          wants both available says which one runs when a request names none.
        * **A default that is not configured here.** ``ocr_default_provider`` selects among the
          recognisers this deployment set up; naming one it did not is a typo that would
          otherwise decide where documents are read.
        * **A service provider id that is not a service provider.** ``rapidocr`` here is not a
          typo worth guessing about — the field selects which endpoint receives documents.
        * **A trust boundary that is not one of the two values.** Anything unrecognised would
          have to fall back to something, and both fallbacks are wrong: to ``external`` and a
          deployment that declared ``on-premises`` with a hyphen gets an alarming page it
          thought it had answered; to ``on_premises`` and a typo silently produces the
          reassuring reading. Refusing is the only option that cannot mislead.

        A *missing endpoint or key* deliberately does not raise. That is usually a secret that
        has not landed yet, and taking the whole service down for it would trade degraded
        ingestion for a total outage of classification, which still works. It surfaces through
        :meth:`ocr_service_problem`, on ``/readyz``, and as a ``needs_ocr`` outcome.

        Raises:
            ValueError: On any configuration above.
        """
        if self.ocr_service_enabled and not is_service_provider(self.ocr_service_provider):
            raise ValueError(
                f"ocr_service_provider={self.ocr_service_provider!r} is not an OCR service "
                f"provider; supported: {', '.join(sorted(SERVICE_ENGINES))}"
            )
        if self.ocr_service_trust_boundary.strip().lower() not in TRUST_BOUNDARIES:
            raise ValueError(
                f"ocr_service_trust_boundary={self.ocr_service_trust_boundary!r} is not a "
                f"trust boundary; supported: {', '.join(sorted(TRUST_BOUNDARIES))}. This field "
                "says where the OCR service endpoint sits relative to this deployment's own "
                "network; a value that is not one of the two cannot be guessed at, because "
                "both guesses misreport it."
            )
        configured = self.configured_providers()
        declared = self.ocr_default_provider.strip().lower()
        if declared and declared not in configured:
            raise ValueError(
                f"ocr_default_provider={self.ocr_default_provider!r} is not configured on this "
                f"deployment; configured: {', '.join(configured) or 'none'}. The default names "
                "one of the recognisers this deployment set up — it cannot switch one on."
            )
        if self.local_ocr_enabled and self.ocr_service_enabled and not declared:
            raise ValueError(
                "local_ocr_enabled and ocr_service_enabled are both true but "
                "ocr_default_provider is empty. Both may be configured — a request can then "
                "choose between them with ingest.ocr_provider — but one of them runs when a "
                "request names none, and there is no precedence between 'read it in this "
                "process' and 'call the OCR service' that would not silently override one of "
                f"the two decisions. Set ocr_default_provider to one of: "
                f"{', '.join(configured) or 'none'}."
            )
        return self

    # ---- what this deployment can do ---------------------------------------
    def configured_providers(self) -> tuple[str, ...]:
        """Every recogniser a request may select here, in a stable order.

        This is the list the ``ocr_provider`` pin is checked against and the set ``/readyz``
        marks available. A provider is in it when the deployment configured it — not when it
        merely exists in the build — so the pin can choose among these and can never add one.

        The service side yields one entry per endpoint that is actually set, which is how a
        single deployment offers ``azure_layout`` and ``azure_read`` side by side. The
        configured default provider is always present even when its endpoint is missing, so
        that a half-landed secret shows up as a named provider with a problem rather than
        vanishing from the list.
        """
        out: list[str] = []
        if self.local_ocr_enabled:
            out.append(self.local_ocr_engine.strip().lower())
        if self.ocr_service_enabled:
            named = self.ocr_service_provider.strip().lower()
            for provider, endpoint in (
                ("azure_layout", self.azure_di_endpoint),
                ("azure_read", self.azure_read_endpoint),
            ):
                if endpoint.strip() and provider not in out:
                    out.append(provider)
            if named not in out:
                out.append(named)
        return tuple(out)

    def default_provider(self) -> str:
        """The recogniser that runs when a request names none, or ``"none"``.

        This is what ``/readyz`` reports as ``ocr.provider`` and what an unpinned request gets.

        Returns:
            ``rapidocr`` | ``tesseract`` | ``azure_read`` | ``azure_layout`` | ``none``.
        """
        declared = self.ocr_default_provider.strip().lower()
        if declared:
            return declared
        if self.ocr_service_enabled:
            return self.ocr_service_provider.strip().lower()
        if self.local_ocr_enabled:
            return self.local_ocr_engine.strip().lower()
        return "none"

    def is_configured(self, provider: str) -> bool:
        """Whether ``provider`` is one a request may select on this deployment."""
        return (provider or "").strip().lower() in self.configured_providers()

    def provider_endpoint(self, provider: str) -> str:
        """The endpoint ``provider`` would send documents to, or ``""``.

        ``""`` for an in-process engine, which sends nothing anywhere, and for a service
        provider whose endpoint has not been configured.
        """
        key = (provider or "").strip().lower()
        if key == "azure_read":
            return self.azure_read_endpoint.strip()
        if key == "azure_layout":
            return self.azure_di_endpoint.strip()
        return ""

    def provider_endpoint_host(self, provider: str) -> str:
        """Host of :meth:`provider_endpoint` — what ``/readyz`` shows an operator.

        The host, not the whole URL: it is the part that answers "which service reads our
        documents", and it cannot carry a key in a query string.
        """
        return _host_of(self.provider_endpoint(provider))

    def provider_problem(self, provider: str) -> str:
        """Why ``provider`` cannot work here, or ``""`` when it can.

        Reported rather than raised, for the reason given in :meth:`_check`. Installation of an
        in-process engine's optional extra is checked by the caller, which knows whether it is
        answering a readiness probe or running a request.
        """
        key = (provider or "").strip().lower()
        if not self.is_configured(key):
            return ""
        if key not in SERVICE_ENGINES:
            return ""
        if not self.provider_endpoint(key):
            variable = (
                "DCE_INGEST_AZURE_READ_ENDPOINT"
                if key == "azure_read"
                else "DCE_INGEST_AZURE_DI_ENDPOINT"
            )
            return (
                f"ocr_service_enabled=true with provider {key!r} but {variable} is empty; "
                "images and scanned PDFs will return needs_ocr"
            )
        return ""

    # ---- the default provider, for the surfaces that report one -------------
    def ocr_service_endpoint(self) -> str:
        """The endpoint the default service provider would send documents to.

        Returns:
            The base URL, or ``""`` when no OCR service is configured or the default
            recogniser is an in-process engine.
        """
        if not self.ocr_service_enabled:
            return ""
        return self.provider_endpoint(self.default_provider())

    def ocr_service_endpoint_host(self) -> str:
        """Host of :meth:`ocr_service_endpoint` — what ``/readyz`` shows an operator."""
        return _host_of(self.ocr_service_endpoint())

    def ocr_service_problem(self) -> str:
        """Why the default service provider cannot work, or ``""`` when it can."""
        if not self.ocr_service_enabled:
            return ""
        return self.provider_problem(self.default_provider())

    def service_providers(self) -> tuple[str, ...]:
        """The configured recognisers that are reached by a call to another host."""
        return tuple(p for p in self.configured_providers() if p in SERVICE_ENGINES)

    def local_providers(self) -> tuple[str, ...]:
        """The configured recognisers that run inside this process."""
        return tuple(p for p in self.configured_providers() if p in ENGINES)

    # ---- the declared boundary ----------------------------------------------
    def trust_boundary(self) -> str:
        """The declared boundary, normalised: ``external`` or ``on_premises``.

        Normalised here rather than at every call site so that ``On_Premises`` in a compose
        file and ``on_premises`` in a Helm chart cannot report differently.
        """
        return self.ocr_service_trust_boundary.strip().lower()

    def trust_boundary_declared(self) -> bool:
        """Whether an operator *set* the boundary, as opposed to inheriting the default.

        Reported because "we chose external" and "nobody said" are different claims about the
        same value, and only one of them is a decision. An auditor reading ``external`` is
        entitled to know which they are looking at.
        """
        return "ocr_service_trust_boundary" in self.model_fields_set

    def trust_boundary_attribution(self) -> str:
        """Who says where the endpoint sits, and that this service did not check.

        This is the whole point of the field. Under ``on_premises`` it reads as configuration —
        this is how this deployment reads an image, and here is who says the endpoint is
        theirs. Under ``external``, declared or defaulted, it stays cautious, because a
        deployment that has declared nothing must not get the reassuring reading.

        Returns:
            One sentence, or ``""`` when no OCR service is configured and the question
            therefore does not arise.
        """
        if not self.ocr_service_enabled:
            return ""
        provider = self.default_provider()
        host = self.ocr_service_endpoint_host() or "the configured endpoint"
        if self.trust_boundary() == TRUST_BOUNDARY_ON_PREMISES:
            return (
                f"this deployment declares {host} is inside its own network "
                "(DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY=on_premises), so documents read there "
                "stay within the operator's own infrastructure. That is the operator's "
                "declaration about their own network; this service records it and has not "
                "verified it — a hostname is not evidence either way. What it does state as "
                f"configuration: images are read by {provider} at that host, over a call from "
                "this process, before the doctype is known."
            )
        if self.trust_boundary_declared():
            return (
                f"this deployment declares {host} is outside its own trust boundary "
                "(DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY=external), so documents sent there to "
                "be read leave this deployment's control before their doctype is known."
            )
        return (
            f"no trust boundary has been declared for {host}, so it is reported as outside "
            "this deployment's boundary — DCE_INGEST_OCR_SERVICE_TRUST_BOUNDARY defaults to "
            "'external' precisely so that saying nothing cannot produce the reassuring answer."
        )

    def limits(self) -> IngestLimits:
        """The :class:`~dce.ingest.limits.IngestLimits` this deployment is configured for."""
        return IngestLimits(
            max_bytes=self.max_bytes,
            max_seconds=self.max_seconds,
            max_pages=self.max_pages,
            max_ocr_pages=self.max_ocr_pages,
            ocr_dpi=self.ocr_dpi,
        )


def _host_of(endpoint: str) -> str:
    """Host of a base URL, tolerating one written without a scheme."""
    if not endpoint:
        return ""
    parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    return parsed.hostname or ""


@lru_cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()


__all__ = [
    "LEGACY_ENV_ALIASES",
    "TRUST_BOUNDARIES",
    "TRUST_BOUNDARY_EXTERNAL",
    "TRUST_BOUNDARY_ON_PREMISES",
    "IngestSettings",
    "get_ingest_settings",
    "legacy_env_aliases_in_use",
]
