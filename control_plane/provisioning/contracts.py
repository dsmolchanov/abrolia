from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from control_plane.crypto import SecretMaterial, reject_secret_fields

_LOCAL_PROVIDER_ERROR_CODES = frozenset({
    "credential_recovery_unknown",
    "fly_delete_pending",
    "fly_delete_rejected",
    "fly_delete_unknown",
    "fly_exact_machine_mismatch",
    "fly_exact_volume_mismatch",
    "fly_inspect_rejected",
    "fly_inspect_unknown",
    "fly_machine_config_drift",
    "fly_machine_failed",
    "fly_machine_id_unknown",
    "fly_machine_metadata_mismatch",
    "fly_machine_volume_mismatch",
    "fly_managed_volume_missing",
    "fly_manifest_rejected",
    "fly_reference_rejected",
    "fly_unrecorded_machine_present",
    "fly_unrecorded_volume_present",
    "fly_volume_backup_policy_mismatch",
    "fly_volume_id_unknown",
    "fly_volume_inspect_unknown",
    "fly_volume_policy_mismatch",
    "provider_absent",
    "provider_rejected",
})


class ProvisioningError(RuntimeError):
    code = "provider_error"


class ProviderRejected(ProvisioningError):
    code = "provider_rejected"


class ProviderWaiting(ProvisioningError):
    code = "waiting_user"

    def __init__(
        self,
        message: str = "provider is waiting for user action",
        *,
        public_result: dict[str, Any] | None = None,
        external_ref: str | None = None,
    ) -> None:
        super().__init__(message)
        self.public_result = public_result or {}
        self.external_ref = external_ref
        reject_secret_fields(self.public_result)
        if external_ref is not None:
            reject_secret_fields({"external_ref": external_ref})


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
        reject_secret_fields({"external_ref": self.external_ref})
        reject_secret_fields(self.public_result)


@dataclass(frozen=True)
class InspectResult:
    state: InspectState
    result: ProvisionResult | None = None
    error_code: str | None = None
    public_result: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reject_secret_fields(self.public_result)
        if self.error_code is not None and self.error_code not in _LOCAL_PROVIDER_ERROR_CODES:
            object.__setattr__(self, "error_code", "provider_rejected")


@dataclass(frozen=True)
class PlannedWrite:
    kind: str
    stable_name: str
    summary: str


class Provisioner(Protocol):
    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult: ...

    def inspect(self, stable_ref: str) -> InspectResult: ...

    def reconcile(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        """Resume an uncertain provision. The worker's only forward re-entry.

        Declared because `reconcile` is a CONTRACT, not an optional attribute
        the worker discovers with `getattr`: an adapter that omits it used to
        fall to a reconcile tail whose first act was `inspect` — and on the
        email adapters `inspect` is a recovery path that mutates provider
        state (Nerve reissues the API key and rotates the webhook; Google
        OAuth calls `ensure`). Shutdown work must never reach it.
        """

    def deprovision(self, external_ref: str) -> InspectResult: ...


class RuntimeProvisioner(Provisioner, Protocol):
    """Runtime adapter with an app-only namespace stage before activation."""

    def ensure_secret_namespace(
        self, household_id: str, idempotency_key: str
    ) -> ProvisionResult: ...

    def prepare(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult: ...

    def launch(
        self,
        intent: dict[str, Any],
        prepared: ProvisionResult,
        idempotency_key: str,
    ) -> ProvisionResult: ...

    def deprovision_runtime(self, external_ref: Any) -> InspectResult:
        """Remove runtime workload while preserving its secret namespace."""
        ...


class SecretSink(Protocol):
    def install(self, runtime_ref: str, material: SecretMaterial) -> None: ...

    def delete(self, runtime_ref: str, name: str) -> None: ...

    def contains(self, runtime_ref: str, name: str) -> bool: ...


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
