"""Checking a pending managed inbox must stay pending, not go outcome_unknown.

Production, 2026-09-04 09:34 UTC. A tester selected `@abrolia.com`; the
managed provider created the Nerve org, inbox, key and webhook, found the
`attachments` flag off and settled `waiting_user` with
`nerve_attachment_flag_pending` — correct. The tester pressed "check again",
and ten seconds later the same household's job settled `outcome_unknown`,
which no tester and no page can clear.

The cause is a reference that moves. `inspect` routes to `_recover_and_probe`,
which deletes and reissues the API key and rotates the webhook secret BEFORE
probing the flag, then answered PENDING with no reference at all. The worker
read the reference from the inspect job's own row — empty, because the job was
created seconds earlier by `check` — and `_validated_email_waiting_result`
refuses a managed Nerve wait without one. That `ValueError` becomes
`OutcomeUnknown`, and a routine "flag still pending" turns into a quarantined
job needing an operator.
"""

from __future__ import annotations

import json

import pytest

from control_plane.crypto import normalize_email
from control_plane.email.models import EmailNerveAttachmentPublicStatus
from control_plane.models import StepKind
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.providers.email.nerve_client import email_org_external_ref
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRegistry,
    ProviderWaiting,
)
from tests.control_plane.test_real_email_wiring import _hold_both_consents

NOW = 1_760_000_000.0
ORG = "47000000-0000-4000-8000-000000000001"


def _refs(household_id: str, identity_id: str, stable_ref: str, *, key_id: str) -> str:
    return json.dumps(
        {
            "household_id": household_id,
            "stable_ref": stable_ref,
            "org_id": ORG,
            "grant_id": "47000000-0000-4000-8000-000000000002",
            "inbox_id": "47000000-0000-4000-8000-000000000003",
            "key_id": key_id,
            "webhook_id": "47000000-0000-4000-8000-000000000005",
            "address": normalize_email("family-agent@abrolia.com"),
            "org_external_ref": email_org_external_ref(household_id, identity_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _pending_status() -> dict:
    return EmailNerveAttachmentPublicStatus.model_validate(
        {
            "nerve_org_id": ORG,
            "operator_action": {
                "arguments": ["set", "attachments", "--org", ORG, "--enabled=true"],
            },
        }
    ).model_dump(mode="json", exclude_none=True)


class _FlagStillPending:
    """The managed adapter's real shape: inspect rotates the key, then probes."""

    email_public_provider = "nerve"
    #: The key id the ensure leg handed out, and the one inspect replaces.
    FIRST_KEY = "47000000-0000-4000-8000-000000000004"
    ROTATED_KEY = "47000000-0000-4000-8000-00000000000a"

    def __init__(self, household_id: str, identity_id: str) -> None:
        self.household_id = household_id
        self.identity_id = identity_id
        self.inspections = 0

    def ensure(self, intent, idempotency_key):  # noqa: ANN001, ANN201
        del intent
        raise ProviderWaiting(
            "Nerve attachments must be activated for this household",
            public_result=_pending_status(),
            external_ref=_refs(
                self.household_id,
                self.identity_id,
                idempotency_key,
                key_id=self.FIRST_KEY,
            ),
        )

    def inspect(self, stable_ref):  # noqa: ANN001, ANN201
        self.inspections += 1
        return InspectResult(
            InspectState.PENDING,
            error_code="nerve_attachment_flag_pending",
            public_result=_pending_status(),
            external_ref=_refs(
                self.household_id,
                self.identity_id,
                stable_ref,
                key_id=self.ROTATED_KEY,
            ),
        )

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        del external_ref
        return InspectResult(InspectState.ABSENT)


def _select_managed(cp_stack, monkeypatch) -> tuple[_FlagStillPending, str]:
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=NOW)
    cp_stack.service.real_email_enabled = True
    cp_stack.service.real_email_all_households = True
    cp_stack.service.email_provider = "nerve-managed"

    version, sha256 = consent_version_and_sha("special_category_content_restriction")
    household_version, household_sha = consent_version_and_sha(
        "special_category_household_content"
    )
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "abrolia_managed",
            "local_part": "family-agent",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "47000000-0000-4000-8000-000000000011",
            "special_category_restriction_text_version": version,
            "special_category_restriction_text_sha256": sha256,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": "47000000-0000-4000-8000-000000000012",
            "special_category_household_text_version": household_version,
            "special_category_household_text_sha256": household_sha,
        },
        context=cp_stack.context(),
        now=NOW,
    )
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    provider = _FlagStillPending(cp_stack.household.id, identity.id)
    registry = ProviderRegistry()
    registry.register("nerve-managed", provider)
    return provider, identity.id


def test_a_check_on_a_pending_flag_stays_waiting_user(cp_stack, monkeypatch) -> None:
    provider, _identity_id = _select_managed(cp_stack, monkeypatch)
    registry = ProviderRegistry()
    registry.register("nerve-managed", provider)

    ensured = cp_stack.make_worker(providers=registry, now=NOW + 1).run_once()
    assert ensured is not None
    assert (ensured.status, ensured.error_code) == ("waiting_user", "waiting_user")

    # The tester presses "check again": the flag is still off.
    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context(), now=NOW + 10
    )
    checked = cp_stack.make_worker(providers=registry, now=NOW + 11).run_once()

    assert provider.inspections == 1
    assert checked is not None
    assert (checked.status, checked.error_code) == ("waiting_user", "waiting_user"), (
        "a flag that is still pending is not an unknown outcome"
    )

    # And the durable resource carries the ROTATED key — the one inspect
    # reissued, not the key the ensure leg handed out and inspect has deleted.
    # Getting this wrong strands a live Nerve key nothing can tear down.
    row = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version FROM external_resources"
        " WHERE household_id = ? AND provider = 'nerve-managed'"
        " ORDER BY updated_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert row is not None
    stored = cp_stack.jobs.decrypt_json(
        "external_resources",
        row["id"],
        "external_id",
        row["external_id_ciphertext"],
        row["encryption_key_version"],
    )
    assert json.loads(stored)["key_id"] == _FlagStillPending.ROTATED_KEY


def test_the_step_is_still_checkable_after_the_check(cp_stack, monkeypatch) -> None:
    """The page must be able to ask again; a quarantined job cannot be."""
    provider, _identity_id = _select_managed(cp_stack, monkeypatch)
    registry = ProviderRegistry()
    registry.register("nerve-managed", provider)
    cp_stack.make_worker(providers=registry, now=NOW + 1).run_once()

    for tick in (10, 20):
        cp_stack.service.check(
            cp_stack.household.id,
            StepKind.EMAIL,
            context=cp_stack.context(),
            now=NOW + tick,
        )
        result = cp_stack.make_worker(providers=registry, now=NOW + tick + 1).run_once()
        assert result is not None and result.status == "waiting_user"
    assert provider.inspections == 2


def test_an_inspection_without_a_reference_is_still_refused(cp_stack, monkeypatch) -> None:
    """The validator is not weakened: a managed wait with NO reference anywhere
    is still an unknown outcome, because nothing names what was created."""
    provider, _identity_id = _select_managed(cp_stack, monkeypatch)

    class DropsTheReference(type(provider)):  # type: ignore[misc]
        def inspect(self, stable_ref):  # noqa: ANN001, ANN201
            del stable_ref
            self.inspections += 1  # noqa: A003
            return InspectResult(
                InspectState.PENDING,
                error_code="nerve_attachment_flag_pending",
                public_result=_pending_status(),
            )

    forgetful = DropsTheReference(provider.household_id, provider.identity_id)
    registry = ProviderRegistry()
    registry.register("nerve-managed", forgetful)
    cp_stack.make_worker(providers=registry, now=NOW + 1).run_once()
    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context(), now=NOW + 10
    )
    result = cp_stack.make_worker(providers=registry, now=NOW + 11).run_once()

    assert result is not None
    assert result.status == "outcome_unknown"


def test_reconcile_runs_beside_the_serving_process(tmp_path) -> None:
    """`serve` holds the writer flock for the life of the process.

    An `outcome_unknown` that can only be settled with production STOPPED is a
    tester's onboarding waiting for a quiet hour. The embedded worker never
    leases a job in that state, so nothing races this one row.
    """
    from control_plane.cli import main
    from control_plane.config import ControlPlaneConfig
    from control_plane.container import ControlPlaneContainer

    config = ControlPlaneConfig.for_test(tmp_path)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    try:
        # No such job: the point is that it gets far enough to look rather
        # than dying on the lock, exactly like `withdraw-consent`.
        with (
            ControlPlaneContainer.build(config, acquire_process_lock=True),
            pytest.raises(ValueError, match="outcome_unknown"),
        ):
            main(["reconcile", "47000000-0000-4000-8000-0000000000ff"])
    finally:
        monkey.undo()
