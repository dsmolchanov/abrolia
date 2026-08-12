from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import httpx

from control_plane import cli
from control_plane.privacy.export import HouseholdExporter, SyntheticRuntimeExporter
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


def _runtime_health_checked_at(cp_stack) -> float:
    return float(cp_stack.database.query_one(
        "SELECT runtime_health_checked_at FROM email_activation_receipts"
        " WHERE email_identity_id = 'identity-health'"
    )["runtime_health_checked_at"])


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
    responses = iter((
        httpx.Response(503, json={"status": "not_ready"}),
        httpx.Response(200, json={
            "status": "ready",
            "household_id": household_id,
            "config_revision": revision,
        }),
    ))
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: next(responses)
        )),
    )

    assert monitor.reconcile_all(now=2)[0].status == "needs_attention"
    assert monitor.reconcile_all(now=3)[0].status == "active"
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_matching_runtime_does_not_clear_unrelated_identity_attention(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_identities SET status = 'needs_attention', updated_at = 2"
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
    assert _identity_status(cp_stack) == "needs_attention"


def test_runtime_failure_does_not_adopt_preexisting_identity_attention(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    with cp_stack.database.write() as connection:
        connection.execute("UPDATE email_identities SET status = 'needs_attention'")
    responses = iter((
        httpx.Response(503, json={"status": "not_ready"}),
        httpx.Response(200, json={
            "status": "ready",
            "household_id": household_id,
            "config_revision": revision,
        }),
    ))
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: next(responses)
        )),
    )

    assert monitor.reconcile_all(now=2)[0].status == "needs_attention"
    assert monitor.reconcile_all(now=3)[0].status == "active"
    receipt = cp_stack.database.query_one(
        "SELECT runtime_health_status, runtime_health_owns_attention"
        " FROM email_activation_receipts WHERE email_identity_id = 'identity-health'"
    )
    assert dict(receipt) == {
        "runtime_health_status": "active",
        "runtime_health_owns_attention": 0,
    }
    assert _identity_status(cp_stack) == "needs_attention"


def test_email_lifecycle_version_change_relinquishes_runtime_ownership(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    responses = iter((
        httpx.Response(503, json={"status": "not_ready"}),
        httpx.Response(200, json={
            "status": "ready",
            "household_id": household_id,
            "config_revision": revision,
        }),
    ))
    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: next(responses)
        )),
    )

    assert monitor.reconcile_all(now=2)[0].status == "needs_attention"
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_identities SET version = version + 1, updated_at = 2.5"
            " WHERE id = 'identity-health'"
        )
    assert monitor.reconcile_all(now=3)[0].status == "active"
    assert _identity_status(cp_stack) == "needs_attention"


def test_matching_runtime_does_not_clear_failed_activation_check(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_activation_receipts SET status = 'needs_attention',"
            " inbound_check = 'failed', runtime_health_status = 'active'"
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
    assert _receipt_status(cp_stack) == "needs_attention"
    assert _identity_status(cp_stack) == "needs_attention"


def test_older_runtime_observation_cannot_overwrite_newer_projection(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    newer = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={
                "status": "ready",
                "household_id": household_id,
                "config_revision": revision,
            })
        )),
    )

    def finish_newer_first(_request: httpx.Request) -> httpx.Response:
        assert newer.reconcile_all(now=3)[0].status == "active"
        return httpx.Response(503, json={"status": "not_ready"})

    older = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(finish_newer_first)),
    )

    assert older.reconcile_all(now=2)[0].status == "needs_attention"
    assert _runtime_health_checked_at(cp_stack) == 3
    assert cp_stack.database.query_one(
        "SELECT checked_at FROM email_activation_receipts"
        " WHERE email_identity_id = 'identity-health'"
    )["checked_at"] == 1
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_runtime_transport_failure_is_inconclusive(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)

    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic transport timeout", request=_request)

    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(fail)),
    )

    assert monitor.reconcile_all(now=4)[0].status == "unknown"
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_runtime_connection_refusal_fails_closed(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection refusal", request=_request)

    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )

    assert monitor.reconcile_all(now=4.5)[0].status == "needs_attention"
    assert _receipt_status(cp_stack) == "needs_attention"
    assert _identity_status(cp_stack) == "needs_attention"


def test_runtime_health_ownership_is_in_household_export(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_activation_receipts"
            " SET status = 'needs_attention', runtime_health_status = 'needs_attention',"
            " runtime_health_checked_at = 2, runtime_health_owns_attention = 1,"
            " runtime_health_identity_version = 2"
        )

    payload = HouseholdExporter(
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.onboarding,
        cp_stack.jobs,
        runtime=SyntheticRuntimeExporter(),
    ).export(cp_stack.account.id, cp_stack.household.id)

    assert payload["email_activation_receipts"][0]["runtime_health_status"] == (
        "needs_attention"
    )
    assert payload["email_activation_receipts"][0]["runtime_health_checked_at"] == 2
    assert payload["email_activation_receipts"][0][
        "runtime_health_owns_attention"
    ] == 1
    assert payload["email_activation_receipts"][0][
        "runtime_health_identity_version"
    ] == 2


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


def test_reset_during_observation_preserves_disconnecting_identity(cp_stack) -> None:
    runtime_ref = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
    household_id, revision = _seed_active_receipt(cp_stack, runtime_ref=runtime_ref)

    def reset_while_observing(_request: httpx.Request) -> httpx.Response:
        with cp_stack.database.write() as connection:
            connection.execute(
                "UPDATE email_identities SET status = 'disconnecting'"
                " WHERE id = 'identity-health'"
            )
            connection.execute(
                "UPDATE households SET status = 'onboarding' WHERE id = ?",
                (household_id,),
            )
        return httpx.Response(200, json={
            "status": "ready",
            "household_id": household_id,
            "config_revision": revision,
        })

    monitor = RuntimeReadinessMonitor(
        cp_stack.database,
        client=httpx.Client(transport=httpx.MockTransport(reset_while_observing)),
    )

    assert monitor.reconcile_all(now=5.5)[0].status == "active"
    assert _identity_status(cp_stack) == "disconnecting"
    assert _receipt_status(cp_stack) == "active"


def test_non_fly_runtime_reference_is_inconclusive(cp_stack) -> None:
    _seed_active_receipt(
        cp_stack,
        runtime_ref=f"synthetic-runtime:{cp_stack.household.id}",
    )
    monitor = RuntimeReadinessMonitor(cp_stack.database)

    assert monitor.reconcile_all(now=6)[0].status == "unknown"
    assert _receipt_status(cp_stack) == "active"
    assert _identity_status(cp_stack) == "active"


def test_runtime_health_cli_remains_available_while_server_owns_lock(
    monkeypatch, capsys
) -> None:
    lock_arguments: list[bool] = []
    active = SimpleNamespace(
        config=SimpleNamespace(runtime_provider="fly-runtime"),
        database=SimpleNamespace(workers_paused=False),
        runtime_health=SimpleNamespace(reconcile_all=lambda **_kwargs: []),
    )

    def container(*, lock: bool = True, mailer=None):
        lock_arguments.append(lock)
        return nullcontext(active)

    monkeypatch.setattr(cli, "_container", container)

    assert cli.main(["runtime-health"]) == 0
    assert lock_arguments == [False]
    assert capsys.readouterr().out.strip() == "[]"


def test_runtime_health_cli_does_not_project_while_workers_are_paused(
    monkeypatch, capsys
) -> None:
    calls: list[float] = []
    active = SimpleNamespace(
        config=SimpleNamespace(runtime_provider="fly-runtime"),
        database=SimpleNamespace(workers_paused=True),
        runtime_health=SimpleNamespace(
            reconcile_all=lambda **kwargs: calls.append(kwargs["now"])
        ),
    )
    monkeypatch.setattr(cli, "_container", lambda **_kwargs: nullcontext(active))

    assert cli.main(["runtime-health"]) == 0
    assert calls == []
    assert capsys.readouterr().out.strip() == "[]"


def test_runtime_health_next_sweep_is_scheduled_from_completion(monkeypatch) -> None:
    observed: list[float] = []
    active = SimpleNamespace(
        runtime_health=SimpleNamespace(
            reconcile_all=lambda **kwargs: observed.append(kwargs["now"])
        )
    )
    monkeypatch.setattr(cli.time, "time", lambda: 175.0)

    deadline = cli._reconcile_runtime_health(active, now=100.0)

    assert observed == [100.0]
    assert deadline == 235.0
