from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from control_plane.crypto import SecretMaterial, reject_secret_fields


class ProvisioningError(RuntimeError):
    code = "provider_error"


class ProviderRejected(ProvisioningError):
    code = "provider_rejected"


class ProviderWaiting(ProvisioningError):
    code = "waiting_user"


class ProviderRateLimited(ProvisioningError):
    code = "rate_limited"

    def __init__(self, retry_after: float = 30.0) -> None:
        super().__init__("provider rate limited the request")
        self.retry_after = retry_after


class OutcomeUnknown(ProvisioningError):
    code = "outcome_unknown"


class InspectState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProvisionResult:
    external_ref: str
    public_result: dict[str, Any]
    secret_material: SecretMaterial = field(
        default_factory=SecretMaterial, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        reject_secret_fields(self.public_result)


@dataclass(frozen=True)
class InspectResult:
    state: InspectState
    result: ProvisionResult | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PlannedWrite:
    kind: str
    stable_name: str
    summary: str


class Provisioner(Protocol):
    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult: ...

    def inspect(self, stable_ref: str) -> InspectResult: ...

    def deprovision(self, external_ref: str) -> InspectResult: ...


class SecretSink(Protocol):
    def install(self, runtime_ref: str, material: SecretMaterial) -> None: ...

    def delete(self, runtime_ref: str, name: str) -> None: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provisioner] = {}

    def register(self, name: str, provider: Provisioner) -> None:
        if name in self._providers:
            raise ValueError(f"provider {name!r} is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> Provisioner:
        try:
            return self._providers[name]
        except KeyError as error:
            raise ProviderRejected(f"provider {name!r} is disabled") from error

    def health(self) -> dict[str, str]:
        return {name: "configured" for name in sorted(self._providers)}
