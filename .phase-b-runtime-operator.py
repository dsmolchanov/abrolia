from __future__ import annotations

import json
import os
import signal
import time
import uuid
from pathlib import Path

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.secrets import FlySecretSink

TOKEN_PATH = Path("/data/.phase-b-retained-bootstrap")


def service_pid() -> int:
    for item in Path("/proc").iterdir():
        if not item.name.isdigit() or int(item.name) == os.getpid():
            continue
        try:
            command = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"abrolia-control-plane serve" in command:
            return int(item.name)
    raise RuntimeError("control-plane service process not found")


class CapturingSink:
    def __init__(self) -> None:
        self.delegate = FlySecretSink()
        self.bootstrap: bytes | None = None

    def install(self, runtime_ref, material) -> None:
        for name, value in material.items():
            if name == "HERMES_BOOTSTRAP_TOKEN":
                self.bootstrap = bytes(value)
        self.delegate.install(runtime_ref, material)

    def contains(self, runtime_ref, name):
        return self.delegate.contains(runtime_ref, name)

    def delete(self, runtime_ref, name):
        return self.delegate.delete(runtime_ref, name)


def main() -> None:
    pid = service_pid()
    os.kill(pid, signal.SIGSTOP)
    active = None
    try:
        active = ControlPlaneContainer.build(
            ControlPlaneConfig.from_env(), acquire_process_lock=False
        )
        stamp = str(int(time.time()))
        email = f"phase-b-runtime-{stamp}@abrolia.test"
        account = active.accounts.create_verified(email)
        household = active.households.create_for_owner(account.id)
        session = active.sessions.issue(account.id)
        sequence = 0

        def context() -> CommandContext:
            nonlocal sequence
            sequence += 1
            workflow = active.onboarding_repository.workflow_for_household(
                household.id
            )
            return CommandContext(
                account_id=account.id,
                session_id=session.id,
                request_id=f"phase-b-runtime-{sequence}",
                idempotency_key=f"phase-b-runtime-{stamp}-{sequence}",
                expected_version=workflow.version,
            )

        active.onboarding.save_profile(
            household.id,
            ProfileInput(
                first_name="Phase",
                last_name="Closure",
                family_language="en",
                timezone="Europe/Prague",
                country_code="CZ",
                residency_mode="eu-app",
            ),
            context=context(),
        )
        namespace = active.worker.run_once()
        if namespace is None or namespace.status != "succeeded":
            raise RuntimeError("secret namespace did not converge")

        version, digest = consent_version_and_sha(
            "special_category_content_restriction"
        )
        selections = (
            (
                StepKind.EMAIL,
                {
                    "kind": "abrolia_managed",
                    "local_part": f"phase-b-{stamp}",
                    "special_category_restriction_acknowledged": True,
                    "special_category_restriction_receipt_id":
                        str(uuid.uuid4()),
                    "special_category_restriction_text_version": version,
                    "special_category_restriction_text_sha256": digest,
                },
            ),
            (
                StepKind.WHATSAPP,
                {
                    "kind": "shared_abrolia",
                    "member_phone_test_ref": f"synthetic-phone:{stamp}",
                    "privacy_notice_receipt_id": f"synthetic-wa-{stamp}",
                },
            ),
            (
                StepKind.PRIMARY_CHANNEL,
                {
                    "kind": "telegram",
                    "actor_id": f"synthetic-actor-{stamp}",
                    "chat_id": f"synthetic-chat-{stamp}",
                },
            ),
        )
        for kind, selection in selections:
            active.onboarding.select(
                household.id, kind, selection, context=context()
            )
            result = active.worker.run_once()
            if result is None or result.status != "succeeded":
                raise RuntimeError(f"{kind.value} did not converge")

        sink = CapturingSink()
        active.worker.secret_sink = sink
        runtime_result = active.worker.run_once()
        if runtime_result is None or runtime_result.status != "succeeded":
            raise RuntimeError("runtime did not converge")
        if sink.bootstrap is None:
            raise RuntimeError("bootstrap token was not captured")
        TOKEN_PATH.write_bytes(sink.bootstrap)
        TOKEN_PATH.chmod(0o600)
        sink.bootstrap = None
        record = active.households.get(household.id)
        revision = active.configs.get(household.id, 1)
        print(json.dumps({
            "account_id": account.id,
            "email": email,
            "household_id": household.id,
            "runtime_ref": record.runtime_ref,
            "config_revision": revision.revision,
            "config_sha256": revision.manifest_sha256,
            "token_file_mode": oct(TOKEN_PATH.stat().st_mode & 0o777),
            "result": "prepared",
        }, sort_keys=True))
    finally:
        if active is not None:
            active.close()
        os.kill(pid, signal.SIGCONT)


if __name__ == "__main__":
    main()
