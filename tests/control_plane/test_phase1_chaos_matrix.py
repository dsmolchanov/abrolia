"""Real SIGKILL coverage for the Phase 1 control-plane crash windows."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from control_plane.models import StepKind
from control_plane.provisioning.bootstrap import BootstrapService
from control_plane.provisioning.manifest_toml import manifest_to_toml
from hermes_cloud.runtime.bootstrap import (
    ActivationReceipt as RuntimeActivationReceipt,
)
from hermes_cloud.runtime.bootstrap import (
    BootstrapClaim as RuntimeBootstrapClaim,
)
from hermes_cloud.runtime.bootstrap import (
    RuntimeBootstrapper,
    load_activation_state,
)

EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "chaos-agent"}
WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:chaos-owner",
    "privacy_notice_receipt_id": "synthetic-chaos-wa-consent",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-chaos-owner",
    "chat_id": "synthetic-chaos-chat",
}


def _kill_at(cp_stack, mode: str, *arguments: object) -> None:
    helper = Path(__file__).with_name("chaos_child.py")
    project_root = Path(__file__).parents[2]
    child = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(cp_stack.database.path),
            mode,
            *(str(argument) for argument in arguments),
        ],
        cwd=project_root,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(project_root),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "crash-window-open"
        child.kill()
        assert child.wait(timeout=5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _advance_to_runtime(cp_stack) -> None:
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=time.time() + 300)
    for kind, selection in (
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
        )
        assert worker.run_once().status == "succeeded"


class _DirectBootstrapClient:
    def __init__(self, bootstrap: BootstrapService) -> None:
        self.bootstrap = bootstrap

    def claim(
        self,
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> RuntimeBootstrapClaim:
        result = self.bootstrap.claim(
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
        result = self.bootstrap.activate(
            token,
            household_id=receipt.household_id,
            runtime_ref=receipt.runtime_ref,
            config_revision=receipt.config_revision,
            activated_sha256=receipt.config_sha256,
            email_inbound_check=receipt.email_inbound_check,
            email_outbound_check=receipt.email_outbound_check,
            email_receipt_digest=receipt.email_receipt_digest,
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

    def acknowledge(self, token: str, receipt: RuntimeActivationReceipt) -> RuntimeActivationReceipt:
        result = self.bootstrap.activate(
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


def test_sigkill_after_transition_commit_leaves_one_recoverable_job(cp_stack) -> None:
    cp_stack.complete_profile()

    _kill_at(
        cp_stack,
        "transition",
        cp_stack.account.id,
        cp_stack.session.id,
        cp_stack.household.id,
    )

    jobs = cp_stack.database.query(
        "SELECT * FROM provisioning_jobs WHERE household_id = ? AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert len(jobs) == 1 and jobs[0]["status"] == "pending"
    recovered = cp_stack.make_worker(now=time.time() + 300).run_once()
    assert recovered is not None and recovered.status == "succeeded"
    assert (
        cp_stack.database.query_one(
            "SELECT COUNT(*) AS count FROM external_resources"
            " WHERE household_id = ? AND resource_type = 'email_identity'",
            (cp_stack.household.id,),
        )["count"]
        == 1
    )


def test_sigkill_inside_result_projection_rolls_back_the_whole_transaction(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
    )

    _kill_at(cp_stack, "projection")

    step = cp_stack.database.query_one(
        "SELECT status, result_ciphertext FROM onboarding_steps"
        " WHERE workflow_id = ? AND kind = 'email_identity'",
        (cp_stack.onboarding.workflow_for_household(cp_stack.household.id).id,),
    )
    job = cp_stack.database.query_one("SELECT * FROM provisioning_jobs WHERE kind = 'email_identity'")
    assert step["status"] == "provisioning" and step["result_ciphertext"] is None
    assert job["status"] == "running" and job["result_ciphertext"] is None

    recovered = cp_stack.make_worker(now=time.time() + 300).run_once()
    assert recovered is not None and recovered.status == "succeeded"
    assert (
        cp_stack.database.query_one(
            "SELECT COUNT(*) AS count FROM external_resources"
            " WHERE household_id = ? AND resource_type = 'email_identity'",
            (cp_stack.household.id,),
        )["count"]
        == 1
    )


def test_sigkill_matrix_from_config_issue_through_bootstrap_cleanup(
    cp_stack,
    tmp_path: Path,
) -> None:
    _advance_to_runtime(cp_stack)
    token_path = tmp_path / "bootstrap.token"
    manifest_path = tmp_path / "household.toml"
    activation_path = tmp_path / "runtime-activation.json"
    bootstrap = BootstrapService(cp_stack.configs, cp_stack.onboarding, cp_stack.jobs)

    _kill_at(cp_stack, "runtime-issued", token_path)

    household = cp_stack.households.get(cp_stack.household.id)
    revision = cp_stack.configs.get(cp_stack.household.id, 1)
    assert household is not None and household.status == "provisioning"
    assert revision is not None and revision.status == "issued"
    assert token_path.stat().st_mode & 0o777 == 0o600

    _kill_at(
        cp_stack,
        "claim",
        token_path,
        cp_stack.household.id,
        household.runtime_ref,
        revision.revision,
    )

    replayed_claim = bootstrap.claim(
        token_path.read_text(encoding="ascii"),
        household_id=cp_stack.household.id,
        runtime_ref=household.runtime_ref,
        config_revision=revision.revision,
    )
    assert replayed_claim.manifest_sha256 == revision.manifest_sha256

    _kill_at(
        cp_stack,
        "runtime-write",
        token_path,
        manifest_path,
        activation_path,
        cp_stack.household.id,
        household.runtime_ref,
        revision.revision,
    )

    assert manifest_path.is_file() and manifest_path.stat().st_mode & 0o777 == 0o600
    assert load_activation_state(activation_path).status == "activating"
    assert cp_stack.households.get(cp_stack.household.id).status == "provisioning"

    _kill_at(
        cp_stack,
        "activate-committed",
        token_path,
        manifest_path,
        activation_path,
        cp_stack.household.id,
        household.runtime_ref,
        revision.revision,
    )

    assert cp_stack.households.get(cp_stack.household.id).status == "active"
    assert load_activation_state(activation_path).status == "activating"
    assert (
        cp_stack.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs"
            " WHERE household_id = ? AND kind = 'bootstrap_cleanup'",
            (cp_stack.household.id,),
        )["count"]
        == 0
    )

    installed = RuntimeBootstrapper(
        _DirectBootstrapClient(bootstrap),
        runtime_ref=household.runtime_ref,
        household_id=cp_stack.household.id,
        config_revision=revision.revision,
        manifest_path=manifest_path,
        activation_path=activation_path,
        env={"HERMES_CONFIG_SHA256": revision.manifest_sha256},
    ).run(token_path.read_text(encoding="ascii"))
    assert installed.config_sha256 == revision.manifest_sha256
    assert load_activation_state(activation_path).status == "active"

    cleanup = cp_stack.make_worker(now=time.time() + 300).run_once()
    assert cleanup is not None and cleanup.status == "succeeded"
    assert (
        cp_stack.database.query_one(
            "SELECT COUNT(*) AS count FROM provisioning_jobs"
            " WHERE household_id = ? AND kind = 'bootstrap_cleanup'",
            (cp_stack.household.id,),
        )["count"]
        == 1
    )
