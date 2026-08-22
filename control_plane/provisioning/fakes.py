from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    PlannedWrite,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)


def _ref(prefix: str, key: str) -> str:
    return f"synthetic:{prefix}:{hashlib.sha256(key.encode()).hexdigest()[:24]}"


@dataclass
class DeterministicFakeProvisioner:
    kind: str
    behavior: str = "success"
    resources: dict[str, ProvisionResult] = field(default_factory=dict)
    ensure_calls: int = 0
    pending: set[str] = field(default_factory=set)

    @property
    def email_public_provider(self) -> str | None:
        return "synthetic" if self.kind == "email" else None

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        self.ensure_calls += 1
        if idempotency_key in self.resources:
            return self.resources[idempotency_key]
        if self.behavior == "wait":
            self.pending.add(idempotency_key)
            raise ProviderWaiting("synthetic provider waits for user")
        if self.behavior == "reject":
            raise ProviderRejected("synthetic provider rejected the request")
        if self.behavior in {"timeout", "unknown", "crash_after_accept"}:
            # The provider deterministically accepted the stable identity even
            # though the caller did not receive a result. inspect() can recover it.
            result = self._result(intent, idempotency_key)
            self.resources[idempotency_key] = result
            raise OutcomeUnknown("connection ended after synthetic acceptance")
        result = self._result(intent, idempotency_key)
        self.resources[idempotency_key] = result
        return result

    def _result(self, intent: dict[str, Any], key: str) -> ProvisionResult:
        selection = intent.get("selection", {})
        option = selection.get("kind", self.kind)
        external_ref = _ref(self.kind, key)
        if self.kind == "email":
            identity_id = intent.get("identity_id") or intent.get("email_identity_id")
            if not identity_id and ":email_identity:" in key:
                identity_id = key.split(":email_identity:", 1)[1].split(":", 1)[0]
            if not isinstance(identity_id, str) or not identity_id:
                raise ProviderRejected("synthetic email identity is missing")
            external_ref = f"synthetic-email:{identity_id}"
            local = selection.get("local_part", "family.assistant")
            public = {
                "agent_inbox": f"{local}@abrolia.com",
                "provider": "synthetic",
                "provider_refs": {"identity_id": identity_id},
                "mode": option,
                "masked_external_ref": external_ref[-8:],
            }
        elif self.kind == "whatsapp":
            public = {
                "mode": option,
                "verified_member_ref": selection.get(
                    "member_phone_test_ref", selection.get("phone_test_ref", "synthetic-phone:owner")
                ),
                "masked_external_ref": external_ref[-8:],
            }
        elif self.kind == "channel":
            public = {
                "channel": option,
                "actor_id": selection.get("actor_id", "synthetic-owner"),
                "chat_id": selection.get("chat_id", "synthetic-chat"),
                "test_receipt_id": _ref("receipt", key),
            }
        else:
            public = {"mode": option, "masked_external_ref": external_ref[-8:]}
        return ProvisionResult(external_ref=external_ref, public_result=public)

    def inspect(self, stable_ref: str) -> InspectResult:
        if stable_ref in self.pending:
            return InspectResult(InspectState.PENDING)
        result = self.resources.get(stable_ref)
        if result:
            return InspectResult(InspectState.READY, result)
        # Fake stable refs are idempotency keys during reconciliation.
        for key, candidate in self.resources.items():
            if candidate.external_ref == stable_ref or key == stable_ref:
                return InspectResult(InspectState.READY, candidate)
        return InspectResult(InspectState.ABSENT)

    def complete_wait(self, stable_ref: str, intent: dict[str, Any]) -> None:
        if stable_ref not in self.pending:
            raise KeyError(stable_ref)
        self.pending.remove(stable_ref)
        self.resources[stable_ref] = self._result(intent, stable_ref)

    def deprovision(self, external_ref: str) -> InspectResult:
        keys = [key for key, value in self.resources.items() if value.external_ref == external_ref]
        for key in keys:
            self.resources.pop(key, None)
        return InspectResult(InspectState.ABSENT)

    def reconcile(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        """Answer what the synthetic provider already recorded, else provision.

        The worker requires `reconcile` of every email adapter — an adapter
        without it used to fall to a tail that probed with `inspect`. This
        keeps the observable behaviour that tail had for the fake (a recorded
        result is recovered; nothing is re-run that timed out once already)
        while making omission of the method impossible to distinguish on.
        """
        found = self.inspect(idempotency_key)
        if found.state is InspectState.READY and found.result is not None:
            return found.result
        return self.ensure(intent, idempotency_key)


class DryRunRuntimeProvisioner(DeterministicFakeProvisioner):
    def __init__(self) -> None:
        super().__init__("runtime")

    def plan(self, spec: Any) -> list[PlannedWrite]:
        """What this provider really creates: one synthetic reference.

        Without it the rehearsal fell back to describing a Fly app, volume and
        Machine — none of which exist in this configuration, which is the
        allowed default for `ABROLIA_RUNTIME_PROVIDER`. An operator was being
        shown three resources that would never be created, under the name of a
        provider that creates one.
        """
        household_id = (
            spec.household_id
            if hasattr(spec, "household_id")
            else str((spec or {}).get("household_id", ""))
        )
        return [
            PlannedWrite(
                "runtime_reference",
                f"synthetic-runtime:{household_id}",
                "record a synthetic runtime reference; no Fly resources are created",
            )
        ]

    def ensure_secret_namespace(
        self, household_id: str, idempotency_key: str
    ) -> ProvisionResult:
        self.ensure_calls += 1
        existing = self.resources.get(idempotency_key)
        if existing is not None:
            return existing
        result = ProvisionResult(
            external_ref=f"synthetic-runtime:{household_id}",
            public_result={
                "runtime_ref": f"synthetic-runtime:{household_id}",
                "stage": "secret_namespace_ready",
                "planned_writes": ["app"],
            },
        )
        self.resources[idempotency_key] = result
        return result

    def _result(self, intent: dict[str, Any], key: str) -> ProvisionResult:
        household_id = intent["manifest"]["household_id"]
        return ProvisionResult(
            external_ref=f"synthetic-runtime:{household_id}",
            public_result={
                "runtime_ref": f"synthetic-runtime:{household_id}",
                "region": "ams",
                "planned_writes": ["app", "volume", "staged-secrets", "machine"],
            },
        )

    def deprovision_runtime(self, external_ref: Any) -> InspectResult:
        target_ref = (
            external_ref.get("runtime_ref") or external_ref.get("app_ref")
            if isinstance(external_ref, dict)
            else external_ref
        )
        runtime_keys = [
            key
            for key, value in self.resources.items()
            if value.external_ref == target_ref
            and value.public_result.get("stage") != "secret_namespace_ready"
        ]
        for key in runtime_keys:
            self.resources.pop(key, None)
        return InspectResult(InspectState.ABSENT)


def synthetic_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("fake-email", DeterministicFakeProvisioner("email"))
    registry.register("fake-whatsapp", DeterministicFakeProvisioner("whatsapp"))
    registry.register("fake-channel", DeterministicFakeProvisioner("channel"))
    registry.register("fake-cleanup", DeterministicFakeProvisioner("cleanup"))
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    return registry
