"""Tiny subprocess entrypoint used by process-lock and SIGKILL recovery tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.crypto import SecretMaterial
from control_plane.db import ControlPlaneDatabase, ProcessAlreadyRunning
from control_plane.email.models import SYNTHETIC_EMAIL_SECRET_BINDING
from control_plane.models import StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.contracts import ProviderRegistry, ProvisionResult
from control_plane.provisioning.fakes import DeterministicFakeProvisioner
from control_plane.provisioning.manifest_toml import manifest_to_toml
from hermes_cloud.runtime.bootstrap import (
    ActivationReceipt as RuntimeActivationReceipt,
)
from hermes_cloud.runtime.bootstrap import (
    BootstrapClaim as RuntimeBootstrapClaim,
)
from hermes_cloud.runtime.bootstrap import (
    RuntimeBootstrapper,
    atomic_write,
)

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
EMAIL_SELECTION = {
    "kind": "abrolia_managed",
    "local_part": "chaos-agent",
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000012",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
}
SECRET_CANARY = "-".join(("phase", "b", "secret", "canary", "value"))


def _checkpoint() -> None:
    print("crash-window-open", flush=True)
    while True:
        time.sleep(1)


def _container(database_path: Path) -> ControlPlaneContainer:
    return ControlPlaneContainer.build(
        ControlPlaneConfig.for_test(database_path.parent),
        acquire_process_lock=False,
    )


def _transition(database_path: Path, account_id: str, session_id: str, household_id: str) -> None:
    active = _container(database_path)
    workflow = active.onboarding_repository.workflow_for_household(household_id)
    active.onboarding.select(
        household_id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=CommandContext(
            account_id=account_id,
            session_id=session_id,
            request_id="chaos-transition",
            idempotency_key="chaos-transition",
            expected_version=workflow.version,
        ),
    )
    _checkpoint()


def _projection(database_path: Path) -> None:
    active = _container(database_path)
    paused = False

    def trace(statement: str) -> None:
        nonlocal paused
        if (
            not paused
            and statement.startswith("UPDATE onboarding_steps SET status =")
            and "result_ciphertext" in statement
        ):
            paused = True
            _checkpoint()

    active.database.connection.set_trace_callback(trace)
    active.worker.run_once()
    raise AssertionError("projection failpoint was not reached")


def _installed_names_path(sink_path: Path) -> Path:
    """Where the durable sink fake records the names it holds.

    A separate file from the value: the tests assert the credential bytes at
    `sink_path` directly, and the names are sink metadata, not the secret.
    """
    return sink_path.with_name(sink_path.name + ".names")


def _installed_names(sink_path: Path) -> frozenset[str]:
    path = _installed_names_path(sink_path)
    if not path.is_file():
        return frozenset()
    return frozenset(path.read_text(encoding="utf-8").split())


def _sink_commit(database_path: Path, sink_path: Path) -> None:
    """Pause after durable sink commit but before the worker records its receipt."""

    class CanaryEmailProvisioner(DeterministicFakeProvisioner):
        def _result(self, intent, key):
            result = super()._result(intent, key)
            return ProvisionResult(
                external_ref=result.external_ref,
                public_result={
                    **result.public_result,
                    "secret_binding_ref": SYNTHETIC_EMAIL_SECRET_BINDING,
                },
                secret_material=SecretMaterial.from_mapping({
                    SYNTHETIC_EMAIL_SECRET_BINDING: SECRET_CANARY,
                }),
            )

    class CheckpointFileSink:
        # A sink retains EVERY name it was handed, and the email handoff hands
        # it two in one installation: the credential and the generation marker
        # that proves which provisioning generation installed it. Recording
        # only the credential would model a state the sink cannot be in, and
        # the crash this simulates would then look unconverged for a reason
        # that has nothing to do with the crash.
        def install(self, runtime_ref, material):
            values = {name: bytes(value) for name, value in material.items()}
            sink_path.write_bytes(values[SYNTHETIC_EMAIL_SECRET_BINDING])
            sink_path.chmod(0o600)
            _installed_names_path(sink_path).write_text(
                "\n".join(sorted(values)), encoding="utf-8"
            )
            material.clear()
            _checkpoint()

        def contains(self, runtime_ref, name):
            return sink_path.is_file() and name in _installed_names(sink_path)

        def delete(self, runtime_ref, name):
            sink_path.unlink(missing_ok=True)
            _installed_names_path(sink_path).unlink(missing_ok=True)

    active = _container(database_path)
    providers = ProviderRegistry()
    providers.register("fake-email", CanaryEmailProvisioner("email"))
    active.worker.providers = providers
    active.worker.secret_sink = CheckpointFileSink()
    active.worker.run_once()
    raise AssertionError("sink commit failpoint was not reached")


def _runtime_issued(database_path: Path, token_path: Path) -> None:
    active = _container(database_path)
    result = active.worker.run_once()
    # Launched, not settled. Since C3e the runtime job stays open until the
    # revision activates — which is precisely what the stages after this one
    # go on to drive, and crash in the middle of.
    if result is None or (result.status, result.error_code) != (
        "pending",
        "awaiting_activation",
    ):
        raise AssertionError(f"runtime provisioning did not launch: {result}")
    household = active.database.query_one(
        "SELECT id, runtime_ref FROM households WHERE status = 'provisioning'"
        " ORDER BY created_at DESC LIMIT 1"
    )
    if household is None:
        raise AssertionError("runtime household was not prepared")
    raw = active.secret_sink.get(household["runtime_ref"], "HERMES_BOOTSTRAP_TOKEN")
    if raw is None:
        raise AssertionError("bootstrap token was not staged")
    atomic_write(token_path, raw)
    _checkpoint()


class _DirectBootstrapClient:
    def __init__(
        self,
        active: ControlPlaneContainer,
        *,
        pause_before_activate: bool = False,
        pause_after_activate: bool = False,
    ) -> None:
        self.active = active
        self.pause_before_activate = pause_before_activate
        self.pause_after_activate = pause_after_activate

    def claim(
        self,
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> RuntimeBootstrapClaim:
        result = self.active.bootstrap.claim(
            token,
            household_id=household_id,
            runtime_ref=runtime_ref,
            config_revision=config_revision,
        )
        return RuntimeBootstrapClaim(
            runtime_ref=result.runtime_ref,
            household_id=result.household_id,
            config_revision=result.config_revision,
            config_sha256=result.manifest_sha256,
            manifest_toml=manifest_to_toml(result.manifest),
        )

    def activate(self, token: str, receipt: RuntimeActivationReceipt) -> RuntimeActivationReceipt:
        if self.pause_before_activate:
            _checkpoint()
        result = self.active.bootstrap.activate(
            token,
            household_id=receipt.household_id,
            runtime_ref=receipt.runtime_ref,
            config_revision=receipt.config_revision,
            activated_sha256=receipt.config_sha256,
            email_inbound_check=receipt.email_inbound_check,
            email_outbound_check=receipt.email_outbound_check,
            email_receipt_digest=receipt.email_receipt_digest,
        )
        if self.pause_after_activate:
            _checkpoint()
        return RuntimeActivationReceipt(
            result.runtime_ref,
            result.household_id,
            result.config_revision,
            result.manifest_sha256,
            receipt.email_inbound_check,
            receipt.email_outbound_check,
            receipt.email_receipt_digest,
        )

    def acknowledge(self, token: str, receipt: RuntimeActivationReceipt) -> RuntimeActivationReceipt:
        result = self.active.bootstrap.activate(
            token,
            household_id=receipt.household_id,
            runtime_ref=receipt.runtime_ref,
            config_revision=receipt.config_revision,
            activated_sha256=receipt.config_sha256,
            email_inbound_check=receipt.email_inbound_check,
            email_outbound_check=receipt.email_outbound_check,
            email_receipt_digest=receipt.email_receipt_digest,
            receipt_acknowledged=True,
        )
        return RuntimeActivationReceipt(
            result.runtime_ref,
            result.household_id,
            result.config_revision,
            result.manifest_sha256,
            receipt.email_inbound_check,
            receipt.email_outbound_check,
            receipt.email_receipt_digest,
        )


def _claim(
    database_path: Path,
    token_path: Path,
    household_id: str,
    runtime_ref: str,
    revision: int,
) -> None:
    active = _container(database_path)
    active.bootstrap.claim(
        token_path.read_text(encoding="ascii"),
        household_id=household_id,
        runtime_ref=runtime_ref,
        config_revision=revision,
    )
    _checkpoint()


def _runtime_bootstrap(
    database_path: Path,
    token_path: Path,
    manifest_path: Path,
    activation_path: Path,
    household_id: str,
    runtime_ref: str,
    revision: int,
    mode: str,
) -> None:
    active = _container(database_path)
    config = active.configs.get(household_id, revision)
    client = _DirectBootstrapClient(
        active,
        pause_before_activate=mode == "runtime-write",
        pause_after_activate=mode == "activate-committed",
    )
    RuntimeBootstrapper(
        client,
        runtime_ref=runtime_ref,
        household_id=household_id,
        config_revision=revision,
        manifest_path=manifest_path,
        activation_path=activation_path,
        env={"HERMES_CONFIG_SHA256": config.manifest_sha256},
    ).run(token_path.read_text(encoding="ascii"))
    raise AssertionError("bootstrap failpoint was not reached")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[2] == "transition":
        _transition(Path(sys.argv[1]), sys.argv[3], sys.argv[4], sys.argv[5])
    if len(sys.argv) >= 3 and sys.argv[2] == "projection":
        _projection(Path(sys.argv[1]))
    if len(sys.argv) >= 4 and sys.argv[2] == "sink-commit":
        _sink_commit(Path(sys.argv[1]), Path(sys.argv[3]))
    if len(sys.argv) >= 4 and sys.argv[2] == "runtime-issued":
        _runtime_issued(Path(sys.argv[1]), Path(sys.argv[3]))
    if len(sys.argv) >= 7 and sys.argv[2] == "claim":
        _claim(
            Path(sys.argv[1]),
            Path(sys.argv[3]),
            sys.argv[4],
            sys.argv[5],
            int(sys.argv[6]),
        )
    if len(sys.argv) >= 9 and sys.argv[2] in {"runtime-write", "activate-committed"}:
        _runtime_bootstrap(
            Path(sys.argv[1]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            sys.argv[6],
            sys.argv[7],
            int(sys.argv[8]),
            sys.argv[2],
        )
    if len(sys.argv) >= 4 and sys.argv[2] in {"lease-only", "lease-and-accept"}:
        database = ControlPlaneDatabase(Path(sys.argv[1]))
        try:
            with database.write() as connection:
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'running', leased_by = 'chaos-child',"
                    " lease_until = 1800000001, attempts = attempts + 1, updated_at = 1800000000"
                    " WHERE id = ? AND status = 'pending'",
                    (sys.argv[3],),
                )
            if sys.argv[2] == "lease-and-accept":
                Path(sys.argv[4]).write_text("accepted", encoding="utf-8")
            _checkpoint()
        finally:
            database.close()
    database = ControlPlaneDatabase(Path(sys.argv[1]))
    try:
        database.acquire_process_lock()
    except ProcessAlreadyRunning:
        return 23
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
