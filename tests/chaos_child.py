"""Процесс, который убивают в заданном окне. Запускается из test_effects.py.

Не тест — участник теста. `kill -9` самому себе честнее любого мока: ни
`finally`, ни `atexit`, ни буфер записи не успевают ничего исправить, и база
остаётся ровно в том состоянии, в каком её застаёт настоящий сбой.

Окна соответствуют плану (Фаза 2, chaos-тесты):

* `after_claim`   — подтверждение засчитано, исполнитель ещё не звался;
* `after_effect`  — исполнитель отработал, отметка о завершении не поставлена;
* `during_send`   — падение посреди отправки наружу (исход неизвестен);
* `mid_loop`      — падение в середине хода модели, между двумя её вызовами.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cloud.core.approvals import ApprovalStore  # noqa: E402
from hermes_cloud.core.db import open_database  # noqa: E402
from hermes_cloud.execute.reminder import ReminderStore  # noqa: E402
from hermes_cloud.runner.card import KIND_ICS, KIND_REMINDER  # noqa: E402
from hermes_cloud.runner.pipeline import Pipeline  # noqa: E402

CHAT = "-100990000101"
ACTOR = "990000001"

REMINDER_PAYLOAD = {
    "kind": KIND_REMINDER,
    "text": "оплатить взнос 15 EUR",
    "due_date": "2026-09-08",
}
ICS_PAYLOAD = {
    "kind": KIND_ICS,
    "title": "Klassenfahrt 3b",
    "start": "2026-09-12T07:45:00+00:00",
    "end": None,
    "location": None,
    "description": None,
}


def die() -> None:
    """Ровно то, что делает OOM-killer или упавший хост."""
    os.kill(os.getpid(), signal.SIGKILL)


class CrashingTransport:
    """Транспорт, который умирает посреди отправки."""

    def __init__(self, *, crash_on_send: bool) -> None:
        self.crash_on_send = crash_on_send

    def send_message(self, **_: object) -> str:
        return "sent"

    def send_document(self, **_: object) -> str:
        if self.crash_on_send:
            die()
        return "sent"

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    def get_updates(self, **_: object) -> list:
        return []


class CrashingReminders(ReminderStore):
    """Напоминание создано, отметить исход процесс уже не успевает."""

    def create(self, **kwargs):  # type: ignore[override]
        reminder = super().create(**kwargs)
        die()
        return reminder


# Ход модели, который родительский тест повторит после перезапуска.
RUN_ID = "chaos00000000000000000000000run"
TOOL_USE_ID = "tu-chaos"
TOOL_ARGUMENTS = {"text": "оплатить взнос 15 EUR", "due_date": "2026-09-08"}


def crash_mid_loop(database) -> None:
    """Модель попросила инструмент, эффект записан — и тут процесс умирает."""
    from hermes_cloud.core.effects import EffectJournal
    from hermes_cloud.core.runcontext import Household, build_run_context
    from hermes_cloud.runner.model import ToolLoop
    from hermes_cloud.runner.tools import Services

    class DyingClient:
        """Первый ответ — просьба об инструменте, на втором вызове смерть."""

        def __init__(self) -> None:
            self.calls = 0

        @property
        def messages(self):
            return self

        def create(self, **_: object):
            self.calls += 1
            if self.calls > 1:
                die()
            return _ToolUseResponse()

    class _Block:
        type = "tool_use"
        id = TOOL_USE_ID
        name = "propose_reminder"
        input = TOOL_ARGUMENTS

    class _ToolUseResponse:
        stop_reason = "tool_use"
        content = [_Block()]
        usage = None

    household = Household(
        owner=ACTOR, family=frozenset({ACTOR}), allowed_chats=frozenset({CHAT})
    )
    context = build_run_context(
        household=household, actor_id=ACTOR, chat_id=CHAT, run_id=RUN_ID
    )
    loop = ToolLoop(
        journal=EffectJournal(database),
        services=Services(
            approvals=ApprovalStore(database), reminders=ReminderStore(database)
        ),
        client=DyingClient(),
    )
    loop.run(context, "напомни оплатить взнос")


def main(argv: list[str]) -> int:
    database_path, window = argv[1], argv[2]
    database = open_database(Path(database_path))
    approvals = ApprovalStore(database)
    if window == "mid_loop":
        crash_mid_loop(database)
        raise SystemExit("процесс обязан был умереть в середине хода")
    payload = ICS_PAYLOAD if window == "during_send" else REMINDER_PAYLOAD
    reminders = (
        CrashingReminders(database) if window == "after_effect" else ReminderStore(database)
    )
    pipeline = Pipeline(
        approvals=approvals,
        reminders=reminders,
        transport=CrashingTransport(crash_on_send=window == "during_send"),
        extractor=None,  # до модели этот путь не доходит
        chat=CHAT,
    )

    staged = approvals.stage(kind=payload["kind"], payload=payload, chat=CHAT, actor=ACTOR)
    approval = approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR
    )
    assert approval is not None
    if window == "after_claim":
        die()

    pipeline.execute(approval)
    raise SystemExit(f"процесс обязан был умереть в окне {window!r}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
