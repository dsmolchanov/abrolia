"""Конфиг из окружения и команды рантайма (`worker`, `tick`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud import cli
from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.config import Config, load_config, load_dotenv
from hermes_cloud.core.db import open_database
from hermes_cloud.core.runtime_manifest import compute_config_sha256
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.pipeline import Pipeline


def runtime_manifest(*, residency_mode: str = "eu-app") -> str:
    body = f'''\
schema_version = 1
household_id = "22222222-2222-4222-8222-222222222222"
config_revision = 3
family_language = "čeština"
timezone = "Europe/Prague"
country_code = "CZ"
residency_mode = "{residency_mode}"

[actors]
owner = "owner"
family = ["owner"]
guests = []

[channels]
primary = "telegram"

[[channel_bindings]]
channel = "telegram"
actor_id = "owner"
chat_id = "configured-chat"
verified = true

[email]
agent_inbox = "agent@abrolia.test"
fallback = "owner@example.test"
'''
    digest = compute_config_sha256(body)
    return body.replace("schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n')


def test_config_defaults_are_safe() -> None:
    config = load_config(env={})

    assert config.database_path == Path("data/hermes.db")
    assert config.thread is None
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


def test_versioned_manifest_wins_over_legacy_runtime_environment(tmp_path: Path) -> None:
    path = tmp_path / "household.toml"
    path.write_text(runtime_manifest(), encoding="utf-8")

    config = load_config(
        manifest_path=path,
        env={
            "HERMES_CHAT": "wrong-chat",
            "HERMES_FAMILY_LANGUAGE": "wrong-language",
            "HERMES_GMAIL_ADDRESS": "wrong@example.test",
            "HERMES_GMAIL_APP_PASSWORD": "legacy-secret-canary",
            "HERMES_LEGACY_IMAP_TEST_ONLY": "1",
        },
    )

    assert config.chat == "configured-chat"
    assert config.language == "čeština"
    assert config.gmail_address == "agent@abrolia.test"
    assert config.gmail_app_password is None
    assert not config.has_gmail
    assert config.timezone == "Europe/Prague"
    assert config.country_code == "CZ"
    assert config.config_revision == 3
    assert config.primary_channel == "telegram"


def test_legacy_imap_requires_explicit_test_only_gate() -> None:
    disabled = load_config(
        env={
            "HERMES_GMAIL_ADDRESS": "fixture@example.test",
            "HERMES_GMAIL_APP_PASSWORD": "legacy-secret-canary",
        }
    )
    enabled = load_config(
        env={
            "HERMES_GMAIL_ADDRESS": "fixture@example.test",
            "HERMES_GMAIL_APP_PASSWORD": "legacy-secret-canary",
            "HERMES_LEGACY_IMAP_TEST_ONLY": "1",
        }
    )

    assert disabled.gmail_app_password is None
    assert not disabled.has_gmail
    assert enabled.has_gmail


def test_eu_strict_manifest_fails_without_explicit_provider(tmp_path: Path) -> None:
    path = tmp_path / "household.toml"
    path.write_text(runtime_manifest(residency_mode="eu-strict"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="refusing downgrade"):
        load_config(manifest_path=path, env={})

    config = load_config(
        manifest_path=path, env={"HERMES_VERTEX_EU_ENABLED": "true"}
    )
    assert config.residency_mode == "eu-strict"


def test_validate_manifest_command_is_safe_and_actionable(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "household.toml"
    path.write_text(runtime_manifest(), encoding="utf-8")

    assert cli.main(["validate-manifest", str(path)]) == 0
    output = capsys.readouterr()
    assert "manifest ok" in output.out
    assert "agent@abrolia.test" not in output.out

    path.write_text(runtime_manifest().replace("Europe/Prague", "Invalid/Zone"), encoding="utf-8")
    assert cli.main(["validate-manifest", str(path)]) == 1
    assert "manifest invalid" in capsys.readouterr().err


def test_bootstrap_command_never_accepts_token_in_argv(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    for key in (
        "HERMES_BOOTSTRAP_TOKEN",
        "HERMES_CONTROL_PLANE_URL",
        "HERMES_RUNTIME_REF",
        "HERMES_HOUSEHOLD_ID",
        "HERMES_CONFIG_REVISION",
    ):
        monkeypatch.delenv(key, raising=False)

    assert cli.main(["bootstrap"]) == 1
    error = capsys.readouterr().err
    assert "HERMES_BOOTSTRAP_TOKEN" in error
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["bootstrap", "--token", "must-not-be-supported"])


def test_bootstrap_command_rejects_non_integer_revision_before_network(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setenv("HERMES_BOOTSTRAP_TOKEN", "one-time-secret")
    monkeypatch.setenv("HERMES_CONTROL_PLANE_URL", "https://control.example.test")
    monkeypatch.setenv("HERMES_RUNTIME_REF", "runtime-ref")
    monkeypatch.setenv("HERMES_HOUSEHOLD_ID", "22222222-2222-4222-8222-222222222222")
    monkeypatch.setenv("HERMES_CONFIG_REVISION", "not-an-integer")

    assert cli.main(["bootstrap"]) == 1

    error = capsys.readouterr().err
    assert "HERMES_CONFIG_REVISION" in error
    assert "one-time-secret" not in error


def test_listen_processes_updates_and_remembers_the_offset(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Перезапуск не переигрывает уже обработанные нажатия."""
    from hermes_cloud.core.events import EventStore
    from hermes_cloud.ingest.inject import ingest_file
    from hermes_cloud.runner.card import ACTION_REJECT

    database = open_database(tmp_path / "hermes.db")
    events = EventStore(database)
    ingest_file(events, Path(__file__).resolve().parent / "fixtures" / "email"
                / "direct_invoice_it.eml")
    transport = FakeTransport()
    approvals = ApprovalStore(database)
    pipeline = Pipeline(
        approvals=approvals,
        reminders=ReminderStore(database),
        transport=transport,
        extractor=object(),
        chat="990000001",
    )
    staged = approvals.stage(
        kind="reminder", payload={"kind": "reminder", "text": "x", "due_date": "2026-09-08"},
        chat="990000001", actor="990000001",
    )
    transport.updates = [{
        "update_id": 42,
        "callback_query": {
            "id": "cb", "from": {"id": 990000001},
            "data": f"{ACTION_REJECT}:{staged.id}",
            "message": {"message_id": 1, "chat": {"id": 990000001}},
        },
    }]
    monkeypatch.setenv("HERMES_CHAT", "990000001")
    monkeypatch.setenv("HERMES_FAMILY_ACTORS", "990000001")
    monkeypatch.setattr(cli, "_pipeline", lambda args, db: pipeline)
    monkeypatch.setattr(cli, "_database", lambda args: database)

    assert cli.main(["listen", "--rounds", "1", "--timeout", "0"]) == 0

    assert approvals.get(staged.id).status == "cancelled"
    assert cli._read_offset(database) == 43, "смещение сохранено для следующего запуска"
