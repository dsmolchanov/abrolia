"""Minimal Web channel (Phase E E6): authenticated chat over same pipeline.

PWA manifest installable; Web chat routes through Pipeline/ToolLoop with
server-verified RunContext (channel_bindings) and staged approval.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.runcontext import RunContext


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class WebChannelMessage:
    actor_id: str
    text: str
    endpoint: str | None = None


def handle_web_message(
    message: WebChannelMessage,
    *,
    context: RunContext,
    loop: Any | None = None,
    pipeline: Any | None = None,
) -> str:
    """Route authenticated Web message through shared pipeline.

    Caller must have built RunContext from verified channel_bindings — never trust
    client payload for household/role. If a ToolLoop/pipeline is supplied, delegate
    to it; otherwise fallback to capability-checked echo for tests.
    """
    if not context.is_known:
        return "Я вас не знаю и поэтому ничего не подтверждаю."
    if loop is not None:
        # Delegate to runner/model.ToolLoop — same as Telegram/WhatsApp dialogue
        answer = loop.run(context, message.text)
        return answer.text if hasattr(answer, "text") else str(answer)
    if pipeline is not None and hasattr(pipeline, "handle_web_event"):
        handled = pipeline.handle_web_event(message, context)
        return handled.message if handled and handled.message else "принято"
    # Fallback for isolated unit tests without model: echo with truncation
    return f"Web: {message.text[:500]}"


# Push subscription is stored as channel_binding; provider disabled until processors.md P11 ✅.
PUSH_ENABLED = False
