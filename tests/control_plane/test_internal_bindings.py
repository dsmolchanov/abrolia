"""C5e: the one question a deployed gateway is allowed to ask.

One test per invariant in
`thoughts/shared/plans/2026-08-29-c5e-gateway-binding-lookup.md`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from control_plane.api.app import create_app
from control_plane.bindings_resolution import SenderNotRoutable, resolve_sender
from control_plane.container import ControlPlaneContainer
from control_plane.crypto import sender_hmac

LOOKUP_TOKEN = "synthetic-gateway-lookup-token-1234567890"
PHONE = "+999511234577"
HOUSEHOLD = "00000000-0000-4000-a000-0000000000e5"
RUNTIME_REF = f"synthetic-runtime:{HOUSEHOLD}"


@pytest.fixture()
def resolve_harness(tmp_path, monkeypatch):
    """A control plane configured with a gateway credential, on private transport."""
    from fastapi.testclient import TestClient

    from control_plane.config import ControlPlaneConfig

    base = ControlPlaneConfig.for_test(tmp_path)
    config = replace(
        base,
        gateway_lookup_token=LOOKUP_TOKEN,
        internal_bootstrap_host="control-plane.flycast",
    ).validate()
    from tests.control_plane.conftest import MemoryMailer

    active = ControlPlaneContainer.build(config, mailer=MemoryMailer())
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO households (id, slug, status, created_at, updated_at,"
            " runtime_ref) VALUES (?, 'hh-c5e', 'active', 1, 1, ?)",
            (HOUSEHOLD, RUNTIME_REF),
        )
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
            " chat_id, external_id_hmac, actor_id, role, verified_at,"
            " verified_by_actor_id, published_revision) VALUES ('b-c5e', ?,"
            " 'whatsapp', ?, ?, ?, 'owner1', 'owner', 1, 'owner1', 1)",
            (HOUSEHOLD, PHONE, PHONE, sender_hmac(PHONE, config.gateway_sender_hmac_key)),
        )
    app = create_app(active_container=active)
    try:
        with TestClient(app, base_url="http://control-plane.flycast") as client:
            yield config, active, client
    finally:
        active.close()


def _resolve(client, body, *, token=LOOKUP_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/internal/v1/bindings/resolve", headers=headers, json=body)


def test_a_bound_sender_resolves_to_its_household_and_runtime(resolve_harness) -> None:
    """L3: identifiers, and only identifiers."""
    config, _active, client = resolve_harness
    response = _resolve(
        client,
        {
            "channel": "whatsapp",
            "external_id_hmac": sender_hmac(PHONE, config.gateway_sender_hmac_key),
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "household_id": HOUSEHOLD,
        "runtime_ref": RUNTIME_REF,
    }
    # Nothing the gateway did not ask about. A reply carrying the external id,
    # the chat or a role would put household content on a wire that exists to
    # avoid exactly that.
    assert PHONE not in response.text


def test_the_endpoint_and_the_local_rule_answer_the_same(resolve_harness) -> None:
    """L4: one rule, in one place.

    The gateway's local resolver and this endpoint would otherwise be two
    implementations of "which household holds this sender" — and two
    implementations of one comparison is the C5a defect, where each end had
    passing tests and disagreed with the other.
    """
    config, active, client = resolve_harness
    digest = sender_hmac(PHONE, config.gateway_sender_hmac_key)
    over_http = _resolve(
        client, {"channel": "whatsapp", "external_id_hmac": digest}
    ).json()
    locally = resolve_sender(
        active.database.connection, channel="whatsapp", external_id_hmac=digest
    )
    assert over_http == locally.public_dict()


def test_a_staged_binding_does_not_resolve(resolve_harness) -> None:
    """C3c's rule, applied at the endpoint rather than only in the gateway.

    A binding is routable only once the revision carrying it has ACTIVATED.
    An endpoint that forgot this would hand the gateway a household whose
    runtime has no pair for that member, and the message would go nowhere.
    """
    config, active, client = resolve_harness
    with active.database.write() as connection:
        connection.execute(
            "UPDATE channel_bindings SET published_revision = NULL WHERE id = 'b-c5e'"
        )
    response = _resolve(
        client,
        {
            "channel": "whatsapp",
            "external_id_hmac": sender_hmac(PHONE, config.gateway_sender_hmac_key),
        },
    )
    assert response.status_code == 404


def test_an_unknown_and_an_ambiguous_sender_answer_alike(resolve_harness) -> None:
    """Different operator problems, one reply.

    Distinguishing them would let the caller tell a bound sender from an
    unbound one by status, which is the enumeration oracle this endpoint must
    not become.
    """
    config, active, client = resolve_harness
    unknown = _resolve(
        client,
        {"channel": "whatsapp", "external_id_hmac": sender_hmac("+999511234000", config.gateway_sender_hmac_key)},
    )
    other = "00000000-0000-4000-a000-0000000000e6"
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO households (id, slug, status, created_at, updated_at)"
            " VALUES (?, 'hh-other', 'active', 1, 1)",
            (other,),
        )
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
            " chat_id, external_id_hmac, actor_id, role, verified_at,"
            " verified_by_actor_id, published_revision) VALUES ('b-dup', ?,"
            " 'whatsapp', ?, ?, ?, 'owner2', 'owner', 1, 'owner2', 1)",
            (other, PHONE, PHONE, sender_hmac(PHONE, config.gateway_sender_hmac_key)),
        )
    ambiguous = _resolve(
        client,
        {"channel": "whatsapp", "external_id_hmac": sender_hmac(PHONE, config.gateway_sender_hmac_key)},
    )
    assert unknown.status_code == ambiguous.status_code == 404
    assert unknown.json() == ambiguous.json()


@pytest.mark.parametrize("token", [None, "wrong-token-entirely"])
def test_the_endpoint_refuses_a_caller_it_cannot_identify(resolve_harness, token) -> None:
    """L5: the gateway's own credential, and nothing without it."""
    config, _active, client = resolve_harness
    response = _resolve(
        client,
        {
            "channel": "whatsapp",
            "external_id_hmac": sender_hmac(PHONE, config.gateway_sender_hmac_key),
        },
        token=token,
    )
    assert response.status_code == 401
    assert HOUSEHOLD not in response.text


def test_resolving_by_both_or_neither_is_refused(resolve_harness) -> None:
    """One question at a time, so the answer cannot depend on which won."""
    config, _active, client = resolve_harness
    digest = sender_hmac(PHONE, config.gateway_sender_hmac_key)
    both = _resolve(
        client,
        {"channel": "whatsapp", "external_id": PHONE, "external_id_hmac": digest},
    )
    neither = _resolve(client, {"channel": "whatsapp"})
    assert both.status_code == 422
    assert neither.status_code == 422


def test_the_shared_rule_refuses_an_ambiguous_sender_directly() -> None:
    """The rule itself, without the endpoint around it."""
    import sqlite3

    from control_plane.db import ControlPlaneDatabase

    with pytest.raises(SenderNotRoutable) as refusal:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE channel_bindings (household_id TEXT, channel TEXT,"
            " external_id TEXT, external_id_hmac TEXT, published_revision INTEGER)"
        )
        connection.execute("CREATE TABLE households (id TEXT, runtime_ref TEXT)")
        for household in ("h1", "h2"):
            connection.execute(
                "INSERT INTO households VALUES (?, 'synthetic-runtime:x')", (household,)
            )
            connection.execute(
                "INSERT INTO channel_bindings VALUES (?, 'whatsapp', '990000001', NULL, 1)",
                (household,),
            )
        resolve_sender(connection, channel="whatsapp", external_id="990000001")
    assert refusal.value.code == "ambiguous_sender"
    assert ControlPlaneDatabase is not None
