from __future__ import annotations

import pytest

from control_plane.email.models import EmailIdentityStatus
from control_plane.models import StepKind, StepStatus, WorkflowState
from control_plane.onboarding.contracts import (
    IdempotencyConflict,
    InvalidTransition,
    WorkflowConflict,
)
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.contracts import ProviderRegistry
from control_plane.provisioning.fakes import DeterministicFakeProvisioner

BASE_TIME = 1_800_000_000.0


_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
EMAIL_SELECTION = {
    "kind": "abrolia_managed",
    "local_part": "family-agent",
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000013",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
}
WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:owner-one",
    "privacy_notice_receipt_id": "synthetic-receipt-wa",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-owner-actor",
    "chat_id": "synthetic-family-chat",
}


def _step(snapshot, kind: StepKind):
    return next(step for step in snapshot.steps if step.kind is kind)


def test_steps_cannot_be_skipped_or_overwritten(cp_stack) -> None:
    with pytest.raises(InvalidTransition, match="skipped or reordered"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            EMAIL_SELECTION,
            context=cp_stack.context(expected_version=0),
            now=BASE_TIME + 1,
        )
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE operation != 'ensure_secret_namespace'"
    ) == []

    cp_stack.complete_profile(now=BASE_TIME + 2)
    with pytest.raises(InvalidTransition, match="skipped or reordered"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.WHATSAPP,
            WHATSAPP_SELECTION,
            context=cp_stack.context(),
            now=BASE_TIME + 3,
        )
    with pytest.raises(InvalidTransition):
        cp_stack.service.save_profile(
            cp_stack.household.id,
            cp_stack.valid_profile(first_name="SilentOverwrite"),
            context=cp_stack.context(),
            now=BASE_TIME + 4,
        )
    assert cp_stack.households.profile(cp_stack.household.id)["first_name"] == "Test"


def test_profile_idempotency_replays_same_body_and_rejects_changed_body(cp_stack) -> None:
    context = cp_stack.context(key="profile-command", expected_version=0)
    first = cp_stack.service.save_profile(
        cp_stack.household.id,
        cp_stack.valid_profile(),
        context=context,
        now=BASE_TIME + 1,
    )
    replay = cp_stack.service.save_profile(
        cp_stack.household.id,
        cp_stack.valid_profile(),
        context=context,
        now=BASE_TIME + 2,
    )
    assert not first.replayed
    assert replay.replayed
    assert replay.snapshot == first.snapshot
    assert len(cp_stack.database.query("SELECT id FROM onboarding_transitions")) == 1

    with pytest.raises(IdempotencyConflict, match="another body"):
        cp_stack.service.save_profile(
            cp_stack.household.id,
            cp_stack.valid_profile(first_name="Changed"),
            context=context,
            now=BASE_TIME + 3,
        )
    row = cp_stack.database.query_one(
        "SELECT idempotency_key_hmac FROM idempotency_requests"
    )
    assert row["idempotency_key_hmac"] == cp_stack.lookup.digest("profile-command")
    assert row["idempotency_key_hmac"] != "profile-command"


def test_expired_idempotency_key_is_replaced_atomically(cp_stack) -> None:
    context = cp_stack.context(key="expired-profile-command", expected_version=0)
    key_hmac = cp_stack.lookup.digest(context.idempotency_key)
    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO idempotency_requests (account_id, route, idempotency_key_hmac,"
            " request_sha, response_status, response_body_json, created_at, expires_at)"
            " VALUES (?, '/api/v1/onboarding/profile', ?, ?, 200, '{}', ?, ?)",
            (
                cp_stack.account.id,
                key_hmac,
                "0" * 64,
                BASE_TIME - 100,
                BASE_TIME - 1,
            ),
        )

    result = cp_stack.service.save_profile(
        cp_stack.household.id,
        cp_stack.valid_profile(),
        context=context,
        now=BASE_TIME + 1,
    )

    assert not result.replayed
    row = cp_stack.database.query_one(
        "SELECT created_at, expires_at, response_body_json FROM idempotency_requests"
        " WHERE account_id = ? AND route = '/api/v1/onboarding/profile'"
        " AND idempotency_key_hmac = ?",
        (cp_stack.account.id, key_hmac),
    )
    assert row["created_at"] == BASE_TIME + 1
    assert row["expires_at"] > row["created_at"]
    assert row["response_body_json"] != "{}"


def test_stale_if_match_conflicts_without_side_effects(cp_stack) -> None:
    cp_stack.complete_profile()
    before = cp_stack.onboarding.snapshot(cp_stack.household.id)
    with pytest.raises(WorkflowConflict, match="stale workflow version"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            EMAIL_SELECTION,
            context=cp_stack.context(expected_version=0),
            now=BASE_TIME + 2,
        )
    assert cp_stack.onboarding.snapshot(cp_stack.household.id) == before
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE operation != 'ensure_secret_namespace'"
    ) == []


def test_select_commits_exactly_one_durable_job_and_replays(cp_stack) -> None:
    cp_stack.complete_profile()
    context = cp_stack.context(key="select-email")
    first = cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=context,
        now=BASE_TIME + 2,
    )
    replay = cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=context,
        now=BASE_TIME + 3,
    )

    assert not first.replayed and replay.replayed
    jobs = cp_stack.database.query(
        "SELECT * FROM provisioning_jobs WHERE operation != 'ensure_secret_namespace'"
    )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["intent_key"].endswith(":abrolia_managed:1")
    assert _step(first.snapshot, StepKind.EMAIL).status is StepStatus.PROVISIONING

    changed = {**EMAIL_SELECTION, "local_part": "different-agent"}
    with pytest.raises(IdempotencyConflict):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            changed,
            context=context,
            now=BASE_TIME + 4,
        )


@pytest.mark.parametrize(
    ("starting_status", "expected_status", "expected_error"),
    [
        ("pending", "cancelled", "cancel_before_provider_call"),
        ("running", "outcome_unknown", "cancel_requires_reconciliation"),
        ("waiting_user", "outcome_unknown", "cancel_requires_reconciliation"),
    ],
)
def test_cancel_only_cancels_intents_that_never_crossed_provider_boundary(
    cp_stack,
    starting_status: str,
    expected_status: str,
    expected_error: str,
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = ?, leased_by = ?, lease_until = ?"
            " WHERE id = ?",
            (
                starting_status,
                "synthetic-in-flight-worker" if starting_status == "running" else None,
                BASE_TIME + 100 if starting_status == "running" else None,
                job["id"],
            ),
        )

    result = cp_stack.service.cancel(
        cp_stack.household.id,
        context=cp_stack.context(),
        now=BASE_TIME + 3,
    )

    current = cp_stack.database.query_one(
        "SELECT status, error_code, leased_by, lease_until FROM provisioning_jobs"
        " WHERE id = ?",
        (job["id"],),
    )
    assert result.snapshot.state is WorkflowState.CANCELLED
    assert current["status"] == expected_status
    assert current["error_code"] == expected_error
    assert current["leased_by"] is None and current["lease_until"] is None
    assert cp_stack.jobs.request(job["id"])["selection"] == {
        "kind": "abrolia_managed",
        "local_part": "family-agent",
    }
    stored_identity = cp_stack.email_identities.get(identity.id)
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity.id,),
    )
    if starting_status == "pending":
        assert stored_identity.status is EmailIdentityStatus.DELETED
        assert reservation["status"] == "released"
    else:
        assert stored_identity.status is EmailIdentityStatus.DISCONNECTING
        assert reservation["status"] == "held"


def test_failed_provider_attempt_has_a_new_explicit_retry_intent(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    fake = DeterministicFakeProvisioner("email", behavior="reject")
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    assert worker.run_once().status == "failed"
    failed = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert _step(failed, StepKind.EMAIL).status is StepStatus.FAILED
    fake.behavior = "success"
    retried = cp_stack.service.retry(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )
    assert _step(retried.snapshot, StepKind.EMAIL).status is StepStatus.PROVISIONING
    assert worker.run_once().status == "succeeded"

    jobs = cp_stack.database.query(
        "SELECT intent_key, status FROM provisioning_jobs WHERE kind = 'email_identity'"
        " ORDER BY created_at, id"
    )
    assert len(jobs) == 2
    assert {row["intent_key"].rsplit(":", 1)[-1] for row in jobs} == {"1", "2"}
    assert {row["status"] for row in jobs} == {"failed", "succeeded"}
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert _step(snapshot, StepKind.EMAIL).status is StepStatus.VERIFIED
    assert snapshot.current_step is StepKind.WHATSAPP


def test_waiting_user_check_is_a_durable_inspect_intent(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    fake = DeterministicFakeProvisioner("email", behavior="wait")
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    assert worker.run_once().status == "waiting_user"
    original = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE operation = 'ensure'"
    )
    checked = cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(key="check-email-once"),
        now=BASE_TIME + 4,
    )
    assert _step(checked.snapshot, StepKind.EMAIL).status is StepStatus.VERIFYING
    assert worker.run_once().status == "waiting_user"

    fake.complete_wait(original["intent_key"], {"selection": EMAIL_SELECTION})
    checked_again = cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(key="check-email-twice"),
        now=BASE_TIME + 5,
    )
    assert _step(checked_again.snapshot, StepKind.EMAIL).status is StepStatus.VERIFYING
    assert worker.run_once().status == "succeeded"
    final = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert _step(final, StepKind.EMAIL).status is StepStatus.VERIFIED
    assert fake.ensure_calls == 1
    assert len(cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE operation = 'inspect'"
    )) == 2


def test_reset_email_cancels_and_clears_all_downstream_state(cp_stack) -> None:
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    for offset, (kind, selection) in enumerate(
        (
            (StepKind.EMAIL, EMAIL_SELECTION),
            (StepKind.WHATSAPP, WHATSAPP_SELECTION),
            (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
        ),
        start=2,
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
            now=BASE_TIME + offset,
        )
        assert worker.run_once().status == "succeeded"

    before = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert before.state is WorkflowState.RUNTIME_PROVISIONING
    assert before.current_step is StepKind.RUNTIME
    assert all(
        _step(before, kind).status is StepStatus.VERIFIED
        for kind in (StepKind.EMAIL, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL)
    )
    assert cp_stack.database.query_one(
        "SELECT status FROM config_revisions WHERE household_id = ?",
        (cp_stack.household.id,),
    )["status"] == "planned"

    reset = cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 60,
    )
    assert reset.snapshot.state is WorkflowState.IN_PROGRESS
    assert reset.snapshot.current_step is StepKind.EMAIL
    assert _step(reset.snapshot, StepKind.EMAIL).status is StepStatus.AVAILABLE
    assert _step(reset.snapshot, StepKind.WHATSAPP).status is StepStatus.LOCKED
    assert _step(reset.snapshot, StepKind.PRIMARY_CHANNEL).status is StepStatus.LOCKED

    cleared = cp_stack.database.query(
        "SELECT kind, selection_ciphertext, result_ciphertext FROM onboarding_steps"
        " WHERE workflow_id = ? AND kind != 'profile'",
        (reset.snapshot.workflow_id,),
    )
    assert all(row["selection_ciphertext"] is None for row in cleared)
    assert all(row["result_ciphertext"] is None for row in cleared)
    assert len(cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup' AND status = 'pending'"
    )) == 3
    cleanup_jobs = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup' AND status = 'pending'"
        " ORDER BY created_at, id"
    )
    assert [
        cp_stack.jobs.request(job["id"])["resource_type"] for job in cleanup_jobs
    ] == ["email_identity", "whatsapp_identity", "channel_binding"]
    assert cp_stack.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE kind = 'runtime'"
        " AND operation = 'ensure_runtime'"
    )["status"] == "cancelled"
    assert cp_stack.database.query_one(
        "SELECT status FROM config_revisions WHERE household_id = ?",
        (cp_stack.household.id,),
    )["status"] == "revoked"
    household = cp_stack.households.get(cp_stack.household.id)
    assert household.status == "onboarding"
    assert household.runtime_ref is None


@pytest.mark.parametrize("unresolved_status", ["running", "waiting_user"])
def test_reset_quarantines_provider_crossed_runtime_intent(
    cp_stack, unresolved_status: str
) -> None:
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    for kind, selection in (
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
        )
        assert worker.run_once().status == "succeeded"
    runtime = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'runtime'"
        " AND operation = 'ensure_runtime'"
    )
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = ?, leased_by = ?, lease_until = ?"
            " WHERE id = ?",
            (
                unresolved_status,
                "synthetic-runtime-worker" if unresolved_status == "running" else None,
                BASE_TIME + 100 if unresolved_status == "running" else None,
                runtime["id"],
            ),
        )

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 60,
    )

    current = cp_stack.database.query_one(
        "SELECT status, error_code, leased_by, lease_until FROM provisioning_jobs"
        " WHERE id = ?",
        (runtime["id"],),
    )
    assert current["status"] == "outcome_unknown"
    assert current["error_code"] == "reset_requires_reconciliation"
    assert current["leased_by"] is None and current["lease_until"] is None
    assert cp_stack.jobs.request(runtime["id"])["manifest"]["household_id"] == (
        cp_stack.household.id
    )


def test_reset_whatsapp_preserves_verified_upstream_email_resource(cp_stack) -> None:
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    for kind, selection in (
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
        )
        assert worker.run_once().status == "succeeded"

    email_resource = cp_stack.database.query_one(
        "SELECT id FROM external_resources WHERE resource_type = 'email_identity'"
    )["id"]
    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.WHATSAPP,
        context=cp_stack.context(),
        now=BASE_TIME + 60,
    )
    email = cp_stack.database.query_one(
        "SELECT status FROM external_resources WHERE id = ?", (email_resource,)
    )
    cleanup_jobs = cp_stack.database.query(
        "SELECT request_ciphertext, id, encryption_key_version FROM provisioning_jobs"
        " WHERE kind = 'cleanup' ORDER BY created_at, id"
    )
    cleanup_types = {
        cp_stack.jobs.decrypt_json(
            "provisioning_jobs",
            row["id"],
            "request",
            row["request_ciphertext"],
            row["encryption_key_version"],
        )["resource_type"]
        for row in cleanup_jobs
    }
    assert email["status"] == "ready"
    assert cleanup_types == {"whatsapp_identity", "channel_binding"}
