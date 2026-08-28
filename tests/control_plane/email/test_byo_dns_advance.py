"""Phase C2 cases 2-3 — DNS advance and bounded polling, never unbounded."""

from __future__ import annotations

from control_plane.models import StepKind
from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
from control_plane.provisioning.contracts import ProviderRegistry
from control_plane.provisioning.secrets import InMemorySecretSink
from tests.control_plane.email.byo_support import (
    BASE_TIME,
    FakeByoNerveAdmin,
    select_byo_domain,
)


def test_dns_status_polls_automatically_with_bounded_backoff(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()

    waiting = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 100
    ).run_once()
    assert waiting.status == "waiting_user"
    scheduled = cp_stack.database.query_one(
        "SELECT operation, status, attempts, not_before FROM provisioning_jobs"
        " WHERE id = ?",
        (waiting.job_id,),
    )
    assert tuple(scheduled) == ("inspect", "waiting_user", 1, BASE_TIME + 130)
    assert cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 129
    ).run_once() is None

    partial = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 130
    ).run_once()
    assert partial.status == "waiting_user"
    scheduled = cp_stack.database.query_one(
        "SELECT attempts, not_before FROM provisioning_jobs WHERE id = ?",
        (waiting.job_id,),
    )
    assert tuple(scheduled) == (2, BASE_TIME + 190)
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert email_step.public_status["record_status"]["mx"] is False

    client.active = True
    client.checks = {"ownership": True, "mx": True, "spf": True, "dkim": True}
    assert cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 189
    ).run_once() is None
    verified = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 190
    ).run_once()
    assert verified.status == "succeeded"
    assert client.inbox_calls == 1


def test_dns_automatic_polling_stops_after_bounded_attempts(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))

    first = cp_stack.make_worker(providers=registry, now=BASE_TIME + 100).run_once()
    assert first.status == "waiting_user"
    for due_at in (
        BASE_TIME + 130,
        BASE_TIME + 190,
        BASE_TIME + 310,
        BASE_TIME + 610,
        BASE_TIME + 1210,
    ):
        result = cp_stack.make_worker(providers=registry, now=due_at).run_once()
        assert result.status == "waiting_user"

    stopped = cp_stack.database.query_one(
        "SELECT operation, status, attempts, not_before FROM provisioning_jobs"
        " WHERE id = ?",
        (first.job_id,),
    )
    assert tuple(stopped) == ("inspect", "waiting_user", 6, None)
    assert cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 10_000
    ).run_once() is None
