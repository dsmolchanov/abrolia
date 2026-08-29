"""The HTTP entrypoint for the shared gateway, and the thing that schedules
its redeliver worker.

Every piece of the WhatsApp path existed before this module and none of it
ran: `WhatsAppGatewayRouter.handle_webhook` had no caller outside tests and
`GatewayRedeliverWorker.run_once` had no scheduler.

Deliberately WSGI from the standard library, the same shape
`hermes_cloud/runtime/service.py` uses. The gateway is a narrow relay with no
model, no tools and no household secrets; giving it a web framework would be
more dependency than the whole component is code.

THE CALLER IS THE INTERNAL RELAY ADAPTER, not Meta or Telegram.
`handle_webhook` verifies the inbound signature with the HOUSEHOLD's relay
key, which only something holding those keys can produce — and this repository
has never had application-secret verification, a subscription challenge or a
`verify_token`. The public provider edge is its own slice; this exposes the
entrypoint the code already implements.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from control_plane.bindings_resolution import (
    BindingLookupUnavailable,
    ResolvedSender,
    SenderNotRoutable,
)
from control_plane.privacy.runtime import RUNTIME_REF
from gateway.whatsapp_router import (
    Delivery,
    GatewayRedeliverWorker,
    GatewayResult,
    WhatsAppGatewayRouter,
)
from hermes_cloud.ingest.whatsapp_webhook import MAX_WHATSAPP_WEBHOOK_BYTES

#: The runtime's own ceiling, IMPORTED rather than restated. The comment here
#: used to say it mirrored the runtime and the number said 128 KiB against the
#: runtime's 256 KiB, so a payload between the two was refused by the gateway
#: and would have been accepted by the end it was going to — a message dropped
#: by a limit that claimed to be shared and was not. Restating a constant is
#: how two ends of one contract drift; the C5a defect in miniature.
MAX_WEBHOOK_BYTES = MAX_WHATSAPP_WEBHOOK_BYTES

ENV_HOST = "ABROLIA_GATEWAY_HOST"
ENV_PORT = "ABROLIA_GATEWAY_PORT"
ENV_CONTROL_PLANE_URL = "ABROLIA_GATEWAY_CONTROL_PLANE_URL"
ENV_LOOKUP_TOKEN = "ABROLIA_GATEWAY_LOOKUP_TOKEN"
ENV_INGRESS = "ABROLIA_GATEWAY_INGRESS_DB"
ENV_SENDER_KEY = "ABROLIA_GATEWAY_SENDER_HMAC_KEY"
ENV_RELAY_ROOT = "ABROLIA_GATEWAY_RELAY_ROOT_KEY"
ENV_REDELIVER_SECONDS = "ABROLIA_GATEWAY_REDELIVER_INTERVAL_SECONDS"
#: The credential the internal relay adapter presents. E2 said detailed codes
#: are safe "because this entrypoint's caller is the internal adapter" — and
#: nothing checked that they were. Assumed, not enforced.
ENV_ADAPTER_TOKEN = "ABROLIA_GATEWAY_ADAPTER_TOKEN"

#: Authentication the caller got wrong. Retrying the same bytes cannot help.
_REFUSED = frozenset({"hmac_rejected", "timestamp_replay"})
#: The one outcome where the gateway took NO responsibility and the caller
#: retrying is the right thing. `handle_webhook` returns this before it
#: persists anything, so answering 200 would drop the message on the floor
#: during exactly the incident the switch was thrown to contain.
_UNAVAILABLE = frozenset({"flag_disabled"})


class RemoteBindingResolver:
    """Asks the control plane who holds a sender.

    The deployed gateway has no `channel_bindings` table. After C5e it has no
    control-plane data at all — this is the only thing it knows about
    households, it is asked once per message, and the answer is never cached
    (L1: a cache with any lifetime is the replicated projection this slice
    rejected, wearing a different name).

    Transport failure is translated to `BindingLookupUnavailable` HERE, at the
    layer that knows what a transport failure is. That is what lets the router
    catch one narrow exception: "the control plane did not answer" is retryable
    and stays in the WAL, while "nobody holds this sender" is terminal — and a
    resolver that let an `httpx` error reach the router as something generic
    would collapse the two.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.timeout = timeout

    def resolve(
        self, *, channel: str, external_id: str | None, external_id_hmac: str | None
    ) -> ResolvedSender:
        try:
            response = self.client.post(
                f"{self.base_url}/internal/v1/bindings/resolve",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "channel": channel,
                    "external_id": external_id,
                    "external_id_hmac": external_id_hmac,
                },
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise BindingLookupUnavailable("control plane did not answer") from error
        if response.status_code == 404:
            # The one terminal answer. `unknown_sender` and `ambiguous_sender`
            # share it deliberately, so the reply cannot tell a bound sender
            # from an unbound one by status.
            raise SenderNotRoutable("unknown_sender")
        if response.status_code != 200:
            # Including 401 and 403: a gateway the control plane refuses is
            # misconfigured, and dropping every message while that is true
            # would turn a credential mistake into silent data loss. Retryable,
            # bounded by `MAX_ATTEMPTS` like anything else.
            raise BindingLookupUnavailable(f"control plane answered {response.status_code}")
        try:
            body = response.json()
            return ResolvedSender(
                household_id=str(body["household_id"]),
                runtime_ref=body.get("runtime_ref"),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise BindingLookupUnavailable("control plane returned an unusable answer") from error


class RuntimeUnavailable(RuntimeError):
    """The household's runtime did not take the delivery.

    Raised rather than returned so it reaches the router's own `except`, which
    is what puts the row in the WAL as `runtime_unavailable` and hands it to
    the redeliver worker. A deliverer that swallowed this would ACK a message
    the runtime never received.
    """


class RuntimeRelayDeliverer:
    """Posts a signed delivery to one household's runtime.

    Without this the gateway ACKs and drops: `handle_webhook` treats a missing
    `runtime_deliver` as "successful HMAC is the delivery proof for this
    pilot", which is a reasonable stand-in for a test and a silent message sink
    in a deployment.

    The runtime reference arrives IN the delivery, from the same resolve that
    produced the household. Two earlier drafts got this wrong in different
    ways: one queried a local `households` table, a second source that can be
    answered inconsistently with the first; the other cached the reference on
    the router, which the WSGI thread and the redeliver scheduler share, so two
    resolutions around a runtime transition could overwrite it between the
    route and the read. Carried per delivery, there is nothing to interleave.

    The reference is checked against the managed namespace before it becomes a
    hostname, so an answer that somehow held an arbitrary string cannot make
    this issue a request to it.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.timeout = timeout

    def __call__(self, delivery: Delivery) -> None:
        ref = delivery.runtime_ref
        if not ref or not RUNTIME_REF.fullmatch(ref):
            # No runtime, or a reference outside the managed namespace. Both
            # are retryable rather than terminal: a household mid-provisioning
            # gets one shortly, and the WAL is what holds the message until it
            # does.
            raise RuntimeUnavailable("household has no addressable runtime")
        try:
            response = self.client.post(
                f"http://{ref}.internal:8080/v1/whatsapp/webhook",
                headers={
                    "Content-Type": "application/json",
                    # The names the runtime reads — `HTTP_X_RELAY_SIGNATURE`
                    # and `HTTP_X_RELAY_TIMESTAMP` in its WSGI environ.
                    "X-Relay-Signature": delivery.signature,
                    "X-Relay-Timestamp": delivery.timestamp,
                },
                content=delivery.payload,
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise RuntimeUnavailable("runtime did not answer") from error
        if response.status_code != 200:
            # Including 403: a runtime refusing the signature is a
            # disagreement between the two ends, and dropping the message
            # would hide it. The WAL keeps it and the attempt count bounds how
            # long that lasts.
            raise RuntimeUnavailable(f"runtime answered {response.status_code}")


class GatewayApp:
    """WSGI in front of one `WhatsAppGatewayRouter`.

    E1: this adds no authority. It turns a request into a `handle_webhook`
    call and a result into a response. Routing, key lookup, signature
    verification, the kill switch and the WAL all stay in the router, where
    four slices of reasoning already live — a decision made here would be a
    second place to get that decision wrong.
    """

    def __init__(
        self, router: WhatsAppGatewayRouter, *, adapter_token: str | None = None
    ) -> None:
        self.router = router
        self.adapter_token = adapter_token

    def __call__(self, environ: Mapping[str, Any], start_response: Callable):
        status, body = self._respond(environ)
        payload = json.dumps(body, sort_keys=True).encode()
        start_response(
            status,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
                # The gateway answers machines, and a cached relay decision is
                # a decision applied to the wrong message.
                ("Cache-Control", "no-store"),
            ],
        )
        return [payload]

    def _respond(self, environ: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "")
        if path == "/healthz" and method == "GET":
            # Liveness only. It deliberately says nothing about bindings,
            # households or the WAL — a health endpoint that reported those
            # would be an unauthenticated read of exactly what this component
            # exists to keep narrow.
            return "200 OK", {"status": "ok"}
        if path != "/v1/whatsapp/webhook":
            return "404 Not Found", {"status": "not_found"}
        if method != "POST":
            return "405 Method Not Allowed", {"status": "method_not_allowed"}
        return self._webhook(environ)

    def _webhook(self, environ: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        # E4: declared, bounded, read once. An unbounded read on a network
        # edge is a memory exhaustion, and a short read is a payload whose
        # signature can never verify — so a length that does not match what
        # arrived is refused rather than passed on to fail confusingly later.
        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0"))
        except ValueError:
            return "400 Bad Request", {"status": "invalid_request"}
        if length < 1:
            return "400 Bad Request", {"status": "invalid_request"}
        if length > MAX_WEBHOOK_BYTES:
            return "413 Payload Too Large", {"status": "payload_too_large"}
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            return "400 Bad Request", {"status": "invalid_request"}
        payload = stream.read(length)
        if not isinstance(payload, bytes) or len(payload) != length:
            return "400 Bad Request", {"status": "invalid_request"}

        # AUTHENTICATE THE CALLER BEFORE LOOKING ANYTHING UP.
        #
        # Routing ran before signature verification, which is unavoidable —
        # the signature is verified with the household's key and the household
        # comes from the route. The consequence was an oracle: with invalid
        # bytes, an unknown sender got `200 unknown_sender` because routing
        # denied first, while a BOUND sender reached verification and got
        # `403 hmac_rejected`. Anyone who could reach this service could
        # enumerate which numbers belong to a household without holding a
        # single key.
        #
        # E2 claimed the detailed codes were safe "because this entrypoint's
        # caller is the internal adapter". That was an assumption about the
        # deployment, not a property of the code, and this is what turns it
        # into one. After it, the codes really are safe.
        if not self._adapter_is_authentic(environ):
            return "401 Unauthorized", {"status": "unauthenticated"}

        sender = str(environ.get("HTTP_X_RELAY_SENDER") or "")
        signature = str(environ.get("HTTP_X_RELAY_SIGNATURE") or "")
        timestamp = str(environ.get("HTTP_X_RELAY_TIMESTAMP") or "")
        if not sender:
            return "400 Bad Request", {"status": "invalid_request"}
        channel = str(environ.get("HTTP_X_RELAY_CHANNEL") or "whatsapp")

        result = self.router.handle_webhook(
            payload,
            sender,
            channel=channel,
            timestamp=timestamp,
            signature=signature,
        )
        return self._status_for(result)

    def _adapter_is_authentic(self, environ: Mapping[str, Any]) -> bool:
        """Whether the caller proved it is the relay adapter.

        FAIL CLOSED when no token is configured. Every other optional key in
        this system degrades to previous behaviour, and this one must not: an
        unauthenticated endpoint that accepts family message bodies and answers
        questions about who is bound is a hole, not an unconfigured
        convenience. There is no previous behaviour to degrade to — this
        endpoint did not exist before.
        """
        if not self.adapter_token:
            return False
        presented = str(environ.get("HTTP_AUTHORIZATION") or "")
        prefix = "Bearer "
        if not presented.startswith(prefix):
            return False
        return hmac.compare_digest(presented[len(prefix) :], self.adapter_token)

    @staticmethod
    def _status_for(result: GatewayResult) -> tuple[str, dict[str, Any]]:
        """E2: the status says what the caller should DO, the body says why.

        Conflating those is how a status code starts leaking whether a
        household exists. `unknown_sender` and `ambiguous_sender` answer 200
        exactly like a delivery does, so the status cannot be used to probe
        whether a sender is bound — E5's "not a 404 leak". The code travels in
        the body, which is safe here because this entrypoint's caller is the
        internal adapter that already holds household keys.

        A retryable denial is also 200, and that is the durability contract
        rather than an oversight: `relay_key_absent` and `runtime_unavailable`
        rows are IN the WAL and belong to the redeliver worker now, so a
        caller that retried would duplicate work already scheduled.
        """
        if result.code in _REFUSED:
            return "403 Forbidden", {"status": result.code}
        if result.code in _UNAVAILABLE:
            return "503 Service Unavailable", {"status": result.code}
        # Everything else the gateway has either delivered, terminally decided,
        # or put in the WAL — and a WAL row belongs to the redeliver worker
        # now, so a caller that retried would duplicate work already scheduled.
        return "200 OK", {"status": result.code}


class RedeliverScheduler:
    """Runs `GatewayRedeliverWorker.run_once` on a timer, never beside itself.

    E5: one thread and one timer. Two concurrent passes would both claim the
    same due rows and deliver them twice, so a run that takes longer than the
    interval delays the next tick instead of starting alongside it — the wait
    is measured after the run returns, not before it starts.

    In-process rather than a cron entry because the WAL is a SQLite file local
    to this machine. A separate scheduler would need its own copy of the
    gateway's keys and its own handle on that file, which is two deployments
    holding one household's message bodies instead of one.
    """

    def __init__(
        self,
        worker: GatewayRedeliverWorker,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self.worker = worker
        self.interval_seconds = interval_seconds
        #: Count only. A gateway never records a payload or a sender.
        self.failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_forever(self, *, stop: threading.Event | None = None) -> None:
        stop = stop or self._stop
        while not stop.is_set():
            try:
                self.worker.run_once()
            except Exception:
                # A pass that fails must not end the scheduler: the WAL still
                # holds the backlog, and a dead scheduler is the durability
                # silently going unspent again — the exact defect C5b existed
                # to close. Nothing is recorded beyond the fact, because a
                # gateway log line must never carry a payload or a sender.
                self.failures += 1
            # Waited AFTER the run, so a pass that overruns the interval
            # delays the next one rather than being joined by it.
            stop.wait(self.interval_seconds)

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever, name="gateway-redeliver", daemon=True
        )
        self._thread = thread
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()


def _decode_key(value: str, *, name: str) -> bytes:
    raw = bytes.fromhex(value.strip())
    if len(raw) != 32:
        raise ValueError(f"{name} must be 32 bytes")
    return raw


def build_router(env: Mapping[str, str] | None = None) -> WhatsAppGatewayRouter:
    """The router a deployment builds, from configuration alone.

    IT OPENS NO CONTROL-PLANE DATABASE. C5d's draft did, on the gateway's own
    volume, which the control plane had never written to — so the gateway came
    up healthy, passed its health check and routed nobody. C5e replaces that
    with a question asked of the control plane, and the consequence is that the
    gateway now holds no household data of any kind: two roots, one credential,
    and its own ingress WAL.

    That is C5c's K1 arrived at completely. The gateway cannot decrypt a
    manifest, cannot correlate another keyed digest, and now cannot read a
    binding either — it can ask about one.
    """
    source = dict(os.environ if env is None else env)
    base_url = source.get(ENV_CONTROL_PLANE_URL, "").strip()
    token = source.get(ENV_LOOKUP_TOKEN, "").strip()
    if not base_url or not token:
        # FAIL CLOSED, and loudly. A gateway without these cannot answer a
        # single message, and the failure it would otherwise produce is
        # `lookup_unavailable` on every request — a WAL filling up behind a
        # configuration mistake that looks like an outage.
        raise ValueError(
            f"{ENV_CONTROL_PLANE_URL} and {ENV_LOOKUP_TOKEN} are required:"
            " the gateway resolves senders through the control plane"
        )
    sender_key = source.get(ENV_SENDER_KEY, "").strip()
    relay_root = source.get(ENV_RELAY_ROOT, "").strip()
    resolver = RemoteBindingResolver(base_url, token)
    router = WhatsAppGatewayRouter(
        resolver=resolver,
        gateway_hmac_key=(
            _decode_key(sender_key, name=ENV_SENDER_KEY) if sender_key else None
        ),
        relay_root=(
            _decode_key(relay_root, name=ENV_RELAY_ROOT) if relay_root else None
        ),
        ingress_path=Path(source.get(ENV_INGRESS, "/data/gateway-ingress.db")),
    )
    router.runtime_deliver = RuntimeRelayDeliverer()
    return router


def build_app(env: Mapping[str, str] | None = None) -> GatewayApp:
    source = dict(os.environ if env is None else env)
    return GatewayApp(
        build_router(source),
        adapter_token=source.get(ENV_ADAPTER_TOKEN, "").strip() or None,
    )


def serve(env: Mapping[str, str] | None = None) -> None:  # pragma: no cover - process entry
    """Run the entrypoint and the redeliver scheduler in one process.

    One process because the ingress WAL is a SQLite file local to this
    machine. Splitting the scheduler out would give a second deployment its
    own copy of the gateway's keys and its own handle on a file holding
    families' message bodies, to save nothing.
    """
    from wsgiref.simple_server import make_server

    source = dict(os.environ if env is None else env)
    app = build_app(source)
    router = app.router
    worker = GatewayRedeliverWorker(router)
    try:
        interval = float(source.get(ENV_REDELIVER_SECONDS, "60"))
    except ValueError as error:
        raise ValueError(f"{ENV_REDELIVER_SECONDS} must be a number") from error
    RedeliverScheduler(worker, interval_seconds=interval).start()

    host = source.get(ENV_HOST, "0.0.0.0")
    try:
        port = int(source.get(ENV_PORT, "8080"))
    except ValueError as error:
        raise ValueError(f"{ENV_PORT} must be an integer") from error
    if not 0 <= port <= 65535:
        raise ValueError(f"{ENV_PORT} is outside the valid range")
    make_server(host, port, app).serve_forever()


if __name__ == "__main__":  # pragma: no cover - process entry
    serve()
