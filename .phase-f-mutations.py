"""Mutation checks for the Phase F closure design pass.

Each mutation is a deliberate reintroduction of one defect the new tests
exist to kill. A mutation is KILLED when the named tests FAIL under it and
the source restores byte-identically afterwards; SURVIVED means the pin is
not doing its job.

Run: .venv/bin/python .phase-f-mutations.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTEST = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "-p", "no:cacheprovider"]

WIRING = "tests/control_plane/test_real_email_wiring.py"

MUTATIONS = [
    {
        "name": "M1 erasure skips the disconnect lifecycle",
        "why": "Item 1: cleanup cannot delete the secret, parent never settles",
        "file": "control_plane/privacy/delete.py",
        "old": """connection.execute(
                        "UPDATE email_identities SET status = 'disconnecting',"
                        " version = version + 1, updated_at = ? WHERE household_id = ?"
                        " AND status NOT IN ('disconnecting','deleted')",
                        (now, household_id),
                    )""",
        "new": "pass  # MUTATION M1\n                    ",
        "tests": [
            f"{WIRING}::test_erasure_sequence_settles_every_job_and_completes_deletion",
        ],
    },
    {
        "name": "M2 shutdown routing back below the adapter-shaped gate",
        "why": "Item 2: an adapter without reconcile must not decide teardown",
        "file": "control_plane/provisioning/worker.py",
        "old": """if job.kind == "email_identity" and (
            self._owned_by_deletion(job) or self._is_shutdown_action(job)
        ):""",
        "new": """if False and job.kind == "email_identity" and (
            self._owned_by_deletion(job) or self._is_shutdown_action(job)
        ):""",
        "tests": [
            f"{WIRING}::test_a_quarantined_job_reconciles_without_the_adapters_reconcile_method",
            "tests/control_plane/test_provisioning_jobs.py::test_late_waiting_response_after_cancel_stays_reconcilable",
        ],
    },
    {
        "name": "M3 the unguarded inspect tail returns",
        "why": "Item 2: a kind without a path must be refused, not probed",
        "file": "control_plane/provisioning/worker.py",
        "old": """return self._mark_step_problem(
            job, request, "outcome_unknown", "reconcile_unsupported"
        )""",
        "new": """inspected = provider.inspect(request.get("stable_ref", job.intent_key))  # MUTATION M3
        return self._mark_step_problem(
            job, request, "outcome_unknown", "reconcile_unsupported"
        )""",
        "tests": [
            f"{WIRING}::test_a_kind_without_a_reconcile_path_is_refused_not_probed",
        ],
    },
    {
        "name": "M4 the fake stops honouring reconcile",
        "why": "Item 2: the contract is declared, not discovered",
        "file": "control_plane/provisioning/fakes.py",
        "old": """found = self.inspect(idempotency_key)
        if found.state is InspectState.READY and found.result is not None:
            return found.result
        return self.ensure(intent, idempotency_key)""",
        "new": 'raise AssertionError("MUTATION M4: fake has no reconcile")',
        "tests": [
            "tests/control_plane/test_provisioning_jobs.py::test_reconcile_of_an_absent_unknown_resumes_through_the_adapter",
            "tests/control_plane/test_provisioning_jobs.py::test_reconcile_recovers_accepted_unknown_without_duplicate_resource",
        ],
    },
    {
        "name": "M5 the synthetic teardown reference stops being derivable",
        "why": (
            "Round eleven: an adapter that declares the synthetic contract"
            " must get its derived reference, or ambiguous synthetic jobs"
            " quarantine forever"
        ),
        "file": "control_plane/provisioning/worker.py",
        "old": """        if declared == SYNTHETIC_PUBLIC_EMAIL:
            return f"synthetic-email:{identity_id}"
""",
        "new": """        if declared == "MUTATION M5: the declaration derives nothing":
            return f"synthetic-email:{identity_id}"
""",
        "tests": [
            f"{WIRING}::test_an_ambiguous_synthetic_job_tears_down_the_reference_it_can_derive",
        ],
    },
]


def run_mutation(mutation: dict) -> bool:
    path = ROOT / mutation["file"]
    original = path.read_text(encoding="utf-8")
    count = original.count(mutation["old"])
    if count != 1:
        print(f"  ABORT: target snippet found {count} times, expected 1")
        return False
    try:
        path.write_text(original.replace(mutation["old"], mutation["new"]), encoding="utf-8")
        result = subprocess.run(
            [*PYTEST, *mutation["tests"]],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        killed = result.returncode != 0
        verdict = "KILLED" if killed else "SURVIVED"
        print(f"  {verdict}  ({mutation['why']})")
        return killed
    finally:
        path.write_text(original, encoding="utf-8")


def main() -> int:
    failures = 0
    for mutation in MUTATIONS:
        print(f"{mutation['name']}:")
        if not run_mutation(mutation):
            failures += 1
    print()
    if failures:
        print(f"{failures} mutation(s) SURVIVED — the pins are not doing their job.")
        return 1
    print("All mutations killed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
