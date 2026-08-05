# Data map и retention-матрица

Что за данные, откуда, где лежат, в какой юрисдикции, сколько живут. Документ —
источник истины для `/export`, `/delete` и retention-джобы (Фаза 2, п. 5).

Правило PR: новый класс данных или новое место хранения → строка здесь **в том же
PR**, что и код.

## 1. Хранилища

| # | Хранилище | Что это | Локация | Шифрование | Владелец ключа |
|---|---|---|---|---|---|
| S1 | SQLite (WAL) dedicated runtime на Fly volume | `events`, runtime-`jobs`/`effects`, домен (todos, reminders, memory, transcripts, actions journal); физически одна семья | Fly.io `ams` (EU, NL) | шифрование диска провайдера | Fly |
| S2 | Content-addressed blob store (тот же volume) | вложения, фото, голосовые, ICS | Fly.io `ams` | шифрование диска провайдера | Fly |
| S3 | Fly apps как раздельные secret namespaces | deterministic app создаётся после profile без volume/Machine; runtime-токены household (Telegram bot, Nerve runtime key, OAuth refresh token отдельного Gmail агента, Evolution apikey, relay-HMAC, per-runtime DSAR bearer) передаются прямо через `SecretSink`; control-plane Fly/provider/encryption/HMAC keys хранятся отдельно; одноразовый bootstrap token появляется только при финальной runtime activation и живёт до durable runtime receipt ack | Fly.io | secrets-стор провайдера | оператор |
| S4 | Бэкапы SQLite (Litestream/снапшоты) | копии S1 и отдельная копия S14; runtime и control plane имеют разные ключи и restore-процедуры | EU object storage | AES-256, отдельный ключ per-household/control-plane | оператор |
| S5 | Nerve inbox (email-опции a/c) | `@abrolia.com` или домен семьи: входящие письма, треды, метаданные вложений | Fly.io `iad` (US) | провайдер | оператор (тот же владелец) |
| S6 | Resend | доставка исходящих; логи/метаданные | US (независимо от sending region) | провайдер | Resend |
| S7 | Anthropic API | промпты и ответы (транзитно) | `global`/`us` (см. residency) | провайдер | Anthropic |
| S8 | Google Calendar/Account | события семьи, OAuth-грант выделенного аккаунта ассистента для календаря | Google (глобально) | провайдер | семья |
| S9 | Telegram | копии сообщений/карточек в чате семьи | Telegram (вне EU) | провайдер | Telegram |
| S10 | Dedicated Evolution-инстанс (WhatsApp option b) | linked-device сессия выделенного номера/SIM семьи, входящие/исходящие сообщения полного WhatsApp-контура | Fly.io `ams` (EU) | шифрование диска | оператор |
| S11 | Отдельный Gmail агента (email-опция b) | отдельный Google-аккаунт, который семья создаёт для ассистента; Gmail API `gmail.readonly` + `gmail.send`, не личный ящик владельца | Google | провайдер | семья |
| S12 | Логи приложения и метрики | ID, статусы, тайминги, размеры — **без содержимого** | Fly.io `ams` + агрегатор (EU) | провайдер | оператор |
| S13 | Legacy/operator records | старые ручные записи до миграции, контакты эскалации и реестр инцидентов; не production-источник account/onboarding/config | у оператора (EU) | шифрование носителя | оператор |
| S14 | Metadata-only control-plane SQLite (WAL) | accounts, households/profiles/memberships, hash-only auth/session records, HMAC-only rate-limit/idempotency keys, onboarding workflow/transitions, provisioning jobs/resource refs, config revisions, bootstrap-token hashes, consent receipts, deletion tombstones | Fly.io `ams` (EU, NL), один Machine/volume | шифрование диска + AES-256-GCM полей; отдельный keyed HMAC для lookup | оператор |
| S15 | Shared WhatsApp gateway durable ingress (будущий adapter; выключен в Phase 1) | webhook общего номера до подтверждённой доставки в один runtime; sender-binding metadata; без модели/tools и household provider secrets | Fly.io `ams` (EU) | шифрование диска + per-household relay-HMAC | оператор |

`app.abrolia.com` обслуживает S14 и onboarding UI. Статический `abrolia.com`
не пишет данные, не устанавливает cookies и не соединяется с control plane.
S14 содержит только account/onboarding/provisioning metadata: семейные письма,
сообщения, attachments, prompts, commitments, approvals и effects в него не
попадают. Прямые идентификаторы и provider refs шифруются AES-256-GCM; поиск по
email, rate-limit buckets и idempotency keys использует отдельный keyed HMAC.
Raw IP/network bucket, email, idempotency key и token/session/bootstrap plaintext
в S14 не хранятся.
Raw Nerve keys, Gmail refresh tokens и webhook secrets также не входят в S14,
job payload/result или runtime manifest: control plane хранит только encrypted
provider refs, secret refs/digests и redacted public state.

## 2. Классы данных

Легенда: **E** — попадает в объединённый account+household `/export`; **D** —
стирается по оркестрированному `/delete` (внешние поверхности — по
[`delete-runbook.md`](delete-runbook.md) и раздел 4). `частично` означает, что
экспортируется или стирается business-запись, но не hash/secret либо сохраняется
минимальный доказательный факт по указанному исключению.

| Класс | Пример | Источник | Хранилища | TTL | E | D |
|---|---|---|---|---|---|---|
| Abrolia account и recovery contact (`accounts`) | account UUID, verified recovery-email ciphertext/lookup-HMAC, status | invite/magic-link onboarding | S14 | жизнь account + **30 дн.** | ✔, кроме lookup-HMAC | ✔ |
| Household/profile/membership (`households`, `household_profiles`, `household_memberships`) | household UUID/slug/status, имя/фамилия (ciphertext), язык, timezone, country, residency, account↔household role | account owner | S14 | жизнь household/account + **30 дн.** | ✔ | ✔ |
| Runtime actor/channel binding | Telegram/WhatsApp/Web external ID, actor UUID, runtime role, verified channel/chat | owner-authorized challenge | S1; provider public ref mirror in S14 config | жизнь account + **30 дн.** | ✔ | ✔ |
| Magic/invite/reauth tokens (`auth_tokens`) | hash, purpose, expiry/used time, attempt count — plaintext token отсутствует | control plane | S14 | link TTL **15 мин.**; запись удаляется **24 ч. после use/expiry** | ✖ | ✔ |
| Web sessions (`sessions`) | session-token hash, CSRF-secret hash, account, idle/absolute expiry, revoked time, coarse security metadata — без raw IP/UA | control plane | S14 | idle 24 ч., absolute 30 дн.; запись удаляется **30 дн. после revoke/expiry** | частично: lifecycle metadata без hashes | ✔ |
| Rate-limit buckets (`rate_limit_buckets`) | keyed-HMAC от network/address/token bucket, kind, начало окна, attempts, timestamps; без raw IP/email/token | control plane | S14 | **24 ч.** | ✖ | ✔ |
| Onboarding workflow/steps (`onboarding_workflows`, `onboarding_steps`) | current step, safe selection/result refs, status/version/attempt | account owner + provider projector | S14 | жизнь household/account + **30 дн.** | ✔ | ✔ |
| Email identity/reservation/activation (`email_identities`, `email_address_reservations`, `email_activation_receipts`) | encrypted address/provider subject, masked address, provider/secret refs and digests, scopes, state/version, local-part reservation, runtime health receipt; без provider credential | owner, email provider projector, runtime activation | S14 | identity/receipt: жизнь account + **30 дн.**; released/expired reservation: **24 ч.** | ✔ | ✔ |
| OAuth transaction (`oauth_transactions`) | state hash, encrypted short-lived PKCE verifier, requested scopes, owner session/workflow binding; без authorization code/refresh token | owner OAuth start/callback | S14 | consume/expiry + **24 ч.** | ✖ | ✔ |
| Onboarding transitions (`onboarding_transitions`) | append-only from/to, command, account/session/request/job IDs, redacted metadata | control plane | S14 | жизнь household/account + **30 дн.** | ✔, кроме session hash/secret | ✔ |
| Idempotency records (`idempotency_requests`) | account+route+keyed-HMAC от idempotency key, SHA-256 запроса, safe response snapshot; raw key не хранится | control plane | S14 | **24 ч.** | ✖ | ✔ |
| Provisioning jobs (`provisioning_jobs`) | encrypted typed intent/result/provider ref, safe status/error/attempt/lease metadata; без secret material/provider body | control-plane worker | S14 | encrypted payload **30 дн. после settled**; техническая metadata **90 дн.** | ✔ без internal idempotency/lease и secret fields | ✔ |
| External resource registry (`external_resources`) | provider/type/stable name, encrypted external ID, status/config revision | control-plane worker | S14 | жизнь household/account + **30 дн.** | ✔ (masked external refs) | ✔ |
| Immutable config revisions (`config_revisions`) | encrypted non-secret manifest, schema/revision/hash/status/activation times | planner | S14; active mirror `/data/household.toml` на runtime volume S1 | жизнь household/account + **30 дн.**; active revision сохраняется, пока household active | ✔ | ✔ |
| Bootstrap records (`bootstrap_tokens`) | token hash, household/runtime/revision binding, expiry/claim/use/revoke timestamps — plaintext только кратковременно в S3 | control plane/runtime | S14 + S3 | encrypted/bootstrap operational payload **30 дн. после used/revoked/expired**; metadata **90 дн.** | ✖ token/hash; lifecycle metadata входит в audit export | ✔ |
| Учётные и provisioning secrets | Fly/provider/runtime API keys, OAuth refresh token, relay-HMAC, field-encryption/HMAC keys | provisioning/OAuth | S3 | пока integration активна; ротация при инциденте; bootstrap secret удаляется после durable runtime receipt ack; DSAR bearer — вместе с exact runtime namespace | ✖ | ✔ (отзыв + удаление) |
| Сырое входящее событие | .eml, подписанный Nerve webhook envelope (`nerve_webhook_events.payload`), WA-сообщение | школа/третьи лица через семью | S1 (`events.raw`, Nerve durable ingress journal), S5/S10/S11 (на стороне канала) | **30 дн.**; Nerve replay/статус metadata — **365 дн.** без payload | ✔ | ✔ |
| Вложения и медиа | PDF-приглашение, фото объявления, голосовая заметка; Nerve bytes с SHA-256, MIME-классом и `retention_until` | входящие / семья | S2 (`nerve_attachments` для Nerve materialization) | **30 дн.** | ✔ | ✔ |
| Извлечённые обязательства | дата, сумма, ответственный, оригинальный отправитель | модель | S1 (`commitments`) | как у порождённой сущности (событие/задача); `superseded`-версии живут столько же, сколько действующая — цепочка версий и есть ответ на «что изменилось» | ✔ | ✔ |
| Прогоны извлечения | модель, хэш промпта, расход токенов, ссылка на событие — **без промпта и без ответа** | система | S1 (`extraction_runs`) | **365 дн.** — столько же, сколько журнал действий: это его часть | ✔ | ✔ |
| Ссылки на источник (`evidence_refs`) | домен отправителя, дата письма, sha содержимого, офсеты — **никогда цитаты** | система | S1 | **365 дн.**; переживает 30-дневный TTL письма намеренно: остаётся проверяемый след без контента третьих лиц | ✔ | ✔ |
| Транскрипты диалога | сообщения семьи ↔ ассистент | семья | S1 | **180 дн.** | ✔ | ✔ |
| Память (`memory_statements`) | «врач Иванова — по вторникам», предпочтения | staged-подтверждение семьи | S1 | бессрочно, **пересмотр раз в 90 дн.** (напоминание семье, устаревшее удаляется); `candidate`, не подтверждённый за 30 дн., удаляется как несостоявшееся предложение | ✔ | ✔ |
| Задачи и напоминания | «оплатить 15 € до 08.09» | подтверждение семьи | S1 | **done + 90 дн.** | ✔ | ✔ |
| Журнал действий (actions) | кто, что, когда подтвердил; effect_id, статус | система | S1 | **365 дн.** | ✔ | ✔ |
| Delivery receipts | `email.delivered/bounced`, WA-статусы | Nerve/Resend/Evolution | S1, S6 | **365 дн.** (у нас); у Resend — по их политике | ✔ | ✔ (у нас) |
| События календаря | «Экскурсия 12.09» | подтверждение семьи | S1 (маппинг) + S8 (сам факт) | маппинг: 365 дн.; событие в Google — у семьи | ✔ (маппинг) | ✔ (маппинг; удаление событий — по выбору семьи) |
| Исходящие письма | текст, получатель, вложения | подтверждение семьи | S1 + S5/S6 для managed/domain options a/c; S1 + S11 для Gmail option b | тело: 365 дн. в журнале; у провайдера — по их политике | ✔ | ✔ (у нас) |
| Данные третьих лиц внутри контента | учитель, другой родитель, реквизиты школы | входящие письма/сообщения | S1, S2 | наследует TTL носителя (30/180/365 дн.) | ✔ | ✔ |
| Данные детей | имя, класс, участие в мероприятии | входящие письма | S1, S2 | наследует TTL носителя; см. [`minors.md`](minors.md) | ✔ | ✔ |
| Промпты и ответы модели | запрос extraction/диалога | система | S7 (транзит), S1 (usage-счётчики без контента) | у нас контент промпта не хранится отдельно; у Anthropic — по коммерческим условиям (без обучения) | ✖ | n/a |
| DLQ: payload неудавшегося события | тот же .eml/сообщение | система | S1 | **30 дн.** — тот же TTL, что и у сырья: неудача обработки не продлевает срок хранения содержимого | ✔ | ✔ |
| DLQ: метаданные отказа | `event_id`, канал, код ошибки, счётчик попыток — **без содержимого** | система | S1 | **90 дн.** *(уточнение к матрице Gate −1)* — нужны для разбора хронических отказов | ✔ | ✔ |
| Логи и метрики | `event_id=…, status=done, 412ms` | система | S12 | **30 дн.** *(уточнение)* | ✔ (идентификаторы связываются с household → это персональные данные, хотя содержания в них нет) | ✔ |
| Onboarding/channel consent receipts (`consent_receipts`) | shared-WhatsApp privacy notice; отдельный dedicated-QR risk consent; purpose, account, locale, text version/hash, accepted/revoked times | account owner onboarding | **S14 — авторитетный append-only источник**; S1 получает только `receipt_id`, digest и enforcement mirror | срок действия + **3 года после отзыва** | ✔ | частично: доказательный факт/версия/hash сохраняются, лишнее содержание стирается |
| Runtime staged consent receipts | подтверждение отдельной записи памяти или runtime-действия | runtime actor | S1 | по цели; доказательный факт согласия — срок действия + **3 года после отзыва** | ✔ | частично |
| Записи по запросам субъектов (DSAR) | account, запрос, этапы runtime/control-plane/external cleanup, partial/unknown outcome | control-plane orchestration | S14; legacy/operator evidence S13 | **3 года** | ✔ | частично (обязанность подотчётности) |
| Deletion tombstone (`deletion_tombstones`) | keyed-HMAC household ID, deleted/expiry time, completion status — без raw ID/content | control plane | S14 | **3 года** | ✖ | ✖ до expiry; затем автоматическое удаление |
| Реестр инцидентов (ст. 33(5)) | факты, оценка, решение об уведомлении | процедура | S13 | **3 года** *(срок наш, требует подтверждения)* | ✖ | ✖ (обязательная запись) |
| Legacy/operator records | исторические manual records до миграции, escalation contact | оператор | S13 | до миграции по соответствующему классу; новые production-записи запрещены | по классу | по классу |
| Бэкапы | отдельные снапшоты S1 и S14 | система | S4 | **30 дн.** скользящее окно *(уточнение)* | ✖ | ✔ по истечении окна; см. раздел 4 |

Сроки новых S14-классов — **provisional pilot policy** для synthetic-only
реализации. Они задаются именованными конфигурационными константами, а не
размазаны по SQL/handlers, и требуют ревью владельца и юриста до real-family
gate. Изменение константы требует синхронного изменения этой матрицы, notice и
retention-тестов. Пока реальных данных нет, эти сроки проверяют корректность
жизненного цикла, но не считаются публичным обещанием production retention.

### Retention-матрица (сводка Gate −1, п. 5)

| Класс | TTL |
|---|---|
| Транскрипты | 180 дней |
| Память | бессрочно, пересмотр раз в 90 дней |
| Журнал действий | 365 дней |
| Задачи/напоминания | выполнено + 90 дней |
| Фото / голос / документы / сырые события | 30 дней |
| Delivery receipts | 365 дней |
| *(уточнения)* DLQ: payload / метаданные отказа | 30 дней / 90 дней |
| *(уточнения)* Логи | 30 дней |
| *(уточнения)* Бэкапы | 30 дней |
| *(уточнения)* Receipts согласий, DSAR, реестр инцидентов | 3 года — срок установлен нами, подтверждается владельцем и юристом |
| Control-plane account/profile/membership/workflow/transition/resource/config | жизнь household/account + 30 дней |
| Auth token / session после завершения | 24 часа после use/expiry / 30 дней после revoke/expiry |
| Rate-limit buckets / idempotency | 24 часа / 24 часа; raw bucket/key не хранится, только keyed-HMAC |
| Provisioning/bootstrap encrypted payload / technical metadata | 30 дней / 90 дней после settled/revoked/expired |
| Deletion tombstone | 3 года |

Runtime реализует ежедневную retention-джобу (родительский план, Фаза 2, п. 5),
control plane — отдельную джобу своей Phase 1.7; отклонение фактического TTL от
таблицы считается дефектом. Payload и technical metadata в `events`/DLQ и
provisioning/bootstrap records хранятся раздельно, иначе разделить сроки
невозможно — это проверяется retention-тестами обоих deployables.

Уточнения помечены как таковые, потому что их нет в п. 5 плана: они требуют
явного согласия владельца (и юриста — для receipts/DSAR/инцидентов).

## 3. Международные передачи

Колонка «механизм» — **планируемый**, а не действующий: на момент Gate −1 не
подписан ни один DPA и не оформлен ни один трансферный механизм (актуальный
статус — [`processors.md`](processors.md), р. 1). Поэтому реальные данные не
обрабатываются.

| Передача | Куда | Планируемый механизм | Статус | Детали |
|---|---|---|---|---|
| Промпты/ответы модели | Anthropic, `global`/`us` | DPA + SCC модуль 2 + TIA | ⏳ не оформлено | режим `eu-app`; `eu-strict` требует Vertex AI EU и падает без него — молчаливого downgrade нет |
| Входящие/исходящие письма (`@abrolia.com`/домен семьи, a/c) | Nerve `iad` (US), Resend (US-метаданные) | DPA + SCC + TIA | ⏳ не оформлено | обсуждается перенос Nerve-инстанса в EU как upgrade-путь |
| Календарь | Google | условия Google для аккаунта семьи | грант выдаёт семья | вне нашего договора |
| Telegram | Telegram | договора нет | ❌ | канал выбирает семья; копии сообщений вне нашего контроля, раскрыто в notice |
| Shared WhatsApp | gateway в EU, WhatsApp/Meta — вне EU | договора нет | ❌, real adapter выключен | только заранее verified взрослые; отдельный channel notice; внешний школьный/group ingress запрещён |
| Dedicated WhatsApp (Evolution) | сервер в EU, WhatsApp/Meta — вне EU | договора нет | ❌, real adapter выключен | linked-device автоматизация отдельного номера с отдельным risk consent, см. [`../SECURITY.md`](../SECURITY.md) |
| Dedicated Gmail агента (опция b) | Google | грант выдаёт семья через OAuth | ⏳ verification/CASA gate | отдельный аккаунт агента; минимальные `gmail.readonly` + `gmail.send`; refresh token encrypted, disconnect отзывает grant |

Полный реестр со статусами — [`processors.md`](processors.md).

## 4. `/export` и `/delete`

- **Инициатор account-flow** — только authenticated owner membership в
  `app.abrolia.com`, с fresh passwordless re-auth не старше 10 минут, CSRF и
  same-origin проверками. Routing UUID/slug и runtime `owner` actor не являются
  источником web-авторизации. Runtime actor-flow сохраняется для содержимого
  S1, но сам по себе не является полным account delete/export.
- **`/export`** объединяет S14 account/onboarding metadata и экспорт dedicated
  runtime; полнота проверяется против колонки **E** автотестами обоих deployables.
  Token/session/bootstrap hashes и secrets не экспортируются.
- **`/delete`** сначала durable фиксирует intent, отменяет новые jobs, отзывает
  account sessions/tokens и оркестрирует runtime/external cleanup. Затем стирает
  S1/S2/S3 и exportable S14 fields и оставляет keyed-HMAC tombstone. Ни runtime
  wipe, ни control-plane cleanup по отдельности не считаются полным удалением;
  `outcome_unknown` честно остаётся незавершённым до reconcile.
- **Внешние поверхности** (полный порядок и что остаётся —
  [`delete-runbook.md`](delete-runbook.md)): Nerve org/inbox → Resend-остатки
  (по их retention) → Google Calendar + отзыв agent-Gmail OAuth grant → Telegram
  (копии у пользователей — вне нашего контроля) → shared-WhatsApp sender unbind
  и gateway payload cleanup → dedicated Evolution logout/delete → runtime и
  control-plane Fly resources/secrets → бэкапы (полное исчезновение в пределах
  30-дневного окна) → Anthropic (по коммерческим условиям).
- Срок ответа на запрос субъекта — 1 месяц (ст. 12(3)).
