from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

from control_plane.channel_preferences import ChannelPreferencesRepository
from control_plane.owners import fallback_owner_query
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
from control_plane.repositories.bindings import ChannelBindingsRepository, web_seat_chat_id
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
        preferences: ChannelPreferencesRepository,
    ) -> None:
        self.accounts = accounts
        self.households = households
        self.onboarding = onboarding
        self.configs = configs
        self.bindings = bindings
        self.preferences = preferences

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
        # `control_plane/owners.py` answers this, and nothing here does: the
        # account chosen becomes the household's email fallback, and choosing
        # it by membership alone let a LOCKED account be picked and then
        # refused — from inside a provisioning job, after the provider had
        # already succeeded.
        owner_sql, owner_params = fallback_owner_query(household_id)
        membership = connection.execute(owner_sql, owner_params).fetchone()
        if membership is None:
            raise ValueError("household has no active account owner")
        account = self.accounts.get(membership["id"])
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
        # THREE names, and each comes from the field that means it.
        #
        # `external_id` is the SENDER — what the gateway matches an inbound
        # message against — and it is seeded from `actor_id`, NOT from
        # `chat_id`. This was the other half of the same defect: the sender
        # column held a conversation, so a strict-mode gateway lookup matched a
        # chat where it meant to match a sender.
        #
        # M1 of the C3a plan called that gap uncorrectable, on the grounds that
        # it "needs the owner's Telegram user ID, which onboarding never
        # captured". That premise is false and the plan is corrected: onboarding
        # DOES capture it. `PrimaryChannelSelection.actor_id` becomes
        # `actors.owner`, and `actors.owner` is compared against
        # `message.from.id` (`hermes_cloud/channels/telegram.py:264`) and
        # against the `+999…` actor WhatsApp ingest reports. It IS the transport
        # sender identity; the planner was reading the wrong field.
        #
        # The manifest projection is unaffected — `external_id` is not projected
        # at all, `chat_id` is — so this changes what the GATEWAY matches and
        # nothing the runtime parses.
        self.bindings.ensure_owner_binding(
            connection,
            household_id=household_id,
            channel=primary,
            external_id=actor_id,
            chat_id=chat_id,
            actor_id=actor_id,
            # The primary channel identifies the owner by its own sender, so it
            # carries no account — except when the primary IS web, which has no
            # sender to identify anyone by.
            account_id=account.id if primary == "web" else None,
        )
        # C3f: the owner's WEB SEAT, seeded here for the same reason the
        # primary binding and the preference row are — a household that has
        # proved a channel should not depend on a later endpoint to record
        # where it is reached.
        #
        # Before this, web chat did not consult bindings at all: the runtime
        # hardcoded `manifest.actors.owner` and refused every other role, so
        # requiring a seat without seeding one would have taken web away from
        # every household that already has it.
        #
        # The seat is bound under the owner's OWN actor, not a new identity.
        # `_insert` requires `actor_id == external_id`, and an actor is a
        # per-channel sender here — so minting a separate web actor would put
        # the owner in `actors.family` twice AND stop `role_for` matching
        # `actors.owner`, quietly demoting the owner to `ROLE_FAMILY` on web
        # and taking export and deletion with it. Reusing the actor keeps one
        # person one member; `account_id` carries the mapping instead.
        #
        # AFTER the primary, and through its own reconciler: a genuine owner
        # reset retires every owner row, and this call then re-establishes the
        # seat from the same authoritative account.
        if primary != "web":
            self.bindings.ensure_owner_web_seat(
                connection,
                household_id=household_id,
                actor_id=actor_id,
                # The seat's OWN conversation, not the primary channel's. They
                # were briefly the same value and that is a real bug: the
                # runtime builds `RunContext.chat_id` from the pair it matched,
                # so a web turn would have been attributed to the family's
                # Telegram chat — the wrong room for scope, replies and every
                # later `knows_binding` comparison.
                chat_id=web_seat_chat_id(actor_id),
                account_id=account.id,
            )
        # The preference row is seeded from the SAME result, for the reason the
        # owner binding is: a household that has proved a channel should not
        # depend on some later endpoint to record where it is reached. C4's
        # audit found this table with a schema, constraints, and no writer at
        # all, which reads as working code until something looks for a row.
        #
        # `primary_channel` is written here rather than made settable, because
        # `channels.primary` below is built from this same `channel_public`.
        # Two records of one fact disagree the moment either moves, and the way
        # to change the channel is to re-run the step that proves it — which
        # already retires the bindings it supersedes.
        #
        # The fallback is the owner's account, not their address: what makes an
        # address usable is `accounts.email_verified_at`, and that is a fact
        # about the account. `set_household` refuses one that is not an active
        # owner here, or whose verified contact IS this household's agent
        # inbox — the self-ingestion loop 0006 named and nothing enforced.
        self.preferences.set_household(
            connection,
            household_id=household_id,
            primary_channel=primary,
            fallback_account_id=account.id,
            verified_at=account.email_verified_at,
        )
        bound = self.bindings.verified(connection, household_id=household_id)
        # Order carries meaning: the owner leads `family`, then adults in the
        # order they were verified. `verified()` is ordered for the same reason
        # — an unstable projection would move `config_sha256` between two runs
        # that bound nothing new, and a changed hash is how this system says
        # "something changed".
        # One entry per ACTOR, not per binding. `verify_challenge` lets one
        # actor hold several bindings — an adult reachable on WhatsApp and on
        # web is two rows and one person — and appending per row produced
        # `family=(owner, adult, adult)`, which `parse_runtime_manifest`
        # rejects as `actors.family: duplicate actor`. A revision that cannot
        # start. First-verification order is preserved because `verified()` is
        # ordered and `dict.fromkeys` keeps first appearance.
        family = tuple(
            dict.fromkeys(
                (actor_id, *(binding.actor_id for binding in bound))
            )
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
                    chat_id=binding.chat_id,
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
