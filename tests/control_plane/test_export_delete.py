from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import new_id
from control_plane.models import TABLE_CLASSIFICATION, StepKind
from control_plane.privacy.delete import DeletionService
from control_plane.privacy.export import HouseholdExporter, SyntheticRuntimeExporter
from control_plane.privacy.retention import RetentionService
from control_plane.privacy.runtime import PrivateRuntimeDsarClient
from control_plane.provisioning.contracts import InspectResult, InspectState
from control_plane.repositories.households import HouseholdNotFound

DAY = 24 * 60 * 60
BASE_TIME = 1_800_000_000.0
EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "export-agent"}


def _insert_consent(database, *, account_id: str, household_id: str, now: float) -> str:
    receipt_id = new_id()
    with database.write() as connection:
        connection.execute(
            "INSERT INTO consent_receipts (id, household_id, account_id, purpose,"
            " text_version, text_sha256, locale, accepted_at, created_at)"
            " VALUES (?, ?, ?, 'synthetic-pilot', 'v1', ?, 'en', ?, ?)",
            (receipt_id, household_id, account_id, "a" * 64, now, now),
        )
    return receipt_id


def test_export_contains_classified_domain_rows_but_no_credentials_or_ciphertext(
    cp_stack,
) -> None:
    raw_magic = "export-magic-token-canary"
    cp_stack.auth.issue_token(
        raw_magic,
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=BASE_TIME + DAY,
        now=BASE_TIME,
    )
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    receipt_id = _insert_consent(
        cp_stack.database,
        account_id=cp_stack.account.id,
        household_id=cp_stack.household.id,
        now=BASE_TIME + 3,
    )

    payload = HouseholdExporter(
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.onboarding,
        cp_stack.jobs,
        runtime=SyntheticRuntimeExporter(),
    ).export(cp_stack.account.id, cp_stack.household.id)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["account"]["recovery_email"] == cp_stack.account.recovery_email
    assert payload["profile"]["first_name"] == "Test"
    assert payload["steps"][1]["selection"] == EMAIL_SELECTION
    email_job = next(job for job in payload["jobs"] if job["kind"] == "email_identity")
    assert email_job["request"]["selection"] == EMAIL_SELECTION
    assert payload["email_identities"][0]["address"] == "export-agent@" + "abrolia.com"
    assert payload["email_identities"][0]["status"] == "provisioning"
    assert payload["email_address_reservations"][0]["normalized_local_part"] == (
        "export-agent"
    )
    assert "address_lookup_hmac" not in payload["email_identities"][0]
    assert "oauth_transactions" not in payload
    assert payload["consent_receipts"][0]["id"] == receipt_id
    assert payload["retention_exceptions"]["consent_receipts"]
    for credential in (
        raw_magic,
        cp_stack.session.token,
        cp_stack.session.csrf_token,
        cp_stack.token_hasher.digest(raw_magic),
    ):
        assert credential not in encoded
    for forbidden_key in (
        "token_hash",
        "csrf_hash",
        "ciphertext",
        "encryption_key_version",
        "rate_limit_buckets",
    ):
        assert forbidden_key not in encoded


def test_export_is_membership_scoped(cp_stack) -> None:
    other = cp_stack.accounts.create_verified("export-foreign@family.test")
    with pytest.raises(HouseholdNotFound):
        HouseholdExporter(
            cp_stack.accounts,
            cp_stack.households,
            cp_stack.onboarding,
            cp_stack.jobs,
            runtime=SyntheticRuntimeExporter(),
        ).export(other.id, cp_stack.household.id)


def test_every_table_has_export_delete_and_retention_policy(cp_stack) -> None:
    tables = {
        row["name"]
        for row in cp_stack.database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == set(TABLE_CLASSIFICATION)
    assert all(isinstance(policy.export, bool) for policy in TABLE_CLASSIFICATION.values())
    assert all(isinstance(policy.delete, bool) for policy in TABLE_CLASSIFICATION.values())
    assert all(policy.retention.strip() for policy in TABLE_CLASSIFICATION.values())
    assert not TABLE_CLASSIFICATION["consent_receipts"].delete
    assert not TABLE_CLASSIFICATION["deletion_tombstones"].delete


def test_fly_wiring_never_defaults_to_synthetic_runtime_dsar_boundaries(tmp_path) -> None:
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        runtime_provider="fly-runtime",
        fly_api_token="synthetic-fly-token",
        fly_org_slug="synthetic-org",
        runtime_image_digest="registry.example.test/runtime@sha256:" + "a" * 64,
        runtime_model_api_key="synthetic-model-key",
    ).validate()
    with ControlPlaneContainer.build(config) as active:
        assert isinstance(active.exporter.runtime, PrivateRuntimeDsarClient)
        assert active.deletion.runtime is active.exporter.runtime
        active.providers.get("fly-runtime").client.close()


@dataclass
class RecordingRuntimeDeleter:
    state: InspectState
    calls: list[str]

    def delete(self, runtime_ref: str) -> InspectState:
        self.calls.append(runtime_ref)
        return self.state


@dataclass
class MutableDeleteProvider:
    state: InspectState

    def deprovision(self, _external_ref: object) -> InspectResult:
        return InspectResult(self.state)


class FailingRuntimeBoundary:
    def export(self, runtime_ref: str) -> dict:
        raise RuntimeError(f"private provider body for {runtime_ref}")

    def delete(self, runtime_ref: str) -> InspectState:
        raise RuntimeError(f"private provider body for {runtime_ref}")


def _deletion_service(api_harness, runtime: RecordingRuntimeDeleter) -> DeletionService:
    active = api_harness.container
    return DeletionService(
        active.accounts,
        active.auth,
        active.households,
        active.jobs,
        active.providers,
        runtime=runtime,
    )


def test_complete_delete_revokes_credentials_deprovisions_and_retains_consent_tombstone(
    api_harness,
) -> None:
    active = api_harness.container
    now = BASE_TIME + 10
    world = api_harness.create_principal("delete-owner@family.test", now=BASE_TIME)
    raw_magic = "delete-magic-token-canary"
    active.auth.issue_token(
        raw_magic,
        purpose="login",
        account_id=world.account.id,
        expires_at=BASE_TIME + DAY,
        now=BASE_TIME,
    )
    _insert_consent(
        active.database,
        account_id=world.account.id,
        household_id=world.household.id,
        now=BASE_TIME,
    )
    with active.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            ("synthetic-runtime:delete", world.household.id),
        )
        resource_id = new_id()
        encrypted = active.jobs.encrypt_json(
            "external_resources", resource_id, "external_id", "synthetic:email:delete"
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at) VALUES (?, ?, 'fake-email', 'email_identity', ?, ?, ?,"
            " 'ready', ?, ?)",
            (
                resource_id,
                world.household.id,
                "delete-email-resource",
                encrypted.ciphertext,
                encrypted.key_version,
                BASE_TIME,
                BASE_TIME,
            ),
        )
    runtime = RecordingRuntimeDeleter(InspectState.ABSENT, [])
    result = _deletion_service(api_harness, runtime).delete(
        world.account.id,
        world.household.id,
        idempotency_key="complete-delete-command",
        now=now,
    )

    assert result.completion_status == "complete"
    assert runtime.calls == ["synthetic-runtime:delete"]
    assert set(result.provider_statuses.values()) == {"absent"}
    assert result.retained_consent_receipts == 1
    assert active.accounts.get(world.account.id) is None
    assert active.households.get(world.household.id) is None
    assert active.database.query(
        "SELECT id FROM sessions WHERE account_id = ?", (world.account.id,)
    ) == []
    assert active.database.query(
        "SELECT id FROM auth_tokens WHERE account_id = ?", (world.account.id,)
    ) == []
    consent = active.database.query_one("SELECT * FROM consent_receipts")
    assert consent["account_id"] is None and consent["household_id"] is None
    tombstone = active.database.query_one("SELECT * FROM deletion_tombstones")
    assert tombstone["household_id_hmac"] == active.lookup.digest(world.household.id)
    assert tombstone["household_id_hmac"] != world.household.id
    assert tombstone["completion_status"] == "complete"


def test_unknown_delete_is_reported_honestly_and_can_be_resumed(api_harness) -> None:
    active = api_harness.container
    world = api_harness.create_principal("partial-delete@family.test", now=BASE_TIME)
    with active.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            ("synthetic-runtime:partial", world.household.id),
        )
    runtime = RecordingRuntimeDeleter(InspectState.UNKNOWN, [])
    service = _deletion_service(api_harness, runtime)
    first = service.delete(
        world.account.id,
        world.household.id,
        idempotency_key="partial-delete-command",
        now=BASE_TIME + 1,
    )
    assert first.completion_status == "outcome_unknown"
    assert active.households.get(world.household.id).status == "deleting"
    assert active.accounts.get(world.account.id).status == "deleting"
    session = active.database.query_one(
        "SELECT revoked_at FROM sessions WHERE id = ?", (world.session.id,)
    )
    assert session["revoked_at"] == BASE_TIME + 1
    idempotency = active.database.query_one(
        "SELECT idempotency_key_hmac FROM idempotency_requests WHERE account_id = ?"
        " AND route = '/api/v1/onboarding/delete'",
        (world.account.id,),
    )
    assert idempotency["idempotency_key_hmac"] == active.lookup.digest(
        "partial-delete-command"
    )
    assert idempotency["idempotency_key_hmac"] != "partial-delete-command"

    replay = service.delete(
        world.account.id,
        world.household.id,
        idempotency_key="partial-delete-command",
        now=BASE_TIME + 2,
    )
    assert replay.replayed
    assert replay.completion_status == first.completion_status
    assert runtime.calls == ["synthetic-runtime:partial"]

    runtime.state = InspectState.ABSENT
    resumed = service.resume_pending(now=BASE_TIME + 3)
    assert len(resumed) == 1
    second = resumed[0]
    assert second.completion_status == "complete"
    assert active.households.get(world.household.id) is None


def test_confirmed_runtime_wipe_survives_unknown_fly_deprovision(api_harness) -> None:
    active = api_harness.container
    world = api_harness.create_principal("durable-runtime-wipe@family.test", now=BASE_TIME)
    runtime = RecordingRuntimeDeleter(InspectState.ABSENT, [])
    provider = MutableDeleteProvider(InspectState.UNKNOWN)
    active.providers.register("mutable-delete-provider", provider)
    resource_id = new_id()
    external_ref = {"app_ref": "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"}
    encrypted = active.jobs.encrypt_json(
        "external_resources", resource_id, "external_id", external_ref
    )
    with active.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            ("synthetic-runtime:durable-wipe", world.household.id),
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at) VALUES (?, ?, 'mutable-delete-provider', 'runtime',"
            " 'durable-runtime', ?, ?, 'ready', ?, ?)",
            (
                resource_id,
                world.household.id,
                encrypted.ciphertext,
                encrypted.key_version,
                BASE_TIME,
                BASE_TIME,
            ),
        )
    service = _deletion_service(api_harness, runtime)

    first = service.delete(
        world.account.id,
        world.household.id,
        idempotency_key="durable-runtime-wipe-command",
        now=BASE_TIME + 1,
    )
    assert first.completion_status == "outcome_unknown"
    assert runtime.calls == ["synthetic-runtime:durable-wipe"]
    assert active.households.get(world.household.id).runtime_deleted_at == BASE_TIME + 1

    runtime.state = InspectState.UNKNOWN
    provider.state = InspectState.ABSENT
    resumed = service.resume(world.household.id, now=BASE_TIME + 2)
    assert resumed.completion_status == "complete"
    assert runtime.calls == ["synthetic-runtime:durable-wipe"]


def test_completed_household_delete_removes_replay_body_when_account_remains(
    api_harness,
) -> None:
    active = api_harness.container
    world = api_harness.create_principal("multi-member@family.test", now=BASE_TIME)
    other = api_harness.create_principal("other-household@family.test", now=BASE_TIME)
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role, status,"
            " created_at, accepted_at) VALUES (?, ?, 'adult', 'active', ?, ?)",
            (world.account.id, other.household.id, BASE_TIME, BASE_TIME),
        )
    result = _deletion_service(
        api_harness, RecordingRuntimeDeleter(InspectState.ABSENT, [])
    ).delete(
        world.account.id,
        world.household.id,
        idempotency_key="multi-membership-delete",
        now=BASE_TIME + 1,
    )
    assert result.completion_status == "complete"
    assert active.accounts.get(world.account.id).status == "active"
    assert active.households.get(other.household.id) is not None
    assert active.database.query(
        "SELECT * FROM idempotency_requests WHERE account_id = ?",
        (world.account.id,),
    ) == []
    assert world.household.id not in "\n".join(active.database.connection.iterdump())


def test_export_and_delete_are_owner_only_for_active_adult(api_harness) -> None:
    active = api_harness.container
    owner = api_harness.create_principal("privacy-owner@family.test", now=BASE_TIME)
    adult = active.accounts.create_verified("privacy-adult@family.test", now=BASE_TIME)
    adult_session = active.sessions.issue(adult.id, now=BASE_TIME)
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role, status,"
            " created_at, accepted_at) VALUES (?, ?, 'adult', 'active', ?, ?)",
            (adult.id, owner.household.id, BASE_TIME, BASE_TIME),
        )

    exporter = HouseholdExporter(
        active.accounts,
        active.households,
        active.onboarding_repository,
        active.jobs,
        runtime=SyntheticRuntimeExporter(),
    )
    with pytest.raises(HouseholdNotFound):
        exporter.export(adult.id, owner.household.id)
    with pytest.raises(HouseholdNotFound):
        _deletion_service(
            api_harness, RecordingRuntimeDeleter(InspectState.ABSENT, [])
        ).delete(
            adult.id,
            owner.household.id,
            idempotency_key="adult-delete-denied",
            now=BASE_TIME + 1,
        )

    api_harness.client.cookies.set(
        api_harness.config.session_cookie_name, adult_session.token
    )
    api_harness.client.cookies.set(
        api_harness.config.csrf_cookie_name, adult_session.csrf_token
    )
    assert api_harness.client.get("/api/v1/onboarding/export").status_code == 404
    workflow = active.onboarding_repository.workflow_for_household(owner.household.id)
    response = api_harness.client.post(
        "/api/v1/onboarding/delete",
        headers={
            **api_harness.mutation_headers,
            "Idempotency-Key": "adult-api-delete-denied",
            "If-Match": str(workflow.version),
        },
    )
    assert response.status_code == 404
    assert active.households.get(owner.household.id).status == "draft"
    assert active.sessions.authenticate(adult_session.token).account_id == adult.id


def test_runtime_boundary_errors_are_redacted_and_never_report_complete(
    api_harness,
) -> None:
    active = api_harness.container
    world = api_harness.create_principal("runtime-boundary@family.test", now=BASE_TIME)
    runtime_ref = "synthetic-runtime:boundary-error"
    with active.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            (runtime_ref, world.household.id),
        )

    payload = HouseholdExporter(
        active.accounts,
        active.households,
        active.onboarding_repository,
        active.jobs,
        runtime=FailingRuntimeBoundary(),
    ).export(world.account.id, world.household.id)
    assert payload["completion_status"] == "outcome_unknown"
    assert payload["runtime"] == {
        "status": "outcome_unknown",
        "data": None,
        "error_code": "runtime_export_unavailable",
    }
    assert "private provider body" not in json.dumps(payload)

    deletion = DeletionService(
        active.accounts,
        active.auth,
        active.households,
        active.jobs,
        active.providers,
        runtime=FailingRuntimeBoundary(),
    )
    result = deletion.delete(
        world.account.id,
        world.household.id,
        idempotency_key="runtime-boundary-delete",
        now=BASE_TIME + 1,
    )
    assert result.completion_status == "outcome_unknown"
    assert result.runtime_status == "unknown"
    assert active.households.get(world.household.id).status == "deleting"


def test_unavailable_external_provider_is_caught_and_reported_unknown(
    api_harness,
) -> None:
    active = api_harness.container
    world = api_harness.create_principal("provider-boundary@family.test", now=BASE_TIME)
    resource_id = new_id()
    external_ref = "synthetic:disabled:private-provider-body"
    encrypted = active.jobs.encrypt_json(
        "external_resources", resource_id, "external_id", external_ref
    )
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at) VALUES (?, ?, 'disabled-provider', 'email_identity',"
            " 'disabled-resource', ?, ?, 'outcome_unknown', ?, ?)",
            (
                resource_id,
                world.household.id,
                encrypted.ciphertext,
                encrypted.key_version,
                BASE_TIME,
                BASE_TIME,
            ),
        )
    result = _deletion_service(
        api_harness, RecordingRuntimeDeleter(InspectState.ABSENT, [])
    ).delete(
        world.account.id,
        world.household.id,
        idempotency_key="provider-boundary-delete",
        now=BASE_TIME + 1,
    )
    assert result.completion_status == "outcome_unknown"
    assert set(result.provider_statuses.values()) == {"unknown"}
    assert external_ref not in json.dumps(result.public_dict())
    resource = active.database.query_one(
        "SELECT status FROM external_resources WHERE id = ?", (resource_id,)
    )
    assert resource["status"] == "outcome_unknown"


def test_owner_delete_api_executes_with_fresh_reauth_and_preconditions(
    api_harness,
) -> None:
    active = api_harness.container
    world = api_harness.create_principal("api-delete-owner@family.test")
    api_harness.authenticate(world)
    workflow = active.onboarding_repository.workflow_for_household(world.household.id)
    response = api_harness.client.post(
        "/api/v1/onboarding/delete",
        headers={
            **api_harness.mutation_headers,
            "Idempotency-Key": "owner-api-delete-command",
            "If-Match": f'W/"{workflow.version}"',
        },
    )
    assert response.status_code == 200
    assert response.json()["completion_status"] == "complete"
    assert active.households.get(world.household.id) is None
    assert api_harness.client.get("/api/v1/me").status_code == 401


def test_preexisting_outcome_unknown_job_blocks_until_background_resume(
    api_harness,
) -> None:
    active = api_harness.container
    world = api_harness.create_principal("unknown-job@family.test", now=BASE_TIME)
    workflow = active.onboarding_repository.workflow_for_household(world.household.id)
    with active.database.write() as connection:
        job_id, _ = active.jobs.create(
            connection,
            household_id=world.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="preexisting-unknown-job",
            request={"selection": {"kind": "abrolia_managed"}},
            provider="fake-email",
            now=BASE_TIME,
        )
        active.jobs.settle(
            connection,
            job_id,
            status="outcome_unknown",
            error_code="synthetic_unknown",
            now=BASE_TIME,
        )
    service = _deletion_service(
        api_harness, RecordingRuntimeDeleter(InspectState.ABSENT, [])
    )
    first = service.delete(
        world.account.id,
        world.household.id,
        idempotency_key="unknown-job-delete",
        now=BASE_TIME + 1,
    )
    assert first.completion_status == "outcome_unknown"
    assert active.households.get(world.household.id).status == "deleting"

    with active.database.write() as connection:
        active.jobs.settle(
            connection,
            job_id,
            status="failed",
            error_code="inspect_proved_absent",
            now=BASE_TIME + 2,
        )
    resumed = service.resume_pending(now=BASE_TIME + 3)
    assert [item.completion_status for item in resumed] == ["complete"]
    assert active.households.get(world.household.id) is None


def test_retention_sweeps_short_lived_rows_and_scrubs_before_metadata_delete(
    cp_stack,
) -> None:
    now = BASE_TIME + 200 * DAY
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    cp_stack.auth.issue_token(
        "retention-old-magic",
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=now - 2 * DAY,
        now=BASE_TIME,
    )
    fresh_token_id = cp_stack.auth.issue_token(
        "retention-fresh-magic",
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=now + DAY,
        now=now,
    )
    fresh_session_id = cp_stack.auth.create_session(
        raw_token="retention-fresh-session",
        raw_csrf="retention-fresh-csrf",
        account_id=cp_stack.account.id,
        idle_expires_at=now + DAY,
        absolute_expires_at=now + 2 * DAY,
        reauthenticated_at=now,
        now=now,
    )
    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO rate_limit_buckets VALUES ('old-rate', 'test', ?, 1, ?)",
            (now - 2 * DAY, now - 2 * DAY),
        )
        connection.execute(
            "INSERT INTO rate_limit_buckets VALUES ('fresh-rate', 'test', ?, 1, ?)",
            (now, now),
        )
        for key, expires in (("old-idem", now - 1), ("fresh-idem", now + DAY)):
            connection.execute(
                "INSERT INTO idempotency_requests (account_id, route, idempotency_key_hmac,"
                " request_sha, response_status, response_body_json, created_at, expires_at)"
                " VALUES (?, '/retention', ?, ?, 200, '{}', ?, ?)",
                (
                    cp_stack.account.id,
                    cp_stack.lookup.digest(key),
                    "b" * 64,
                    BASE_TIME,
                    expires,
                ),
            )
        job_31, _ = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="retention-job-31",
            request={"opaque": "payload-31-canary"},
            provider="fake-email",
            now=BASE_TIME,
        )
        cp_stack.jobs.settle(
            connection,
            job_31,
            status="succeeded",
            result={"opaque": "result-31-canary"},
            now=now - 31 * DAY,
        )
        job_91, _ = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="retention-job-91",
            request={"opaque": "payload-91-canary"},
            provider="fake-email",
            now=BASE_TIME,
        )
        cp_stack.jobs.settle(
            connection,
            job_91,
            status="succeeded",
            result={"opaque": "result-91-canary"},
            now=now - 91 * DAY,
        )
        unresolved_job, _ = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="retention-unresolved-job",
            request={"opaque": "unresolved-payload-canary"},
            provider="fake-email",
            now=BASE_TIME,
        )
        cp_stack.jobs.settle(
            connection,
            unresolved_job,
            status="outcome_unknown",
            error_code="provider_outcome_unknown",
            now=now - 120 * DAY,
        )
        manifest = {"schema_version": 1, "config_revision": 1, "household_id": cp_stack.household.id}
        revision = cp_stack.configs.create_revision(
            connection,
            household_id=cp_stack.household.id,
            schema_version=1,
            manifest=manifest,
            manifest_sha256="c" * 64,
            now=BASE_TIME,
        )
        token_31 = cp_stack.configs.issue_bootstrap(
            connection,
            raw_token="retention-bootstrap-31",
            household_id=cp_stack.household.id,
            runtime_ref="synthetic-runtime:retention",
            revision=revision.revision,
            manifest_sha256=revision.manifest_sha256,
            expires_at=now + DAY,
            now=BASE_TIME,
        )
        token_91 = cp_stack.configs.issue_bootstrap(
            connection,
            raw_token="retention-bootstrap-91",
            household_id=cp_stack.household.id,
            runtime_ref="synthetic-runtime:retention",
            revision=revision.revision,
            manifest_sha256=revision.manifest_sha256,
            expires_at=now + DAY,
            now=BASE_TIME,
        )
        connection.execute(
            "UPDATE bootstrap_tokens SET used_at = ?, revoked_at = NULL WHERE id = ?",
            (now - 31 * DAY, token_31),
        )
        connection.execute(
            "UPDATE bootstrap_tokens SET used_at = ?, revoked_at = NULL WHERE id = ?",
            (now - 91 * DAY, token_91),
        )
        orphan_consent = new_id()
        connection.execute(
            "INSERT INTO consent_receipts (id, purpose, text_version, text_sha256, locale,"
            " accepted_at, revoked_at, created_at) VALUES (?, 'old', 'v1', ?, 'en', ?, ?, ?)",
            (orphan_consent, "d" * 64, BASE_TIME, now - 3 * 365 * DAY - 1, BASE_TIME),
        )
        expired_tombstone = cp_stack.lookup.digest("expired-household")
        connection.execute(
            "INSERT INTO deletion_tombstones VALUES (?, ?, ?, 'complete', ?)",
            (expired_tombstone, BASE_TIME, now - 1, BASE_TIME),
        )

    result = RetentionService(cp_stack.accounts).run(now=now)
    assert result.scrubbed["provisioning_job_payloads"] >= 1
    assert result.scrubbed["bootstrap_token_hashes"] >= 1
    assert cp_stack.database.query_one(
        "SELECT id FROM auth_tokens WHERE id = ?", (fresh_token_id,)
    ) is not None
    assert cp_stack.database.query_one(
        "SELECT id FROM sessions WHERE id = ?", (fresh_session_id,)
    ) is not None
    assert cp_stack.database.query_one(
        "SELECT bucket_hmac FROM rate_limit_buckets WHERE bucket_hmac = 'old-rate'"
    ) is None
    assert cp_stack.database.query_one(
        "SELECT bucket_hmac FROM rate_limit_buckets WHERE bucket_hmac = 'fresh-rate'"
    ) is not None
    assert cp_stack.database.query_one(
        "SELECT idempotency_key_hmac FROM idempotency_requests"
        " WHERE idempotency_key_hmac = ?",
        (cp_stack.lookup.digest("old-idem"),),
    ) is None
    assert cp_stack.jobs.get(job_91) is None
    assert cp_stack.jobs.get(job_31) is not None
    assert cp_stack.jobs.request(job_31) == {}
    assert cp_stack.jobs.result(job_31) is None
    assert cp_stack.jobs.get(unresolved_job).status == "outcome_unknown"
    assert cp_stack.jobs.request(unresolved_job) == {
        "opaque": "unresolved-payload-canary"
    }
    bootstrap_31 = cp_stack.database.query_one(
        "SELECT token_hash FROM bootstrap_tokens WHERE id = ?", (token_31,)
    )
    assert bootstrap_31["token_hash"] == f"retained:{token_31}"
    assert cp_stack.database.query_one(
        "SELECT id FROM bootstrap_tokens WHERE id = ?", (token_91,)
    ) is None
    assert cp_stack.database.query_one(
        "SELECT id FROM consent_receipts WHERE id = ?", (orphan_consent,)
    ) is None
    assert cp_stack.database.query_one(
        "SELECT household_id_hmac FROM deletion_tombstones WHERE household_id_hmac = ?",
        (expired_tombstone,),
    ) is None


def test_the_export_carries_the_household_s_channel_bindings(cp_stack) -> None:
    """`channel_bindings` is marked exportable and `docs/privacy/data-map.md`
    promises it. Nothing wrote a row until C3, so the omission cost nothing;
    the table now holds channel identities, which is precisely what a subject
    access request exists to return.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection,
            household_id=hid,
            channel="telegram",
            external_id="synthetic-owner-chat",
            actor_id="synthetic-owner",
            now=BASE_TIME,
        )
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=hid,
            channel="whatsapp",
            external_id="+999511234567",
            actor_id="synthetic-adult",
            role="adult",
            issued_by_actor_id="synthetic-owner",
            now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=hid,
            owner_actor_id="synthetic-owner",
            now=BASE_TIME + 1,
        )

    document = HouseholdExporter(
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.onboarding,
        cp_stack.jobs,
        runtime=SyntheticRuntimeExporter(),
    ).export(cp_stack.account.id, hid)
    bindings = document["channel_bindings"]

    assert [(b["channel"], b["external_id"], b["role"]) for b in bindings] == [
        ("telegram", "synthetic-owner-chat", "owner"),
        ("whatsapp", "+999511234567", "adult"),
    ]
    assert bindings[1]["verified_by_actor_id"] == "synthetic-owner"

    # A credential never rides along: the keyed lookup digest adds nothing for
    # the reader, and the challenge table is export=False by classification.
    serialized = json.dumps(document)
    assert "external_id_hmac" not in serialized
    assert issued.code not in serialized
    assert "code_hash" not in serialized
