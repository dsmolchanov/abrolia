"""Small durable scheduler for reminders and one daily family digest."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hermes_cloud.channels.telegram import SendOutcomeUnknown, Transport
from hermes_cloud.core.db import Database
from hermes_cloud.execute.reminder import ReminderStore

STATE_KEY = "scheduler:v1"


@dataclass(frozen=True)
class SchedulerConfig:
    timezone: str
    digest_time: str = "07:00"
    digest_chats: tuple[str, ...] = ()
    max_scheduled_runs: int = 4

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        try:
            hour, minute = (int(part) for part in self.digest_time.split(":"))
        except (TypeError, ValueError) as error:
            raise ValueError("digest_time must be HH:MM") from error
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("digest_time must be HH:MM")
        if self.max_scheduled_runs < 1:
            raise ValueError("max_scheduled_runs must be positive")


@dataclass(frozen=True)
class SchedulerResult:
    reminders: int = 0
    digests: int = 0


class Scheduler:
    def __init__(
        self,
        database: Database,
        transport: Transport,
        config: SchedulerConfig,
        *,
        compose_digest: Callable[[datetime], str],
    ) -> None:
        self.db = database
        self.reminders = ReminderStore(database)
        self.transport = transport
        self.config = config
        self.compose_digest = compose_digest

    def run_once(self, *, now: datetime | None = None) -> SchedulerResult:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        reminders = self._deliver_reminders(moment.timestamp())
        digests = self._deliver_digest(moment)
        return SchedulerResult(reminders=reminders, digests=digests)

    def _deliver_reminders(self, timestamp: float) -> int:
        delivered = 0
        while reminder := self.reminders.claim_due(now=timestamp):
            try:
                self.transport.send_message(
                    chat=reminder.chat,
                    thread=reminder.thread,
                    text=f"Напоминание: {reminder.text}",
                )
            except SendOutcomeUnknown:
                # It may have arrived; settle to prevent an automatic duplicate.
                self.reminders.mark_delivered(reminder.id, now=timestamp)
                raise
            self.reminders.mark_delivered(reminder.id, now=timestamp)
            delivered += 1
        return delivered

    def _deliver_digest(self, moment: datetime) -> int:
        if not self.config.digest_chats:
            return 0
        local = moment.astimezone(ZoneInfo(self.config.timezone))
        today = local.date().isoformat()
        if local.strftime("%H:%M") < self.config.digest_time:
            return 0
        state = self._state()
        if state.get("date") != today:
            state = {"date": today, "runs": 0, "digest": "pending"}
        if state.get("digest") != "pending" or int(state.get("runs", 0)) >= self.config.max_scheduled_runs:
            return 0
        # Reserve before external work: a crash or unknown outcome cannot duplicate a digest.
        state.update(digest="reserved", runs=int(state.get("runs", 0)) + 1)
        self._write_state(state, moment.timestamp())
        text = self.compose_digest(local).strip()
        if not text:
            state["digest"] = "failed"
            self._write_state(state, moment.timestamp())
            return 0
        for chat in self.config.digest_chats:
            self.transport.send_message(chat=chat, text=f"☀️ {text}")
        state["digest"] = "sent"
        self._write_state(state, moment.timestamp())
        return len(self.config.digest_chats)

    def _state(self) -> dict:
        row = self.db.query_one("SELECT value FROM channel_state WHERE key = ?", (STATE_KEY,))
        if row is None:
            return {}
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            return {"digest": "failed"}
        return value if isinstance(value, dict) else {"digest": "failed"}

    def _write_state(self, state: dict, timestamp: float) -> None:
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO channel_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (STATE_KEY, json.dumps(state, sort_keys=True), timestamp),
            )
