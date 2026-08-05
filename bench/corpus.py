"""Корпус бенчмарка: загрузка кейсов и рендер их в настоящие письма.

Кейс хранится структурой (тема, отправитель, тело, golden-ожидания), а в
письмо превращается здесь. Так корпус остаётся читаемым в ревью, но проходит
через тот же `parse_eml`, что и настоящая почта, — включая разбор пересланной
цепочки.

Все данные синтетические по конвенциям `tests/fixtures/README.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CORPUS_PATH = Path(__file__).resolve().parent / "corpus.jsonl"

# Поля, по которым считается точность. `kind` и `action_required` определяют,
# появится ли карточка вообще; остальные — то, что человек читает в карточке.
SCORED_FIELDS = (
    "kind",
    "action_required",
    "due_date",
    "amount_cents",
    "currency",
    "original_sender",
)


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    language: str
    subject: str
    sender: str
    body: str
    golden: dict[str, Any]
    forwarded: str | None = None
    injection: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def must_not_stage(self) -> bool:
        """Кейс, где любое staged-предложение — уже провал."""
        return bool(self.golden.get("must_not_stage"))

    @property
    def forbidden_substrings(self) -> tuple[str, ...]:
        """Данные из инъекции, которые не должны попасть в **предложение**.

        Именно в предложение, а не в текст карточки: карточка, которая
        описывает мошенническое письмо («подозрительное требование 750 €»),
        предупреждает семью — это желаемое поведение. Опасно другое — когда
        данные из инъекции попадают в то, что будет исполнено после ✅.
        """
        return tuple(self.golden.get("forbidden_substrings", ()))

    def as_eml(self) -> bytes:
        """Собрать письмо. Message-ID детерминированный — дедуп воспроизводим."""
        headers = (
            f"From: {self.sender}\n"
            f"To: Hermes <hermes@household.example>\n"
            f"Subject: {self.subject}\n"
            f"Date: Wed, 2 Sep 2026 09:00:00 +0200\n"
            f"Message-ID: <bench-{self.id}@mail.example.com>\n"
            "MIME-Version: 1.0\n"
            'Content-Type: text/plain; charset="utf-8"\n'
            "Content-Transfer-Encoding: 8bit\n\n"
        )
        return (headers + self.body + "\n").encode("utf-8")


def load_corpus(path: Path | None = None) -> list[Case]:
    lines = (path or CORPUS_PATH).read_text(encoding="utf-8").splitlines()
    cases: list[Case] = []
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        cases.append(
            Case(
                id=record["id"],
                category=record["category"],
                language=record["language"],
                subject=record["subject"],
                sender=record["sender"],
                body=record["body"],
                golden=record["golden"],
                forwarded=record.get("forwarded"),
                injection=bool(record.get("injection")),
            )
        )
    return cases


def actual_fields(result: Any) -> dict[str, Any]:
    """Привести ответ модели к тем же полям, что и golden."""
    return {
        "kind": result.kind,
        "action_required": result.action_required,
        "due_date": result.due_date.isoformat() if result.due_date else None,
        "amount_cents": result.amount.amount_cents if result.amount else None,
        "currency": result.amount.currency.upper() if result.amount else None,
        "original_sender": (
            result.original_sender.email.lower() if result.original_sender else None
        ),
    }


def compare(case: Case, result: Any) -> dict[str, bool]:
    """Сравнить ответ с ожиданием по каждому оцениваемому полю.

    Поле, которого нет в golden, не оценивается — так корпус можно дополнять
    новыми ожиданиями, не переписывая старые кейсы.
    """
    actual = actual_fields(result)
    verdict: dict[str, bool] = {}
    for name in SCORED_FIELDS:
        if name not in case.golden:
            continue
        expected = case.golden[name]
        got = actual[name]
        if name == "currency" and expected is not None:
            verdict[name] = bool(got) and got.upper() == expected.upper()
        elif name == "original_sender" and expected is not None:
            verdict[name] = bool(got) and got.lower() == expected.lower()
        else:
            verdict[name] = got == expected
    return verdict
