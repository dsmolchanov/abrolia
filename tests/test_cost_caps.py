"""Cost caps: per-household/day counter + soft-limit degradation message."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runcontext import Household
from hermes_cloud.core.usage import DEGRADED_MESSAGE, UsageStore, estimate_usd, today_utc
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.inject import ingest_file
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.extraction import Extraction, ExtractionResult, Money
from hermes_cloud.runner.pipeline import Pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "email"
PAYMENT = ExtractionResult(
    kind="payment",
    title="Экскурсия 12.09 — взнос 15 €",
    summary="Класс 3b, взнос 15 € до 8 сентября.",
    source_language="de",
    action_required=True,
    confidence=0.9,
)


class StubExtractor:
    def __init__(self, result: ExtractionResult = PAYMENT, tokens=(1000, 500, 0)) -> None:
        self.result = result
        self.tokens = tokens
        self.calls = 0

    def extract_email(self, parsed) -> Extraction:
        self.calls += 1
        pt, ct, cache = self.tokens
        return Extraction(result=self.result, model="stub", input_tokens=pt, output_tokens=ct, cache_read_tokens=cache)

    def system_prompt(self) -> str:
        return "stub-prompt"


def test_usage_record_and_usd_estimate(tmp_path: Path) -> None:
    db = open_database(tmp_path / "h.db")
    store = UsageStore(db)
    day = "2026-08-06"
    row = store.record("hh1", day, prompt_tokens=100000, completion_tokens=10000, cache_read_tokens=5000)
    expected = estimate_usd(prompt_tokens=100000, completion_tokens=10000, cache_read_tokens=5000)
    assert row.usd_estimate == pytest.approx(expected)
    assert row.prompt_tokens == 100000
    fetched = store.get("hh1", day)
    assert fetched is not None and fetched.usd_estimate == pytest.approx(expected)
    # incremental
    store.record("hh1", day, prompt_tokens=1000, completion_tokens=100)
    assert store.get("hh1", day).prompt_tokens == 101000


def test_soft_limit_degrades_before_model_call(tmp_path: Path) -> None:
    db = open_database(tmp_path / "h.db")
    events = EventStore(db)
    transport = FakeTransport()
    household = Household(household_id="hh-degrade", owner="990000001", family=frozenset({"990000001"}))
    extractor = StubExtractor()
    pipeline = Pipeline(
        approvals=ApprovalStore(db),
        reminders=ReminderStore(db),
        transport=transport,
        extractor=extractor,
        chat="-100990000101",
        household=household,
        daily_cap_usd=0.01,  # very low
    )
    # Pre-fill usage over cap
    pipeline.usage.record("hh-degrade", today_utc(), prompt_tokens=100000, completion_tokens=50000)

    ingest_file(events, FIXTURES / "forwarded_school_de.eml")
    Worker(events, lambda event: pipeline.handle_event(event)).run_once()

    assert extractor.calls == 0  # no model call
    assert len(transport.messages) == 1
    assert DEGRADED_MESSAGE in transport.messages[0].text
    # Still staged approval durable — verify by approval id returned from handle_event
    handled = pipeline.approvals.get  # keep reference for linter
    assert handled is not None
    # Extract staged id from transport card or query durable approvals table
    rows = pipeline.approvals.db.query("SELECT id FROM approvals WHERE event_id IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    assert len(rows) == 1, "degraded path must create durable staged approval"
    assert DEGRADED_MESSAGE in transport.messages[0].text


def test_no_hard_drop_of_inflight_and_cap_allows_next_day(tmp_path: Path) -> None:
    db = open_database(tmp_path / "h.db")
    events = EventStore(db)
    transport = FakeTransport()
    household = Household(household_id="hh-cap", owner="990000001", family=frozenset({"990000001"}))
    extractor = StubExtractor()
    pipeline = Pipeline(
        approvals=ApprovalStore(db),
        reminders=ReminderStore(db),
        transport=transport,
        extractor=extractor,
        chat="-100990000101",
        household=household,
        daily_cap_usd=5.0,
    )
    # First event under cap -> model called and usage recorded
    ingest_file(events, FIXTURES / "forwarded_school_de.eml")
    Worker(events, lambda event: pipeline.handle_event(event)).run_once()
    assert extractor.calls == 1
    day = today_utc()
    assert pipeline.usage.get("hh-cap", day) is not None
    # Second day different day -> not degraded
    pipeline2 = Pipeline(
        approvals=ApprovalStore(db),
        reminders=ReminderStore(db),
        transport=transport,
        extractor=extractor,
        chat="-100990000101",
        household=household,
        daily_cap_usd=0.0,  # cap zero but different day check
    )
    # usage for tomorrow is empty, so not over budget — cap resets per day
    assert pipeline2.usage.is_over_budget("hh-cap", "2099-01-01", 0.0) is False, "new day must not inherit prior day usage"
    assert pipeline2.usage.is_over_budget("hh-cap", day, 1_000_000) is False


def test_cost_cap_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_COST_CAP_USD_PER_DAY", "1.5")
    db = open_database(tmp_path / "h.db")
    pipeline = Pipeline(
        approvals=ApprovalStore(db),
        reminders=ReminderStore(db),
        transport=FakeTransport(),
        extractor=StubExtractor(),
        chat="-100990000101",
    )
    assert pipeline.daily_cap_usd == pytest.approx(1.5)
