from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from pydantic import TypeAdapter

from control_plane.crypto import canonical_json
from control_plane.email.models import SYNTHETIC_EMAIL_SECRET_BINDING
from control_plane.email.service import EmailIdentityService
from control_plane.models import (
    EmailSelection,
    OnboardingSnapshot,
    PrimaryChannelSelection,
    ProfileInput,
    StepKind,
    StepStatus,
    WhatsAppSelection,
)
from control_plane.onboarding.contracts import (
    CommandContext,
    CommandResult,
    IdempotencyConflict,
    InvalidTransition,
    WorkflowConflict,
)
from control_plane.onboarding.state import CHECK, RETRY, SAVE_PROFILE, SELECT, next_status
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.repositories.households import HouseholdsRepository
from control_plane.repositories.jobs import JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository, WorkflowRecord

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class OnboardingService:
    def __init__(
        self,
        households: HouseholdsRepository,
        onboarding: OnboardingRepository,
        jobs: JobsRepository,
        *,
        runtime_provider: str = "dry-run-runtime",
        email_provider: str = "fake-email",
        gmail_provider: str | None = None,
        byo_domain_provider: str | None = None,
        allow_real_email_domains: bool = False,
        real_email_enabled: bool = False,
        real_email_household_allowlist: frozenset[str] = frozenset(),
        email_identities: EmailIdentityService | None = None,
    ) -> None:
        self.households = households
        self.onboarding = onboarding
        self.jobs = jobs
        self.runtime_provider = runtime_provider
        self.email_provider = email_provider
        self.gmail_provider = gmail_provider or email_provider
        self.byo_domain_provider = byo_domain_provider or email_provider
        self.allow_real_email_domains = allow_real_email_domains
        self.real_email_enabled = real_email_enabled
        self.real_email_household_allowlist = real_email_household_allowlist
        self.email_identities = email_identities

    @staticmethod
    def _request_sha(body: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def _replay(
        self,
        connection,
        *,
        context: CommandContext,
        route: str,
        request_sha: str,
        now: float,
    ) -> CommandResult | None:
        key_hmac = self.onboarding.lookup.digest(context.idempotency_key)
        row = connection.execute(
            "SELECT * FROM idempotency_requests WHERE account_id = ? AND route = ?"
            " AND idempotency_key_hmac = ?",
            (context.account_id, route, key_hmac),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now:
            # The idempotency row's primary key is the reusable command key.
            # Remove an expired generation inside the command transaction so
            # _remember() can insert the new generation without a PK collision.
            connection.execute(
                "DELETE FROM idempotency_requests WHERE account_id = ? AND route = ?"
                " AND idempotency_key_hmac = ? AND expires_at <= ?",
                (context.account_id, route, key_hmac, now),
            )
            return None
        if row["request_sha"] != request_sha:
            raise IdempotencyConflict("idempotency key was already used with another body")
        body = json.loads(row["response_body_json"])
        return CommandResult(OnboardingSnapshot.model_validate(body), replayed=True)

    def _remember(
        self,
        connection,
        *,
        context: CommandContext,
        route: str,
        request_sha: str,
        snapshot: OnboardingSnapshot,
        now: float,
    ) -> None:
        key_hmac = self.onboarding.lookup.digest(context.idempotency_key)
        connection.execute(
            "INSERT INTO idempotency_requests (account_id, route, idempotency_key_hmac,"
            " request_sha, response_status, response_body_json, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, 200, ?, ?, ?)",
            (
                context.account_id,
                route,
                key_hmac,
                request_sha,
                json.dumps(snapshot.model_dump(mode="json"), sort_keys=True),
                now,
                now + IDEMPOTENCY_TTL_SECONDS,
            ),
        )

    @staticmethod
    def _scoped_workflow(connection, account_id: str, household_id: str):
        row = connection.execute(
            "SELECT w.* FROM onboarding_workflows w"
            " JOIN household_memberships m ON m.household_id = w.household_id"
            " JOIN households h ON h.id = w.household_id"
            " WHERE w.household_id = ? AND m.account_id = ? AND m.status = 'active'"
            " AND h.status NOT IN ('deleting','deleted')",
            (household_id, account_id),
        ).fetchone()
        if row is None:
            raise KeyError(household_id)
        return row

    @staticmethod
    def _check_version(row, expected: int) -> None:
        if row["version"] != expected:
            raise WorkflowConflict(
                f"stale workflow version {expected}; current version is {row['version']}"
            )

    @staticmethod
    def _workflow_record(row) -> WorkflowRecord:
        return WorkflowRecord(
            row["id"], row["household_id"], row["state"], row["current_step"], row["version"]
        )

    def save_profile(
        self,
        household_id: str,
        profile: ProfileInput,
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        now = time.time() if now is None else now
        route = "/api/v1/onboarding/profile"
        body = profile.model_dump(mode="json")
        request_sha = self._request_sha(body)
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            self._check_version(row, context.expected_version)
            step = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = 'profile'",
                (row["id"],),
            ).fetchone()
            new_status = next_status(
                StepKind.PROFILE, StepStatus(step["status"]), SAVE_PROFILE
            )
            self.households.save_profile(
                household_id, profile, now=now, connection=connection
            )
            namespace_job_id, _ = self.jobs.create(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                kind="runtime",
                operation="ensure_secret_namespace",
                intent_key=f"{household_id}:secret-namespace",
                request={"household_id": household_id},
                provider=self.runtime_provider,
                now=now,
            )
            connection.execute(
                "UPDATE onboarding_steps SET status = ?, public_status_json = ?,"
                " updated_at = ? WHERE workflow_id = ? AND kind = 'profile'",
                (new_status.value, '{"state":"complete"}', now, row["id"]),
            )
            connection.execute(
                "UPDATE onboarding_steps SET status = 'available', updated_at = ?"
                " WHERE workflow_id = ? AND kind = 'email_identity' AND status = 'locked'",
                (now, row["id"]),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET state = 'in_progress',"
                " current_step = 'email_identity', version = ?, updated_at = ? WHERE id = ?",
                (new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command=SAVE_PROFILE,
                to_state="in_progress",
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                step_kind="profile",
                related_job_id=namespace_job_id,
                from_step_status=step["status"],
                to_step_status=new_status.value,
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)

    def _parse_selection(self, kind: StepKind, selection: dict[str, Any]) -> dict[str, Any]:
        adapters = {
            StepKind.EMAIL: TypeAdapter(EmailSelection),
            StepKind.WHATSAPP: TypeAdapter(WhatsAppSelection),
            StepKind.PRIMARY_CHANNEL: TypeAdapter(PrimaryChannelSelection),
        }
        if kind not in adapters:
            raise InvalidTransition(f"{kind.value} is not a selectable user step")
        return adapters[kind].validate_python(
            selection,
            context={"allow_real_email_domains": self.allow_real_email_domains},
        ).model_dump(mode="json")

    def _provider_for(self, kind: StepKind, selection_kind: str) -> str:
        if kind is StepKind.EMAIL and selection_kind == "family_domain":
            return self.byo_domain_provider
        if kind is StepKind.EMAIL and selection_kind == "gmail_agent":
            return self.gmail_provider
        return {
            StepKind.EMAIL: self.email_provider,
            StepKind.WHATSAPP: "fake-whatsapp",
            StepKind.PRIMARY_CHANNEL: "fake-channel",
        }[kind]

    def _assert_email_rollout(self, household_id: str, selection: dict[str, Any]) -> None:
        if not self.real_email_enabled:
            return
        if selection.get("kind") == "gmail_agent":
            return
        if household_id not in self.real_email_household_allowlist:
            raise InvalidTransition("real email is not enabled for this household")

    @staticmethod
    def _record_whatsapp_consents(
        connection,
        *,
        parsed: dict[str, Any],
        household_id: str,
        account_id: str,
        locale: str,
        now: float,
    ) -> None:
        receipts = [
            (
                parsed["privacy_notice_receipt_id"],
                "whatsapp_channel_privacy",
            )
        ]
        if parsed["kind"] == "dedicated_number":
            receipts.append((
                parsed["linked_device_risk_receipt_id"],
                "whatsapp_linked_device_risk",
            ))
        for receipt_id, purpose in receipts:
            existing = connection.execute(
                "SELECT household_id, account_id, purpose FROM consent_receipts WHERE id = ?",
                (receipt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["household_id"] != household_id
                    or existing["account_id"] != account_id
                    or existing["purpose"] != purpose
                ):
                    raise IdempotencyConflict("consent receipt belongs to another command")
                continue
            text_version, text_sha = consent_version_and_sha(purpose)
            connection.execute(
                "INSERT INTO consent_receipts (id, household_id, account_id, purpose,"
                " text_version, text_sha256, locale, accepted_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_id,
                    household_id,
                    account_id,
                    purpose,
                    text_version,
                    text_sha,
                    locale,
                    now,
                    now,
                ),
            )

    def select(
        self,
        household_id: str,
        kind: StepKind,
        selection: dict[str, Any],
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        now = time.time() if now is None else now
        parsed = self._parse_selection(kind, selection)
        route = f"/api/v1/onboarding/steps/{kind.value}/select"
        request_sha = self._request_sha(parsed)
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            if kind is StepKind.EMAIL:
                self._assert_email_rollout(household_id, parsed)
            self._check_version(row, context.expected_version)
            if row["current_step"] != kind.value:
                raise InvalidTransition("onboarding steps cannot be skipped or reordered")
            step = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
                (row["id"], kind.value),
            ).fetchone()
            if kind is StepKind.WHATSAPP:
                household_row = connection.execute(
                    "SELECT family_language FROM households WHERE id = ?", (household_id,)
                ).fetchone()
                self._record_whatsapp_consents(
                    connection,
                    parsed=parsed,
                    household_id=household_id,
                    account_id=context.account_id,
                    locale=household_row["family_language"] or "en",
                    now=now,
                )
            email_identity = None
            if kind is StepKind.EMAIL and self.email_identities is not None:
                email_identity = self.email_identities.select(
                    connection,
                    household_id=household_id,
                    selection=parsed,
                    now=now,
                )
            new_status = next_status(kind, StepStatus(step["status"]), SELECT)
            attempt = step["attempt"] + 1
            selection_kind = parsed["kind"]
            encrypted = self.onboarding.encrypt_json(
                "onboarding_steps", f"{row['id']}:{kind.value}", "selection", parsed
            )
            intent_key = f"{household_id}:{kind.value}:{selection_kind}:{attempt}"
            job_request = {
                "step_kind": kind.value,
                "selection": parsed,
                "attempt": attempt,
            }
            if email_identity is not None:
                intent_key = (
                    f"{household_id}:{kind.value}:{email_identity.id}:"
                    f"{selection_kind}:{attempt}"
                )
                job_request.update({
                    "email_identity_id": email_identity.id,
                    "household_id": household_id,
                    "option": email_identity.option.value,
                })
            job_id, _ = self.jobs.create(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                kind=("channel_binding" if kind is StepKind.PRIMARY_CHANNEL else kind.value),
                operation="ensure",
                intent_key=intent_key,
                request=job_request,
                provider=self._provider_for(kind, selection_kind),
                now=now,
            )
            public = {"state": "setting_up", "option": selection_kind}
            connection.execute(
                "UPDATE onboarding_steps SET status = ?, selection_kind = ?,"
                " selection_ciphertext = ?, result_ciphertext = NULL,"
                " encryption_key_version = ?, public_status_json = ?, error_code = NULL,"
                " attempt = ?, updated_at = ? WHERE workflow_id = ? AND kind = ?",
                (
                    new_status.value,
                    selection_kind,
                    encrypted.ciphertext,
                    encrypted.key_version,
                    self.onboarding.public_json(public),
                    attempt,
                    now,
                    row["id"],
                    kind.value,
                ),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET version = ?, updated_at = ? WHERE id = ?",
                (new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command=SELECT,
                to_state=row["state"],
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                step_kind=kind.value,
                from_step_status=step["status"],
                to_step_status=new_status.value,
                related_job_id=job_id,
                metadata={"selection_kind": selection_kind},
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)

    def retry(
        self,
        household_id: str,
        kind: StepKind,
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        self.households.authorized(context.account_id, household_id)
        workflow = self.onboarding.workflow_for_household(household_id)
        selection = self.onboarding.selection(workflow.id, kind)
        if selection is None:
            raise InvalidTransition("failed step has no durable selection to retry")
        # A retry is a new attempt/intent but uses the same selection. Give it a
        # distinct route so a prior select idempotency key cannot alias it.
        return self._retry_selection(
            household_id, kind, selection, context=context, now=now
        )

    def check(
        self,
        household_id: str,
        kind: StepKind,
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        if kind not in {StepKind.EMAIL, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL}:
            raise InvalidTransition("only a user identity/channel step can be checked")
        now = time.time() if now is None else now
        route = f"/api/v1/onboarding/steps/{kind.value}/check"
        request_sha = self._request_sha({"kind": kind.value})
        job_kind = "channel_binding" if kind is StepKind.PRIMARY_CHANNEL else kind.value
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            self._check_version(row, context.expected_version)
            if row["current_step"] != kind.value:
                raise InvalidTransition("only the current waiting step can be checked")
            step = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
                (row["id"], kind.value),
            ).fetchone()
            new_status = next_status(kind, StepStatus(step["status"]), CHECK)
            waiting_job = connection.execute(
                "SELECT * FROM provisioning_jobs WHERE workflow_id = ? AND kind = ?"
                " AND status = 'waiting_user' ORDER BY created_at DESC, id DESC LIMIT 1",
                (row["id"], job_kind),
            ).fetchone()
            if waiting_job is None:
                raise InvalidTransition("waiting step has no inspectable provider intent")
            stable_ref = waiting_job["intent_key"]
            if waiting_job["operation"] == "inspect":
                prior_request = self.jobs.decrypt_json(
                    "provisioning_jobs",
                    waiting_job["id"],
                    "request",
                    waiting_job["request_ciphertext"],
                    waiting_job["encryption_key_version"],
                )
                stable_ref = prior_request.get("stable_ref", stable_ref)
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?,"
                " updated_at = ? WHERE id = ? AND status = 'waiting_user'",
                (now, now, waiting_job["id"]),
            )
            inspect_request = {
                "step_kind": kind.value,
                "stable_ref": stable_ref,
                "attempt": step["attempt"],
            }
            if kind is StepKind.EMAIL and self.email_identities is not None:
                identity = self.email_identities.repository.current_for_household(
                    household_id
                )
                if identity is None:
                    raise InvalidTransition("email check has no durable identity")
                inspect_request.update({
                    "email_identity_id": identity.id,
                    "household_id": household_id,
                    "option": identity.option.value,
                    "selection": self.onboarding.selection(row["id"], kind),
                })
            inspect_id, _ = self.jobs.create(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                kind=job_kind,
                operation="inspect",
                intent_key=(
                    f"{household_id}:{kind.value}:inspect:{step['attempt']}:{row['version'] + 1}"
                ),
                request=inspect_request,
                provider=waiting_job["provider"],
                now=now,
            )
            connection.execute(
                "UPDATE onboarding_steps SET status = ?, public_status_json = ?,"
                " error_code = NULL, updated_at = ? WHERE workflow_id = ? AND kind = ?",
                (
                    new_status.value,
                    self.onboarding.public_json({"state": "checking"}),
                    now,
                    row["id"],
                    kind.value,
                ),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET version = ?, updated_at = ? WHERE id = ?",
                (new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command=CHECK,
                to_state=row["state"],
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                step_kind=kind.value,
                from_step_status=step["status"],
                to_step_status=new_status.value,
                related_job_id=inspect_id,
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)

    def _retry_selection(
        self,
        household_id: str,
        kind: StepKind,
        selection: dict[str, Any],
        *,
        context: CommandContext,
        now: float | None,
    ) -> CommandResult:
        now = time.time() if now is None else now
        parsed = self._parse_selection(kind, selection)
        route = f"/api/v1/onboarding/steps/{kind.value}/retry"
        request_sha = self._request_sha(parsed)
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            self._check_version(row, context.expected_version)
            step = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
                (row["id"], kind.value),
            ).fetchone()
            new_status = next_status(kind, StepStatus(step["status"]), RETRY)
            attempt = step["attempt"] + 1
            intent_key = f"{household_id}:{kind.value}:{parsed['kind']}:{attempt}"
            job_request = {
                "step_kind": kind.value,
                "selection": parsed,
                "attempt": attempt,
            }
            if kind is StepKind.EMAIL and self.email_identities is not None:
                identity = self.email_identities.repository.current_for_household(
                    household_id
                )
                if identity is None:
                    raise InvalidTransition("email retry has no durable identity")
                self.email_identities.repository.retry_provisioning(
                    connection, identity.id, now=now
                )
                intent_key = (
                    f"{household_id}:{kind.value}:{identity.id}:{parsed['kind']}:"
                    f"{attempt}"
                )
                job_request.update({
                    "email_identity_id": identity.id,
                    "household_id": household_id,
                    "option": identity.option.value,
                })
            job_id, _ = self.jobs.create(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                kind=("channel_binding" if kind is StepKind.PRIMARY_CHANNEL else kind.value),
                operation="ensure",
                intent_key=intent_key,
                request=job_request,
                provider=self._provider_for(kind, parsed["kind"]),
                now=now,
            )
            connection.execute(
                "UPDATE onboarding_steps SET status = ?, error_code = NULL, attempt = ?,"
                " public_status_json = ?, updated_at = ? WHERE workflow_id = ? AND kind = ?",
                (
                    new_status.value,
                    attempt,
                    self.onboarding.public_json({"state": "setting_up", "option": parsed["kind"]}),
                    now,
                    row["id"],
                    kind.value,
                ),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET version = ?, updated_at = ? WHERE id = ?",
                (new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command=RETRY,
                to_state=row["state"],
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                step_kind=kind.value,
                from_step_status=step["status"],
                to_step_status=new_status.value,
                related_job_id=job_id,
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)

    def _schedule_registered_cleanup(
        self,
        connection,
        *,
        household_id: str,
        workflow_id: str,
        workflow_version: int,
        now: float,
        resource_types: set[str] | None = None,
    ) -> list[str]:
        job_ids: list[str] = []
        resources = connection.execute(
            "SELECT * FROM external_resources WHERE household_id = ?"
            " AND status IN ('creating','ready','outcome_unknown')"
            " ORDER BY CASE resource_type"
            " WHEN 'email_identity' THEN 5 WHEN 'whatsapp_identity' THEN 4"
            " WHEN 'channel_binding' THEN 3 WHEN 'runtime' THEN 2"
            " WHEN 'secret_namespace' THEN 1"
            " ELSE 0 END DESC, created_at DESC, id DESC",
            (household_id,),
        ).fetchall()
        resources = [
            resource
            for resource in resources
            if resource_types is None or resource["resource_type"] in resource_types
        ]
        email_identity = connection.execute(
            "SELECT id FROM email_identities WHERE household_id = ?"
            " AND status != 'deleted' ORDER BY created_at DESC LIMIT 1",
            (household_id,),
        ).fetchone()
        waiting_email_job = connection.execute(
            "SELECT id FROM provisioning_jobs WHERE household_id = ?"
            " AND kind = 'email_identity' AND status = 'waiting_user'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (household_id,),
        ).fetchone()
        for sequence, resource in enumerate(resources):
            external_ref = self.jobs.decrypt_json(
                "external_resources",
                resource["id"],
                "external_id",
                resource["external_id_ciphertext"],
                resource["encryption_key_version"],
            )
            if resource["resource_type"] == "runtime" and isinstance(
                external_ref, dict
            ):
                # The manifest digest proves the runtime configuration during
                # provisioning, but deletion is bound to the recorded app,
                # Machine and volume identifiers. Keeping a 64-hex digest under
                # the cleanup request's open `external_ref` channel makes the
                # credential scanner correctly reject the whole transaction.
                # Remove only this non-secret, deletion-irrelevant field; exact
                # resource identifiers remain intact.
                external_ref = dict(external_ref)
                external_ref.pop("config_sha256", None)
            request = {
                "resource_id": resource["id"],
                "resource_type": resource["resource_type"],
                "external_ref": external_ref,
            }
            if resource["resource_type"] == "email_identity" and email_identity:
                request["email_identity_id"] = email_identity["id"]
                if waiting_email_job is not None:
                    request["parent_job_id"] = waiting_email_job["id"]
            elif (
                resource["resource_type"] == "email_identity"
                and resource["provider"] == "fake-email"
            ):
                # Phase-1 rows can predate durable email identities. They used
                # one fixed synthetic binding, so cleanup can still prove the
                # provider absent and idempotently remove that exact secret.
                request["legacy_secret_binding_ref"] = (
                    SYNTHETIC_EMAIL_SECRET_BINDING
                )
            job_id, _ = self.jobs.create(
                connection,
                household_id=household_id,
                workflow_id=workflow_id,
                kind="cleanup",
                operation="deprovision",
                intent_key=(
                    f"{household_id}:cleanup:{resource['id']}:{workflow_version}"
                ),
                request=request,
                provider=resource["provider"],
                now=now + sequence * 0.000001,
            )
            job_ids.append(job_id)
            connection.execute(
                "UPDATE external_resources SET status = 'deleting', updated_at = ?"
                " WHERE id = ?",
                (now, resource["id"]),
            )
        return job_ids

    @staticmethod
    def _supersede_unsettled_jobs(
        connection,
        *,
        household_id: str,
        reason: str,
        now: float,
    ) -> None:
        # A pending job has never crossed the provider boundary and is safe to
        # cancel. Running and waiting-user jobs may already have created
        # upstream state, so preserve their durable intent for explicit
        # inspect/reconcile/compensation instead of claiming they are absent.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?,"
            " updated_at = ?, lease_until = NULL, leased_by = NULL,"
            " error_code = ? WHERE household_id = ? AND status = 'pending'"
            " AND kind NOT IN ('cleanup','bootstrap_cleanup')",
            (now, now, f"{reason}_before_provider_call", household_id),
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown', settled_at = ?,"
            " updated_at = ?, lease_until = NULL, leased_by = NULL,"
            " error_code = ? WHERE household_id = ?"
            " AND status IN ('running','waiting_user')"
            " AND kind NOT IN ('cleanup','bootstrap_cleanup')",
            (now, now, f"{reason}_requires_reconciliation", household_id),
        )

    def _finish_safe_email_disconnect(
        self, connection, household_id: str, *, now: float
    ) -> None:
        if self.email_identities is None:
            return
        identity = connection.execute(
            "SELECT id FROM email_identities WHERE household_id = ?"
            " AND status = 'disconnecting' ORDER BY created_at DESC LIMIT 1",
            (household_id,),
        ).fetchone()
        if identity is None:
            return
        self.email_identities.repository.finish_disconnect(
            connection, identity["id"], now=now
        )

    def reset_from(
        self,
        household_id: str,
        kind: StepKind,
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        if kind not in {StepKind.EMAIL, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL}:
            raise InvalidTransition("profile reset is not a product step reset")
        now = time.time() if now is None else now
        route = f"/api/v1/onboarding/reset/{kind.value}"
        request_sha = self._request_sha({"kind": kind.value})
        order = [StepKind.EMAIL, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL]
        start = order.index(kind)
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            self._check_version(row, context.expected_version)
            target = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
                (row["id"], kind.value),
            ).fetchone()
            resettable = target["status"] == "verified" or (
                kind is StepKind.EMAIL and target["status"] == "waiting_user"
            )
            if not resettable:
                raise InvalidTransition(
                    "only a verified choice or pending email domain can be reset explicitly"
                )
            # Cleanup jobs are persisted before any external deprovision call,
            # including a runtime already activated from these choices.
            cleanup_jobs = self._schedule_registered_cleanup(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                workflow_version=row["version"] + 1,
                now=now,
                resource_types={
                    StepKind.EMAIL: {
                        "email_identity",
                        "whatsapp_identity",
                        "channel_binding",
                        "runtime",
                    },
                    StepKind.WHATSAPP: {
                        "whatsapp_identity",
                        "channel_binding",
                        "runtime",
                    },
                    StepKind.PRIMARY_CHANNEL: {"channel_binding", "runtime"},
                }[kind],
            )
            self._supersede_unsettled_jobs(
                connection,
                household_id=household_id,
                reason="reset",
                now=now,
            )
            if kind is StepKind.EMAIL:
                namespace = connection.execute(
                    "SELECT 1 FROM external_resources WHERE household_id = ?"
                    " AND resource_type = 'secret_namespace'"
                    " AND status IN ('creating','ready','outcome_unknown') LIMIT 1",
                    (household_id,),
                ).fetchone()
                if namespace is None:
                    # A pre-namespace production baseline can have a live Fly
                    # app/runtime resource but no durable namespace row. Queue
                    # this after superseding old work so the repair itself is
                    # not cancelled by the reset transaction.
                    self.jobs.create(
                        connection,
                        household_id=household_id,
                        workflow_id=row["id"],
                        kind="runtime",
                        operation="ensure_secret_namespace",
                        intent_key=(
                            f"{household_id}:secret-namespace:reset:"
                            f"{row['version'] + 1}"
                        ),
                        request={"household_id": household_id},
                        provider=self.runtime_provider,
                        now=now + (len(cleanup_jobs) + 1) * 0.000001,
                    )
            if kind is StepKind.EMAIL and self.email_identities is not None:
                self.email_identities.repository.begin_disconnect(
                    connection, household_id, now=now
                )
                self._finish_safe_email_disconnect(
                    connection, household_id, now=now
                )
            for index, downstream in enumerate(order[start:]):
                connection.execute(
                    "UPDATE onboarding_steps SET status = ?, selection_kind = NULL,"
                    " selection_ciphertext = NULL, result_ciphertext = NULL, error_code = NULL,"
                    " public_status_json = '{}', updated_at = ?"
                    " WHERE workflow_id = ? AND kind = ?",
                    (
                        "available" if index == 0 else "locked",
                        now,
                        row["id"],
                        downstream.value,
                    ),
                )
            connection.execute(
                "UPDATE config_revisions SET status = 'revoked' WHERE household_id = ?"
                " AND status IN ('planned','issued','claimed','active')",
                (household_id,),
            )
            connection.execute(
                "UPDATE bootstrap_tokens SET revoked_at = ? WHERE household_id = ?"
                " AND used_at IS NULL AND revoked_at IS NULL",
                (now, household_id),
            )
            connection.execute(
                "UPDATE households SET status = 'onboarding', runtime_ref = NULL,"
                " current_config_revision = 0, updated_at = ?"
                " WHERE id = ?",
                (now, household_id),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET state = 'in_progress', current_step = ?,"
                " version = ?, updated_at = ?, completed_at = NULL WHERE id = ?",
                (kind.value, new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command="reset",
                to_state="in_progress",
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                step_kind=kind.value,
                from_step_status=target["status"],
                to_step_status="available",
                related_job_id=cleanup_jobs[0] if cleanup_jobs else None,
                metadata={"cleanup_jobs": len(cleanup_jobs)},
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)

    def cancel(
        self,
        household_id: str,
        *,
        context: CommandContext,
        now: float | None = None,
    ) -> CommandResult:
        now = time.time() if now is None else now
        route = "/api/v1/onboarding/cancel"
        request_sha = self._request_sha({"cancel": True})
        with self.onboarding.db.write() as connection:
            replay = self._replay(
                connection, context=context, route=route, request_sha=request_sha, now=now
            )
            if replay:
                return replay
            row = self._scoped_workflow(connection, context.account_id, household_id)
            self._check_version(row, context.expected_version)
            if row["state"] in {"complete", "cancelled"}:
                raise InvalidTransition("workflow is already terminal")
            cleanup_jobs = self._schedule_registered_cleanup(
                connection,
                household_id=household_id,
                workflow_id=row["id"],
                workflow_version=row["version"] + 1,
                now=now,
            )
            self._supersede_unsettled_jobs(
                connection,
                household_id=household_id,
                reason="cancel",
                now=now,
            )
            if self.email_identities is not None:
                self.email_identities.repository.begin_disconnect(
                    connection, household_id, now=now
                )
                self._finish_safe_email_disconnect(
                    connection, household_id, now=now
                )
            connection.execute(
                "UPDATE onboarding_steps SET status = 'cancelled', updated_at = ?"
                " WHERE workflow_id = ? AND status != 'verified'",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE config_revisions SET status = 'revoked' WHERE household_id = ?"
                " AND status IN ('planned','issued','claimed')",
                (household_id,),
            )
            connection.execute(
                "UPDATE bootstrap_tokens SET revoked_at = ? WHERE household_id = ?"
                " AND used_at IS NULL AND revoked_at IS NULL",
                (now, household_id),
            )
            connection.execute(
                "UPDATE households SET status = 'draft', runtime_ref = NULL,"
                " current_config_revision = 0, updated_at = ?"
                " WHERE id = ?",
                (now, household_id),
            )
            new_version = row["version"] + 1
            connection.execute(
                "UPDATE onboarding_workflows SET state = 'cancelled', version = ?,"
                " updated_at = ? WHERE id = ?",
                (new_version, now, row["id"]),
            )
            self.onboarding.append_transition(
                connection,
                workflow=self._workflow_record(row),
                new_version=new_version,
                command="cancel",
                to_state="cancelled",
                account_id=context.account_id,
                session_id=context.session_id,
                request_id=context.request_id,
                related_job_id=cleanup_jobs[0] if cleanup_jobs else None,
                metadata={"cleanup_jobs": len(cleanup_jobs)},
                now=now,
            )
            snapshot = self.onboarding.snapshot(household_id)
            self._remember(
                connection,
                context=context,
                route=route,
                request_sha=request_sha,
                snapshot=snapshot,
                now=now,
            )
            return CommandResult(snapshot)
