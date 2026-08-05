"""Private, per-runtime authenticated transport for DSAR export and deletion."""

from __future__ import annotations

import re
from typing import Any

import httpx

from control_plane.crypto import LookupHasher
from control_plane.provisioning.contracts import InspectState

RUNTIME_REF = re.compile(r"^abrolia-hh-[a-z2-7]{26}$")


class RuntimeBoundaryError(RuntimeError):
    """The dedicated runtime boundary could not be proven complete."""


class PrivateRuntimeDsarClient:
    """Call a Fly runtime only through its private ``.internal`` DNS name."""

    def __init__(
        self,
        token_hasher: LookupHasher,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token_hasher = token_hasher
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.timeout = timeout

    def _token(self, runtime_ref: str) -> str:
        if not RUNTIME_REF.fullmatch(runtime_ref):
            raise RuntimeBoundaryError("runtime reference is outside the managed namespace")
        return self.token_hasher.digest(f"runtime-dsar:{runtime_ref}")

    def _request(self, runtime_ref: str, operation: str) -> dict[str, Any]:
        token = self._token(runtime_ref)
        try:
            response = self.client.post(
                f"http://{runtime_ref}.internal:8080/internal/v1/dsar/{operation}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                content=b"{}",
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise RuntimeBoundaryError("runtime boundary outcome is unknown") from error
        if response.status_code != 200:
            raise RuntimeBoundaryError("runtime boundary did not confirm completion")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeBoundaryError("runtime boundary returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeBoundaryError("runtime boundary returned an invalid payload")
        return payload

    def export(self, runtime_ref: str) -> dict[str, Any]:
        payload = self._request(runtime_ref, "export")
        if not isinstance(payload.get("tables"), dict):
            raise RuntimeBoundaryError("runtime export is missing classified tables")
        return payload

    def delete(self, runtime_ref: str) -> InspectState:
        try:
            payload = self._request(runtime_ref, "delete")
        except RuntimeBoundaryError:
            return InspectState.UNKNOWN
        return (
            InspectState.ABSENT
            if payload.get("state") == InspectState.ABSENT.value
            else InspectState.UNKNOWN
        )
