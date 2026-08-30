"""Authenticated private transport for one web-chat turn to a household runtime.

The control plane stays metadata-only: message text crosses this client in
memory on its way to the household's dedicated runtime and is neither stored
nor logged here. The model call itself — and the usage counter it must obey —
live inside the runtime, where the household's own SQLite holds the budget
state; proxying from here would otherwise bypass the cap C1 put on every
other channel.
"""

from __future__ import annotations

from typing import Any

import httpx

from control_plane.crypto import LookupHasher
from control_plane.privacy.runtime import RUNTIME_REF, RuntimeBoundaryError


class PrivateRuntimeWebChatClient:
    """Proxy a web-chat turn only through the runtime's ``.internal`` DNS name.

    The bearer credential is the same per-runtime secret the DSAR client uses
    (``runtime-dsar:{ref}``), so the runtime authenticates every internal
    route through its single hoisted gate rather than per-handler copies.
    """

    def __init__(
        self,
        token_hasher: LookupHasher,
        *,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.token_hasher = token_hasher
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.timeout = timeout

    def send(
        self,
        runtime_ref: str,
        *,
        actor_id: str,
        chat_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Forward one turn as TRUSTED PROVENANCE: who, and in which room.

        C3f replaced `role` with `chat_id` here, and the swap is the point
        rather than a rename. A role is a CONCLUSION, and sending it meant the
        runtime believed a claim this side computed — the same shape that once
        let a failed membership lookup hand the caller `owner`. The runtime now
        derives the role from its own manifest, and what travels is the pair
        the manifest can check: the member's actor and the conversation they
        hold, both read from a published binding rather than from the request.
        """
        if not RUNTIME_REF.fullmatch(runtime_ref):
            raise RuntimeBoundaryError("runtime reference is outside the managed namespace")
        token = self.token_hasher.digest(f"runtime-dsar:{runtime_ref}")
        try:
            response = self.client.post(
                f"http://{runtime_ref}.internal:8080/internal/v1/web/chat",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"text": text, "actor_id": actor_id, "chat_id": chat_id},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise RuntimeBoundaryError("runtime boundary outcome is unknown") from error
        if response.status_code != 200:
            raise RuntimeBoundaryError("runtime did not answer the chat turn")
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeBoundaryError("runtime boundary returned invalid JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("reply"), str):
            raise RuntimeBoundaryError("runtime boundary returned an invalid chat reply")
        return payload
