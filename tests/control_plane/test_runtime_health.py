from __future__ import annotations

import httpx

from control_plane.provisioning.runtime_health import RuntimeReadinessMonitor


def _seed_active_receipt(cp_stack, *, runtime_ref: str) -> tuple[str, int]:
    household_id = cp_stack.household.id
    revision = 1
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'active', runtime_ref = ?,"
            " current_config_revision = ? WHERE id = ?",
            (runtime_ref, revision, household_id),
        )
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " encryption_key_version, version, created_at, updated_at)"
            " VALUES ('identity-health', ?, 'managed_abrolia', 'active',"
            " 'test-v1', 1, 1, 1)",
            (household_id,),
        )
        connection.execute(
            "INSERT INTO email_activation_receipts (email_identity_id, desired_revision,"
            " runtime_ref, provider, inbound_check, outbound_check, checked_at,"
            " receipt_digest, status) VALUES"
            " ('identity-health', ?, ?, 'synthetic', 'healthy', 'healthy', 1, ?, 'active')",
            (revision, runtime_ref, "a" * 64),
        )
    return household_id, revision


def _receipt_status(cp_stack) -> str:
    return str(cp_stack.database.query_one(
        "SELECT status FROM email_activation_receipts"
        " WHERE email_identity_id = 'identity-health'"
    )["status"])


def _identity_status(cp_stack) -> str:
    return str(cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE id = 'identity-health'"
    )["status"])


def test_runtime_tamper_marks_activation_receipt_needs_attention(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                503, json={"status": "not_ready", "reason": "revision_mismatch"}
            )
        )),
    )

    assert monitor.reconcile_all(now=2)[0].status == "needs_attention"
    assert _receipt_status(cp_stack) == "needs_attention"
    assert _identity_status(cp_stack) == "needs_attention"


def test_matching_runtime_readiness_clears_attention(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_activation_receipts SET status = 'needs_attention'"
        )
        connection.execute(
            "UPDATE email_identities SET status = 'needs_attention'"
        )
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "status": "ready",
                "household_id": household_id,
                "config_revision": revision,
            })
        )),
    )

    assert monitor.reconcile_all(now=3)[0].status == "active"
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_runtime_transport_failure_is_inconclusive(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)

    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport failure", request=_request)

    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )

    assert monitor.reconcile_all(now=4)[0].status == "unknown"
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_runtime_identity_mismatch_fails_closed(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "status": "ready",
                "household_id": "foreign-household",
                "config_revision": 1,
            })
        )),
    )

    assert monitor.reconcile_all(now=5)[0].status == "needs_attention"
    assert _receipt_status(cp_stack) == "needs_attention"
