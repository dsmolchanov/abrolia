"""Local onboarding choices, with no simulated external connection.

The worker durably records these choices and the planner creates the web seat
under the household's authenticated owner. There is no upstream resource: a
replay can recompute the same result, and teardown has nothing external to delete.
"""
from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from control_plane.models import web_channel_identity
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRegistry,
    ProviderRejected,
    ProvisionResult,
)


def local_configuration_ref(intent_key: str) -> str:
    return "local-configuration:" + hashlib.sha256(intent_key.encode()).hexdigest()


class LocalConfigurationProvisioner:
    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        selection = intent.get("selection", {})
        kind = intent.get("step_kind")
        try:
            household_id = str(UUID(idempotency_key.split(":", 1)[0]))
        except (ValueError, AttributeError) as error:
            raise ProviderRejected("local configuration requires a household") from error
        if kind == "whatsapp_identity" and selection == {"kind": "disabled"}:
            public = {"mode": "disabled", "connected": False}
        elif kind == "primary_channel" and selection.get("kind") == "web":
            actor_id, chat_id = web_channel_identity(household_id)
            if (selection.get("actor_id"), selection.get("chat_id")) != (actor_id, chat_id):
                raise ProviderRejected("web identity does not belong to this household")
            public = {"channel": "web", "actor_id": actor_id, "chat_id": chat_id}
        else:
            raise ProviderRejected("unsupported local configuration")
        return ProvisionResult(local_configuration_ref(idempotency_key), public)

    def reconcile(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        return self.ensure(intent, idempotency_key)

    def inspect(self, stable_ref: str) -> InspectResult:
        # Local configuration is held by the workflow, never an upstream service.
        return InspectResult(InspectState.ABSENT)

    def deprovision(self, external_ref: str) -> InspectResult:
        return InspectResult(InspectState.ABSENT)


class RetiredSyntheticProvisioner:
    """Resolve old job names for cleanup without ever fabricating new results."""

    email_public_provider = "synthetic"

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        raise ProviderRejected("synthetic provisioning is disabled in production")

    def reconcile(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        raise ProviderRejected("synthetic provisioning is disabled in production")

    def inspect(self, stable_ref: str) -> InspectResult:
        return InspectResult(InspectState.ABSENT)

    def deprovision(self, external_ref: str) -> InspectResult:
        if not external_ref.startswith(("synthetic:", "synthetic-")):
            raise ProviderRejected("not a synthetic resource")
        return InspectResult(InspectState.ABSENT)

    def deprovision_runtime(self, external_ref: Any) -> InspectResult:
        target = (external_ref.get("runtime_ref") or external_ref.get("app_ref")) if isinstance(
            external_ref, dict
        ) else external_ref
        if not isinstance(target, str):
            raise ProviderRejected("not a synthetic resource")
        return self.deprovision(target)


def production_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for name in ("fake-email", "fake-whatsapp", "fake-channel", "fake-cleanup", "dry-run-runtime"):
        registry.register(name, RetiredSyntheticProvisioner())
    return registry
