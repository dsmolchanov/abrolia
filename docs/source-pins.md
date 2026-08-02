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

- `POST /v1/orgs`, `POST /v1/domains` (+`/dns`, `/verify`), `POST /v1/inboxes`, `POST /v1/keys`, `POST /v1/webhooks` (секрет — только в ответе create/rotate)
- `GET /v1/inboxes/{id}/threads[/{thread_id}]`, `GET /v1/messages/{id}/attachments/{aid}` (после scope-фикса)
- MCP: `list_threads, get_thread, search_inbox, draft_reply, send_reply, compose_email` (+`attachments` после Nerve-плана)
- Подпись webhook: `X-Nerve-Signature: t=..,v1=..` HMAC-SHA256 над `ts.body`

После реализации Nerve-плана (фазы 1–4) пины nerve-cloud/nerve-oss обновляются на landed-SHA, contract-тесты перегоняются.
