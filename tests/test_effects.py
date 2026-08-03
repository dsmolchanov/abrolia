"""Журнал эффектов и разбор после падения.

Главный вопрос этих тестов один: после `kill -9` в любом окне — сколько
эффектов? Ответ обязан быть «ровно один», и «один» включает случай «ни одного,
но человеку сказали правду».
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport, SendOutcomeUnknown
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.effects import (
    APPROVAL_TOOL_USE_ID,
    DEFAULT_LEASE_SECONDS,
    STATUS_DONE,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PENDING,
    EffectInFlight,
    EffectJournal,
)
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.card import KIND_ICS, KIND_REMINDER
from hermes_cloud.runner.pipeline import TEXT_OUTCOME_UNKNOWN, Pipeline

CHILD = Path(__file__).resolve().parent / "chaos_child.py"

# Аренда исполнителя переживает падение: пока она жива, повисший эффект —
# возможно, работа соседнего процесса (worker и listen ходят в одну базу).
# Разбор начинается после её истечения, поэтому тесты смотрят из будущего.
LATER = time.time() + 10 * DEFAULT_LEASE_SECONDS

sys.path.insert(0, str(CHILD.parent))
from chaos_child import ACTOR, CHAT, ICS_PAYLOAD, REMINDER_PAYLOAD  # noqa: E402


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=None,
        chat=CHAT,
    )
    return pipeline, transport


def stage_and_claim(pipeline: Pipeline, payload: dict):
    staged = pipeline.approvals.stage(
        kind=payload["kind"], payload=payload, chat=CHAT, actor=ACTOR
    )
    approval = pipeline.approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR
    )
    assert approval is not None
    return approval


# --- журнал -------------------------------------------------------------------


def test_repeated_tool_use_id_returns_the_prior_result(tmp_path: Path) -> None:
    """Идемпотентность хода: тот же tool_use_id не исполняется дважды."""
    journal = EffectJournal(open_database(tmp_path / "hermes.db"))

    first, fresh = journal.begin(run_id="run-1", tool_use_id="tu-1", kind="reminder")
    assert fresh is True
    journal.complete(first.id, "напоминание создано")

    again, fresh_again = journal.begin(run_id="run-1", tool_use_id="tu-1", kind="reminder")

    assert fresh_again is False
    assert again.id == first.id
    assert again.status == STATUS_DONE and again.result == "напоминание создано"


def test_live_lease_forbids_a_second_executor(tmp_path: Path) -> None:
    journal = EffectJournal(open_database(tmp_path / "hermes.db"))
    journal.begin(run_id="run-1", tool_use_id="tu-1", kind="ics", now=1000.0)

    with pytest.raises(EffectInFlight):
        journal.begin(run_id="run-1", tool_use_id="tu-1", kind="ics", now=1001.0)


def test_expired_lease_is_visible_but_not_restarted(tmp_path: Path) -> None:
    """Истёкшая аренда — повод разобраться, а не повод сделать ещё раз."""
    journal = EffectJournal(open_database(tmp_path / "hermes.db"))
    started, _ = journal.begin(run_id="run-1", tool_use_id="tu-1", kind="ics", now=1000.0)

    stale = journal.stale(now=1000.0 + 10_000)

    assert [effect.id for effect in stale] == [started.id]
    assert stale[0].status == STATUS_PENDING
    # Ничего не «перезапустилось» само: судьбу решает стратегия вида эффекта.
    reopened, fresh = journal.begin(
        run_id="run-1", tool_use_id="tu-1", kind="ics", now=1000.0 + 10_000
    )
    assert fresh is False and reopened.id == started.id


def test_different_runs_do_not_collide(tmp_path: Path) -> None:
    journal = EffectJournal(open_database(tmp_path / "hermes.db"))

    one, _ = journal.begin(run_id="run-1", tool_use_id="tu-1", kind="reminder")
    two, _ = journal.begin(run_id="run-2", tool_use_id="tu-1", kind="reminder")

    assert one.id != two.id
    assert journal.has_effects("run-1") and journal.has_effects("run-2")
    assert not journal.has_effects("run-3")


# --- claim и журнал — одна транзакция ----------------------------------------


def test_claim_records_the_attempt_in_the_same_transaction(world) -> None:
    pipeline, _ = world

    approval = stage_and_claim(pipeline, REMINDER_PAYLOAD)

    effect = pipeline.effects.find(run_id=approval.id, tool_use_id=APPROVAL_TOOL_USE_ID)
    assert effect is not None, "подтверждение без записи о попытке невозможно"
    assert effect.status == STATUS_PENDING
    assert effect.kind == KIND_REMINDER and effect.approval_id == approval.id


def test_successful_execution_settles_the_effect(world) -> None:
    pipeline, transport = world
    approval = stage_and_claim(pipeline, REMINDER_PAYLOAD)

    handled = pipeline.execute(approval)

    effect = pipeline.effects.find(run_id=approval.id, tool_use_id=APPROVAL_TOOL_USE_ID)
    assert handled.executed == KIND_REMINDER
    assert effect.status == STATUS_DONE and effect.result == handled.message


def test_broken_connection_is_outcome_unknown_not_failure(world) -> None:
    """Отправка могла дойти. Врать «не получилось» нельзя, повторять — тем более."""
    pipeline, transport = world
    approval = stage_and_claim(pipeline, ICS_PAYLOAD)

    def explode(**_):
        raise SendOutcomeUnknown("соединение оборвалось")

    transport.send_document = explode  # type: ignore[method-assign]

    handled = pipeline.execute(approval)

    effect = pipeline.effects.find(run_id=approval.id, tool_use_id=APPROVAL_TOOL_USE_ID)
    assert effect.status == STATUS_OUTCOME_UNKNOWN
    assert handled.executed is None
    assert transport.messages[-1].text == TEXT_OUTCOME_UNKNOWN


# --- chaos: kill -9 в каждом окне --------------------------------------------


def crash_in(window: str, database_path: Path) -> None:
    """Запустить дочерний процесс и убедиться, что его убили, а не он вышел."""
    result = subprocess.run(
        [sys.executable, str(CHILD), str(database_path), window],
        capture_output=True,
        cwd=str(CHILD.parent.parent),
    )
    assert result.returncode == -signal.SIGKILL, (
        f"процесс обязан был умереть в окне {window}: "
        f"код {result.returncode}, stderr {result.stderr.decode()[-500:]}"
    )


def restart(database_path: Path):
    """Перезапуск: новая база, новый транспорт, разбор повисшего."""
    database = open_database(database_path)
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=None,
        chat=CHAT,
    )
    return pipeline, transport


def test_crash_after_claim_leaves_exactly_one_reminder(tmp_path: Path) -> None:
    """Окно «подтвердили, исполнить не успели» — доделываем, ровно один раз."""
    database_path = tmp_path / "hermes.db"
    crash_in("after_claim", database_path)
    pipeline, transport = restart(database_path)

    assert pipeline.reconcile() == [], "живая аренда — не повод вмешиваться"

    handled = pipeline.reconcile(now=LATER)

    assert len(handled) == 1
    assert len(pipeline.reminders.pending()) == 1
    assert pipeline.approvals.pending_for(CHAT) == [], "подтверждение не висит в staged"
    assert pipeline.effects.stale(now=LATER) == [], "повисших эффектов не осталось"

    # Повторный разбор ничего не добавляет: он идемпотентен по построению.
    assert pipeline.reconcile(now=LATER) == []
    assert len(pipeline.reminders.pending()) == 1


def test_crash_between_effect_and_mark_does_not_duplicate(tmp_path: Path) -> None:
    """Окно «сделали, отметить не успели» — доделка не создаёт второе."""
    database_path = tmp_path / "hermes.db"
    crash_in("after_effect", database_path)
    pipeline, transport = restart(database_path)
    assert len(pipeline.reminders.pending()) == 1, "напоминание пережило падение"

    pipeline.reconcile(now=LATER)

    assert len(pipeline.reminders.pending()) == 1, "второго напоминания нет"
    effects = pipeline.effects.for_run(pipeline.reminders.pending()[0].approval_id)
    assert [effect.status for effect in effects] == [STATUS_DONE]


def test_crash_during_an_outward_send_is_never_retried(tmp_path: Path) -> None:
    """Окно «упали в середине отправки» — исход неизвестен, повтора нет."""
    database_path = tmp_path / "hermes.db"
    crash_in("during_send", database_path)
    pipeline, transport = restart(database_path)

    handled = pipeline.reconcile(now=LATER)

    assert len(handled) == 1
    assert transport.documents == [], "наружу повторно ничего не ушло"
    assert transport.messages[-1].text == TEXT_OUTCOME_UNKNOWN
    approval_id = handled[0].approval_id
    assert pipeline.approvals.get(approval_id).status == "failed"
    effects = pipeline.effects.for_run(approval_id)
    assert [effect.status for effect in effects] == [STATUS_OUTCOME_UNKNOWN]
    assert effects[0].kind == KIND_ICS
