"""Withdrawal of an authoritative consent, Art. 7(3) GDPR.

The Art 9(2)(a) copy promises the family that consent "can be withdrawn at any
time in one step" and that "withdrawal stops further processing". Until this
module existed nothing in the codebase ever set `consent_receipts.revoked_at`:
retention deleted already-revoked rows and export read them, but no code
performed a withdrawal. The promise was kept, if at all, by a support engineer
editing the database.

Three things have to happen, and the first two alone are not enough:

1. **Mark the receipt.** Every enforcement boundary reads `revoked_at IS NULL`,
   so this is what stops the next job, the next activation, and the next issued
   revision.

2. **Revoke the active configuration revision.** The planner cannot simply issue
   a replacement that drops the purpose: for a real-email household the
   requirement is derived from the provider, not from what the household holds,
   so the purpose is still owed and the planner refuses. Revoking the revision
   is what makes the durable state agree with the receipt.

3. **Tell the running runtime.** This is the step that is easy to miss and the
   only one the family can observe. A live runtime serves a manifest that is
   IMMUTABLE — the purpose, version and digest embedded in it stay
   valid-looking forever — and it re-reads that local file, not the control
   plane's database. So steps 1 and 2 protect every FUTURE job and activation
   while the instance already serving the household carries on processing. The
   runtime is told over the same private-network path the readiness monitor
   uses, with the deterministically derived DSAR credential.

Step 3 is enqueued rather than performed inline: an unreachable runtime must not
fail the withdrawal, which is a right and not an attempt. The receipt is already
revoked when the job runs, so the boundaries are closed either way, and the job
retries until the runtime confirms.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from control_plane.db import ControlPlaneDatabase
from control_plane.privacy.consent import CONSENT_TEXTS
from control_plane.privacy.runtime import RUNTIME_REF
from control_plane.repositories.jobs import JobsRepository

#: The runtime job that carries the stop signal. It reuses the `runtime` kind
#: because `provisioning_jobs.kind` is constrained by a CHECK and `operation` is
#: not — a new kind would need a table rebuild for no behavioural gain.
REVOKE_CONSENT_OPERATION = "revoke_consent"


class ConsentNotHeld(RuntimeError):
    """Nothing to withdraw: no current receipt for this purpose."""


@dataclass(frozen=True)
class WithdrawalResult:
    household_id: str
    purpose: str
    receipts_revoked: int
    revisions_revoked: int
    runtime_job_id: str | None
    email_cleanup_job_ids: tuple[str, ...] = ()

    @property
    def email_disconnected(self) -> bool:
        """Whether an upstream inbox teardown was scheduled."""
        return bool(self.email_cleanup_job_ids)

    @property
    def runtime_notified(self) -> bool:
        """Whether a live runtime was asked to stop; False when none exists."""
        return self.runtime_job_id is not None


class ConsentWithdrawalService:
    def __init__(
        self,
        database: ControlPlaneDatabase,
        *,
        jobs: JobsRepository,
        onboarding: Any | None = None,
    ) -> None:
        self.database = database
        self.jobs = jobs
        #: The onboarding service owns the email teardown. Optional so the
        #: control-plane half stays testable on its own, but wired in the
        #: container — without it, withdrawal stops our runtime and leaves the
        #: provisioned inbox upstream still receiving.
        self.onboarding = onboarding

    def withdraw(
        self, household_id: str, purpose: str, *, now: float
    ) -> WithdrawalResult:
        if purpose not in CONSENT_TEXTS:
            raise KeyError(purpose)

        with self.database.write() as connection:
            # Capture WHICH receipts are being withdrawn before revoking them:
            # the intent key below is keyed to this consent cycle, and after the
            # UPDATE they are indistinguishable from any earlier withdrawal.
            cycle = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM consent_receipts WHERE household_id = ?"
                    " AND purpose = ? AND revoked_at IS NULL ORDER BY id",
                    (household_id, purpose),
                ).fetchall()
            ]
            if not cycle:
                # Nothing left to revoke: this is a repeat of a withdrawal
                # already performed. Identify that same cycle by the receipts it
                # revoked, so the key stays stable and the stop stays a single
                # job — an empty cycle would hash differently every time and
                # enqueue a fresh stop on every retry.
                cycle = [
                    str(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM consent_receipts WHERE household_id = ?"
                        " AND purpose = ? AND revoked_at = ("
                        "  SELECT MAX(revoked_at) FROM consent_receipts"
                        "  WHERE household_id = ? AND purpose = ?"
                        ") ORDER BY id",
                        (household_id, purpose, household_id, purpose),
                    ).fetchall()
                ]
            revoked = connection.execute(
                "UPDATE consent_receipts SET revoked_at = ?"
                " WHERE household_id = ? AND purpose = ? AND revoked_at IS NULL",
                (now, household_id, purpose),
            ).rowcount
            if not revoked:
                # Idempotent only where it is safe to be: a second withdrawal of
                # an already-withdrawn consent is a no-op, but withdrawing a
                # consent that was never given is a caller error worth surfacing.
                held = connection.execute(
                    "SELECT 1 FROM consent_receipts WHERE household_id = ?"
                    " AND purpose = ? LIMIT 1",
                    (household_id, purpose),
                ).fetchone()
                if held is None:
                    raise ConsentNotHeld(f"{household_id} holds no {purpose} receipt")

            # Any revision still standing embeds the withdrawn receipt and would
            # otherwise remain activatable.
            revisions = connection.execute(
                "UPDATE config_revisions SET status = 'revoked'"
                " WHERE household_id = ? AND status IN ('planned','issued','claimed','active')",
                (household_id,),
            ).rowcount

            # Disconnect the inbox BEFORE queuing the runtime stop, so the
            # ordering in the job table matches the order the effects matter in:
            # stop accepting new content, then stop processing what arrived.
            email_cleanup_job_ids: list[str] = []
            if self.onboarding is not None:
                email_cleanup_job_ids = (
                    self.onboarding.disconnect_email_for_withdrawal(
                        connection, household_id, now=now
                    )
                )

            runtime_job_id = self._enqueue_runtime_stop(
                connection,
                household_id=household_id,
                purpose=purpose,
                cycle=cycle,
                now=now,
            )

        return WithdrawalResult(
            household_id=household_id,
            purpose=purpose,
            receipts_revoked=revoked,
            revisions_revoked=revisions,
            runtime_job_id=runtime_job_id,
            email_cleanup_job_ids=tuple(email_cleanup_job_ids),
        )

    def _enqueue_runtime_stop(
        self,
        connection,
        *,
        household_id: str,
        purpose: str,
        cycle: list[str],
        now: float,
    ) -> str | None:
        household = connection.execute(
            "SELECT runtime_ref FROM households WHERE id = ?", (household_id,)
        ).fetchone()
        runtime_ref = str((household or {})["runtime_ref"] or "") if household else ""
        if not RUNTIME_REF.fullmatch(runtime_ref):
            # Nothing serving that can be told. Either nothing was provisioned,
            # or the reference is a synthetic one — `DryRunRuntimeProvisioner`
            # stores `synthetic-runtime:<household_id>`, which is non-empty and
            # so passed an emptiness check, producing a stop job the worker was
            # certain to reject and settle as failed. A withdrawal reported as
            # `runtime_notified` and then failing is worse than one that says
            # plainly there was nothing to notify.
            #
            # Nothing is lost by skipping: the revoked receipt already closes
            # every control-plane boundary, and a synthetic runtime holds no
            # real family content to stop processing.
            return None
        workflow = connection.execute(
            "SELECT id FROM onboarding_workflows WHERE household_id = ?",
            (household_id,),
        ).fetchone()
        if workflow is None:
            return None
        # Keyed to THIS consent cycle, not just the purpose. A household can
        # withdraw, reset onboarding, accept a new receipt, provision a fresh
        # runtime, and withdraw again. A key of household+purpose matched the
        # first withdrawal's job, `JobsRepository.create` returned that already
        # succeeded job, and no stop was queued for the new runtime — so the
        # second withdrawal silently did nothing to the instance then serving.
        #
        # The receipt ids being revoked identify the cycle: they are new after
        # re-consent, and identical within one withdrawal, which is exactly the
        # idempotency the previous key was reaching for. The runtime ref is in
        # the key too, so a re-provisioned runtime gets its own stop even if the
        # same receipts somehow persist.
        fingerprint = hashlib.sha256(
            "|".join([runtime_ref, purpose, *cycle]).encode("utf-8")
        ).hexdigest()[:32]
        job_id, _created = self.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="runtime",
            operation=REVOKE_CONSENT_OPERATION,
            intent_key=f"{household_id}:consent-revoke:{fingerprint}",
            request={"runtime_ref": runtime_ref, "purpose": purpose},
            provider="dry-run-runtime",
            now=now,
        )
        return job_id
