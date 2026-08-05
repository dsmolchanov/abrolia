from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from control_plane.crypto import SecretFieldError
from control_plane.models import (
    FamilyDomainSelection,
    PrimaryChannelSelection,
    ProviderPublicResult,
    StepKind,
)
from control_plane.provisioning.contracts import ProvisionResult
from control_plane.provisioning.manifest import (
    ActorsV1,
    ChannelBindingV1,
    ChannelsV1,
    ConsentAuthorityV1,
    ConsentReceiptV1,
    DesiredHouseholdSpecV1,
    EmailV1,
    manifest_sha256,
)
from control_plane.provisioning.manifest_toml import manifest_to_toml
from control_plane.provisioning.planner import DesiredSpecPlanner
from hermes_cloud.core.runtime_manifest import (
    compute_config_sha256,
    parse_runtime_manifest,
)

BASE_TIME = 1_800_000_000.0
EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "family-agent"}
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


def _manifest(**changes) -> DesiredHouseholdSpecV1:
    values = {
        "household_id": "00000000-0000-4000-8000-000000000001",
        "config_revision": 1,
        "family_language": "en",
        "timezone": "Europe/Prague",
        "country_code": "CZ",
        "residency_mode": "eu-app",
        "actors": ActorsV1(owner="synthetic-owner", family=("synthetic-owner",)),
        "channels": ChannelsV1(primary="telegram"),
        "channel_bindings": (
            ChannelBindingV1(
                channel="telegram",
                actor_id="synthetic-owner",
                chat_id="synthetic-chat",
                external_ref="synthetic:channel:one",
            ),
        ),
        "email": EmailV1(
            agent_inbox="agent@assistant.test",
            fallback="owner@family.test",
        ),
        "consent": ConsentAuthorityV1(
            required_purposes=("whatsapp_channel_privacy",),
            receipts=(
                ConsentReceiptV1(
                    receipt_id="synthetic-consent-receipt",
                    purpose="whatsapp_channel_privacy",
                    text_version="2026-08-04.1",
                    text_sha256="a" * 64,
                ),
            ),
        ),
        "provider_refs": {
            "primary_channel": "synthetic:channel:one",
            "email": "synthetic:email:one",
        },
    }
    values.update(changes)
    return DesiredHouseholdSpecV1.model_validate(values)


def test_manifest_canonical_bytes_and_hash_are_deterministic() -> None:
    first = _manifest().with_hash()
    second = _manifest(
        provider_refs={
            "email": "synthetic:email:one",
            "primary_channel": "synthetic:channel:one",
        }
    ).with_hash()
    assert first == second
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.config_sha256 == manifest_sha256(first)

    changed = first.model_dump(mode="json")
    changed["timezone"] = "Europe/Berlin"
    with pytest.raises(ValueError, match="hash does not match"):
        manifest_sha256(changed)


def test_manifest_toml_is_deterministic_parseable_and_hash_equivalent() -> None:
    first = _manifest(family_language='Русский "семья"').with_hash()
    reordered = first.model_dump(mode="json")
    reordered["provider_refs"] = {
        "email": reordered["provider_refs"]["email"],
        "primary_channel": reordered["provider_refs"]["primary_channel"],
    }

    first_toml = manifest_to_toml(first)
    second_toml = manifest_to_toml(reordered)
    parsed = parse_runtime_manifest(first_toml, env={})

    assert first_toml == second_toml
    assert first_toml.encode("utf-8") == second_toml.encode("utf-8")
    assert compute_config_sha256(first_toml) == first.config_sha256
    assert parsed.config_sha256 == first.config_sha256
    assert parsed.family_language == 'Русский "семья"'
    assert parsed.provider_refs == first.provider_refs
    assert parsed.consent is not None
    assert parsed.consent.authority == "control_plane"
    assert parsed.consent.receipts[0].receipt_id == "synthetic-consent-receipt"


def test_manifest_rejects_unverified_or_mismatched_runtime_identity() -> None:
    with pytest.raises(ValidationError, match="verified binding"):
        _manifest(channel_bindings=())
    with pytest.raises(ValidationError, match="runtime owner"):
        _manifest(actors=ActorsV1(owner="account-principal"))
    with pytest.raises(ValidationError, match="cannot be the recovery-email"):
        _manifest(
            email=EmailV1(
                agent_inbox="Owner@FAMILY.TEST.",
                fallback="Owner@family.test",
            )
        )
    with pytest.raises(ValidationError):
        DesiredHouseholdSpecV1.model_validate(
            {**_manifest().model_dump(mode="json"), "schema_version": 2}
        )


@pytest.mark.parametrize(
    ("secret_field", "secret_value"),
    (
        ("client_secret", "GOCSPX-" + "oauth-client-secret-canary"),
        ("refresh_token", "1" + "//refresh-token-canary-0123456789"),
        ("nerve_bootstrap_key", "nrv_" + "live_bootstrap-canary-0123456789"),
        ("nerve_runtime_key", "nrv_" + "live_runtime-canary-0123456789"),
        ("webhook_secret", "webhook-secret-canary"),
    ),
)
def test_manifest_and_provider_public_result_reject_secret_shaped_fields(
    secret_field: str, secret_value: str
) -> None:
    with pytest.raises(SecretFieldError):
        ProvisionResult(
            external_ref="synthetic:email:one",
            public_result={secret_field: secret_value},
        )
    with pytest.raises(ValidationError, match="secret-like field"):
        _manifest(provider_refs={secret_field: secret_value})


def test_phase_one_input_contracts_reject_real_provider_facing_identities() -> None:
    with pytest.raises(ValidationError, match="reserved .test"):
        FamilyDomainSelection(domain="family.example.com")
    assert FamilyDomainSelection(domain="family.example.test").domain.endswith(".test")

    for field, value in (("actor_id", "real-owner"), ("chat_id", "external-chat")):
        payload = {
            "kind": "telegram",
            "actor_id": "synthetic-owner",
            "chat_id": "synthetic-family-chat",
            field: value,
        }
        with pytest.raises(ValidationError, match="synthetic- namespace"):
            PrimaryChannelSelection.model_validate(payload)

    with pytest.raises(ValidationError, match="synthetic namespace"):
        ProviderPublicResult(external_ref="provider-account-42", public_result={})
    assert ProviderPublicResult(
        external_ref="synthetic:channel:contract",
        public_result={"mode": "synthetic"},
    ).verified


def test_manifest_models_are_frozen() -> None:
    spec = _manifest().with_hash()
    with pytest.raises(ValidationError, match="frozen"):
        spec.timezone = "Europe/Berlin"


def test_planner_requires_all_three_verified_provider_results(cp_stack) -> None:
    cp_stack.complete_profile()
    planner = DesiredSpecPlanner(
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.onboarding,
        cp_stack.configs,
    )
    with pytest.raises(ValueError, match="verified results"), cp_stack.database.write() as connection:
        planner.issue(connection, household_id=cp_stack.household.id)
    assert cp_stack.database.query("SELECT id FROM config_revisions") == []


def test_verified_inputs_create_encrypted_account_free_manifest(cp_stack) -> None:
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

    stored = cp_stack.configs.manifest(cp_stack.household.id, 1)
    spec = DesiredHouseholdSpecV1.model_validate(stored)
    row = cp_stack.database.query_one(
        "SELECT manifest_ciphertext, manifest_sha256, status FROM config_revisions"
        " WHERE household_id = ? AND revision = 1",
        (cp_stack.household.id,),
    )
    assert row["status"] == "planned"
    assert row["manifest_sha256"] == manifest_sha256(spec)
    assert cp_stack.account.id.encode() not in spec.canonical_bytes()
    assert cp_stack.account.recovery_email.encode() in spec.canonical_bytes()
    assert cp_stack.account.recovery_email.encode() not in bytes(row["manifest_ciphertext"])
    assert spec.actors.owner == CHANNEL_SELECTION["actor_id"]
    assert spec.consent.authority == "control_plane"
    assert spec.consent.required_purposes == ("whatsapp_channel_privacy",)


def test_manifest_payload_of_revision_cannot_be_mutated(cp_stack) -> None:
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
        worker.run_once()

    with pytest.raises(sqlite3.IntegrityError), cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE config_revisions SET manifest_sha256 = ?"
            " WHERE household_id = ? AND revision = 1",
            ("0" * 64, cp_stack.household.id),
        )
