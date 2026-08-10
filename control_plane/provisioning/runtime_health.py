"""Fail-closed readiness reconciliation for dedicated Fly runtimes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from control_plane.db import ControlPlaneDatabase
from control_plane.privacy.runtime import RUNTIME_REF


@dataclass(frozen=True)
class RuntimeHealthResult:
    runtime_ref: str
    status: str


class RuntimeReadinessMonitor:
    """Project a definitive runtime readiness failure into durable CP state.

    Transport failures are inconclusive and leave the previous receipt status
    untouched. A public ``503`` or a mismatched successful response is a
    definitive fail-closed observation and marks the activation receipt
    ``needs_attention``. A matching ``200`` restores it to ``active``.
    """

    def __init__(
        self,
        database: ControlPlaneDatabase,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        maximum_concurrency: int = 4,
    ) -> None:
        self.database = database
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.timeout = timeout
        self.maximum_concurrency = max(1, maximum_concurrency)

    def reconcile_all(self, *, now: float) -> list[RuntimeHealthResult]:
        rows = self.database.query(
            "SELECT h.id AS household_id, h.runtime_ref, h.current_config_revision,"
            " ear.email_identity_id FROM households h"
            " JOIN email_identities ei ON ei.household_id = h.id"
            " JOIN email_activation_receipts ear ON ear.email_identity_id = ei.id"
            " AND ear.desired_revision = h.current_config_revision"
            " WHERE h.status = 'active' AND ei.status IN ('active','needs_attention')"
            " AND ear.status IN ('active','needs_attention')"
            " ORDER BY h.id"
        )
        def observe(row: Any) -> RuntimeHealthResult:
            runtime_ref = str(row["runtime_ref"] or "")
            return RuntimeHealthResult(
                runtime_ref,
                self._observe(
                    runtime_ref,
                    household_id=str(row["household_id"]),
                    revision=int(row["current_config_revision"]),
                ),
            )

        with ThreadPoolExecutor(max_workers=self.maximum_concurrency) as executor:
            results = list(executor.map(observe, rows))
        for row, result in zip(rows, results, strict=True):
            status = result.status
            if status == "unknown":
                continue
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE email_activation_receipts SET status = ?, checked_at = ?"
                    " WHERE email_identity_id = ? AND desired_revision = ?",
                    (
                        status,
                        now,
                        row["email_identity_id"],
                        row["current_config_revision"],
                    ),
                )
                connection.execute(
                    "UPDATE email_identities SET status = ?, version = version + 1,"
                    " updated_at = ? WHERE id = ? AND status != ?",
                    (status, now, row["email_identity_id"], status),
                )
        return results

    def _observe(self, runtime_ref: str, *, household_id: str, revision: int) -> str:
        if not RUNTIME_REF.fullmatch(runtime_ref):
            return "needs_attention"
        try:
            response = self.client.get(
                f"http://{runtime_ref}.internal:8080/readyz",
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return "unknown"
        if response.status_code != 200:
            return "needs_attention"
        try:
            payload: Any = response.json()
        except ValueError:
            return "needs_attention"
        if not isinstance(payload, dict):
            return "needs_attention"
        if (
            payload.get("status") != "ready"
            or payload.get("household_id") != household_id
            or payload.get("config_revision") != revision
        ):
            return "needs_attention"
        return "active"
