# syntax=docker/dockerfile:1.7
#
# The BERT checkpoint is deliberately NOT in this image. It is ~1.3 GB, the tier is off by
# default, and baking it in would make every deployment pay for a feature most never enable —
# so it is mounted read-only at /models instead (see docker-compose.yml).
#
# Neither is an HTTP client. That is the strongest form the invariant takes: the default image
# cannot make a network call about a document, because the capability is not installed, and
# that is checkable on the shipped artifact rather than in the source tree:
#
#   docker run --rm --entrypoint python dce:latest -c \
#     "import importlib.util as u; print({m: bool(u.find_spec(m)) for m in ('httpx','requests','aiohttp','openai','azure','boto3')})"
#
# The post-classification tiers (T2 Azure prebuilt, T3 queryFields, T4 LLM) import httpx
# INSIDE the call that needs it, so a build without one degrades to "tier unavailable" in the
# /process response instead of failing at import. To use them, install it deliberately:
#
#   docker build --build-arg EXTRA_PACKAGES="httpx>=0.27" .
#
# and understand what you traded: from that build on, the guarantee rests on dce/egress.py,
# the abstain rule and the socket tripwire test rather than on the absence of the capability.
#
# What IS in the image is the whole classification path, because that is the point: an
# unclassified document is classified by this container, on this CPU, with no network.

# ---------------------------------------------------------------------------
# UI: compile the console into a static bundle
# ---------------------------------------------------------------------------
# The console is served by this same process out of frontend/dist, so it is compiled here
# rather than trusted from the checkout. Node exists only in this stage: the runtime image
# carries the built assets and no JavaScript toolchain at all.
#
# The bundle is entirely self-contained — no CDN, no web font, no remote anything. A service
# whose argument is that documents do not leave must not ship a console that phones out, and
# building it here keeps that checkable on the artifact rather than in the source tree.
FROM node:22-slim AS ui

WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Build: resolve and install into a self-contained venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

#: Extra runtime packages, empty by default. This is where an HTTP client goes when a
#: deployment enables T2/T3/T4 — as a deliberate build argument, not as a base dependency
#: everybody carries.
ARG EXTRA_PACKAGES=""

WORKDIR /src
# pyproject declares the readme, so it has to be present for the build to resolve metadata.
COPY pyproject.toml README.md ./
COPY dce ./dce

# The `pdf` extra ships in the image; the other extras do not.
#
# It is the one extra that is not a policy choice. PDF is the format a KYC pipeline receives more
# than all the others combined, and without it the service's first-contact behaviour is to refuse
# the most ordinary document there is — a reviewer drops a PDF on the console and gets an ingest
# error from a build that reports itself ready. That is a worse default than the disk cost.
#
# It also stays honest about the invariant: PyMuPDF parses bytes in-process and opens no socket,
# so this changes what the service can READ, never where anything goes. The extras deliberately
# left out are the ones that change that: `ocr` (a recogniser and its weights), `bert` (torch),
# and whatever EXTRA_PACKAGES an operator adds to switch on the egress tiers.
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache '.[pdf]' ${EXTRA_PACKAGES}

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="document-classification-extraction" \
      org.opencontainers.image.description="In-process document classification and field extraction" \
      org.opencontainers.image.source="https://github.com/NilenduGanguli/document-classification-extraction"

# The posture, baked in so `docker inspect` answers "what can this container do?" without a
# trip to the config. Every tier that can leave the process is off; an operator turns one on
# deliberately, per deployment, and /readyz then reports it.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8200 \
    BERT_ENABLED=false \
    ALLOW_PRECLASSIFICATION_EGRESS=false \
    T2_ENABLED=false \
    T3_ENABLED=false \
    T4_ENABLED=false \
    REVIEW_QUEUE_BACKEND=memory \
    REVIEW_QUEUE_PATH=/app/data/review_queue.json

RUN groupadd --gid 10001 dce \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dce \
    && mkdir -p /app /models /app/data \
    && chown -R dce:dce /app /models

COPY --from=build --chown=dce:dce /opt/venv /opt/venv

WORKDIR /app
COPY --chown=dce:dce dce ./dce

# The console, at the path dce/api/app.py resolves relative to the package (/app/frontend/dist).
# Nothing imports it and nothing depends on it: if this COPY is removed the API, the probes and
# the OpenAPI docs all still serve, and "/" says how to build the bundle.
COPY --from=ui --chown=dce:dce /ui/dist ./frontend/dist

USER dce
EXPOSE 8200

# Loopback only. This is the container asking itself whether it is alive; it is not egress,
# and it is not on the classification path — which is why it uses the stdlib rather than
# adding an HTTP client to the runtime dependencies.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "dce.api.app:app", "--host", "0.0.0.0", "--port", "8200"]
