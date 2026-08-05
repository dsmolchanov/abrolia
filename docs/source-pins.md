# Source Pins

Зафиксированные ревизии доноров и зависимостей (требование Gate −1 основного плана). Порт кода и contract-тесты ведутся против этих SHA; обновление пина — отдельный коммит с обоснованием.

| Репозиторий | Ветка | SHA | Роль |
|---|---|---|---|
| `dsmolchanov/hermes` (private) | `agent/foundation-activation` | `c26fc414ef02d259b2a22069eaddbff5314fd4c1` | Донор: actions-фасад (`claim_pending(code, chat, thread, actor)`), сторы, scheduler, Telegram/voice-конвейер, тесты |
| `dsmolchanov/nerve-cloud` | `main` | `c6f5ed03bdfa878aaea5806938e34e3e55c28a9d` | Production control plane после Phase 7: inbound event journal, durable attachments, org-scoped activation и SDK `0.2.0` contract |
| `dsmolchanov/nerve-oss` | `main` | `e3d011e2af5089b322340300066b8a79a2e9e603` | Runtime `v0.0.16`, MCP attachment schemas и core migrations through `0027` |

## Связанные планы

- Основной: `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md`
- Nerve-расширения (Фаза 3): `nerve-cloud/thoughts/shared/plans/2026-08-02-inbound-events-and-attachments.md` (production implementation pinned above at `c6f5ed03`)

## API-снапшот Nerve, на который полагаемся

- `POST /v1/orgs`, `POST /v1/domains`, **`GET /v1/domains/dns`**, **`POST /v1/domains/verify`**, `POST /v1/inboxes`, `POST /v1/keys`, `POST /v1/webhooks` (секрет — только в ответе create/rotate)
- Bootstrap-only domain-grant contract for managed `@abrolia.com`:
  `POST /v1/domain-grants`, `GET /v1/domain-grants` and
  `DELETE /v1/domain-grants/{id}`. The platform owner grants the household org
  access to the verified root domain **before** inbox creation. Abrolia's
  `NerveManagedEmailProvisioner` already follows this order; the exact request
  body and bootstrap credential boundary are covered by
  `test_client_uses_bootstrap_admin_and_exact_platform_grant_contract`.
- `GET /v1/inboxes/{id}/threads[/{thread_id}]`, `GET /v1/messages/{id}/attachments/{aid}` с `nerve:email.read`; состояния `pending`, `available`, `expired`, `too_large`, `failed` имеют типизированную семантику SDK
- MCP: `list_threads, get_thread, search_inbox, draft_reply_with_policy, send_reply, compose_email`; `compose_email`/`send_reply` принимают `attachments`, `draft_reply_with_policy` — нет. Инструмента `draft_reply` не существует — в первой редакции документа имя было указано неверно
- Attachment capability has a separate activation prerequisite: the household
  org must have effective `attachments=true`, verified with its runtime key at
  `GET /internal/feature-flags/attachments`. The global default remains off.
  Until the org flag converges, `tools/list` omits attachment inputs and an
  attempted attachment send returns `attachment_feature_disabled`; a valid
  inbox/key alone is not evidence that attachment onboarding is complete.
- Подпись webhook: `X-Nerve-Signature: t=..,v1=..` HMAC-SHA256 над `ts.body`

## Nerve Phase 7 release evidence (2026-08-05)

Пины обновлены после завершённого production cutover, а не по движущейся
ветке. Upstream production workflow
[`30997902548`](https://github.com/dsmolchanov/nerve-cloud/actions/runs/30997902548)
прошёл snapshot rehearsal, миграции до core `0027` / cloud `0008`, backfill до
`remaining_pending_total=0`, household-canary isolation, реальный inbound PDF,
подписанный `email.received`, внешний outbound attachment и pilot smoke.

Неизменяемые артефакты этого пина:

- runtime `v0.0.16`:
  `sha256:4f82687e4d62047a251cb24dad2157b49c340eb55cd9cc7ee32d83bc4d59161e`;
- control plane:
  `sha256:333b02e923f8e176e73e139a020fad3988bda3ea9f31a413ec8a60857fc6d215`;
- `nerve-email==0.2.0` wheel:
  `9f0a7d6316bf47eef64236f96d1a7a151b5517641930422b1b16711da8b02540`.

Abrolia `main` на момент перепина содержит HTTP-contract тесты bootstrap/admin
provisioning, но ещё не содержит полный live consumer-suite из Phase 3. Поэтому
upstream production contract и локальный provisioning suite записываются как
два разных доказательства; локальные тесты нельзя называть staging/live smoke.

**Attachment activation gate:** managed provisioning создаёт domain grant и
inbox, после чего остаётся resumable `waiting_user`. Оператор включает
org-scoped `attachments` только через аудируемую upstream-команду `nerve-flags`
(см. `scripts/activate_nerve_attachments.sh`). Затем Abrolia проверяет effective
state household runtime key через `GET /internal/feature-flags/attachments` и
объявляет capability ready только при точном `enabled=true`; ошибки probe
остаются fail-closed `outcome_unknown`. Это закрывает
[abrolia#6](https://github.com/dsmolchanov/abrolia/issues/6) без нового
bootstrap writer endpoint.

Consumer verification на `abrolia@1596310502f7127de2ef83512e4ffa9ca71ffa42`:

- Nerve/email subset: 42 passed;
- полный pytest: 670 passed, 1 skipped (единственный skip — Anthropic live test
  без `ANTHROPIC_API_KEY`, к Nerve-контракту не относится);
- `ruff check .` и публичный fixture sanitizer — green. Приватный deny-list
  доступен только CI, поэтому `--require-deny` остаётся обязательным PR gate.

## Отложенный перепин hermes (решение ревизии Gate −1, 2026-08-02)

Текущий пин `c26fc41` — единственная ревизия донора, опубликованная на remote,
поэтому он и остаётся в документе. Рабочая ветка ушла далеко вперёд и содержит
исправления crash-safety, scoped reads, барьеров, jobs, бэкапов и fail-closed
approval-лимитера — портировать более старую базу означает переносить уже
исправленные дефекты. Это довод в пользу будущего перепина, но не основание
сделать его сейчас: см. ниже.

**Перепин отложен, кандидат не принят.** По ревизии 2026-08-03 у `a02ab33`
статус валидации донора — `FAILED / NOT READY`. Ветка при этом активно движется:
за один день проверки её HEAD сменился трижды (`1633ffe` → `a02ab33` → `e0ab625`
→ `e1be033`, ~20 коммитов впереди remote), а зелёная independent validation не
получена (последний прогон — `FAILED / ROLLOUT NOT AUTHORIZED`; deploy и drills
тоже не пройдены). Конкретный HEAD здесь намеренно не фиксируется — он
устаревает быстрее документа; фиксируется правило: пиновать движущуюся и не
прошедшую валидацию базу нельзя.

Условия перепина (все сразу):

1. ветка `agent/foundation-activation` доведена до зелёного отчёта валидации;
2. дерево чистое, ветка запушена (пин обязан указывать на опубликованный коммит);
3. выбранный SHA зафиксирован отдельным коммитом здесь — с перечнем того, что
   меняется в портируемом слое, и с перегоном contract-тестов.

До этого в документе остаётся `c26fc41` — единственная опубликованная ревизия,
и порт кода из неопубликованных коммитов не начинается. Промежуточные `1633ffe`
и `a02ab33` кандидатами не считаются.
