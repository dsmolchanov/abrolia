"""Транспорт в консоль: посмотреть карточку, не подключая бота.

Нужен для двух случаев — первый запуск на новой машине и отладка промпта,
когда заводить канал ради одного письма избыточно. Кнопки печатаются вместе с
их callback-данными, поэтому подтвердить предложение можно вручную:
`hermes-cloud confirm <id>` делает ровно то же, что нажатие ✅ в чате.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConsoleTransport:
    """Печатает то, что ушло бы в канал. Ничего наружу не отправляет."""

    documents_dir: str = "."
    sent: list[str] = field(default_factory=list)

    def send_message(
        self, *, chat: str, text: str, thread: int | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> str:
        print("\n" + "─" * 60)
        print(text)
        if buttons:
            print("─" * 60)
            for label, data in buttons:
                print(f"  {label}   →   {data}")
        print("─" * 60)
        self.sent.append(text)
        return f"console-{len(self.sent)}"

    def send_document(
        self, *, chat: str, filename: str, content: bytes, caption: str = "",
        thread: int | None = None,
    ) -> str:
        from pathlib import Path

        path = Path(self.documents_dir) / filename
        path.write_bytes(content)
        print(f"\n[файл] {path}  — {caption}")
        return str(path)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        return None

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict[str, Any]]:
        """В консоли апдейтов нет: подтверждение идёт командой `confirm`."""
        return []
