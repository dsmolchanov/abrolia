"""C5d: the entrypoint that calls the gateway, and the scheduler that drains it.

One test per invariant in
`thoughts/shared/plans/2026-08-29-c5d-gateway-entrypoint.md`.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from control_plane.bindings_resolution import (
    BindingLookupUnavailable,
    ResolvedSender,
)
from control_plane.crypto import sender_hmac
from control_plane.db import open_control_plane_database
from gateway.app import (
    MAX_WEBHOOK_BYTES,
    GatewayApp,
    RedeliverScheduler,
    RuntimeRelayDeliverer,
    RuntimeUnavailable,
    build_router,
)
from gateway.whatsapp_router import (
    Delivery,
    GatewayRedeliverWorker,
    WhatsAppGatewayRouter,
    relay_hmac,
)

ADAPTER_TOKEN = "synthetic-adapter-token-1234567890"
GATEWAY_KEY = b"synthetic-gateway-hmac-key-1234567890abcdef"
RELAY_KEY = b"household-relay-key-1234567890abcdef"
HOUSEHOLD = "00000000-0000-4000-a000-0000000000d5"
PHONE = "+999511234588"


def _bound(tmp_path: Path):
    db = open_control_plane_database(tmp_path / "cp.db")
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at, runtime_ref)"
        " VALUES (?, 'hh-c5d', 'active', 1, 1, ?)",
        (HOUSEHOLD, f"synthetic-runtime:{HOUSEHOLD}"),
    )
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
        " chat_id, external_id_hmac, actor_id, role, verified_at,"
        " verified_by_actor_id, published_revision) VALUES ('b-c5d', ?, 'whatsapp',"
        " ?, ?, ?, 'owner1', 'owner', 1, 'owner1', 1)",
        (HOUSEHOLD, PHONE, PHONE, sender_hmac(PHONE, GATEWAY_KEY)),
    )
    db.connection.commit()
    return db


def _request(body: bytes, *, signature: str, timestamp: str, sender: str = PHONE, **over):
    environ = {
        "PATH_INFO": "/v1/whatsapp/webhook",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_AUTHORIZATION": f"Bearer {ADAPTER_TOKEN}",
        "HTTP_X_RELAY_SENDER": sender,
        "HTTP_X_RELAY_SIGNATURE": signature,
        "HTTP_X_RELAY_TIMESTAMP": timestamp,
    }
    environ.update(over)
    return environ


def _call(app, environ) -> tuple[str, dict]:
    captured: dict = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(app(environ, start_response))
    return captured["status"], json.loads(body)


def _router(tmp_path, *, deliver=None, now=1_000_000.0):
    return WhatsAppGatewayRouter(
        _bound(tmp_path),
        relay_keys={HOUSEHOLD: RELAY_KEY},
        gateway_hmac_key=GATEWAY_KEY,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: now,
    )


def _ingress_count(path: Path) -> int:
    """Rows the WAL is holding — and zero when it was never even opened.

    The store is lazy, so a path that refuses before persisting leaves no file
    at all. That is a count of zero and not an error: it is the strongest form
    of "nothing was retained".
    """
    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM gateway_ingress").fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        connection.close()


def test_a_signed_request_for_a_bound_sender_reaches_the_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    """The entrypoint the whole slice exists for."""
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    delivered: list[tuple[str, bytes]] = []
    now = 1_000_000.0
    router = _router(tmp_path, deliver=lambda d: delivered.append((d.household_id, d.payload)), now=now)
    body = b'{"message":"through the front door"}'
    ts = str(int(now))

    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        _request(body, signature=relay_hmac(RELAY_KEY, body, ts), timestamp=ts),
    )
    assert status.startswith("200")
    assert payload == {"status": "ok"}
    assert delivered == [(HOUSEHOLD, body)]
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_a_wrong_signature_is_refused_and_keeps_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """403, because retrying the same bytes cannot help — and no row.

    A signature a present key rejects can never verify, so keeping it would
    store a message body for a redelivery that must never happen.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    now = 1_000_000.0
    router = _router(tmp_path, deliver=lambda *a: None, now=now)
    body = b'{"message":"not signed by the relay"}'

    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        _request(body, signature="sha256=" + "0" * 64, timestamp=str(int(now))),
    )
    assert status.startswith("403")
    assert payload == {"status": "hmac_rejected"}
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_an_unknown_sender_is_indistinguishable_from_a_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    """E2: the status must not be usable to probe whether a sender is bound.

    E5 calls this "not a 404 leak". The reason it matters even behind an
    internal adapter is that a status code is the cheapest possible oracle:
    anything that can reach this endpoint could otherwise enumerate which
    numbers belong to a household by watching the status alone.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    now = 1_000_000.0
    router = _router(tmp_path, deliver=lambda d: None, now=now)
    body = b'{"message":"nobody"}'
    ts = str(int(now))
    app = GatewayApp(router, adapter_token=ADAPTER_TOKEN)

    bound_status, _ = _call(
        app, _request(body, signature=relay_hmac(RELAY_KEY, body, ts), timestamp=ts)
    )
    unknown_status, unknown_body = _call(
        app,
        _request(
            body,
            signature=relay_hmac(RELAY_KEY, body, ts),
            timestamp=ts,
            sender="+999511234000",
        ),
    )
    assert unknown_status == bound_status, "the status told the caller who exists"
    assert unknown_body == {"status": "unknown_sender"}

    # AND UNDER INVALID BYTES, which is where the oracle actually was. Routing
    # runs before signature verification — it has to, because the signature is
    # verified with the household's key and the household comes from the route
    # — so with a bad signature an unknown sender was denied at routing and
    # answered `200 unknown_sender`, while a BOUND sender reached verification
    # and answered `403 hmac_rejected`. The earlier version of this test
    # compared two VALID requests and never touched that path.
    # Under INVALID bytes the two DO differ — `403 hmac_rejected` for a bound
    # sender, `200 unknown_sender` for one nobody holds — and that difference
    # is the oracle Codex found. It is acceptable only because the caller had
    # to authenticate to reach it at all: routing runs before signature
    # verification by necessity, since the signature is checked with the
    # household's key and the household comes from the route.
    #
    # So the property that has to hold is about the UNAUTHENTICATED caller, and
    # `test_an_unauthenticated_caller_learns_nothing_at_all` is where it lives.
    # Pinned here rather than left implicit, so a later change that removed the
    # credential would have to confront what it re-exposes.
    forged = "sha256=" + "0" * 64
    bound_bad = _call(app, _request(body, signature=forged, timestamp=ts))
    unknown_bad = _call(
        app, _request(body, signature=forged, timestamp=ts, sender="+999511234000")
    )
    assert bound_bad != unknown_bad
    assert bound_bad[0].startswith("403") and unknown_bad[0].startswith("200")


def test_a_delivery_failure_is_owned_by_the_gateway_not_the_caller(
    tmp_path: Path, monkeypatch
) -> None:
    """E2 and E3: 200 with a WAL row, and the scheduled worker finishes it.

    Answering anything retryable here would duplicate the redelivery the WAL
    has already scheduled — the caller sending it again, and the worker
    sending it too.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    clock = [1_000_000.0]
    delivered: list[bytes] = []
    failing = True

    def deliver(delivery):
        if failing:
            raise RuntimeUnavailable("runtime is down")
        delivered.append(delivery.payload)

    router = _router(tmp_path, deliver=deliver)
    router.now_fn = lambda: clock[0]
    body = b'{"message":"the runtime was down"}'
    ts = str(int(clock[0]))

    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        _request(body, signature=relay_hmac(RELAY_KEY, body, ts), timestamp=ts),
    )
    assert status.startswith("200"), "the caller was told to retry what we already own"
    assert payload == {"status": "runtime_unavailable"}
    assert _ingress_count(tmp_path / "ingress.db") == 1

    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    assert GatewayRedeliverWorker(router).run_once().public_dict()["delivered"] == 1
    assert delivered == [body]


def test_the_kill_switch_closes_the_entrypoint_without_dropping_the_message(
    tmp_path: Path, monkeypatch
) -> None:
    """E3: 503, because nothing was persisted and nothing was decided.

    `handle_webhook` returns `flag_disabled` BEFORE it persists, so a 200 here
    would drop the message on the floor during exactly the incident the switch
    was thrown to contain. 503 is the one case where the caller retrying is
    the right thing.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    now = 1_000_000.0
    router = _router(tmp_path, deliver=lambda *a: None, now=now)
    body = b'{"message":"during an incident"}'
    ts = str(int(now))

    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        _request(body, signature=relay_hmac(RELAY_KEY, body, ts), timestamp=ts),
    )
    assert status.startswith("503")
    assert payload == {"status": "flag_disabled"}
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_an_oversized_body_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch
) -> None:
    """E4: the ceiling is enforced on the DECLARED length, not after reading.

    Reading first and checking after is the memory exhaustion this exists to
    prevent, so the stream is one that fails if touched.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    router = _router(tmp_path, deliver=lambda *a: None)

    class _Explodes:
        def read(self, *_a):  # pragma: no cover - must never run
            raise AssertionError("the body was read before the ceiling was checked")

    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        {
            "PATH_INFO": "/v1/whatsapp/webhook",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(MAX_WEBHOOK_BYTES + 1),
            "wsgi.input": _Explodes(),
            "HTTP_X_RELAY_SENDER": PHONE,
            "HTTP_X_RELAY_SIGNATURE": "x",
            "HTTP_X_RELAY_TIMESTAMP": "1",
        },
    )
    assert status.startswith("413")
    assert payload == {"status": "payload_too_large"}


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/healthz", "GET", "200"),
        ("/", "GET", "404"),
        ("/v1/whatsapp/webhook", "GET", "405"),
    ],
)
def test_the_surface_is_exactly_two_routes(
    tmp_path: Path, path: str, method: str, expected: str
) -> None:
    """A relay with more surface than it needs is more to get wrong."""
    router = _router(tmp_path, deliver=lambda *a: None)
    status, _ = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN), {"PATH_INFO": path, "REQUEST_METHOD": method}
    )
    assert status.startswith(expected)


def test_the_scheduler_never_runs_beside_itself(tmp_path: Path) -> None:
    """E5: a pass that overruns the interval delays the next, never joins it.

    Two concurrent passes would both claim the same due rows and deliver them
    twice, so the wait is measured after the run returns.
    """
    overlaps: list[int] = []
    running = threading.Lock()
    done = threading.Event()
    passes = [0]

    class _SlowWorker:
        def run_once(self):
            if not running.acquire(blocking=False):
                overlaps.append(1)
                return
            try:
                passes[0] += 1
                if passes[0] >= 3:
                    done.set()
            finally:
                running.release()

    scheduler = RedeliverScheduler(_SlowWorker(), interval_seconds=0.01)
    thread = scheduler.start()
    assert done.wait(timeout=5), "the scheduler stopped running"
    scheduler.stop()
    thread.join(timeout=5)
    assert overlaps == []
    assert passes[0] >= 3


def test_a_failing_pass_does_not_end_the_scheduler(tmp_path: Path) -> None:
    """A dead scheduler is the durability going silently unspent again.

    That is the defect C5b existed to close, and an unhandled exception in one
    pass would reintroduce it — the WAL would fill while nothing drained it.
    """
    done = threading.Event()
    calls = [0]

    class _Angry:
        def run_once(self):
            calls[0] += 1
            if calls[0] >= 3:
                done.set()
            raise RuntimeError("this pass failed")

    scheduler = RedeliverScheduler(_Angry(), interval_seconds=0.01)
    thread = scheduler.start()
    assert done.wait(timeout=5), "the scheduler died on the first failure"
    scheduler.stop()
    thread.join(timeout=5)
    assert scheduler.failures >= 3


def test_the_gateway_opens_no_control_plane_database(tmp_path: Path) -> None:
    """C5c's K1, arrived at completely.

    C5d's draft opened `/data/control-plane.db` on the gateway's own volume,
    which the control plane had never written to — so the gateway came up
    healthy, passed its health check and routed nobody. C5e replaces reading
    with asking, and the consequence is that the gateway holds no household
    data of any kind: two roots, one credential and its own WAL.

    Fail-closed and loud without the two settings, because the alternative
    failure is `lookup_unavailable` on every request — a WAL filling up behind
    a configuration mistake that looks exactly like an outage.
    """
    with pytest.raises(ValueError, match="resolves senders through the control plane"):
        build_router({"ABROLIA_GATEWAY_INGRESS_DB": str(tmp_path / "wal.db")})


def test_a_control_plane_that_does_not_answer_is_retryable_not_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """L2: the two facts are different and the row's fate depends on which.

    "The control plane did not answer" is not "nobody holds this sender".
    Collapsing them drops a family's message TERMINALLY because a deployment
    happened to be restarting — and the redeliver worker never sees it, because
    a terminal outcome deletes the row.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")

    class _Down:
        def resolve(self, **_kwargs):
            raise BindingLookupUnavailable("control plane is restarting")

    clock = [1_000_000.0]
    delivered: list[bytes] = []
    router = WhatsAppGatewayRouter(
        resolver=_Down(),
        relay_keys={HOUSEHOLD: RELAY_KEY},
        gateway_hmac_key=GATEWAY_KEY,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda d: delivered.append(d.payload),
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"during a control-plane restart"}'
    ts = str(int(clock[0]))
    status, payload = _call(
        GatewayApp(router, adapter_token=ADAPTER_TOKEN),
        _request(body, signature=relay_hmac(RELAY_KEY, body, ts), timestamp=ts),
    )
    assert payload == {"status": "lookup_unavailable"}
    assert status.startswith("200"), "the caller was told to retry what we own"
    assert _ingress_count(tmp_path / "ingress.db") == 1, (
        "a control-plane restart dropped the message terminally"
    )


def test_the_runtime_reference_comes_from_the_resolve_that_found_the_household(
    tmp_path: Path, monkeypatch
) -> None:
    """One answer, not two questions that can disagree.

    An earlier draft asked a local `households` table for the runtime after
    resolving the household elsewhere. Two sources can be answered
    inconsistently, and the failure that produces is a message routed for one
    household delivered to the runtime of another.

    Also pinned: a reference outside the managed namespace never becomes a
    hostname, and is retryable rather than terminal — a household
    mid-provisioning gets one shortly, and the WAL holds the message until it
    does.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")

    class _Resolver:
        def __init__(self, ref):
            self.ref = ref

        def resolve(self, **_kwargs):
            return ResolvedSender(household_id=HOUSEHOLD, runtime_ref=self.ref)

    class _NeverCalled:
        def post(self, *_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("a request was made to an unmanaged reference")

    now = 1_000_000.0
    router = WhatsAppGatewayRouter(
        resolver=_Resolver("evil.example.com"),
        relay_keys={HOUSEHOLD: RELAY_KEY},
        gateway_hmac_key=GATEWAY_KEY,
        ingress_path=tmp_path / "ingress.db",
        now_fn=lambda: now,
    )
    routed = router.route("+999511234588", "whatsapp", timestamp=str(int(now)))
    assert routed.runtime_ref == "evil.example.com", "the resolve's answer is carried"
    deliverer = RuntimeRelayDeliverer(client=_NeverCalled())
    with pytest.raises(RuntimeUnavailable):
        deliverer(
            Delivery(
                household_id=HOUSEHOLD,
                runtime_ref=routed.runtime_ref,
                payload=b"{}",
                timestamp="1",
                signature="sig",
            )
        )

    # And a managed one is carried through to the delivery unchanged.
    managed = f"synthetic-runtime:{HOUSEHOLD}"
    router.resolver = _Resolver(managed)
    assert router.route(
        "+999511234588", "whatsapp", timestamp=str(int(now))
    ).runtime_ref == managed


def test_the_deploy_unit_gives_the_gateway_no_household_data() -> None:
    """C5c's K1 at the deployment level, and C5e completes it.

    A first draft mounted a control-plane database on the gateway's own volume,
    which the control plane had never written to — the gateway came up healthy
    and routed nobody. It now asks instead of reading, so the deployment names
    no database at all, and the gateway holds no household data of any kind.
    """
    config = Path("deploy/gateway/fly.toml").read_text(encoding="utf-8")
    for forbidden in (
        "ABROLIA_ENCRYPTION_KEY",
        "ABROLIA_LOOKUP_HMAC_KEY",
        "ABROLIA_TOKEN_HMAC_KEY",
        "ABROLIA_CONTROL_PLANE_BACKUP_KEY",
        "ABROLIA_FLY_API_TOKEN",
        # The database itself, which is the C5e change.
        "ABROLIA_GATEWAY_CONTROL_PLANE_DB",
    ):
        assert f"{forbidden} =" not in config, f"the gateway was given {forbidden}"
    # Every credential is a Fly secret rather than [env], which is recorded in
    # the image configuration.
    for secret in (
        "ABROLIA_GATEWAY_SENDER_HMAC_KEY",
        "ABROLIA_GATEWAY_RELAY_ROOT_KEY",
        "ABROLIA_GATEWAY_LOOKUP_TOKEN",
        "ABROLIA_GATEWAY_ADAPTER_TOKEN",
    ):
        assert f"{secret} =" not in config, f"{secret} was set as [env]"
    # It asks the control plane, over private transport.
    assert ".flycast" in config
    assert 'ABROLIA_WHATSAPP_SHARED_ENABLED = "0"' in config
    # The only mount is the WAL it owns.
    assert config.count("[mounts]") == 1
    assert "gateway-ingress.db" in config


def test_the_image_installs_the_package_it_runs() -> None:
    """`python -m gateway.app` needs `gateway` in the distribution.

    Package discovery listed only `hermes_cloud*`, `control_plane*` and `web*`,
    so the wheel built cleanly and the container exited immediately with
    `No module named gateway` — a deploy unit that cannot start.
    """
    import tomllib

    with open("pyproject.toml", "rb") as handle:
        include = tomllib.load(handle)["tool"]["setuptools"]["packages"]["find"]["include"]
    assert "gateway*" in include
    dockerfile = Path("deploy/gateway/Dockerfile").read_text(encoding="utf-8")
    assert "COPY gateway ./gateway" in dockerfile
    assert 'CMD ["python", "-m", "gateway.app"]' in dockerfile
