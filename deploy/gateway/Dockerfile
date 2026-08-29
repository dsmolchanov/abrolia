# The shared gateway: a narrow relay, and the image is narrow to match.
#
# No flyctl, unlike the control plane's image — the gateway never provisions
# anything. No `web/`, because it serves no pages. What it needs is the router,
# the control-plane package it reads bindings and crypto primitives from, and
# nothing else.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml requirements.txt ./
COPY control_plane ./control_plane
COPY hermes_cloud ./hermes_cloud
COPY gateway ./gateway
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ABROLIA_GATEWAY_CONTROL_PLANE_DB=/data/control-plane.db \
    ABROLIA_GATEWAY_INGRESS_DB=/data/gateway-ingress.db

RUN set -eu; \
    useradd --system --create-home --uid 10001 gateway; \
    mkdir -p /data; \
    chown gateway:gateway /data

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels abrolia \
    && rm -rf /wheels

USER gateway
EXPOSE 8080
CMD ["python", "-m", "gateway.app"]
