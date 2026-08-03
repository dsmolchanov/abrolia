#!/usr/bin/env python3
"""Gate -1 fixture sanitiser.

Запрещает попадание реальных персональных данных и секретов в код и фикстуры
публичного репозитория. Донорские payload-фикстуры (`~/Programs/hermes`)
содержат реальные Telegram ID, имена и адреса — их нельзя копировать as-is
(см. CONTRIBUTING.md).

Правила (все — fail на находке):

* ``email``      — адрес вне зарезервированных доменов документации (RFC 2606:
                   example.com/net/org/edu, TLD .example/.invalid/.test/.localhost).
* ``phone``      — номер вне зарезервированных диапазонов (+999… по ITU-T E.164
                   и NANP 555-01xx); ловит формы ``+34…`` и ``0034…``.
* ``telegram``   — числовой ID рядом с telegram-контекстом (в пределах окна в
                   несколько строк — структурный JSON держит ключ и значение на
                   разных строках) вне синтетического диапазона.
* ``iban``       — строка, проходящая проверку контрольной суммы IBAN (mod-97),
                   в любом регистре и с любыми пробелами, вне списка примеров.
* ``secret``     — форма ключа/токена (Anthropic, Nerve, Telegram bot, GitHub,
                   AWS, Google OAuth, Slack, PEM private key).
* ``unscannable``— файл или отдельное вложение письма, содержимое которого
                   проверить нельзя (бинарь, не-UTF-8, слишком большой) и
                   которое не описано в PROVENANCE.md ближайшего родительского
                   каталога. Заголовки и текстовые части письма проверяются в
                   любом случае — непрозрачное вложение не прячет письмо.
* ``custom``     — паттерны из приватного файла (env ``HERMES_EXTRA_DENY_FILE``):
                   реальные имена/ID донора, которые нельзя коммитить даже в
                   виде правила. **Исключениями не подавляется никогда.**

Исключения (``.check-fixtures-allow``) действуют точечно: ``путь | правило |
точное значение``. Строка целиком никогда не выключается — соседнее нарушение
на той же строке останется видимым.

Находки печатаются замаскированными: CI-лог не должен становиться новой утечкой.

Использование::

    python3 scripts/check_fixtures.py                 # код и фикстуры
    python3 scripts/check_fixtures.py --all           # всё дерево
    python3 scripts/check_fixtures.py --all --require-deny   # режим CI
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import email
import email.errors
import email.policy
import html
import json
import os
import quopri
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

# Пути со «строгим» режимом: код и фикстуры. Проза (docs/, thoughts/, README)
# добавляется флагом --all; CI гоняет именно --all.
DEFAULT_PATHS = ("tests", "bench", "scripts", "hermes_cloud", "onboarding")

# Мусор ОС: не данные и не фикстуры, происхождение описывать нечему.
OS_JUNK = {".DS_Store", "Thumbs.db"}

ALLOW_FILE = ".check-fixtures-allow"
EXTRA_DENY_ENV = "HERMES_EXTRA_DENY_FILE"
PROVENANCE_FILE = "PROVENANCE.md"

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".mypy_cache", "dist", "build",
}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tgz", ".ico",
    ".ogg", ".oga", ".mp3", ".mp4", ".wav", ".m4a", ".sqlite", ".db", ".woff",
    ".woff2", ".ttf", ".pyc", ".so", ".dylib",
}
MAX_FILE_BYTES = 2_000_000
# Письмо читается MIME-парсером даже когда распухло от вложения: отсекается
# только патология, при которой парсер съест память.
MAX_EML_BYTES = 50_000_000

# Части письма, которые санитайзер действительно понимает как текст.
# RTF сюда не входит намеренно: hex-escape вида \\'40 прячет содержимое, а
# парсера RTF у нас нет — такая часть считается непрозрачной.
TEXTUAL_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "message/delivery-status",
}

# Транспортные кодировки, которые умеет разворачивать stdlib. Всё остальное
# (`x-base64`, `x-uuencode`, мусор) — непрозрачно: содержимое неизвестно.
KNOWN_TRANSFER_ENCODINGS = {"7bit", "8bit", "binary", "quoted-printable", "base64"}

# Сигнатуры бинарных форматов: тип части может врать (PDF как text/plain).
BINARY_MAGIC = (
    b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"PK\x03\x04", b"\x1f\x8b",
    b"{\\rtf", b"\xd0\xcf\x11\xe0", b"OggS", b"ID3", b"SQLite format 3",
)

# Защита от «матрёшки» из вложенных писем.
MAX_MIME_DEPTH = 12

# Дефекты, при которых прочитанное — не содержимое, а его огрызок.
DECODING_DEFECTS = (
    email.errors.InvalidBase64CharactersDefect,
    email.errors.InvalidBase64PaddingDefect,
    email.errors.InvalidBase64LengthDefect,
)

# Окно контекста для Telegram-ID (в обе стороны): в JSON ключ `"from": {` и
# `"id": …` стоят на разных строках и в любом порядке, поэтому построчная
# проверка их не связывает.
CONTEXT_WINDOW = 4

RULE_EMAIL = "email"
RULE_PHONE = "phone"
RULE_TELEGRAM = "telegram"
RULE_IBAN = "iban"
RULE_CUSTOM = "custom"
RULE_UNSCANNABLE = "unscannable"

# --- разрешённые синтетические значения ------------------------------------

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.edu"}
ALLOWED_EMAIL_TLDS = (".example", ".invalid", ".test", ".localhost")

# ITU-T E.164: код 999 зарезервирован; NANP: 555-0100..555-0199 — вымышленные.
SYNTHETIC_PHONE_RE = re.compile(r"^(?:999\d{0,12}|1\d{3}55501\d{2})$")

# Синтетические Telegram ID: пользователь 99 + 7 цифр, супергруппа -10099 + 7,
# legacy-группа -99 + 7.
SYNTHETIC_TELEGRAM_RE = re.compile(r"^(?:99\d{7}|-99\d{7}|-10099\d{7})$")

# Общеизвестные документационные IBAN.
ALLOWED_IBANS = {
    "DE89370400440532013000",
    "NL91ABNA0417164300",
    "GB82WEST12345698765432",
    "IT60X0542811101000000123456",
}

# --- регулярки поиска -------------------------------------------------------

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# Разделители внутри номера и IBAN: обычный пробел, неразрывный, узкий
# неразрывный, тонкий. NBSP приходит из вставок в Word/Outlook и раньше
# разрывал распознавание.
SPACERS = " \u00a0\u202f\u2009"
PHONE_PLUS_RE = re.compile(rf"\+\d[\d{SPACERS}().\-]{{5,17}}\d")
PHONE_00_RE = re.compile(rf"(?<![\d.])00[1-9][\d{SPACERS}().\-]{{5,16}}\d")
TELEGRAM_CONTEXT_RE = re.compile(
    r"(telegram|tg_id|chat_id|chat-id|chatId|user_id|userId|from_id|sender_id"
    r"|from_user|update_id|\"from\"|\"chat\"|\"user\"|\"message\")",
    re.IGNORECASE,
)
# Telegram ID — самостоятельный целочисленный токен. Цифровые куски внутри
# SHA, IBAN и API-ключей под это правило не подпадают (их ловят свои правила),
# иначе окно контекста заливает вывод ложными находками.
INT_RE = re.compile(r"(?<![\w.])-?\d{6,14}(?![\w.])")
IBAN_START_RE = re.compile(rf"\b[A-Za-z]{{2}}\d{{2}}(?=[A-Za-z0-9{SPACERS}]{{10,}})")

SECRET_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    # Nerve cloud API key: генератор выдаёт "nrv_live_" + 64 hex
    # (nerve-cloud internal/cloudapi/handler_keys.go, generateCloudAPIKeyMaterial).
    # Правило намеренно шире генератора: любой префикс окружения и любой
    # достаточно длинный хвост, чтобы смена формата не открыла дыру.
    ("nerve-key", re.compile(r"\bnrv_(?:[a-z]+_)?[A-Za-z0-9]{24,}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9\-_]{10,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b")),
)

RULE_DETAIL = {
    RULE_EMAIL: "адрес вне зарезервированных доменов документации",
    RULE_PHONE: "телефон вне зарезервированных диапазонов (+999…, 555-01xx)",
    RULE_TELEGRAM: "Telegram ID вне синтетического диапазона (99XXXXXXX / -10099XXXXXXX)",
    RULE_IBAN: "IBAN с валидной контрольной суммой вне списка примеров",
    RULE_CUSTOM: "приватный deny-паттерн",
    RULE_UNSCANNABLE: f"непроверяемый файл фикстуры без записи в {PROVENANCE_FILE}",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    rule: str
    detail: str
    masked: str
    # Каноническое значение нужно для точечных исключений; наружу не печатается.
    value: str = field(default="", repr=False, compare=False)

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


@dataclass(frozen=True)
class AllowEntry:
    """Точечное исключение: путь + правило + точное значение."""

    path_glob: str
    rule: str
    value: str

    def matches(self, path: str, rule: str, value: str) -> bool:
        return (
            self.rule == rule
            and self.value == value
            and (fnmatch(path, self.path_glob) or fnmatch(Path(path).name, self.path_glob))
        )


class AllowFileError(ValueError):
    """Некорректный файл исключений — считаем нарушением, а не мелочью."""


def mask(value: str) -> str:
    """Замаскировать находку: CI-лог не должен воспроизводить утечку."""
    value = value.strip()
    if len(value) <= 4:
        return "*" * len(value)
    keep = 2 if len(value) < 12 else 3
    return f"{value[:keep]}{'*' * (len(value) - 2 * keep)}{value[-keep:]}"


# --- правила ----------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """Срабатывание правила на строке.

    Диапазон нужен, чтобы правила не спорили друг с другом: цифры внутри
    найденного IBAN — не телефон и не Telegram ID. `reportable=False` — это
    разрешённое значение, которое всё равно занимает свой участок строки.
    """

    rule: str
    value: str
    start: int
    end: int
    reportable: bool = True

    def overlaps(self, span: tuple[int, int]) -> bool:
        return self.start < span[1] and span[0] < self.end


def _email_domain_allowed(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    if domain in ALLOWED_EMAIL_DOMAINS:
        return True
    if any(domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS):
        return True
    return domain.endswith(ALLOWED_EMAIL_TLDS)


def check_email(line: str) -> Iterator[Hit]:
    for match in EMAIL_RE.finditer(line):
        address = match.group(0)
        if not _email_domain_allowed(address.rsplit("@", 1)[1]):
            yield Hit(RULE_EMAIL, address.lower(), *match.span())


def check_phone(line: str) -> Iterator[Hit]:
    """Найти телефон в формах `+34…` и `0034…`, в том числе с разделителями.

    Разрешённые (синтетические) номера тоже возвращаются, но с
    ``reportable=False``: их диапазон нужен, чтобы те же цифры не были потом
    объявлены Telegram-ID.
    """
    for regex, offset in ((PHONE_PLUS_RE, 0), (PHONE_00_RE, 2)):
        for match in regex.finditer(line):
            digits = re.sub(r"\D", "", match.group(0))[offset:]
            if len(digits) < 7:
                continue
            yield Hit(
                RULE_PHONE,
                "+" + digits,
                *match.span(),
                reportable=not SYNTHETIC_PHONE_RE.match(digits),
            )


def iban_checksum_ok(candidate: str) -> bool:
    """mod-97 == 1 (ISO 13616). Отсекает случайные буквенно-цифровые строки."""
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", candidate):
        return False
    rearranged = candidate[4:] + candidate[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def check_iban(line: str) -> Iterator[Hit]:
    """Найти IBAN независимо от регистра и расстановки пробелов.

    Наивный `\\b[A-Z]{2}\\d{2}[A-Z0-9]{11,30}\\b` не видит «de89 3704 0044 …»
    и захватывает следующие слова, поэтому кандидат собирается посимвольно, а
    решение принимает контрольная сумма.
    """
    seen: set[str] = set()
    for start in IBAN_START_RE.finditer(line):
        chars: list[str] = []
        positions: list[int] = []
        i = start.start()
        while i < len(line) and len(chars) < 34:
            ch = line[i]
            if ch.isalnum():
                chars.append(ch.upper())
                positions.append(i)
            elif ch in SPACERS and chars:
                pass  # пробелы (в т.ч. неразрывные) внутри IBAN допустимы при печати
            else:
                break
            i += 1
        for length in range(len(chars), 14, -1):
            candidate = "".join(chars[:length])
            if iban_checksum_ok(candidate):
                if candidate not in ALLOWED_IBANS and candidate not in seen:
                    seen.add(candidate)
                yield Hit(RULE_IBAN, candidate, start.start(), positions[length - 1] + 1,
                          reportable=candidate not in ALLOWED_IBANS)
                break


def check_secret(line: str) -> Iterator[Hit]:
    for name, pattern in SECRET_RES:
        for match in pattern.finditer(line):
            yield Hit(f"secret:{name}", match.group(0), *match.span())


LINE_CHECKS: tuple[Callable[[str], Iterator[Hit]], ...] = (
    check_email,
    check_phone,
    check_iban,
    check_secret,
)


def telegram_ids(line: str) -> Iterator[Hit]:
    for match in INT_RE.finditer(line):
        value = match.group(0)
        if value.lstrip("-").startswith("0"):
            continue  # Telegram ID не начинается с нуля — это номер или код
        if not SYNTHETIC_TELEGRAM_RE.match(value):
            yield Hit(RULE_TELEGRAM, value, *match.span())


# --- загрузка конфигурации ---------------------------------------------------


def load_allow(path: Path) -> list[AllowEntry]:
    """Разобрать `.check-fixtures-allow`: `путь | правило | точное значение`."""
    if not path.is_file():
        return []
    entries: list[AllowEntry] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not all(parts):
            raise AllowFileError(
                f"{path.name}:{line_no}: ожидается «путь | правило | точное значение», получено: {line!r}"
            )
        path_glob, rule, value = parts
        if rule == RULE_CUSTOM:
            raise AllowFileError(
                f"{path.name}:{line_no}: приватные deny-паттерны не подлежат исключению"
            )
        entries.append(AllowEntry(path_glob, rule, value))
    return entries


def load_deny(path: Path) -> list[re.Pattern[str]]:
    if not path.is_file():
        return []
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    return patterns


# --- чтение файлов -----------------------------------------------------------


class Unscannable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Scannable:
    """Что удалось прочитать из файла и что осталось непрозрачным.

    Непрозрачная часть письма не должна прятать остальное письмо: заголовки и
    текстовые части проверяются всегда, а provenance требуется отдельно для
    каждого нечитаемого вложения.
    """

    text: str
    opaque_parts: tuple[str, ...] = ()
    # Дополнительные представления того же файла (развёрнутое письмо,
    # нормализованный текст). Сканируются отдельно, чтобы нумерация строк
    # оставалась верной; одно значение в отчёт попадает один раз.
    extra_views: tuple[str, ...] = ()

    @property
    def multi_view(self) -> bool:
        return bool(self.extra_views)


def _is_textual(content_type: str) -> bool:
    """Считается ли часть письма текстом.

    Успешный `decode()` текстом не делает: ASCII-only PDF декодируется без
    ошибок, но его содержимое санитайзер не понимает. Решает объявленный тип —
    и, дополнительно, сигнатура байтов (см. `_looks_binary`).
    """
    if content_type.startswith("text/"):
        return True
    return content_type in TEXTUAL_MIME_TYPES


def _has_decoding_defect(part) -> bool:
    """Записал ли парсер дефект кодирования у этой части."""
    return any(isinstance(defect, DECODING_DEFECTS) for defect in getattr(part, "defects", ()))


def _looks_binary(payload: bytes) -> bool:
    """Похоже ли содержимое на бинарь вопреки объявленному типу.

    PDF, объявленный `text/plain`, декодируется как ASCII и молча проходил бы
    проверку, ничего при этом не значив.
    """
    if b"\x00" in payload[:4096]:
        return True
    return any(payload.startswith(magic) for magic in BINARY_MAGIC)


def normalize_text(text: str) -> str:
    """Снять экранирование, за которым прячется значение.

    `anna&#64;example.com` в HTML, `\\u0040` в JSON и разметка вокруг адреса —
    всё это проходит мимо правил, если сканировать байты как есть. Обработка
    построчная: нумерация строк в отчёте остаётся верной.
    """
    unescaped = html.unescape(text)
    return re.sub(
        r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), unescaped
    )


def normalized_views(text: str) -> tuple[str, ...]:
    """Дополнительные представления текста для сканирования.

    Разметку снимаем в двух вариантах: со склейкой (адрес, разорванный тегами,
    собирается обратно) и с пробелом (соседние слова не слипаются).
    """
    unescaped = normalize_text(text)
    joined = re.sub(r"<[^>\n]{1,200}>", "", unescaped)
    spaced = re.sub(r"<[^>\n]{1,200}>", " ", unescaped)
    views = [view for view in (unescaped, joined, spaced) if view != text]
    unique: list[str] = []
    for view in views:
        if view not in unique:
            unique.append(view)
    return tuple(unique)


def _decode(payload: bytes, charset: str) -> str:
    for candidate in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(candidate, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")  # pragma: no cover


def _walk_part(part, *, stem: str, index: int, chunks: list[str], opaque: list[str],
               depth: int = 0) -> None:
    """Разобрать часть письма: заголовки, текст, вложенные письма.

    Заголовки берутся у **каждой** части, а не только у корня: адрес умеет
    прятаться в `Content-Description` вложения. Вложенное письмо
    (`message/rfc822`) разбирается рекурсивно, в том числе когда оно приехало
    в base64.
    """
    with contextlib.suppress(Exception):  # битые заголовки не должны ронять разбор
        chunks.extend(f"{header}: {value}" for header, value in part.items())
    content_type = part.get_content_type()
    name = part.get_filename() or f"{stem}#{index}"
    if depth > MAX_MIME_DEPTH:
        opaque.append(f"{name} ({content_type}, глубина вложенности)")
        return

    if content_type.startswith("multipart/"):
        payload = part.get_payload()
        if isinstance(payload, list):
            for sub_index, sub in enumerate(payload):
                _walk_part(sub, stem=stem, index=sub_index, chunks=chunks,
                           opaque=opaque, depth=depth + 1)
            return
        # multipart без разобранных частей: тело осталось строкой — разбираем
        # его как сырьё ниже, чтобы содержимое не исчезло из проверки.

    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte and cte not in KNOWN_TRANSFER_ENCODINGS:
        # Неизвестное кодирование: считать его текстом нельзя — что именно
        # лежит внутри, мы не знаем.
        opaque.append(f"{name} ({content_type}, transfer-encoding {cte})")
        return

    if content_type.startswith("message/"):
        # stdlib не разворачивает transfer-encoding у message/*: вложенное
        # письмо в base64 иначе остаётся нечитаемым блобом, а не письмом.
        source = part.get_payload(decode=True)
        if source is None:
            nested_payload = part.get_payload()
            if isinstance(nested_payload, list):
                source = b"\n".join(
                    item.as_bytes() if hasattr(item, "as_bytes") else str(item).encode()
                    for item in nested_payload
                )
            else:
                source = str(nested_payload).encode("utf-8", errors="replace")
        try:
            if cte == "base64":
                # validate=True: повреждённый base64 не должен молча
                # превращаться в усечённое «письмо», которое нечего проверять.
                source = base64.b64decode(re.sub(rb"\s+", b"", source), validate=True)
            elif cte == "quoted-printable":
                source = quopri.decodestring(source)
        except Exception:
            opaque.append(f"{name} ({content_type}, не разворачивается)")
            return
        try:
            nested = email.message_from_bytes(source, policy=email.policy.default)
        except Exception:  # pragma: no cover - не разобралось как письмо
            opaque.append(f"{name} ({content_type})")
            return
        _walk_part(nested, stem=f"{stem}#{index}", index=0, chunks=chunks,
                   opaque=opaque, depth=depth + 1)
        return

    try:
        payload = part.get_payload(decode=True)
    except Exception:  # pragma: no cover - битый transfer-encoding
        payload = None
    if _has_decoding_defect(part):
        # stdlib не бросает исключение на повреждённом base64: он возвращает
        # усечённые (или пустые) байты и записывает дефект. Прочитанным это
        # считать нельзя — что было в исходных байтах, мы не знаем.
        opaque.append(f"{name} ({content_type}, повреждённое кодирование)")
        return
    if payload is None:
        raw_payload = part.get_payload()
        if isinstance(raw_payload, list):
            for sub_index, sub in enumerate(raw_payload):
                _walk_part(sub, stem=stem, index=sub_index, chunks=chunks,
                           opaque=opaque, depth=depth + 1)
            return
        if isinstance(raw_payload, str):
            payload = raw_payload.encode("utf-8", errors="replace")
        else:  # pragma: no cover
            return

    if not _is_textual(content_type) or _looks_binary(payload):
        # PDF, картинка, архив — и то же самое, объявленное текстом.
        opaque.append(f"{name} ({content_type})")
        return

    chunks.append(_decode(payload, part.get_content_charset() or "utf-8"))


def eml_to_text(raw: bytes, *, stem: str = "eml") -> tuple[str, list[str]]:
    """Развернуть .eml: заголовки всех частей + текст, отдельно — непрозрачные.

    Тело письма приходит в base64/quoted-printable, и без декодирования
    санитайзер «не видит» ни адресов, ни телефонов внутри фикстуры.
    """
    try:
        # policy.default раскрывает encoded-words в заголовках (=?utf-8?B?…?=),
        # иначе адрес в теме письма проходит мимо проверки.
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # pragma: no cover - битые заголовки
        message = email.message_from_bytes(raw, policy=email.policy.compat32)
    chunks: list[str] = []
    opaque: list[str] = []
    _walk_part(message, stem=stem, index=0, chunks=chunks, opaque=opaque)
    return "\n".join(chunks), opaque


def opaque_part_name(label: str) -> str:
    """Имя вложения из метки `имя (тип)` — его и ищем в PROVENANCE.md."""
    return label.rsplit(" (", 1)[0]


def read_scannable(path: Path) -> Scannable:
    """Содержимое файла для проверки. Бросает Unscannable, если нечего читать."""
    try:
        size = path.stat().st_size
    except OSError as exc:  # pragma: no cover - файловая система
        raise Unscannable(f"недоступен: {exc}") from exc

    if path.suffix.lower() == ".eml":
        # Письмо разбирается MIME-парсером до любых отсечек по кодировке и
        # размеру: иначе 8bit-письмо в ISO-8859-1 или письмо, распухшее от
        # вложения, целиком выпадало из проверки — вместе с заголовками и
        # текстом, которые прочитать можно всегда.
        if size > MAX_EML_BYTES:
            raise Unscannable(f"больше {MAX_EML_BYTES} байт")
        raw = path.read_bytes()
        decoded, opaque = eml_to_text(raw, stem=path.name)
        envelope = raw.decode("utf-8", errors="replace")
        # Сырьё письма — основное представление (нумерация строк совпадает с
        # файлом), развёрнутый MIME и его нормализация — дополнительные.
        views = (decoded, *normalized_views(decoded))
        return Scannable(envelope, tuple(opaque), extra_views=views)

    if size > MAX_FILE_BYTES:
        raise Unscannable(f"больше {MAX_FILE_BYTES} байт")
    if path.suffix.lower() in BINARY_SUFFIXES:
        raise Unscannable(f"бинарный формат {path.suffix.lower()}")
    raw = path.read_bytes()
    if _looks_binary(raw):
        # Расширение может врать так же, как MIME-тип внутри письма: `.txt`
        # с сигнатурой %PDF- или {\rtf — непрозрачный файл, а не текст.
        raise Unscannable("бинарная сигнатура при текстовом расширении")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unscannable("не UTF-8") from exc
    # Нормализация нужна не только внутри письма: `.json` с `\u0040` и `.html`
    # с `&#64;` — такие же фикстуры и точно так же прячут адрес.
    return Scannable(text, extra_views=normalized_views(text))


# Запись манифеста: `- \`путь\` — описание` вне блоков кода.
# Отступ ограничен тремя пробелами: четыре и больше — это indented code block
# по CommonMark, то есть пример, а не запись.
PROVENANCE_ENTRY_RE = re.compile(r"^ {0,3}[-*] +`([^`]+)`\s*[—–-]\s*\S")
# Открывающий/закрывающий fence: не меньше трёх одинаковых символов.
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def load_provenance(manifest: Path) -> set[str]:
    """Разобрать PROVENANCE.md в набор описанных путей.

    Путь — относительно каталога самого манифеста. Ищется структурированная
    запись, а не подстрока: пояснение в прозе или пример в блоке кода не
    должны молча разрешать одноимённый файл. Абсолютные пути и `..`
    отбрасываются — иначе запись из одного каталога разрешала бы файл в другом.
    """
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover
        return set()
    entries: set[str] = set()
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        opener = FENCE_RE.match(line)
        if opener:
            marker = opener.group(1)
            char, length = marker[0], len(marker)
            if fence is None:
                fence = (char, length)
                continue
            # Закрыть блок может только тот же символ той же или большей длины:
            # строка `~~~` внутри ```-блока — это содержимое, а не конец.
            if char == fence[0] and length >= fence[1] and not opener.group(2).strip():
                fence = None
            continue
        if fence is not None:
            continue
        match = PROVENANCE_ENTRY_RE.match(line)
        if not match:
            continue
        token = match.group(1).strip()
        path_part, _, attachment = token.partition("#")
        if path_part.startswith("/") or ".." in PurePosixPath(path_part).parts:
            continue
        normalized = PurePosixPath(path_part).as_posix().removeprefix("./")
        entries.add(f"{normalized}#{attachment}" if attachment else normalized)
    return entries


def provenance_covers(file_path: Path, repo_root: Path, *, part: str | None = None) -> bool:
    """Описан ли файл (или вложение внутри него) записью в PROVENANCE.md.

    Сравнивается точный путь относительно каталога манифеста: запись
    `other/notice.png` не разрешает `media/notice.png`, а вложение
    привязывается к письму (`email/with_pdf.eml#einladung.pdf`), поэтому одно
    имя не покрывает одноимённые вложения разных писем.
    """
    for parent in file_path.parents:
        manifest = parent / PROVENANCE_FILE
        if manifest.is_file():
            relative = file_path.relative_to(parent).as_posix()
            token = f"{relative}#{part}" if part else relative
            if token in load_provenance(manifest):
                return True
        if parent == repo_root:
            break
    return False


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
            yield path


# --- сканирование ------------------------------------------------------------


def scan_text(
    text: str,
    *,
    path: str,
    allow: Sequence[AllowEntry] = (),
    deny: Sequence[re.Pattern[str]] = (),
    context_window: int = CONTEXT_WINDOW,
) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        line_no = index + 1
        raw_hits: list[Hit] = []
        for check in LINE_CHECKS:
            raw_hits.extend(check(line))
        # Окно симметричное: в JSON ключ контекста стоит и до значения
        # (`"from": {` … `"id": …`), и после него (`"id": …` … `"chat": {`).
        window = lines[max(0, index - context_window) : index + context_window + 1]
        if any(TELEGRAM_CONTEXT_RE.search(candidate) for candidate in window):
            raw_hits.extend(telegram_ids(line))
        # Цифры внутри распознанного IBAN — не телефон и не Telegram ID:
        # «DE02 1203 0044 …» иначе давал ложный номер вида 0044…
        iban_spans = [(hit.start, hit.end) for hit in raw_hits if hit.rule == RULE_IBAN]
        phone_spans = [(hit.start, hit.end) for hit in raw_hits if hit.rule == RULE_PHONE]
        # ID бота внутри токена `<id>:<секрет>` уже покрыт правилом secret.
        secret_spans = [
            (hit.start, hit.end) for hit in raw_hits if hit.rule.startswith("secret:")
        ]
        for hit in raw_hits:
            if not hit.reportable:
                continue
            blocking = iban_spans if hit.rule == RULE_PHONE else []
            if hit.rule == RULE_TELEGRAM:
                blocking = iban_spans + phone_spans + secret_spans
            if blocking and any(hit.overlaps(span) for span in blocking):
                continue
            if any(entry.matches(path, hit.rule, hit.value) for entry in allow):
                continue
            detail = RULE_DETAIL.get(hit.rule, "запрещённый паттерн")
            findings.append(Finding(path, line_no, hit.rule, detail, mask(hit.value), hit.value))
        # Приватные deny-паттерны исключениями не подавляются.
        for pattern in deny:
            match = pattern.search(line)
            if match:
                findings.append(
                    Finding(
                        path,
                        line_no,
                        RULE_CUSTOM,
                        RULE_DETAIL[RULE_CUSTOM],
                        mask(match.group(0)),
                        match.group(0),
                    )
                )
    return findings


def scan_scannable(
    scannable: Scannable,
    *,
    path: str,
    allow: Sequence[AllowEntry] = (),
    deny: Sequence[re.Pattern[str]] = (),
) -> list[Finding]:
    """Просканировать все представления файла и убрать повторы.

    Представления (сырьё, развёрнутое письмо, нормализованный текст)
    сканируются отдельно, чтобы нумерация строк в каждом была верной; одно и
    то же значение показывается пользователю один раз — из первого
    представления, где оно найдено.
    """
    findings = scan_text(scannable.text, path=path, allow=allow, deny=deny)
    for view in scannable.extra_views:
        findings.extend(scan_text(view, path=path, allow=allow, deny=deny))
    if not scannable.multi_view:
        return findings
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = (finding.rule, finding.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def scan_paths(
    roots: Sequence[Path],
    *,
    repo_root: Path,
    allow: Sequence[AllowEntry] = (),
    deny: Sequence[re.Pattern[str]] = (),
) -> list[Finding]:
    findings: list[Finding] = []
    for file_path in iter_files(roots):
        try:
            rel = str(file_path.relative_to(repo_root))
        except ValueError:
            rel = str(file_path)
        try:
            scannable = read_scannable(file_path)
        except Unscannable as exc:
            # Непроверяемый файл — слепая зона, поэтому он либо описан в
            # PROVENANCE.md ближайшего родительского каталога, либо это находка.
            if file_path.name not in OS_JUNK and not provenance_covers(
                file_path, repo_root
            ):
                findings.append(
                    Finding(rel, 0, RULE_UNSCANNABLE, RULE_DETAIL[RULE_UNSCANNABLE],
                            exc.reason, rel)
                )
            continue
        findings.extend(scan_scannable(scannable, path=rel, allow=allow, deny=deny))
        for label in scannable.opaque_parts:
            # Provenance на само письмо не покрывает вложение внутри него:
            # иначе одна бинарная часть прятала бы весь файл от проверки.
            name = opaque_part_name(label)
            if not provenance_covers(file_path, repo_root, part=name):
                findings.append(
                    Finding(
                        rel,
                        0,
                        RULE_UNSCANNABLE,
                        "непроверяемая часть письма",
                        # Имя вложения может само оказаться персональными
                        # данными («Krankmeldung Erika Mustermann.pdf»).
                        label.replace(name, mask(name), 1),
                        label,
                    )
                )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--paths", nargs="*", help="пути для проверки (по умолчанию код и фикстуры)")
    parser.add_argument("--all", action="store_true", help="проверить всё дерево репозитория")
    parser.add_argument(
        "--require-deny",
        action="store_true",
        help=f"падать, если приватные deny-паттерны ({EXTRA_DENY_ENV}) не сконфигурированы",
    )
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

    try:
        allow = load_allow(repo_root / ALLOW_FILE)
    except AllowFileError as exc:
        print(f"check_fixtures: {exc}", file=sys.stderr)
        return 2

    deny_path = os.environ.get(EXTRA_DENY_ENV)
    deny = load_deny(Path(deny_path).expanduser()) if deny_path else []
    if not deny:
        message = (
            f"приватные deny-паттерны не загружены ({EXTRA_DENY_ENV}"
            f"{'=' + deny_path if deny_path else ' не задан'})"
        )
        if args.require_deny:
            print(f"check_fixtures: {message} — режим --require-deny", file=sys.stderr)
            return 2
        print(f"warning: {message}", file=sys.stderr)

    findings = scan_paths(roots, repo_root=repo_root, allow=allow, deny=deny)

    if args.json:
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(finding.render())
        scanned = ", ".join(
            str(r.relative_to(repo_root)) if r != repo_root else "." for r in roots
        )
        if findings:
            print(
                f"\ncheck_fixtures: {len(findings)} нарушени(й) в путях: {scanned}",
                file=sys.stderr,
            )
        else:
            print(f"check_fixtures: чисто ({scanned})")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
