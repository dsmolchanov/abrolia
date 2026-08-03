"""Конфиг из окружения и команды рантайма (`worker`, `tick`)."""

from __future__ import annotations

from pathlib import Path

from hermes_cloud import cli
from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.config import Config, load_config, load_dotenv
from hermes_cloud.core.db import open_database
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.pipeline import Pipeline


def test_config_defaults_are_safe() -> None:
    config = load_config(env={})

    assert config.database_path == Path("data/hermes.db")
    assert config.thread is None
    assert config.family_actors == frozenset()
    assert config.has_telegram is False
    assert config.model == "claude-sonnet-5", "решение бенчмарка Фазы 1"
    assert config.effort == "medium"


def test_config_reads_the_environment() -> None:
    config = load_config(env={
        "HERMES_DB": "/tmp/h.db",
        "HERMES_CHAT": "-100990000101",
        "HERMES_THREAD": "7",
        "HERMES_FAMILY_ACTORS": "990000001, 990000002",
        "HERMES_FAMILY_LANGUAGE": "español",
        "HERMES_EXTRACTION_MODEL": "claude-sonnet-5",
        "HERMES_EXTRACTION_EFFORT": "medium",
        "TELEGRAM_BOT_TOKEN": "secret",
    })

    assert config.database_path == Path("/tmp/h.db")
    assert config.thread == 7
    assert config.family_actors == frozenset({"990000001", "990000002"})
    assert config.language == "español"
    assert config.model == "claude-sonnet-5"
    assert config.effort == "medium"
    assert config.has_telegram is True


def test_missing_chat_is_an_explicit_error() -> None:
    config = load_config(env={})
    try:
        config.require_chat()
    except RuntimeError as error:
        assert "HERMES_CHAT" in str(error)
    else:  # pragma: no cover
        raise AssertionError("отсутствие чата обязано быть явной ошибкой")


def test_dotenv_does_not_override_the_environment(tmp_path: Path, monkeypatch) -> None:
    """Fly secrets важнее локального .env — иначе прод возьмёт чужой ключ."""
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_CHAT=from-file\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CHAT", "from-environment")

    load_dotenv(env_file)

    assert load_config().chat == "from-environment"


def test_tick_delivers_due_reminders(tmp_path: Path, monkeypatch, capsys) -> None:
    database = open_database(tmp_path / "hermes.db")
    reminders = ReminderStore(database)
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=reminders,
        transport=transport,
        extractor=object(),  # tick до модели не доходит
        chat="-100990000101",
    )
    reminders.create(chat="-100990000101", text="оплатить 15 EUR", due_at=1.0)
    monkeypatch.setattr(cli, "_pipeline", lambda args, db: pipeline)
    monkeypatch.setattr(cli, "_database", lambda args: database)

    assert cli.main(["tick"]) == 0

    assert "Напоминание: оплатить 15 EUR" in transport.messages[0].text
    assert reminders.pending() == []
    assert "доставлено" in capsys.readouterr().out

    # Повторный tick ничего не дублирует.
    transport.messages.clear()
    assert cli.main(["tick"]) == 0
    assert transport.messages == []


def test_worker_command_reports_processed_events(tmp_path: Path, monkeypatch, capsys) -> None:
    from hermes_cloud.core.events import EventStore
    from hermes_cloud.ingest.inject import ingest_file

    database = open_database(tmp_path / "hermes.db")
    events = EventStore(database)
    fixture = Path(__file__).resolve().parent / "fixtures" / "email" / "direct_invoice_it.eml"
    ingest_file(events, fixture)

    handled: list = []
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=FakeTransport(),
        extractor=object(),
        chat="-100990000101",
    )
    monkeypatch.setattr(pipeline, "handle_event", lambda event: handled.append(event))
    monkeypatch.setattr(cli, "_pipeline", lambda args, db: pipeline)
    monkeypatch.setattr(cli, "_database", lambda args: database)

    assert cli.main(["worker"]) == 0

    assert len(handled) == 1
    assert "ок" in capsys.readouterr().out


def test_config_is_immutable() -> None:
    config = load_config(env={})
    assert isinstance(config, Config)
    try:
        config.chat = "другой"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("конфиг должен быть неизменяемым")
