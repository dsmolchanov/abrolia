"""Разбор пересланных цепочек: кто на самом деле отправил письмо.

Ошибка здесь стоит дорого: ответ уйдёт родителю вместо школы, а карточка
покажет не того получателя. Поэтому проверяются все три способа оформления
пересылки и отдельно — прямое письмо, где никакой цепочки нет.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.eml import external_id, parse_eml
from hermes_cloud.ingest.inject import ingest_bytes, ingest_file

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "email"


def load(name: str):
    return parse_eml((FIXTURES / name).read_bytes())


@pytest.mark.parametrize(
    "fixture, sender, method",
    [
        ("forwarded_school_de.eml", "sekretariat@grundschule.example", "marker"),
        ("outlook_forward_de.eml", "klassenleitung@grundschule.example", "marker"),
        ("apple_forward_nl.eml", "administratie@basisschool.example", "marker"),
        ("attachment_forward_fr.eml", "secretariat@ecole.example", "attachment"),
    ],
)
def test_original_sender_is_extracted(fixture: str, sender: str, method: str) -> None:
    parsed = load(fixture)

    assert parsed.is_forwarded, "пересылка не распознана"
    assert parsed.original_sender.email == sender
    assert parsed.original_sender.method == method
    # Отправитель самого письма — родитель, и его нельзя путать с оригиналом.
    assert parsed.from_email != sender


def test_forwarded_subject_and_date_are_kept() -> None:
    parsed = load("apple_forward_nl.eml")
    original = parsed.original_sender

    assert original.subject == "Schoolreis 15 oktober - eigen bijdrage"
    assert original.date is not None and "2026" in original.date


def test_attachment_forward_is_the_most_confident() -> None:
    """У вложения настоящие заголовки — гадать не о чем."""
    attachment = load("attachment_forward_fr.eml").original_sender
    inline = load("forwarded_school_de.eml").original_sender

    assert attachment.confidence > inline.confidence


def test_direct_letter_has_no_original_sender() -> None:
    parsed = load("direct_invoice_it.eml")

    assert parsed.is_forwarded is False
    assert parsed.original_sender is None
    assert parsed.from_email == "amministrazione@scuola.example"


def test_body_text_of_forwarded_letter_is_available() -> None:
    parsed = load("forwarded_school_de.eml")

    assert "Klassenfahrt" in parsed.text
    assert "15,00 EUR" in parsed.text


def test_subject_only_forward_is_marked_as_weak() -> None:
    """Клиент не оставил маркера — принимаем, но помечаем низкой уверенностью."""
    raw = (
        b"From: Anna Beispiel <anna@example.com>\n"
        b"Subject: Fwd: Schwimmunterricht\n"
        b"Message-ID: <weak@mail.example.com>\n"
        b'Content-Type: text/plain; charset="utf-8"\n\n'
        b"Von: Sportlehrer <sport@grundschule.example>\n"
        b"Betreff: Schwimmunterricht\n\n"
        b"Bitte Badesachen mitgeben.\n"
    )
    original = parse_eml(raw).original_sender

    assert original.email == "sport@grundschule.example"
    assert original.method == "subject-only"
    assert original.confidence < 0.6


def test_html_only_letter_is_readable() -> None:
    raw = (
        b"From: Schule <info@grundschule.example>\n"
        b"Subject: Hinweis\n"
        b"Message-ID: <html@mail.example.com>\n"
        b'Content-Type: text/html; charset="utf-8"\n\n'
        b"<html><body><p>Beitrag: <b>15,00 EUR</b></p></body></html>\n"
    )
    parsed = parse_eml(raw)

    assert "15,00 EUR" in parsed.text
    assert "<b>" not in parsed.text


def test_external_id_prefers_message_id_and_falls_back_to_hash() -> None:
    parsed = load("direct_invoice_it.eml")
    raw = (FIXTURES / "direct_invoice_it.eml").read_bytes()
    assert external_id(parsed, raw) == "eml:DIR0006synthetic@mail.example.com"

    headerless = b"Subject: kein Message-ID\n\nText\n"
    generated = external_id(parse_eml(headerless), headerless)
    assert generated.startswith("eml:sha256:")


def test_ingest_is_idempotent_and_uses_thread_as_context(tmp_path: Path) -> None:
    store = EventStore(open_database(tmp_path / "hermes.db"))
    path = FIXTURES / "forwarded_school_de.eml"

    first = ingest_file(store, path)
    second = ingest_bytes(store, path.read_bytes())

    assert first.created is True
    assert second.created is False
    assert second.event_id == first.event_id
    event = store.get(first.event_id)
    assert event.context_key == first.parsed.thread_key
    assert event.raw == path.read_bytes()


def test_attachment_metadata_is_recorded_without_storing_files() -> None:
    parsed = load("attachment_forward_fr.eml")
    # Вложенное письмо учитывается как цепочка, а не как файл на диске.
    assert parsed.nested_messages and parsed.attachments == ()
