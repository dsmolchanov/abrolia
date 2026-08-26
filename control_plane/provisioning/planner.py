from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

from control_plane.privacy.consent import (
    consent_version_and_sha,
    required_consent_purposes,
)
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
from control_plane.repositories.bindings import ChannelBindingsRepository
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
        bindings: ChannelBindingsRepository,
    ) -> None:
        self.accounts = accounts
        self.households = households
        self.onboarding = onboarding
        self.configs = configs
        self.bindings = bindings

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
        # What the household OWES is derived from the provider it is being
        # provisioned onto, never from what it happens to hold. Keying the
        # Art 9(2)(a) requirement on "a receipt row exists" made the check
        # self-satisfying: a real-email household created before that purpose
        # existed, or one that later revoked it, simply stopped owing it.
        provider_kind = str(
            email_public.get("provider", email_public.get("mode", "synthetic"))
        )
        required_purposes = required_consent_purposes(
            provider_kind=provider_kind,
            whatsapp_dedicated_number=bool(
                whatsapp_selection
                and whatsapp_selection.get("kind") == "dedicated_number"
            ),
        )
        receipt_rows = connection.execute(
            "SELECT id, purpose, text_version, text_sha256 FROM consent_receipts"
            " WHERE household_id = ? AND revoked_at IS NULL"
            " ORDER BY accepted_at, id",
            (household_id,),
        ).fetchall()
        receipts_by_purpose = {row["purpose"]: row for row in receipt_rows}
        # Presence is not currency. The receipt carried into the manifest must be
        # the one for the copy in force now, or the runtime would enforce a
        # version the family never saw.
        for purpose in required_purposes:
            expected_version, expected_sha = consent_version_and_sha(purpose)
            row = receipts_by_purpose.get(purpose)
            if (
                row is None
                or row["text_version"] != expected_version
                or not hmac.compare_digest(str(row["text_sha256"]), expected_sha)
            ):
                raise ValueError(
                    "authoritative onboarding consent receipt is missing,"
                    f" revoked, or superseded for {purpose}"
                )
        current = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM config_revisions"
            " WHERE household_id = ?",
            (household_id,),
        ).fetchone()["revision"]
        next_revision = int(current) + 1
        primary = channel_public["channel"]
        actor_id = str(channel_public["actor_id"])
        chat_id = str(channel_public["chat_id"])
        # The manifest is a PROJECTION of `channel_bindings`, not a second
        # record of the same fact. Before C3 this method built the owner's
        # binding inline and the table stayed empty, so the gateway — whose
        # only source is that table — could not route to a household the
        # manifest considered fully bound. Seeding the owner's row here from
        # the onboarding step that already proved the channel makes the two
        # agree by construction: everything below reads the table.
        self.bindings.ensure_owner_binding(
            connection,
            household_id=household_id,
            channel=primary,
            external_id=chat_id,
            actor_id=actor_id,
        )
        bound = self.bindings.verified(connection, household_id=household_id)
        # Order carries meaning: the owner leads `family`, then adults in the
        # order they were verified. `verified()` is ordered for the same reason
        # — an unstable projection would move `config_sha256` between two runs
        # that bound nothing new, and a changed hash is how this system says
        # "something changed".
        family = (actor_id,) + tuple(
            binding.actor_id
            for binding in bound
            if binding.actor_id != actor_id
        )
        spec = DesiredHouseholdSpecV1(
            household_id=household_id,
            config_revision=next_revision,
            family_language=profile["family_language"],
            timezone=profile["timezone"],
            country_code=profile["country_code"],
            residency_mode=profile["residency_mode"],
            actors=ActorsV1(owner=actor_id, family=family),
            channels=ChannelsV1(primary=primary),
            channel_bindings=tuple(
                ChannelBindingV1(
                    channel=binding.channel,
                    actor_id=binding.actor_id,
                    chat_id=binding.external_id,
                    # Only the owner's binding came from a provider result, so
                    # only it carries that provider's reference. An adult
                    # verified by challenge has no provider row to point at,
                    # and inventing one would put a reference in the manifest
                    # that resolves to nothing.
                    external_ref=(
                        channel_result["external_ref"]
                        if binding.role == "owner" and binding.channel == primary
                        else None
                    ),
                )
                for binding in bound
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
