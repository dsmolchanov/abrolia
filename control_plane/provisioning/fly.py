"""Idempotent Fly Machines adapter for synthetic Phase 1 runtimes."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from control_plane.config import ControlPlaneConfig
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    PlannedWrite,
    ProviderRateLimited,
    ProviderRejected,
    ProvisionResult,
)
from control_plane.provisioning.manifest import DesiredHouseholdSpecV1, manifest_sha256

FLY_API_BASE = "https://api.machines.dev"
VOLUME_SNAPSHOT_RETENTION_DAYS = 5


class _Conflict(RuntimeError):
    pass


@dataclass(frozen=True)
class _RuntimeTarget:
    app_ref: str
    volume_ref: str | None = None
    machine_ref: str | None = None
    household_id: str | None = None
    config_revision: int | None = None
    config_sha256: str | None = None


class FlyRuntimeProvisioner:
    """Ensure one app, volume, and Machine by deterministic names and metadata."""

    def __init__(
        self,
        *,
        api_token: str,
        org_slug: str,
        image_digest: str,
        bootstrap_url: str,
        region: str = "ams",
        mount_path: str = "/data",
        client: httpx.Client | None = None,
        api_base: str = FLY_API_BASE,
    ) -> None:
        if not api_token or not org_slug:
            raise ValueError("Fly token and organization are required")
        if "@sha256:" not in image_digest:
            raise ValueError("runtime image must use an immutable digest")
        if region != "ams" or mount_path != "/data":
            raise ValueError("Phase 1 Fly placement is locked to ams:/data")
        self._token = api_token
        self.org_slug = org_slug
        self.image_digest = image_digest
        self.bootstrap_url = bootstrap_url.rstrip("/")
        self.region = region
        self.mount_path = mount_path
        self.api_base = api_base.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)

    @classmethod
    def from_config(cls, config: ControlPlaneConfig) -> FlyRuntimeProvisioner:
        assert config.fly_api_token
        assert config.fly_org_slug
        assert config.runtime_image_digest
        assert config.internal_bootstrap_host
        bootstrap_scheme = (
            "http" if config.internal_bootstrap_host.endswith(".flycast") else "https"
        )
        return cls(
            api_token=config.fly_api_token,
            org_slug=config.fly_org_slug,
            image_digest=config.runtime_image_digest,
            bootstrap_url=f"{bootstrap_scheme}://{config.internal_bootstrap_host}",
            region=config.runtime_region,
            mount_path=config.runtime_volume_mount,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        allow_not_found: bool = False,
        allow_conflict: bool = False,
    ) -> Any | None:
        try:
            response = self.client.request(
                method,
                f"{self.api_base}{path}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json=json,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OutcomeUnknown("Fly request outcome is unknown") from error
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code == 409 and allow_conflict:
            raise _Conflict
        if response.status_code == 429:
            raw_retry = response.headers.get("Retry-After", "30")
            try:
                retry_after = max(1.0, float(raw_retry))
            except ValueError:
                retry_after = 30.0
            raise ProviderRateLimited(retry_after)
        if response.status_code >= 500:
            raise OutcomeUnknown("Fly service outcome is unknown")
        if response.status_code in {401, 403, 422}:
            raise ProviderRejected("Fly rejected synthetic runtime configuration")
        if not 200 <= response.status_code < 300:
            raise ProviderRejected(f"Fly request failed with HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise OutcomeUnknown("Fly returned an unreadable response") from error

    @staticmethod
    def stable_app_name(household_id: str) -> str:
        try:
            household_bytes = uuid.UUID(household_id).bytes
        except (AttributeError, ValueError) as error:
            raise ValueError("household_id must be a UUID") from error
        encoded = base64.b32encode(household_bytes).decode("ascii").rstrip("=").lower()
        if len(encoded) != 26:  # Defensive: a UUID is exactly 128 bits.
            raise ValueError("household UUID did not produce a 26-character identity")
        return f"abrolia-hh-{encoded}"

    @staticmethod
    def stable_volume_name() -> str:
        return "abrolia_data"

    @staticmethod
    def stable_machine_name() -> str:
        return "abrolia-runtime"

    @staticmethod
    def _validate_app_ref(app_ref: str) -> str:
        prefix = "abrolia-hh-"
        suffix = app_ref.removeprefix(prefix)
        alphabet = frozenset("abcdefghijklmnopqrstuvwxyz234567")
        if not app_ref.startswith(prefix) or len(suffix) != 26 or not set(suffix) <= alphabet:
            raise ProviderRejected("Fly runtime reference is outside the managed namespace")
        return app_ref

    def plan(self, spec: DesiredHouseholdSpecV1 | dict[str, Any]) -> list[PlannedWrite]:
        parsed = (
            spec if isinstance(spec, DesiredHouseholdSpecV1) else DesiredHouseholdSpecV1.model_validate(spec)
        )
        app = self.stable_app_name(parsed.household_id)
        return [
            PlannedWrite("app", app, "ensure synthetic Fly app"),
            PlannedWrite("volume", self.stable_volume_name(), "ensure encrypted ams volume"),
            PlannedWrite("machine", self.stable_machine_name(), "ensure pinned runtime Machine"),
        ]

    def _get_app(self, app: str) -> dict[str, Any] | None:
        return self._request("GET", f"/v1/apps/{app}", allow_not_found=True)

    def _ensure_app(self, app: str) -> dict[str, Any]:
        current = self._get_app(app)
        if current is not None:
            return current
        try:
            created = self._request(
                "POST",
                "/v1/apps",
                json={"app_name": app, "org_slug": self.org_slug},
                allow_conflict=True,
            )
        except _Conflict:
            created = self._get_app(app)
        if created is None:
            raise OutcomeUnknown("Fly app identity could not be confirmed")
        return created

    def _volumes(self, app: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/v1/apps/{app}/volumes")
        return payload if isinstance(payload, list) else payload.get("volumes", [])

    def _managed_volume(
        self,
        volumes: list[dict[str, Any]],
        *,
        volume_ref: str | None = None,
    ) -> dict[str, Any] | None:
        managed = [
            item
            for item in volumes
            if item.get("name") == self.stable_volume_name()
        ]
        if len(managed) > 1:
            raise ProviderRejected("Fly has conflicting managed volumes")
        if not managed:
            return None
        volume = managed[0]
        if volume_ref is not None and volume.get("id") != volume_ref:
            return None
        return volume

    def _validate_volume_identity(self, volume: Mapping[str, Any]) -> str:
        volume_id = volume.get("id")
        if not isinstance(volume_id, str) or not volume_id:
            raise OutcomeUnknown("Fly volume has no stable identifier")
        if volume.get("name") != self.stable_volume_name():
            raise ProviderRejected("Fly volume name does not match the managed identity")
        if volume.get("region") != self.region:
            raise ProviderRejected("Fly volume region is missing or incorrect")
        if volume.get("encrypted") is not True:
            raise ProviderRejected("Fly volume encryption is missing or disabled")
        return volume_id

    @staticmethod
    def _backup_policy_matches(volume: Mapping[str, Any]) -> bool:
        return (
            volume.get("auto_backup_enabled") is True
            and volume.get("snapshot_retention") == VOLUME_SNAPSHOT_RETENTION_DAYS
        )

    def _reinspect_volume(self, app: str, volume_id: str) -> dict[str, Any]:
        observed = self._managed_volume(self._volumes(app), volume_ref=volume_id)
        if observed is None:
            raise OutcomeUnknown("Fly volume disappeared during policy convergence")
        self._validate_volume_identity(observed)
        if not self._backup_policy_matches(observed):
            raise ProviderRejected("Fly volume backup policy did not converge")
        return observed

    def _converge_volume(self, app: str, volume: dict[str, Any]) -> dict[str, Any]:
        volume_id = self._validate_volume_identity(volume)
        if self._backup_policy_matches(volume):
            return volume
        self._request(
            "PUT",
            f"/v1/apps/{app}/volumes/{volume_id}",
            json={
                "auto_backup_enabled": True,
                "snapshot_retention": VOLUME_SNAPSHOT_RETENTION_DAYS,
            },
        )
        # A successful mutation response only means Fly accepted the request.
        # Re-read authoritative state and fail closed on incomplete drift.
        return self._reinspect_volume(app, volume_id)

    def _ensure_volume(self, app: str) -> dict[str, Any]:
        name = self.stable_volume_name()
        existing = self._managed_volume(self._volumes(app))
        if existing is not None:
            return self._converge_volume(app, existing)
        try:
            created = self._request(
                "POST",
                f"/v1/apps/{app}/volumes",
                json={
                    "name": name,
                    "region": self.region,
                    "size_gb": 1,
                    "encrypted": True,
                    "auto_backup_enabled": True,
                    "snapshot_retention": VOLUME_SNAPSHOT_RETENTION_DAYS,
                },
                allow_conflict=True,
            )
        except _Conflict:
            created = self._managed_volume(self._volumes(app))
        if created is None:
            raise OutcomeUnknown("Fly volume identity could not be confirmed")
        return self._converge_volume(app, created)

    def _machines(self, app: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/v1/apps/{app}/machines")
        return payload if isinstance(payload, list) else payload.get("machines", [])

    def _machine_payload(
        self,
        app: str,
        spec: DesiredHouseholdSpecV1,
        volume: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = {
            "abrolia_managed": "true",
            "abrolia_household": spec.household_id,
            "abrolia_revision": str(spec.config_revision),
            "abrolia_manifest_sha256": spec.config_sha256,
            # Fly release operations (including staged app-secret deployment)
            # select Machines by process group. Keep these stable in the
            # desired config even though the Machine is created through the API.
            "fly_platform_version": "v2",
            "fly_process_group": "app",
        }
        return {
            "name": self.stable_machine_name(),
            "region": self.region,
            "config": {
                "image": self.image_digest,
                "metadata": metadata,
                "env": {
                    "HERMES_HOUSEHOLD_ID": spec.household_id,
                    "HERMES_CONFIG_REVISION": str(spec.config_revision),
                    "HERMES_CONFIG_SHA256": spec.config_sha256,
                    "HERMES_CONTROL_PLANE_URL": self.bootstrap_url,
                    "HERMES_RUNTIME_REF": app,
                    "HERMES_DB": f"{self.mount_path}/hermes.db",
                },
                "mounts": [
                    {"volume": volume["id"], "path": self.mount_path}
                ],
                "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512},
                "restart": {"policy": "on-failure", "max_retries": 3},
            },
        }

    def _ensure_machine(
        self, app: str, spec: DesiredHouseholdSpecV1, volume: dict[str, Any]
    ) -> dict[str, Any]:
        desired = self._machine_payload(app, spec, volume)
        existing = next(
            (item for item in self._machines(app) if item.get("name") == self.stable_machine_name()),
            None,
        )
        if existing is not None:
            if self._machine_matches(existing, desired):
                return existing
            machine_id = existing.get("id")
            if not machine_id:
                raise OutcomeUnknown("Fly Machine has no stable identifier")
            try:
                updated = self._request(
                    "POST",
                    f"/v1/apps/{app}/machines/{machine_id}",
                    json=desired,
                    allow_conflict=True,
                )
            except _Conflict:
                updated = next(
                    (item for item in self._machines(app) if item.get("id") == machine_id), None
                )
            if updated is None:
                raise OutcomeUnknown("Fly Machine update could not be confirmed")
            if not self._machine_matches(updated, desired):
                raise OutcomeUnknown("Fly Machine update did not reach desired metadata")
            return updated
        try:
            created = self._request(
                "POST",
                f"/v1/apps/{app}/machines",
                json=desired,
                allow_conflict=True,
            )
        except _Conflict:
            created = next(
                (
                    item
                    for item in self._machines(app)
                    if item.get("name") == self.stable_machine_name()
                ),
                None,
            )
        if created is None:
            raise OutcomeUnknown("Fly Machine identity could not be confirmed")
        if not self._machine_matches(created, desired):
            raise OutcomeUnknown("Fly Machine creation did not reach desired metadata")
        return created

    @staticmethod
    def _mounts_match(actual: Any, desired: Any) -> bool:
        if not isinstance(actual, list) or not isinstance(desired, list):
            return False
        if len(actual) != len(desired):
            return False
        return all(
            isinstance(observed, Mapping)
            and isinstance(expected, Mapping)
            and observed.get("volume") == expected.get("volume")
            and observed.get("path") == expected.get("path")
            for observed, expected in zip(actual, desired, strict=True)
        )

    @staticmethod
    def _machine_matches(
        machine: Mapping[str, Any], desired: Mapping[str, Any]
    ) -> bool:
        actual_config = machine.get("config", {})
        desired_config = desired["config"]
        expected_env = desired_config["env"]
        actual_env = actual_config.get("env", {})
        expected_metadata = desired_config["metadata"]
        actual_metadata = actual_config.get("metadata", {})
        return (
            machine.get("name") == desired.get("name")
            and machine.get("region") in {None, desired.get("region")}
            and actual_config.get("image") == desired_config["image"]
            and all(
                actual_metadata.get(key) == value
                for key, value in expected_metadata.items()
            )
            and all(actual_env.get(key) == value for key, value in expected_env.items())
            and FlyRuntimeProvisioner._mounts_match(
                actual_config.get("mounts"), desired_config["mounts"]
            )
            and actual_config.get("guest") == desired_config["guest"]
        )

    @staticmethod
    def _spec(intent: Mapping[str, Any]) -> DesiredHouseholdSpecV1:
        spec = DesiredHouseholdSpecV1.model_validate(intent["manifest"])
        digest = manifest_sha256(spec)
        if not spec.config_sha256:
            spec = spec.model_copy(update={"config_sha256": digest})
        return spec

    @staticmethod
    def _public_payload(
        value: ProvisionResult | Mapping[str, Any],
    ) -> dict[str, Any]:
        if isinstance(value, ProvisionResult):
            return {"external_ref": value.external_ref, **value.public_result}
        payload = dict(value)
        nested = payload.get("public_result")
        if isinstance(nested, Mapping):
            return {**payload, **dict(nested)}
        return payload

    def _target(
        self, value: str | ProvisionResult | Mapping[str, Any]
    ) -> _RuntimeTarget:
        if isinstance(value, Mapping) and "manifest" in value:
            spec = self._spec(value)
            return _RuntimeTarget(
                app_ref=self.stable_app_name(spec.household_id),
                household_id=spec.household_id,
                config_revision=spec.config_revision,
                config_sha256=spec.config_sha256,
            )
        if isinstance(value, str):
            return _RuntimeTarget(app_ref=self._validate_app_ref(value))
        payload = self._public_payload(value)
        app_ref = payload.get("app_ref") or payload.get("runtime_ref") or payload.get(
            "external_ref"
        )
        if not isinstance(app_ref, str) or not app_ref:
            raise ProviderRejected("Fly runtime reference has no app identifier")
        raw_revision = payload.get("config_revision")
        try:
            revision = int(raw_revision) if raw_revision is not None else None
        except (TypeError, ValueError) as error:
            raise ProviderRejected("Fly runtime reference has an invalid revision") from error
        return _RuntimeTarget(
            app_ref=self._validate_app_ref(app_ref),
            volume_ref=(
                str(payload["volume_ref"]) if payload.get("volume_ref") else None
            ),
            machine_ref=(
                str(payload["machine_ref"]) if payload.get("machine_ref") else None
            ),
            household_id=(
                str(payload["household_id"]) if payload.get("household_id") else None
            ),
            config_revision=revision,
            config_sha256=(
                str(payload["config_sha256"])
                if payload.get("config_sha256")
                else None
            ),
        )

    @staticmethod
    def _result(
        spec: DesiredHouseholdSpecV1,
        app_name: str,
        volume: Mapping[str, Any],
        machine: Mapping[str, Any] | None = None,
    ) -> ProvisionResult:
        public_result: dict[str, Any] = {
            "runtime_ref": app_name,
            "app_ref": app_name,
            "volume_ref": str(volume.get("id", "")),
            "region": "ams",
            "household_id": spec.household_id,
            "config_revision": spec.config_revision,
            "config_sha256": spec.config_sha256,
        }
        if machine is not None:
            public_result["machine_ref"] = str(machine.get("id", ""))
            public_result["stage"] = "launched"
        else:
            public_result["stage"] = "prepared"
        return ProvisionResult(external_ref=app_name, public_result=public_result)

    def prepare(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        """Ensure app and volume, intentionally stopping before Machine launch."""

        del idempotency_key  # Stable names are the idempotency boundary.
        spec = self._spec(intent)
        app_name = self.stable_app_name(spec.household_id)
        self._ensure_app(app_name)
        volume = self._ensure_volume(app_name)
        return self._result(spec, app_name, volume)

    def ensure_secret_namespace(
        self, household_id: str, idempotency_key: str
    ) -> ProvisionResult:
        """Ensure only the deterministic Fly app used as the secret namespace."""

        del idempotency_key
        app_name = self.stable_app_name(household_id)
        self._ensure_app(app_name)
        return ProvisionResult(
            external_ref=app_name,
            public_result={
                "runtime_ref": app_name,
                "app_ref": app_name,
                "household_id": household_id,
                "region": self.region,
                "stage": "secret_namespace_ready",
            },
        )

    def launch(
        self,
        intent: dict[str, Any],
        prepared: ProvisionResult | Mapping[str, Any],
        idempotency_key: str,
    ) -> ProvisionResult:
        """Launch only after the caller has staged secrets into the prepared app."""

        del idempotency_key
        spec = self._spec(intent)
        target = self._target(prepared)
        app_name = self.stable_app_name(spec.household_id)
        if target.app_ref != app_name:
            raise ProviderRejected("prepared Fly app does not match the household")
        if self._get_app(app_name) is None:
            raise OutcomeUnknown("prepared Fly app disappeared before Machine launch")
        volumes = self._volumes(app_name)
        volume = self._managed_volume(volumes, volume_ref=target.volume_ref)
        if volume is None:
            raise OutcomeUnknown("prepared Fly volume disappeared before Machine launch")
        volume = self._converge_volume(app_name, volume)
        machine = self._ensure_machine(app_name, spec, volume)
        return self._result(spec, app_name, volume, machine)

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        """Compatibility helper; two-phase callers must use prepare then launch."""

        prepared = self.prepare(intent, idempotency_key)
        return self.launch(intent, prepared, idempotency_key)

    def inspect(
        self, intent_or_ref: str | ProvisionResult | Mapping[str, Any]
    ) -> InspectResult:
        desired_spec = None
        if isinstance(intent_or_ref, Mapping) and "manifest" in intent_or_ref:
            try:
                desired_spec = self._spec(intent_or_ref)
            except (ValueError, ProviderRejected):
                return InspectResult(
                    InspectState.FAILED, error_code="fly_manifest_rejected"
                )
        try:
            target = self._target(intent_or_ref)
        except (ValueError, ProviderRejected):
            return InspectResult(InspectState.FAILED, error_code="fly_reference_rejected")
        try:
            app = self._get_app(target.app_ref)
            if app is None:
                return InspectResult(InspectState.ABSENT)
            volumes = self._volumes(target.app_ref)
            machines = self._machines(target.app_ref)
        except (OutcomeUnknown, ProviderRateLimited):
            return InspectResult(InspectState.UNKNOWN, error_code="fly_inspect_unknown")
        except ProviderRejected:
            return InspectResult(InspectState.FAILED, error_code="fly_inspect_rejected")
        try:
            volume = self._managed_volume(volumes, volume_ref=target.volume_ref)
            if volume is None:
                return InspectResult(
                    InspectState.FAILED,
                    error_code="fly_managed_volume_missing",
                )
            volume_id = self._validate_volume_identity(volume)
            if not self._backup_policy_matches(volume):
                return InspectResult(
                    InspectState.FAILED,
                    error_code="fly_volume_backup_policy_mismatch",
                )
        except ProviderRejected:
            return InspectResult(
                InspectState.FAILED,
                error_code="fly_volume_policy_mismatch",
            )
        except OutcomeUnknown:
            return InspectResult(
                InspectState.UNKNOWN,
                error_code="fly_volume_inspect_unknown",
            )
        if not machines:
            return InspectResult(InspectState.PENDING)
        if target.machine_ref:
            machine = next(
                (item for item in machines if item.get("id") == target.machine_ref), None
            )
        else:
            machine = next(
                (
                    item
                    for item in machines
                    if item.get("name") == self.stable_machine_name()
                ),
                None,
            )
        if machine is None:
            return InspectResult(InspectState.PENDING)
        metadata = machine.get("config", {}).get("metadata", {})
        raw_revision = metadata.get("abrolia_revision")
        raw_hash = metadata.get("abrolia_manifest_sha256")
        raw_household = metadata.get("abrolia_household")
        try:
            observed_revision = int(raw_revision)
            uuid.UUID(str(raw_household))
        except (TypeError, ValueError, AttributeError):
            return InspectResult(
                InspectState.FAILED, error_code="fly_machine_metadata_mismatch"
            )
        if (
            metadata.get("abrolia_managed") != "true"
            or observed_revision <= 0
            or not isinstance(raw_hash, str)
            or len(raw_hash) != 64
            or not set(raw_hash) <= set("0123456789abcdef")
        ):
            return InspectResult(
                InspectState.FAILED, error_code="fly_machine_metadata_mismatch"
            )
        if not self._mounts_match(
            machine.get("config", {}).get("mounts"),
            [{"volume": volume_id, "path": self.mount_path}],
        ):
            return InspectResult(
                InspectState.FAILED,
                error_code="fly_machine_volume_mismatch",
            )
        expected_metadata = {
            "abrolia_household": target.household_id,
            "abrolia_revision": (
                str(target.config_revision)
                if target.config_revision is not None
                else None
            ),
            "abrolia_manifest_sha256": target.config_sha256,
        }
        if any(
            expected is not None and metadata.get(key) != expected
            for key, expected in expected_metadata.items()
        ):
            return InspectResult(
                InspectState.FAILED, error_code="fly_machine_metadata_mismatch"
            )
        if desired_spec is not None:
            desired = self._machine_payload(target.app_ref, desired_spec, volume)
            if not self._machine_matches(machine, desired):
                # Exact identity metadata is valid, but mutable config drifted.
                # Reconcile may converge this Machine; it must not settle the job
                # merely because its stable name exists.
                return InspectResult(
                    InspectState.PENDING,
                    error_code="fly_machine_config_drift",
                )
        state = str(machine.get("state", "")).lower()
        if state in {"failed", "destroyed"}:
            return InspectResult(InspectState.FAILED, error_code="fly_machine_failed")
        if state in {"started", "running"}:
            return InspectResult(
                InspectState.READY,
                ProvisionResult(
                    external_ref=target.app_ref,
                    public_result={
                        "runtime_ref": target.app_ref,
                        "app_ref": target.app_ref,
                        "volume_ref": target.volume_ref
                        or str(
                            next(
                                iter(machine.get("config", {}).get("mounts", [])), {}
                            ).get("volume", "")
                        ),
                        "machine_ref": str(machine.get("id", "")),
                        "region": self.region,
                        "household_id": str(raw_household),
                        "config_revision": observed_revision,
                        "config_sha256": raw_hash,
                        "stage": "launched",
                    },
                ),
            )
        return InspectResult(InspectState.PENDING)

    def deprovision(
        self, exact_ref: str | ProvisionResult | Mapping[str, Any]
    ) -> InspectResult:
        try:
            target = self._target(exact_ref)
        except (ValueError, ProviderRejected):
            return InspectResult(InspectState.FAILED, error_code="fly_reference_rejected")
        try:
            if self._get_app(target.app_ref) is None:
                return InspectResult(InspectState.ABSENT)
            machines = self._machines(target.app_ref)
            owned_machines = [
                item
                for item in machines
                if (
                    item.get("id") == target.machine_ref
                    if target.machine_ref
                    else item.get("name") == self.stable_machine_name()
                )
            ]
            if target.machine_ref and not owned_machines and machines:
                return InspectResult(
                    InspectState.FAILED, error_code="fly_exact_machine_mismatch"
                )
            for machine in owned_machines:
                machine_id = machine.get("id")
                if not machine_id:
                    return InspectResult(
                        InspectState.UNKNOWN, error_code="fly_machine_id_unknown"
                    )
                self._request(
                    "DELETE",
                    f"/v1/apps/{target.app_ref}/machines/{machine_id}?force=true",
                    allow_not_found=True,
                )
            if self._get_app(target.app_ref) is None:
                return InspectResult(InspectState.ABSENT)
            remaining_machines = self._machines(target.app_ref)
            if owned_machines and any(
                item.get("id") in {owned.get("id") for owned in owned_machines}
                for item in remaining_machines
            ):
                return InspectResult(
                    InspectState.PENDING, error_code="fly_delete_pending"
                )
            if remaining_machines:
                return InspectResult(
                    InspectState.FAILED, error_code="fly_unrecorded_machine_present"
                )

            volumes = self._volumes(target.app_ref)
            owned_volumes = [
                item
                for item in volumes
                if (
                    item.get("id") == target.volume_ref
                    if target.volume_ref
                    else item.get("name") == self.stable_volume_name()
                )
            ]
            if target.volume_ref and not owned_volumes and volumes:
                return InspectResult(
                    InspectState.FAILED, error_code="fly_exact_volume_mismatch"
                )
            for volume in owned_volumes:
                volume_id = volume.get("id")
                if not volume_id:
                    return InspectResult(
                        InspectState.UNKNOWN, error_code="fly_volume_id_unknown"
                    )
                self._request(
                    "DELETE",
                    f"/v1/apps/{target.app_ref}/volumes/{volume_id}",
                    allow_not_found=True,
                )
            if self._get_app(target.app_ref) is None:
                return InspectResult(InspectState.ABSENT)
            remaining_volumes = self._volumes(target.app_ref)
            if owned_volumes and any(
                item.get("id") in {owned.get("id") for owned in owned_volumes}
                for item in remaining_volumes
            ):
                return InspectResult(
                    InspectState.PENDING, error_code="fly_delete_pending"
                )
            if remaining_volumes:
                return InspectResult(
                    InspectState.FAILED, error_code="fly_unrecorded_volume_present"
                )

            self._request(
                "DELETE", f"/v1/apps/{target.app_ref}", allow_not_found=True
            )
            if self._get_app(target.app_ref) is None:
                return InspectResult(InspectState.ABSENT)
            return InspectResult(InspectState.PENDING, error_code="fly_delete_pending")
        except (OutcomeUnknown, ProviderRateLimited):
            return InspectResult(InspectState.UNKNOWN, error_code="fly_delete_unknown")
        except ProviderRejected:
            return InspectResult(InspectState.FAILED, error_code="fly_delete_rejected")
