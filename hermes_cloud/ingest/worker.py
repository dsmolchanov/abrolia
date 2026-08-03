"""Фоновый обработчик событий: аренда → обработка → done/failed.

Разделение труда с транспортом жёсткое и намеренное: webhook (или CLI) только
фиксирует событие и отвечает «принято», а вся работа идёт здесь. Причина в
плане: обработка занимает секунды (модель, вложения), и делать её в
обработчике запроса — значит терять письма при таймауте канала.

Обработчик события передаётся снаружи (`handler`). Воркер ничего не знает про
модель и карточки: он отвечает за долговечность — аренду, счёт попыток, DLQ,
— и это единственное, что здесь тестируется.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from hermes_cloud.core.events import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    Event,
    EventStore,
)

logger = logging.getLogger(__name__)

# Обработчик события. Исключение = неудача попытки; возврат = успех.
Handler = Callable[[Event], None]


@dataclass(frozen=True)
class Processed:
    event: Event
    ok: bool
    error: str | None = None


class Worker:
    def __init__(
        self,
        store: EventStore,
        handler: Handler,
        *,
        worker_id: str = "worker",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.store = store
        self.handler = handler
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def run_once(self, *, now: float | None = None) -> Processed | None:
        """Обработать одно событие. None — очередь пуста."""
        event = self.store.lease(
            self.worker_id, lease_seconds=self.lease_seconds, now=now
        )
        if event is None:
            return None
        try:
            self.handler(event)
        except Exception as error:  # обработчик падает — это данные, не баг воркера
            # Лог без содержимого письма: только идентификаторы и причина
            # (docs/privacy/data-map.md, S12 — «логи без содержимого»).
            logger.warning(
                "event %s failed on attempt %s: %s",
                event.id, event.attempts + 1, type(error).__name__,
            )
            updated = self.store.mark_failed(
                event.id, f"{type(error).__name__}: {error}",
                max_attempts=self.max_attempts, now=now,
            )
            return Processed(event=updated, ok=False, error=str(error))
        self.store.mark_done(event.id, now=now)
        done = self.store.get(event.id)
        assert done is not None
        return Processed(event=done, ok=True)

    def drain(self, *, limit: int = 100, now: float | None = None) -> list[Processed]:
        """Обработать всё, что доступно сейчас (используется в тестах и CLI)."""
        results: list[Processed] = []
        for _ in range(limit):
            processed = self.run_once(now=now)
            if processed is None:
                break
            results.append(processed)
        return results

    def run_forever(self, *, idle_sleep: float = 1.0, should_stop=lambda: False) -> None:
        """Цикл воркера. Пустая очередь — короткий сон, а не busy-wait."""
        while not should_stop():
            if self.run_once() is None:
                time.sleep(idle_sleep)
