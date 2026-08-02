# Hermes Cloud — Family Ops Assistant MVP Implementation Plan (v2)

> v2 после внешней ревизии 2026-08-02. Ключевые изменения против v1: fail-closed residency вместо fail-open `inference_geo`; долговечный ingress (SQLite WAL events/jobs/effects) вместо «журнала message_id»; ручной tool-loop с идемпотентными эффектами вместо автоматического runner-replay; RunContext-авторизация каждого хода; WhatsApp исключён из MVP (официальный Business Platform — post-pilot); compose вместо reply для пересланных писем; Nerve-работы вынесены в отдельный план; GDPR перенесён в Gate −1; пилотная цена €59–99.

## Overview

Пилотный MVP «операционного ассистента для семьи, живущей не на родном языке»: пересланное письмо школы / фото объявления / голосовая заметка → извлечённое обязательство (дата, сумма, ответственный) на языке семьи → карточка-предложение → после подтверждения — событие в семейном Google-календаре, задача, напоминание или исходящее письмо с явно подтверждённым получателем. Dedicated-инстанс на household. Прототип — на синтетических данных; реальные семьи — только после закрытия Gate −1 и Trust Foundation.

Активы: **Hermes** (донор state-машин и тестов; ветка `agent/foundation-activation` — уже содержит фасад `claim_pending(code, chat, thread, actor)` c per-actor rate-limit, actions.py:175), **Nerve** (email-слой; требует расширений — отдельный план), **Anthropic Commercial API** (модельный слой).

## Current State Analysis

### Hermes (донор)

- **Внимание: донор движется.** Рабочая ветка `agent/foundation-activation` переписала actions.py в фасад над outbox-бэкендом: `claim_pending(code, chat, thread, actor)` / `claim_pending_by_id`, exact chat+thread биндинг, failed-code rate limit per (actor, chat) (actions.py:48, :160-191). Портируем **эту** версию, не HEAD master. Gate −1 фиксирует донорский SHA.
- Сторы читают пути из env при импорте; тесты изолируются через setenv+reload — при порте пути становятся параметрами конструктора.
- Известные дефекты донора (проверить актуальность против запиненного SHA — часть уже исправлена в foundation-ветке): неатомарная запись reminders (старый reminders.py:91), отсутствие fsync, гонки check-then-act вне критических секций.
- Email-ингеста непрошенной почты нет (mailwatch поллит только watches) — вход строится с нуля.
- Модельный слой (`claude -p` subprocess) — полная замена на SDK.
- Календарь: gtool.py — googleapiclient-обёртки, authorized-user token refresh-in-place, scopes `calendar`+`drive.readonly` — портируется.
- Тесты: ~60 из 102 портируются, **но фикстуры содержат реальные PII** (test_sender.py:9 — реальный Telegram ID и имена) — в публичный репо только после санитизации.

### Nerve

- Онбординг (org → managed-поддомен/BYO-домен+DNS → inbox → cloud API key со скоупами) готов; RLS-тенантность; SDK `NerveAdmin`/`NerveClient`; готовые Anthropic tool definitions.
- **Пробелы (закрываются отдельным Nerve-планом, см. Фазу 3):**
  1. inbound-пуша с гарантиями нет: `forward_to` — unsigned/no-retry; org-webhooks рассылают только outbound delivery events, и их доставка завязана на outbound-журнал — для `email.received` нужен общий event journal, а не «маленькое изменение»;
  2. метаданные входящих вложений не персистятся, а attachment-proxy требует скоуп `nerve:admin.billing | nerve:email.inbox.create` (handler_messages.go:43) — runtime-ключу household недоступен; нужен и персист, и scope-фикс;
  3. исходящих вложений нет нигде в тракте (provider.go, outbox.go, outbox_worker.go — ноль упоминаний) — это store+worker+MCP+SDK работа с content-addressed blobs;
  4. webhook-секрет генерируется сервером и показывается один раз (org_webhooks.go:62-81) — provisioning должен читать его из ответа, не задавать свой.
- **Семантика reply:** `send_reply(thread_id)` отвечает адресу From последнего сообщения. Пересланное родителем письмо имеет From = родитель → reply уйдёт родителю, не школе. MVP использует **compose** с извлечённым из пересланной цепочки оригинальным отправителем и обязательным подтверждением получателя в карточке.

### Провайдеры и право (входные ограничения)

- `inference_geo` на first-party API сегодня — `global`/`us`; EU-роутинг — только Vertex AI EU (отдельный клиент/контракт). Haiku 4.5 `inference_geo` не поддерживает.
- Nerve деплой — `iad`; Resend хранит метаданные/логи в США независимо от sending region.
- Вывод: обещание «EU processing» в чистом виде сейчас невыполнимо → фиксируем честную формулировку и fail-closed конфигурацию (см. Locked Decisions).
- WhatsApp: QR-пейринг Evolution = автоматизация обычного WhatsApp, для коммерческого продукта недопустим; официальный путь — WhatsApp Business Platform (EEA/Brazil AI-продукты разрешены с ограничениями). Вне MVP.
- GDPR: письма школ содержат данные детей и третьих лиц → data map, lawful bases, DPIA, privacy notice, реестр процессоров, DPA/SCC/TIA, политика по несовершеннолетним — **до** кода, работающего с реальными данными.

## Desired End State

Пилот на 5–20 семей: провижининг одним прогоном (без WhatsApp); пересланное письмо проходит durable-конвейер (событие переживает рестарт, не теряется и не дублируется); карточка на языке семьи; подтверждение создаёт событие/задачу/письмо ровно один раз (включая crash-recovery); residency-режим и субпроцессоры честно задокументированы; `/export`/`/delete` с определённой owner-auth; экономика проверяется на €59–99/мес.

Verify: сквозной чек-лист Фазы 5 + chaos-тесты (kill -9 в каждом окне) из Фазы 2.

## Locked Decisions

| Решение | Выбор |
|---|---|
| Скоуп | Пилотный MVP, dedicated-инстанс на household; прототип — synthetic data only |
| Репозиторий | Публичный `dsmolchanov/hermes-cloud`; донорские фикстуры — только санитизированные |
| Residency | Честная формулировка: «EU-hosted application, документированные международные передачи»: приложение и данные — Fly EU (`ams`), субпроцессоры Anthropic (global/US) и Resend (US) — под DPA/SCC в реестре процессоров. Конфиг `residency_mode: eu-app | eu-strict`; `eu-strict` требует Vertex-EU-клиент и падает при его отсутствии — **никакого молчаливого downgrade**. Пилот стартует в `eu-app`; Vertex EU — задокументированный upgrade-путь после бенчмарка |
| Email-вход | Nerve: managed-поддомен (база) / BYO-домен + DNS (pro) |
| Исходящий email | Только `compose_email` с явно подтверждённым получателем (оригинальный отправитель из пересланной цепочки); auto-reply на thread — вне MVP |
| WhatsApp | **Вне MVP.** Post-pilot через официальный WhatsApp Business Platform; Evolution/QR не используется для клиентов |
| Календарь | Семейный Google: выделенный аккаунт ассистента на household, семья шарит календари; порт gtool; клиентские детерминированные event ID + reconcile |
| Модели | Диалог — `claude-opus-5`. Извлечение — старт на `claude-opus-5` (quality-first), выбор рабочей модели (Haiku/Sonnet/Opus) — по бенчмарку на обезличенном корпусе в Фазе 1; порог confidence не считается калиброванной вероятностью — управляет только рендером карточки, не автоисполнением |
| Ingress | SQLite (WAL) `events/jobs/effects`: fsync-before-ACK, leases, FIFO per context, DLQ + replay CLI; webhook только верифицирует подпись и фиксирует событие, обработка — background worker |
| Tool-цикл | Ручной loop (не runner): policy-check и аудит каждого tool_use; `run_id`+`effect_id`-идемпотентность мутирующих tools; запрет авто-replay хода после первого side effect; лимиты итераций/времени/токенов |
| Авторизация | Server-issued `RunContext {household, actor, chat, thread, scope, read_caps, mutate_caps}` на каждый ход; unknown/anonymous actor — ноль tools; `memory_append` — только family-actor и через staged-подтверждение |
| Хранение | SQLite WAL на per-household Fly volume (заменяет и file-JSON сторы v1 — единый транзакционный слой); Postgres — после пилота |
| Цена пилота | €59–99/мес dedicated (COGS честно: ~$8–11 inference + ~$12 Fly + Nerve/backups/monitoring ≈ $20–25 до поддержки) |

## What We're NOT Doing (MVP)

- WhatsApp (любой), auto-reply на email-thread, multi-tenant в одном процессе
- Web vault PWA, мобильные клиенты, billing-автоматизация
- Обещания E2EE / zero-knowledge / «EU processing» без оговорок
- OAuth к почте клиента; Postgres; миграция семейного Hermes
- Автоисполнение чего-либо по порогу confidence

## Implementation Approach

Порядок фаз — по принципу «сначала доверие, потом функции»: Gate −1 (право/санитизация/пиннинг) → тонкий вертикальный срез на синтетике → trust foundation (durable execution + авторизация) → Nerve-расширения (отдельный план) → реальные действия → пилотизация. Реальные данные семей не попадают в систему до завершения Фазы 2.

Структура репозитория — как в v1 (`hermes_cloud/{core,runner,ingest,execute,scheduler}`, `onboarding/`, `tests/`), с заменами: `core/db.py` (SQLite WAL, миграции), `core/runcontext.py`, `ingest/worker.py`; `execute/whatsapp.py` исключён.

---

## Phase 0 (Gate −1): Right-to-build

### Overview
Юридический и инженерный фундамент до любого кода, касающегося реальных данных. Блокирующий gate: ни одна следующая фаза не принимает реальные данные, пока Gate −1 не закрыт.

### Changes Required

1. **Пиннинг доноров**: закоммитить/зафиксировать состояние `hermes@agent/foundation-activation` (сейчас есть незакоммиченные изменения в reminders/scheduler/todos/mailwatch — договориться с владельцем ветки о фиксации), записать SHA донора и SHA nerve-cloud/nerve-oss в `docs/source-pins.md`; API-снапшот Nerve-эндпоинтов, на которые полагаемся.
2. **Санитизация**: `tests/fixtures/` — только синтетика (RFC-example домены, телефоны из зарезервированных диапазонов, вымышленные имена, Telegram ID из документированного synthetic-диапазона); скрипт `scripts/check_fixtures.py` (запрещённые паттерны: реальные ID донора, @gmail, +7/+34-номера) + gitleaks в CI; правило в CONTRIBUTING: донорские payload-фикстуры не копируются as-is.
3. **Privacy-пакет** (`docs/privacy/`): data map (классы данных × хранилища × TTL), lawful bases, DPIA-драфт, privacy notice (RU/EN), реестр процессоров (Anthropic, Resend/Nerve, Fly, Google, Telegram) с DPA/SCC/TIA-статусами, политика данных несовершеннолетних, incident-response заметка.
4. **Threat model** (`docs/SECURITY.md`): акторы (внешний отправитель письма, prompt injection в контенте/вложении, неизвестный участник Telegram-группы, компрометация одного household), границы (модель без shell/send, staged-sends, RunContext), явные не-цели.
5. **Retention-матрица**: TTL для transcripts (180д), memory (пересмотр раз в 90д), actions journal (365д), todos/reminders (done+90д), photos/voice/docs (30д), delivery receipts (365д) — фиксируется в data map и реализуется в Фазе 2.
6. README: заменить «EU processing» на честную формулировку residency (см. Locked Decisions).

### Success Criteria

#### Automated Verification:
- [ ] `python scripts/check_fixtures.py` и gitleaks в CI зелёные на всей истории
- [ ] `docs/source-pins.md` существует и содержит три SHA

#### Manual Verification:
- [ ] Privacy-пакет отревьюирован владельцем продукта (и юристом до реальных семей)
- [ ] Донорская ветка зафиксирована, SHA записан

**Пауза для ручного подтверждения.**

---

## Phase 1: Thin vertical slice (synthetic data)

### Overview
Минимальный сквозной контур на синтетике: durable event → extraction → Telegram-карточка → reminder или ICS. Без Nerve-зависимости (вход — CLI-инжект .eml), без outbound email, без Google write, без WhatsApp. Параллельно — модельный бенчмарк.

### Changes Required

1. **`core/db.py`**: SQLite WAL, таблицы `events {id, source, external_id UNIQUE, raw BLOB, received_at, status: received|processing|done|failed, lease_until, attempts}`, `jobs`, `effects {id, run_id, tool_use_id, kind, payload_sha, status, receipt}`; fsync-before-commit; миграции (простые нумерованные .sql).
2. **`ingest/inject.py`**: CLI `hermes-cloud inject-eml path.eml` — парсинг .eml (включая пересланные цепочки: извлечение оригинального From/Date из forwarded-заголовков и тела), запись события. Это же ядро потом переиспользует nerve-webhook.
3. **`ingest/worker.py`**: цикл: lease событие (FIFO per context, `lease_until`, reclaim просроченных) → extraction → карточка → `done`/`failed(attempts++)`; DLQ после N попыток; `hermes-cloud replay <event_id>`.
4. **`runner/extraction.py`**: Pydantic `ExtractionResult` (как v1 + `original_sender {email, name, confidence}` из forwarded-цепочки); `messages.parse` на `claude-opus-5`; политика untrusted-контента; вложения — content blocks.
5. **Карточка**: staged-предложение (порт фасада actions с claim_pending донора) c кнопками ✅/✏️/❌; `payment`/`info` рендерятся: payment → задача с суммой и дедлайном; info → сообщение без кнопок. ✏️ отменяет staged-код (invalidation) и открывает диалог, новое предложение = новый код.
6. **Исполнители slice**: только `reminder` (порт ReminderStore поверх SQLite) и `ics` (генерация файла в чат). Порт минимума Telegram-plumbing (webhook, allowlists, resolve_sender, send_reply, кнопки).
7. **`bench/`**: корпус ≥40 синтетических писем (DE/IT/NL/EN × школа/счёт/приглашение/спам/adversarial/forwarded-chain/OCR-фото), golden-ожидания; прогон Haiku/Sonnet/Opus → отчёт точности/стоимости; решение по extraction-модели фиксируется здесь.

### Success Criteria

#### Automated Verification:
- [ ] `pytest`: событие переживает kill между insert и обработкой (реобработка), дубль external_id — no-op, DLQ после N неудач, replay работает
- [ ] Forwarded-парсер: оригинальный отправитель извлекается на фикстурах пересылок (Gmail/Outlook/Apple Mail форматы)
- [ ] `bench/run.py` выдаёт отчёт по трём моделям (live, `@pytest.mark.live`)
- [ ] Инъекционный корпус: 0 staged-действий, не соответствующих фактическому содержанию

#### Manual Verification:
- [ ] Синтетическое немецкое школьное письмо через inject-eml → корректная карточка на русском → ✅ → напоминание сработало
- [ ] ICS-файл открывается в Google/Apple Calendar
- [ ] Решение по extraction-модели принято и записано в план

**Пауза для ручного подтверждения.**

---

## Phase 2: Trust foundation

### Overview
Инварианты, позволяющие подключать реальные данные: авторизация каждого хода, идемпотентность эффектов, crash-safe исполнение подтверждений, retention/export/delete, backup.

### Changes Required

1. **`core/runcontext.py`**: `RunContext` собирается сервером на входе каждого хода из проверенного Telegram-апдейта: `{household_id, actor_id, chat_id, thread_id, scope: personal|shared, read_caps, mutate_caps}`. Маппинг actor→роль в `household.toml`. Unknown/anonymous actor: ответ без tools («представьтесь / попросите взрослого добавить вас»), нулевые caps. Все tools получают RunContext первым аргументом и проверяют caps сами (defense in depth) — тест на каждый tool × unknown actor.
2. **Ручной tool-loop** (`runner/model.py`): цикл `messages.create` → перебор tool_use блоков → policy check (caps, лимиты) → исполнение → журналирование в `effects (run_id, tool_use_id)` → tool_result. Идемпотентность: повторный тот же `tool_use_id` возвращает прежний результат без повторного эффекта. Retry API-вызова разрешён только если в текущем ходе ещё нет записанных эффектов; иначе — честное сообщение «ход прерван, вот что успело выполниться» из журнала. Лимиты: ≤8 итераций, ≤5 мин, токен-бюджет.
3. **`memory_append`**: только family-actor; запись становится staged-предложением («добавить в память: …» ✅/❌) — persistent injection через контент писем невозможен.
4. **Approval-исполнение crash-safe**: `claim_pending` (порт донорского фасада: code+chat+thread+actor, rate-limit неверных кодов) пишет attempt в `effects` в той же транзакции; executor-вызов с lease; startup-reconciliation: висящие `executing` с истёкшим lease → канальная стратегия: nerve-email — повторить с тем же idempotency_key; calendar — reconcile по детерминированному client-generated event ID (`events.insert` с заданным `id`, поиск перед повтором); прочее → `outcome_unknown`, никогда слепой retry.
5. **Retention/export/delete**: ежедневная retention-джоба по матрице Gate −1; `GET /export` и `POST /delete` — owner-auth: инициируются только primary-owner-актором + повторное подтверждение кодом (тот же approval-механизм); delete-runbook перечисляет внешние поверхности (nerve-org, Resend-остатки, Google-события, Anthropic retention, Telegram-копии, бэкапы) с процедурой по каждой; в приложении — wipe SQLite+файлов+tombstone.
6. **Backup/restore**: Litestream (или периодический snapshot) SQLite на EU object storage, шифрование per-household ключом; `restore.md` + тест восстановления.

### Success Criteria

#### Automated Verification:
- [ ] Chaos-тесты: kill -9 в каждом окне (после claim/до executor; после executor/до mark; в середине tool-loop) → после рестарта ровно один эффект, статусы согласованы
- [ ] Матрица авторизации: каждый tool × {family, guest, unknown} — ожидаемые allow/deny
- [ ] Повтор tool_use_id не создаёт второй эффект; retry хода с эффектами запрещён
- [ ] `/export` полон по data map; `/delete` без owner-auth отклоняется
- [ ] Restore из бэкапа проходит smoke-набор

#### Manual Verification:
- [ ] Неизвестный участник, добавленный в группу, не получает доступ к почте/памяти/календарю
- [ ] Ревью delete-runbook

**Пауза для ручного подтверждения. После этой фазы разрешены реальные данные владельца-теста (не клиентов).**

---

## Phase 3: Nerve extension (отдельный план в nerve-cloud)

### Overview
Работы в nerve-cloud/nerve-oss/SDK — **отдельный план** `nerve-cloud/thoughts/shared/plans/2026-08-XX-inbound-events-and-attachments.md` со своими миграциями, contract-тестами и последовательностью PR. Здесь — только контракт-требования потребителя.

### Контракт, который обязан дать Nerve-план

1. **Общий event journal + фан-аут `email.received`** в org-webhooks (HMAC, ретраи) — с учётом того, что текущая доставка завязана FK на outbound-журнал: нужна общая таблица событий или эквивалент, не «просто ещё один event type».
2. **Входящие вложения**: персист метаданных (`message_attachments`), выдача в GET thread, и **scope-фикс attachment-proxy** — доступ по runtime-ключу household (сейчас требует `nerve:admin.billing | nerve:email.inbox.create`, handler_messages.go:43).
3. **Исходящие вложения**: content-addressed blob store → outbox → worker → Resend/SMTP; лимит 10MB, MIME-allowlist; MCP `compose_email(attachments=)`; SDK 0.2.0.
4. **Webhook-секрет**: серверная генерация, показ один раз (уже так, org_webhooks.go:68) — provisioning hermes-cloud читает секрет из ответа `create_webhook`, никогда не задаёт свой.
5. Contract-тесты, которые hermes-cloud прогоняет против staging: подпись/ретрай/дедуп `email.received`; скачивание вложения runtime-ключом; отправка compose с PDF.

### Success Criteria

#### Automated Verification:
- [ ] Nerve-план написан, принят и реализован (свои критерии там)
- [ ] Contract-тесты hermes-cloud против staging-Nerve зелёные

#### Manual Verification:
- [ ] Письмо с PDF на staging-inbox → подписанный webhook → вложение скачано runtime-ключом → compose с вложением доставлен

**Пауза. Фазы 1–2 не зависят от этой фазы (inject-eml); Фаза 4 email-часть — зависит.**

---

## Phase 4: Real actions

### Overview
Google Calendar write и исходящий email с доказанной семантикой получателя; подключение nerve-webhook как второго входа рядом с inject-eml.

### Changes Required

1. **`ingest/nerve_webhook.py`**: `POST /webhooks/nerve` — верификация `X-Nerve-Signature` (окно ±5 мин), немедленный insert события + 200; всё остальное — worker (общий с Фазой 1). Вложения качаются в worker сразу (Resend-URL ~1ч) в content-addressed store.
2. **`execute/gcal.py`**: порт gtool; executor `calendar_event` с client-generated deterministic event ID = f(action_id); reconcile-поиск перед любым повтором; линк события в чат. Read-only tool `calendar_list_events` (caps: family).
3. **`execute/email_send.py`**: executor `email_compose` → `NerveClient.compose_email(to=подтверждённый получатель, attachments=?, idempotency_key=action_id)`. Карточка исходящего письма всегда показывает получателя отдельной строкой «Кому: …» — подтверждение покрывает и текст, и адресата. Delivery-webhooks (`email.delivered|bounced`) обновляют журнал.
4. **`ActionBundle`**: извлечение одного письма может породить связку (событие + задача о взносе). Карточка-бандл: общий заголовок, дочерние пункты с чекбоксами (по умолчанию все включены), одно подтверждение → транзакционное staged-исполнение каждого дочернего эффекта со своим effect_id; частичный фейл отражается по-пунктно.
5. Фото/голос-ингест (порт vision-блоков и faster-whisper) — переносится из v1 без изменений сути; scheduler/digest — порт с SchedulerConfig.

### Success Criteria

#### Automated Verification:
- [ ] Идемпотентность calendar: повторное исполнение того же action не создаёт второе событие (мок + live-smoke)
- [ ] Compose: получатель в payload обязан совпасть с подтверждённым в карточке (payload-sha это гарантирует) — тест на подмену
- [ ] Bundle: частичный фейл → корректные статусы каждого дочернего эффекта
- [ ] Webhook-вход и inject-eml дают идентичный pipeline-результат на одном .eml

#### Manual Verification:
- [ ] Синтетика end-to-end: письмо «экскурсия 12.09, взнос 15€» → бандл (событие+задача) → ✅ → событие в шаренном календаре, задача в списке
- [ ] Staged-письмо школе с PDF: карточка показывает «Кому: schule@example.de», после ✅ доставлено
- [ ] Kill-switch email_send=0 блокирует с честным сообщением

**Пауза для ручного подтверждения.**

---

## Phase 5: Pilotization

### Overview
Воспроизводимый пилот: провижининг, наблюдаемость, cost-caps, метрики, восстановление.

### Changes Required

1. **`onboarding/provision.py`** (без WhatsApp): nerve org+inbox/BYO-домен (webhook-секрет — из ответа сервера), Fly-приложение в `ams` + volume + секреты, Google-аккаунт ассистента (`google_oauth_flow.py`, consent «In production»), генерация `household.toml` в приватный ops-стор; `--dry-run`.
2. **Observability**: структурные логи (без контента писем — только ID/статусы), `/health` (nerve key, telegram, google token, db, backup age), алёрты (DLQ>0, застрявшие executing, backup стар, бюджет превышен).
3. **Cost caps**: счётчик токенов per-household/день (из usage ответов) с мягким лимитом (деградация: extraction-only, диалог отвечает «дневной бюджет исчерпан») — защита от runaway-цикла и от abuse.
4. **Rollback/upgrade**: релизы по тегам, migrate-on-start с backup-before-migrate, runbook отката.
5. **Метрики пилота** (из вердикта go-сигналов): активность на 8-й неделе, подтверждённые операции/семью/неделю, доля принятых без правок, ноль несанкционированных действий — события в лог, скрипт сводки.
6. Прайсинг: €59–99 dedicated; страница/письмо-оффер вне этого плана.

### Success Criteria

#### Automated Verification:
- [ ] `provision.py --dry-run` на staging проходит все шаги
- [ ] CI: pytest + ruff + gitleaks + check_fixtures на каждый PR
- [ ] Cost-cap тест: превышение бюджета переводит в деградацию, не в тишину

#### Manual Verification:
- [ ] Онбординг тестового household с нуля ≤60 мин по runbook
- [ ] BYO-домен: DNS из `get_dns_records` проходит верификацию
- [ ] Сквозной wedge-сценарий на первом реальном household владельца
- [ ] Upgrade+rollback отрепетированы

---

## Testing Strategy

- **Unit**: портированный baseline с санитизированными фикстурами; chaos-тесты Фазы 2; авторизационная матрица.
- **Bench**: корпус Фазы 1, расширяемый — каждая ошибка пилота становится фикстурой; регрессионный прогон перед сменой модели/промпта.
- **Contract**: hermes-cloud ↔ Nerve staging (Фаза 3).
- **Live smoke** (`@pytest.mark.live`): tool-loop, cache-hit, structured outputs, gcal idempotency.

## Performance / COGS

Inference $8–11 + Fly ~$12 + Nerve/Resend + бэкапы/мониторинг ≈ **$20–25/household до поддержки** → пилотная цена €59–99. Пересмотр после бенчмарка extraction-модели (даунгрейд с Opus может срезать 30–50% inference-части). Prompt caching обязателен (системный префикс household).

## Migration Notes

- Семейный Hermes не трогаем.
- SQLite-слой проектируется схемой, переносимой на Postgres (без SQLite-специфики в запросах, ID — UUID) — постпилотная миграция отдельным планом.
- Nerve-изменения — additive; SDK 0.2.0 без breaking changes.

## References

- Доноры (SHA — в `docs/source-pins.md` после Gate −1): `~/Programs/hermes` @ `agent/foundation-activation` (actions-фасад: actions.py:124-191), `~/Programs/nerve-cloud` (handler_messages.go:43 — scope attachment-proxy; org_webhooks.go:62-81 — серверный секрет), `~/Programs/nerve-oss` (tools/service.go — reply-семантика)
- Ревизия v1→v2: сессия 2026-08-02 (12 пунктов; все верифицируемые подтверждены по коду)
- Прошлые планы: `hermes/thoughts/shared/plans/2026-07-02-hermes-family-assistant.md`, `2026-08-01-hermes-platform-foundation-activation.md`
- Право: Anthropic data residency (inference_geo: global/us), Resend regions (US metadata), WhatsApp Business Solution Terms, GDPR/DPIA, AI Act Art. 50
