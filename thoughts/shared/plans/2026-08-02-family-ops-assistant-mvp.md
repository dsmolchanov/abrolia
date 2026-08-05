# Abrolia (Hermes Cloud) — Family Ops Assistant MVP Implementation Plan (v3)

> v3 фиксирует решение владельца от 2026-08-04: трёхшаговый онбординг `email-идентичность → WhatsApp-идентичность → предпочтительный канал общения`, Telegram по умолчанию, WhatsApp Beta и минимальный Abrolia Web как альтернативы, email как обязательный fallback. Email-опции приведены к продуктовой формулировке Abrolia: `@abrolia.com` по умолчанию, отдельный Gmail агента через Google OAuth, либо домен семьи. Технический фундамент v2 (residency, durable ingress, RunContext, staged effects, Nerve-план и Gate −1) сохраняется.

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
- WhatsApp: QR-пейринг Evolution = автоматизация обычного WhatsApp Web — вне официальных условий; реальный риск — блокировка номера. Решение владельца: в MVP общий номер Abrolia даёт quick start только для семейного диалога, а семья может подключить свой выделенный номер с informed consent для полного внешнего контура. Официальный Business Platform не считается глобальным автоматическим GA-путём: действующие Business Solution Terms ограничивают AI-provider/general-purpose-assistant use case и требуют географического eligibility review (на дату v3 исключение заявлено для пользователей с EEA/Brazil country codes). Обязательное следствие для dedicated режима: per-instance apikey и per-household relay-секрет (сервис-глобальный ключ Evolution — недопустим); для shared режима — отдельный sender-bound gateway с изоляцией household.
- GDPR: письма школ содержат данные детей и третьих лиц → data map, lawful bases, DPIA, privacy notice, реестр процессоров, DPA/SCC/TIA, политика по несовершеннолетним — **до** кода, работающего с реальными данными.

## Desired End State

Пилот на 5–20 семей: возобновляемый трёхшаговый онбординг создаёт email- и WhatsApp-идентичности агента, затем связывает предпочтительный канал семьи; Telegram предвыбран, WhatsApp доступен как Beta после шага 2, минимальный Abrolia Web — независимый fallback. Пересланное письмо проходит durable-конвейер (событие переживает рестарт, не теряется и не дублируется); карточка на языке семьи; подтверждение создаёт событие/задачу/письмо ровно один раз (включая crash-recovery); residency-режим и субпроцессоры честно задокументированы; `/export`/`/delete` с определённой owner-auth; экономика проверяется на €59–99/мес.

Verify: сквозной чек-лист Фазы 5 + chaos-тесты (kill -9 в каждом окне) из Фазы 2.

## Locked Decisions

| Решение | Выбор |
|---|---|
| Скоуп | Пилотный MVP, dedicated-инстанс на household; прототип — synthetic data only |
| Репозиторий | Публичный `dsmolchanov/abrolia`; донорские фикстуры — только санитизированные |
| Residency | Честная формулировка: «EU-hosted application, документированные международные передачи»: приложение и данные — Fly EU (`ams`), субпроцессоры Anthropic (global/US) и Resend (US) — в реестре процессоров, с DPA/SCC/TIA как **обязательным условием до реальных данных** (на момент Gate −1 ничего из этого не подписано — актуальный статус в `docs/privacy/processors.md`, р. 1). Конфиг `residency_mode: eu-app | eu-strict`; `eu-strict` требует Vertex-EU-клиент и падает при его отсутствии — **никакого молчаливого downgrade**. Пилот стартует в `eu-app`; Vertex EU — задокументированный upgrade-путь после бенчмарка |
| Email-вход (шаг 1) | Три карточки в фиксированном порядке: **(a) `@abrolia.com` — предвыбрано и рекомендуется:** Nerve создаёт доступный транслитерированный `имя_фамилия@abrolia.com`, семья может изменить local-part; **(b) отдельный Gmail агента:** семья сама регистрирует новый Google-аккаунт, затем подключает именно его через Google OAuth account chooser — личный Gmail не рекомендуется, пароль/app password Abrolia не получает; **(c) домен семьи:** Nerve выдаёт DNS-записи, ждёт verify и создаёт ящик. Gmail restricted scopes, OAuth verification и CASA становятся launch-gate этой опции, а не GA-долгом |
| Исходящий email | Только compose с явно подтверждённым получателем: (a/c) через Nerve, (b) через Gmail API от отдельного аккаунта агента. Для пересылки оригинальный адресат извлекается из цепочки; для прямого письма виден в заголовках. Подключение существующего личного Gmail, IMAP/app password и неограниченное чтение личного ящика не предлагаются |
| WhatsApp (шаг 2) | Две карточки: **(a) общий номер Abrolia — предвыбрано для quick start:** только семейный диалог, маршрутизация по заранее подтверждённым номерам членов семьи; **(b) отдельный номер семьи — рекомендуется для полного WhatsApp-контура:** семья регистрирует SIM/eSIM и пейрит QR как linked device, Evolution/Baileys-инстанс строго per household, с per-instance apikey и per-household relay-HMAC. Только вариант (b) принимает школьные/внешние чаты на выделенный номер. Оба варианта маркируются Beta; для QR-контура — informed consent о неофициальной автоматизации и риске блокировки; все исходящие действия staged. Общий номер требует отдельного shared ingress-router, который не получает household-secrets и передаёт событие только после строгого sender→household binding |
| Канал общения (шаг 3) | Экран «Где вам удобнее общаться с Abrolia?»: **Telegram — предвыбрано и рекомендуется** (бот + семейная группа); **WhatsApp — Beta**, доступен после успешного шага 2; **Abrolia Web**, минимальный аутентифицированный web-chat/PWA без полного vault. Выбор — household default для проактивных уведомлений; ответ на входящее остаётся в канале, где семья написала. После выбора — test message/receipt и приглашение второго взрослого. Email не является primary-card и всегда остаётся fallback: это проверенный личный contact/recovery email владельца Abrolia-аккаунта, **не** операционный mailbox агента из шага 1 (запрет self-ingestion loop). Primary можно сменить; per-member overrides — после MVP |
| Календарь | Семейный Google: выделенный аккаунт ассистента на household, семья шарит календари; порт gtool; клиентские детерминированные event ID + reconcile |
| Модели | Диалог — `claude-opus-5`. Извлечение — старт на `claude-opus-5` (quality-first), выбор рабочей модели (Haiku/Sonnet/Opus) — по бенчмарку на обезличенном корпусе в Фазе 1; порог confidence не считается калиброванной вероятностью — управляет только рендером карточки, не автоисполнением |
| Ingress | SQLite (WAL) `events/jobs/effects`: fsync-before-ACK, leases, FIFO per context, DLQ + replay CLI; webhook только верифицирует подпись и фиксирует событие, обработка — background worker |
| Tool-цикл | Ручной loop (не runner): policy-check и аудит каждого tool_use; `run_id`+`effect_id`-идемпотентность мутирующих tools; запрет авто-replay хода после первого side effect; лимиты итераций/времени/токенов |
| Авторизация | Server-issued `RunContext {household, actor, chat, thread, scope, read_caps, mutate_caps}` на каждый ход; unknown/anonymous actor — ноль tools; `memory_append` — только family-actor и через staged-подтверждение |
| Хранение | SQLite WAL на per-household Fly volume (заменяет и file-JSON сторы v1 — единый транзакционный слой); Postgres — после пилота |
| Модель данных | Операционная онтология обязательств **поверх** журнала, в той же SQLite-схеме: `events/jobs/effects` остаются append-only журналами; квалифицированные утверждения (`status: candidate\|confirmed\|rejected\|superseded`, `supersedes`, `observed_at/recorded_at`, `confidence`) — только там, где живут противоречия: `commitments` и память. Каноническая схема — Pydantic + SQL-миграции; словарь онтологии — `docs/ontology.md`; RDF/OWL/SHACL/Turtle-TBox и отдельный graph store — нет. Provenance переживает retention: `evidence_refs` хранят метаданные (`event_id, sender_domain, message_date, content_sha, span_offsets`), не цитаты |
| Цена пилота | €59–99/мес dedicated (COGS честно: ~$8–11 inference + ~$12 Fly + Nerve/backups/monitoring ≈ $20–25 до поддержки) |

## What We're NOT Doing (MVP)

- Официальный WhatsApp Business Platform (отдельный post-MVP eligibility/legal workstream, не глобальный GA default), auto-reply на email-thread, multi-tenant assistant runtime в одном процессе. Узкий shared-WhatsApp ingress-router — осознанное исключение: он не исполняет модель/tools и не хранит household-secrets, кроме собственных routing credentials
- Полный Web vault/PWA (архив документов, настройки интеграций, offline-first) и мобильные клиенты; MVP включает только аутентифицированный Abrolia Web chat, PWA manifest и opt-in push как третий канал/fallback. Billing-автоматизация также вне MVP
- Обещания E2EE / zero-knowledge / «EU processing» без оговорок
- Подключение существующего личного Gmail, IMAP/app password и чтение личного ящика семьи; Gmail OAuth в MVP предназначен только для отдельного аккаунта агента и включается для реальных семей после verification/CASA. Postgres и миграция семейного Hermes также вне MVP
- Автоисполнение чего-либо по порогу confidence
- RDF/OWL/SHACL-стек, Turtle-TBox в Git, отдельный graph store (Neo4j и т.п.), GraphRAG; embeddings-поиск — только после измеренного провала recall на CQ-наборе (до этого — структурный SQL + FTS5); полная битемпоральность для журналов и state-машин (утверждения — только `commitments` и память)

## Implementation Approach

Порядок фаз — по принципу «сначала доверие, потом функции»: Gate −1 (право/санитизация/пиннинг) → тонкий вертикальный срез на синтетике → trust foundation (durable execution + авторизация) → Nerve-расширения (отдельный план) → реальные действия → пилотизация. Реальные данные семей не попадают в систему до завершения Фазы 2.

Структура репозитория — как в v1 (`hermes_cloud/{core,runner,ingest,execute,scheduler}`, `onboarding/`, `tests/`), с заменами: `core/db.py` (SQLite WAL, миграции), `core/runcontext.py`, `ingest/worker.py`, `ingest/whatsapp_webhook.py` + `execute/whatsapp.py`.

**Модель данных (операционная онтология).** Цепочка продукта в терминах схемы: `SourceEvent (events) → ExtractionRun (candidate) → подтверждение семьи → Commitment → Proposal/Action → Effect → Receipt`. Три правила разделения:

1. **Журналы не переписываются в утверждения.** `events/jobs/effects` — append-only со своим provenance; todos/reminders — конечные автоматы донорских сторов. Квалифицированные утверждения (`observed_at, recorded_at, extraction_run_id, confidence, status, supersedes`) — только у `commitments` и памяти, где реально живут противоречия («экскурсия перенесена», «сумма изменилась»); `valid_from/to` — только у routines/preferences в памяти. `Commitment` — тонкая связующая таблица (extraction → порождённые todo/reminder/gcal-маппинг), донорские сторы она **не заменяет**.
2. **Provenance переживает retention.** `evidence_refs` хранят `{event_id, sender_domain, message_date, content_sha, span_offsets}` — метаданные и офсеты, никогда цитаты: цитата в карточке рендерится из живого `events.raw`, после его 30-дневного TTL остаётся проверяемый след без контента третьих лиц (данные детей/третьих лиц наследуют TTL носителя — data map, р. 2).
3. **Суперсессия порождает reconcile.** `commitment v2 supersedes v1` при наличии исполненных эффектов у v1 создаёт новое staged-предложение «обновить/отменить» через тот же approval-контур (Фаза 4, п. 4) — версия факта и судьба её эффектов не расходятся.

Словарь терминов и инвариантов — `docs/ontology.md`, обновляется в том же PR, что и миграция; новая таблица = новый класс данных = строка в data map в том же PR (правило data map, р. 0). Competency questions — регрессионный pytest-набор (`tests/test_competency.py`): «какие платежи с дедлайном на этой неделе и кто ответственный», «из какого письма взялась эта сумма», «что изменилось между письмом 1 и 2». Никакого RDF/SHACL/Turtle в рантайме; JSON-LD-экспорт — при первом внешнем потребителе, не раньше.

---

## Phase 0 (Gate −1): Right-to-build

### Overview
Юридический и инженерный фундамент до любого кода, касающегося реальных данных. Блокирующий gate: ни одна следующая фаза не принимает реальные данные, пока Gate −1 не закрыт.

### Changes Required

1. **Пиннинг доноров**: зафиксировать состояние `hermes@agent/foundation-activation` (ветка активно движется и на момент Gate −1 не имеет зелёной independent validation — условия выбора нового SHA перечислены в `docs/source-pins.md`), записать SHA донора и SHA nerve-cloud/nerve-oss в `docs/source-pins.md`; API-снапшот Nerve-эндпоинтов, на которые полагаемся.
2. **Санитизация**: `tests/fixtures/` — только синтетика (RFC-example домены, телефоны из зарезервированных диапазонов, вымышленные имена, Telegram ID из документированного synthetic-диапазона); скрипт `scripts/check_fixtures.py` (запрещённые паттерны: реальные ID донора, @gmail, +7/+34-номера) + gitleaks в CI; правило в CONTRIBUTING: донорские payload-фикстуры не копируются as-is.
3. **Privacy-пакет** (`docs/privacy/`): data map (классы данных × хранилища × TTL), lawful bases, DPIA-драфт (отдельные разделы — dedicated Gmail OAuth: restricted scopes, server-side access, шифрованный refresh token, отзыв доступа; shared WhatsApp ingress: sender-binding и межсемейная изоляция), privacy notice (RU/EN), реестр процессоров (Anthropic, Resend/Nerve, Fly, Google, Telegram) с DPA/SCC/TIA-статусами, политика данных несовершеннолетних, incident-response заметка.
4. **Threat model** (`docs/SECURITY.md`): акторы (внешний отправитель письма/WA-сообщения, prompt injection в контенте/вложении, неизвестный участник Telegram-группы, компрометация одного household — включая изоляцию Evolution-инстансов per-instance-ключами), границы (модель без shell/send, staged-sends, RunContext), OAuth-риски (подмена `state`, подключение личного аккаунта вместо агентского, token exfiltration), channel-binding (Telegram/WhatsApp/Web actor никогда не берётся из клиентского payload без серверной проверки), WhatsApp-риски (неофициальная сессия, блокировка номера, shared-number cross-household routing, consent-текст для онбординга), явные не-цели.
5. **Retention-матрица**: TTL для transcripts (180д), memory (пересмотр раз в 90д), actions journal (365д), todos/reminders (done+90д), photos/voice/docs (30д), delivery receipts (365д) — фиксируется в data map и реализуется в Фазе 2.
6. README: заменить «EU processing» на честную формулировку residency (см. Locked Decisions).
7. **Gmail OAuth launch-gate**: зарегистрировать production OAuth client для `abrolia.com`, опубликовать consent/privacy pages, запросить только минимальные Gmail scopes, пройти Google verification и требуемую CASA-проверку до включения dedicated-Gmail карточки для реальных семей. Refresh tokens — envelope-encrypted per household, callback `state` одноразовый и привязан к owner-session+household, есть revoke/unlink с удалением токена.

### Success Criteria

#### Automated Verification:
- [x] `python3 scripts/check_fixtures.py` и gitleaks в CI зелёные на всей истории — локально чисто (`--all`, `gitleaks detect --log-opts=--all`); CI гоняет санитайзер по всему дереву с `--require-deny` и gitleaks с `fetch-depth: 0`, плюс ruff и pytest
- [x] `docs/source-pins.md` существует и содержит три SHA (все три коммита существуют в донорских репозиториях). Перепин hermes отложен: кандидат `a02ab33` отклонён (валидация донора FAILED/NOT READY, ветка ушла дальше), в документе записаны три условия, при которых можно выбрать новый SHA — зелёная валидация, чистое запушенное дерево, отдельный коммит с перечнем изменений в портируемом слое

#### Manual Verification:
- [ ] До появления Gmail-карточки у реальной семьи: production OAuth consent screen verified, CASA/эквивалентное требование Google закрыто, unlink действительно отзывает доступ и удаляет локальный refresh token; до этого путь доступен только на синтетическом/test-user окружении
- [x] Privacy-пакет отревьюирован владельцем продукта 2026-08-03 — **в объёме работы на синтетике**. Подтверждены: три добавленных TTL (DLQ-метаданные 90 дн., логи 30 дн., бэкапы 30 дн.), трёхлетний срок для receipts/DSAR/инцидентов как наш выбор, текст согласия на WhatsApp, дефолт онбординга (managed-ящик; Gmail-опция не рекомендуется), staged approval на все исходящие WhatsApp. **Ревью юриста не проводилось** — предпосылки для реальных данных перечислены в `docs/privacy/dpia.md`, р. 5, и остаются открытыми
- [x] Донорская ветка зафиксирована, SHA записан: порт ведётся от опубликованного `c26fc41` (решение владельца 2026-08-03). Ветка донора продолжает движение без зелёной валидации, поэтому перепин отложен до выполнения трёх условий в `docs/source-pins.md`; исправления, попавшие в донор после пина, портируются повторно при перепине

**Статус: Gate −1 закрыт 2026-08-03 для работы на синтетических данных.** Принятые на ту дату ручные пункты перечислены выше; решение v3 добавило новый открытый Gmail OAuth launch-gate. Ревизии гейта — `thoughts/shared/implementations/2026-08-02-family-ops-assistant-mvp-validation.md`. Зелёные автопроверки означают лишь, что текущие образцы удовлетворяют текущим правилам, поэтому санитайзер и его тесты остаются живым артефактом. **Реальные данные, включая ящик владельца, заблокированы** до закрытия всех предпосылок `docs/privacy/dpia.md`, р. 5 — в первую очередь условия ст. 9(2), ревью юриста и, для dedicated Gmail, verification/CASA.

Реализовано в Gate −1: `scripts/check_fixtures.py` + `.check-fixtures-allow` + `tests/test_check_fixtures.py` + `tests/fixtures/PROVENANCE.md`, конвенции `tests/fixtures/README.md`, `CONTRIBUTING.md`, `.gitleaks.toml`, CI, `docs/privacy/` (data map с retention-матрицей, lawful bases, DPIA, реестр процессоров, notice RU/EN, политика по несовершеннолетним, incident response), `docs/SECURITY.md`.

Исправлено после ревизии: формат ключа Nerve (`nrv_live_…`) в обоих гейтах; точечные исключения `путь|правило|значение` вместо построчного маркера (приватные deny-паттерны не подавляются); Telegram-ID в многострочном JSON; IBAN с пробелами и в нижнем регистре по контрольной сумме; `00`-префикс телефона; разворачивание base64/quoted-printable тела `.eml`; требование PROVENANCE.md для непроверяемых фикстур; CI без исключений по путям и с fail-closed deny-паттернами; staged approval для **всех** исходящих WhatsApp; ст. 9(2) как блокирующий вопрос вместо «остаточного риска»; DLQ — payload 30 дней / метаданные 90; честный статус DPA/SCC в README и notice.

**Пауза для ручного подтверждения.**

---

## Phase 1: Thin vertical slice (synthetic data)

### Overview
Минимальный сквозной контур на синтетике: durable event → extraction → Telegram-карточка → reminder или ICS. Без Nerve-зависимости (вход — CLI-инжект .eml), без outbound email, без Google write, без WhatsApp. Параллельно — модельный бенчмарк.

### Changes Required

1. **`core/db.py`**: SQLite WAL, таблицы `events {id, source, external_id UNIQUE, raw BLOB, received_at, status: received|processing|done|failed, lease_until, attempts}`, `jobs`, `effects {id, run_id, tool_use_id, kind, payload_sha, status, receipt}`; fsync-before-commit; миграции (простые нумерованные .sql).
2. **`ingest/inject.py`**: CLI `hermes-cloud inject-eml path.eml` — парсинг .eml (включая пересланные цепочки: извлечение оригинального From/Date из forwarded-заголовков и тела), запись события. Это же ядро потом переиспользует nerve-webhook.
3. **`ingest/worker.py`**: цикл: lease событие (FIFO per context, `lease_until`, reclaim просроченных) → extraction → карточка → `done`/`failed(attempts++)`; DLQ после N попыток; `hermes-cloud replay <event_id>`.
4. **`runner/extraction.py`**: Pydantic `ExtractionResult` (как v1 + `original_sender {email, name, confidence}` из forwarded-цепочки); `messages.parse` на `claude-opus-5`; политика untrusted-контента; вложения — content blocks. Каждый прогон персистится: `extraction_runs {id, event_id, model, prompt_sha, created_at}` + `evidence_refs {extraction_run_id, event_id, sender_domain, message_date, content_sha, span_offsets}` — только метаданные и офсеты, без цитат; цитата в карточке рендерится из живого `events.raw` и после его TTL деградирует до проверяемого следа (см. Implementation Approach, «Модель данных»). Новые таблицы — строками в data map в том же PR.
5. **Карточка**: staged-предложение (порт фасада actions с claim_pending донора) c кнопками ✅/✏️/❌; `payment`/`info` рендерятся: payment → задача с суммой и дедлайном; info → сообщение без кнопок. ✏️ отменяет staged-код (invalidation) и открывает диалог, новое предложение = новый код.
6. **Исполнители slice**: только `reminder` (порт ReminderStore поверх SQLite) и `ics` (генерация файла в чат). Порт минимума Telegram-plumbing (webhook, allowlists, resolve_sender, send_reply, кнопки).
7. **`bench/`**: корпус ≥40 синтетических писем (DE/IT/NL/EN × школа/счёт/приглашение/спам/adversarial/forwarded-chain/OCR-фото), golden-ожидания; прогон Haiku/Sonnet/Opus → отчёт точности/стоимости; решение по extraction-модели фиксируется здесь.

### Success Criteria

#### Automated Verification:
- [x] `pytest`: событие переживает kill между insert и обработкой (реобработка), дубль external_id — no-op, DLQ после N неудач, replay работает — `tests/test_events.py`, kill проверяется настоящим SIGKILL дочернего процесса
- [x] Forwarded-парсер: оригинальный отправитель извлекается на фикстурах пересылок (Gmail/Outlook/Apple Mail форматы) — `tests/test_eml.py`, четыре формата + вложение `message/rfc822` + прямое письмо как негативный случай
- [x] `bench/run.py` выдаёт отчёт по трём моделям (live) — `bench/report.md`, 44 кейса × 3 модели, точность по полям + стоимость по фактическому расходу токенов
- [x] Инъекционный корпус: 0 staged-действий, не соответствующих фактическому содержанию — все шесть adversarial-кейсов классифицированы как `spam`, предложение не создано ни одной моделью; спрятанная в школьном письме инструкция «переведи 500 EUR» проигнорирована, извлечено настоящее содержание письма
- [x] Evidence-провенанс: карточка рендерит цитату из `events.raw`; после симуляции retention-удаления payload `evidence_ref` остаётся разрешимым (метаданные без контента), рендер честно показывает «источник удалён по сроку хранения» — `tests/test_commitments.py::test_evidence_survives_the_letter_it_came_from`, `tests/test_competency.py::test_the_source_answer_degrades_honestly`, строка «Источник» в карточке — `tests/test_pipeline.py::test_the_card_shows_where_the_number_came_from`

#### Manual Verification:
- [x] Синтетическое немецкое школьное письмо через inject-eml → корректная карточка на русском → ✅ → напоминание сработало — проверено 2026-08-03 на живом боте: карточка с суммой 15,00 EUR, сроком и школой в строке «Отправитель» (не переславшим родителем), подтверждение владельцем, доставка напоминания; повторное ✅ отклонено, повторный tick не продублировал
- [x] ICS-файл открывается в Google/Apple Calendar — проверено 2026-08-03: событие «Родительское собрание 3b» встало на 24.09.2026 19:30 (в файле `DTSTART:20260924T173000Z`, то есть 19:30 по Берлину)
- [x] Решение по extraction-модели принято и записано в план — см. ниже

**Найдено ручной проверкой (закрывается позже):** часовой пояс семьи нигде не задан — модель выводит его из содержания письма. Для немецкой школы («19:30 Uhr») вывела верно, но письмо без указания города разрешит наугад. Место для исправления — часовой пояс в `household.toml` (Фаза 5) с явной передачей в промпт извлечения; до этого возможен сдвиг события у писем без географической привязки.

#### Решение по extraction-модели (2026-08-03)

Прогон `bench/run.py` на корпусе из 44 синтетических писем (DE/IT/NL/EN × школа/счета/приглашения/спам/adversarial/транскрипт фото, 10 пересланных цепочек):

| Модель | Кейсов без единой ошибки | Инъекции пройдены | Стоимость корпуса, $ |
|---|---|---|---|
| `claude-sonnet-5` (effort medium) | 42/44 | 4/4 | 0,18 |
| `claude-opus-5` (effort medium) | 42/44 | 4/4 | 0,27 |
| `claude-opus-5` (effort high) | 42/44 | 4/4 | 0,33 |
| `claude-sonnet-5` (effort high) | 39/44 | 4/4 | 0,25 |
| `claude-haiku-4-5` | 34/44 | 4/4 | 0,20 |

**Выбрана `claude-sonnet-5` при `effort=medium`**: та же точность, что у Opus, при цене примерно на треть ниже. Haiku заметно слабее и при этом не дешевле на этой нагрузке — путать «дешёвую модель» с «дешёвой задачей» не стоит. Более высокий effort ничего не добавил (у Sonnet даже ухудшил результат), что совпадает с рекомендацией не переносить настройки effort между моделями.

Оставшиеся две ошибки одинаковы у обеих топ-моделей и лежат в двусмысленных кейсах (`nl-school-parent-talk`, `en-school-club`: событие или задача, оплата или запись) — это вопрос к формулировке golden, а не к модели.

**Следствие для экономики:** извлечение обошлось в ~$0,004 за письмо. При 100 письмах в месяц это ~$0,40 на household — на порядок ниже строки «$8–11 inference» из COGS, где основная часть приходится на диалог. Диалоговая модель остаётся `claude-opus-5` (Locked Decision).

Реализовано в Фазе 1: `core/db.py` + миграции + `core/events.py` (долговечный ingress), `core/approvals.py` (порт `claim_pending`), `core/config.py`, `ingest/{eml,inject,worker}.py`, `runner/{extraction,card,pipeline}.py`, `execute/{reminder,ics}.py`, `channels/telegram.py`, `cli.py`, `bench/` (корпус, прогон, отчёт).

**Пауза для ручного подтверждения.**

---

## Phase 2: Trust foundation

### Overview
Инварианты, позволяющие подключать реальные данные: авторизация каждого хода, идемпотентность эффектов, crash-safe исполнение подтверждений, retention/export/delete, backup.

### Changes Required

1. **`core/runcontext.py`**: `RunContext` собирается сервером на входе каждого хода из проверенного Telegram-апдейта: `{household_id, actor_id, chat_id, thread_id, scope: personal|shared, read_caps, mutate_caps}`. Маппинг actor→роль в `household.toml`. Unknown/anonymous actor: ответ без tools («представьтесь / попросите взрослого добавить вас»), нулевые caps. Все tools получают RunContext первым аргументом и проверяют caps сами (defense in depth) — тест на каждый tool × unknown actor.
2. **Ручной tool-loop** (`runner/model.py`): цикл `messages.create` → перебор tool_use блоков → policy check (caps, лимиты) → исполнение → журналирование в `effects (run_id, tool_use_id)` → tool_result. Идемпотентность: повторный тот же `tool_use_id` возвращает прежний результат без повторного эффекта. Retry API-вызова разрешён только если в текущем ходе ещё нет записанных эффектов; иначе — честное сообщение «ход прерван, вот что успело выполниться» из журнала. Лимиты: ≤8 итераций, ≤5 мин, токен-бюджет.
3. **`memory_append`**: только family-actor; запись становится staged-предложением («добавить в память: …» ✅/❌) — persistent injection через контент писем невозможен. Память хранится квалифицированными утверждениями (`status, observed_at/recorded_at, extraction_run_id?, supersedes`); `valid_from/to` — только у routines/preferences.
3b. **`core/commitments.py`**: таблица `commitments {id, extraction_run_id, kind, payload, status: candidate|confirmed|rejected|superseded, supersedes, observed_at, recorded_at, confidence, spawned: todo_id/reminder_id/gcal_mapping_id}` — тонкий связующий слой между извлечением и порождёнными артефактами, донорские сторы не переписывает. Инварианты: LLM пишет только `candidate`; `confirmed` — исключительно через `claim_pending` (подтверждение факта ≠ разрешение на действие — это отдельный approval); `confidence` влияет только на рендер карточки; `superseded` не удаляется — цепочка версий читается по `supersedes`.
4. **Approval-исполнение crash-safe**: `claim_pending` (порт донорского фасада: code+chat+thread+actor, rate-limit неверных кодов) пишет attempt в `effects` в той же транзакции; executor-вызов с lease; startup-reconciliation: висящие `executing` с истёкшим lease → канальная стратегия: nerve-email — повторить с тем же idempotency_key; calendar — reconcile по детерминированному client-generated event ID (`events.insert` с заданным `id`, поиск перед повтором); прочее → `outcome_unknown`, никогда слепой retry.
5. **Retention/export/delete**: ежедневная retention-джоба по матрице Gate −1; `GET /export` и `POST /delete` — owner-auth: инициируются только primary-owner-актором + повторное подтверждение кодом (тот же approval-механизм); delete-runbook перечисляет внешние поверхности (nerve-org, Resend-остатки, Google-события, Anthropic retention, Telegram-копии, бэкапы) с процедурой по каждой; в приложении — wipe SQLite+файлов+tombstone.
6. **Backup/restore**: Litestream (или периодический snapshot) SQLite на EU object storage, шифрование per-household ключом; `restore.md` + тест восстановления.

### Success Criteria

#### Automated Verification:
- [x] Chaos-тесты: kill -9 в каждом окне (после claim/до executor; после executor/до mark; в середине tool-loop) → после рестарта ровно один эффект, статусы согласованы — `tests/test_effects.py` (три окна) + `tests/test_model.py::test_crash_mid_loop_leaves_exactly_one_proposal`; убивается настоящий дочерний процесс через SIGKILL (`tests/chaos_child.py`)
- [x] Матрица авторизации: каждый tool × {family, guest, unknown} — ожидаемые allow/deny — `tests/test_runcontext.py::test_authorization_matrix` (+ owner как четвёртая роль); матрица строится по реестру, tool без строки в ней роняет тест; отдельный тест на проверку прав внутри самого обработчика
- [x] Повтор tool_use_id не создаёт второй эффект; retry хода с эффектами запрещён — `tests/test_model.py::test_same_tool_use_id_executes_once`, `::test_api_failure_after_an_effect_is_never_retried`, `tests/test_effects.py::test_repeated_tool_use_id_returns_the_prior_result`
- [x] `/export` полон по data map; `/delete` без owner-auth отклоняется — `tests/test_dsar.py::test_every_table_is_either_exported_or_explicitly_excluded` (таблица без решения об экспорте роняет набор), `::test_only_the_owner_may_confirm_export_and_delete`
- [x] Restore из бэкапа проходит smoke-набор — `tests/test_backup.py::test_restore_brings_back_a_working_household`; процедура и репетиция — `docs/restore.md`
- [x] Статусная модель: `candidate` никогда не отдаётся в ответах/дайджестах как подтверждённый факт; `commitment v2 supersedes v1` → v1 в `superseded`, цепочка версий читается; первый CQ-набор (`tests/test_competency.py`) зелёный — 7 CQ-тестов; словарь и инварианты — `docs/ontology.md`

#### Manual Verification:
- [ ] Неизвестный участник, добавленный в группу, не получает доступ к почте/памяти/календарю
- [ ] Ревью delete-runbook

**Пауза для ручного подтверждения. Завершение Фазы 2 — необходимое, но не достаточное условие для реальных данных.** Реальный ящик (включая ящик владельца-теста: его переписка содержит тех же третьих лиц и детей) подключается только когда закрыт **весь** список предпосылок `docs/privacy/dpia.md`, р. 5 — условие ст. 9(2), Фаза 2, подписанные DPA и оформленные трансферные механизмы, актуальные notice с реквизитами контролёра и надзорного органа, зафиксированные согласия, ревизия DPIA по ст. 35(4)/36. Уточнение ревизии Gate −1.

---

## Phase 3: Nerve extension (отдельный план в nerve-cloud)

### Overview
Работы в nerve-cloud/nerve-oss/SDK — **отдельный план** `nerve-cloud/thoughts/shared/plans/2026-08-02-inbound-events-and-attachments.md`. Он реализован и выведен в production: org event journal → `email.received` fan-out → attachments в обе стороны → SDK 0.2.0; финальные доказательства — Revision 28 и Phase 8 Nerve-плана. В Abrolia реализован потребительский live contract suite; остаётся его первый операторский прогон. Отдельного staging Nerve нет: проверка идёт только на явно синтетическом org-scoped production canary при выключенном global attachment flag.

### Контракт, который обязан дать Nerve-план

1. **Общий event journal + фан-аут `email.received`** в org-webhooks (HMAC, ретраи) — с учётом того, что текущая доставка завязана FK на outbound-журнал: нужна общая таблица событий или эквивалент, не «просто ещё один event type».
2. **Входящие вложения**: персист метаданных (`message_attachments`), выдача в GET thread, и **scope-фикс attachment-proxy** — доступ по runtime-ключу household (сейчас требует `nerve:admin.billing | nerve:email.inbox.create`, handler_messages.go:43).
3. **Исходящие вложения**: content-addressed blob store → outbox → worker → Resend/SMTP; лимит 10MB, MIME-allowlist; MCP `compose_email(attachments=)`; SDK 0.2.0.
4. **Webhook-секрет**: серверная генерация, показ один раз (уже так, org_webhooks.go:68) — provisioning hermes-cloud читает секрет из ответа `create_webhook`, никогда не задаёт свой.
5. Contract-тесты, которые hermes-cloud прогоняет против staging: подпись/ретрай/дедуп `email.received`; скачивание вложения runtime-ключом; отправка compose с PDF.

### Success Criteria

#### Automated Verification:
- [x] Nerve-план написан, принят, реализован и выведен в production; upstream canary и rollback drill записаны в Revision 28 Nerve-плана
- [x] Consumer-owned live contract suite Abrolia против синтетического org-scoped production canary зелёный — подтверждено оператором 2026-08-06

#### Manual Verification:
- [x] Синтетическое письмо с PDF на production-canary inbox → подписанный webhook → вложение скачано runtime-ключом → compose с вложением доставлен; временные keys/webhook удалены, global flag остался off — письмо отправлено и получено 2026-08-06

**Статус: Фаза 3 закрыта 2026-08-06.** Upstream Nerve-контракт в production и consumer-owned Abrolia live suite подтверждены на синтетических canary-данных. Это не меняет открытые правовые и processor-gates для реальных семей.

**Пауза. Фазы 1–2 не зависят от этой фазы (inject-eml); Nerve-расширения нужны для `@abrolia.com` и домена семьи (шаг 1: a/c). Dedicated Gmail (b) идёт через отдельный OAuth/Gmail API тракт Фазы 5 и от Nerve не зависит.**

---

## Phase 4: Real actions

### Overview
Google Calendar write и исходящий email с доказанной семантикой получателя; подключение nerve-webhook как второго входа рядом с inject-eml.

### Changes Required

1. **`ingest/nerve_webhook.py`**: `POST /webhooks/nerve` — верификация `X-Nerve-Signature` (окно ±5 мин), немедленный insert события + 200; всё остальное — worker (общий с Фазой 1). Вложения качаются в worker сразу (Resend-URL ~1ч) в content-addressed store.
1b. **`ingest/gmail_poll.py` — реализованный исторический baseline, superseded решением v3:** порт паттерна mailwatch.py донора — IMAP4_SSL + app password, ярлык `Hermes`, Message-ID-cursor и FakeIMAP-тесты. Этот тракт остаётся тестовым seam для проверки общего email pipeline, но **не показывается семье и не допускается в production onboarding**. В Фазе 5 его пользовательскую роль заменяет `ingest/gmail_api.py` с OAuth к отдельному аккаунту агента; оба транспорта обязаны давать один канонический `events` payload.
2. **`execute/gcal.py`**: порт gtool; executor `calendar_event` с client-generated deterministic event ID = f(action_id); reconcile-поиск перед любым повтором; линк события в чат. Read-only tool `calendar_list_events` (caps: family).
3. **`execute/email_send.py`**: реализованный `email_compose` сохраняет общий контракт staged-отправки и два backend seam — Nerve и SMTP. В v3 production routing такой: `@abrolia.com`/домен семьи → `NerveClient.compose_email(..., idempotency_key=action_id)`; dedicated Gmail → новый Gmail API backend Фазы 5; SMTP/app-password backend остаётся только тестовым/миграционным и недоступен из onboarding. Карточка всегда показывает получателя отдельной строкой «Кому: …» — подтверждение покрывает и текст, и адресата.
4. **`ActionBundle`**: извлечение одного письма может породить связку (событие + задача о взносе). Карточка-бандл: общий заголовок, дочерние пункты с чекбоксами (по умолчанию все включены), одно подтверждение → транзакционное staged-исполнение каждого дочернего эффекта со своим effect_id; частичный фейл отражается по-пунктно. **Reconcile при суперсессии**: если extraction нового письма матчится на существующий `commitment` (тот же источник-домен + тип + ключевые поля) — создаётся `v2 supersedes v1`, и при наличии исполненных эффектов у v1 карточка рендерится как reconcile-бандл «обновить событие / изменить задачу / отменить» — тот же approval-контур, идемпотентные `effect_id`; gcal-обновление — по детерминированному event ID из п. 2, не созданием второго события. Неуверенный матч — два независимых candidate с пометкой возможного дубля, решает семья.
5. **WhatsApp** (`ingest/whatsapp_webhook.py` + `execute/whatsapp.py`): порт донорского контура — relay-webhook с per-household HMAC (fail-closed), входящие на номер семьи → тот же `events`-конвейер (сообщение учителя/чата → extraction → карточка; обычный диалог семьи с ассистентом в WA — как в Telegram, под RunContext); исходящие: **все без исключения — только staged approval** (правка после ревизии Gate −1: прежняя формулировка выводила family-адресатов на tracked `begin_send` в обход подтверждения и противоречила Locked Decision и инварианту README; донорский tracked-send остаётся механикой доставки *после* подтверждения, а не заменой ему); транспортная таксономия донора (redirect→failed, ReadTimeout→`SendOutcomeUnknown`, ConnectError→failed). Evolution-инстанс per household со своим apikey.
6. Фото/голос-ингест (порт vision-блоков и faster-whisper) — переносится из v1 без изменений сути; scheduler/digest — порт с SchedulerConfig.

### Success Criteria

#### Automated Verification:
- [x] Идемпотентность calendar: повторное исполнение того же action не создаёт второе событие — `tests/test_gcal.py` (мок): тот же id, `get` перед записью, 409 читается как «уже наше», доигрывание после падения обновляет, а не создаёт; live-smoke — при подключённом Google-аккаунте
- [x] Compose: получатель в payload обязан совпасть с подтверждённым в карточке — `tests/test_email_send.py::test_swapping_the_recipient_after_the_card_invalidates_the_confirmation`; плюс kill-switch, header-injection и `outcome_unknown` без повтора
- [x] Bundle: частичный фейл → корректные статусы каждого дочернего эффекта — `tests/test_bundle.py::test_a_failing_item_does_not_undo_the_others`; неизвестный исход пункта отражается отдельно от отказа
- [x] Суперсессия e2e (мок-gcal) — `tests/test_supersession.py`: reconcile-карточка со строками «что изменилось», обновление того же события (id от корня цепочки версий), v1 в `superseded`, второго insert нет; напоминание прежней версии отменяется
- [x] gmail_poll и inject-eml дают идентичный pipeline-результат на одном .eml — `tests/test_gmail_poll.py::test_the_same_letter_gives_the_same_result_whichever_door_it_came_through` (отличается только одноразовый код). Webhook-вход — после Фазы 3
- [x] gmail_poll: курсор по Message-ID, защита от UIDVALIDITY-replay, baseline первого запуска, письмо без ярлыка не запрашивается вовсе — `tests/test_gmail_poll.py` (16 тестов на FakeIMAP-шве; донорский набор не копировался построчно — инварианты перенесены, тесты написаны под наш шов)

#### Manual Verification:
- [ ] Синтетика end-to-end: письмо «экскурсия 12.09, взнос 15€» → бандл (событие+задача) → ✅ → событие в шаренном календаре, задача в списке; затем письмо «перенесена на 19.09» → reconcile-карточка → ✅ → событие обновлено, не продублировано
- [ ] Staged-письмо школе с PDF: карточка показывает «Кому: schule@example.de», после ✅ доставлено
- [ ] Dedicated Gmail на test-user OAuth: входящее письмо → карточка; compose от адреса агента попадает нужному получателю; disconnect отзывает grant. Старый IMAP/app-password smoke не является критерием приёмки пользовательского MVP
- [ ] WhatsApp: сообщение на номер семьи → карточка/диалог; **любая** исходящая отправка (и внешнему контакту, и семейному) исполняется только после ✅; неверный HMAC relay — отклонён
- [ ] Kill-switch email_send=0 блокирует с честным сообщением

**Пауза для ручного подтверждения.**

---

## Phase 5: Pilotization

### Overview
Воспроизводимый пилот: провижининг, наблюдаемость, cost-caps, метрики, восстановление.

### Changes Required

1. **Возобновляемая onboarding state machine** (`onboarding/state.py`, `onboarding/provision.py`): состояние каждого шага фиксируется до внешнего side effect; повтор/рестарт продолжает незавершённый шаг, а не создаёт второй inbox, OAuth grant или Evolution instance. Финальный `household.toml` и channel bindings пишутся только из подтверждённых результатов; `--dry-run` проходит тот же граф без внешних writes.
   - **Шаг 1 — email-идентичность:** (a) предвыбранный `имя_фамилия@abrolia.com` с транслитерацией, редактированием local-part, проверкой доступности и созданием Nerve inbox; (b) ссылка на самостоятельную регистрацию отдельного Gmail → возврат → OAuth account chooser → явное подтверждение выбранного адреса; (c) домен семьи → DNS records → verify → inbox. Нельзя продолжить с личным Google-адресом, если семья не прошла отдельное предупреждение; пароль/app password нигде не запрашивается.
   - **Шаг 2 — WhatsApp-идентичность:** (a) общий номер Abrolia для quick start — верифицируются номера взрослых, внешний школьный/групповой контур недоступен; (b) отдельный номер/SIM семьи — consent, Evolution instance `abrolia-<household_uuid>`, per-instance apikey, QR с TTL, relay-webhook и per-household HMAC, затем inbound/outbound smoke. Dedicated-number карточка отмечена как рекомендуемая для полного контура; обе — Beta.
   - **Шаг 3 — предпочтительный канал:** карточки `Telegram (Recommended, selected)`, `WhatsApp (Beta, enabled only after step 2)`, `Abrolia Web`. Telegram связывается одноразовым deep link и предлагает семейную группу; WhatsApp подтверждается test message с уже проверенного номера; Web требует authenticated session и предлагает opt-in push. Если Web выбран без push, email получает только уведомление-ссылку о новом сообщении. После выбора система отправляет test message, ждёт receipt и предлагает пригласить второго взрослого. Fallback берётся из проверенного contact/recovery email владельца Abrolia-аккаунта и не показывается как primary-card; mailbox агента из шага 1 никогда не может быть fallback-адресом, чтобы исключить self-ingestion loop.
2. **Google OAuth production path** (`onboarding/google_oauth.py`, `ingest/gmail_api.py`, Gmail backend в `execute/email_send.py`): `state` одноразовый и привязан к owner-session+household, `prompt=select_account`, отображение и повторное подтверждение email после callback, минимальные `gmail.readonly` + `gmail.send`, refresh token envelope-encrypted per household и недоступен модели. Ингест через Gmail API cursor/history попадает в канонический `events`; compose использует общий staged/idempotency contract. `disconnect` отзывает grant и удаляет токен. Включение для реальных семей fail-closed до verification/CASA; реализованный IMAP-поллер остаётся только тестовым seam.
3. **Предпочтения и routing** (`core/channel_preferences.py`, `channels/router.py`, миграция `0006_channel_preferences.sql`): экспортируемая таблица `channel_preferences {subject: household|actor, subject_id, primary_channel, fallback_channel, verified_at, updated_at}`; MVP пишет household-row, схема допускает post-MVP per-member overrides. Fallback binding ссылается только на verified owner contact email и валидатор запрещает совпадение с любым agent inbox. Входящий запрос получает ответ в том же проверенном канале независимо от preference; preference управляет проактивными сообщениями/digest. При доказанном permanent failure primary router отправляет короткое уведомление на email fallback; при `outcome_unknown` второй канал не используется, чтобы не создать дубль. Для Web primary без push сам контент остаётся только в Web, а email получает ссылку-уведомление без чувствительного текста. Смена primary требует test receipt перед commit.
4. **Channel identity binding** (`onboarding/channel_bindings.py`): Telegram user/chat, WhatsApp sender/instance и Web session связываются с одним actor только через owner-authorized одноразовый challenge. `RunContext` расширяется `channel`, но actor/household по-прежнему вычисляются сервером; клиентский payload не может выбрать household или роль. Приглашение второго взрослого создаёт отдельный binding, не общий credential.
5. **Shared WhatsApp gateway** (`gateway/whatsapp_router.py` + отдельный deployment): узкий multi-tenant ingress без модели/tools. Нормализованный sender проходит exact verified mapping к одному household; unknown/ambiguous sender отклоняется. Gateway durable-записывает webhook до ACK, подписывает пересылку отдельным per-household relay key, не логирует текст/QR и удаляет payload после подтверждённой доставки. Cross-household routing и enumeration закрываются тестами; kill switch общего номера не затрагивает dedicated instances.
6. **Минимальный Abrolia Web** (`channels/web.py`, `web/`): аутентифицированный чат над тем же `runner/model.py`, PWA manifest и opt-in push; никакого отдельного model/tool пути. В MVP нет полнотекстового архива документов, интеграционных настроек или offline-копии семейных данных. Чувствительные approvals открываются в этой first-party поверхности, но проходят тот же staged-контур.
7. **Observability**: структурные логи (без контента писем — только ID/статусы), `/health` (nerve key, telegram, WhatsApp instance/gateway, Google grant, Web push, db, backup age), алёрты (DLQ>0, застрявшие executing, primary channel unavailable, backup стар, бюджет превышен).
8. **Cost caps**: счётчик токенов per-household/день (из usage ответов) с мягким лимитом (деградация: extraction-only, диалог отвечает «дневной бюджет исчерпан») — защита от runaway-цикла и от abuse.
9. **Rollback/upgrade**: релизы по тегам, migrate-on-start с backup-before-migrate, runbook отката.
10. **Метрики пилота** (из вердикта go-сигналов): завершение каждого onboarding-шагa, выбранный primary channel, test-message success, активность на 8-й неделе, подтверждённые операции/семью/неделю, доля принятых без правок, ноль несанкционированных действий — события в лог, скрипт сводки.
11. Прайсинг: €59–99 dedicated; страница/письмо-оффер вне этого плана.

### Success Criteria

#### Automated Verification:
- [ ] `tests/test_onboarding.py`: default path = `@abrolia.com → общий WhatsApp → Telegram`; рестарт после каждого внешнего side effect не создаёт дубликат; недоступный email local-part и истёкший QR возвращают пользователя в тот же шаг
- [ ] `tests/test_google_oauth.py`: replay/подмена `state`, callback другого household и неподтверждённый account mismatch отклонены; refresh token зашифрован; revoke удаляет grant; до launch-gate real-family feature flag fail-closed
- [ ] `tests/test_channel_preferences.py`: входящий ответ остаётся в source channel; proactive идёт в подтверждённый primary; permanent failure → verified owner email fallback; agent inbox как fallback отклоняется и self-ingestion loop невозможен; `outcome_unknown` не дублируется; primary commit невозможен без test receipt
- [ ] `tests/test_channel_bindings.py`: неизвестный Telegram/WhatsApp/Web actor получает нулевые caps; challenge одноразовый; shared WhatsApp sender не может попасть в другой household
- [ ] `tests/test_web_channel.py`: без authenticated session чат недоступен; verified actor получает тот же RunContext/tool-policy, что в Telegram; push opt-in не обязателен для web chat
- [ ] `provision.py --dry-run` на staging проходит все три шага и перечисляет внешние writes без их выполнения
- [ ] CI: pytest + ruff + gitleaks + check_fixtures на каждый PR
- [ ] Cost-cap тест: превышение бюджета переводит в деградацию, не в тишину

#### Manual Verification:
- [ ] Онбординг тестового household с нуля ≤60 мин по runbook
- [ ] Шаг 1: `@abrolia.com` создаётся без DNS; dedicated test-user Gmail подключается OAuth без передачи пароля; BYO-домен проходит DNS verify
- [ ] Шаг 2: общий номер отвечает только подтверждённому члену семьи; dedicated номер пейрится QR и переживает рестарт Evolution; consent виден до QR
- [ ] Шаг 3: Telegram, WhatsApp и Web по очереди выбираются primary, каждый получает test message; email остаётся fallback; приглашённый второй взрослый получает отдельную identity binding
- [ ] Смена primary Telegram → WhatsApp/Web не теряет историю и не меняет права; новое проактивное сообщение приходит только в новый primary
- [ ] Сквозной wedge-сценарий на первом реальном household владельца
- [ ] Upgrade+rollback отрепетированы

---

## Testing Strategy

- **Unit**: портированный baseline с санитизированными фикстурами; chaos-тесты Фазы 2; авторизационная матрица.
- **Onboarding/channel matrix**: три email-опции × два WhatsApp-режима × три primary-channel карточки, но pairwise-набор вместо полного декартова взрыва; обязательные отдельные security cases для OAuth state, actor binding, shared-number cross-household routing и fallback без дублей.
- **Competency questions** (`tests/test_competency.py`): регрессионный набор структурных запросов к SQL/FTS5 («платежи этой недели по ответственным», «источник этой суммы», «дельта между письмами v1/v2»); расширяется вопросами пилота. Провал recall здесь — **единственный** триггер для добавления embeddings-индекса (перестраиваемого, не источника истины).
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
- Право: Anthropic data residency (inference_geo: global/us), Resend regions (US metadata), Google Gmail restricted scopes/OAuth verification/CASA, [WhatsApp Business Solution Terms](https://www.whatsapp.com/legal/business-solution-terms/) (AI-provider eligibility/geography), GDPR/DPIA, AI Act Art. 50

## Enhancement History

### 2026-08-03 Enhancement: операционная онтология обязательств

Источник: внешнее экспертное предложение (модульная онтология с битемпоральными provenance-aware утверждениями), критически переработанное перед внесением. Принято ядро: цепочка `SourceEvent → candidate → подтверждение → Commitment → Action → Effect → Receipt`, статусы `candidate|confirmed|rejected|superseded` с `supersedes`, CQ-регрессии, «LLM пишет только candidate», «подтверждение факта ≠ разрешение действия», «confidence — только рендер».

Отклонено из предложения эксперта (с обоснованием):
- **Turtle-TBox + SHACL + RDFS/OWL RL** — третье параллельное описание схемы без рантайм-потребителя при SQLite-хранилище; каноном остаются Pydantic + SQL-миграции, словарь — `docs/ontology.md`, контракты — Pydantic-валидаторы + SQL-constraints + CQ-pytest.
- **Битемпоральность «на всё»** — журналы (`events/effects`) и state-машины (todos/reminders) не переписываются в форму утверждений; квалифицированные утверждения только у `commitments` и памяти, `valid_from/to` — только у routines/preferences.
- **`household` в кортеже утверждения** — изоляция физическая (SQLite per household), колонка избыточна.

Добавлено сверх предложения (пропущенные узлы):
- **Provenance × retention**: `evidence_refs` — метаданные без цитат, переживающие 30-дневный TTL сырья без нарушения правила наследования TTL для данных третьих лиц (data map, р. 2).
- **Суперсессия → reconcile**: новая версия обязательства с исполненными эффектами у старой порождает staged reconcile-бандл через общий approval-контур (Фаза 4, п. 4), gcal — обновлением по детерминированному event ID.
- **Commitment как связующий слой**, не переписывание донорских сторов — совместимость с портом Фаз 1–2.

Изменения: Locked Decisions (+строка «Модель данных»), Implementation Approach (+раздел «Модель данных»), What We're NOT Doing (+RDF/graph store/GraphRAG/embeddings-до-измерений), Фаза 1 п. 4 + критерий evidence-провенанса, Фаза 2 п. 3/3b + критерий статусной модели, Фаза 4 п. 4 + критерии суперсессии (auto + manual), Testing Strategy (+CQ-набор).

### 2026-08-04 Enhancement: трёхшаговый onboarding и предпочтительный канал

Решение владельца: user-facing продукт называется Abrolia; онбординг идёт в строгом порядке `email → WhatsApp → канал общения`. Шаг 1: `@abrolia.com` предвыбран, отдельный Gmail агента через Google OAuth — второй, домен семьи с DNS — третий. Шаг 2: общий номер Abrolia для quick start либо отдельный номер семьи через QR/Evolution. Шаг 3: Telegram предвыбран и рекомендуется, WhatsApp маркирован Beta и доступен после шага 2, минимальный Abrolia Web — first-party альтернатива; email всегда fallback.

Архитектурные следствия, добавленные в план:
- возобновляемая onboarding state machine и идемпотентное provisioning;
- Gmail OAuth перенесён из GA-долга в MVP с fail-closed verification/CASA launch-gate; IMAP/app password удалён из пользовательского пути и оставлен только тестовым seam;
- household-level `channel_preferences`, source-channel replies, проверенный primary для proactive-сообщений и fallback без дубля при `outcome_unknown`;
- отдельные actor bindings для Telegram/WhatsApp/Web и приглашённого взрослого;
- узкий shared-WhatsApp gateway как изолированное исключение из запрета multi-tenant runtime;
- минимальный authenticated Web chat/PWA в MVP, но не полный vault;
- автоматические и ручные критерии для каждого шага, смены primary, OAuth, cross-household routing и test receipt.
