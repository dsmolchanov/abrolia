from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from control_plane.config import ControlPlaneConfig
from control_plane.crypto import SecretMaterial, normalize_email
from control_plane.email.models import (
    GMAIL_EMAIL_SCOPES,
    GMAIL_EMAIL_SECRET_BINDING,
    EmailGoogleOAuthPublicStatus,
    EmailOption,
    EmailProvisionIntent,
)
from control_plane.email.repository import EmailIdentityRepository
from control_plane.owners import owner_contact_query
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
    SecretSink,
)
from control_plane.repositories.accounts import AccountsRepository
from control_plane.repositories.jobs import JobsRepository

OAUTH_TTL_SECONDS = 10 * 60
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKEN_INFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_USER_INFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
DISCLOSURE = (
    "Abrolia reads and sends mail only for this dedicated agent mailbox; "
    "Google data is not used to train a general model."
)


class GoogleOAuthError(RuntimeError):
    """A redacted OAuth failure safe to expose as a fixed error code."""


class GoogleOAuthDisabled(GoogleOAuthError):
    pass


class GoogleOAuthInvalidState(GoogleOAuthError):
    pass


class GoogleOAuthSecretHandoffUnknown(GoogleOAuthError):
    pass


@dataclass(frozen=True, repr=False)
class GoogleTokenGrant:
    subject: str
    email: str
    scopes: tuple[str, ...]
    refresh_credential: str
    access_token: str


class OAuthClient(Protocol):
    client_id: str

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    def exchange(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokenGrant: ...

    def revoke(self, credential: str) -> None: ...


class RuntimeGrantRevoker(Protocol):
    def revoke_google(self, runtime_ref: str) -> InspectState: ...


class SecretNamespaceGrantRevoker(Protocol):
    def revoke_google_secret(
        self, app_ref: str, transaction_id: str
    ) -> InspectState: ...


class GoogleOAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Google OAuth client configuration is incomplete")
        self.client_id = client_id
        self._client_secret = client_secret
        self._client = client or httpx.Client(timeout=20.0)

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(GMAIL_EMAIL_SCOPES),
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "select_account consent",
                "include_granted_scopes": "false",
            }
        )
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    def exchange(self, *, code: str, code_verifier: str, redirect_uri: str) -> GoogleTokenGrant:
        try:
            response = self._client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OutcomeUnknown("Google OAuth exchange outcome is unknown") from error
        if response.status_code != 200:
            raise ProviderRejected("Google rejected the OAuth exchange")
        try:
            token = response.json()
            access_token = str(token["access_token"])
            refresh_credential = str(token["refresh_token"])
            scopes = tuple(sorted(str(token.get("scope", "")).split()))
            token_info_response = self._client.get(
                GOOGLE_TOKEN_INFO_ENDPOINT,
                params={"id_token": str(token["id_token"])},
            )
            user_info_response = self._client.get(
                GOOGLE_USER_INFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if token_info_response.status_code != 200 or user_info_response.status_code != 200:
                raise ValueError("identity endpoint rejected token")
            token_info = token_info_response.json()
            user_info = user_info_response.json()
        except (KeyError, TypeError, ValueError, httpx.HTTPError) as error:
            raise ProviderRejected("Google returned an invalid OAuth identity") from error
        if (
            token_info.get("aud") != self.client_id
            or token_info.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
            or token_info.get("sub") != user_info.get("sub")
            or user_info.get("email_verified") is not True
            or not user_info.get("email")
        ):
            raise ProviderRejected("Google OAuth identity validation failed")
        return GoogleTokenGrant(
            subject=str(user_info["sub"]),
            email=normalize_email(str(user_info["email"])),
            scopes=scopes,
            refresh_credential=refresh_credential,
            access_token=access_token,
        )

    def revoke(self, credential: str) -> None:
        try:
            response = self._client.post("https://oauth2.googleapis.com/revoke", params={"token": credential})
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OutcomeUnknown("Google revoke outcome is unknown") from error
        if response.status_code not in {200, 400}:
            raise OutcomeUnknown("Google revoke outcome is unknown")


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str
    expires_at: float


class GoogleOAuthService:
    def __init__(
        self,
        *,
        config: ControlPlaneConfig,
        client: OAuthClient | None,
        identities: EmailIdentityRepository,
        accounts: AccountsRepository,
        jobs: JobsRepository,
        secret_sink: SecretSink,
        token_hasher,
        clock=time.time,
    ) -> None:
        self.config = config
        self.client = client
        self.identities = identities
        self.accounts = accounts
        self.jobs = jobs
        self.secret_sink = secret_sink
        self.token_hasher = token_hasher
        self.clock = clock

    @property
    def redirect_uri(self) -> str:
        return f"{self.config.public_origin}/api/v1/email/google/callback"

    def _allowed(self, account_id: str) -> bool:
        account = self.accounts.get(account_id)
        return bool(
            account
            and self.client
            and (
                self.config.gmail_real_enabled
                or account.recovery_email.casefold() in self.config.google_oauth_test_users
            )
        )

    def _namespace_ref(self, household_id: str) -> str:
        row = self.jobs.db.query_one(
            "SELECT * FROM external_resources WHERE household_id = ?"
            " AND resource_type = 'secret_namespace' AND status = 'ready'"
            " ORDER BY updated_at DESC, id DESC LIMIT 1",
            (household_id,),
        )
        if row is None:
            raise GoogleOAuthDisabled("runtime secret namespace is not ready")
        value = self.jobs.decrypt_json(
            "external_resources",
            row["id"],
            "external_id",
            row["external_id_ciphertext"],
            row["encryption_key_version"],
        )
        if not isinstance(value, str) or not value:
            raise GoogleOAuthDisabled("runtime secret namespace is not ready")
        return value

    @staticmethod
    def _mask(address: str) -> str:
        local, domain = address.split("@", 1)
        return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"

    def start(
        self,
        *,
        household_id: str,
        account_id: str,
        session_id: str,
        workflow_version: int,
    ) -> OAuthStart:
        if not self._allowed(account_id):
            raise GoogleOAuthDisabled("Google OAuth is disabled by policy")
        identity = self.identities.current_for_household(household_id)
        if identity is None or identity.option is not EmailOption.GMAIL:
            raise GoogleOAuthInvalidState("no pending Gmail identity")
        self._namespace_ref(household_id)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode("ascii")
        )
        transaction_id = secrets.token_hex(16)
        encrypted = self.identities.encrypt_json(
            "oauth_transactions", transaction_id, "pkce_verifier", verifier
        )
        now = self.clock()
        with self.jobs.db.write() as connection:
            connection.execute(
                "UPDATE oauth_transactions SET failed_at = ? WHERE email_identity_id = ?"
                " AND failed_at IS NULL AND revoked_at IS NULL",
                (now, identity.id),
            )
            connection.execute(
                "INSERT INTO oauth_transactions (id, household_id, onboarding_session_id,"
                " account_id, state_hash, pkce_verifier_ciphertext, requested_scopes_json,"
                " workflow_version, encryption_key_version, expires_at, created_at,"
                " email_identity_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transaction_id,
                    household_id,
                    session_id,
                    account_id,
                    self.token_hasher.digest(state),
                    encrypted.ciphertext,
                    json.dumps(GMAIL_EMAIL_SCOPES, separators=(",", ":")),
                    workflow_version,
                    encrypted.key_version,
                    now + OAUTH_TTL_SECONDS,
                    now,
                    identity.id,
                ),
            )
        assert self.client is not None
        return OAuthStart(
            self.client.authorization_url(
                state=state, code_challenge=challenge, redirect_uri=self.redirect_uri
            ),
            now + OAUTH_TTL_SECONDS,
        )

    def callback(
        self,
        *,
        state: str,
        code: str,
        household_id: str,
        account_id: str,
        session_id: str,
    ) -> str:
        now = self.clock()
        row = self.jobs.db.query_one(
            "SELECT * FROM oauth_transactions WHERE state_hash = ?",
            (self.token_hasher.digest(state),),
        )
        if (
            row is None
            or row["household_id"] != household_id
            or row["account_id"] != account_id
            or row["onboarding_session_id"] != session_id
            or row["expires_at"] <= now
            or row["consumed_at"] is not None
            or row["failed_at"] is not None
            or row["revoked_at"] is not None
        ):
            raise GoogleOAuthInvalidState("invalid or expired OAuth state")
        identity = self.identities.current_for_household(household_id)
        workflow = self.jobs.db.query_one(
            "SELECT version FROM onboarding_workflows WHERE household_id = ?",
            (household_id,),
        )
        if (
            identity is None
            or identity.id != row["email_identity_id"]
            or workflow is None
            or workflow["version"] != row["workflow_version"]
        ):
            raise GoogleOAuthInvalidState("OAuth transaction no longer matches onboarding")
        verifier = self.identities.decrypt_json(
            "oauth_transactions",
            row["id"],
            "pkce_verifier",
            row["pkce_verifier_ciphertext"],
            row["encryption_key_version"],
        )
        with self.jobs.db.write() as connection:
            claimed = connection.execute(
                "UPDATE oauth_transactions SET consumed_at = ? WHERE id = ?"
                " AND consumed_at IS NULL AND failed_at IS NULL AND revoked_at IS NULL",
                (now, row["id"]),
            )
        if claimed.rowcount != 1:
            raise GoogleOAuthInvalidState("OAuth state was already consumed")
        assert self.client is not None
        try:
            grant = self.client.exchange(code=code, code_verifier=verifier, redirect_uri=self.redirect_uri)
        except OutcomeUnknown as error:
            with self.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE oauth_transactions SET failed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            raise GoogleOAuthSecretHandoffUnknown("OAuth exchange outcome could not be proven") from error
        except ProviderRejected:
            with self.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE oauth_transactions SET failed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            raise
        if tuple(sorted(grant.scopes)) != tuple(sorted(GMAIL_EMAIL_SCOPES)):
            with self.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE oauth_transactions SET failed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            self.client.revoke(grant.refresh_credential)
            raise ProviderRejected("Google granted an unexpected scope set")
        account = self.accounts.get(account_id)
        # Two rules, and they are not the same one. The first is this option's
        # own: a household's assistant does not read the mail of the person who
        # connected it. The second is C4a's, and this is the path that could
        # not ask it earlier — `EmailIdentityService.select` refuses a mailbox
        # equal to an active owner's contact address, but a Gmail address is
        # not known until Google grants it, so `gmail_agent` reaches selection
        # with no address at all.
        #
        # An adult, or a second owner, could therefore connect the mailbox the
        # FALLBACK owner is reached at. Refused here, before the credential is
        # installed, and refused the way this method already refuses: revoke
        # the grant, then the correctable provider rejection.
        collision_sql, collision_params = owner_contact_query(
            self.accounts.lookup, household_id=row["household_id"], address=grant.email
        )
        if (
            account is None
            or normalize_email(account.recovery_email) == grant.email
            or self.accounts.db.query_one(collision_sql, collision_params) is not None
        ):
            self.client.revoke(grant.refresh_credential)
            raise ProviderRejected("Gmail must be a separate dedicated mailbox")
        namespace_ref = self._namespace_ref(row["household_id"])
        bundle = json.dumps(
            {
                "client_id": self.config.google_oauth_client_id,
                "client_secret": self.config.google_oauth_client_secret,
                "refresh_credential": grant.refresh_credential,
                "provider_subject": grant.subject,
                "scopes": list(GMAIL_EMAIL_SCOPES),
                "wrapping_key": base64.urlsafe_b64encode(secrets.token_bytes(32))
                .rstrip(b"=")
                .decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(grant.refresh_credential.encode()).hexdigest()
        material = SecretMaterial.from_mapping({GMAIL_EMAIL_SECRET_BINDING: bundle})
        try:
            self.secret_sink.install(namespace_ref, material)
        except Exception as error:
            material.clear()
            try:
                self.client.revoke(grant.refresh_credential)
            finally:
                with self.jobs.db.write() as connection:
                    connection.execute(
                        "UPDATE oauth_transactions SET failed_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
            raise GoogleOAuthSecretHandoffUnknown("OAuth credential handoff could not be proven") from error
        subject = self.identities.encrypt_json(
            "oauth_transactions", row["id"], "provider_subject", grant.subject
        )
        address = self.identities.encrypt_json("oauth_transactions", row["id"], "address", grant.email)
        with self.jobs.db.write() as connection:
            connection.execute(
                "UPDATE oauth_transactions SET provider_subject_ciphertext = ?,"
                " address_ciphertext = ?, granted_scopes_json = ?, secret_binding_ref = ?,"
                " credential_digest = ?, callback_at = ? WHERE id = ?",
                (
                    subject.ciphertext,
                    address.ciphertext,
                    json.dumps(GMAIL_EMAIL_SCOPES, separators=(",", ":")),
                    GMAIL_EMAIL_SECRET_BINDING,
                    digest,
                    now,
                    row["id"],
                ),
            )
            masked = self._mask(grant.email)
            connection.execute(
                "UPDATE onboarding_steps SET public_status_json = ?, updated_at = ?"
                " WHERE workflow_id = (SELECT id FROM onboarding_workflows"
                " WHERE household_id = ?) AND kind = 'email_identity'",
                (
                    json.dumps(
                        EmailGoogleOAuthPublicStatus(
                            state="dedicated_account_confirmation",
                            disclosure=DISCLOSURE,
                            connected_address_masked=masked,
                        ).model_dump(mode="json", exclude_none=True),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                    household_id,
                ),
            )
        return masked

    def confirm(self, *, household_id: str, account_id: str, dedicated_mailbox: bool) -> None:
        if not dedicated_mailbox:
            raise GoogleOAuthInvalidState("dedicated mailbox confirmation is required")
        now = self.clock()
        with self.jobs.db.write() as connection:
            updated = connection.execute(
                "UPDATE oauth_transactions SET confirmed_at = ? WHERE household_id = ?"
                " AND account_id = ? AND callback_at IS NOT NULL AND confirmed_at IS NULL"
                " AND failed_at IS NULL AND revoked_at IS NULL",
                (now, household_id, account_id),
            )
        if updated.rowcount != 1:
            raise GoogleOAuthInvalidState("no OAuth account awaits confirmation")

    def expire(self) -> int:
        now = self.clock()
        with self.jobs.db.write() as connection:
            return connection.execute(
                "UPDATE oauth_transactions SET failed_at = ? WHERE expires_at <= ?"
                " AND consumed_at IS NULL AND failed_at IS NULL",
                (now, now),
            ).rowcount


class GoogleOAuthProvisioner:
    email_public_provider = "gmail"

    def __init__(
        self,
        service: GoogleOAuthService,
        revoker: RuntimeGrantRevoker | None = None,
        namespace_revoker: SecretNamespaceGrantRevoker | None = None,
    ) -> None:
        self.service = service
        self.revoker = revoker
        self.namespace_revoker = namespace_revoker

    def _row(self, identity_id: str):
        return self.service.jobs.db.query_one(
            "SELECT * FROM oauth_transactions WHERE email_identity_id = ?"
            " AND failed_at IS NULL AND revoked_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1",
            (identity_id,),
        )

    def _result(self, identity_id: str, row) -> ProvisionResult:
        subject = self.service.identities.decrypt_json(
            "oauth_transactions",
            row["id"],
            "provider_subject",
            row["provider_subject_ciphertext"],
            row["encryption_key_version"],
        )
        address = self.service.identities.decrypt_json(
            "oauth_transactions",
            row["id"],
            "address",
            row["address_ciphertext"],
            row["encryption_key_version"],
        )
        return ProvisionResult(
            external_ref=f"google-oauth:{identity_id}",
            public_result={
                "agent_inbox": address,
                "provider": "gmail",
                "provider_subject": subject,
                "provider_refs": {
                    "google_subject": subject,
                },
                "secret_binding_ref": GMAIL_EMAIL_SECRET_BINDING,
                "granted_scopes": list(GMAIL_EMAIL_SCOPES),
                "masked_external_ref": row["credential_digest"][-8:],
                "mode": "gmail_agent",
            },
        )

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        parsed = EmailProvisionIntent.model_validate(intent)
        if parsed.option is not EmailOption.GMAIL:
            raise ProviderRejected("Google OAuth provider received another email option")
        row = self._row(parsed.identity_id)
        if row is not None and row["confirmed_at"] is not None:
            return self._result(parsed.identity_id, row)
        status = EmailGoogleOAuthPublicStatus(
            state=(
                "dedicated_account_confirmation"
                if row is not None and row["callback_at"] is not None
                else "oauth_required"
            ),
            disclosure=DISCLOSURE,
        )
        raise ProviderWaiting(
            "Google OAuth user action is required",
            public_result=status.model_dump(mode="json", exclude_none=True),
            external_ref=f"google-oauth:{parsed.identity_id}",
        )

    def reconcile(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        return self.ensure(intent, idempotency_key)

    def inspect_intent(self, request: dict[str, Any], stable_ref: str) -> InspectResult:
        try:
            result = self.ensure(
                {
                    "identity_id": request["email_identity_id"],
                    "household_id": request["household_id"],
                    "option": request["option"],
                    "selection": request["selection"],
                    "secret_namespace_ref": request["secret_namespace_ref"],
                },
                stable_ref,
            )
        except ProviderWaiting as error:
            return InspectResult(InspectState.PENDING, public_result=error.public_result)
        return InspectResult(InspectState.READY, result)

    def inspect(self, stable_ref: str) -> InspectResult:
        if not stable_ref.startswith("google-oauth:"):
            return InspectResult(InspectState.UNKNOWN)
        identity_id = stable_ref.split(":", 1)[1]
        row = self._row(identity_id)
        if row is None:
            return InspectResult(InspectState.ABSENT)
        if row["confirmed_at"] is None:
            return InspectResult(InspectState.PENDING)
        return InspectResult(InspectState.READY, self._result(identity_id, row))

    def pre_staged_secret_verified(
        self,
        request: dict[str, Any],
        namespace_ref: str,
        binding_ref: str,
    ) -> bool:
        identity_id = request.get("email_identity_id")
        if not isinstance(identity_id, str):
            return False
        row = self._row(identity_id)
        return bool(
            row is not None
            and row["confirmed_at"] is not None
            and row["credential_digest"]
            and row["secret_binding_ref"] == binding_ref == GMAIL_EMAIL_SECRET_BINDING
            and self.service._namespace_ref(row["household_id"]) == namespace_ref
        )

    def deprovision(self, external_ref: str) -> InspectResult:
        if not external_ref.startswith("google-oauth:"):
            raise ProviderRejected("invalid Google OAuth resource reference")
        identity_id = external_ref.split(":", 1)[1]
        row = self._row(identity_id)
        if row is not None:
            runtime = self.service.jobs.db.query_one(
                "SELECT runtime_ref FROM households WHERE id = ?",
                (row["household_id"],),
            )
            if runtime is not None and runtime["runtime_ref"]:
                if self.revoker is None or (
                    self.revoker.revoke_google(runtime["runtime_ref"])
                    is not InspectState.ABSENT
                ):
                    return InspectResult(InspectState.UNKNOWN, error_code="google_revoke_unknown")
            elif self.namespace_revoker is None or (
                self._revoke_before_runtime(row) is not InspectState.ABSENT
            ):
                return InspectResult(
                    InspectState.UNKNOWN,
                    error_code="google_revoke_runtime_unavailable",
                )
            namespace = self.service._namespace_ref(row["household_id"])
            self.service.secret_sink.delete(namespace, GMAIL_EMAIL_SECRET_BINDING)
            with self.service.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE oauth_transactions SET revoked_at = ? WHERE id = ?",
                    (self.service.clock(), row["id"]),
                )
        return InspectResult(InspectState.ABSENT)

    def _revoke_before_runtime(self, row) -> InspectState:
        if row["revoke_completed_at"] is not None:
            return InspectState.ABSENT
        now = self.service.clock()
        with self.service.jobs.db.write() as connection:
            connection.execute(
                "UPDATE oauth_transactions SET"
                " revoke_requested_at = COALESCE(revoke_requested_at, ?) WHERE id = ?",
                (now, row["id"]),
            )
        assert self.namespace_revoker is not None
        namespace = self.service._namespace_ref(row["household_id"])
        state = self.namespace_revoker.revoke_google_secret(namespace, row["id"])
        if state is InspectState.ABSENT:
            with self.service.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE oauth_transactions SET revoke_completed_at = ? WHERE id = ?",
                    (self.service.clock(), row["id"]),
                )
        return state
