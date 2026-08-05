from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from control_plane.crypto import SecretMaterial

SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class SecretInstallError(RuntimeError):
    def __init__(self, message: str = "secret installation failed") -> None:
        super().__init__(message)


class FlySecretSink:
    """Stage Fly secrets over stdin; values never enter argv or exception text."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        fly_binary: str = "fly",
    ) -> None:
        self.runner = runner
        self.fly_binary = fly_binary

    def install(self, runtime_ref: str, material: SecretMaterial) -> None:
        payload = bytearray()
        try:
            for name, value in material.items():
                if not SECRET_NAME.fullmatch(name):
                    raise SecretInstallError("invalid secret name")
                payload.extend(name.encode("ascii"))
                payload.extend(b"=")
                payload.extend(value)
                payload.extend(b"\n")
            command = [
                self.fly_binary,
                "secrets",
                "import",
                "--stage",
                "--app",
                runtime_ref,
            ]
            try:
                completed = self.runner(
                    command,
                    input=bytes(payload),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    shell=False,
                )
            except Exception as error:
                raise SecretInstallError() from error
            if completed.returncode != 0:
                raise SecretInstallError()
        finally:
            payload[:] = b"\x00" * len(payload)
            material.clear()

    def delete(self, runtime_ref: str, name: str) -> None:
        if not SECRET_NAME.fullmatch(name):
            raise SecretInstallError("invalid secret name")
        command = [
            self.fly_binary,
            "secrets",
            "unset",
            name,
            "--app",
            runtime_ref,
        ]
        try:
            completed = self.runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
        except Exception as error:
            raise SecretInstallError() from error
        if completed.returncode != 0:
            raise SecretInstallError()


@dataclass
class InMemorySecretSink:
    _installed: dict[str, dict[str, bytes]] = field(default_factory=dict, repr=False)

    def install(self, runtime_ref: str, material: SecretMaterial) -> None:
        self._installed[runtime_ref] = {
            name: bytes(value) for name, value in material.items()
        }
        material.clear()

    def get(self, runtime_ref: str, name: str) -> bytes | None:
        return self._installed.get(runtime_ref, {}).get(name)

    def delete(self, runtime_ref: str, name: str) -> None:
        values = self._installed.get(runtime_ref)
        if values:
            values.pop(name, None)
