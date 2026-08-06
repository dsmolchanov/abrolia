---
title: "Abrolia Phase 2 — Family Email Identity"
status: implementation-ready-with-prerequisites
created_at: "2026-08-04 18:30:15 CEST"
repository: arbolia
branch: main
base_commit: 781f122b2ab562c83d886da4631e5600d2fff8bc
parent_plan: thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md
depends_on: thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md
scope: construction-unit-2
data_policy: synthetic-only-until-explicit-launch-gates
---

# Abrolia Phase 2 — Family Email Identity

> **Revision 2026-08-05 — production wiring and reconnect identity.** Real
> Nerve email remains independently gated from real family data and is enabled
> only for an explicit household allowlist with a complete HTTPS/admin/platform
> configuration. Nerve org reconciliation identity is lifecycle-scoped as
> `arbolia:household:<household_id>:email:<email_identity_id>`, not household-only.
> Cleanup leaves the old org tombstoned: retrying the old identity remains a
> conflict, while a new email identity creates a fresh isolated org. Automatic
> tombstone resurrection is forbidden because it could revive a delayed job and
> expose retained org history to a newly issued runtime credential.
>
> **Revision 2026-08-06 — Gmail runtime execution closure.** The production
> runtime now starts a bounded Gmail History worker and the provisioned sender
> factory selects the OAuth Gmail API provider. Immediate timeout and
> crash-left-pending sends reconcile by exact RFC Message-ID in Sent; absent or
> ambiguous results remain `outcome_unknown` without resend. Process tests cover
> initial baseline, durable ingest, restart/cursor continuity, encrypted grant
> reuse and idempotent revoke. Live Google test-user acceptance remains a manual
> release gate and does not enable the real-family/CASA flags.

## Overview

Эта фаза заменяет fake email step из Phase 1 тремя реальными, но единообразно
управляемыми путями:

1. рекомендуемый семейный ящик `first_last@abrolia.com` через Nerve;
2. отдельный Gmail, который семья создаёт для агента и подключает через Google
   OAuth;
3. ящик на домене семьи через Nerve с DNS-записями и самостоятельной
   верификацией.

Фаза охватывает не только выдачу адреса. Её итог — активная email identity с
проверенными ingress и compose/send, безопасной передачей credentials в
dedicated runtime, возобновляемым provisioning, disconnect/delete и одинаковыми
гарантиями approvals/effect journal для Nerve и Gmail.

```text
onboarding email card
        |
        +-- @abrolia.com -- Nerve platform domain -- household tenant
        |
        +-- Gmail -------- Google OAuth ---------- household runtime
        |
        `-- own domain --- DNS verify / Nerve ---- household tenant
                                  |
                       verified provider binding
                                  |
                  runtime ingress + staged compose/send
                                  |
                         activation health receipt
```

Phase 2 не изменяет порядок онбординга: email остаётся первым из трёх
продуктовых шагов. Она реализует `EmailIdentityProvisioner`, заложенный в Phase
1, и снимает только email-specific fake. WhatsApp и primary communication
channel остаются отдельными construction units.

## Current State Analysis

### Что уже есть в Arbolia runtime

- RFC822/EML parser извлекает original sender, forwarded chain, attachments и
  thread key (`hermes_cloud/ingest/eml.py:1-341`).
- Event store даёт durable append и dedup по `external_id`
  (`hermes_cloud/core/events.py:80-215`).
- Email compose проходит typed proposal, approval, effect journal, kill switch
  и останавливается в `outcome_unknown`, не повторяя side effect вслепую
  (`hermes_cloud/execute/email_send.py:1-192`,
  `hermes_cloud/runner/pipeline.py:433-513,613-682`).
- Runtime export/delete и raw-email retention уже существуют, но не управляют
  реальными provider resources (`hermes_cloud/core/dsar.py:27-145`,
  `hermes_cloud/core/retention.py:29-138`).

### Что является legacy, а не целевой реализацией

Текущие `gmail_poll.py`, SMTP backend и env `HERMES_GMAIL_ADDRESS` /
`HERMES_GMAIL_APP_PASSWORD` реализуют личный Gmail через IMAP/SMTP app password
(`hermes_cloud/core/config.py:1-122`, `hermes_cloud/ingest/gmail_poll.py:1-262`).
Этот путь:

- не показывается в production UI;
- не включается real-provider flag;
- остаётся только test seam до удаления отдельной migration;
- не используется как шаблон хранения Google refresh token.

`GoogleCalendar.from_token_file()` перезаписывает OAuth JSON в plaintext на
volume (`hermes_cloud/execute/gcal.py:264-282`); Gmail adapter не должен копировать
этот pattern.

### Что Phase 1 предоставит, но на base commit ещё отсутствует

- отдельный `control_plane/` и migration `0001_control_plane.sql`;
- owner session, household и строгий onboarding state machine;
- durable provisioning jobs, reconcile и `outcome_unknown`;
- `EmailIdentityProvisioner`, `SecretSink`, immutable desired spec;
- dedicated Fly app/volume/Machine и two-phase runtime bootstrap;
- fake email options с теми же typed states.

Phase 2 нельзя merge поверх выдуманного API. После реализации Phase 1 нужно
сначала сверить реальные migrations, Protocol signatures и state names с этим
планом и обновить только Phase 2 paths/names без изменения зафиксированных ниже
инвариантов.

### Nerve blockers, подтверждённые на `nerve-cloud` HEAD `7a98e13`

- Root `abrolia.com` может быть active domain только одной Nerve organization;
  managed-domain mode распознаёт лишь дочерние домены вида
  `family.abrolia.com` (`internal/cloudapi/handler_domains.go:21-32`,
  `internal/store/migrations/core/0008_org_domains.sql:31-35`).
- Inbox обязан принадлежать тому же org, что и domain, а RLS и long-lived key
  изолируют только по org, не по inbox
  (`internal/cloudapi/handler_inboxes.go:78-126`,
  `internal/store/migrations/core/0003_tenant_rls.sql:14-42`).
- Поэтому общий Nerve org для всех `@abrolia.com` ящиков недопустим: credential
  одной семьи потенциально получает доступ к почте других семей.
- Create org/domain/inbox не имеет полноценного idempotency/reconcile contract;
  lost response может создать duplicate. Для inbox нет DB uniqueness по адресу.
- Org hard delete отсутствует; inbox delete только disables; provider-domain
  cleanup и referential lifecycle неполны.
- Signed org webhooks существуют для outbound events, но durable inbound
  `email.received` и attachments из отдельного Nerve plan ещё не реализованы.

Следовательно, Phase 2 начинается с отдельного Nerve construction unit. Abrolia
adapter не компенсирует tenant gap общим admin proxy и не передаёт Nerve
bootstrap key в household runtime.

## Desired End State

После Phase 2 владелец выбирает один из трёх вариантов и получает один
`active` email binding:

### `@abrolia.com` — default/recommended

- UI предлагает транслитерированный `first_last@abrolia.com` и позволяет
  изменить local part.
- Control plane резервирует адрес, создаёт отдельный household tenant в Nerve и
  ящик на platform-owned root domain.
- Почта и runtime credential изолированы на уровне household org; root-domain
  DNS и platform ownership остаются общими.
- `hello@abrolia.com` и другие системные имена никогда не предлагаются семье.

### Dedicated Gmail — option 2

- Семья сама создаёт новый Google account для агента; Abrolia не создаёт Google
  account и никогда не запрашивает password/app password.
- OAuth выбирает account явно, показывает фактически подключённый адрес и требует
  отдельного подтверждения, что это dedicated agent mailbox.
- Runtime получает только `gmail.readonly` и `gmail.send`; refresh credential
  encrypted at rest, access token memory-only.
- Подключение реальных семей fail-closed до Google verification, Restricted
  Scope security assessment/CASA и одобрения описанного AI use case.

### Family-owned domain — option 3

- Control plane создаёт household Nerve org/domain intent и показывает точные
  DNS records без секретов.
- State остаётся `waiting_user`, пока Nerve не подтвердит домен.
- После verification семья выбирает local part, создаётся inbox и отдельный
  household runtime credential.

### Общий runtime result

- Inbound email сначала durable append, затем ACK/process.
- Одинаковый RFC822 даёт одинаковый canonical event/thread независимо от Nerve
  или Gmail source.
- Отправка всегда начинается с существующего staged approval/effect contract.
- Provider timeout reconciles по stable identity; неопределённый результат не
  отправляется повторно автоматически.
- Disconnect/delete останавливает ingress, revokes credential, удаляет/tombstones
  provider resource согласно выбранному варианту и не сообщает success до
  подтверждённого cleanup либо явного `outcome_unknown`.

## Locked Architectural Decisions

### 1. Nerve root-domain tenancy — domain grant, не shared household org

В Nerve вводится различие:

- platform org владеет и верифицирует `abrolia.com` один раз;
- `org_domain_grants` разрешает конкретному household org создавать inbox на
  этом domain;
- inbox и все его messages принадлежат household org;
- unique `(org_domain_id, normalized_local_part)` защищает адрес глобально;
- RLS, tenant keys и webhook остаются org-scoped и тем самым изолируют семью.

Это сохраняет точный продуктовый адрес `name@abrolia.com` и существующую
org-level security boundary. Альтернативы `name@family.abrolia.com`, общий org
или multi-tenant Abrolia email gateway не используются.

### 2. Provider work всегда durable и idempotent

Nerve API и SDK получают stable `external_ref`/`Idempotency-Key` для org,
domain grant/domain, inbox, key и webhook. Unique constraints являются финальной
защитой от races. На timeout:

- inspect by external ref до повторного create;
- one-time key с потерянным response revokes unknown key(s) и issues новый;
- webhook с потерянным secret rotates secret;
- raw key/secret не сохраняется в idempotency response ledger.

Control plane записывает job intent до network call и сохраняет только provider
IDs, prefix/digest и redacted public result.

### 3. Secret namespace появляется раньше финального runtime activation

Phase 1 должна разделить Fly provisioning на две ступени:

1. `ensure_secret_namespace(household_id)` создаёт dedicated Fly app и staged
   secret namespace после создания household/profile;
2. volume/Machine/image/bootstrap создаются после трёх verified onboarding steps.

Это обязательный handoff contract: Google OAuth callback и Nerve create-key
возвращают one-time secret во время первого шага. Они сразу передаются в
`SecretSink`; control-plane DB, browser, job JSON и manifest их не получают.

Если Phase 1 уже реализовала app+secret staging только в финальном
`ensure_runtime`, до начала Phase 2 её API расширяется additive методом. Нельзя
временно хранить refresh token или Nerve key в control-plane SQLite.

### 4. Email identity `verified` и runtime binding `active` — разные состояния

- `verified`: provider grant/resource существует, public identity совпадает,
  credential safely staged.
- `activating`: runtime импортировал binding, но health checks не завершены.
- `active`: inbound cursor/webhook и outbound self-test проверены runtime receipt.
- `needs_attention`: credential revoked, DNS drift, history gap, webhook failure.
- `disconnecting` / `deleted` / `outcome_unknown`: lifecycle states.

Workflow может строить desired spec из `verified`, но onboarding completion
требует runtime activation receipt со статусом `active`.

### 5. Один canonical email ingress seam

Добавляется `ingest_rfc822(source, provider_event_id, raw_bytes, received_at)`,
который:

1. нормализует RFC Message-ID одинаково для всех providers;
2. формирует source-scoped external ID, сохраняя cross-source message digest;
3. вызывает существующий EML parser;
4. append'ит raw event с `synchronous=FULL` до ACK/cursor advance;
5. возвращает canonical event/thread key worker'у.

Это исправляет текущий drift: IMAP poller сохраняет bracketed Message-ID и иной
context key, чем `ingest_bytes()`.

### 6. Gmail использует History polling в dedicated runtime

Для pilot 5–20 families runtime выполняет scheduled `users.history.list`, а не
центральный Pub/Sub consumer. Так body/attachments не проходят через
multi-household control plane.

- Initial connect сохраняет текущий `historyId`; старые письма не импортируются.
- Обрабатываются только сообщения, которые на момент fetch относятся к `INBOX`;
  `SENT` используется отдельно для delivery reconciliation.
- Cursor advancing происходит только после durable append всех fetched items.
- `404` для expired `startHistoryId` запускает bounded resync с overlap от
  `last_success_at/connected_at`; EventStore dedup закрывает повторы.
- Если bounded resync не может доказать полноту интервала, binding переходит в
  `needs_attention`; silent rebaseline запрещён, владелец явно подтверждает
  новый baseline после понятного предупреждения о возможном gap.
- Backoff учитывает quota/429; runtime показывает stale sync health.

Push/Pub/Sub остаётся future optimization. Если он понадобится, `watch` должен
регулярно renew и иметь fallback history sync; это не входит в эту фазу.

### 7. Gmail send reconciles по deterministic RFC Message-ID

Gmail backend получает stable approval/effect identity и формирует deterministic
`Message-ID`. После timeout он ищет exact `rfc822msgid:` в `SENT`:

- found once → persisted receipt и success;
- definitely rejected → failed;
- not found/ambiguous → `outcome_unknown`, без blind resend.

Nerve adapter использует provider idempotency key и возвращает тот же canonical
receipt shape: `message_id`, `provider_ref`, `accepted_at`, `status`.

### 8. Google credentials принадлежат runtime, не control plane

OAuth web-server flow использует Authorization Code + PKCE:

- one-time state hash связан с owner session, household, workflow revision и
  requested option; TTL 10 минут;
- `prompt=select_account`, `access_type=offline`;
- exact scopes: `openid email`, `gmail.readonly`, `gmail.send`;
- callback сверяет state, PKCE, issuer/audience, Google `sub`, granted scopes и
  selected address;
- владелец видит адрес и отдельным действием подтверждает dedicated mailbox;
- refresh credential immediately encrypted/staged through `SecretSink`;
- control plane хранит `sub`, encrypted/masked address, scope set, secret ref и
  digest, но не token.

Runtime volume содержит encrypted grant record/ciphertext; wrapping key живёт в
Fly secret namespace. Access token существует только в памяти и никогда не
пишется в logs/SQLite. Disconnect вызывает Google revoke, удаляет runtime grant
и secret material.

### 9. Google policy gate является частью runtime config

`gmail_real_enabled` нельзя включить только env-переменной. Gate требует:

- verified production OAuth app/domain/privacy disclosures;
- approved `gmail.readonly` Restricted Scope use case;
- completed current CASA/security assessment and annual revalidation process;
- explicit in-product disclosure/consent immediately before OAuth;
- Limited Use disclosure;
- documented prompt-injection controls и запрет использовать семейную почту для
  обучения общего/чужого AI model;
- production and test Google Cloud projects separated.

До закрытия gate разрешены только synthetic adapter и allowlisted OAuth test
users без real family data.

### 10. Local-part policy серверная и не раскрывает household inventory

Canonical suggestion:

- Unicode name → explicit transliteration → lowercase ASCII;
- separator `_`, collapse repeated separators;
- allowed `[a-z0-9]+(?:[._-][a-z0-9]+)*`, length 3–48;
- reserved: `hello`, `support`, `security`, `privacy`, `admin`, `abuse`,
  `postmaster`, `webmaster`, `billing`, `team`, `noreply`, `no-reply` и конфиг
  system aliases;
- collision suggestions use neutral suffix `2`, `3`, ...;
- public availability endpoint returns only `available/unavailable` under owner
  session + rate limit, without owner/provider metadata;
- reservation has TTL; final Nerve unique constraint is authoritative.

## Data Model

### Control-plane migration `0002_email_identity.sql`

Additive tables after Phase 1 `0001_control_plane.sql`:

#### `email_identities`

```text
id UUID-text PK
household_id FK
option managed_abrolia | gmail | own_domain
status selected | provisioning | waiting_user | verified | activating |
       active | needs_attention | disconnecting | deleted | outcome_unknown
address_ciphertext / address_lookup_hmac / address_masked
provider_subject_ciphertext nullable
provider_resource_refs_json redacted
secret_binding_ref nullable
granted_scopes_json nullable
verified_at / activated_at / disconnected_at nullable
version integer
created_at / updated_at
UNIQUE active-ish identity per household
```

#### `email_address_reservations`

```text
normalized_domain + normalized_local_part UNIQUE
household_id / email_identity_id
status held | consumed | released | expired
expires_at / created_at / consumed_at
```

#### `oauth_transactions`

```text
id UUID-text PK
household_id / onboarding_session_id / account_id
state_hash UNIQUE
pkce_verifier_ciphertext or secret reference
requested_scopes_json / workflow_version
expires_at / consumed_at / failed_at
```

PKCE verifier is short-lived encrypted metadata, not a provider credential. A
background cleanup removes consumed/expired rows after audit-minimum TTL; state
plaintext is never stored.

#### `email_activation_receipts`

```text
email_identity_id + desired_revision UNIQUE
runtime_ref / provider
inbound_check / outbound_check / checked_at
receipt_digest / status
```

Provisioning job/resource tables from Phase 1 remain the side-effect ledger;
provider secrets never move into these new tables.

### Runtime migration `0006_email_identity.sql`

Current runtime migrations end at `0005_ontology.sql`; reserve `0006` for:

- `email_bindings`: provider, address, public provider IDs, state, timestamps;
- `email_ingress_receipts`: binding/event IDs, HMAC/replay state, durable materialization status;
- `oauth_grants`: encrypted refresh credential blob, key version, Google sub,
  scopes, revoked_at;
- `email_sync_state`: provider binding, cursor/history ID, connected_at,
  last_success_at, backoff/health;
- `email_sends`: effect ID, binding revision, request hash, provider idempotency
  key and state, without MIME body;
- `email_delivery_receipts`: effect/approval ID, RFC Message-ID, provider ref,
  state and reconciliation timestamps.

If Phase 1 or concurrent work consumes `0006`, renumber before merge; do not
reuse a shipped migration number.

## Implementation Approach

Работа идёт четырьмя independently testable strata:

1. исправить Nerve tenancy/idempotency/lifecycle;
2. реализовать provider-neutral identity state и Nerve provisioning;
3. унифицировать runtime ingress/egress и подключить Nerve;
4. добавить Google OAuth/Gmail adapter и закрыть privacy/security rollout.

Nerve prerequisite можно делать параллельно с Phase 1, потому что он находится
в соседнем repo. Изменения Abrolia control plane начинаются после стабилизации
Phase 1 contracts. Каждый provider проходит общий scenario suite; production
flags остаются false до provider-specific gates.

## Detailed Implementation Phases

### Phase 2.0 — Phase 1 handoff and canon/privacy alignment

#### Files

- Reconcile implemented Phase 1 files under `control_plane/` and tests.
- Modify `README.md`, `docs/SECURITY.md`.
- Modify `docs/privacy/data-map.md`, `dpia.md`, `processors.md`,
  `lawful-bases.md`, `delete-runbook.md`, `privacy-notice-{ru,en}.md`.
- Modify `scripts/check_fixtures.py` and tests only if new secret shapes are not
  already covered.

#### Changes

1. Add early `ensure_secret_namespace` to `RuntimeProvisioner`/`FlySecretSink`.
2. Split legacy personal Gmail/app-password docs/config from production paths;
   make legacy flags test-only and visibly deprecated.
3. Document Google/Nerve data flows, control-plane metadata, runtime content,
   transfer locations, retention, DSAR and processor/subprocessor gates.
4. Add Google Limited Use and contextual OAuth disclosure copy; do not claim
   CASA/verification complete before evidence exists.
5. Add fixture/log scanners and canaries for OAuth client secret, refresh token,
   Nerve bootstrap/runtime keys and webhook secrets.

#### Acceptance

- [x] Phase 1 fake scenario remains green.
- [x] Fly app/secret namespace can be ensured before volume/Machine with no duplicate.
- [x] `rg` finds no production recommendation for personal Gmail/app password.
- [x] DB/log/API/manifest test canaries contain no raw provider secret.

#### Status — complete (2026-08-05)

- Full repository regression: 656 passed, 1 skipped (`pytest -q`).
- `ruff check .` and `git diff --check` pass.
- `scripts/check_fixtures.py --all` is clean; CI/private donor deny-list remains
  an environment-supplied gate and was not present in this local shell.
- Google verification/CASA and the remaining legal/processor launch gates are
  still explicitly fail-closed; this status does not claim they are complete.

### Phase 2.1 — Nerve root-domain tenancy and durable admin contract

#### Repository/files (`/Users/dmitrymolchanov/Programs/nerve-cloud`)

- Add core migrations after current `0018` without colliding with the existing
  inbound-events plan reservation.
- Modify `internal/cloudapi/handler_{orgs,domains,inboxes,keys,webhooks}.go`.
- Modify matching `internal/store/*` repositories and tenant auth/RLS policies.
- Modify `sdk/python/src/nerve_email/admin.py` and SDK contract tests.
- Implement/complete the separate
  `thoughts/shared/plans/2026-08-02-inbound-events-and-attachments.md` before
  enabling inbound in Abrolia.

#### Changes

1. Add platform-domain grants from owner org to tenant org, with explicit
   create/revoke/list authorization and audit trail.
2. Keep inbox/messages owned by household org while resolving mail on granted
   root domain.
3. Add DB uniqueness for normalized full address and concurrency tests.
4. Add stable external refs/idempotency + inspect/list for org, domain, grant,
   inbox, key and webhook.
5. Add reactivation/disable/delete semantics, org lifecycle and provider cleanup
   with honest `outcome_unknown`.
6. Complete signed `email.received`, durable attachments and inbox/thread APIs
   needed by household runtime.
7. Align SDK version, response fields (`id` vs `key_id`), docs and immutable
   package pin.

#### Acceptance

- Two household orgs can own isolated inboxes on `abrolia.com`; each key receives
  404/403 for the other's inbox, messages, thread and attachments.
- Concurrent create for one address produces exactly one inbox.
- Lost response at every create/rotate/delete boundary reconciles without
  duplicate resource or leaked usable key.
- Runtime tenant key cannot call bootstrap/admin or manage another org.
- Signed inbound webhook retries until ACK and never loses attachment metadata.
- Domain/provider cleanup behavior is covered by integration and chaos tests.

### Phase 2.2 — Email identity state, API and common provider contracts

#### Files

- Add `control_plane/migrations/0002_email_identity.sql`.
- Add `control_plane/email/{contracts,models,repository,service,local_part}.py`.
- Add `control_plane/providers/email/{base,fake}.py`.
- Modify `control_plane/onboarding/{state,service}.py`.
- Modify `control_plane/provisioning/{contracts,jobs,worker,planner,secrets}.py`.
- Add `control_plane/api/email.py`.
- Add `tests/control_plane/email/*`.

#### Changes

1. Implement locked state model and downstream invalidation/reset semantics.
2. Add typed intents/results for managed, Gmail and own-domain paths.
3. Implement local-part transliteration, reserved aliases, reservation TTL and
   rate-limited availability.
4. Ensure `ProvisionResult` hands secret material directly to SecretSink before
   a durable verified result is written.
5. Define stable error taxonomy: user-action, safe-retry, definitive-failure,
   auth-revoked, provider-degraded, outcome-unknown.
6. Extend desired manifest with provider kind/public binding/secret references,
   never credential values.

#### Acceptance

- Cannot own two active email identities or consume another household reservation.
- Reserved/system aliases and Unicode/confusable edge cases are rejected.
- Crash before/after provider response/SecretSink/durable result resumes safely.
- Changing email invalidates old runtime activation and all downstream onboarding
  results that reference the old binding.

### Phase 2.3 — Managed `@abrolia.com` Nerve provisioning

#### Files

- Add `control_plane/providers/email/nerve_client.py`.
- Add `control_plane/providers/email/nerve_managed.py`.
- Add `control_plane/provisioning/reconcile_email.py`.
- Add contract/integration/chaos tests with Nerve staging or hermetic server.

#### Changes

1. Ensure the Nerve org by a stable email-lifecycle external ref containing both
   the Abrolia household ID and email identity ID. Replays of one identity return
   the same org; reconnect after cleanup creates a fresh org and never restores
   the tombstoned predecessor.
2. Ensure platform-domain grant and reserved inbox.
3. Create minimum-scope tenant runtime key and per-household signed webhook.
4. Stream one-time key/webhook secret directly into household Fly secrets;
   persist IDs/digests only.
5. Inspect address, org ownership, grant, key and webhook before marking verified.
6. Implement managed lifecycle: revoke webhook/key, disable/delete inbox, revoke
   grant; never delete shared platform domain/org.

#### Acceptance

- Default suggested address provisions end-to-end exactly once.
- `hello@abrolia.com` remains system-owned and unavailable.
- Household credential cannot enumerate another household.
- Re-run after process kill yields same org/grant/inbox and one active key/webhook.
- Delete never removes `abrolia.com` or another family's resource.

### Phase 2.4 — Family-owned domain and DNS waiting flow

#### Files

- Add `control_plane/providers/email/nerve_byo_domain.py`.
- Extend `control_plane/api/email.py` and onboarding templates/components.
- Add DNS fixture/integration tests.

#### Changes

1. Canonicalize IDNA/domain, block public suffixes, Abrolia-controlled domains,
   localhost/test-reserved and provider-unsupported values.
2. Recommend a dedicated mail subdomain (for example `assistant.example.com`)
   when apex already handles family mail; show a blocking warning before MX
   changes that could disrupt the existing root-domain mailbox.
3. Ensure household org/domain intent, return exact DNS records as typed public
   result and enter `waiting_user`.
4. Poll/inspect with bounded backoff; expose record-level pending state without
   internal provider errors.
5. After verified domain, reserve/create inbox/key/webhook as in managed path.
6. On reset/delete, revoke credential/webhook/inbox before domain/org cleanup;
   preserve explicit unknown outcome when provider cleanup is unconfirmed.

#### Acceptance

- [x] Reload/login resumes the same DNS records and state.
- [x] Wrong/partial DNS stays waiting; verified DNS advances once.
- [x] Domain cannot be claimed by two households through race or normalization trick.
- [x] Delete/reconnect tests cover DNS still present, provider unavailable and lost
  response.
- [x] BYO DNS inspection resumes automatically from durable `waiting_user` jobs
  with bounded 30/60/120/240-second backoff; after the fifth total job attempt,
  polling stops and explicit `CHECK` remains available.
- [x] Production composition registers both Nerve providers only when the
  fail-closed real-email config and household allowlist are complete.
- [x] Cleanup followed by reconnect of the same household creates a new org, while
  a delayed ensure for the deleted email identity remains rejected.

#### Status — complete and live-accepted (2026-08-05)

- The operator-owned `test.axiomatlas.llc` subdomain completed the production
  BYO flow using six provider-supplied DNS records and no real family data.
- The first durable inspect job stayed pending through four checks and advanced
  exactly once on attempt five. Login and two reloads retained the same verified
  identity and DNS state.
- Cleanup succeeded while the DNS records remained published. Reconnect of the
  same household/domain created a distinct identity and complete Nerve graph,
  then verified through the same durable job on attempt three.
- The live run captured missing/unrecognized records followed by the complete
  record set. A particular partial-record combination was not separately
  snapshotted; the identical pending branch remains covered by the hermetic
  record-level matrix.

### Phase 2.5 — Provider-neutral runtime email core

#### Files

- Add `hermes_cloud/core/migrations/0006_email_identity.sql`.
- Add `hermes_cloud/email/{contracts,service,receipts}.py`.
- Add `hermes_cloud/ingest/rfc822.py`.
- Modify `hermes_cloud/ingest/{inject,eml,worker}.py`.
- Modify `hermes_cloud/execute/email_send.py`.
- Modify `hermes_cloud/runner/pipeline.py` and `cli.py`.
- Add/modify `tests/test_{eml,email_send,pipeline,dsar}.py`.

#### Changes

1. Introduce common provider protocols for polling/webhook ingest, send and
   reconciliation without leaking SDK types into pipeline.
2. Route all raw email through canonical `ingest_rfc822` and preserve existing
   parser behavior.
3. Extend send receipt/result with provider ref and stable effect identity while
   preserving kill switch, role checks and payload binding.
4. Separate config fields for `email_provider`, public address and secret names;
   remove combined `has_gmail` production meaning.
5. Carry `effect_id`, `from_identity_id` and immutable binding revision through
   email payload/card/executor. A binding change makes an older approval stale
   and requires cancel/re-stage; UI shows `From`, not only `To` and subject.
6. Add runtime service loop, health/readiness and migration-aware DSAR/retention.

#### Acceptance

- [x] Same EML through Nerve/Gmail/test injection yields one normalized Message-ID,
  same thread key and no duplicate work.
- [x] Existing email approval tests remain green for every backend contract fixture.
- [x] An approval created for binding revision N cannot send after revision N+1 activates.
- [x] Unknown send outcome is never replayed automatically.
- [x] Provider secrets are absent from config repr, diagnostics, DSAR and logs.

#### Status — automated implementation complete (2026-08-05)

- Runtime migration `0006_email_identity.sql`, provider-neutral contracts,
  canonical RFC822 ingress and durable send/delivery receipts are implemented.
- Full repository regression, Ruff, fixture scanner and `git diff --check` pass.
- Manual runtime smoke with a real provider remains deferred to the Phase 2.6
  Nerve adapter; Phase 2.5 itself makes no external provider call.

### Phase 2.6 — Nerve runtime ingress and send

#### Files

- Add `hermes_cloud/ingest/nerve_webhook.py` and attachment worker support.
- Add `hermes_cloud/execute/nerve_send.py`.
- Add runtime routes/service wiring and `tests/test_nerve_*.py`.

#### Changes

1. Verify per-household HMAC, timestamp skew and replay ID before accepting
   `email.received`; durable append before 2xx ACK.
2. Fetch bodies/attachments in worker using tenant key, validate size/type/hash,
   and apply retention/classification.
3. Implement compose/send with stable Nerve idempotency key and delivery receipt
   updates; key is the child `effect_id`, not the reusable approval ID.
4. Surface webhook age, DLQ, attachment fetch failure and credential revocation
   to runtime health/control-plane activation receipt.

#### Acceptance

- [x] Invalid/expired/replayed signature never appends or ACKs as success.
- [x] Crash after append before ACK causes provider retry but one event.
- [x] Attachment failure is retryable/DLQ without losing parent message.
- [x] Managed and BYO domain pass the same ingress/send scenario suite.

#### Status — automated implementation complete (2026-08-05)

- Runtime migration `0007_nerve_runtime.sql`, signed webhook journal, async
  body/attachment materializer, Nerve MCP compose adapter and health signals
  are implemented.
- Full repository regression, Ruff, fixture scanner and `git diff --check` pass.
- Real-provider staging smoke (signed inbound PDF through Nerve and approved
  outbound compose) remains the manual verification gate; no real family data
  is authorized by these automated results.

### Phase 2.7 — Google OAuth control-plane flow

#### Status — automated implementation complete (2026-08-05)

- PKCE/state/session/household/workflow binding, exact scopes, dedicated mailbox
  confirmation, immediate SecretSink handoff, replay rejection and allowlisted
  policy gates are implemented.
- Runtime-first revoke and late-callback rejection are implemented; live Google
  test-user connect/revoke remains the manual staging gate.

#### Files

- Add `control_plane/providers/email/google_oauth.py`.
- Add OAuth start/callback/confirm/disconnect routes.
- Add contextual disclosure/account-confirmation UI.
- Add `tests/control_plane/email/test_google_oauth.py`.

#### Changes

1. Implement state/PKCE transaction, secure callback validation and TTL cleanup.
2. Request exact scopes and record exact granted set; missing/extra/unexpected
   account result fails closed.
3. Show connected address, compare it with Abrolia recovery address and require
   explicit dedicated-mailbox attestation before verified.
4. Stage credential immediately in household secret namespace, then consume state.
5. Represent the one-time code-exchange/SecretSink crash window explicitly as
   `secret_handoff_unknown`. Inspect only non-secret sink evidence/digest; if the
   install cannot be proven, revoke when possible and require a new user connect
   instead of replaying the authorization code or persisting the token in a job.
6. Implement revoke/disconnect and late-callback/tombstone rejection.
7. Keep real flow behind test-user allowlist and policy gate.

#### Acceptance

- State replay, session swap, household swap, expired state, PKCE mismatch,
  account mismatch and scope downgrade are rejected.
- Browser history/API/logs/control-plane DB contain no code, token or secret.
- Crash windows around token exchange and SecretSink have defined reconcile or
  explicit restart-with-revoke behavior.
- OAuth test user can connect/revoke without enabling real families.

### Phase 2.8 — Gmail History ingest and Gmail API send

#### Status — automated implementation complete (2026-08-05)

- Encrypted grant rotation, initial History baseline, INBOX-only ingestion,
  append-before-cursor acknowledgement, bounded resync, health taxonomy and
  exact Sent Message-ID reconciliation are implemented and hermetically tested.
- Real Gmail quota/cursor/restart smoke remains manual and opt-in.
- Runtime execution wiring was completed on 2026-08-06: `serve_runtime()` owns
  the Gmail History loop, the sender factory owns `GmailSendProvider`, and
  timeout/restart reconciliation is process-tested. The immutable image deploy
  and live Google test-user matrix remain open.

#### Files

- Add `hermes_cloud/ingest/gmail_api.py`.
- Add `hermes_cloud/execute/gmail_api_send.py`.
- Add encrypted grant store/token refresh helper.
- Add `tests/test_gmail_api_{ingest,send,oauth_grant}.py`.

#### Changes

1. Save initial profile history ID without importing historical mailbox.
2. Poll History, fetch RAW messages, filter current INBOX membership and call
   canonical ingress seam.
3. Handle expired cursor with bounded resync/overlap and EventStore dedup.
4. Refresh access token in memory, atomically rotate encrypted credential only
   when Google returns a new refresh credential.
5. Send MIME through Gmail API; reconcile timeout through exact Sent
   `rfc822msgid` query and persist canonical receipt.
6. Expose quota/auth/history-gap health without addresses/content in metrics.

#### Acceptance

- [x] Restart resumes cursor without gap or duplicate processing.
- [x] 404 expired history, 429 quota, revoked grant and malformed RAW fixtures are
  covered.
- [x] Sent messages are not re-ingested as incoming work.
- [x] Timeout-found-in-Sent resolves once; absent/ambiguous remains unknown.
- [x] Refresh/access tokens never reach volume plaintext, logs or exception text.

### Phase 2.9 — UI activation, lifecycle, observability and rollout

#### Status — automated implementation complete (2026-08-05)

- Contextual OAuth UI, dual-check activation receipts, runtime Gmail health,
  reconnect/reset cleanup, runtime-first revoke/delete, DSAR classification and
  independent rollout gates/runbook are implemented.
- Disconnect before the household runtime Machine exists uses a deterministic
  one-shot Fly revoker Machine: the pinned runtime image inherits the app secret,
  mounts no volume or service, and cleanup proceeds only after a trusted exit-0
  event proves Google revoke (HTTP 400 is treated as already revoked). Timeout,
  config drift, non-zero exit and unverifiable cleanup remain fail-closed.
- Cross-provider staging, backup/restore and policy evidence checks remain manual
  release gates and are not claimed by the automated suite.

#### Files

- Modify Phase 1 onboarding email templates/API snapshots.
- Modify runtime activation/bootstrap service.
- Modify DSAR/delete/export and operator runbooks.
- Add provider dashboards/alerts and end-to-end tests.

#### Changes

1. Replace fake cards with real typed states while preserving canonical order and
   recommended labels.
2. Require runtime inbound/outbound health receipt before `active` completion.
3. Add reconnect/change-address flows with fresh reauth and compensating cleanup.
4. Export provider metadata/receipts but never secrets; execute provider cleanup
   before final tombstone and reject late callbacks/webhooks after deletion.
5. Alert on stale Gmail cursor, Nerve webhook lag/DLQ, repeated auth revoke,
   address reservation leakage and unknown outcomes.
6. Roll out synthetic → operator accounts → invited pilot families per provider,
   with independent kill switches.

#### Acceptance

- All three paths complete/resume/reset/delete end-to-end in staging.
- Provider kill switch stops new connect/send while preserving safe inspect and
  cleanup.
- Backup/restore preserves identity state/cursors without restoring usable secret
  outside the dedicated secret namespace.
- Real Gmail remains disabled until policy evidence; real Nerve remains disabled
  until tenant/inbound/lifecycle contracts pass staging chaos tests.

## Testing Strategy

### Automated

- Unit: local-part/IDNA, state transitions, OAuth validation, HMAC/replay,
  Message-ID normalization and receipt taxonomy.
- Contract: one email identity scenario suite for fake, Nerve managed, Nerve BYO
  and Gmail; one send suite for SMTP-test/Nerve/Gmail backends.
- Integration: hermetic Nerve/Google fake servers with recorded response shapes;
  no secrets in cassettes.
- Chaos: SIGKILL before call, after provider commit, after response, during
  SecretSink, before durable result, before ACK and during cursor advance.
- Security: CSRF/IDOR/OAuth mix-up, tenant escape, DNS normalization/confusable,
  webhook forgery/replay, prompt-injection fixtures and secret/PII canaries.
- Privacy: export/delete/retention/classification completeness for every new
  table, provider object and attachment.

### Live opt-in staging

- Nerve: two synthetic household orgs on reserved test aliases; prove cross-tenant
  403/404, inbound, send, attachment and cleanup.
- Google: separate test project + allowlisted operator-created dedicated test
  Gmail; connect, receive, approve/send, revoke and delete.
- Fly: credential staged before Machine, bootstrap activation, restart and restore.

Live tests create exact resource registries and clean only recorded IDs. Unknown
cleanup is reported for operator reconciliation; no wildcard deletion.

## Performance and Capacity

Pilot targets, not public SLA:

- address availability p95 < 300 ms without provider call;
- Nerve provisioning/DNS status remains asynchronous; API never holds DB
  transaction across network call;
- Gmail poll default 60 s with jitter, adaptive backoff and per-user quota budget;
- webhook ACK after durable append < 2 s p95; body/attachments process async;
- raw inbound size and attachment count/bytes are bounded before parsing/fetch;
- one runtime owns one Gmail cursor and one writer lease;
- no email content/cardinality-bearing address in metrics labels.

## Migration and Rollout

1. Merge Phase 1 and freeze its provider/SecretSink contracts.
2. Implement and release Nerve tenancy/idempotency/lifecycle; pin SDK artifact.
3. Apply Abrolia `0002` and runtime `0006` with all real flags false.
4. Ship managed Nerve path to synthetic staging; then BYO DNS path.
5. Complete Nerve inbound plan and activate runtime Nerve smoke.
6. Ship Google OAuth/Gmail behind test-user allowlist.
7. Complete privacy/security evidence and Google verification/CASA.
8. Enable invited pilots provider-by-provider with independent kill switches.
9. Remove legacy personal Gmail/app-password production config only after no
   active deployment references it; keep migration rollback read-compatible.

Rollback never drops additive migrations. Old runtime keeps last active binding;
new connect/send is disabled, inspect/revoke/cleanup remains available. A provider
resource in `outcome_unknown` is reconciled before UI claims rollback complete.

## What We Are Not Doing

- Creating Google accounts for families or handling Google passwords/recovery.
- Connecting an existing personal Gmail or supporting IMAP/SMTP app passwords.
- Shared mailbox across households or a common Nerve tenant credential.
- Email-based primary chat interface; Telegram/WhatsApp/Web choice is Phase 3.
- Auto-sending without the existing approval/effect policy.
- Bulk mail, campaigns, cold outreach or training a general model on Gmail data.
- Gmail Pub/Sub push in pilot unless polling fails measured capacity targets.
- Full DNS hosting/registrar automation; family enters supplied records itself.
- Claiming Swiss/EU-only residency where Google/Nerve subprocessors do not prove it.

## Cross-Phase Dependencies and Handoff

Phase 1 implementation must hand Phase 2:

- actual `EmailIdentityProvisioner` and typed job/state contracts;
- household-scoped authorization/idempotency/reconcile;
- early Fly secret namespace and SecretSink;
- desired spec revision/bootstrap activation receipt;
- control-plane export/delete/tombstone hooks;
- real-provider gates defaulting false.

Phase 2 hands later phases:

- one active email binding independent of primary communication channel;
- runtime secret refs and provider health, not raw credentials;
- verified public agent address for onboarding summary;
- reset/delete hooks that later WhatsApp/channel phases must include in their
  downstream compensation graph.

## Success Criteria

Phase 2 is complete only when all are true:

- [ ] Exact `@abrolia.com` addresses are household-isolated in Nerve.
- [ ] Managed, Gmail and own-domain choices share one durable state contract.
- [ ] One-time secrets go directly to household secret namespace.
- [ ] Nerve inbound/attachments and lifecycle blockers are implemented/tested.
- [ ] Gmail OAuth uses dedicated account confirmation and minimum scopes.
- [ ] Gmail policy/CASA gates remain fail-closed until evidence is current.
- [ ] Nerve/Gmail ingress converge on one canonical RFC822 pipeline.
- [ ] Nerve/Gmail send preserve approvals, deterministic identity and unknown
      outcome safety.
- [ ] Resume/reset/disconnect/export/delete pass for every provider.
- [ ] Tenant escape, OAuth mix-up, webhook replay and prompt-injection tests pass.
- [ ] Synthetic and opt-in live staging suites pass without secret leakage.
- [ ] No production path requests a Gmail password/app password.

## References

### Arbolia repository

- Parent canon:
  `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md:47-60,216-252,283-305`
- Phase 1 contracts:
  `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md:220-278,580-640,740-910`
- Legacy Gmail poller: `hermes_cloud/ingest/gmail_poll.py:1-262`
- Canonical EML parser: `hermes_cloud/ingest/eml.py:1-341`
- Current send safety: `hermes_cloud/execute/email_send.py:1-192`
- Durable effects: `hermes_cloud/core/effects.py:1-240`
- Export/delete: `hermes_cloud/core/dsar.py:27-145`
- Current privacy map: `docs/privacy/data-map.md:1-118`

### Nerve repository

- Routes: `/Users/dmitrymolchanov/Programs/nerve-cloud/internal/cloudapi/handler.go:69-96`
- Domain provisioning:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/internal/cloudapi/handler_domains.go:175-737`
- Inbox lifecycle:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/internal/cloudapi/handler_inboxes.go:26-250`
- Tenant RLS:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/internal/store/migrations/core/0003_tenant_rls.sql:14-42`
- Domain uniqueness:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/internal/store/migrations/core/0008_org_domains.sql:31-55`
- Admin SDK:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/sdk/python/src/nerve_email/admin.py:32-179`
- Inbound prerequisite:
  `/Users/dmitrymolchanov/Programs/nerve-cloud/thoughts/shared/plans/2026-08-02-inbound-events-and-attachments.md`

### Google documentation verified 2026-08-04

- Workspace User Data and Developer Policy:
  https://developers.google.com/workspace/workspace-api-user-data-developer-policy
- Gmail OAuth scopes:
  https://developers.google.com/workspace/gmail/api/auth/scopes
- OAuth web-server flow:
  https://developers.google.com/identity/protocols/oauth2/web-server
- OAuth best practices:
  https://developers.google.com/identity/protocols/oauth2/resources/best-practices
- Gmail History:
  https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list
- Gmail send:
  https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send
- Gmail push/watch lifecycle:
  https://developers.google.com/workspace/gmail/api/guides/push
- OAuth verification requirements:
  https://support.google.com/cloud/answer/13464321?hl=en
- Restricted Scope security assessment/CASA:
  https://support.google.com/cloud/answer/13465431?hl=en

## Implementation Order Summary

```text
Phase 1 actual contract + early secret namespace
        |
        +--> Nerve root-domain tenancy/idempotency/lifecycle
        |         `--> managed @abrolia.com + BYO DNS
        |
        +--> provider-neutral runtime email core
        |         `--> Nerve inbound/send
        |
        `--> Google OAuth policy-gated flow
                  `--> Gmail History ingest/send
                            |
                    activation + lifecycle + pilot rollout
```
