# Source Pins

Зафиксированные ревизии доноров и зависимостей (требование Gate −1 основного плана). Порт кода и contract-тесты ведутся против этих SHA; обновление пина — отдельный коммит с обоснованием.

| Репозиторий | Ветка | SHA | Роль |
|---|---|---|---|
| `dsmolchanov/hermes` (private) | `agent/foundation-activation` | `c26fc414ef02d259b2a22069eaddbff5314fd4c1` | Донор: actions-фасад (`claim_pending(code, chat, thread, actor)`), сторы, scheduler, Telegram/voice-конвейер, тесты |
| `dsmolchanov/nerve-cloud` | `main` | `c85d27c463a65b7e49358449ea0eacbcc7024bd5` | Email control plane. Consumer-baseline до реализации Nerve-плана |
| `dsmolchanov/nerve-oss` | `agent/compose-email-sender-name` | `a29da6c3c53dcbcd3a9170397d0efe2d47a54f2a` | MCP runtime (tools/schemas) |

## Связанные планы

- Основной: `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md`
- Nerve-расширения (Фаза 3): `nerve-cloud/thoughts/shared/plans/2026-08-02-inbound-events-and-attachments.md` (закоммичен в nerve-cloud @ `0ec3758`)

## API-снапшот Nerve, на который полагаемся

- `POST /v1/orgs`, `POST /v1/domains`, **`GET /v1/domains/dns`**, **`POST /v1/domains/verify`**, `POST /v1/inboxes`, `POST /v1/keys`, `POST /v1/webhooks` (секрет — только в ответе create/rotate)
- `GET /v1/inboxes/{id}/threads[/{thread_id}]`, `GET /v1/messages/{id}/attachments/{aid}` (после scope-фикса)
- MCP: `list_threads, get_thread, search_inbox, draft_reply_with_policy, send_reply, compose_email` (+`attachments` после Nerve-плана). Инструмента `draft_reply` не существует — в первой редакции документа имя было указано неверно (фактическое имя: `nerve-oss internal/mcp/types.go`)
- Подпись webhook: `X-Nerve-Signature: t=..,v1=..` HMAC-SHA256 над `ts.body`

После реализации Nerve-плана (фазы 1–4) пины nerve-cloud/nerve-oss обновляются на landed-SHA, contract-тесты перегоняются.

## Отложенный перепин hermes (решение ревизии Gate −1, 2026-08-02)

Текущий пин `c26fc41` — единственная ревизия донора, опубликованная на remote,
поэтому он и остаётся в документе. Но рабочая ветка ушла вперёд на 7 коммитов
до `a02ab33c5c287b0d6ccecd112042991d27c12203` (чистое дерево, 473 теста
зелёные), и разница `c26fc41..a02ab33` — 23 файла (+1695/−195) с исправлениями
crash-safety, scoped reads, барьеров, jobs, бэкапов и fail-closed
approval-лимитера. Портировать более старую базу означает переносить уже
исправленные дефекты.

**Перепин отложен, кандидат не принят.** По ревизии 2026-08-03 у `a02ab33`
статус валидации донора — `FAILED / NOT READY`, а сама ветка с тех пор ушла
дальше (на момент проверки HEAD — `e0ab625`, 17 коммитов впереди remote, дерево
грязное: незакоммиченные `backup_store.py`, `server.py`, `statectl.py`).
Пиновать движущуюся и не прошедшую валидацию базу нельзя.

Условия перепина (все сразу):

1. ветка `agent/foundation-activation` доведена до зелёного отчёта валидации;
2. дерево чистое, ветка запушена (пин обязан указывать на опубликованный коммит);
3. выбранный SHA зафиксирован отдельным коммитом здесь — с перечнем того, что
   меняется в портируемом слое, и с перегоном contract-тестов.

До этого в документе остаётся `c26fc41` — единственная опубликованная ревизия,
и порт кода из неопубликованных коммитов не начинается. Промежуточные `1633ffe`
и `a02ab33` кандидатами не считаются.
