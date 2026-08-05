from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from control_plane.email.models import EmailProvisionIntent
from control_plane.provisioning.contracts import InspectResult, ProvisionResult


class EmailFailureKind(StrEnum):
    USER_ACTION = "user_action"
    SAFE_RETRY = "safe_retry"
    DEFINITIVE_FAILURE = "definitive_failure"
    AUTH_REVOKED = "auth_revoked"
    PROVIDER_DEGRADED = "provider_degraded"
    OUTCOME_UNKNOWN = "outcome_unknown"


class EmailProviderError(RuntimeError):
    def __init__(self, kind: EmailFailureKind, code: str) -> None:
        super().__init__(code)
        self.kind = kind
        self.code = code


class EmailIdentityProvisioner(Protocol):
    def ensure(
        self, intent: EmailProvisionIntent | dict, idempotency_key: str
    ) -> ProvisionResult: ...

    def inspect(self, stable_ref: str) -> InspectResult: ...

    def deprovision(self, external_ref: str) -> InspectResult: ...
