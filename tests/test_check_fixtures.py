"""Тесты санитайзера фикстур (Gate −1).

Санитайзер — сам по себе часть контроля: если он молча перестанет ловить
реальные значения, публичный репозиторий получит утечку без единого красного
теста. Поэтому каждое правило проверяется и на срабатывание, и на отсутствие
ложных срабатываний, а отдельный блок закрывает найденные ревизией обходы:
формат ключа Nerve, построчное подавление исключением, структурный JSON,
пробелы/регистр в IBAN, `00`-префикс телефона, закодированное тело .eml и
непроверяемые файлы.

Запрещённые образцы собираются из кусков (`joined`): файл теста не должен
выглядеть как утечка и не должен требовать широких исключений.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    path = REPO_ROOT / "scripts" / "check_fixtures.py"
    spec = importlib.util.spec_from_file_location("check_fixtures", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_fixtures"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def joined(*parts: str) -> str:
    """Собрать запрещённый образец так, чтобы он не лежал в файле целиком."""
    return "".join(parts)


def rules(text: str) -> list[str]:
    return [f.rule for f in checker.scan_text(text, path="probe.txt")]


GMAIL = joined("parent@", "gmail.", "com")
SCHOOL_DE = joined("lehrer@", "grundschule.", "de")
SCHULE = joined("schule@", "example.", "de")  # значение из .check-fixtures-allow
CHAT_ID = joined("12345", "6789")
FROM_ID = joined("8765", "4321")
BUILD_NUMBER = joined("2026", "0802")
IBAN_SPACED = joined("DE02 1203 ", "0000 0000 2020 51")
NERVE_SAMPLE = joined("nrv_", "live_", "0123456789abcdef" * 4)  # формат генератора: 64 hex
ANTHROPIC_SAMPLE = joined("sk-", "ant-", "api03-", "01234", "56789", "abcdefghij")
TELEGRAM_SAMPLE = joined("12345", "67890", ":", "AAF1abcdefghijklmnopqrstuvwxyz01234")
AWS_SAMPLE = joined("AKIA", "IOSFODNN7EXAMPLE")
REAL_IBAN = joined("DE02", "1203", "0000", "0000", "2020", "51")


@pytest.mark.parametrize(
    "text, rule",
    [
        (f"to = '{GMAIL}'", "email"),
        (f"to = '{SCHOOL_DE}'", "email"),
        (joined("phone = '+34 ", "612 345 678'"), "phone"),
        (joined("phone = '+7 ", "916 123 45 67'"), "phone"),
        (joined("phone = '0034", "612345678'"), "phone"),
        # форматированный префикс 00 и неразрывные пробелы (вставка из Word/Outlook)
        (joined("phone = '0034 ", "612 345 678'"), "phone"),
        (joined("phone = '+34 ", "612 345 678'"), "phone"),
        (joined("iban = 'DE02 1203 ", "0000 0000 2020 51'"), "iban"),
        (f"OWNER_CHAT_ID = {CHAT_ID}", "telegram"),
        (f'{{"from_id": {FROM_ID}}}', "telegram"),
        (f"iban = '{REAL_IBAN}'", "iban"),
        (f"KEY={ANTHROPIC_SAMPLE}", "secret:anthropic-key"),
        (f"NERVE_API_KEY={NERVE_SAMPLE}", "secret:nerve-key"),
        (f"TOKEN='{TELEGRAM_SAMPLE}'", "secret:telegram-bot-token"),
        (f"aws = '{AWS_SAMPLE}'", "secret:aws-access-key"),
        (joined("-----BEGIN ", "RSA PRIVATE KEY-----"), "secret:private-key"),
    ],
)
def test_forbidden_values_are_caught(text: str, rule: str) -> None:
    assert rule in rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "to = 'sekretariat@grundschule.example'",
        "to = 'anna.beispiel@example.com'",
        "phone = '+999015550123'",
        "phone = '+1 202 555 0143'",
        "OWNER_CHAT_ID = 990000001",
        '{"chat_id": -100990000101}',
        "iban = 'DE89370400440532013000'",
        "iban = 'de89 3704 0044 0532 0130 00'",
        "date = '2026-09-12'",
        "amount_cents = 1500",
        "commit = 'c26fc414ef02d259b2a22069eaddbff5314fd4c1'",
    ],
)
def test_synthetic_values_pass(text: str) -> None:
    assert rules(text) == []


def test_findings_are_masked_not_echoed() -> None:
    """CI-лог не должен воспроизводить утечку целиком."""
    findings = checker.scan_text(f"to = '{GMAIL}'", path="probe.txt")
    assert findings, "адрес на gmail обязан быть найден"
    assert GMAIL not in findings[0].masked
    assert GMAIL not in findings[0].render()


# --- обходы, найденные ревизией Gate −1 -------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        joined("nrv_", "live_", "0123456789abcdef" * 4),
        joined("nrv_", "test_", "fedcba9876543210" * 4),
        joined("nrv_", "0123456789abcdefghijklmn"),
    ],
)
def test_nerve_key_formats_are_caught(key: str) -> None:
    """Генератор Nerve выдаёт `nrv_live_` + 64 hex — второй `_` ломал старое правило."""
    assert "secret:nerve-key" in rules(f"key = '{key}'")


def test_allow_entry_does_not_disable_the_rest_of_the_line() -> None:
    """Исключение снимает одну находку, а не всю строку."""
    allow = [checker.AllowEntry("probe.txt", "email", SCHULE)]
    text = f"to = '{SCHULE}'; key = '{ANTHROPIC_SAMPLE}'"
    found = checker.scan_text(text, path="probe.txt", allow=allow)
    assert [f.rule for f in found] == ["secret:anthropic-key"]


def test_allow_entry_is_scoped_to_path_rule_and_value() -> None:
    allow = [checker.AllowEntry("docs/*.md", "email", SCHULE)]
    assert checker.scan_text(f"a = '{SCHULE}'", path="docs/x.md", allow=allow) == []
    # другой путь
    assert rules_with("tests/x.py", f"a = '{SCHULE}'", allow) == ["email"]
    # другое значение того же правила
    assert rules_with("docs/x.md", f"a = '{GMAIL}'", allow) == ["email"]


def rules_with(path: str, text: str, allow) -> list[str]:
    return [f.rule for f in checker.scan_text(text, path=path, allow=allow)]


def test_allow_cannot_suppress_private_deny() -> None:
    import re

    secret_name = joined("Vollstaendiger", " Realname")
    deny = [re.compile(secret_name)]
    allow = [checker.AllowEntry("probe.txt", "email", SCHULE)]
    text = f"parent = '{secret_name}' # {SCHULE}"
    found = checker.scan_text(text, path="probe.txt", allow=allow, deny=deny)
    assert "custom" in [f.rule for f in found]


def test_allow_file_rejects_custom_rule(tmp_path: Path) -> None:
    allow_file = tmp_path / checker.ALLOW_FILE
    allow_file.write_text("probe.txt | custom | whatever\n", encoding="utf-8")
    with pytest.raises(checker.AllowFileError):
        checker.load_allow(allow_file)


def test_allow_file_rejects_two_field_entries(tmp_path: Path) -> None:
    allow_file = tmp_path / checker.ALLOW_FILE
    allow_file.write_text("probe.txt | email\n", encoding="utf-8")
    with pytest.raises(checker.AllowFileError):
        checker.load_allow(allow_file)


def test_telegram_id_is_caught_across_lines() -> None:
    """В webhook-JSON ключ `"from"` и `"id"` стоят на разных строках."""
    payload = '{\n  "message": {\n    "from": {\n      "id": ' + CHAT_ID + ",\n"
    assert "telegram" in rules(payload)


def test_telegram_id_is_caught_when_context_follows_the_value() -> None:
    """Обратный порядок в JSON: значение раньше ключа контекста."""
    payload = '{\n  "id": ' + CHAT_ID + ',\n  "is_bot": false\n}, "chat": {\n'
    assert "telegram" in rules(payload)


def test_telegram_context_does_not_leak_far_down_the_file() -> None:
    payload = '{"chat_id": 990000001}\n' + "\n" * 8 + f"build_number = {BUILD_NUMBER}\n"
    assert rules(payload) == []


@pytest.mark.parametrize(
    "text",
    [
        f"iban = '{IBAN_SPACED}'",
        f"iban = '{IBAN_SPACED.lower()}'",
        f"Bitte auf {IBAN_SPACED} ueberweisen, Verwendungszweck Klasse 3b",
    ],
)
def test_iban_is_caught_with_spaces_and_case(text: str) -> None:
    assert "iban" in rules(text)


def test_iban_rule_ignores_checksum_invalid_noise() -> None:
    """Проверка контрольной суммы отсекает случайные буквенно-цифровые строки."""
    assert rules("iban_like = 'AB12CDEFGHIJKLMNOPQR'") == []


def test_eml_body_is_decoded_before_scanning(tmp_path: Path) -> None:
    body = base64.b64encode(f"Bitte antworten an {GMAIL}\n".encode()).decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "Subject: Test\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{body}\n"
    )
    path = tmp_path / "encoded.eml"
    path.write_text(eml, encoding="utf-8")
    scannable = checker.read_scannable(path)
    assert scannable.opaque_parts == ()
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="encoded.eml")]


def _eml_with_pdf_attachment(text_body: str) -> str:
    """Письмо с текстовой частью и нечитаемым PDF-вложением."""
    pdf = base64.b64encode(b"%PDF-1.4\n\x00\x01\x02binary-\xff\xfe-payload\n").decode()
    return (
        "From: Anna Beispiel <anna@example.com>\n"
        "Subject: Klassenfahrt\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n\n'
        f"{text_body}\n\n"
        "--b1\n"
        "Content-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="einladung.pdf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{pdf}\n"
        "--b1--\n"
    )


def test_opaque_attachment_does_not_hide_the_rest_of_the_email(tmp_path: Path) -> None:
    """Одна бинарная часть не должна выключать проверку письма целиком."""
    fixtures = tmp_path / "tests" / "fixtures" / "email"
    fixtures.mkdir(parents=True)
    (fixtures / "with_pdf.eml").write_text(
        _eml_with_pdf_attachment(f"Bitte antworten an {GMAIL}"), encoding="utf-8"
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert sorted({f.rule for f in findings}) == ["email", "unscannable"]


def test_latin1_8bit_email_is_still_scanned(tmp_path: Path) -> None:
    """Кодировка тела не должна выводить письмо из-под проверки целиком."""
    body = f"Grüße, bitte antworten an {GMAIL}\n".encode("iso-8859-1")
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "Subject: Test\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/plain; charset="iso-8859-1"\n'
        "Content-Transfer-Encoding: 8bit\n\n"
    ).encode("ascii") + body
    path = tmp_path / "latin1.eml"
    path.write_bytes(eml)
    scannable = checker.read_scannable(path)
    assert scannable.opaque_parts == ()
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="latin1.eml")]


def test_large_email_is_still_scanned(tmp_path: Path) -> None:
    """Письмо, распухшее от вложения, не должно терять текстовую часть."""
    filler = base64.b64encode(b"x" * (checker.MAX_FILE_BYTES + 1000)).decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "Subject: Anhang\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n\n'
        f"Bitte antworten an {GMAIL}\n\n"
        "--b1\n"
        "Content-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="gross.pdf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{filler}\n"
        "--b1--\n"
    )
    path = tmp_path / "gross.eml"
    path.write_text(eml, encoding="utf-8")
    scannable = checker.read_scannable(path)
    assert [checker.opaque_part_name(p) for p in scannable.opaque_parts] == ["gross.pdf"]
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="gross.eml")]


def test_ascii_pdf_attachment_is_opaque_by_type(tmp_path: Path) -> None:
    """Успешный decode не делает PDF текстом — решает объявленный MIME-тип."""
    ascii_pdf = base64.b64encode(b"%PDF-1.4 plain ascii payload").decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n\n'
        "Anbei die Einladung.\n\n"
        "--b1\n"
        "Content-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="ascii.pdf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{ascii_pdf}\n"
        "--b1--\n"
    )
    path = tmp_path / "ascii_pdf.eml"
    path.write_text(eml, encoding="utf-8")
    assert [checker.opaque_part_name(p) for p in checker.read_scannable(path).opaque_parts] == [
        "ascii.pdf"
    ]


def _read(tmp_path: Path, name: str, eml: str | bytes):
    path = tmp_path / name
    if isinstance(eml, bytes):
        path.write_bytes(eml)
    else:
        path.write_text(eml, encoding="utf-8")
    return checker.read_scannable(path)


def test_nested_base64_email_is_parsed(tmp_path: Path) -> None:
    """message/rfc822 в base64 раньше проходил как multipart-обёртка."""
    inner = (
        f"From: Lehrer <{GMAIL}>\n"
        "Subject: Weiterleitung\n"
        'Content-Type: text/plain; charset="utf-8"\n\n'
        "Bitte um Rueckmeldung.\n"
    )
    nested = base64.b64encode(inner.encode()).decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n\n'
        "Siehe Anhang.\n\n"
        "--b1\n"
        "Content-Type: message/rfc822\n"
        "Content-Transfer-Encoding: base64\n\n"
        f"{nested}\n"
        "--b1--\n"
    )
    scannable = _read(tmp_path, "nested.eml", eml)
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="nested.eml")]


def test_unknown_transfer_encoding_is_opaque(tmp_path: Path) -> None:
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        "Content-Transfer-Encoding: x-base64\n"
        'Content-Disposition: attachment; filename="mystery.txt"\n\n'
        f"{base64.b64encode(GMAIL.encode()).decode()}\n"
        "--b1--\n"
    )
    scannable = _read(tmp_path, "cte.eml", eml)
    assert [checker.opaque_part_name(x) for x in scannable.opaque_parts] == ["mystery.txt"]


def test_part_headers_are_scanned(tmp_path: Path) -> None:
    """Адрес умеет прятаться в заголовке вложения, а не только в корневых."""
    encoded = base64.b64encode(f"Kontakt {GMAIL}".encode()).decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        f"Content-Description: =?utf-8?B?{encoded}?=\n\n"
        "Kein Text.\n"
        "--b1--\n"
    )
    scannable = _read(tmp_path, "hdr.eml", eml)
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="hdr.eml")]


def test_html_entities_are_normalised(tmp_path: Path) -> None:
    local, domain = GMAIL.split("@")
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: text/html; charset="utf-8"\n\n'
        f"<p>Kontakt: <b>{local}</b>&#64;{domain}</p>\n"
    )
    scannable = _read(tmp_path, "html.eml", eml)
    assert "email" in [f.rule for f in checker.scan_scannable(scannable, path="html.eml")]


def test_json_unicode_escape_is_normalised() -> None:
    local, domain = GMAIL.split("@")
    payload = '{"to": "' + local + "\\u0040" + domain + '"}'
    assert "email" in rules(checker.normalize_text(payload))


def test_rtf_attachment_is_opaque(tmp_path: Path) -> None:
    """RTF прячет содержимое в hex-escape, парсера у нас нет."""
    rtf = base64.b64encode(rb"{\rtf1 Kontakt: parent\'40gmail.com}").decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        "Content-Type: application/rtf\n"
        'Content-Disposition: attachment; filename="brief.rtf"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{rtf}\n"
        "--b1--\n"
    )
    scannable = _read(tmp_path, "rtf.eml", eml)
    assert [checker.opaque_part_name(x) for x in scannable.opaque_parts] == ["brief.rtf"]


def test_pdf_declared_as_text_is_opaque(tmp_path: Path) -> None:
    """Тип может врать — сигнатура байтов решает."""
    pdf = base64.b64encode(b"%PDF-1.4 fake but declared as text").decode()
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        'Content-Disposition: attachment; filename="liar.txt"\n'
        "Content-Transfer-Encoding: base64\n\n"
        f"{pdf}\n"
        "--b1--\n"
    )
    scannable = _read(tmp_path, "liar.eml", eml)
    assert [checker.opaque_part_name(x) for x in scannable.opaque_parts] == ["liar.txt"]


def _scan_fixture_file(tmp_path: Path, name: str, content) -> list[str]:
    """Прогнать один файл через настоящий гейт (scan_paths), а не через helper."""
    fixtures = tmp_path / "tests" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    target = fixtures / name
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return [f.rule for f in checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)]


def test_json_escape_in_plain_file_is_caught(tmp_path: Path) -> None:
    """Нормализация обязана работать и вне писем — файл фикстуры это файл."""
    local, domain = GMAIL.split("@")
    payload = '{"to": "' + local + "\\u0040" + domain + '"}\n'
    assert "email" in _scan_fixture_file(tmp_path, "webhook.json", payload)


def test_html_entity_in_plain_file_is_caught(tmp_path: Path) -> None:
    local, domain = GMAIL.split("@")
    page = f"<p>Kontakt: <b>{local}</b>&#64;{domain}</p>\n"
    assert "email" in _scan_fixture_file(tmp_path, "page.html", page)


def test_ascii_rtf_file_is_opaque(tmp_path: Path) -> None:
    rtf = rb"{\rtf1 Kontakt: parent\'40gmail.com}"
    assert _scan_fixture_file(tmp_path, "brief.rtf", rtf) == ["unscannable"]


def test_pdf_with_text_extension_is_opaque(tmp_path: Path) -> None:
    assert _scan_fixture_file(tmp_path, "liar.txt", b"%PDF-1.4 plain ascii payload") == [
        "unscannable"
    ]


def test_corrupted_base64_part_is_opaque(tmp_path: Path) -> None:
    """stdlib не бросает исключение: он возвращает огрызок и пишет дефект."""
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        'Content-Type: text/plain; charset="utf-8"\n'
        'Content-Disposition: attachment; filename="broken.txt"\n'
        "Content-Transfer-Encoding: base64\n\n"
        "%%%%\n"
        "--b1--\n"
    )
    path = tmp_path / "broken.eml"
    path.write_text(eml, encoding="utf-8")
    scannable = checker.read_scannable(path)
    assert [checker.opaque_part_name(x) for x in scannable.opaque_parts] == ["broken.txt"]


def test_corrupted_base64_nested_email_is_opaque(tmp_path: Path) -> None:
    eml = (
        "From: Anna Beispiel <anna@example.com>\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="b1"\n\n'
        "--b1\n"
        "Content-Type: message/rfc822\n"
        'Content-Disposition: attachment; filename="inner.eml"\n'
        "Content-Transfer-Encoding: base64\n\n"
        "!!!!not base64!!!!\n"
        "--b1--\n"
    )
    path = tmp_path / "broken_nested.eml"
    path.write_text(eml, encoding="utf-8")
    scannable = checker.read_scannable(path)
    assert scannable.opaque_parts, "повреждённое вложенное письмо обязано быть непрозрачным"


def test_tilde_line_inside_backtick_fence_does_not_open_entries(tmp_path: Path) -> None:
    """Смешанные fence: `~~~` внутри ```-блока не выводит парсер наружу."""
    fixtures = tmp_path / "tests" / "fixtures" / "media"
    fixtures.mkdir(parents=True)
    (fixtures / "notice.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    (tmp_path / "tests" / "fixtures" / "PROVENANCE.md").write_text(
        "Пример формата:\n\n"
        "```\n"
        "~~~\n"
        "- `media/notice.png` — это пример внутри блока кода, а не запись\n"
        "```\n",
        encoding="utf-8",
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["unscannable"]


def test_provenance_needs_a_structured_entry(tmp_path: Path) -> None:
    """Упоминание имени в прозе или в примере не является записью о файле."""
    fixtures = tmp_path / "tests" / "fixtures" / "media"
    fixtures.mkdir(parents=True)
    (fixtures / "notice.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    manifest = tmp_path / "tests" / "fixtures" / "PROVENANCE.md"

    manifest.write_text(
        "Формат записи:\n\n```\n- `notice.png` — пример записи\n```\n\n"
        "В прозе тоже можно упомянуть notice.png — это не запись.\n",
        encoding="utf-8",
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["unscannable"]

    manifest.write_text(
        "- `media/notice.png` — сгенерирован scripts/make_media.py\n", encoding="utf-8"
    )
    assert checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path) == []


def test_provenance_for_the_email_does_not_cover_its_attachment(tmp_path: Path) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "email"
    fixtures.mkdir(parents=True)
    (fixtures / "with_pdf.eml").write_text(
        _eml_with_pdf_attachment(f"Bitte antworten an {GMAIL}"), encoding="utf-8"
    )
    (fixtures / "PROVENANCE.md").write_text(
        "- `with_pdf.eml` — синтетическое письмо\n", encoding="utf-8"
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert sorted({f.rule for f in findings}) == ["email", "unscannable"]

    # Запись о вложении связывает его с конкретным письмом.
    (fixtures / "PROVENANCE.md").write_text(
        "- `with_pdf.eml#einladung.pdf` — сгенерирован scripts/make_pdf.py, синтетика\n",
        encoding="utf-8",
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["email"]


def test_attachment_entry_does_not_cover_the_same_name_in_another_email(
    tmp_path: Path,
) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "email"
    fixtures.mkdir(parents=True)
    for name in ("first.eml", "second.eml"):
        (fixtures / name).write_text(_eml_with_pdf_attachment("Anbei."), encoding="utf-8")
    (fixtures / "PROVENANCE.md").write_text(
        "- `first.eml#einladung.pdf` — синтетика\n", encoding="utf-8"
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [(f.path, f.rule) for f in findings] == [
        ("tests/fixtures/email/second.eml", "unscannable")
    ]


def test_opaque_attachment_name_is_masked_in_output(tmp_path: Path) -> None:
    """Имя вложения само может быть персональными данными."""
    fixtures = tmp_path / "tests" / "fixtures" / "email"
    fixtures.mkdir(parents=True)
    eml = _eml_with_pdf_attachment("Anbei.").replace(
        "einladung.pdf", "Krankmeldung Erika Mustermann.pdf"
    )
    (fixtures / "sick.eml").write_text(eml, encoding="utf-8")
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["unscannable"]
    assert "Krankmeldung Erika Mustermann.pdf" not in findings[0].render()
    assert "application/pdf" in findings[0].render()


def test_binary_fixture_without_provenance_is_a_finding(tmp_path: Path) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "media"
    fixtures.mkdir(parents=True)
    (fixtures / "notice.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["unscannable"]


def test_binary_fixture_with_provenance_passes(tmp_path: Path) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "media"
    fixtures.mkdir(parents=True)
    (fixtures / "notice.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    (tmp_path / "tests" / "fixtures" / "PROVENANCE.md").write_text(
        "- `media/notice.png` — сгенерирован scripts/make_media.py, синтетика\n",
        encoding="utf-8",
    )
    assert checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path) == []


@pytest.mark.parametrize(
    "entry",
    [
        "- `other/notice.png` — запись про файл в другом каталоге",
        "- `notice.png` — запись без каталога",
        "- `../notice.png` — выход за пределы каталога манифеста",
        "- `/media/notice.png` — абсолютный путь",
        "    - `media/notice.png` — отступ в четыре пробела: это блок кода",
    ],
)
def test_provenance_entry_must_match_the_exact_path(tmp_path: Path, entry: str) -> None:
    fixtures = tmp_path / "tests" / "fixtures" / "media"
    fixtures.mkdir(parents=True)
    (fixtures / "notice.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00binary")
    (tmp_path / "tests" / "fixtures" / "PROVENANCE.md").write_text(
        entry + "\n", encoding="utf-8"
    )
    findings = checker.scan_paths([tmp_path / "tests"], repo_root=tmp_path)
    assert [f.rule for f in findings] == ["unscannable"]


def test_require_deny_fails_without_private_patterns(monkeypatch, capsys) -> None:
    monkeypatch.delenv(checker.EXTRA_DENY_ENV, raising=False)
    code = checker.main(["--all", "--require-deny", "--repo-root", str(REPO_ROOT)])
    assert code == 2
    assert "require-deny" in capsys.readouterr().err


def test_require_deny_passes_with_private_patterns(monkeypatch, tmp_path: Path) -> None:
    deny_file = tmp_path / "deny.txt"
    # Паттерн не должен встречаться в репозитории буквально — иначе он найдёт
    # сам себя в этом тесте (проверка идёт по всему дереву).
    deny_file.write_text(f"# comment\n{joined('SomeReal', 'DonorName')}\n", encoding="utf-8")
    monkeypatch.setenv(checker.EXTRA_DENY_ENV, str(deny_file))
    assert checker.main(["--all", "--require-deny", "--repo-root", str(REPO_ROOT)]) == 0


# --- гейт репозитория --------------------------------------------------------


def test_repo_is_clean_including_prose() -> None:
    """CI гоняет --all: код, фикстуры, docs/ и thoughts/."""
    allow = checker.load_allow(REPO_ROOT / checker.ALLOW_FILE)
    findings = checker.scan_paths([REPO_ROOT], repo_root=REPO_ROOT, allow=allow)
    assert [f.render() for f in findings] == []


def test_shipped_email_fixture_is_synthetic() -> None:
    fixture = REPO_ROOT / "tests" / "fixtures" / "email" / "forwarded_school_de.eml"
    scannable = checker.read_scannable(fixture)
    assert scannable.opaque_parts == ()
    assert [f.render() for f in checker.scan_scannable(scannable, path=str(fixture))] == []
