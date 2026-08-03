"""Ручной цикл tool-use: ход диалога с семьёй.

Цикл написан вручную, а не взят готовым, ровно по одной причине: между
«модель попросила» и «мы сделали» должно помещаться то, что нельзя доверить
библиотеке — проверка прав, журнал эффектов и решение о том, можно ли вообще
повторить сорвавшийся вызов API.

Три правила, из которых состоит весь модуль.

**Ни одного эффекта без записи.** Каждый `tool_use` проходит через
`EffectJournal` по ключу `(run_id, tool_use_id)`. Модель, повторившая тот же
`tool_use_id`, получает прежний результат — второй раз ничего не происходит.

**Повтор хода — только пока ход пуст.** Сорвался вызов API и эффектов ещё нет —
повторяем. Хотя бы один эффект записан — не повторяем никогда, а честно
рассказываем человеку, что успело произойти. Иначе «просто повторим запрос»
однажды означает второе письмо школе.

**Ход конечен.** Итерации, время и токены ограничены: зациклившаяся модель
обязана упереться в предел, а не в счёт за месяц.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hermes_cloud.core.effects import EffectInFlight, EffectJournal
from hermes_cloud.core.runcontext import CapabilityDenied, RunContext
from hermes_cloud.runner.tools import (
    REGISTRY,
    Services,
    ToolInputError,
    ToolRegistry,
    UnknownTool,
)

logger = logging.getLogger(__name__)

# Диалоговая модель — Locked Decision плана (извлечение живёт на Sonnet 5,
# см. `runner/extraction.py`; здесь разговор с семьёй, и он идёт на Opus).
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_EFFORT = "medium"

# Пределы хода (план, Фаза 2, п. 2).
MAX_ITERATIONS = 8
MAX_SECONDS = 5 * 60
TOKEN_BUDGET = 120_000

STOP_COMPLETED = "completed"
STOP_ITERATIONS = "max_iterations"
STOP_TIMEOUT = "timeout"
STOP_BUDGET = "token_budget"
STOP_INTERRUPTED = "interrupted"
STOP_REFUSED = "refused"
STOP_NO_TOOLS = "no_tools"

SYSTEM_PROMPT = """\
Ты — семейный операционный ассистент. Ты помогаешь семье, которая живёт в \
стране с чужим языком, не потерять важное: сроки, взносы, встречи, документы.

ЧТО ТЫ МОЖЕШЬ. Ты отвечаешь на вопросы по тому, что уже сохранено, и \
предлагаешь действия. Ты НЕ выполняешь действия сам: любой инструмент, \
меняющий что-либо, только ставит предложение, которое семья подтверждает \
кнопкой. Так и говори: «предложил, подтвердите» — не «сделал».

ЧЕГО ТЫ НЕ ДЕЛАЕШЬ. Не выдумываешь факты и суммы. Не обещаешь того, чего \
инструменты не умеют. Если данных не хватает — спрашиваешь, а не \
достраиваешь. Если инструмент вернул отказ по правам — сообщаешь об этом \
прямо, а не пытаешься обойти.

КАК ОТВЕЧАЕШЬ. Коротко и на языке семьи: {language}. Даты — в виде, понятном \
человеку. Сумму называешь с валютой. Без канцелярита и без извинений на \
полстраницы.
"""

TEXT_NO_TOOLS = (
    "Я вас пока не знаю и поэтому ничего не покажу и не сделаю. "
    "Попросите, пожалуйста, взрослого из семьи добавить вас."
)
TEXT_INTERRUPTED = (
    "Ход прервался на полпути. Повторять сам не буду, чтобы не сделать дважды. "
    "Вот что успело произойти:"
)
TEXT_NOTHING_HAPPENED = "Ход прервался, ничего сделать не успел. Попробуйте ещё раз."
TEXT_LIMIT = "Я слишком долго кручусь вокруг этой задачи и остановился. Давайте уточним, что нужно."
TEXT_REFUSED = "Не могу этого сделать."


@dataclass
class LoopResult:
    """Чем кончился ход. Всё, что нужно логам и тестам, — без содержимого."""

    text: str
    stopped: str = STOP_COMPLETED
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ToolLoop:
    def __init__(
        self,
        *,
        journal: EffectJournal,
        services: Services,
        client: Any | None = None,
        registry: ToolRegistry = REGISTRY,
        model: str = DEFAULT_MODEL,
        family_language: str = "русский",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = DEFAULT_EFFORT,
        max_iterations: int = MAX_ITERATIONS,
        max_seconds: float = MAX_SECONDS,
        token_budget: int = TOKEN_BUDGET,
        clock=time.monotonic,
    ) -> None:
        self.journal = journal
        self.services = services
        self._client = client
        self.registry = registry
        self.model = model
        self.family_language = family_language
        self.max_tokens = max_tokens
        self.effort = effort
        self.max_iterations = max_iterations
        self.max_seconds = max_seconds
        self.token_budget = token_budget
        self.clock = clock

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic  # импорт ленивый: тесты живут без ключа

            self._client = anthropic.Anthropic()
        return self._client

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(language=self.family_language)

    # --- ход ----------------------------------------------------------------

    def run(
        self,
        context: RunContext,
        user_text: str,
        *,
        history: list[dict[str, Any]] | None = None,
    ) -> LoopResult:
        """Один ход диалога: сообщение человека → ответ, возможно через tools."""
        if not context.has_tools:
            # Неизвестному актору не отвечает даже модель: платить за чужой
            # промпт незачем, а «поговорить» — уже доступ.
            return LoopResult(text=TEXT_NO_TOOLS, stopped=STOP_NO_TOOLS)

        messages: list[dict[str, Any]] = [
            *(history or []),
            {"role": "user", "content": user_text},
        ]
        tools = self.registry.specs(context)
        result = LoopResult(text="")
        started = self.clock()

        while True:
            if result.iterations >= self.max_iterations:
                return self._stop(result, STOP_ITERATIONS)
            if self.clock() - started > self.max_seconds:
                return self._stop(result, STOP_TIMEOUT)
            if result.tokens > self.token_budget:
                return self._stop(result, STOP_BUDGET)

            try:
                response = self._call(context, messages, tools)
            except _CallFailed as failure:
                return self._interrupted(context, result, failure)
            result.iterations += 1
            self._account(result, response)

            if getattr(response, "stop_reason", None) == "refusal":
                # Отказ приходит обычным ответом; читать content до проверки —
                # значит падать на пустом списке.
                result.text = TEXT_REFUSED
                return self._stop(result, STOP_REFUSED)

            blocks = list(getattr(response, "content", ()) or ())
            if getattr(response, "stop_reason", None) != "tool_use":
                result.text = _text_of(blocks)
                return self._stop(result, STOP_COMPLETED)

            messages.append({"role": "assistant", "content": blocks})
            outcomes = [
                self._run_tool(context, block, result)
                for block in blocks
                if getattr(block, "type", None) == "tool_use"
            ]
            messages.append({"role": "user", "content": outcomes})

    # --- вызов модели -------------------------------------------------------

    def _call(
        self, context: RunContext, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        """Вызов API с единственным разрешённым повтором — пока ход пуст."""
        options: dict[str, Any] = {}
        if self.effort is not None:
            options["output_config"] = {"effort": self.effort}
        attempts = 0
        while True:
            attempts += 1
            try:
                return self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": self.system_prompt(),
                            # Системный префикс одинаков для всех ходов семьи.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=messages,
                    tools=tools,
                    **options,
                )
            except Exception as error:  # noqa: BLE001 — сеть и API-ошибки вперемешку
                if self.journal.has_effects(context.run_id):
                    # В ходе уже что-то произошло. Повторять запрос нельзя:
                    # модель может попросить то же самое ещё раз, а мы не
                    # знаем, что из отправленного дошло.
                    raise _CallFailed(error) from error
                if attempts > 1:
                    raise _CallFailed(error) from error
                logger.warning("повтор вызова модели после %s", type(error).__name__)

    def _account(self, result: LoopResult, response: Any) -> None:
        usage = getattr(response, "usage", None)
        result.input_tokens += getattr(usage, "input_tokens", 0) or 0
        result.output_tokens += getattr(usage, "output_tokens", 0) or 0

    # --- вызов инструмента --------------------------------------------------

    def _run_tool(self, context: RunContext, block: Any, result: LoopResult) -> dict[str, Any]:
        """Исполнить один `tool_use`: журнал → права → инструмент → результат."""
        tool_use_id = str(getattr(block, "id", "") or "")
        name = str(getattr(block, "name", "") or "")
        arguments = dict(getattr(block, "input", {}) or {})
        result.tool_calls.append(name)

        try:
            effect, fresh = self.journal.begin(
                run_id=context.run_id, tool_use_id=tool_use_id, kind=name
            )
        except EffectInFlight:
            # Тот же tool_use_id прямо сейчас исполняется в другом месте.
            return _tool_result(tool_use_id, "этот вызов уже выполняется", is_error=True)
        if not fresh:
            if effect.settled:
                # Тот же tool_use_id уже исполнялся: отдаём прежний результат,
                # второй эффект не создаём.
                logger.info("повтор tool_use %s — результат из журнала", tool_use_id)
                return _tool_result(
                    tool_use_id, effect.result or effect.error or "",
                    is_error=effect.status != "done",
                )
            return _tool_result(
                tool_use_id, "исход прошлой попытки неизвестен; повторять нельзя",
                is_error=True,
            )
        result.effects.append(effect.id)

        try:
            payload = self.registry.invoke(context, name, arguments, services=self.services)
        except CapabilityDenied as denied:
            self.journal.fail(effect.id, f"CapabilityDenied: {denied.capability}")
            return _tool_result(tool_use_id, f"нет прав: {denied.capability}", is_error=True)
        except (UnknownTool, ToolInputError) as error:
            # Ошибка модели, а не сбой: пусть исправится в следующей итерации.
            self.journal.fail(effect.id, f"{type(error).__name__}: {error}")
            return _tool_result(tool_use_id, str(error) or type(error).__name__, is_error=True)
        except Exception as error:  # noqa: BLE001 — инструмент не должен ронять ход
            self.journal.fail(effect.id, f"{type(error).__name__}: {error}")
            logger.warning("инструмент %s упал: %s", name, type(error).__name__)
            return _tool_result(tool_use_id, "инструмент не сработал", is_error=True)

        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        self.journal.complete(effect.id, rendered)
        return _tool_result(tool_use_id, rendered)

    # --- завершение ---------------------------------------------------------

    def _stop(self, result: LoopResult, reason: str) -> LoopResult:
        result.stopped = reason
        if reason in {STOP_ITERATIONS, STOP_TIMEOUT, STOP_BUDGET} and not result.text:
            result.text = TEXT_LIMIT
        return result

    def _interrupted(
        self, context: RunContext, result: LoopResult, failure: _CallFailed
    ) -> LoopResult:
        """Ход оборвался. Рассказываем по журналу, что успело произойти."""
        done = [
            effect for effect in self.journal.for_run(context.run_id)
            if effect.status == "done"
        ]
        logger.warning(
            "ход %s прерван после %s эффектов: %s",
            context.run_id, len(done), type(failure.error).__name__,
        )
        if not done:
            result.text = TEXT_NOTHING_HAPPENED
        else:
            lines = [TEXT_INTERRUPTED, *(f"— {effect.kind}" for effect in done)]
            result.text = "\n".join(lines)
        return self._stop(result, STOP_INTERRUPTED)


class _CallFailed(RuntimeError):
    """Вызов API не удался и повторять его больше нельзя."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def _tool_result(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


def _text_of(blocks: list[Any]) -> str:
    return "\n".join(
        str(getattr(block, "text", "")).strip()
        for block in blocks
        if getattr(block, "type", None) == "text"
    ).strip()
