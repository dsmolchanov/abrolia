from __future__ import annotations

from dataclasses import dataclass

from control_plane.models import OnboardingSnapshot


class WorkflowConflict(RuntimeError):
    pass


class IdempotencyConflict(WorkflowConflict):
    pass


class InvalidTransition(WorkflowConflict):
    pass


@dataclass(frozen=True)
class CommandContext:
    account_id: str
    session_id: str
    request_id: str
    idempotency_key: str
    expected_version: int


@dataclass(frozen=True)
class CommandResult:
    snapshot: OnboardingSnapshot
    replayed: bool = False
