FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.txt ./
COPY control_plane ./control_plane
COPY hermes_cloud ./hermes_cloud
COPY web ./web
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim

ARG TARGETARCH
ARG FLYCTL_VERSION=0.4.58
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ABROLIA_CONTROL_PLANE_DB=/data/control-plane.db \
    ABROLIA_SYNTHETIC_ONLY=1 \
    REAL_FAMILY_DATA_ENABLED=0
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) fly_arch=x86_64; fly_sha=d97b7ed076923ccdc5a5bd49feb759b6c0ae651dc517ebb059ee33fb5debc385 ;; \
      arm64) fly_arch=arm64; fly_sha=dea794417ad362e460b55bdee1afea356e62ac73440d997a3039b35cc4191450 ;; \
      *) echo "unsupported Fly CLI architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install --no-install-recommends -y ca-certificates curl; \
    archive="flyctl_${FLYCTL_VERSION}_Linux_${fly_arch}.tar.gz"; \
    curl -fsSL "https://github.com/superfly/flyctl/releases/download/v${FLYCTL_VERSION}/${archive}" -o "/tmp/${archive}"; \
    echo "${fly_sha}  /tmp/${archive}" | sha256sum -c -; \
    tar -xzf "/tmp/${archive}" -C /tmp flyctl; \
    install -m 0755 /tmp/flyctl /usr/local/bin/fly; \
    ln -s /usr/local/bin/fly /usr/local/bin/flyctl; \
    rm -f "/tmp/${archive}" /tmp/flyctl; \
    rm -rf /var/lib/apt/lists/*; \
    useradd --create-home --uid 10001 abrolia; \
    mkdir -p /data; \
    chown abrolia:abrolia /data
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER abrolia
WORKDIR /home/abrolia
EXPOSE 8080
# Migrate-on-start with backup-before-migrate: a pending migration is applied
# only after an authenticated snapshot of the volume database succeeds, and any
# failure stops the container before `serve` can open the schema.
#
# `--archive` is the separate, non-fatal step that writes the durability
# archive `/readyz` reads (`backups/*.cpb`). `--backup-first` never did: it
# writes a migration rollback point beside the database, and only when a
# migration is pending, so the readiness signal had no automated writer at all
# and production ran ~237 hours with no archive while deploys reported success.
CMD ["sh", "-c", "python -m control_plane.db migrate --backup-first --archive && exec abrolia-control-plane serve --host 0.0.0.0 --port 8080"]
