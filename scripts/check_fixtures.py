#!/usr/bin/env python3
"""Gate -1 fixture sanitiser.

Запрещает попадание реальных персональных данных в код и фикстуры публичного
репозитория. Донорские payload-фикстуры (`~/Programs/hermes`) содержат реальные
Telegram ID, имена и адреса — их нельзя копировать as-is (см. CONTRIBUTING.md).

Правила (все — fail на находке):

* ``email``      — адрес вне зарезервированных доменов документации (RFC 2606:
                   example.com/net/org/edu, TLD .example/.invalid/.test/.localhost).
                   Ловит в том числе @gmail.com из донорских фикстур.
* ``phone``      — E.164-номер вне зарезервированных диапазонов (+999… по
                   ITU-T E.164 и NANP 555-01xx). Ловит реальные +7/+34 номера.
* ``telegram``   — числовой ID рядом с telegram-контекстом вне синтетического
                   диапазона (пользователь ``99XXXXXXX``, супергруппа ``-10099XXXXXXX``).
* ``iban``       — IBAN вне списка общеизвестных примеров.
* ``secret``     — форма ключа/токена (Anthropic, Telegram bot, GitHub, AWS,
                   Google OAuth, PEM private key). Дублирует gitleaks как быстрый
                   локальный барьер.
* ``custom``     — паттерны из приватного файла (env ``HERMES_EXTRA_DENY_FILE``):
                   там держим реальные имена/ID донора, которые нельзя коммитить
                   даже в виде правила.

Исключения — построчные regex в ``.check-fixtures-allow`` (по одному на строку,
``#`` — комментарий). Находки печатаются замаскированными: CI-лог не должен
становиться новой утечкой.

Использование::

    python scripts/check_fixtures.py             # дефолтные пути (код и фикстуры)
    python scripts/check_fixtures.py --all       # всё дерево, включая docs/ и thoughts/
    python scripts/check_fixtures.py --paths tests/fixtures --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Пути со «строгим» режимом: код и фикстуры. Проза (docs/, thoughts/, README)
# проверяется только с --all: там встречаются иллюстративные адреса на
# незарезервированных доменах (заводятся в .check-fixtures-allow поимённо),
# но не секреты — их ловит gitleaks по всей истории.
DEFAULT_PATHS = ("tests", "bench", "scripts", "hermes_cloud", "onboarding")

ALLOW_FILE = ".check-fixtures-allow"
EXTRA_DENY_ENV = "HERMES_EXTRA_DENY_FILE"

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".mypy_cache", "data", "dist", "build",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tgz", ".ico",
    ".ogg", ".oga", ".mp3", ".mp4", ".wav", ".m4a", ".sqlite", ".db", ".woff",
    ".woff2", ".ttf", ".pyc", ".so", ".dylib",
}
MAX_FILE_BYTES = 2_000_000

# --- разрешённые синтетические значения ------------------------------------

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.edu"}
ALLOWED_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")

# ITU-T E.164: код +999 зарезервирован; NANP: 555-0100..555-0199 — вымышленные.
SYNTHETIC_PHONE_RE = re.compile(r"^(?:999\d{0,12}|1\d{3}55501\d{2})$")

# Синтетические Telegram ID: пользователь 99 + 7 цифр, супергруппа -10099 + 7 цифр,
# legacy-группа -99 + 7 цифр.
SYNTHETIC_TELEGRAM_RE = re.compile(r"^(?:99\d{7}|-99\d{7}|-10099\d{7})$")

ALLOWED_IBANS = {
    "DE89370400440532013000",
    "NL91ABNA0417164300",
    "GB82WEST12345698765432",
    "IT60X0542811101000000123456",
}

# --- регулярки поиска -------------------------------------------------------

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\+\d[\d\s().\-]{5,17}\d")
TELEGRAM_CONTEXT_RE = re.compile(
    r"(telegram|tg_id|chat_id|chat-id|chatId|user_id|userId|from_id|sender_id|from_user)",
    re.IGNORECASE,
)
INT_RE = re.compile(r"-?\d{6,}")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")

SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9\-_]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule: str
    detail: str
    masked: str

    def render(self) -> str:
        return f"{self.path}:{self.line_no}: [{self.rule}] {self.detail}: {self.masked}"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line_no,
            "rule": self.rule,
            "detail": self.detail,
            "masked": self.masked,
        }


def mask(value: str) -> str:
    """Замаскировать находку: CI-лог не должен воспроизводить утечку."""
    value = value.strip()
    if len(value) <= 4:
        return "*" * len(value)
    keep = 2 if len(value) < 12 else 3
    return f"{value[:keep]}{'*' * (len(value) - 2 * keep)}{value[-keep:]}"


def _email_domain_allowed(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    if domain in ALLOWED_EMAIL_DOMAINS:
        return True
    if any(domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
        return True
    return domain.endswith(ALLOWED_EMAIL_TLDS)


def check_email(line: str) -> Iterator[tuple[str, str]]:
    for match in EMAIL_RE.finditer(line):
        address = match.group(0)
        domain = address.rsplit("@", 1)[1]
        if not _email_domain_allowed(domain):
            yield "email", address


def check_phone(line: str) -> Iterator[tuple[str, str]]:
    for match in PHONE_RE.finditer(line):
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7:
            continue
        if not SYNTHETIC_PHONE_RE.match(digits):
            yield "phone", raw


def check_telegram(line: str) -> Iterator[tuple[str, str]]:
    if not TELEGRAM_CONTEXT_RE.search(line):
        return
    for match in INT_RE.finditer(line):
        value = match.group(0)
        if not SYNTHETIC_TELEGRAM_RE.match(value):
            yield "telegram", value


def check_iban(line: str) -> Iterator[tuple[str, str]]:
    for match in IBAN_RE.finditer(line):
        value = match.group(0)
        if value not in ALLOWED_IBANS:
            yield "iban", value


def check_secret(line: str) -> Iterator[tuple[str, str]]:
    for name, pattern in SECRET_RES:
        for match in pattern.finditer(line):
            yield f"secret:{name}", match.group(0)


CHECKS: tuple[Callable[[str], Iterator[tuple[str, str]]], ...] = (
    check_email,
    check_phone,
    check_telegram,
    check_iban,
    check_secret,
)

RULE_DETAIL = {
    "email": "адрес вне зарезервированных доменов документации",
    "phone": "телефон вне зарезервированных диапазонов (+999…, 555-01xx)",
    "telegram": "Telegram ID вне синтетического диапазона (99XXXXXXX / -10099XXXXXXX)",
    "iban": "IBAN вне списка примеров",
}


def load_patterns(path: Path) -> list[re.Pattern[str]]:
    if not path.is_file():
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    return patterns


def iter_files(roots: Sequence[Path]) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def scan_text(
    text: str,
    *,
    path: str,
    allow: Iterable[re.Pattern[str]] = (),
    deny: Iterable[re.Pattern[str]] = (),
) -> list[Finding]:
    allow = tuple(allow)
    deny = tuple(deny)
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(p.search(line) for p in allow):
            continue
        for check in CHECKS:
            for rule, value in check(line):
                detail = RULE_DETAIL.get(rule, "запрещённый паттерн")
                findings.append(Finding(path, line_no, rule, detail, mask(value)))
        for pattern in deny:
            match = pattern.search(line)
            if match:
                findings.append(
                    Finding(path, line_no, "custom", "приватный deny-паттерн", mask(match.group(0)))
                )
    return findings


def scan_paths(
    roots: Sequence[Path],
    *,
    repo_root: Path,
    allow: Sequence[re.Pattern[str]],
    deny: Sequence[re.Pattern[str]],
) -> list[Finding]:
    findings: list[Finding] = []
    for file_path in iter_files(roots):
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # бинарь или нечитаемый файл — не наша зона
        try:
            rel = str(file_path.relative_to(repo_root))
        except ValueError:
            rel = str(file_path)
        findings.extend(scan_text(text, path=rel, allow=allow, deny=deny))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--paths", nargs="*", help="пути для проверки (по умолчанию код и фикстуры)")
    parser.add_argument("--all", action="store_true", help="проверить всё дерево репозитория")
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if args.paths:
        roots = [Path(p) if Path(p).is_absolute() else repo_root / p for p in args.paths]
    elif args.all:
        roots = [repo_root]
    else:
        roots = [repo_root / p for p in DEFAULT_PATHS]

    allow = load_patterns(repo_root / ALLOW_FILE)
    deny_file = os.environ.get(EXTRA_DENY_ENV)
    deny = load_patterns(Path(deny_file).expanduser()) if deny_file else []
    if deny_file and not Path(deny_file).expanduser().is_file():
        print(f"warning: {EXTRA_DENY_ENV}={deny_file} не найден — приватные паттерны не проверены",
              file=sys.stderr)

    findings = scan_paths(roots, repo_root=repo_root, allow=allow, deny=deny)

    if args.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        scanned = ", ".join(str(r.relative_to(repo_root)) if r != repo_root else "." for r in roots)
        if findings:
            print(f"\ncheck_fixtures: {len(findings)} нарушени(й) в путях: {scanned}", file=sys.stderr)
        else:
            print(f"check_fixtures: чисто ({scanned})")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
