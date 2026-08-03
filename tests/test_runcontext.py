"""Авторизация хода: роли, caps и матрица «каждый tool × каждая роль».

Матрица строится по реестру, а не по списку имён: новый tool автоматически
попадает в неё и обязан объявить своё право. Забыть добавить tool в тест
нельзя — можно только осознанно дать ему право.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.runcontext import (
    ALL_READ,
    ALL_WRITE,
    DATA_DELETE,
    DATA_EXPORT,
    READ_CALENDAR,
    READ_EMAIL,
    READ_MEMORY,
    READ_TASKS,
    ROLE_FAMILY,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_UNKNOWN,
    SCOPE_PERSONAL,
    SCOPE_SHARED,
    WRITE_EMAIL,
    WRITE_MEMORY,
    WRITE_REMINDER,
    CapabilityDenied,
    Household,
    build_run_context,
    load_household,
)
from hermes_cloud.runner.tools import REGISTRY, Services, UnknownTool

OWNER = "990000001"
PARENT = "990000002"
NANNY = "990000003"
STRANGER = "990000009"
CHAT = "-100990000101"

HOUSEHOLD = Household(
    household_id="test",
    owner=OWNER,
    family=frozenset({OWNER, PARENT}),
    guests=frozenset({NANNY}),
    allowed_chats=frozenset({CHAT, OWNER}),
)


def context_for(actor: str, *, chat: str = CHAT, thread: int | None = None):
    return build_run_context(
        household=HOUSEHOLD, actor_id=actor, chat_id=chat, thread_id=thread
    )


@pytest.fixture
def services(tmp_path: Path) -> Services:
    from hermes_cloud.execute.gcal import Calendar, FakeCalendar

    return Services.on(
        open_database(tmp_path / "hermes.db"), calendar=Calendar(FakeCalendar())
    )


# --- роли ---------------------------------------------------------------------


def test_roles_come_from_the_household_not_from_the_message() -> None:
    assert HOUSEHOLD.role_for(OWNER) == ROLE_OWNER
    assert HOUSEHOLD.role_for(PARENT) == ROLE_FAMILY
    assert HOUSEHOLD.role_for(NANNY) == ROLE_GUEST
    assert HOUSEHOLD.role_for(STRANGER) == ROLE_UNKNOWN


def test_unknown_actor_has_no_capabilities_at_all() -> None:
    """Не «мало прав», а ноль: неизвестному не показывается ни один tool."""
    context = context_for(STRANGER)

    assert context.role == ROLE_UNKNOWN
    assert context.read_caps == frozenset()
    assert context.mutate_caps == frozenset()
    assert context.has_tools is False
    assert REGISTRY.available(context) == []
    assert REGISTRY.specs(context) == []


def test_guest_sees_tasks_and_calendar_but_changes_nothing() -> None:
    context = context_for(NANNY)

    assert context.can(READ_TASKS) and context.can(READ_CALENDAR)
    assert not context.can(READ_EMAIL), "почта семьи няне не видна"
    assert not context.can(READ_MEMORY), "память семьи няне не видна"
    assert context.mutate_caps == frozenset()


def test_family_can_act_but_not_export_or_delete() -> None:
    context = context_for(PARENT)

    assert context.read_caps == ALL_READ
    assert context.can(WRITE_REMINDER) and context.can(WRITE_MEMORY)
    assert not context.can(DATA_EXPORT), "экспорт — операция владельца"
    assert not context.can(DATA_DELETE), "удаление — операция владельца"


def test_owner_has_everything() -> None:
    context = context_for(OWNER, chat=OWNER)

    assert context.role == ROLE_OWNER
    assert context.read_caps == ALL_READ
    assert context.mutate_caps == ALL_WRITE


def test_known_actor_in_a_foreign_chat_gets_nothing() -> None:
    """Свой человек, написавший из чужого чата, там ничего не может."""
    context = context_for(OWNER, chat="-100777000777")

    assert context.role == ROLE_UNKNOWN
    assert context.has_tools is False


def test_scope_follows_the_chat_kind() -> None:
    assert context_for(PARENT, chat=CHAT).scope == SCOPE_SHARED
    assert context_for(OWNER, chat=OWNER).scope == SCOPE_PERSONAL


def test_require_raises_and_names_the_capability() -> None:
    context = context_for(NANNY)

    with pytest.raises(CapabilityDenied) as denied:
        context.require(WRITE_REMINDER)

    assert denied.value.capability == WRITE_REMINDER
    assert denied.value.role == ROLE_GUEST


def test_run_ids_are_unique_per_turn() -> None:
    assert context_for(PARENT).run_id != context_for(PARENT).run_id


# --- матрица авторизации ------------------------------------------------------

# Ожидаемое право на каждый tool. Таблица ведётся вручную: новый tool обязан
# появиться здесь осознанно, иначе тест ниже упадёт на «не описан в матрице».
EXPECTED_CAPABILITY = {
    "list_reminders": READ_TASKS,
    "list_pending_proposals": READ_TASKS,
    "memory_search": READ_MEMORY,
    "calendar_list_events": READ_CALENDAR,
    "propose_reminder": WRITE_REMINDER,
    "memory_append": WRITE_MEMORY,
    "propose_email": WRITE_EMAIL,
}

VALID_ARGUMENTS = {
    "list_reminders": {"limit": 5},
    "list_pending_proposals": {},
    "memory_search": {"query": "плавание"},
    "calendar_list_events": {"days": 14},
    "propose_reminder": {"text": "оплатить взнос 15 EUR", "due_date": "2026-09-08"},
    "memory_append": {"text": "Лиза ходит на плавание по вторникам", "kind": "routine"},
    "propose_email": {
        "to": "sekretariat@grundschule.example",
        "subject": "Klassenfahrt 3b",
        "body": "Guten Tag, wir nehmen teil.",
    },
}

ROLES = {
    ROLE_OWNER: OWNER,
    ROLE_FAMILY: PARENT,
    ROLE_GUEST: NANNY,
    ROLE_UNKNOWN: STRANGER,
}


def test_every_tool_is_described_in_the_matrix() -> None:
    names = {tool.name for tool in REGISTRY}

    assert names == set(EXPECTED_CAPABILITY), "новый tool не описан в матрице авторизации"
    assert names == set(VALID_ARGUMENTS)


@pytest.mark.parametrize("role,actor", sorted(ROLES.items()))
@pytest.mark.parametrize("tool_name", sorted(EXPECTED_CAPABILITY))
def test_authorization_matrix(role: str, actor: str, tool_name: str, services: Services) -> None:
    """Каждый tool × каждая роль: разрешение или отказ — и никогда «частично»."""
    tool = REGISTRY.get(tool_name)
    context = context_for(actor, chat=OWNER if role == ROLE_OWNER else CHAT)
    allowed = tool.capability in {
        *context.read_caps, *context.mutate_caps
    }

    assert allowed == (tool in REGISTRY.available(context)), "видимость обязана совпадать с правом"

    if allowed:
        REGISTRY.invoke(context, tool_name, VALID_ARGUMENTS[tool_name], services=services)
        return

    with pytest.raises(CapabilityDenied):
        REGISTRY.invoke(context, tool_name, VALID_ARGUMENTS[tool_name], services=services)


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_CAPABILITY))
def test_handler_denies_even_when_called_past_the_registry(
    tool_name: str, services: Services
) -> None:
    """Defense in depth: проверка внутри обработчика — не дубль, а страховка."""
    tool = REGISTRY.get(tool_name)
    context = context_for(STRANGER)

    with pytest.raises(CapabilityDenied):
        tool(context, services, VALID_ARGUMENTS[tool_name])


def test_declared_capability_matches_the_matrix() -> None:
    for name, capability in EXPECTED_CAPABILITY.items():
        assert REGISTRY.get(name).capability == capability


# --- поведение самих tools ----------------------------------------------------


def test_propose_reminder_only_stages_a_proposal(services: Services) -> None:
    """Мутирующий tool ничего не создаёт — он ставит предложение человеку."""
    context = context_for(PARENT)

    result = REGISTRY.invoke(
        context, "propose_reminder", VALID_ARGUMENTS["propose_reminder"], services=services
    )

    assert services.reminders.pending() == [], "напоминание не создаётся до подтверждения"
    staged = services.approvals.get(result["proposal_id"])
    assert staged.status == "staged"
    assert staged.chat == CHAT and staged.actor == PARENT
    assert "code" not in result, "код подтверждения модели не отдаётся"


def test_list_reminders_does_not_leak_another_chat(services: Services) -> None:
    services.reminders.create(chat=CHAT, text="наш взнос", due_at=1.0)
    services.reminders.create(chat="-100777000777", text="чужое дело", due_at=1.0)

    listed = REGISTRY.invoke(context_for(PARENT), "list_reminders", {}, services=services)

    assert [item["text"] for item in listed["reminders"]] == ["наш взнос"]


def test_unknown_tool_is_a_name_error_not_a_permission_error(services: Services) -> None:
    with pytest.raises(UnknownTool):
        REGISTRY.invoke(context_for(OWNER, chat=OWNER), "rm_rf", {}, services=services)


def test_bad_arguments_do_not_stage_anything(services: Services) -> None:
    from hermes_cloud.runner.tools import ToolInputError

    context = context_for(PARENT)

    with pytest.raises(ToolInputError):
        REGISTRY.invoke(
            context, "propose_reminder", {"text": "х", "due_date": "завтра"}, services=services
        )

    assert services.approvals.pending_for(chat=CHAT) == []


# --- household ----------------------------------------------------------------


def test_household_file_wins_over_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "household.toml"
    path.write_text(
        "household_id = \"muc\"\n"
        "[actors]\n"
        f"owner = \"{OWNER}\"\n"
        f"family = [\"{PARENT}\"]\n"
        f"guests = [\"{NANNY}\"]\n"
        "[channels]\n"
        f"allowed_chats = [\"{CHAT}\"]\n",
        encoding="utf-8",
    )

    household = load_household(path, env={"HERMES_OWNER": "someone-else"})

    assert household.household_id == "muc"
    assert household.owner == OWNER
    assert household.role_for(PARENT) == ROLE_FAMILY
    assert household.knows_chat(CHAT)


def test_household_from_environment_keeps_the_owner_in_the_family(tmp_path: Path) -> None:
    household = load_household(
        tmp_path / "absent.toml",
        env={"HERMES_OWNER": OWNER, "HERMES_FAMILY_ACTORS": PARENT, "HERMES_CHAT": CHAT},
    )

    assert household.role_for(OWNER) == ROLE_OWNER
    assert household.role_for(PARENT) == ROLE_FAMILY
    assert household.role_for(NANNY) == ROLE_UNKNOWN


def test_household_without_chats_trusts_no_chat() -> None:
    """Пустой allowlist — «не настроено», а не «разрешено всё»."""
    household = Household(owner=OWNER, family=frozenset({OWNER}))

    assert household.knows_chat(CHAT) is False
    assert build_run_context(
        household=household, actor_id=OWNER, chat_id=CHAT
    ).role == ROLE_UNKNOWN
