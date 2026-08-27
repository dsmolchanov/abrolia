from __future__ import annotations

import json
import time
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest

from control_plane.api.internal_bootstrap import _bootstrap_transport_allowed
from control_plane.db import new_id
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.bootstrap import (
    BootstrapConflict,
    BootstrapDenied,
    BootstrapGone,
)
from hermes_cloud.runtime import bootstrap as runtime_bootstrap_module
from hermes_cloud.runtime.bootstrap import (
    BootstrapError,
    BootstrapOutcomeUnknown,
    ControlPlaneBootstrapClient,
    RuntimeBootstrapper,
    load_activation_state,
)
from hermes_cloud.runtime.service import RuntimeService

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
EMAIL_SELECTION = {
    "kind": "abrolia_managed",
    "local_part": "bootstrap-agent",
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000011",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
}
WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:bootstrap-owner",
    "privacy_notice_receipt_id": "synthetic-bootstrap-wa-receipt",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-bootstrap-owner",
    "chat_id": "synthetic-bootstrap-chat",
}


def test_bootstrap_origin_allows_https_or_private_flycast_http_only() -> None:
    private = ControlPlaneBootstrapClient("http://control-plane.flycast")
    public = ControlPlaneBootstrapClient("https://control-plane.example.test")

    assert private.base_url == "http://control-plane.flycast"
    assert public.base_url == "https://control-plane.example.test"

    for invalid in (
        "http://control-plane.example.test",
        "http://control-plane.internal:8080",
        "http://control-plane.flycast:8080",
        "https://control-plane.example.test:8443",
        "http://user" + "@control-plane.flycast",
        "http://control-plane.flycast/path",
    ):
        with pytest.raises(BootstrapError, match="HTTPS or private Flycast HTTP"):
            ControlPlaneBootstrapClient(invalid)


@pytest.mark.parametrize(
    ("scheme", "hostname", "expected_host", "allowed"),
    (
        ("http", "control-plane.flycast", "control-plane.flycast", True),
        ("https", "control-plane.example.test", "control-plane.example.test", True),
        ("http", "control-plane.example.test", "control-plane.example.test", False),
        ("http", "other.flycast", "control-plane.flycast", False),
        ("https", "public.example.test", "control-plane.flycast", False),
        ("http", "control-plane.flycast", None, False),
    ),
)
def test_bootstrap_api_transport_matches_client_contract(
    scheme: str,
    hostname: str,
    expected_host: str | None,
    allowed: bool,
) -> None:
    assert (
        _bootstrap_transport_allowed(
            scheme=scheme,
            hostname=hostname,
            expected_host=expected_host,
        )
        is allowed
    )


@dataclass(frozen=True)
class RuntimeBootstrap:
    world: object
    raw_token: str
    runtime_ref: str
    revision: int
    manifest_sha256: str


def _context(container, world, sequence: int) -> CommandContext:
    version = container.onboarding_repository.workflow_for_household(world.household.id).version
    return CommandContext(
        account_id=world.account.id,
        session_id=world.session.id,
        request_id=f"bootstrap-request-{sequence}",
        idempotency_key=f"bootstrap-command-{sequence}",
        expected_version=version,
    )


def _provision_runtime(api_harness) -> RuntimeBootstrap:
    active = api_harness.container
    world = api_harness.create_principal("bootstrap-owner@family.test")
    active.onboarding.save_profile(
        world.household.id,
        ProfileInput(
            first_name="Bootstrap",
            last_name="Owner",
            family_language="en",
            timezone="Europe/Prague",
            country_code="DE",
            residency_mode="eu-app",
        ),
        context=_context(active, world, 1),
    )
    for sequence, (kind, selection) in enumerate(
        (
            (StepKind.EMAIL, EMAIL_SELECTION),
            (StepKind.WHATSAPP, WHATSAPP_SELECTION),
            (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
        ),
        start=2,
    ):
        active.onboarding.select(
            world.household.id,
            kind,
            selection,
            context=_context(active, world, sequence),
        )
        assert active.worker.run_once().status == "succeeded"
    runtime = active.worker.run_once()
    assert runtime.status == "succeeded"
    household = active.households.get(world.household.id)
    revision = active.configs.get(world.household.id, 1)
    raw = active.secret_sink.get(household.runtime_ref, "HERMES_BOOTSTRAP_TOKEN")
    if raw is None:
        # Transitional fallback keeps protocol tests focused; the dedicated
        # contract test below rejects the legacy name for the completed phase.
        raw = active.secret_sink.get(household.runtime_ref, "ABROLIA_BOOTSTRAP_TOKEN")
    assert raw is not None
    return RuntimeBootstrap(
        world,
        raw.decode("ascii"),
        household.runtime_ref,
        revision.revision,
        revision.manifest_sha256,
    )


def _binding(runtime: RuntimeBootstrap) -> dict[str, object]:
    return {
        "household_id": runtime.world.household.id,
        "runtime_ref": runtime.runtime_ref,
        "config_revision": runtime.revision,
    }


def test_runtime_stages_only_expected_hermes_runtime_secrets(api_harness) -> None:
    runtime = _provision_runtime(api_harness)
    sink = api_harness.container.secret_sink
    assert sink.get(runtime.runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None
    assert sink.get(runtime.runtime_ref, "ABROLIA_BOOTSTRAP_TOKEN") is None
    assert sink.get(runtime.runtime_ref, "HERMES_RUNTIME_DSAR_TOKEN") == (
        api_harness.container.configs.token_hasher.digest(f"runtime-dsar:{runtime.runtime_ref}").encode(
            "ascii"
        )
    )


@pytest.mark.parametrize("changed", ["household_id", "runtime_ref", "config_revision"])
def test_bootstrap_token_is_bound_to_household_runtime_and_revision(api_harness, changed: str) -> None:
    runtime = _provision_runtime(api_harness)
    binding = _binding(runtime)
    binding[changed] = {
        "household_id": new_id(),
        "runtime_ref": "synthetic-runtime:foreign",
        "config_revision": runtime.revision + 1,
    }[changed]
    with pytest.raises(BootstrapDenied, match="binding mismatch"):
        api_harness.container.bootstrap.claim(runtime.raw_token, **binding)
    row = api_harness.container.database.query_one(
        "SELECT claimed_at, used_at FROM bootstrap_tokens WHERE household_id = ?",
        (runtime.world.household.id,),
    )
    assert row["claimed_at"] is None and row["used_at"] is None


def test_activation_rechecks_content_restriction_after_runtime_issue(
    api_harness,
) -> None:
    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    active.bootstrap.claim(runtime.raw_token, **binding)
    with active.database.write() as connection:
        connection.execute(
            "DELETE FROM consent_receipts WHERE household_id = ? AND purpose = ?",
            (runtime.world.household.id, "special_category_content_restriction"),
        )

    with pytest.raises(BootstrapConflict, match="activation consent receipt"):
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256=runtime.manifest_sha256,
        )
    assert active.households.get(runtime.world.household.id).status == "provisioning"
    token = active.database.query_one(
        "SELECT used_at FROM bootstrap_tokens WHERE household_id = ?",
        (runtime.world.household.id,),
    )
    assert token["used_at"] is None


def test_claim_and_activation_are_resumable_and_cleanup_is_durable(
    api_harness,
) -> None:
    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    with pytest.raises(BootstrapConflict, match="claimed before activation"):
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256=runtime.manifest_sha256,
        )

    first = active.bootstrap.claim(runtime.raw_token, **binding)
    resumed = active.bootstrap.claim(runtime.raw_token, **binding)
    assert resumed == first
    encoded = json.dumps(first.manifest, sort_keys=True)
    assert runtime.raw_token not in encoded
    assert "token" not in encoded.casefold()

    with pytest.raises(BootstrapConflict, match="hash mismatch"):
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256="0" * 64,
        )
    assert active.households.get(runtime.world.household.id).status == "provisioning"
    assert active.configs.get(runtime.world.household.id, 1).status == "claimed"

    receipt = active.bootstrap.activate(
        runtime.raw_token,
        **binding,
        activated_sha256=runtime.manifest_sha256,
    )
    assert not receipt.cleanup_pending
    assert active.households.get(runtime.world.household.id).status == "active"
    assert active.configs.get(runtime.world.household.id, 1).status == "active"
    workflow = active.onboarding_repository.workflow_for_household(runtime.world.household.id)
    assert workflow.state == "complete"
    cleanup = active.database.query_one("SELECT * FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'")
    assert cleanup is None

    with pytest.raises(BootstrapGone):
        active.bootstrap.claim(runtime.raw_token, **binding)
    replayed = active.bootstrap.activate(
        runtime.raw_token,
        **binding,
        activated_sha256=runtime.manifest_sha256,
    )
    assert replayed == receipt
    assert (
        active.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'"
        )["count"]
        == 0
    )
    with pytest.raises(BootstrapConflict, match="hash mismatch"):
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256="0" * 64,
        )
    with active.database.write() as connection:
        connection.execute(
            "DELETE FROM consent_receipts WHERE household_id = ? AND purpose = ?",
            (runtime.world.household.id, "special_category_content_restriction"),
        )
    acknowledged = active.bootstrap.activate(
        runtime.raw_token,
        **binding,
        activated_sha256=runtime.manifest_sha256,
        receipt_acknowledged=True,
    )
    assert acknowledged.cleanup_pending
    cleanup = active.database.query_one("SELECT * FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'")
    assert cleanup["status"] == "pending"
    assert (
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256=runtime.manifest_sha256,
            receipt_acknowledged=True,
        )
        == acknowledged
    )
    assert (
        active.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'"
        )["count"]
        == 1
    )

    result = active.worker.run_once()
    assert result.job_id == cleanup["id"]
    assert result.status == "succeeded"
    assert active.secret_sink.get(runtime.runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is None
    assert active.secret_sink.get(runtime.runtime_ref, "ABROLIA_BOOTSTRAP_TOKEN") is None
    assert active.secret_sink.get(
        runtime.runtime_ref, "HERMES_RUNTIME_DSAR_TOKEN"
    ) == active.configs.token_hasher.digest(f"runtime-dsar:{runtime.runtime_ref}").encode("ascii")


def test_failed_email_health_receipt_blocks_runtime_activation(api_harness) -> None:
    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    active.bootstrap.claim(runtime.raw_token, **binding)

    with pytest.raises(BootstrapConflict, match="healthy email receipt"):
        active.bootstrap.activate(
            runtime.raw_token,
            **binding,
            activated_sha256=runtime.manifest_sha256,
            email_inbound_check="failed",
            email_outbound_check="healthy",
            email_receipt_digest="a" * 64,
        )

    assert active.households.get(runtime.world.household.id).status == "provisioning"
    identity = active.email_identities.current_for_household(runtime.world.household.id)
    assert identity is not None and identity.status.value == "verified"


def test_tombstone_and_expiry_block_delayed_bootstrap_callbacks(api_harness) -> None:
    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    now = time.time()
    tombstone = active.lookup.digest(runtime.world.household.id)
    with active.database.write() as connection:
        connection.execute(
            "INSERT INTO deletion_tombstones (household_id_hmac, deleted_at, expires_at,"
            " completion_status, created_at) VALUES (?, ?, ?, 'complete', ?)",
            (tombstone, now, now + 1000, now),
        )
    with pytest.raises(BootstrapGone, match="deleted"):
        active.bootstrap.claim(runtime.raw_token, **_binding(runtime))

    with active.database.write() as connection:
        connection.execute("DELETE FROM deletion_tombstones")
        connection.execute(
            "UPDATE bootstrap_tokens SET expires_at = ? WHERE household_id = ?",
            (now - 1, runtime.world.household.id),
        )
    with pytest.raises(BootstrapGone, match="expired"):
        active.bootstrap.claim(runtime.raw_token, **_binding(runtime), now=now)


def test_delete_retains_tombstone_rejects_old_bootstrap_and_allows_fresh_household(
    api_harness,
) -> None:
    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    deleted_household_id = runtime.world.household.id

    result = active.deletion.delete(
        runtime.world.account.id,
        deleted_household_id,
        idempotency_key="delete-before-bootstrap-replay",
    )

    assert result.completion_status == "complete"
    # Complete deletion removes the token row, so the old credential is denied
    # without revealing whether its former household existed. The tombstone is
    # retained independently to stop delayed callbacks that still carry an ID.
    with pytest.raises((BootstrapDenied, BootstrapGone)):
        active.bootstrap.claim(runtime.raw_token, **_binding(runtime))
    tombstone = active.database.query_one(
        "SELECT household_id_hmac, completion_status FROM deletion_tombstones"
    )
    assert tombstone["household_id_hmac"] == active.lookup.digest(
        deleted_household_id
    )
    assert tombstone["completion_status"] == "complete"

    replacement_account = active.accounts.create_verified(
        "bootstrap-owner@family.test"
    )
    replacement = active.households.create_for_owner(replacement_account.id)
    assert replacement.id != deleted_household_id
    assert active.database.query_one(
        "SELECT household_id_hmac FROM deletion_tombstones"
        " WHERE household_id_hmac = ?",
        (active.lookup.digest(deleted_household_id),),
    ) is not None


def test_internal_bootstrap_api_maps_conflicts_replay_and_never_echoes_token(
    api_harness,
) -> None:
    runtime = _provision_runtime(api_harness)
    binding = _binding(runtime)
    bearer = {"Authorization": f"Bearer {runtime.raw_token}"}
    assert api_harness.client.post("/internal/v1/bootstrap/claim", json=binding).status_code == 401

    claimed = api_harness.client.post("/internal/v1/bootstrap/claim", headers=bearer, json=binding)
    resumed = api_harness.client.post("/internal/v1/bootstrap/claim", headers=bearer, json=binding)
    assert claimed.status_code == resumed.status_code == 200
    assert claimed.json() == resumed.json()
    assert runtime.raw_token not in claimed.text

    mismatch = api_harness.client.post(
        "/internal/v1/bootstrap/activate",
        headers=bearer,
        json={**binding, "config_sha256": "0" * 64},
    )
    assert mismatch.status_code == 409
    assert runtime.raw_token not in mismatch.text
    assert api_harness.container.households.get(runtime.world.household.id).status != "active"

    activated = api_harness.client.post(
        "/internal/v1/bootstrap/activate",
        headers=bearer,
        json={**binding, "config_sha256": runtime.manifest_sha256},
    )
    assert activated.status_code == 200
    assert activated.json()["bootstrap_cleanup"] == "awaiting_runtime_receipt"
    replay = api_harness.client.post(
        "/internal/v1/bootstrap/activate",
        headers=bearer,
        json={**binding, "config_sha256": runtime.manifest_sha256},
    )
    assert replay.status_code == 200
    assert replay.json() == activated.json()
    assert runtime.raw_token not in replay.text
    acknowledged = api_harness.client.post(
        "/internal/v1/bootstrap/activate",
        headers={
            **bearer,
            "X-Hermes-Runtime-Receipt-Acknowledged": "true",
        },
        json={**binding, "config_sha256": runtime.manifest_sha256},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["bootstrap_cleanup"] == "pending"


def test_raw_bootstrap_token_is_absent_from_database_bytes(api_harness) -> None:
    runtime = _provision_runtime(api_harness)
    database = api_harness.container.database
    database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = database.path.read_bytes()
    assert runtime.raw_token.encode() not in raw
    row = database.query_one(
        "SELECT token_hash FROM bootstrap_tokens WHERE household_id = ?",
        (runtime.world.household.id,),
    )
    assert row["token_hash"] != runtime.raw_token


def test_fastapi_to_real_runtime_client_resumes_lost_activation_response(
    api_harness,
    tmp_path: Path,
) -> None:
    runtime = _provision_runtime(api_harness)
    calls: list[tuple[str, dict[str, object]]] = []
    lose_activation_response = True

    def transport(
        url: str,
        *,
        data: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        nonlocal lose_activation_response
        assert timeout > 0
        assert runtime.raw_token not in url
        path = urlparse(url).path
        payload = json.loads(data)
        calls.append((path, payload))
        response = api_harness.client.post(
            path,
            content=data,
            headers=dict(headers),
        )
        if path.endswith("/activate") and lose_activation_response:
            lose_activation_response = False
            assert response.status_code == 200
            raise urllib.error.URLError("synthetic response loss")
        return response.status_code, response.content

    client = ControlPlaneBootstrapClient(
        api_harness.config.public_origin,
        transport=transport,
    )
    manifest_path = tmp_path / "household.toml"
    activation_path = tmp_path / "activation.json"
    env = {
        "HERMES_HOUSEHOLD_ID": runtime.world.household.id,
        "HERMES_CONFIG_REVISION": str(runtime.revision),
        "HERMES_CONFIG_SHA256": runtime.manifest_sha256,
    }
    bootstrapper = RuntimeBootstrapper(
        client,
        runtime_ref=runtime.runtime_ref,
        manifest_path=manifest_path,
        activation_path=activation_path,
        env=env,
    )

    with pytest.raises(BootstrapOutcomeUnknown):
        bootstrapper.run(runtime.raw_token)
    assert load_activation_state(activation_path).status == "activating"
    assert api_harness.container.households.get(runtime.world.household.id).status == "active"

    manifest = bootstrapper.run(runtime.raw_token)
    assert load_activation_state(activation_path).status == "active"
    assert manifest.config_sha256 == runtime.manifest_sha256
    assert (
        RuntimeService(
            manifest_path=manifest_path,
            activation_path=activation_path,
            runtime_ref=runtime.runtime_ref,
            env=env,
        )
        .readyz()
        .status_code
        == 200
    )
    assert [path for path, _payload in calls] == [
        "/internal/v1/bootstrap/claim",
        "/internal/v1/bootstrap/activate",
        "/internal/v1/bootstrap/activate",
        "/internal/v1/bootstrap/activate",
    ]
    assert set(calls[0][1]) == {"household_id", "runtime_ref", "config_revision"}
    assert set(calls[1][1]) == {
        "household_id",
        "runtime_ref",
        "config_revision",
        "config_sha256",
        "email_inbound_check",
        "email_outbound_check",
        "email_receipt_digest",
    }
    assert (
        api_harness.container.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'"
        )["count"]
        == 1
    )


def test_crash_after_activate_response_cannot_delete_secret_before_local_receipt(
    api_harness,
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _provision_runtime(api_harness)

    def transport(
        url: str,
        *,
        data: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        assert timeout > 0
        response = api_harness.client.post(
            urlparse(url).path,
            content=data,
            headers=dict(headers),
        )
        return response.status_code, response.content

    client = ControlPlaneBootstrapClient(
        api_harness.config.public_origin,
        transport=transport,
    )
    manifest_path = tmp_path / "household.toml"
    activation_path = tmp_path / "activation.json"
    bootstrapper = RuntimeBootstrapper(
        client,
        runtime_ref=runtime.runtime_ref,
        household_id=runtime.world.household.id,
        config_revision=runtime.revision,
        manifest_path=manifest_path,
        activation_path=activation_path,
        env={"HERMES_CONFIG_SHA256": runtime.manifest_sha256},
    )
    durable_write = runtime_bootstrap_module.write_activation_state
    fail_active_write = True

    def crash_before_active_receipt(path, state) -> None:
        nonlocal fail_active_write
        if state.status == "active" and fail_active_write:
            fail_active_write = False
            raise OSError("synthetic crash before local active receipt")
        durable_write(path, state)

    monkeypatch.setattr(
        runtime_bootstrap_module,
        "write_activation_state",
        crash_before_active_receipt,
    )

    with pytest.raises(BootstrapError, match="cannot persist active revision receipt"):
        bootstrapper.run(runtime.raw_token)
    assert load_activation_state(activation_path).status == "activating"
    assert (
        api_harness.container.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'"
        )["count"]
        == 0
    )
    assert (
        api_harness.container.secret_sink.get(
            runtime.runtime_ref,
            "HERMES_BOOTSTRAP_TOKEN",
        )
        is not None
    )

    recovered = bootstrapper.run(runtime.raw_token)
    assert recovered.config_sha256 == runtime.manifest_sha256
    assert load_activation_state(activation_path).status == "active"
    cleanup = api_harness.container.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE kind = 'bootstrap_cleanup'"
    )
    assert cleanup is not None and cleanup["status"] == "pending"


def test_a_member_rollout_activates_the_revision_without_rewriting_onboarding(
    api_harness,
) -> None:
    """Activation is the other half of the path, and it was rewriting history.

    `_workflow_states_for` keeps a re-provisioning job at `complete` so the
    onboarding page does not claim a settled household is mid-setup. But
    `activate` then re-stamped `completed_at`, bumped the version and appended
    an `activate_runtime` transition unconditionally — moving the date a family
    finished setting up to the day somebody added an adult, and recording a
    `complete -> complete` event that never happened. Guarding the worker's
    transition and not this one protected half the path.
    """
    from control_plane.provisioning.rollout import schedule_runtime_rollout

    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    active.bootstrap.claim(runtime.raw_token, **binding)
    active.bootstrap.activate(
        runtime.raw_token, **binding, activated_sha256=runtime.manifest_sha256
    )

    household_id = runtime.world.household.id
    settled = active.database.query_one(
        "SELECT state, version, completed_at FROM onboarding_workflows"
        " WHERE household_id = ?",
        (household_id,),
    )
    transitions_before = active.database.query_one(
        "SELECT COUNT(*) AS total FROM onboarding_transitions t"
        " JOIN onboarding_workflows w ON w.id = t.workflow_id"
        " WHERE w.household_id = ?",
        (household_id,),
    )["total"]
    assert settled["state"] == "complete" and settled["completed_at"] is not None

    # A member is added: plan revision N and schedule its rollout.
    with active.database.write() as connection:
        planned = active.planner.issue(connection, household_id=household_id)
        schedule_runtime_rollout(
            connection,
            jobs=active.jobs,
            onboarding=active.onboarding_repository,
            household_id=household_id,
            planned=planned,
            runtime_provider=active.config.runtime_provider,
        )
    rollout = active.worker.run_once()
    assert rollout is not None and rollout.status == "succeeded"

    # Activate the NEW revision — without this the guard under test is never
    # reached, and the assertions below would pass on a broken implementation.
    household = active.households.get(household_id)
    raw = active.secret_sink.get(household.runtime_ref, "HERMES_BOOTSTRAP_TOKEN")
    assert raw is not None, "the rollout issued no bootstrap token"
    rollout_binding = {
        "household_id": household_id,
        "runtime_ref": household.runtime_ref,
        "config_revision": planned.revision.revision,
    }
    active.bootstrap.claim(raw.decode("ascii"), **rollout_binding)
    active.bootstrap.activate(
        raw.decode("ascii"),
        **rollout_binding,
        activated_sha256=planned.spec.config_sha256,
    )

    # The revision really did become the active one.
    assert (
        active.households.get(household_id).current_config_revision
        == planned.revision.revision
    )

    after = active.database.query_one(
        "SELECT state, version, completed_at FROM onboarding_workflows"
        " WHERE household_id = ?",
        (household_id,),
    )
    transitions_after = active.database.query_one(
        "SELECT COUNT(*) AS total FROM onboarding_transitions t"
        " JOIN onboarding_workflows w ON w.id = t.workflow_id"
        " WHERE w.household_id = ?",
        (household_id,),
    )["total"]

    # The onboarding record is stamped exactly once, whatever else happens.
    assert after["completed_at"] == settled["completed_at"]
    assert after["state"] == "complete"
    assert transitions_after == transitions_before


def test_a_rollout_that_fails_gives_the_household_back(api_harness) -> None:
    """C3b acceptance criterion 5, which the first implementation did not meet.

    `schedule_runtime_rollout` moves the household to `provisioning` and to
    revision N before any provider work, because the worker's currency guards
    read that pair at every phase. A provider that then rejects left the job
    `failed` and the household permanently `provisioning`, pointing at a
    revision that never activated, with no runnable job to move it.
    """
    from control_plane.provisioning.contracts import ProviderRejected
    from control_plane.provisioning.rollout import schedule_runtime_rollout

    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    active.bootstrap.claim(runtime.raw_token, **binding)
    active.bootstrap.activate(
        runtime.raw_token, **binding, activated_sha256=runtime.manifest_sha256
    )
    household_id = runtime.world.household.id
    settled = active.households.get(household_id)
    assert settled.status == "active"

    with active.database.write() as connection:
        planned = active.planner.issue(connection, household_id=household_id)
        schedule_runtime_rollout(
            connection,
            jobs=active.jobs,
            onboarding=active.onboarding_repository,
            household_id=household_id,
            planned=planned,
            runtime_provider=active.config.runtime_provider,
        )
    # Mid-rollout the household is deliberately not settled.
    assert active.households.get(household_id).status == "provisioning"

    provider = active.providers.get(active.config.runtime_provider)

    def _reject(*args, **kwargs):
        raise ProviderRejected("runtime_rejected", "synthetic refusal")

    provider.prepare = _reject  # type: ignore[method-assign]
    provider.ensure = _reject  # type: ignore[method-assign]

    result = active.worker.run_once()
    assert result is not None and result.status == "failed"

    # Given back: the revision that is actually serving, and a status that lets
    # the owner try again.
    restored = active.households.get(household_id)
    assert restored.status == "active"
    assert restored.current_config_revision == settled.current_config_revision


def test_a_rollout_is_refused_while_the_first_one_is_still_in_flight(
    api_harness,
) -> None:
    """Verifying a binding mid-onboarding used to strand two jobs at once.

    `_workflow_states_for` accepts `complete` and nothing else for a rollout,
    so scheduling one before the initial `ensure_runtime` activates enqueued
    revision N+1 and overwrote the household's single
    `current_config_revision`: the original job no longer matched the revision,
    the new one no longer matched the workflow, and both cancelled — after
    prepare, taking the shared runtime with them.
    """
    from control_plane.provisioning.rollout import (
        RolloutNotReady,
        schedule_runtime_rollout,
    )

    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    household_id = runtime.world.household.id
    # Deliberately NOT activated: the first rollout is still in flight.
    assert active.households.get(household_id).status == "provisioning"

    with active.database.write() as connection:
        planned = active.planner.issue(connection, household_id=household_id)
        with pytest.raises(RolloutNotReady, match="settled state"):
            schedule_runtime_rollout(
                connection,
                jobs=active.jobs,
                onboarding=active.onboarding_repository,
                household_id=household_id,
                planned=planned,
                runtime_provider=active.config.runtime_provider,
            )

    # The in-flight rollout is untouched: one runtime job, still pointing at
    # the revision it was planned for.
    jobs = active.database.query(
        "SELECT desired_revision FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'",
        (household_id,),
    )
    assert [row["desired_revision"] for row in jobs] == [runtime.revision]
    assert (
        active.households.get(household_id).current_config_revision == runtime.revision
    )


def test_a_rollout_that_touched_the_provider_is_not_declared_active(
    api_harness,
) -> None:
    """The limit that keeps the recovery honest.

    `_restore_settled_household` writes only to the database. After a failure
    that reached the provider, the Machine may already carry revision N's
    config, so recording N-1 as active would say something false about what is
    running. A household stuck in `provisioning` is visible and fixable; one
    that is falsely `active` is neither, because nothing goes looking.
    """
    from control_plane.provisioning.rollout import schedule_runtime_rollout

    runtime = _provision_runtime(api_harness)
    active = api_harness.container
    binding = _binding(runtime)
    active.bootstrap.claim(runtime.raw_token, **binding)
    active.bootstrap.activate(
        runtime.raw_token, **binding, activated_sha256=runtime.manifest_sha256
    )
    household_id = runtime.world.household.id

    with active.database.write() as connection:
        planned = active.planner.issue(connection, household_id=household_id)
        schedule_runtime_rollout(
            connection,
            jobs=active.jobs,
            onboarding=active.onboarding_repository,
            household_id=household_id,
            planned=planned,
            runtime_provider=active.config.runtime_provider,
        )
        # The provider was reached for THIS revision: `_finish_runtime` records
        # exactly this row as soon as `prepare` returns.
        connection.execute(
            "UPDATE external_resources SET config_revision = ?"
            " WHERE household_id = ? AND resource_type = 'runtime'",
            (planned.revision.revision, household_id),
        )

    job = active.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ? AND desired_revision = ?",
        (household_id, planned.revision.revision),
    )
    with active.database.write() as connection:
        active.worker._restore_settled_household(
            connection,
            active.jobs.get(job["id"]),
            now=1_800_000_900.0,
        )

    # Declined to guess: still provisioning, and honestly so.
    assert active.households.get(household_id).status == "provisioning"
