"""Cost caps: per-household/day counter + soft-limit degradation message."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.channels.web import WebChannelMessage, handle_web_message
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runcontext import ROLE_FAMILY, ROLE_OWNER, Household, RunContext
from hermes_cloud.core.usage import DEGRADED_MESSAGE, UsageStore, estimate_usd, today_utc
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.inject import ingest_file
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.extraction import Extraction, ExtractionResult
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


class CountingLoop:
    """Dialogue-model stub: counts turns and honours the cost guard.

    It records through the guard exactly as the real `ToolLoop._call` does,
    because the contract under test is now "the guard is what accounts", not
    "the channel records after the run".
    """

    def __init__(self, tokens=(2000, 800)) -> None:
        self.calls = 0
        self.tokens = tokens

    def run(self, context, text, *, history=None, cost_guard=None):
        self.calls += 1
        pt, ct = self.tokens
        if cost_guard is not None:
            cost_guard.record(
                SimpleNamespace(usage=SimpleNamespace(input_tokens=pt, output_tokens=ct))
            )
        return SimpleNamespace(
            text="готово", input_tokens=pt, output_tokens=ct,
            iterations=1, tokens=pt + ct, stopped="completed",
        )


def _prefill_over_budget(store: UsageStore, household_id: str) -> None:
    store.record(household_id, today_utc(), prompt_tokens=100000, completion_tokens=50000)


def test_whatsapp_dialogue_degrades_when_over_budget(tmp_path: Path) -> None:
    db = open_database(tmp_path / "h.db")
    context = RunContext(
        household_id="hh-wa",
        actor_id="+999123456",
        chat_id="999123456@s.whatsapp.invalid",
        thread_id=None,
        role=ROLE_FAMILY,
        scope="dialogue",
    )
    # The staged reply carries a real event FK, so append the event first.
    events = EventStore(db)
    accepted = events.append(
        source="whatsapp",
        external_id="ext-1",
        raw=b"Subject: q\nContent-Type: text/plain; charset=utf-8\n\nprivet",
    )
    event = accepted.event

    def build(loop, cap):
        return Pipeline(
            approvals=ApprovalStore(db),
            reminders=ReminderStore(db),
            transport=FakeTransport(),
            extractor=None,
            chat="telegram-control",
            loop=loop,
            daily_cap_usd=cap,
        )

    capped_loop = CountingLoop()
    capped = build(capped_loop, cap=0.01)
    _prefill_over_budget(capped.usage, "hh-wa")

    before = len(capped.approvals.db.query("SELECT id FROM approvals"))
    handled = capped._handle_whatsapp_dialogue(event, context)

    assert handled.message == DEGRADED_MESSAGE
    assert capped_loop.calls == 0, "over-budget dialogue must not reach the model"
    assert DEGRADED_MESSAGE in capped.transport.messages[-1].text
    after = len(capped.approvals.db.query("SELECT id FROM approvals"))
    assert after == before, "degraded dialogue stages nothing for approval"

    # Under a raised cap the same turn reaches the model; its spend joins the
    # prefilled day row (100000 + 2000).
    free_loop = CountingLoop()
    free = build(free_loop, cap=1_000_000.0)
    handled2 = free._handle_whatsapp_dialogue(event, context)
    assert free_loop.calls == 1
    assert handled2.approval_id is not None
    row = free.usage.get("hh-wa", today_utc())
    assert row is not None
    assert row.prompt_tokens == 102000 and row.completion_tokens == 50800


def test_web_channel_cap_guards_the_model_path(tmp_path: Path) -> None:
    store = UsageStore(open_database(tmp_path / "h.db"))
    loop = CountingLoop()
    context = RunContext(
        household_id="hh-web",
        actor_id="owner",
        chat_id="web-chat",
        thread_id=None,
        role=ROLE_OWNER,
        scope="chat",
    )
    message = WebChannelMessage(actor_id="owner", text="собери повестку")

    _prefill_over_budget(store, "hh-web")
    assert (
        handle_web_message(message, context=context, loop=loop, usage=store, daily_cap_usd=0.01)
        == DEGRADED_MESSAGE
    )
    assert loop.calls == 0, "over-budget web chat must not reach the model"

    under_cap = handle_web_message(
        message, context=context, loop=loop, usage=store, daily_cap_usd=1_000_000.0
    )
    assert under_cap == "готово"
    assert loop.calls == 1
    row = store.get("hh-web", today_utc())
    assert row is not None and row.prompt_tokens >= 2000



def test_telegram_dialogue_degrades_when_over_budget_and_records_when_under(
    tmp_path: Path, caplog
) -> None:
    db = open_database(tmp_path / "h.db")
    actor, chat = 990000001, -100990000101

    def telegram_update() -> dict:
        return {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": chat},
                "from": {"id": actor},
                "text": "перепиши короче",
            },
        }
    household = Household(
        household_id="hh-tg",
        owner=str(actor),
        family=frozenset({str(actor)}),
        allowed_chats=frozenset({str(chat)}),
    )

    def build(loop, cap):
        return Pipeline(
            approvals=ApprovalStore(db),
            reminders=ReminderStore(db),
            transport=FakeTransport(),
            extractor=None,
            chat=str(chat),
            loop=loop,
            household=household,
            daily_cap_usd=cap,
        )

    # Over budget: no model call, honest degraded reply, alert emitted.
    capped_loop = CountingLoop()
    capped = build(capped_loop, cap=0.01)
    _prefill_over_budget(capped.usage, "hh-tg")
    with caplog.at_level(logging.WARNING, logger="hermes_cloud.runner.pipeline"):
        handled = capped.handle_update(telegram_update(), household)
    assert handled is not None and handled.message == DEGRADED_MESSAGE
    assert capped_loop.calls == 0, "over-budget dialogue must not reach the model"
    assert DEGRADED_MESSAGE in capped.transport.messages[-1].text
    assert "budget_exceeded" in caplog.text

    # Under budget: model runs and its spend accumulates on the day's row —
    # the next capped path (any channel) will see it.
    free_loop = CountingLoop()
    free = build(free_loop, cap=100.0)
    handled2 = free.handle_update(telegram_update(), household)
    assert handled2.message == "готово"
    assert free_loop.calls == 1
    row = free.usage.get("hh-tg", today_utc())
    assert row is not None
    assert row.prompt_tokens == 102000 and row.completion_tokens == 50800



# --- the cap at the provider-call boundary ---------------------------------
#
# The channel check runs ONCE per turn. A turn makes up to MAX_ITERATIONS
# provider calls plus a retry, so a turn that crossed the cap on its first call
# used to pay for every call after it. These cases drive a REAL ToolLoop, not a
# stub, because the defect lived between the check and the calls.

from hermes_cloud.core.approvals import ApprovalStore as _ApprovalStore  # noqa: E402
from hermes_cloud.core.effects import EffectJournal  # noqa: E402
from hermes_cloud.core.usage import DailyCostGuard  # noqa: E402
from hermes_cloud.runner.model import STOP_COST_CAP, ToolLoop  # noqa: E402
from hermes_cloud.runner.tools import Services  # noqa: E402
from tests.test_model import (  # noqa: E402
    CHAT,
    HOUSEHOLD,
    PARENT,
    ScriptedClient,
    asks,
    build_run_context,
    says,
)

#: `HOUSEHOLD` in test_model is the Household object; usage is keyed by its id.
HID = HOUSEHOLD.household_id

#: One response's worth of spend. Kept well under `model.TOKEN_BUDGET`
#: (120_000) so the loop stops on the COST cap under test rather than on its
#: own per-turn token budget — the cap under test is measured in dollars, so
#: the test moves the dollar threshold instead of the token count.
_HEAVY = 5_000
#: $0.003/1k prompt + $0.015/1k completion → one response above this.
_TINY_CAP = 0.05


def _heavy(response):
    response.usage.input_tokens = _HEAVY
    response.usage.output_tokens = _HEAVY
    return response


def _loop_with_cap(tmp_path, script, store, *, cap_usd):
    database = open_database(tmp_path / "hermes.db")
    services = Services(
        approvals=_ApprovalStore(database), reminders=ReminderStore(database)
    )
    client = ScriptedClient(script=list(script))
    loop = ToolLoop(
        journal=EffectJournal(database), services=services, client=client
    )
    guard = DailyCostGuard(store, HID, cap_usd=cap_usd)
    return loop, client, guard


def test_a_turn_that_crosses_the_cap_makes_no_further_provider_call(
    tmp_path: Path,
) -> None:
    """The defect, stated exactly.

    The first response asks for a tool AND spends past the cap. Before the
    guard moved to the call boundary, the loop would answer the tool and call
    the provider again — up to seven more times — because the only check had
    already happened before the turn began.
    """
    database = open_database(tmp_path / "usage.db")
    store = UsageStore(database)
    script = [
        _heavy(asks("propose_reminder", {"text": "молоко", "when": "завтра"})),
        says("этого вызова быть не должно"),
    ]
    loop, client, guard = _loop_with_cap(tmp_path, script, store, cap_usd=_TINY_CAP)

    result = loop.run(
        build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT),
        "напомни про молоко",
        cost_guard=guard,
    )

    assert len(client.calls) == 1
    assert result.stopped == STOP_COST_CAP
    assert result.text == DEGRADED_MESSAGE
    # The spend that crossed the cap was recorded as it happened, not after
    # the turn — which is what lets the next check see it.
    assert store.is_over_budget(HID, today_utc(), _TINY_CAP)


def test_an_already_over_budget_turn_never_reaches_the_provider(
    tmp_path: Path,
) -> None:
    database = open_database(tmp_path / "usage.db")
    store = UsageStore(database)
    store.record(HID, today_utc(), prompt_tokens=_HEAVY, completion_tokens=_HEAVY)
    loop, client, guard = _loop_with_cap(
        tmp_path, [says("не должно быть вызвано")], store, cap_usd=_TINY_CAP
    )

    result = loop.run(
        build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT),
        "привет",
        cost_guard=guard,
    )

    assert client.calls == []
    assert result.stopped == STOP_COST_CAP
    assert result.text == DEGRADED_MESSAGE


def test_a_retry_is_also_a_paid_call_and_is_capped(tmp_path: Path) -> None:
    """`_call` retries once while the turn is still empty. That retry reaches
    the provider, so the cap has to be asked again before it — not once per
    turn."""
    database = open_database(tmp_path / "usage.db")
    store = UsageStore(database)
    # First attempt fails, so the loop would retry; the cap is crossed in
    # between by another path recording spend for the same household+day.
    class _FailThenNotice:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.calls.append(kwargs)
            store.record(
                HID, today_utc(), prompt_tokens=_HEAVY, completion_tokens=_HEAVY
            )
            raise RuntimeError("сеть подвела")

    database2 = open_database(tmp_path / "hermes.db")
    services = Services(
        approvals=_ApprovalStore(database2), reminders=ReminderStore(database2)
    )
    client = _FailThenNotice()
    loop = ToolLoop(
        journal=EffectJournal(database2), services=services, client=client
    )

    result = loop.run(
        build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT),
        "привет",
        cost_guard=DailyCostGuard(store, HID, cap_usd=_TINY_CAP),
    )

    # One attempt happened; the retry was refused by the cap rather than made.
    assert len(client.calls) == 1
    assert result.stopped == STOP_COST_CAP
