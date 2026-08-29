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

from control_plane.crypto import sender_hmac
from control_plane.db import open_control_plane_database
from gateway.app import (
    MAX_WEBHOOK_BYTES,
    GatewayApp,
    RedeliverScheduler,
    RuntimeRelayDeliverer,
    RuntimeUnavailable,
)
from gateway.whatsapp_router import (
    GatewayRedeliverWorker,
    WhatsAppGatewayRouter,
    relay_hmac,
)

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
    router = _router(tmp_path, deliver=lambda h, p, t, s: delivered.append((h, p)), now=now)
    body = b'{"message":"through the front door"}'
    ts = str(int(now))

    status, payload = _call(
        GatewayApp(router),
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
        GatewayApp(router),
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
    router = _router(tmp_path, deliver=lambda h, p, t, s: None, now=now)
    body = b'{"message":"nobody"}'
    ts = str(int(now))
    app = GatewayApp(router)

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

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeUnavailable("runtime is down")
        delivered.append(payload)

    router = _router(tmp_path, deliver=deliver)
    router.now_fn = lambda: clock[0]
    body = b'{"message":"the runtime was down"}'
    ts = str(int(clock[0]))

    status, payload = _call(
        GatewayApp(router),
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
        GatewayApp(router),
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
        GatewayApp(router),
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
        GatewayApp(router), {"PATH_INFO": path, "REQUEST_METHOD": method}
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


def test_a_runtime_reference_outside_the_namespace_never_becomes_a_hostname(
    tmp_path: Path,
) -> None:
    """The deliverer builds a URL from a database column, so it checks it.

    Retryable rather than terminal: a household mid-provisioning has no
    runtime yet and gets one shortly, and the WAL is what holds the message
    until it does.
    """
    db = _bound(tmp_path)
    db.connection.execute(
        "UPDATE households SET runtime_ref = 'evil.example.com' WHERE id = ?",
        (HOUSEHOLD,),
    )
    db.connection.commit()

    class _NeverCalled:
        def post(self, *_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("a request was made to an unmanaged reference")

    deliverer = RuntimeRelayDeliverer(db, client=_NeverCalled())
    with pytest.raises(RuntimeUnavailable):
        deliverer(HOUSEHOLD, b"{}", "1", "sig")


def test_the_deploy_unit_carries_no_household_secret() -> None:
    """E6: C5c's K1, at the deployment level.

    The gateway gets two roots and a database. Giving it the field encryption
    key or the lookup HMAC key would let the layer that resolves senders
    decrypt a manifest and correlate every other keyed digest in the system —
    which is the whole reason its keys are separate.
    """
    config = Path("deploy/gateway/fly.toml").read_text(encoding="utf-8")
    for forbidden in (
        "ABROLIA_ENCRYPTION_KEY",
        "ABROLIA_LOOKUP_HMAC_KEY",
        "ABROLIA_TOKEN_HMAC_KEY",
        "ABROLIA_CONTROL_PLANE_BACKUP_KEY",
        "ABROLIA_FLY_API_TOKEN",
    ):
        assert f"{forbidden} =" not in config, f"the gateway was given {forbidden}"
    # And the two roots are secrets rather than [env], because [env] is
    # recorded in the image configuration.
    assert "ABROLIA_GATEWAY_SENDER_HMAC_KEY =" not in config
    assert "ABROLIA_GATEWAY_RELAY_ROOT_KEY =" not in config
    # Fail-closed by default: the relay carries family message content.
    assert 'ABROLIA_WHATSAPP_SHARED_ENABLED = "0"' in config
