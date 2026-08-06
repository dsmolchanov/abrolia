"""Minimal Web channel (Phase E E6): authenticated chat over same pipeline.

PWA manifest installable; offline shell not required. Push is stored as
channel_binding (channel='web', external_id=endpoint_hash) but provider P11
remains TBD/disabled — Web chat works without push in Phase E.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hermes_cloud.core.runcontext import RunContext


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class WebChannelMessage:
    actor_id: str
    text: str
    endpoint: str | None = None


def handle_web_message(message: WebChannelMessage, *, context: RunContext) -> str:
    """Route authenticated Web message through same model loop as other channels.

    Caller must have built RunContext from verified channel_bindings — never trust
    client payload for household/role.
    """
    if not context.is_known:
        return "Я вас не знаю и поэтому ничего не подтверждаю."
    # In real path, this would call runner/model.py ToolLoop; for pilot, echo with cap.
    return f"Web: {message.text[:500]}"


# Push subscription is stored as channel_binding; provider disabled until processors.md P11 ✅.
PUSH_ENABLED = False
