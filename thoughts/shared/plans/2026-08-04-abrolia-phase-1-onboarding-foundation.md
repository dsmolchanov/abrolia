---
title: "Abrolia Phase 1 — Onboarding Foundation"
status: implemented-awaiting-manual-validation
created_at: "2026-08-04 16:53:36 CEST"
repository: arbolia
branch: main
base_commit: 781f122b2ab562c83d886da4631e5600d2fff8bc
parent_plan: thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md
scope: construction-unit-1
data_policy: synthetic-only
---

# Abrolia Phase 1 — Onboarding Foundation

## Overview

Эта фаза превращает описание онбординга из канонического MVP-плана в исполнимый
фундамент: Abrolia-аккаунт владельца, household, возобновляемый трёхшаговый
workflow, durable provisioning jobs и защищённая передача итоговой конфигурации
в dedicated runtime семьи.

Название «Phase 1» здесь означает **первый construction unit нового онбординга**,
а не уже завершённую секцию `Phase 1: Thin vertical slice` родительского плана
(`thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md:124-180`). Эта
работа детализирует фундамент пункта `Phase 5.1` родительского плана
(`thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md:283-299`).

Фаза заканчивается работающим synthetic-only потоком:

```text
pilot invite / magic link
        ↓
owner account → household profile
        ↓
email identity → WhatsApp identity → primary channel
   fake adapter      fake adapter       fake adapter
        ↓
immutable DesiredHouseholdSpec
        ↓
dedicated Fly app + volume + secrets + versioned household.toml
        ↓
runtime activation receipt → onboarding complete
```

Реальные Nerve, Gmail OAuth, Evolution/shared WhatsApp и Telegram/Web channel
adapters остаются следующими construction units. Но их интерфейсы, состояния,
идемпотентность, секретный handoff и точки подключения должны быть полностью
определены и протестированы здесь.

## Current State

### Что уже есть

- Runtime физически рассчитан на одну семью: `Database` описан как база одного
  household, использует SQLite WAL, `synchronous=FULL` и одного явного writer
  (`hermes_cloud/core/db.py:1-14`, `hermes_cloud/core/db.py:36-42`,
  `hermes_cloud/core/db.py:78-88`).
- `Household` содержит runtime-роли и allowlist чатов, а `load_household()` уже
  называет `household.toml` целевой формой provisioning
  (`hermes_cloud/core/runcontext.py:84-143`).
- `RunContext` строится только из проверенного транспортом actor/chat; чужой
  чат и неизвестный actor получают нулевые capabilities
  (`hermes_cloud/core/runcontext.py:151-207`).
- Runtime-секреты приходят из env/Fly secrets и намеренно отделены от membership
  (`hermes_cloud/core/config.py:1-9`, `hermes_cloud/core/config.py:46-56`).
- Есть проверенные patterns для append-before-effect, idempotency, leases, DLQ и
  `outcome_unknown` (`hermes_cloud/core/events.py:80-215`,
  `hermes_cloud/core/effects.py:1-18`, `hermes_cloud/core/effects.py:121-240`).
- Статический landing уже развёртывается отдельно и не делает third-party
  requests (`landing/README.md:1-21`).

### Чего нет

- Abrolia account, verified recovery email, owner session и recovery flow.
- Центрального реестра households и отдельного control-plane storage.
- Onboarding state machine, transition journal и provisioning outbox.
- Web/API service, CSRF/session/IDOR protection и onboarding UI.
- One-time bootstrap token и handoff control plane → dedicated runtime.
- Deployment manifests для control plane и runtime.
- Provider interfaces и synthetic adapters.
- Автоматического materialize/validate для versioned `household.toml`.

### Drift, который нельзя переносить в реализацию

Текущая незакоммиченная v3 правка родительского плана — продуктовый канон, но
README/privacy/security всё ещё частично описывают v2: существующий Gmail с app
password и только отдельный WhatsApp-номер. До начала functional code этот drift
нужно устранить, иначе тесты и notice будут защищать старый продукт.

Ключевые места: `README.md:1-12`, `docs/privacy/data-map.md:9-36`,
`docs/privacy/dpia.md:61-90`, `docs/roadmap-channels.md:1-23`.

## Key Discoveries

- Текущий `Household.owner` — ID актора канала, а не Abrolia account principal
  (`hermes_cloud/core/runcontext.py:84-102`). Использовать его как web-account
  ID означало бы смешать две trust boundaries.
- Существующая SQLite и DSAR-логика физически принадлежат одному runtime
  (`hermes_cloud/core/db.py:36-42`, `hermes_cloud/core/dsar.py:82-145`).
  Onboarding metadata требует отдельной схемы, export и delete orchestration.
- Фраза «фиксировать состояние до side effect» недостаточна без отдельного
  transition journal, provisioning intent, stable resource identity и состояния
  `outcome_unknown`.
- Landing не может быть скрытым backend: он статический, а его CSP намеренно
  запрещает network connections (`landing/vercel.json:1-32`). Onboarding должен
  жить на отдельной first-party surface `app.abrolia.com`.
- Consent возникает раньше dedicated runtime. Поэтому текущий S1-only источник
  истины (`docs/privacy/data-map.md:55-60`) технически невыполним и должен быть
  заменён control-plane authority + runtime enforcement mirror.
- В репозитории нет web/auth/deploy stack (`requirements.txt:1-13`,
  `pyproject.toml:1-22`), поэтому packaging и operational boundary входят в эту
  фазу, а не считаются готовой инфраструктурой.

## Desired End State

После фазы существует отдельный deployable `control_plane`, который:

1. аутентифицирует приглашённого владельца passwordless magic link;
2. создаёт account, household и preflight-профиль;
3. ведёт строго упорядоченный workflow `email → WhatsApp → primary channel`;
4. фиксирует transition и provisioning intent **до** внешнего side effect;
5. выполняет side effects background worker'ом с idempotency/reconcile;
6. строит immutable, versioned `DesiredHouseholdSpec` только из verified results;
7. создаёт synthetic dedicated runtime на Fly в `ams`, не передавая секреты
   браузеру;
8. получает activation receipt с совпадающими `household_id`, revision и hash;
9. поддерживает control-plane export/delete и tombstone;
10. не принимает семейные сообщения, не вызывает модель/tools и не читает
    runtime SQLite.

Dedicated runtime по-прежнему:

- имеет отдельный Fly app, Machine, volume, SQLite и secret namespace;
- запускается fail-closed при несовпадении household/config revision;
- не принимает ingress и не стартует model/tool workers до `active` revision;
- строит runtime authorization из channel bindings, а не из account cookie или
  browser-supplied household ID.

### Проверяемый итог фазы

На staging пользователь с `owner@example.test` проходит default synthetic path:

```text
profile
→ firstname.lastname@abrolia.com
→ shared Abrolia WhatsApp
→ Telegram
→ dedicated runtime provisioned
→ config revision activated
```

Повтор любого HTTP command, рестарт control plane/worker или kill процесса в
каждом окне внешнего вызова не создаёт второй household, app, volume, Machine
или provisioning result.

## Locked Architectural Decisions

### 1. Control plane — отдельный deployable

Код располагается в `control_plane/`, а не в `hermes_cloud/onboarding/`.
Это делает запрет импортов видимым: dedicated assistant runtime не импортирует
multi-household account/session/job storage. Общими могут быть только чистые
versioned contract types без DB/client dependencies.

Control plane multi-tenant только по metadata. Он не является multi-tenant
assistant runtime и не хранит письма, сообщения, attachments, prompts,
commitments, approvals или effects семьи.

### 2. Pilot storage — отдельный SQLite на одном Fly Machine

Для 5–20 семей control plane использует собственный `control-plane.db` на EU
Fly volume, WAL + `synchronous=FULL`, с одним process writer. Это **не** runtime
DB и не переиспользует runtime migrations.

Ограничения фиксируются в deploy config и health-check:

- ровно одна control-plane Machine;
- autoscaling/вторая write replica запрещены;
- volume mounted только к этой Machine;
- переход на Postgres обязателен до HA или второй control-plane replica.

### 3. Три независимые идентичности

- `account_id` — principal first-party Abrolia Web/control plane;
- `actor_id` — внутренняя runtime identity члена семьи;
- `agent mailbox` — операционный адрес ассистента.

Recovery email аккаунта не становится agent inbox. Account owner не получает
runtime capabilities, пока отдельный channel binding не связал его с actor.
Telegram/WhatsApp external ID не становится primary key аккаунта.

### 4. Passwordless pilot auth

Phase 1 использует invite-only email magic links. Google Sign-In не добавляется:
он не нужен для Gmail restricted scopes и только смешал бы owner authentication
с отдельным agent-Gmail OAuth flow.

- magic-link TTL: 15 минут, one-time;
- в БД хранится только SHA-256/HMAC hash токена;
- magic link несёт token в URL fragment, который не попадает в HTTP access log;
  first-party confirmation page переносит его в POST и очищает browser history;
- session — opaque random token, в БД только hash;
- cookie: `Secure`, `HttpOnly`, `SameSite=Lax`, host-only;
- idle TTL: 24 часа, absolute TTL: 30 дней;
- rotate session после login/reauth;
- unsafe requests требуют same-origin `Origin` и CSRF token;
- destructive actions требуют свежую re-auth не старше 10 минут.

Mailer задаётся интерфейсом. В Phase 1 доступны in-memory/console adapters только
для `.test` адресов; production adapter и real-family flag остаются fail-closed.

### 5. Preflight profile — не четвёртый продуктовый выбор

До трёх карточек собираются поля, без которых варианты нельзя построить:

- legal/display first name и last name;
- verified recovery email из account;
- family language;
- IANA timezone;
- country;
- residency mode (`eu-app` default, `eu-strict` fail-closed without provider).

Profile показывается как короткая настройка аккаунта, а progress indicator всё
равно содержит три продуктовых шага.

### 6. Consent authority переносится в control plane

До создания runtime authoritative consent receipt физически не может жить в
его S1 SQLite, как сейчас утверждает data map. Новое правило:

- authoritative onboarding receipt хранится append-only в control plane;
- receipt содержит `text_version`, `text_sha`, purpose, actor account, locale,
  accepted/revoked timestamps;
- runtime получает только `receipt_id`, digest и enforcement fields;
- после activation runtime mirror используется для fail-closed enforcement,
  но не переписывает источник истины.

`docs/privacy/data-map.md`, threat model, export и delete runbook обновляются в
том же PR, что создаёт таблицу.

### 7. Browser никогда не получает provisioning credentials

Browser видит только выбор, human-readable status и masked external refs.
Nerve admin key, Fly org token, provider tokens, runtime API keys, bootstrap
tokens и secret values никогда не сериализуются в API response, job JSON,
transition metadata или logs.

### 8. Runtime provisioning — desired state, не сценарий команд

Planner строит immutable `DesiredHouseholdSpec`. Provisioner реализует
`ensure_*` и reconcile по stable identity; повтор стремится к тому же state.

Для Fly:

- app name генерируется сервером: `abrolia-hh-<26-char base32 household uuid>`;
- app/volume/Machine создаются официальным Machines REST API;
- secrets устанавливаются `fly secrets import --stage` через stdin, без shell
  и без значений в argv;
- Machine создаётся после app, volume и staged secrets;
- image задаётся immutable digest, не mutable tag;
- region только `ams`, volume encrypted/auto-backup enabled;
- API token существует только как control-plane Fly secret.

### 9. Bootstrap — двухфазный и one-time

Control plane генерирует 256-bit token, хранит только hash и помещает plaintext
непосредственно в Fly secret dedicated app.

1. Runtime вызывает `claim` и получает non-secret manifest.
2. Повторный `claim` тем же token допустим до activation и возвращает тот же
   revision — это закрывает crash после скачивания.
3. Runtime атомарно пишет config (`temp + fsync + rename`), валидирует его и
   вызывает `activate` с manifest hash.
4. Control plane сверяет expected runtime ref, household, revision и hash,
   помечает token used и job succeeded.
5. Worker удаляет bootstrap secret из app; повтор used-token получает 410.

Provider secrets не входят в manifest: они уже находятся в Fly secret namespace.

### 10. Synthetic-only remains fail-closed

Phase 1 не снимает legal gate. Production config не стартует, если включён хотя
бы один real provider adapter при незакрытом `REAL_FAMILY_DATA_ENABLED` gate.
Тестовый UI и adapters принимают только reserved `.test` emails, synthetic IDs
и documented test phone ranges.

## Implementation Approach

Работа идёт по цепочке `command → durable intent → provider result → verified
step → immutable spec → runtime activation`. Каждый слой имеет один источник
истины и не перескакивает через соседний:

1. API отвечает только за authentication, authorization, validation и commit
   command/transition/job intent.
2. Worker отвечает за side effects и никогда не двигает UI в `verified`, пока
   durable provider result не записан.
3. Projector/state service применяет result к workflow ровно один раз.
4. Planner читает только verified results и создаёт новую immutable revision.
5. Runtime самостоятельно валидирует revision/hash до readiness.

Реализация разбита на additive slices: сначала документы и separate schema,
затем auth/state/fakes, после этого compatibility layer runtime и только затем
synthetic Fly provisioning. Это сохраняет текущий CLI/runtime зелёным на каждом
промежуточном merge и не требует big-bang переключения.

Direct identifiers и provider refs в control plane шифруются на уровне полей
AES-256-GCM ключом из Fly secrets; deterministic lookup использует отдельный
keyed HMAC. Disk encryption Fly остаётся вторым слоем, а не единственной мерой.
Ключ имеет version ID для ротации и не попадает в DB/backup.

## Data Model

### Control-plane migration `0001_control_plane.sql`

Создать отдельный migration runner и следующие таблицы.

#### `accounts`

```text
id UUID-text PK
recovery_email_lookup_hmac TEXT UNIQUE NOT NULL
recovery_email_ciphertext BLOB NOT NULL
encryption_key_version TEXT NOT NULL
email_verified_at REAL NOT NULL
status active|locked|deleting|deleted NOT NULL
created_at, updated_at REAL NOT NULL
```

Перед HMAC email проходит Unicode-aware normalization + casefold для domain;
display и normalized values лежат внутри AES-GCM ciphertext. В logs email
запрещён.

#### `households`

```text
id UUID-text PK
slug TEXT UNIQUE NOT NULL                 # generated, never user authorization
status draft|onboarding|provisioning|active|deleting|deleted
family_language TEXT
timezone TEXT
country_code TEXT
residency_mode eu-app|eu-strict
current_config_revision INTEGER NOT NULL DEFAULT 0
runtime_ref TEXT
created_at, updated_at, deleted_at
```

`slug` и URL UUID — routing hints; membership check остаётся обязательным.

#### `household_profiles`

First/last names хранятся AES-GCM ciphertext с household ID как associated
data; таблица содержит key version и timestamps. Язык/timezone/country остаются
в `households`, потому что нужны runtime planner и не являются direct identifier.

#### `household_memberships`

```text
account_id FK accounts
household_id FK households
role owner|adult
status invited|active|revoked
created_at, accepted_at, revoked_at
PRIMARY KEY(account_id, household_id)
```

MVP API позволяет одному account создать один household и одного active owner,
но схема не зашивает это ограничение. Invitation/channel binding второго
взрослого реализуется позже.

#### `auth_tokens` и `sessions`

`auth_tokens` хранит token hash, purpose `invite|login|reauth`, account/email
reference, expires/used timestamps и attempt counter. `sessions` хранит token
hash, account, CSRF secret hash, idle/absolute expiry, revoked timestamp и
coarse security metadata без raw IP/user-agent.

#### `onboarding_workflows`

```text
id PK
household_id UNIQUE FK
state profile_required|in_progress|runtime_provisioning|activating|complete|cancelled
current_step profile|email_identity|whatsapp_identity|primary_channel|runtime
version INTEGER NOT NULL
created_at, updated_at, completed_at
```

`version` увеличивается на каждую accepted command и проверяется через
`If-Match`. Lost update возвращает 409 с актуальным snapshot.

#### `onboarding_steps`

Ровно четыре строки на workflow: internal `profile` плюс три user-facing steps.

```text
workflow_id FK
kind profile|email_identity|whatsapp_identity|primary_channel
ordinal 0..3
status locked|available|selected|provisioning|waiting_user|verifying|verified|failed|cancelled
selection_kind TEXT
selection_ciphertext BLOB        # typed PII, AES-GCM, no secrets
result_ciphertext BLOB           # typed provider refs, AES-GCM, no secrets
public_status_json TEXT          # masked fields safe for UI
error_code TEXT                  # stable code, no provider body
attempt INTEGER
updated_at
UNIQUE(workflow_id, kind)
UNIQUE(workflow_id, ordinal)
```

#### `onboarding_transitions`

Append-only audit: workflow/version, from/to state+step status, command,
account/session IDs, request ID, related job ID, redacted metadata and time.
Нет `UPDATE`/`DELETE` в normal flow.

#### `idempotency_requests`

Unique `(account_id, route, idempotency_key)`, request hash, response status/body
snapshot, created/expiry. Повтор с тем же key и другим request hash → 409.

#### `provisioning_jobs`

```text
id PK
household_id, workflow_id FK
kind email_identity|whatsapp_identity|channel_binding|runtime
operation TEXT
intent_key TEXT UNIQUE
desired_revision INTEGER
request_sha TEXT
request_ciphertext BLOB         # typed intent, AES-GCM
status pending|running|waiting_user|succeeded|failed|outcome_unknown|cancelled
provider TEXT
external_ref_ciphertext BLOB
result_ciphertext BLOB          # typed result, AES-GCM
error_code TEXT
attempts INTEGER
lease_until, leased_by
created_at, updated_at, settled_at
```

`intent_key = household_id:step:selection_kind:attempt_or_revision`. Новый retry
после definite failure создаёт новый attempt key; сетевой unknown тот же job
автоматически не переисполняет.

#### `external_resources`

Registry resource type/provider/stable name/encrypted external ID/status/config revision.
Секреты и provider response отсутствуют. Unique `(provider, resource_type,
stable_name)` позволяет ensure/reconcile.

#### `config_revisions`

Immutable AES-GCM encrypted non-secret manifest JSON, schema version, SHA-256, status
`planned|issued|claimed|active|superseded|revoked`, activation timestamps.
Новая revision — новая строка; active revision не редактируется.

#### `bootstrap_tokens`

Token hash, household/runtime/config revision binding, expires, claimed, used,
revoked timestamps. Plaintext не хранится ни в одной таблице.

#### `consent_receipts`

Append-only receipt из Locked Decision 6. Текст consent хранится versioned в
репозитории; DB хранит version + hash, не дублирует большой текст.

Для shared WhatsApp фиксируется принятие channel privacy notice; для dedicated
QR дополнительно требуется отдельный risk consent о linked-device automation и
возможной блокировке номера. Один receipt не подменяет другой.

#### `deletion_tombstones`

Keyed HMAC household ID, deleted_at, expires_at (3 года) и completion status.
Tombstone проверяется перед созданием jobs и bootstrap claim.

### State invariants enforced by SQL + service

- У household ровно один workflow.
- Только один non-settled job на intent key.
- Verified step имеет typed result; locked/available step результата не имеет.
- Step `n+1` нельзя сделать available до verified `n`.
- Workflow `complete` требует три verified user steps и active config revision.
- Household `active` требует workflow complete и runtime activation receipt.
- Deleted/deleting household не создаёт новые transitions/jobs/config revisions.
- Agent inbox lookup HMAC не может равняться recovery-email lookup HMAC;
  service также сравнивает расшифрованные normalized values перед verify.
- Secret-like keys (`token`, `secret`, `password`, `api_key`, `refresh_token`)
  запрещены recursive validator'ом в selection/result/job/manifest JSON.

## State Machine

### Happy path

```text
profile_required
  └─ save_profile
     → email_identity.available

email_identity.available
  └─ select_email
     → selected → provisioning → verified
     → whatsapp_identity.available

whatsapp_identity.available
  └─ select_whatsapp
     → selected → provisioning/waiting_user → verified
     → primary_channel.available

primary_channel.available
  └─ select_primary
     → selected → provisioning/waiting_user → verified
     → runtime_provisioning

runtime_provisioning
  └─ ensure_runtime → issue_revision → bootstrap claim → activation receipt
     → complete
```

API command транзакционно проверяет session+membership+workflow version,
записывает selection, transition и job intent, коммитит и только затем отвечает.
Worker никогда не держит DB transaction во время network/subprocess call.

### Failure and retry

- Definite provider rejection → step `failed`, stable `error_code`, safe retry
  button; raw provider error только redacted operator log.
- Rate-limit → job remains pending with bounded `not_before`; attempt counter
  растёт, selection не сбрасывается.
- Network timeout после send → `outcome_unknown`; никаких auto retries.
- Operator reconcile вызывает provider `inspect(stable_ref)` и переводит тот же
  job в succeeded/failed либо оставляет unknown.
- QR/DNS/user action timeout → `waiting_user`, а не failed; refresh/check создаёт
  idempotent inspect intent.
- Изменение verified выбора требует `reset_from(step)`: сначала durable cleanup
  plan, затем downstream steps reset. Silent overwrite запрещён.

### Cancel/delete

`cancel` запрещает новые jobs и помечает pending jobs cancelled. Уже созданные
external resources удаляются отдельными compensating jobs в обратном порядке.
Unknown outcome требует reconcile до заявления «удалено».

`delete` дополнительно отзывает sessions/tokens, deprovisions runtime, очищает
exportable control-plane data по policy и оставляет tombstone. Runtime `/delete`
и control-plane delete — две части одного orchestration, ни одна не считается
полным DSAR delete отдельно.

## API and UI Contracts

### Public same-origin routes (`app.abrolia.com`)

- `POST /api/v1/auth/request-link` — invite/test mailer, generic response.
- `GET /auth/verify#token=...` — confirmation page с
  `Referrer-Policy: no-referrer`; fragment не доходит до proxy/server log, GET
  не расходует token, а first-party JS сразу делает `history.replaceState`.
- `POST /api/v1/auth/consume` — consume once, rotate session, redirect на URL
  без token; token из fragment принимается только этой confirmation page.
- `POST /api/v1/auth/logout`.
- `GET /api/v1/me` — account + accessible household summaries.
- `POST /api/v1/households` — one pilot household, idempotent.
- `GET /api/v1/onboarding/current` — current household derived from session,
  current version, three-step view model and safe statuses.
- `PUT /api/v1/onboarding/profile`.
- `POST /api/v1/onboarding/steps/{kind}/select`.
- `POST /api/v1/onboarding/steps/{kind}/retry`.
- `POST /api/v1/onboarding/reset/{kind}`.
- `POST /api/v1/onboarding/cancel`.
- `GET /api/v1/onboarding/export`.
- `POST /api/v1/onboarding/delete` — reauth required.

Все mutating endpoints требуют `Idempotency-Key`, `If-Match`, CSRF и same-origin.
Household из body/query никогда не является authorization source.

### Internal runtime routes

- `POST /internal/v1/bootstrap/claim`
- `POST /internal/v1/bootstrap/activate`

Они принимают bearer bootstrap token, не browser session; rate-limited,
HTTPS/private-network only и всегда связывают token с expected runtime ref.

### UI shell

Создать first-party onboarding UI в control-plane deployable, а не превращать
Vercel landing в secret-bearing backend. Landing меняется только ссылкой CTA на
`https://app.abrolia.com/start`; его CSP остаётся `connect-src 'none'`.

UI содержит:

- profile preflight;
- progress `1 Email → 2 WhatsApp → 3 Communication`;
- три email cards, две WhatsApp cards, три primary-channel cards в каноническом
  порядке с правильными default/recommended/Beta labels;
- resumable status screens `setting up`, `waiting for you`, `needs attention`;
- human-readable retry без provider payload;
- banner `Synthetic staging — no real family data` в этой фазе.

Cards вызывают provider-agnostic API. Synthetic adapters возвращают те же
typed states, которые позже будут возвращать реальные integrations.

## Provider Contracts

В `control_plane/provisioning/contracts.py` определить Protocols и Pydantic
contracts без HTTP/SDK types:

```python
class ProvisionResult: external_ref, public_result, secret_material
class InspectResult: pending | ready | failed | absent

class EmailIdentityProvisioner:
    ensure(intent, idempotency_key) -> ProvisionResult
    inspect(external_ref) -> InspectResult
    deprovision(external_ref) -> InspectResult

class WhatsAppIdentityProvisioner: ...
class ChannelBindingProvisioner: ...
class RuntimeProvisioner:
    plan(spec) -> list[PlannedWrite]
    ensure_runtime(spec, idempotency_key) -> RuntimeRef
    apply_config(runtime_ref, revision, secrets) -> None
    inspect(runtime_ref, revision) -> RuntimeInspection
    deprovision(runtime_ref) -> RuntimeInspection
```

`secret_material` — in-memory short-lived object. Он может быть передан только
`SecretSink`; сериализация/repr запрещены. Сразу после установки в runtime
namespace mutable buffers очищаются best-effort, ссылки освобождаются. Python не
гарантирует zeroization immutable strings, поэтому adapters не создают лишних
копий и сужают lifetime; DB хранит только external key ID/prefix/digest.

Phase 1 adapters:

- deterministic fake email for all three options;
- fake shared/dedicated WhatsApp including `waiting_user`/expired QR simulation;
- fake Telegram/WhatsApp/Web binding + test receipt;
- `DryRunRuntimeProvisioner`;
- real `FlyRuntimeProvisioner` только для synthetic staging.

## Detailed Implementation Phases

### Phase 1.0 — Canon and privacy alignment

#### Files

- Modify `README.md`.
- Modify `docs/privacy/data-map.md`.
- Modify `docs/privacy/dpia.md`.
- Modify `docs/privacy/lawful-bases.md`.
- Modify `docs/privacy/processors.md`.
- Modify `docs/privacy/privacy-notice-{ru,en}.md`.
- Modify `docs/privacy/delete-runbook.md`.
- Modify `docs/SECURITY.md`.
- Modify `docs/roadmap-channels.md`.
- Modify parent MVP plan only where this plan resolves an ambiguity.

#### Changes

1. Remove user-facing existing Gmail/app-password path; describe dedicated Gmail
   agent OAuth and its verification/CASA gate.
2. Add metadata-only control-plane trust boundary, session/CSRF/IDOR threats,
   app subdomain and synthetic-only flag.
3. Replace manual S13 production store with control-plane SQLite S14; keep S13
   only for legacy/operator records until migrated.
4. Add account/session/workflow/job/config/consent data classes, TTL, export and
   delete rules.
5. Record authoritative control-plane consent rule and runtime mirror.
6. Update shared/dedicated WhatsApp and minimal Web descriptions to v3.
7. Resolve Gate wording: engineering Gate −1 closed for synthetic work; real
   family data remains blocked by legal/processor/OAuth prerequisites.

#### Acceptance

- `rg` finds no public recommendation to connect existing Gmail/app password.
- Data-map has a row for every new table/class before migrations merge.
- Threat model explicitly covers account takeover, CSRF, IDOR, bootstrap token,
  control-plane compromise and provisioning replay.

### Phase 1.1 — Package and control-plane persistence

#### Files

- Modify `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`.
- Add `control_plane/__init__.py`.
- Add `control_plane/config.py`.
- Add `control_plane/crypto.py`.
- Add `control_plane/db.py`.
- Add `control_plane/migrations/0001_control_plane.sql`.
- Add `control_plane/models.py`.
- Add `control_plane/repositories/*.py`.
- Add `tests/control_plane/test_db.py`.
- Add `tests/control_plane/test_crypto.py`.
- Add `tests/control_plane/test_schema_contract.py`.

#### Changes

1. Package `hermes_cloud` and `control_plane` explicitly; add console entrypoints
   without breaking `python -m hermes_cloud.cli`.
2. Add pinned-compatible FastAPI/uvicorn/httpx dependencies and template support;
   keep provider SDKs optional behind adapters.
3. Implement separate DB/migration path; do not import runtime `Database` class
   directly even if transaction semantics are mirrored.
4. Apply schema and SQL constraints from Data Model.
5. Implement AES-GCM field cipher with random nonce, key version and AAD
   `table:row:field`; implement keyed-HMAC lookup separately from encryption key.
6. Add recursive secret-field validator before encryption at every typed write boundary.
7. Add schema classification registry so an unclassified table fails export/
   delete tests, mirroring `tests/test_dsar.py`.

#### Acceptance

- Fresh migration and repeated migration are deterministic.
- Partial migration rolls back.
- Runtime migrations never create control-plane tables and vice versa.
- Concurrent writers serialize; second process is rejected by startup lock.
- Tests prove no serialized row contains seeded secret canaries.
- Equal emails have equal lookup HMAC but different ciphertext; ciphertext
  tampering or wrong AAD/key version fails closed.

### Phase 1.2 — Account, magic-link auth and household creation

#### Files

- Add `control_plane/auth/tokens.py`.
- Add `control_plane/auth/sessions.py`.
- Add `control_plane/auth/mailer.py`.
- Add `control_plane/services/accounts.py`.
- Add `control_plane/services/households.py`.
- Add `control_plane/api/dependencies.py`.
- Add `control_plane/api/auth.py`.
- Add `control_plane/api/households.py`.
- Add `tests/control_plane/test_auth.py`.
- Add `tests/control_plane/test_authorization.py`.

#### Changes

1. Implement invite issuance and login/reauth magic links with hash-only storage,
   one-time consume and generic anti-enumeration response.
2. Implement revocable opaque sessions, CSRF/Origin checks and security headers.
3. Create account/household/membership in one transaction; owner role exists only
   in control plane at this point.
4. API lookup always scopes through active membership. Add helper that accepts
   resolved session principal, never a caller-supplied owner/account.
5. Rate-limit request-link and consume by token/account buckets without storing
   raw IP.
6. Add fresh-reauth dependency for cancel/delete/export containing sensitive
   account data.
7. Decrypt direct identifiers only inside service methods that need them; API
   serializers require explicit masked/full view and default to masked.

#### Acceptance

- Replay/expired/tampered magic link rejected.
- Session fixation impossible; login rotates token.
- Missing/wrong Origin or CSRF rejected on every unsafe endpoint.
- Account A cannot read/mutate household B even with valid UUID in URL/body.
- Recovery email cannot be silently changed without new verification.
- Cookie/token values never appear in logs or DB plaintext.

### Phase 1.3 — Onboarding state machine and durable worker

#### Files

- Add `control_plane/onboarding/contracts.py`.
- Add `control_plane/onboarding/state.py`.
- Add `control_plane/onboarding/service.py`.
- Add `control_plane/provisioning/contracts.py`.
- Add `control_plane/provisioning/jobs.py`.
- Add `control_plane/provisioning/worker.py`.
- Add `control_plane/provisioning/fakes.py`.
- Add `control_plane/api/onboarding.py`.
- Add `control_plane/cli.py` commands `worker`, `jobs`, `reconcile`, `dry-run`.
- Add `tests/control_plane/test_onboarding_state.py`.
- Add `tests/control_plane/test_provisioning_jobs.py`.
- Add `tests/control_plane/chaos_child.py`.

#### Changes

1. Encode allowed transitions as a complete transition table, not scattered
   handler conditionals.
2. Implement command transaction: auth/version/idempotency → transition → job
   intent → commit.
3. Implement worker lease/reclaim, bounded retry for known-safe failures and
   explicit `outcome_unknown` stop.
4. Implement provider registry keyed by option + feature flags.
5. Build deterministic fakes that can simulate success, wait, reject, timeout,
   crash and reconcile.
6. Implement `reset_from`, cancellation and compensating job graph.
7. Expose redacted polling snapshot; do not use websockets in this phase.

#### Acceptance

- Cannot skip/reorder steps or activate from selected/unverified results.
- Same `Idempotency-Key` + same body returns previous response; different body
  conflicts.
- Stale `If-Match` conflicts instead of overwriting.
- Kill after transition commit leaves exactly one runnable job.
- Kill before/after provider response does not create duplicate fake resource.
- Unknown outcome never auto-retries; reconcile is required.
- Reset email invalidates/cleans WhatsApp/channel/runtime downstream state.

### Phase 1.4 — Desired spec and runtime manifest compatibility

#### Files

- Add `control_plane/provisioning/manifest.py`.
- Add `control_plane/provisioning/planner.py`.
- Add `hermes_cloud/core/runtime_manifest.py`.
- Modify `hermes_cloud/core/runcontext.py`.
- Modify `hermes_cloud/core/config.py`.
- Modify `hermes_cloud/cli.py`.
- Add `tests/control_plane/test_manifest.py`.
- Modify `tests/test_runcontext.py`.
- Modify `tests/test_config_and_cli.py`.

#### Changes

1. Define `DesiredHouseholdSpecV1` and canonical JSON serialization/hash.
2. Build spec only from profile + three verified typed results.
3. Define versioned `household.toml` fields: schema version, household UUID,
   config revision/hash, language, IANA timezone, country, residency mode,
   runtime actor/channel bindings and provider public refs.
4. Preserve legacy `household.toml` and env fallback for local tests; versioned
   file wins in provisioned runtime.
5. Fail startup when schema unsupported, timezone invalid, household/revision env
   mismatch, active channel lacks verified binding or agent inbox equals fallback.
6. Keep current RunContext fail-closed semantics; no account ID enters runtime
   caps calculation.
7. Add `hermes-cloud validate-manifest` and `bootstrap` entrypoints.

#### Acceptance

- Same inputs produce byte-identical manifest/hash.
- Mutation after revision creation is impossible.
- Generated TOML loads through runtime and preserves unknown actor/chat = zero caps.
- Timezone reaches runtime extraction config; no location guessing fallback.
- Secret canary in provider result is rejected before manifest creation.
- Older supported schema loads; future schema fails with actionable error.

### Phase 1.5 — Secure bootstrap and Fly synthetic provisioner

#### Files

- Add `control_plane/provisioning/fly.py`.
- Add `control_plane/provisioning/secrets.py`.
- Add `control_plane/api/internal_bootstrap.py`.
- Add `hermes_cloud/runtime/bootstrap.py`.
- Add `hermes_cloud/runtime/service.py`.
- Add `deploy/control-plane/Dockerfile`.
- Add `deploy/control-plane/fly.toml`.
- Add `deploy/runtime/Dockerfile`.
- Add `deploy/runtime/machine-config.json`.
- Add `tests/control_plane/test_fly_provisioner.py`.
- Add `tests/control_plane/test_bootstrap.py`.
- Add `tests/test_runtime_service.py`.

#### Changes

1. Implement Machines REST client for app/volume/Machine get/create/update/stop.
2. Implement `FlySecretSink` with `subprocess.run([...], input=..., shell=False)`;
   pass secret values only on stdin, redact exceptions and zero buffers.
3. Ensure app/volume/Machine by stable name/metadata; inspect before create.
4. Create volume before Machine in `ams`; attach at `/data`; use immutable image
   digest and minimum resources defined in config.
5. Implement two-phase bootstrap claim/activate and token cleanup.
6. Runtime writes `/data/household.toml` atomically, validates, persists active
   revision receipt and reports readiness.
7. `/healthz` may be healthy during bootstrap; `/readyz` remains false until
   active revision. Model, channel listeners and ingress remain stopped.
8. Add operator reconcile/deprovision commands and stable error taxonomy.

#### Acceptance

- Mocked Fly tests cover 200/404/409/422/429/5xx and network unknown.
- Secret values absent from argv, exception text, logs, DB and API responses.
- Repeated ensure returns the same app/volume/Machine refs.
- Bootstrap token bound to another runtime/household/revision is rejected.
- Crash after claim resumes same revision; replay after activation gets 410.
- Revision/hash mismatch leaves runtime not ready and household not active.
- Deprovision unknown result does not claim deletion complete.

### Phase 1.6 — Onboarding UI and staging composition

#### Files

- Add `control_plane/api/app.py`.
- Add `control_plane/web/templates/*.html`.
- Add `control_plane/web/static/{onboarding.css,onboarding.js}`.
- Modify `landing/index.html` CTA only.
- Add `tests/control_plane/test_api.py`.
- Add `tests/control_plane/test_ui_contract.py`.
- Add `docs/onboarding-runbook.md`.
- Modify `.github/workflows/ci.yml`.

#### Changes

1. Render same-origin server pages with progressive enhancement; workflow remains
   usable without client-side state as source of truth.
2. Implement canonical card order/defaults and accessible progress/errors.
3. Poll safe onboarding snapshot during jobs; resume exact step after reload/login.
4. Add security headers/CSP for `app.abrolia.com`; no analytics/cookies beyond
   first-party session.
5. Add synthetic staging config, bootstrap runbook, operator reconcile and cleanup.
6. CI runs runtime + control-plane tests, ruff, fixture sanitizer and gitleaks;
   tests assert real provider flags default false.

#### Acceptance

- Reload/browser restart resumes the exact state without duplicate command.
- Back button cannot mutate a verified step; reset is explicit.
- Default cards are `@abrolia.com`, shared WhatsApp, Telegram.
- Gmail card says separate agent account; no personal Gmail/app password copy.
- WhatsApp cards both show Beta; dedicated QR consent is explicit.
- Email fallback is displayed as verified owner recovery contact, never agent inbox.
- Landing remains static, cookie-free and does not call control-plane API.

### Phase 1.7 — Export/delete, observability and release gate

#### Files

- Add `control_plane/privacy/export.py`.
- Add `control_plane/privacy/delete.py`.
- Add `control_plane/observability.py`.
- Add `tests/control_plane/test_export_delete.py`.
- Add `tests/control_plane/test_observability.py`.
- Add `docs/control-plane-restore.md`.
- Add `thoughts/shared/implementations/<date>-onboarding-foundation-validation.md`
  during validation, not implementation planning.

#### Changes

1. Export every classified account/household/workflow/transition/job/config/
   receipt row except token/session hashes and secrets.
2. Orchestrate runtime export/delete then control-plane deletion/tombstone;
   report partial/unknown external cleanup honestly.
3. Daily retention for tokens/sessions/idempotency/jobs and policy-defined data.
4. Structured metrics only: workflow/step/job IDs, status, duration, attempts;
   no emails, phone numbers, domain values, QR, tokens or provider bodies.
5. Health: DB/volume/backup age, worker lease backlog, unknown outcomes,
   bootstrap expirations; provider health is adapter status, not secret echo.
6. Backup/restore control-plane SQLite with separate key and documented restore
   test; encryption/HMAC keys are restored through an independently protected
   secret procedure and never embedded in the backup. Runtime provider secrets
   never enter the backup.

#### Acceptance

- New unclassified table fails export/delete test.
- Delete cancels pending jobs, revokes sessions, deprovisions runtime, leaves
  tombstone and cannot be resurrected by delayed callback.
- Consent retention exception is represented in export/delete result.
- Restore produces a working control plane with jobs paused for explicit resume.
- Logs captured under failure contain none of the seeded PII/secret canaries.

## Automated Verification

Mandatory commands at phase completion:

```bash
python3 scripts/check_fixtures.py --all --require-deny
ruff check .
pytest -m "not live"
pytest tests/control_plane -q
```

Add a dedicated crash matrix:

| Window | Expected recovery |
|---|---|
| after transition commit, before worker lease | one pending job |
| after lease, before provider call | lease expires, safe retry/inspect |
| after provider accepted, before response persisted | inspect/reconcile; no blind duplicate |
| after result persisted, before step verified | projector completes transition once |
| after config issued, before bootstrap claim | same revision claimable |
| after claim, before config rename | same token/revision returned |
| after runtime write, before activate callback | activate retry, same hash |
| after activate, before token cleanup | token is used; cleanup job resumes |

Security matrix:

- account A × household B × every route → 404/deny;
- anonymous/expired/revoked session × every private route → deny;
- missing/wrong Origin/CSRF × every unsafe route → deny;
- duplicate/stale idempotency/version × every command → deterministic conflict;
- token/secret canary × every serialization/log path → absent;
- deleted household × callback/job/bootstrap → tombstone deny.

## Manual Verification

All manual tests use synthetic data and dedicated staging resources.

1. Generate invite for `owner@example.test`, consume once, confirm replay fails.
2. Complete preflight with `Europe/Prague`; refresh and re-login; state remains.
3. Complete default three-step fake path and observe one Fly app, one volume, one
   Machine in `ams`.
4. Stop worker at each crash window and resume; resource counts stay one.
5. Inspect Fly app: secrets exist by name, values never appear in control-plane
   DB/logs/API; bootstrap token disappears after activation.
6. Inspect runtime: `/healthz=200`, `/readyz=200` only after matching activation;
   `household.toml` has correct language/timezone/revision and mode 0600.
7. Tamper config revision/hash and confirm `/readyz` stays non-ready.
8. Attempt cross-household URLs from a second synthetic account; no existence
   or state leaks.
9. Export household and compare with data-map; secrets/session hashes absent.
10. Delete household; verify runtime deprovisioned, session revoked, tombstone
    blocks delayed bootstrap/provider callbacks.
11. Restore control-plane backup in isolated staging and run smoke tests with
    workers paused until operator resume.

## What This Phase Does NOT Do

- Не создаёт реальный Nerve org/inbox/domain/webhook/API key.
- Не реализует Gmail OAuth, Gmail history ingest или Gmail send.
- Не создаёт Evolution instance/QR и shared WhatsApp gateway.
- Не связывает реальный Telegram bot/user/chat и не отправляет test message.
- Не реализует Abrolia Web chat/PWA/push; только onboarding account surface.
- Не приглашает второго взрослого в runtime; schema поддерживает membership,
  но channel binding/invite acceptance идёт вместе с channel unit.
- Не переносит существующий household runtime на Postgres.
- Не добавляет billing, self-service public signup, admin dashboard, HA или
  multi-region control plane.
- Не снимает legal/DPA/CASA gates и не обрабатывает реальные family content.

## Testing Strategy

- **Pure state tests:** полная таблица разрешённых/запрещённых transitions.
- **Repository/SQL tests:** constraints, migrations, classification, retention.
- **API security tests:** session, CSRF, IDOR, optimistic concurrency,
  idempotency, reauth.
- **Contract tests:** один и тот же scenario suite для fake и Fly runtime
  provisioners; позже его обязаны пройти Nerve/Evolution/channel adapters.
- **Chaos tests:** настоящие subprocess SIGKILL в перечисленных окнах.
- **Serialization tests:** secret/PII canaries на DB, logs, API and manifests.
- **Runtime compatibility tests:** generated config через существующий
  `load_household()` и fail-closed RunContext matrix.
- **Live synthetic smoke:** отдельный staging Fly org/app namespace; test is
  opt-in marker и всегда удаляет только exact generated resources.

## Performance and Capacity

Pilot targets, не публичные SLA:

- API p95 без provider call < 250 ms;
- state snapshot < 64 KiB;
- worker lease batch ≤ 20, один writer;
- provider concurrency ≤ 4 с per-provider limit;
- onboarding metadata < 10 MiB на 20 households без logs/backups;
- no request держит SQLite transaction во время network/subprocess call;
- backlog/unknown outcomes отражаются в health до того, как UI обещает success.

При необходимости второй control-plane Machine performance work останавливается
и сначала планируется Postgres migration; SQLite over network/shared volume не
используется.

## Migration and Rollout

1. Merge docs/canon alignment separately from runtime behavior.
2. Ship control-plane schema/auth/state machine with only fake adapters.
3. Ship manifest loader in backwards-compatible mode; current CLI flows and tests
   обязаны остаться зелёными.
4. Deploy control plane to staging with single EU volume and synthetic-only flag.
5. Publish immutable runtime image digest.
6. Enable Fly synthetic adapter for operator account only.
7. Run chaos/manual matrix and backup/restore rehearsal.
8. Link landing CTA to staging only after auth and security tests pass.
9. Keep every real provider adapter disabled; enabling each requires its own
   implementation plan and contract suite.

Rollback:

- API can be rolled back while DB migration remains additive;
- worker is stopped before rollback so old code does not lease new job kinds;
- active config revisions are immutable and runtimes keep the last valid config;
- synthetic Fly resources are removed by exact external registry IDs, never by
  wildcard or prefix deletion;
- `outcome_unknown` resources are reconciled manually before cleanup claim.

## Success Criteria

Phase 1 is complete only when all are true:

- [x] Canon/privacy/security docs describe v3 onboarding and control-plane data.
- [x] Account/session/household API is secure against replay, CSRF and IDOR.
- [x] Three user-facing steps are strict, resumable and auditable.
- [x] Commands and jobs are idempotent; unknown outcome is never blindly retried.
- [x] Desired spec is built only from verified results and contains no secrets.
- [x] Dedicated synthetic Fly runtime is provisioned once and activated by
      matching revision/hash.
- [x] Existing runtime authorization remains fail-closed.
- [x] Export/delete covers both control plane and dedicated runtime boundary.
- [x] Full automated suite, chaos matrix, live synthetic smoke and restore test
      are green.
- [x] Production real-provider flags remain false and synthetic-only gate is
      visibly enforced.

## References

### Repository

- Parent canon: `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md:47-72,278-320,367-378`
- Runtime identity: `hermes_cloud/core/runcontext.py:66-207`
- Runtime config/secrets: `hermes_cloud/core/config.py:1-122`
- SQLite transaction pattern: `hermes_cloud/core/db.py:25-138`
- Durable ingress pattern: `hermes_cloud/core/events.py:80-215`
- Effect/reconcile pattern: `hermes_cloud/core/effects.py:1-240`
- Runtime export/delete: `hermes_cloud/core/dsar.py:1-145`
- Current trust boundary: `docs/SECURITY.md:17-105`
- Current data classes/stores: `docs/privacy/data-map.md:9-118`
- Nerve admin contract:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/sdk/python/src/nerve_email/admin.py:32-184`

### Platform documentation verified 2026-08-04

- Fly Machines API: https://fly.io/docs/machines/api/
- Fly Apps resource: https://fly.io/docs/machines/api/apps-resource/
- Fly Machines resource: https://fly.io/docs/machines/api/machines-resource/
- Fly Volumes resource: https://fly.io/docs/machines/api/volumes-resource/
- `fly secrets import` via stdin: https://fly.io/docs/flyctl/secrets-import/

## Implementation Order Summary

```text
canon/privacy sync
  → separate control-plane DB
  → passwordless owner auth + household
  → strict state machine + durable jobs + fakes
  → versioned desired spec/runtime manifest
  → secure bootstrap + synthetic Fly provisioner
  → onboarding UI
  → export/delete/observability
  → chaos + staging + restore validation
```
