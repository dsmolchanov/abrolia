from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from control_plane.db import new_id
from control_plane.models import (
    OnboardingSnapshot,
    StepKind,
    StepSnapshot,
    StepStatus,
    WorkflowState,
)
from control_plane.repositories.base import Repository


@dataclass(frozen=True)
class WorkflowRecord:
    id: str
    household_id: str
    state: str
    current_step: str
    version: int


class OnboardingRepository(Repository):
    def workflow_for_household(self, household_id: str) -> WorkflowRecord:
        row = self.db.query_one(
            "SELECT * FROM onboarding_workflows WHERE household_id = ?",
            (household_id,),
        )
        if row is None:
            raise KeyError(household_id)
        return WorkflowRecord(
            id=row["id"],
            household_id=row["household_id"],
            state=row["state"],
            current_step=row["current_step"],
            version=row["version"],
        )

    def step_row(self, workflow_id: str, kind: StepKind | str):
        row = self.db.query_one(
            "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
            (workflow_id, str(kind)),
        )
        if row is None:
            raise KeyError((workflow_id, kind))
        return row

    def selection(self, workflow_id: str, kind: StepKind | str) -> Any | None:
        row = self.step_row(workflow_id, kind)
        if row["selection_ciphertext"] is None:
            return None
        return self.decrypt_json(
            "onboarding_steps",
            f"{workflow_id}:{row['kind']}",
            "selection",
            row["selection_ciphertext"],
            row["encryption_key_version"],
        )

    def result(self, workflow_id: str, kind: StepKind | str) -> Any | None:
        row = self.step_row(workflow_id, kind)
        if row["result_ciphertext"] is None:
            return None
        return self.decrypt_json(
            "onboarding_steps",
            f"{workflow_id}:{row['kind']}",
            "result",
            row["result_ciphertext"],
            row["encryption_key_version"],
        )

    def snapshot(self, household_id: str) -> OnboardingSnapshot:
        workflow = self.workflow_for_household(household_id)
        rows = self.db.query(
            "SELECT * FROM onboarding_steps WHERE workflow_id = ? ORDER BY ordinal",
            (workflow.id,),
        )
        steps = tuple(
            StepSnapshot(
                kind=StepKind(row["kind"]),
                ordinal=row["ordinal"],
                status=StepStatus(row["status"]),
                selection_kind=row["selection_kind"],
                public_status=self.parse_public_json(row["public_status_json"]),
                error_code=row["error_code"],
            )
            for row in rows
        )
        return OnboardingSnapshot(
            household_id=household_id,
            workflow_id=workflow.id,
            version=workflow.version,
            state=WorkflowState(workflow.state),
            current_step=StepKind(workflow.current_step),
            steps=steps,
        )

    def append_transition(
        self,
        connection: sqlite3.Connection,
        *,
        workflow: WorkflowRecord,
        new_version: int,
        command: str,
        to_state: str,
        account_id: str,
        session_id: str | None,
        request_id: str,
        step_kind: str | None = None,
        from_step_status: str | None = None,
        to_step_status: str | None = None,
        related_job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> str:
        now = time.time() if now is None else now
        transition_id = new_id()
        connection.execute(
            "INSERT INTO onboarding_transitions (id, workflow_id, workflow_version,"
            " command, from_state, to_state, step_kind, from_step_status, to_step_status,"
            " account_id, session_id, request_id, related_job_id, redacted_metadata_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transition_id,
                workflow.id,
                new_version,
                command,
                workflow.state,
                to_state,
                step_kind,
                from_step_status,
                to_step_status,
                account_id,
                session_id,
                request_id,
                related_job_id,
                self.public_json(metadata or {}),
                now,
            ),
        )
        return transition_id
