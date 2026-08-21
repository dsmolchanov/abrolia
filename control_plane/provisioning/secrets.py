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

    def contains(self, runtime_ref: str, name: str) -> bool:
        if not SECRET_NAME.fullmatch(name):
            return False
        command = [
            self.fly_binary,
            "secrets",
            "list",
            "--app",
            runtime_ref,
            "--json",
        ]
        try:
            completed = self.runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
        except Exception:
            return False
        if completed.returncode != 0:
            return False
        try:
            import json

            data = json.loads(completed.stdout.decode("utf-8") or "[]")
            names = {entry.get("Name") or entry.get("name") for entry in data if isinstance(entry, dict)}
            return name in names
        except Exception:
            return False


@dataclass
class InMemorySecretSink:
    _installed: dict[str, dict[str, bytes]] = field(default_factory=dict, repr=False)

    def install(self, runtime_ref: str, material: SecretMaterial) -> None:
        if runtime_ref not in self._installed:
            self._installed[runtime_ref] = {}
        for name, value in material.items():
            if not SECRET_NAME.fullmatch(name):
                raise SecretInstallError("invalid secret name")
            self._installed[runtime_ref][name] = bytes(value)
        material.clear()

    def get(self, runtime_ref: str, name: str) -> bytes | None:
        return self._installed.get(runtime_ref, {}).get(name)

    def contains(self, runtime_ref: str, name: str) -> bool:
        return name in self._installed.get(runtime_ref, {})

    def delete(self, runtime_ref: str, name: str) -> None:
        values = self._installed.get(runtime_ref)
        if values:
            values.pop(name, None)
