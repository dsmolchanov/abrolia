"""Тесты санитайзера фикстур (Gate −1).

Санитайзер — сам по себе часть контроля: если он молча перестанет ловить
реальные значения, публичный репозиторий получит утечку без единого красного
теста. Поэтому каждое правило проверяется и на срабатывание, и на отсутствие
ложных срабатываний на разрешённой синтетике.
"""

from __future__ import annotations

import importlib.util
import re
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


def rules(text: str) -> list[str]:
    return [f.rule for f in checker.scan_text(text, path="probe.txt")]


# В параметрах ниже запрещённые формы обязаны присутствовать буквально —
# иначе тест не проверяет то, ради чего написан. Каждая такая строка помечена
# маркером `check-fixtures: allow` (см. .check-fixtures-allow); значения —
# вымышленные (документационные ключи AWS/Anthropic, несуществующие абоненты).
@pytest.mark.parametrize(
    "text, rule",
    [
        ("to = 'parent@gmail.com'", "email"),  # check-fixtures: allow
        ("to = 'lehrer@grundschule.de'", "email"),  # check-fixtures: allow
        ("phone = '+34 612 345 678'", "phone"),  # check-fixtures: allow
        ("phone = '+7 916 123 45 67'", "phone"),  # check-fixtures: allow
        ("OWNER_CHAT_ID = 123456789", "telegram"),  # check-fixtures: allow
        ('{"from_id": 87654321}', "telegram"),  # check-fixtures: allow
        ("iban = 'DE02120300000000202051'", "iban"),  # check-fixtures: allow
        ("KEY=sk-" + "ant-api03-01234" + "56789abcdefghij", "secret:anthropic-key"),  # check-fixtures: allow
        ("TOKEN='12345" + "67890:AAF1abcdefghijklmnopqrstuvwxyz0123456'", "secret:telegram-bot-token"),  # check-fixtures: allow
        ("aws = 'AKIA" + "IOSFODNN7EXAMPLE'", "secret:aws-access-key"),  # check-fixtures: allow
        ("-----BEGIN RSA PRIVATE KEY-----", "secret:private-key"),  # check-fixtures: allow
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
        "date = '2026-09-12'",
        "amount_cents = 1500",
    ],
)
def test_synthetic_values_pass(text: str) -> None:
    assert rules(text) == []


def test_findings_are_masked_not_echoed() -> None:
    """CI-лог не должен воспроизводить утечку целиком."""
    leak = "parent@gmail.com"  # check-fixtures: allow
    findings = checker.scan_text(f"to = '{leak}'", path="probe.txt")
    assert findings, "адрес на gmail обязан быть найден"
    assert leak not in findings[0].masked
    assert leak not in findings[0].render()


def test_allowlist_suppresses_line() -> None:
    allow = [re.compile(r"schule@example\.de")]
    text = "recipient = 'schule@example.de'"
    assert rules(text) == ["email"]
    assert checker.scan_text(text, path="probe.txt", allow=allow) == []


def test_extra_deny_patterns_are_applied() -> None:
    deny = [re.compile(r"Vollstaendiger Realname")]
    findings = checker.scan_text("parent = 'Vollstaendiger Realname'", path="probe.txt", deny=deny)
    assert [f.rule for f in findings] == ["custom"]


def test_repo_default_paths_are_clean() -> None:
    """Гейт целиком: дефолтные пути репозитория не содержат нарушений."""
    allow = checker.load_patterns(REPO_ROOT / checker.ALLOW_FILE)
    roots = [REPO_ROOT / p for p in checker.DEFAULT_PATHS]
    findings = checker.scan_paths(roots, repo_root=REPO_ROOT, allow=allow, deny=[])
    assert [f.render() for f in findings] == []


def test_shipped_email_fixture_is_synthetic() -> None:
    fixture = REPO_ROOT / "tests" / "fixtures" / "email" / "forwarded_school_de.eml"
    findings = checker.scan_text(fixture.read_text(encoding="utf-8"), path=str(fixture))
    assert [f.render() for f in findings] == []
