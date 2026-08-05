FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.txt ./
COPY hermes_cloud ./hermes_cloud
COPY control_plane ./control_plane
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_DATA_DIR=/data \
    HERMES_DB=/data/hermes.db \
    HERMES_HOUSEHOLD=/data/household.toml \
    HERMES_ACTIVATION_STATE=/data/runtime-activation.json \
    HERMES_REQUIRE_MANIFEST=1 \
    HERMES_RUNTIME_HOST=0.0.0.0 \
    HERMES_RUNTIME_PORT=8080
RUN useradd --create-home --uid 10001 abrolia && mkdir -p /data && chown abrolia:abrolia /data
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER abrolia
WORKDIR /home/abrolia
EXPOSE 8080
CMD ["python", "-m", "hermes_cloud.runtime.service"]
