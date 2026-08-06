"""Авторизация каждого хода: кто, где и что ему позволено.

Единственный источник прав — **проверенный транспортом апдейт**, а не текст
сообщения и не то, что «сказала модель». Контекст собирается сервером на входе
хода и передаётся первым аргументом в каждый tool; tool проверяет права **сам**,
даже если вызывающий уже проверил (`docs/SECURITY.md`, T4: defense in depth —
одна забытая проверка в новом месте вызова не должна открывать доступ).

Неизвестный актор получает не «урезанный набор», а **пустой**: ни одного tool.
Разница принципиальная — «мало прав» отлаживают, «нет прав» проверяют.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from hermes_cloud.core.db import new_id
from hermes_cloud.core.runtime_manifest import maybe_load_runtime_manifest

# --- права --------------------------------------------------------------------

# Чтение
READ_TASKS = "tasks.read"
READ_CALENDAR = "calendar.read"
READ_EMAIL = "email.read"
READ_MEMORY = "memory.read"

# Мутации. Разделены не по «опасности», а по тому, что человек подтверждает:
# создание напоминания видно семье, экспорт и удаление — операции владельца.
WRITE_REMINDER = "reminder.create"
WRITE_CALENDAR = "calendar.write"
WRITE_MEMORY = "memory.append"
WRITE_EMAIL = "email.compose"
WRITE_WHATSAPP = "whatsapp.send"
DATA_EXPORT = "data.export"
DATA_DELETE = "data.delete"

ALL_READ = frozenset({READ_TASKS, READ_CALENDAR, READ_EMAIL, READ_MEMORY})
ALL_WRITE = frozenset({
    WRITE_REMINDER, WRITE_CALENDAR, WRITE_MEMORY, WRITE_EMAIL, WRITE_WHATSAPP,
    DATA_EXPORT, DATA_DELETE
})

ROLE_OWNER = "owner"
ROLE_FAMILY = "family"
ROLE_GUEST = "guest"
ROLE_UNKNOWN = "unknown"

# Роль → права. Таблица намеренно плоская и читаемая целиком: права, которые
# нельзя окинуть взглядом, никто не ревьюит.
ROLE_CAPS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    # Владелец: всё, включая экспорт и удаление household'а.
    ROLE_OWNER: (ALL_READ, ALL_WRITE),
    # Семья: обычная работа, но не экспорт и не удаление — это решение владельца.
    ROLE_FAMILY: (ALL_READ, ALL_WRITE - {DATA_EXPORT, DATA_DELETE}),
    # Гость (няня, бабушка, репетитор): видит задачи и календарь, не меняет
    # ничего и не читает почту и память семьи.
    ROLE_GUEST: (frozenset({READ_TASKS, READ_CALENDAR}), frozenset()),
    # Неизвестный: ноль. Не «read-only», а именно ноль.
    ROLE_UNKNOWN: (frozenset(), frozenset()),
}

SCOPE_PERSONAL = "personal"
SCOPE_SHARED = "shared"

ENV_HOUSEHOLD_FILE = "HERMES_HOUSEHOLD"
ENV_OWNER = "HERMES_OWNER"
ENV_FAMILY = "HERMES_FAMILY_ACTORS"
ENV_GUESTS = "HERMES_GUEST_ACTORS"
DEFAULT_HOUSEHOLD_FILE = "household.toml"


class CapabilityDenied(PermissionError):
    """Ход попытался сделать то, на что у актора нет прав."""

    def __init__(self, capability: str, context: RunContext) -> None:
        super().__init__(
            f"актор {context.actor_id} (роль {context.role}) не имеет права {capability!r}"
        )
        self.capability = capability
        self.role = context.role


@dataclass(frozen=True)
class Household:
    """Кто входит в household и в какой роли."""

    household_id: str = "household"
    owner: str | None = None
    family: frozenset[str] = frozenset()
    guests: frozenset[str] = frozenset()
    allowed_chats: frozenset[str] = frozenset()
    # Legacy config authorizes actors and chats as two allowlists.  A versioned
    # manifest carries exact verified bindings and therefore denies cross-pairs.
    verified_bindings: frozenset[tuple[str, str]] | None = None

    def role_for(self, actor_id: str) -> str:
        actor = str(actor_id)
        if self.owner and actor == self.owner:
            return ROLE_OWNER
        if actor in self.family:
            return ROLE_FAMILY
        if actor in self.guests:
            return ROLE_GUEST
        return ROLE_UNKNOWN

    def knows_chat(self, chat_id: str) -> bool:
        """Пустой allowlist означает «чат не настроен», а не «разрешены все»."""
        return bool(self.allowed_chats) and str(chat_id) in self.allowed_chats

    def knows_binding(self, actor_id: str, chat_id: str) -> bool:
        if self.verified_bindings is None:
            return self.knows_chat(chat_id)
        return (str(actor_id), str(chat_id)) in self.verified_bindings


def _split(value: str | None) -> frozenset[str]:
    return frozenset(item.strip() for item in (value or "").split(",") if item.strip())


def load_household(
    path: Path | str | None = None, *, env: dict[str, str] | None = None
) -> Household:
    """Прочитать household.toml, иначе собрать из окружения.

    Файл — целевая форма (Фаза 5, провижининг), окружение — рабочая до неё.
    Файл важнее: если он есть, окружение его не перебивает.
    """
    source = dict(os.environ if env is None else env)
    file = Path(path or source.get(ENV_HOUSEHOLD_FILE) or DEFAULT_HOUSEHOLD_FILE)
    manifest = maybe_load_runtime_manifest(file, env=source)
    if manifest is not None:
        return Household(
            household_id=manifest.household_id,
            owner=manifest.actors.owner,
            family=manifest.actors.family,
            guests=manifest.actors.guests,
            allowed_chats=manifest.allowed_chats,
            verified_bindings=manifest.verified_actor_chat_pairs,
        )
    if file.is_file():
        data = tomllib.loads(file.read_text(encoding="utf-8"))
        actors = data.get("actors", {})
        channels = data.get("channels", {})
        return Household(
            household_id=str(data.get("household_id", "household")),
            owner=str(actors["owner"]) if actors.get("owner") else None,
            family=frozenset(str(item) for item in actors.get("family", ())),
            guests=frozenset(str(item) for item in actors.get("guests", ())),
            allowed_chats=frozenset(str(item) for item in channels.get("allowed_chats", ())),
        )
    owner = (source.get(ENV_OWNER) or "").strip() or None
    family = _split(source.get(ENV_FAMILY))
    return Household(
        household_id=source.get("HERMES_HOUSEHOLD_ID", "household"),
        owner=owner,
        # Владелец всегда и член семьи: иначе он теряет обычные права,
        # получив особые.
        family=family | ({owner} if owner else set()),
        guests=_split(source.get(ENV_GUESTS)),
        allowed_chats=_split(source.get("HERMES_CHAT")),
    )


@dataclass(frozen=True)
class RunContext:
    """Права одного хода. Собирается сервером, не приходит снаружи."""

    household_id: str
    actor_id: str
    chat_id: str
    thread_id: int | None
    role: str
    scope: str
    read_caps: frozenset[str] = frozenset()
    mutate_caps: frozenset[str] = frozenset()
    run_id: str = field(default_factory=new_id)

    @property
    def is_known(self) -> bool:
        return self.role != ROLE_UNKNOWN

    @property
    def has_tools(self) -> bool:
        """Есть ли вообще что дать модели в этом ходе."""
        return bool(self.read_caps or self.mutate_caps)

    def can(self, capability: str) -> bool:
        return capability in self.read_caps or capability in self.mutate_caps

    def require(self, capability: str) -> None:
        """Проверка, которую делает сам tool. Отказ — исключение, не False."""
        if not self.can(capability):
            raise CapabilityDenied(capability, self)


def build_run_context(
    *,
    household: Household,
    actor_id: str,
    chat_id: str,
    thread_id: int | None = None,
    run_id: str | None = None,
) -> RunContext:
    """Собрать контекст хода из проверенных транспортом данных.

    Чат вне allowlist household'а обнуляет права так же, как неизвестный
    актор: свой человек, написавший из чужого чата, не должен там ничего мочь.
    """
    role = household.role_for(actor_id)
    if not household.knows_binding(actor_id, chat_id):
        role = ROLE_UNKNOWN
    read_caps, mutate_caps = ROLE_CAPS[role]
    return RunContext(
        household_id=household.household_id,
        actor_id=str(actor_id),
        chat_id=str(chat_id),
        thread_id=thread_id,
        role=role,
        # Личный чат — personal, групповой (id с минусом у Telegram) — shared.
        scope=SCOPE_SHARED if str(chat_id).startswith("-") else SCOPE_PERSONAL,
        read_caps=read_caps,
        mutate_caps=mutate_caps,
        run_id=run_id or new_id(),
    )
