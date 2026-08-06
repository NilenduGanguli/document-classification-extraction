# syntax=docker/dockerfile:1.7
#
# The BERT checkpoint is deliberately NOT in this image. It is ~1.3 GB, the tier is off by
# default, and baking it in would make every deployment pay for a feature most never enable —
# so it is mounted read-only at /models instead (see docker-compose.yml).
#
# What IS in the image is the whole classification path, because that is the point: an
# unclassified document is classified by this container, on this CPU, with no network.

# ---------------------------------------------------------------------------
# Build: resolve and install into a self-contained venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src
# pyproject declares the readme, so it has to be present for the build to resolve metadata.
COPY pyproject.toml README.md ./
COPY dce ./dce

RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="document-classification-extraction" \
      org.opencontainers.image.description="In-process document classification and field extraction" \
      org.opencontainers.image.source="https://github.com/NilenduGanguli/document-classification-extraction"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8200 \
    BERT_ENABLED=false \
    ALLOW_PRECLASSIFICATION_EGRESS=false

RUN groupadd --gid 10001 dce \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin dce \
    && mkdir -p /app /models /app/data \
    && chown -R dce:dce /app /models

COPY --from=build --chown=dce:dce /opt/venv /opt/venv

WORKDIR /app
COPY --chown=dce:dce dce ./dce

USER dce
EXPOSE 8200

# Loopback only. This is the container asking itself whether it is alive; it is not egress,
# and it is not on the classification path — which is why it uses the stdlib rather than
# adding an HTTP client to the runtime dependencies.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "dce.api.app:app", "--host", "0.0.0.0", "--port", "8200"]
