"""The Gmail option: a hidden Nerve relay, then the Google OAuth wait states.

The send-only Gmail grant (owner decision 2026-09-13) has no read path. Mail
reaches the household because the family turns on Gmail forwarding to a hidden
inbox on the platform domain — a Nerve inbox like a managed one, owned by the
same email identity, under an address nobody can guess. This provisioner is
registered as `google-oauth`, so selection routing and the kill-switch table
in `feature_flags.CUT_EMAIL_OPTIONS` stay as they were; what it adds is the
relay in front of the OAuth states it inherits.

Order: relay first. A family should not be walked through Google's consent
screen for a household whose relay Nerve cannot create, or whose org still
waits on the attachments flag an operator has to set — both are known before
anyone is asked to consent. The relay graph is idempotent on external refs, so
re-running it on every check while OAuth is pending costs a key rotation and
creates nothing twice.

The relay address is DERIVED, not drawn: `fwd-<26 base32 chars>@abrolia.com`,
the 128 bits being the lookup HMAC of the identity id. The plan asked for
random bits persisted before the inbox call; a keyed derivation is what that
persistence was for — the same address on every attempt, computable before the
first provider call, and still unguessable — without a write the provisioner
has no row to make. `email_org_external_ref` already works this way.

Teardown reaches both halves from what the control plane can name
(`AGENTS.repo-invariants.md`, "Withdrawal tears down what it can NAME"): the
settled `{google, nerve}` reference, a bare `google-oauth:<identity_id>` (the
relay's org reference is then computed from the identity's household), or a
computed `nerve-org:<org_external_ref>`. None of the three asks the provider
what it holds.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from control_plane.crypto import SecretMaterial
from control_plane.email.models import (
    GMAIL_DISCLOSURE,
    GMAIL_RELAY_LOCAL_PART_PREFIX,
    NERVE_EMAIL_SECRET_BINDING,
    EmailGoogleOAuthPublicStatus,
    EmailNerveAttachmentPublicStatus,
    EmailOption,
    EmailProvisionIntent,
)
from control_plane.feature_flags import check_gmail_relay_enabled
from control_plane.providers.email.google_oauth import (
    GoogleOAuthProvisioner,
    GoogleOAuthService,
    RuntimeGrantRevoker,
    SecretNamespaceGrantRevoker,
)
from control_plane.providers.email.nerve_client import (
    ORG_TEARDOWN_REF_PREFIX,
    org_teardown_ref,
)
from control_plane.providers.email.nerve_managed import (
    AttachmentFlagPending,
    ManagedNerveRefs,
    NerveManagedEmailProvisioner,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)

GOOGLE_REF_PREFIX = "google-oauth:"
RELAY_DOMAIN = "abrolia.com"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def relay_binding(refs: ManagedNerveRefs) -> str:
    """What the runtime reads: the relay's org, inbox and its own address."""
    return _canonical(
        {"org_id": refs.org_id, "inbox_id": refs.inbox_id, "address": refs.address}
    )


class GmailForwardingProvisioner(GoogleOAuthProvisioner):
    def __init__(
        self,
        service: GoogleOAuthService,
        nerve: NerveManagedEmailProvisioner | None,
        revoker: RuntimeGrantRevoker | None = None,
        namespace_revoker: SecretNamespaceGrantRevoker | None = None,
    ) -> None:
        super().__init__(service, revoker=revoker, namespace_revoker=namespace_revoker)
        self.nerve = nerve

    # --- the relay ------------------------------------------------------------

    def relay_address(self, identity_id: str) -> str:
        digest = bytes.fromhex(self.service.token_hasher.digest(f"gmail-relay:{identity_id}"))
        local = base64.b32encode(digest[:16]).decode("ascii").rstrip("=").lower()
        return f"{GMAIL_RELAY_LOCAL_PART_PREFIX}{local}@{RELAY_DOMAIN}"

    def _ensure_relay(
        self, parsed: EmailProvisionIntent, idempotency_key: str
    ) -> tuple[ManagedNerveRefs, str] | None:
        """The relay, or None where this deployment has no Nerve to put it on.

        A deployment without Nerve configured — the synthetic and test ones —
        still offers the Gmail option and still runs real Google OAuth for
        its test users; what it cannot do is receive. Its Gmail household is
        then send-only with `forwarding = none`, which is exactly the Phase 1
        shape and what the runtime reports. Production configures Nerve, so
        there the relay is never optional.
        """
        if self.nerve is None:
            return None
        # The switches, at the provider call, before the first Nerve request:
        # the option's own, and the managed/BYO incident brake, because the
        # relay is a real Nerve inbox.
        try:
            check_gmail_relay_enabled()
        except RuntimeError as error:
            raise ProviderRejected(str(error)) from error
        try:
            return self.nerve.ensure_inbox_graph(
                household_id=parsed.household_id,
                identity_id=parsed.identity_id,
                address=self.relay_address(parsed.identity_id),
                secret_namespace_ref=parsed.secret_namespace_ref,
                idempotency_key=idempotency_key,
            )
        except AttachmentFlagPending as pending:
            # The managed adapter's waiting shape, carried inside the Gmail
            # one: the worker validates a Gmail job's waiting state as
            # `EmailGoogleOAuthPublicStatus` under `google-oauth:<identity>`.
            status = EmailGoogleOAuthPublicStatus(
                state="relay_pending",
                disclosure=GMAIL_DISCLOSURE,
                relay=EmailNerveAttachmentPublicStatus.model_validate(pending.public_result),
            )
            raise ProviderWaiting(
                "the Gmail relay awaits the Nerve attachments flag",
                public_result=status.model_dump(mode="json", exclude_none=True),
                external_ref=f"{GOOGLE_REF_PREFIX}{parsed.identity_id}",
            ) from pending

    # --- forward --------------------------------------------------------------

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        parsed = EmailProvisionIntent.model_validate(intent)
        if parsed.option is not EmailOption.GMAIL:
            raise ProviderRejected("Google OAuth provider received another email option")
        relay = self._ensure_relay(parsed, idempotency_key)
        row = self._row(parsed.identity_id)
        if row is not None and row["confirmed_at"] is not None:
            if relay is None:
                return self._result(parsed.identity_id, row)
            return self._relay_result(parsed.identity_id, row, *relay)
        status = EmailGoogleOAuthPublicStatus(
            state=(
                "dedicated_account_confirmation"
                if row is not None and row["callback_at"] is not None
                else "oauth_required"
            ),
            disclosure=GMAIL_DISCLOSURE,
        )
        raise ProviderWaiting(
            "Google OAuth user action is required",
            public_result=status.model_dump(mode="json", exclude_none=True),
            external_ref=f"{GOOGLE_REF_PREFIX}{parsed.identity_id}",
        )

    def _relay_result(
        self, identity_id: str, row, refs: ManagedNerveRefs, bundle: str
    ) -> ProvisionResult:
        google = self._result(identity_id, row)
        return ProvisionResult(
            external_ref=_canonical(
                {"google": google.external_ref, "nerve": json.loads(refs.encode())}
            ),
            public_result={
                **google.public_result,
                "inbound_binding_ref": relay_binding(refs),
                "inbound_secret_binding_ref": NERVE_EMAIL_SECRET_BINDING,
            },
            # The Google grant was installed by the OAuth callback and is
            # proven by `pre_staged_secret_verified`; the material here is the
            # relay's runtime credential, and only that.
            secret_material=SecretMaterial.from_mapping({NERVE_EMAIL_SECRET_BINDING: bundle}),
        )

    def inspect(self, stable_ref: str) -> InspectResult:
        if not stable_ref.startswith("{"):
            return super().inspect(stable_ref)
        parts = self._composite(stable_ref)
        google = super().inspect(parts["google"])
        if google.state is not InspectState.READY or google.result is None:
            return google
        if self.nerve is None:
            return InspectResult(InspectState.UNKNOWN, error_code="relay_unavailable")
        try:
            check_gmail_relay_enabled()
        except RuntimeError as error:
            raise ProviderRejected(str(error)) from error
        nerve = self.nerve.inspect(_canonical(parts["nerve"]))
        if nerve.state is not InspectState.READY or nerve.result is None:
            return nerve
        refs = ManagedNerveRefs.decode(nerve.result.external_ref)
        secret = next(
            (bytes(value).decode() for name, value in nerve.result.secret_material.items()),
            None,
        )
        if secret is None:
            return InspectResult(InspectState.UNKNOWN, error_code="credential_recovery_unknown")
        identity_id = parts["google"][len(GOOGLE_REF_PREFIX) :]
        return InspectResult(
            InspectState.READY, self._relay_result(identity_id, self._row(identity_id), refs, secret)
        )

    # --- teardown -------------------------------------------------------------

    @staticmethod
    def _composite(external_ref: str) -> dict[str, Any]:
        try:
            parts = json.loads(external_ref)
        except ValueError as error:
            raise ProviderRejected("invalid Gmail relay resource reference") from error
        if (
            not isinstance(parts, dict)
            or set(parts) != {"google", "nerve"}
            or not isinstance(parts["google"], str)
            or not parts["google"].startswith(GOOGLE_REF_PREFIX)
            or not isinstance(parts["nerve"], dict)
        ):
            raise ProviderRejected("invalid Gmail relay resource reference")
        return parts

    def deprovision(self, external_ref: str) -> InspectResult:
        """Google revoke and secret delete, then the relay, by whatever names this."""
        if external_ref.startswith(ORG_TEARDOWN_REF_PREFIX):
            return self._relay_teardown(external_ref)
        if external_ref.startswith("{"):
            parts = self._composite(external_ref)
            google = super().deprovision(parts["google"])
            if google.state is not InspectState.ABSENT:
                return google
            return self._relay_teardown(_canonical(parts["nerve"]))
        google = super().deprovision(external_ref)
        if google.state is not InspectState.ABSENT:
            return google
        # A bare Google reference — the waiting state's, or the derived one a
        # shutdown computes — says nothing about the relay, which may have
        # been created before OAuth ever started. Its org reference is
        # arithmetic on the identity's household, read from this side's own
        # store: no provider is asked what it holds.
        identity = self.service.identities.get(external_ref[len(GOOGLE_REF_PREFIX) :])
        if identity is None:
            return google
        return self._relay_teardown(
            org_teardown_ref(identity.household_id, identity.id)
        )

    def _relay_teardown(self, reference: str) -> InspectResult:
        if self.nerve is None:
            # Nothing could have created a relay without the client; there is
            # no relay to remove.
            return InspectResult(InspectState.ABSENT)
        return self.nerve.deprovision(reference)
