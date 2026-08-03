"""Целостность бенч-корпуса и логика оценки.

Прогон по моделям — ручной (`python3 bench/run.py`), но сам корпус обязан
оставаться валидным и синтетическим постоянно: он станет регрессионным
набором, в который попадает каждая ошибка пилота.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from bench.corpus import SCORED_FIELDS, compare, load_corpus
from hermes_cloud.ingest.eml import parse_eml
from hermes_cloud.runner.extraction import ExtractionResult, Money, OriginalSenderHint

CASES = load_corpus()
VALID_KINDS = {"event", "payment", "task", "info", "spam"}


def test_corpus_meets_the_plan_requirements() -> None:
    """План требует ≥40 писем с покрытием языков и категорий."""
    assert len(CASES) >= 40

    languages = Counter(case.language for case in CASES)
    assert set(languages) >= {"de", "it", "nl", "en"}
    assert min(languages.values()) >= 4, "каждый язык представлен не одним кейсом"

    categories = Counter(case.category for case in CASES)
    assert set(categories) >= {"school", "invoice", "invitation", "spam", "adversarial", "ocr"}

    assert sum(1 for case in CASES if case.forwarded) >= 8, "пересылки — основной вход"
    assert sum(1 for case in CASES if case.injection) >= 5, "инъекционный корпус"


def test_case_ids_are_unique() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_case_renders_and_parses() -> None:
    for case in CASES:
        parsed = parse_eml(case.as_eml())
        assert parsed.subject == case.subject
        assert parsed.text.strip(), f"{case.id}: пустое тело"
        assert parsed.message_id == f"bench-{case.id}@mail.example.com"


def test_forwarded_cases_expose_the_original_sender() -> None:
    """Ожидание golden обязано совпадать с тем, что видит парсер."""
    for case in CASES:
        if not case.forwarded:
            continue
        parsed = parse_eml(case.as_eml())
        assert parsed.original_sender is not None, f"{case.id}: цепочка не распознана"
        expected = case.golden.get("original_sender")
        if expected:
            assert parsed.original_sender.email == expected, case.id


def test_golden_expectations_are_well_formed() -> None:
    for case in CASES:
        golden = case.golden
        assert golden["kind"] in VALID_KINDS, case.id
        assert isinstance(golden["action_required"], bool), case.id
        if golden.get("due_date"):
            date.fromisoformat(golden["due_date"])
        if golden.get("amount_cents") is not None:
            assert golden["amount_cents"] > 0, case.id
            assert golden.get("currency"), f"{case.id}: сумма без валюты"
        unknown = set(golden) - set(SCORED_FIELDS) - {"must_not_stage", "forbidden_substrings"}
        assert not unknown, f"{case.id}: неизвестные поля golden {unknown}"


def test_injection_cases_declare_what_must_not_leak() -> None:
    for case in CASES:
        if not case.injection:
            continue
        assert case.forbidden_substrings, f"{case.id}: не указано, что не должно утечь"
        # Запрещённая строка должна быть опознаваемой: короткое число вроде
        # «500» встречается в тексте случайно и превращает проверку в шум.
        for needle in case.forbidden_substrings:
            assert len(needle) >= 4, f"{case.id}: слишком общая строка {needle!r}"


def test_scoring_counts_only_declared_fields() -> None:
    case = next(c for c in CASES if c.id == "de-school-trip-fee")
    perfect = ExtractionResult(
        kind="payment", title="t", summary="s", source_language="de",
        action_required=True, due_date=date(2026, 9, 8),
        amount=Money(amount_cents=1500, currency="eur"),
        original_sender=OriginalSenderHint(email="Sekretariat@Grundschule.Example"),
        confidence=0.9,
    )

    verdict = compare(case, perfect)

    assert all(verdict.values()), verdict
    assert set(verdict) <= set(SCORED_FIELDS)


def test_scoring_catches_a_wrong_amount() -> None:
    case = next(c for c in CASES if c.id == "de-school-trip-fee")
    wrong = ExtractionResult(
        kind="payment", title="t", summary="s", source_language="de",
        action_required=True, due_date=date(2026, 9, 8),
        amount=Money(amount_cents=150, currency="EUR"),
        original_sender=OriginalSenderHint(email="sekretariat@grundschule.example"),
        confidence=0.9,
    )

    verdict = compare(case, wrong)

    assert verdict["amount_cents"] is False
    assert verdict["due_date"] is True
