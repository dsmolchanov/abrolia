"""Minimal Web channel (Phase E E6): authenticated chat over same pipeline.

PWA manifest installable; Web chat routes through Pipeline/ToolLoop with
server-verified RunContext (channel_bindings) and staged approval.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.observability import emit_alert
from hermes_cloud.core.runcontext import RunContext
from hermes_cloud.core.usage import DEFAULT_DAILY_USD_CAP, DEGRADED_MESSAGE, today_utc


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
    usage: Any | None = None,
    daily_cap_usd: float = DEFAULT_DAILY_USD_CAP,
) -> str:
    """Route authenticated Web message through shared pipeline.

    Caller must have built RunContext from verified channel_bindings — never trust
    client payload for household/role. If a ToolLoop/pipeline is supplied, delegate
    to it; otherwise fallback to capability-checked echo for tests.

    When `usage` is supplied the cost cap applies here exactly as on every other
    model path — check before the call, record after it. The web channel is a
    tenant surface; an uncapped chat would spend outside the budget.
    """
    if not context.is_known:
        return "Я вас не знаю и поэтому ничего не подтверждаю."
    if loop is not None:
        logger_ = logging.getLogger(__name__)
        day = today_utc()
        if usage is not None and usage.is_over_budget(
            context.household_id, day, daily_cap_usd
        ):
            emit_alert(logger_, "budget_exceeded", household_id=context.household_id, day=day)
            return DEGRADED_MESSAGE
        # Delegate to runner/model.ToolLoop — same as Telegram/WhatsApp dialogue
        answer = loop.run(context, message.text)
        if usage is not None:
            try:
                # getattr keeps lightweight test stubs without token counts
                # green; the real ToolLoop always reports both.
                usage.record(
                    context.household_id,
                    day,
                    prompt_tokens=getattr(answer, "input_tokens", 0),
                    completion_tokens=getattr(answer, "output_tokens", 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger_.warning("usage record failed for %s/%s: %s", context.household_id, day, exc)
                raise
        return answer.text if hasattr(answer, "text") else str(answer)
    if pipeline is not None and hasattr(pipeline, "handle_web_event"):
        handled = pipeline.handle_web_event(message, context)
        return handled.message if handled and handled.message else "принято"
    # Fallback for isolated unit tests without model: echo with truncation
    return f"Web: {message.text[:500]}"


# Push subscription is stored as channel_binding; provider disabled until processors.md P11 ✅.
PUSH_ENABLED = False
