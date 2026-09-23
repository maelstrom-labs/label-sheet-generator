# Single-stage-built, multi-stage-shipped image for free-tier hosting.
# Builds a wheel in a fat layer, installs it into a slim runtime layer, and
# runs as a non-root user with no build toolchain present at runtime.

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN pip install --no-cache-dir "hatchling>=1.27.0"

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m hatchling build -t wheel

# Resolve dependencies into a prefix the runtime layer copies wholesale, so the
# final image never contains pip's cache or a compiler.
RUN pip install --no-cache-dir --prefix=/install "$(ls dist/*.whl)[web]"

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Label Sheet Generator" \
      org.opencontainers.image.description="Print-ready label sheet PDFs from JSON templates." \
      org.opencontainers.image.source="https://github.com/maelstrom-labs/label-sheet-generator" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    LSG_HOST=0.0.0.0 \
    PORT=8000

COPY --from=build /install /usr/local

# System user with no shell and no home to write to; the app needs neither.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin --uid 10001 labels
USER 10001

EXPOSE 8000

# Probes the always-cheap liveness endpoint, which answers even when every
# render worker is busy, so a loaded container is not reported as dead.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/livez', timeout=2).status==200 else 1)"

# Honour $PORT so Render, Fly, Koyeb and Hugging Face Spaces all work unchanged.
# label-sheet serve reads $PORT and $LSG_HOST and installs the app's own JSON
# logging, so uvicorn's default config never has to be disabled.
CMD ["sh", "-c", "exec label-sheet serve"]
