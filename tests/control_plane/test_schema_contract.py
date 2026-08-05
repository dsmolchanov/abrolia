from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from control_plane.crypto import SecretFieldError
from control_plane.db import ControlPlaneDatabase
from control_plane.models import TABLE_CLASSIFICATION


def test_every_control_plane_table_has_an_explicit_privacy_classification(cp_stack) -> None:
    actual = {
        row["name"]
        for row in cp_stack.database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert actual == set(TABLE_CLASSIFICATION)
    assert all(item.retention and (item.export or item.reason) for item in TABLE_CLASSIFICATION.values())


def test_schema_constraints_reject_impossible_states(cp_stack) -> None:
    with pytest.raises(sqlite3.IntegrityError), cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE onboarding_steps SET status = 'verified'"
            " WHERE kind = 'email_identity'"
        )
    with pytest.raises(sqlite3.IntegrityError), cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE accounts SET status = 'unknown' WHERE id = ?",
            (cp_stack.account.id,),
        )


@pytest.mark.parametrize(
    ("secret_field", "secret_value"),
    (
        ("client_secret", "GOCSPX-" + "oauth-client-secret-canary"),
        ("refresh_token", "1" + "//refresh-token-canary-0123456789"),
        ("nerve_bootstrap_key", "nrv_" + "live_bootstrap-canary-0123456789"),
        ("nerve_runtime_key", "nrv_" + "live_runtime-canary-0123456789"),
        ("webhook_secret", "0123456789abcdef" * 4),
    ),
)
def test_secret_shaped_job_payload_is_rejected_before_insert(
    cp_stack, secret_field: str, secret_value: str
) -> None:
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with pytest.raises(SecretFieldError), cp_stack.database.write() as connection:
        cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="secret-boundary-test",
            request={"provider": {secret_field: secret_value}},
            provider="fake-email",
        )
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE intent_key = 'secret-boundary-test'"
    ) == []
    assert secret_value.encode() not in Path(cp_stack.database.path).read_bytes()


def test_direct_identifiers_and_credentials_are_absent_from_sqlite_bytes(
    cp_stack,
) -> None:
    raw_email = "db-pii-canary@synthetic.test"
    raw_magic = "raw-magic-token-canary"
    raw_session = "raw-session-cookie-canary"
    raw_csrf = "raw-csrf-token-canary"
    first_name = "FirstnamePiiCanary"
    provider_value = "ProviderOpaqueCanary"

    account = cp_stack.accounts.create_verified(raw_email)
    household = cp_stack.households.create_for_owner(account.id)
    cp_stack.auth.issue_token(
        raw_magic,
        purpose="login",
        account_id=account.id,
        expires_at=1_900_000_000,
    )
    cp_stack.auth.create_session(
        raw_token=raw_session,
        raw_csrf=raw_csrf,
        account_id=account.id,
        idle_expires_at=1_850_000_000,
        absolute_expires_at=1_900_000_000,
        reauthenticated_at=1_800_000_000,
    )
    cp_stack.households.save_profile(
        household.id,
        cp_stack.valid_profile(first_name=first_name, last_name="LastnamePiiCanary"),
    )
    workflow = cp_stack.onboarding.workflow_for_household(household.id)
    with cp_stack.database.write() as connection:
        cp_stack.jobs.create(
            connection,
            household_id=household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="encrypted-payload-test",
            request={"opaque_value": provider_value},
            provider="fake-email",
        )

    path = Path(cp_stack.database.path)
    cp_stack.database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cp_stack.database.close()
    sqlite_bytes = b"".join(
        candidate.read_bytes()
        for candidate in (path, path.with_name(path.name + "-wal"))
        if candidate.exists()
    )
    for canary in (
        raw_email,
        raw_magic,
        raw_session,
        raw_csrf,
        first_name,
        provider_value,
    ):
        assert canary.encode() not in sqlite_bytes


def test_control_plane_migrations_do_not_touch_runtime_database_file(tmp_path: Path) -> None:
    control_path = tmp_path / "control-plane.db"
    runtime_path = tmp_path / "household.db"
    runtime_path.write_bytes(b"runtime-sentinel")
    database = ControlPlaneDatabase(control_path)
    try:
        database.migrate()
    finally:
        database.close()
    assert runtime_path.read_bytes() == b"runtime-sentinel"
