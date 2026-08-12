from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
from control_plane.repositories.accounts import AccountsRepository
from control_plane.repositories.configs import ConfigRepository, ConfigRevisionRecord
from control_plane.repositories.households import HouseholdsRepository
from control_plane.repositories.onboarding import OnboardingRepository


@dataclass(frozen=True)
class PlannedRevision:
    spec: DesiredHouseholdSpecV1
    revision: ConfigRevisionRecord


class DesiredSpecPlanner:
    def __init__(
        self,
        accounts: AccountsRepository,
        households: HouseholdsRepository,
        onboarding: OnboardingRepository,
        configs: ConfigRepository,
    ) -> None:
        self.accounts = accounts
        self.households = households
        self.onboarding = onboarding
        self.configs = configs

    def issue(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
    ) -> PlannedRevision:
        household = self.households.get(household_id)
        profile = self.households.profile(household_id)
        if household is None or profile is None:
            raise ValueError("household profile is incomplete")
        workflow = self.onboarding.workflow_for_household(household_id)
        rows = connection.execute(
            "SELECT kind, status FROM onboarding_steps WHERE workflow_id = ?",
            (workflow.id,),
        ).fetchall()
        statuses = {row["kind"]: row["status"] for row in rows}
        for kind in ("email_identity", "whatsapp_identity", "primary_channel"):
            if statuses.get(kind) != "verified":
                raise ValueError("desired spec can only be built from verified results")
        email_result = self.onboarding.result(workflow.id, "email_identity")
        whatsapp_result = self.onboarding.result(workflow.id, "whatsapp_identity")
        channel_result = self.onboarding.result(workflow.id, "primary_channel")
        results = (email_result, whatsapp_result, channel_result)
        if not all(result and result.get("verified") for result in results):
            raise ValueError("provider results are missing durable verification")
        membership = connection.execute(
            "SELECT account_id FROM household_memberships WHERE household_id = ?"
            " AND role = 'owner' AND status = 'active' LIMIT 1",
            (household_id,),
        ).fetchone()
        if membership is None:
            raise ValueError("household has no active account owner")
        account = self.accounts.get(membership["account_id"])
        if account is None:
            raise ValueError("account owner is unavailable")
        email_public = email_result["public_result"]
        channel_public = channel_result["public_result"]
        whatsapp_selection = self.onboarding.selection(workflow.id, "whatsapp_identity")
        required_purposes = [
            "special_category_content_restriction",
            "whatsapp_channel_privacy",
        ]
        if whatsapp_selection and whatsapp_selection.get("kind") == "dedicated_number":
            required_purposes.append("whatsapp_linked_device_risk")
        receipt_rows = connection.execute(
            "SELECT id, purpose, text_version, text_sha256 FROM consent_receipts"
            " WHERE household_id = ? ORDER BY accepted_at, id",
            (household_id,),
        ).fetchall()
        receipts_by_purpose = {row["purpose"]: row for row in receipt_rows}
        # A household that accepted the Art 9(2)(a) condition carries it into the
        # manifest, so the runtime enforces its exact version like every other
        # authoritative purpose. Synthetic households never hold that receipt.
        if "special_category_household_content" in receipts_by_purpose:
            required_purposes.append("special_category_household_content")
        if any(purpose not in receipts_by_purpose for purpose in required_purposes):
            raise ValueError("authoritative onboarding consent receipt is missing")
        current = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM config_revisions"
            " WHERE household_id = ?",
            (household_id,),
        ).fetchone()["revision"]
        next_revision = int(current) + 1
        primary = channel_public["channel"]
        actor_id = str(channel_public["actor_id"])
        chat_id = str(channel_public["chat_id"])
        spec = DesiredHouseholdSpecV1(
            household_id=household_id,
            config_revision=next_revision,
            family_language=profile["family_language"],
            timezone=profile["timezone"],
            country_code=profile["country_code"],
            residency_mode=profile["residency_mode"],
            actors=ActorsV1(owner=actor_id, family=(actor_id,)),
            channels=ChannelsV1(primary=primary),
            channel_bindings=(
                ChannelBindingV1(
                    channel=primary,
                    actor_id=actor_id,
                    chat_id=chat_id,
                    external_ref=channel_result["external_ref"],
                ),
            ),
            email=EmailV1(
                agent_inbox=email_public["agent_inbox"],
                fallback=account.recovery_email,
                provider_kind=str(
                    email_public.get("provider", email_public.get("mode", "synthetic"))
                ),
                provider_binding_ref=email_result["external_ref"],
                secret_binding_ref=(
                    str(email_public["secret_binding_ref"])
                    if email_public.get("secret_binding_ref")
                    else None
                ),
            ),
            consent=ConsentAuthorityV1(
                required_purposes=tuple(required_purposes),
                receipts=tuple(
                    ConsentReceiptV1(
                        receipt_id=receipts_by_purpose[purpose]["id"],
                        purpose=purpose,
                        text_version=receipts_by_purpose[purpose]["text_version"],
                        text_sha256=receipts_by_purpose[purpose]["text_sha256"],
                    )
                    for purpose in required_purposes
                ),
            ),
            provider_refs={
                "email": email_result["external_ref"],
                "whatsapp": whatsapp_result["external_ref"],
                "primary_channel": channel_result["external_ref"],
                "consent_authority": "control_plane",
            },
        ).with_hash()
        revision = self.configs.create_revision(
            connection,
            household_id=household_id,
            schema_version=1,
            manifest=spec.model_dump(mode="json"),
            manifest_sha256=manifest_sha256(spec),
        )
        return PlannedRevision(spec, revision)
