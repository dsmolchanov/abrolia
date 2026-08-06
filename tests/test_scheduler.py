from datetime import UTC, datetime
from pathlib import Path

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.db import open_database
from hermes_cloud.execute.reminder import STATUS_DONE, ReminderStore
from hermes_cloud.scheduler import Scheduler, SchedulerConfig


def test_scheduler_delivers_due_reminders_and_one_daily_digest(tmp_path: Path) -> None:
    now = datetime(2026, 9, 12, 7, 30, tzinfo=UTC)
    with open_database(tmp_path / "scheduler.db") as database:
        reminders = ReminderStore(database)
        due = reminders.create(chat="family", text="оплатить взнос", due_at=now.timestamp() - 1)
        transport = FakeTransport()
        scheduler = Scheduler(
            database,
            transport,
            SchedulerConfig(
                timezone="UTC", digest_time="07:00", digest_chats=("family",)
            ),
            compose_digest=lambda local: "Сегодня экскурсия.",
        )

        first = scheduler.run_once(now=now)
        second = scheduler.run_once(now=now)

        assert first.reminders == 1 and first.digests == 1
        assert second.reminders == 0 and second.digests == 0
        assert reminders.get(due.id).status == STATUS_DONE
        assert [message.text for message in transport.messages] == [
            "Напоминание: оплатить взнос",
            "☀️ Сегодня экскурсия.",
        ]


def test_digest_waits_for_local_time_and_is_disabled_without_chats(tmp_path: Path) -> None:
    with open_database(tmp_path / "scheduler.db") as database:
        transport = FakeTransport()
        early = Scheduler(
            database,
            transport,
            SchedulerConfig(timezone="Europe/Prague", digest_time="09:00", digest_chats=("family",)),
            compose_digest=lambda local: "not yet",
        )
        assert early.run_once(now=datetime(2026, 1, 1, 7, 0, tzinfo=UTC)).digests == 0

        disabled = Scheduler(
            database,
            transport,
            SchedulerConfig(timezone="UTC"),
            compose_digest=lambda local: "disabled",
        )
        assert disabled.run_once(now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)).digests == 0
        assert transport.messages == []
