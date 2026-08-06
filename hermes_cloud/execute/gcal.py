"""Google Calendar: событие, которое нельзя создать дважды.

Идемпотентность здесь достигается не «повторить и надеяться», а тем, что **id
события задаём мы сами** и выводим его из id подтверждения. Из этого следует
всё остальное:

* повтор после падения находит событие по тому же id и **обновляет** его, а не
  заводит второе;
* суперсессия («экскурсия перенесена на 19.09») — это тот же id и `patch`, а не
  новое событие рядом со старым (план, Фаза 4, п. 4);
* поиск идёт **перед** любой записью — `get` по id, и только если события нет,
  `insert`. Гонка двух воркеров упирается в 409 от Google, который мы читаем как
  «уже существует», а не как ошибку.

Именно поэтому календарь — единственный наружный эффект, который разрешено
доигрывать после падения: у него есть ключ, по которому видно, произошло ли уже.

API Google требует от client-generated id base32hex (`0-9a-v`), 5–1024 символа
(`Events: insert`, поле `id`). Хэш подтверждения кодируется именно так.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_DURATION = timedelta(hours=1)
DEFAULT_CALENDAR = "primary"
# Префикс — только буквы из алфавита base32hex, иначе Google отвергнет id.
ID_PREFIX = "hc"
ID_LENGTH = 32


class CalendarError(RuntimeError):
    """Календарь отказал явно: события не создано."""


class CalendarOutcomeUnknown(RuntimeError):
    """Связь оборвалась. Для календаря это поправимо: id детерминированный."""


def event_id_for(seed: str) -> str:
    """Детерминированный id события Google.

    Seed — **id обязательства**, если оно есть, и id подтверждения иначе.
    Разница принципиальная: обязательство переживает версии («экскурсия
    перенесена»), а подтверждение у каждой версии своё. Выводя id из факта, мы
    получаем обновление того же события; выводя из кнопки — второе событие
    рядом с первым.

    base32hex без паддинга: алфавит `0-9a-v` — ровно то, что принимает Google.
    Base64 или hex-с-дефисами он отвергает, а UUID содержит символы вне
    разрешённого набора.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    encoded = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    return (ID_PREFIX + encoded)[:ID_LENGTH]


@dataclass(frozen=True)
class CalendarEvent:
    """Событие в терминах нашего домена, до перевода в тело запроса Google."""

    id: str
    title: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None
    timezone: str = "UTC"

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "summary": self.title,
            "start": {"dateTime": _iso(self.start), "timeZone": self.timezone},
            "end": {"dateTime": _iso(self.end), "timeZone": self.timezone},
        }
        if self.description:
            body["description"] = self.description
        if self.location:
            body["location"] = self.location
        return body

    def differs_from(self, remote: dict[str, Any]) -> bool:
        """Разошлось ли то, что в календаре, с тем, что мы подтвердили."""
        return (
            (remote.get("summary") or None) != (self.title or None)
            or _comparison_moment(remote.get("start")) != _comparison_datetime(self.start)
            or _comparison_moment(remote.get("end")) != _comparison_datetime(self.end)
            or (remote.get("location") or None) != (self.location or None)
            or (remote.get("description") or None) != (self.description or None)
        )


def build_calendar_event(
    *,
    approval_id: str,
    commitment_id: str | None = None,
    title: str,
    start: datetime,
    end: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    timezone: str = "UTC",
) -> CalendarEvent:
    return CalendarEvent(
        id=event_id_for(commitment_id or approval_id),
        title=title,
        start=start,
        end=end or start + DEFAULT_DURATION,
        description=description,
        location=location,
        timezone=timezone,
    )


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _remote_moment(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("dateTime") or value.get("date")
    return None


def _comparison_datetime(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _comparison_moment(value: Any) -> str | None:
    raw = _remote_moment(value)
    if raw is None or "T" not in raw:
        return raw
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        return parsed.isoformat()
    return parsed.astimezone(UTC).isoformat()


class CalendarBackend(Protocol):
    """То немногое, что исполнителю нужно от календаря."""

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None: ...

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    def delete_event(self, calendar_id: str, event_id: str) -> None: ...

    def list_events(
        self, calendar_id: str, *, time_min: datetime, time_max: datetime, limit: int = 50
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Written:
    """Что стало с событием. `created=False` — оно уже было."""

    event: dict[str, Any]
    created: bool
    updated: bool

    @property
    def link(self) -> str | None:
        return self.event.get("htmlLink")


class Calendar:
    """Исполнитель календаря: сначала посмотреть, потом писать."""

    def __init__(self, backend: CalendarBackend, *, calendar_id: str = DEFAULT_CALENDAR) -> None:
        self.backend = backend
        self.calendar_id = calendar_id

    def upsert(self, event: CalendarEvent) -> Written:
        """Создать событие или привести существующее к подтверждённому виду."""
        existing = self.backend.get_event(self.calendar_id, event.id)
        if existing is not None:
            if not event.differs_from(existing):
                logger.info("calendar event %s уже на месте", event.id)
                return Written(event=existing, created=False, updated=False)
            # Суперсессия или доделка: то же событие, новое содержание.
            patched = self.backend.patch_event(self.calendar_id, event.id, event.to_body())
            logger.info("calendar event %s обновлён", event.id)
            return Written(event=patched, created=False, updated=True)

        try:
            created = self.backend.insert_event(self.calendar_id, event.to_body())
        except AlreadyExists:
            # Кто-то успел раньше — ровно тот же id, значит то же событие.
            found = self.backend.get_event(self.calendar_id, event.id)
            if found is None:  # pragma: no cover — календарь противоречит сам себе
                raise CalendarError(f"событие {event.id} и есть, и нет") from None
            return Written(event=found, created=False, updated=False)
        logger.info("calendar event %s создан", event.id)
        return Written(event=created, created=True, updated=False)

    def cancel(self, seed: str) -> bool:
        """Отменить событие, порождённое этим обязательством (или подтверждением)."""
        event_id = event_id_for(seed)
        if self.backend.get_event(self.calendar_id, event_id) is None:
            return False
        self.backend.delete_event(self.calendar_id, event_id)
        return True

    def upcoming(self, *, days: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        return self.backend.list_events(
            self.calendar_id, time_min=now, time_max=now + timedelta(days=days)
        )


class AlreadyExists(RuntimeError):
    """Событие с таким id уже есть (Google отвечает 409)."""


@dataclass
class FakeCalendar:
    """Календарь для тестов и `--console`: помнит события в памяти."""

    events: dict[str, dict[str, Any]] | None = None
    inserts: int = 0
    patches: int = 0

    def __post_init__(self) -> None:
        if self.events is None:
            self.events = {}

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        return self.events.get(f"{calendar_id}/{event_id}")

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        key = f"{calendar_id}/{body['id']}"
        if key in self.events:
            raise AlreadyExists(body["id"])
        self.inserts += 1
        stored = {**body, "htmlLink": f"https://calendar.example/{body['id']}"}
        self.events[key] = stored
        return stored

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        key = f"{calendar_id}/{event_id}"
        if key not in self.events:
            raise CalendarError(f"нет события {event_id}")
        self.patches += 1
        self.events[key] = {**self.events[key], **body}
        return self.events[key]

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.events.pop(f"{calendar_id}/{event_id}", None)

    def list_events(
        self, calendar_id: str, *, time_min: datetime, time_max: datetime, limit: int = 50
    ) -> list[dict[str, Any]]:
        found = [
            event for key, event in self.events.items()
            if key.startswith(f"{calendar_id}/")
            and _iso(time_min) <= (_remote_moment(event.get("start")) or "") <= _iso(time_max)
        ]
        return found[:limit]


class GoogleCalendar:
    """Живой бэкенд. Учётные данные — из secrets household'а, не из кода."""

    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def from_token_file(cls, path: str, *, scopes: tuple[str, ...] = ()) -> GoogleCalendar:
        """Собрать сервис из OAuth-токена ассистента (Фаза 5 — провижининг)."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials.from_authorized_user_file(path, list(scopes) or None)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(credentials.to_json())
        return cls(build("calendar", "v3", credentials=credentials, cache_discovery=False))

    def _events(self) -> Any:
        return self._service.events()

    def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        try:
            return self._events().get(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as error:  # noqa: BLE001 — googleapiclient.HttpError и сеть
            if _status_of(error) in {404, 410}:
                return None
            raise _translate(error, "get") from error

    def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._events().insert(calendarId=calendar_id, body=body).execute()
        except Exception as error:  # noqa: BLE001
            if _status_of(error) == 409:
                raise AlreadyExists(body.get("id", "")) from error
            raise _translate(error, "insert") from error

    def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return (
                self._events()
                .patch(calendarId=calendar_id, eventId=event_id, body=body)
                .execute()
            )
        except Exception as error:  # noqa: BLE001
            raise _translate(error, "patch") from error

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        try:
            self._events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except Exception as error:  # noqa: BLE001
            if _status_of(error) in {404, 410}:
                return
            raise _translate(error, "delete") from error

    def list_events(
        self, calendar_id: str, *, time_min: datetime, time_max: datetime, limit: int = 50
    ) -> list[dict[str, Any]]:
        try:
            response = (
                self._events()
                .list(
                    calendarId=calendar_id,
                    timeMin=_iso(time_min),
                    timeMax=_iso(time_max),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=limit,
                )
                .execute()
            )
        except Exception as error:  # noqa: BLE001
            raise _translate(error, "list") from error
        return list(response.get("items", []))


def _status_of(error: BaseException) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    return int(status) if status is not None else None


def _translate(error: BaseException, operation: str) -> BaseException:
    """Ответ сервера — отказ; отсутствие ответа — неизвестный исход."""
    status = _status_of(error)
    if status is None:
        return CalendarOutcomeUnknown(f"{operation}: {error}")
    if status >= 500:
        return CalendarOutcomeUnknown(f"{operation}: HTTP {status}")
    return CalendarError(f"{operation}: HTTP {status} {error}")
