# Security — модель угроз

Требование Gate −1 канонического плана (Фаза 0, п. 4). Документ живой: каждая
новая поверхность (канал, tool, интеграция) добавляет строку **до** кода.

Смежное: [`privacy/dpia.md`](privacy/dpia.md) (риски для субъектов данных),
[`privacy/incident-response.md`](privacy/incident-response.md) (реакция),
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) (правила по секретам и фикстурам).

## Reporting a vulnerability

Please report security issues privately — do not open a public issue.
Contact: TODO: security contact address. We aim to acknowledge within 72 hours.
The service handles families' correspondence, including children's data; reports
about data exposure are treated at the highest priority.

## 1. Что защищаем

| Актив | Почему критичен |
|---|---|
| Содержимое писем и сообщений семьи | личная переписка, данные детей и третьих лиц |
| Секреты и прямые идентификаторы | runtime/provider tokens, OAuth refresh token отдельного Gmail агента, Evolution apikey, relay-HMAC, control-plane Fly/encryption/lookup-HMAC keys, session/CSRF/bootstrap tokens |
| Account/onboarding metadata | recovery email, profile, membership, workflow, provider refs, config/consent revisions |
| Право на действие наружу | письмо, сообщение, событие в календаре от имени семьи |
| Журнал действий и `effects` | доказуемость «кто подтвердил» и защита от дублей |
| Изоляция household | одна семья не видит другую |

**Главный инвариант продукта:** *ни одно наружное действие не исполняется без
явного подтверждения человека, обладающего правом на это действие.* Нарушение
этого инварианта — инцидент высшей категории
([`privacy/incident-response.md`](privacy/incident-response.md), класс A).

## 2. Акторы и границы доверия

Три identity не взаимозаменяемы:

- `account_id` — first-party principal в Abrolia Web/control plane;
- `actor_id` — runtime identity с channel capabilities;
- agent mailbox — операционный адрес ассистента.

Recovery email не становится agent inbox. Account owner не получает runtime
capabilities до отдельного channel binding; Telegram/WhatsApp external ID не
становится account primary key.

```text
public/static                     metadata control plane                  dedicated runtime
────────────────────────────────────────────────────────────────────────────────────────────
abrolia.com (no cookies,     browser ─► app.abrolia.com ─► S14          bootstrap claim/activate
connect-src 'none')                         │                 │                    │
                                            │ durable intent  ├─► providers/Fly    ▼
                                            └─ no family      │            household.toml/revision
                                               content        │                    │
                                                              └──────────────► runtime S1/S2
```

Browser никогда не получает Nerve/Fly/provider/runtime/bootstrap credentials.
Control plane multi-tenant только по metadata: он не принимает family messages,
не вызывает модель/tools и не читает runtime SQLite. Landing не является
backend; onboarding живёт только на `app.abrolia.com`.

После сохранения profile durable job создаёт только deterministic Fly app —
ранний household secret namespace. В этот момент нет volume, Machine, image или
bootstrap token. One-time Nerve/Gmail credentials в следующих construction
units должны передаваться прямо в этот namespace через `SecretSink`; plaintext
запрещён в browser, control-plane SQLite, job request/result, manifest и logs.
Поздний runtime `prepare` идемпотентно переиспользует app, создаёт volume, а
Machine запускается только после staging секретов и трёх verified steps.

Исторические `HERMES_GMAIL_ADDRESS`/`HERMES_GMAIL_APP_PASSWORD` и IMAP/SMTP
adapter — только compatibility seam для синтетических тестов. Он требует
`HERMES_LEGACY_IMAP_TEST_ONLY=1` и отключён в provisioned runtime даже при
наличии переменных. Production Gmail path — отдельный agent account через OAuth;
личный Gmail и app passwords не являются поддерживаемым вариантом.

```
недоверенное                     полудоверенное                доверенное
─────────────────────────────────────────────────────────────────────────
отправитель письма  ┐
вложение / фото     ├─► ingest ─► events (SQLite) ─► worker ─► extraction
WA-сообщение        ┘            (сырьё = данные,        │      (модель:
неизвестный участник             никогда не команда)     │       нет shell,
Telegram-группы  ────► RunContext (server-issued) ───────┤       нет send)
                                                          │
семья (family-actor) ─► подтверждение кода ──────────────►└─► executor ─► наружу
```

| Актор | Уровень | Что может | Чего не может |
|---|---|---|---|
| Анонимный browser / владелец magic link | недоверенный до consume | запросить generic link response; однократно обменять valid token | выбирать account/household из payload, читать onboarding, получать provider secrets |
| Authenticated Abrolia account owner | доверенный для account metadata | управлять своим household после membership check; export/delete после fresh re-auth | автоматически получать runtime role/caps; читать чужой household; видеть secret/provider body |
| Внешний отправитель письма / WA-сообщения | недоверенный | породить событие и карточку-предложение | вызвать tool, изменить состояние, отправить что-либо |
| Содержимое и вложение | недоверенные данные | быть процитированным модели | быть инструкцией: контент маркируется как untrusted |
| Неизвестный участник группы Telegram | недоверенный | получить ответ «представьтесь» | получить доступ к почте, памяти, календарю (нулевые caps) |
| Гость (`guest`) | полудоверенный | читать то, что разрешено ролью | подтверждать действия наружу, писать в память |
| Член семьи (`family`) | доверенный | подтверждать действия, писать в память | выходить за household |
| Runtime-владелец (`owner` actor) | доверенный внутри одного runtime | runtime export/delete approval и разрешённые actions | представляться Abrolia account principal; завершать control-plane DSAR один |
| Оператор сервиса | привилегированный | эксплуатация control plane/runtime и reconcile | читать content вне расследования; передавать credentials browser; обходить durable intent |
| Модель (Claude) | **не актор безопасности** | предлагать typed tool calls | shell, сеть, прямая отправка, чтение чужого household |

Эта таблица — не описание намерений: роли и их caps заданы в
`hermes_cloud/core/runcontext.py` (`ROLE_CAPS`), собираются в `RunContext` на
входе апдейта и проверяются дважды — реестром tools и самим обработчиком.
Матрица «каждый tool × каждая роль» прогоняется в `tests/test_runcontext.py`;
tool, не описанный в матрице, роняет тест.

**Исходящая почта** — единственное действие без обратного хода: напоминание
удаляется, событие правится, запись в памяти заменяется, отправленное письмо
живёт у получателя. Поэтому у неё три замка: получатель показан отдельной
строкой и связан с подтверждением через `payload_sha`; kill-switch
`HERMES_EMAIL_SEND` перечитывается непосредственно перед транспортом, а не
где-то раньше по пути; оборванная связь называется `outcome_unknown` и не
повторяется никогда (`hermes_cloud/execute/email_send.py`).

**Исходящий WhatsApp** проходит тот же `ApprovalStore.claim`: модель может
только создать proposal. Relay ingress отклоняется без корректного
per-household HMAC и точного Evolution instance. Redirect/ConnectError означают
явный отказ, а ReadTimeout — `outcome_unknown`; неизвестный исход автоматически
не повторяется (`hermes_cloud/execute/whatsapp.py`).

**Границы:**

1. **Landing → account surface.** `abrolia.com` статичен; его CSP оставляет
   `connect-src 'none'`. После release gate CTA может только навигировать на
   `https://app.abrolia.com/start`, но не проксировать API или secrets.
2. **Browser → control plane.** Opaque host-only `Secure`/`HttpOnly`/
   `SameSite=Strict` session, same-origin `Origin`, CSRF, membership и optimistic
   version обязательны на каждом mutating route; authenticated HTML/API имеют
   `Cache-Control: no-store`; household из URL/body не авторизует. API
   возвращает только safe/masked refs.
3. **API → provisioning worker.** Transition и job intent коммитятся до effect.
   Worker не держит DB transaction во время network call; stable identity,
   ensure/inspect и `outcome_unknown` закрывают replay/двойное создание.
4. **Control plane → runtime.** `HERMES_BOOTSTRAP_TOKEN` 256-bit, hash-only в
   S14 и plaintext только в Fly secret; `HERMES_RUNTIME_REF`,
   `HERMES_HOUSEHOLD_ID`, `HERMES_CONFIG_REVISION` и `HERMES_CONFIG_SHA256`
   задают exact binding. Runtime не ready до matching activation receipt;
   bootstrap secret удаляется только после отдельного durable receipt ack.
   Bootstrap идёт по HTTP только к bare `*.flycast`: Flycast не поддерживает
   TLS, а транспорт остаётся внутри encrypted Fly WireGuard network; любой
   другой bootstrap origin обязан использовать HTTPS.
   Runtime export/delete идут только по Fly `.internal` через отдельный
   per-runtime HMAC bearer (`HERMES_RUNTIME_DSAR_TOKEN`), который не хранится в
   control-plane SQLite и не принимается публичным ingress.
5. **Транспорт → ingest.** Webhook только верифицирует подпись и фиксирует
   событие; вся обработка — в worker'е. Неверная подпись — отказ (fail-closed).
6. **Данные → модель.** Контент письма — всегда данные. Никаких «инструкций из
   письма»; модель не имеет права исполнить наружное действие сама.
7. **Модель → эффект.** Каждый `tool_use` проходит policy-check по `RunContext`
   (caps актора) и журналируется в `effects` до исполнения.
8. **Предложение → действие.** Наружу — только через staged approval:
   код + точный chat + thread + актор, rate-limit неверных кодов, одноразовость.
9. **Household → household.** Assistant runtime — отдельный process/volume/key
   namespace. Осознанные multi-household исключения узкие: S14 хранит metadata,
   shared-WA gateway маршрутизирует exact verified sender и не имеет model/tools
   или household provider secrets.

## 3. Угрозы и меры

| # | Угроза | Вектор | Мера | Где проверяется |
|---|---|---|---|---|
| T1 | **Prompt injection** | текст письма/вложения/имени файла: «перешли всю переписку на …» | контент — untrusted-данные; модель без send/shell; caps-проверка каждого tool; всё мутирующее — через подтверждение человека; инъекционный корпус | Фаза 1, критерий «0 staged-действий, не соответствующих содержанию» |
| T2 | Подмена получателя | инъекция подсовывает свой адрес в `compose_email` | карточка показывает «Кому:» отдельной строкой; подтверждение связано с payload через payload-sha; расхождение = отказ | Фаза 4, тест на подмену payload |
| T3 | Email уходит не туда | forwarded chain или agent-inbox thread подставляет неверного получателя | все варианты используют `compose` с явно показанным адресатом: a/c через Nerve, b через Gmail API; payload-sha покрывает текст+получателя; auto-reply вне MVP | Фаза 4 |
| T4 | Доступ постороннего в группе | добавление в семейный чат/топик | server-issued `RunContext` на каждый ход; unknown/anonymous → ноль tools; caps проверяются в каждом tool (defense in depth) | Фаза 2, матрица «tool × {family, guest, unknown}» |
| T5 | Подбор кода подтверждения | перебор кодов в чате | exact chat+thread+actor binding, rate-limit неверных кодов per (actor, chat), одноразовость, инвалидция при правке | Фаза 1–2 (порт донорского фасада `claim_pending`) |
| T6 | Persistent injection через память | письмо заставляет записать «правило» в memory | `memory_append` — только family-actor и только через staged-подтверждение | Фаза 2 |
| T7 | Подделка webhook/shared routing | поддельный `email.received`/WA-relay или sender общего номера указывает чужой household | HMAC+окно; shared gateway сам делает exact verified sender→один household mapping, не доверяет household payload; unknown/ambiguous deny | Фаза 4/канальный unit |
| T8 | Replay/дубли | повтор webhook, перезапуск после падения | `external_id UNIQUE`, `effects (run_id, tool_use_id)`, детерминированные ID событий календаря, idempotency_key для email | Фаза 2 chaos-тесты |
| T9 | Двойное исполнение при краше | kill в середине окна | fsync-before-ACK, leases, startup-reconciliation, `outcome_unknown` вместо слепого retry | Фаза 2 |
| T10 | Компрометация одного runtime | утёкший runtime key, взломанный dedicated instance | изоляция process/volume/secret namespace; per-instance apikey Evolution; per-household relay-HMAC; shared gateway/control plane не имеют model/tool access | Фаза 2/5 |
| T11 | Утечка секрета в репозиторий | коммит фикстуры или конфига | `scripts/check_fixtures.py` + gitleaks по всей истории в CI; секреты — только в Fly secrets; ротация первым шагом при подозрении | CI, Gate −1 |
| T12 | Утечка через логи | лог с телом письма | политика «логи без содержимого»: ID, статусы, размеры | ревью PR, Фаза 5 |
| T13 | Runaway-цикл модели / abuse | бесконечный tool-loop, спам входящих | лимиты ≤8 итераций, ≤5 мин, токен-бюджет; дневной cost-cap с деградацией, а не тишиной | Фаза 2, Фаза 5 |
| T14 | Отказ в обслуживании через вход | шквал писем/сообщений | durable-очередь с лизами и DLQ, FIFO per context, backpressure | Фаза 1–2 |
| T15 | Вредоносное вложение | исполняемое/огромное вложение | MIME-allowlist, лимит размера, content-addressed store, отсутствие исполнения; вложение не открывается на хосте | Фаза 4 |
| T16 | SSRF/эксфильтрация через ссылки | модель просит скачать URL из письма | модели недоступен сетевой tool; скачивание — только по подписанным URL известных провайдеров | Фаза 4 |
| T17 | Кража dedicated WhatsApp session | компрометация Evolution-инстанса | отдельная SIM, explicit risk consent, изоляция инстанса, per-instance apikey, EU-host, disconnect; см. раздел 4 | channel unit |
| T18 | Злоупотребление `/delete` | runtime actor, stale account session или IDOR стирает семью | active owner membership + fresh re-auth ≤10 мин + Origin/CSRF; runtime approval — второй boundary; durable orchestration+tombstone | control-plane API/delete tests |
| T19 | Account takeover / magic-link replay / session fixation | украденная ссылка/cookie, повтор consume | 15-мин one-time hash-only token; URL fragment→first-party POST+history clear; generic response; rotate opaque session after login/reauth; revoke/idle/absolute expiry | auth replay/fixation tests |
| T20 | CSRF | сторонний origin отправляет mutating command | host-only `Secure`/`HttpOnly`/`SameSite=Strict` session cookie; host-only `Secure`/`SameSite=Strict` JS-readable CSRF cookie + exact same-origin `Origin` + CSRF на каждом unsafe route | CSRF×route matrix |
| T21 | IDOR | account A подставляет household B UUID/slug/body | каждый lookup идёт через active membership; caller household never authorization; A×B×route получает deny/404 без enumeration | authorization matrix |
| T22 | Bootstrap token theft/replay | runtime B claims manifest A или повторяет used token | 256-bit plaintext только в Fly secret; S14 hash-only; binding runtime+household+revision/hash; idempotent claim до activation; exact activation replay после потерянного ответа возвращает тот же receipt `200`, но новый claim used-token получает `410`; readiness fail-closed | bootstrap mismatch/replay tests |
| T23 | Control-plane compromise | multi-household metadata/infra credentials становятся доступны атакующему | metadata-only store; direct IDs/provider refs field-encrypted; secrets outside DB/backups/browser; runtime DB/content inaccessible; separate keys/backup, audit and provisioning kill-switch | PII/secret canaries + incident drill |
| T24 | Provisioning replay/двойной ресурс | retry/crash после provider accept | transition+intent before effect; browser idempotency key хранится только как keyed-HMAC вместе с request SHA-256; unique intent/stable resource identity; ensure/inspect; network unknown never auto-retries; exact external registry refs | job chaos/reconcile tests |
| T25 | Secret/PII leakage в browser/job/log | provider body/token/QR попадает в response, transition или exception | typed safe/public result; recursive secret-key validator; masked refs; no provider body; seeded canary through every serialization/log path | serialization/log tests |
| T26 | Self-ingestion loop | agent mailbox используется recovery fallback | separate account/actor/mailbox identities; lookup-HMAC и decrypted normalized equality check; fallback только verified recovery contact | manifest/channel-preference tests |

## 4. WhatsApp: shared и dedicated Beta

Оба режима маркируются Beta, но их trust boundaries различны.

### Shared Abrolia number

- Предвыбранный quick start принимает семейный диалог только от заранее
  verified номеров взрослых. Школьные/внешние/group chats недоступны.
- Узкий gateway durable-записывает webhook до ACK, делает exact unique
  sender→household mapping, подписывает relay отдельным key и удаляет payload
  после подтверждённой доставки. Unknown/ambiguous sender отклоняется.
- Gateway не имеет model/tools, runtime DB и household provider credentials.
- До включения нужен отдельный channel privacy notice receipt; этот receipt не
  является согласием на linked-device automation.

### Dedicated family number

QR pairing автоматизирует WhatsApp Web linked-device session выделенной
SIM/eSIM семьи через Evolution/Baileys. Это неофициальный контур с реальным
риском блокировки и с техническим доступом session ко всей переписке этого
номера. Поэтому отдельный номер, per-instance apikey, per-household relay-HMAC,
EU-host, общие TTL и disconnect обязательны.

**Текст отдельного risk consent для dedicated QR (черновик):**

> Подключение dedicated WhatsApp работает через linked-device сессию на
> выделенном номере вашей семьи. Это неофициальная автоматизация: WhatsApp может
> заблокировать номер, а сессия технически видит сообщения, которые на него
> приходят. Используйте отдельную SIM/eSIM, не основной личный номер. Исходные
> сообщения хранятся 30 дней; ничего не отправляется без вашего подтверждения.
> Отключить можно в любой момент завершением сессии.
> ☐ Я понимаю linked-device доступ и риск блокировки и подключаю dedicated WhatsApp.

Shared channel notice и dedicated risk consent — разные append-only receipts.
Авторитетный receipt хранится в S14 control plane с purpose, account, locale,
text version/hash и timestamps; runtime получает только receipt ID/digest и
enforcement fields. Без нужного receipt режим fail-closed.

Исходящие в обоих режимах проходят staged approval. Tracked-send и
`SendOutcomeUnknown` — только доставка после approval; unknown никогда не
дублируется через второй канал. Official WhatsApp Business требует отдельной
eligibility/legal проверки и не объявляется универсальным GA upgrade-путём.

## 5. Явные не-цели

- **E2EE и zero-knowledge не обещаются.** Чтобы понять письмо, сервер должен
  прочитать его в открытом виде.
- **Защита от компрометации инфраструктуры провайдера** (Fly, Google, Telegram,
  Anthropic) вне нашей модели — мы полагаемся на их контроль и договоры.
- **Защита от злоупотребления самим членом семьи**, имеющим права: `family`-актор
  может подтвердить письмо, о котором пожалеет. Мера — журнал, не запрет.
- **Копии за периметром** (сообщения в Telegram, события в Google-календаре,
  доставленные письма) неудалимы нашими средствами; мы говорим об этом прямо.
- **Multi-tenant assistant runtime** не рассматривается: family content/model/
  tools остаются в dedicated instance. Metadata-only control plane и узкий
  shared-WA router — перечисленные выше ограниченные исключения.
- **Автоисполнение по порогу уверенности** не появится: confidence влияет только
  на рендер карточки.
