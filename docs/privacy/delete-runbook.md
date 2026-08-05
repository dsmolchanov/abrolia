# Runbook удаления household'а

Порядок действий по запросу на стирание (ст. 17 GDPR). Срок ответа заявителю —
**1 месяц** со дня запроса (ст. 12(3)).

Документ отвечает на вопрос, который важнее самого удаления: **что мы удалить
не можем и почему**. Обещать полное исчезновение данных, часть которых лежит в
телефонах членов семьи и в логах провайдеров, — значит обмануть.

## 0. Проверка заявителя

- Account-flow принимается только от authenticated Abrolia account с active
  `owner` membership. Household выводится из server-side session membership;
  UUID/slug/body/query — только routing hints и никогда не источник права.
- Перед destructive command нужна fresh passwordless re-auth не старше 10 минут,
  `Secure`/`HttpOnly` session, same-origin `Origin`, CSRF и `If-Match`. Ошибка
  авторизации не раскрывает существование чужого household.
- Runtime `owner` actor и его одноразовый approval остаются дополнительным
  подтверждением удаления family content, но не заменяют account principal.
  `hermes-cloud delete` сам по себе стирает только dedicated runtime и не может
  объявить полный DSAR delete.
- Если запрос пришёл от adult без owner membership или от channel actor без
  account re-auth, — отказ с объяснением. Стирание затрагивает всех членов.
- До первого side effect control plane durable-записывает deletion intent,
  отзывает новые commands и создаёт compensating jobs. `outcome_unknown` требует
  inspect/reconcile: слово «удалено» не используется, пока результат не доказан.

## 1. В control plane и dedicated runtime (автоматически)

Control plane сначала:

1. переводит household/account в `deleting`, блокирует новые transitions,
   provisioning jobs, bootstrap claim и provider callbacks;
2. отзывает sessions, invite/login/reauth tokens и активные bootstrap tokens;
3. отменяет pending jobs; running/`outcome_unknown` jobs останавливает только
   после provider inspect/reconcile;
4. по private Fly `.internal` DSAR endpoint с exact per-runtime HMAC bearer
   запрашивает runtime export (если владелец выбрал его), затем runtime wipe;
5. deprovision'ит runtime по exact registry IDs, без wildcard/prefix cleanup.

Подтверждённый runtime wipe durable-записывается до Fly deprovision. Если
удаление app/volume/Machine затем получает `outcome_unknown`, повтор orchestration
не требует ответа от уже недоступного runtime и опирается на сохранённую receipt.

`hermes-cloud delete` после подтверждения выполняет `wipe_household()`:

1. `evidence_refs`, `extraction_runs` — провенанс;
2. `effects` — журнал эффектов;
3. `commitments`, `memory_statements` — утверждения и память;
4. `reminders`, `approval_attempts`, `approvals` — артефакты и подтверждения;
5. `jobs`, `events` — очередь и сырьё;
6. `channel_state` — курсоры каналов;
7. **надгробие** `household_deleted_at` — единственная оставшаяся запись.

Надгробие обязательно: отложенный вебхук, пришедший после стирания, обязан
получить отказ (`HouseholdDeleted`), а не создать событие и вместе с ним новый
household «из ниоткуда».

После подтверждённого runtime/provider cleanup control plane удаляет exportable
account/profile/membership/workflow/job/resource/config fields и ciphertext,
сохраняет только DSAR/consent exceptions и `deletion_tombstones`: keyed-HMAC
household ID, completion status, `deleted_at`/`expires_at` на 3 года. Raw
household ID и family content в tombstone отсутствуют. Delayed job/callback/
bootstrap проверяет tombstone до любого write.

Проверка: `tests/test_dsar.py::test_wipe_removes_everything_and_leaves_a_tombstone`
и `::test_a_deleted_household_cannot_be_resurrected`.

## 2. Внешние поверхности (вручную, по порядку)

| # | Поверхность | Что делаем | Что остаётся и на сколько |
|---|---|---|---|
| 1 | Nerve org/inbox | удалить inbox и org household'а, отозвать runtime-ключ | письма в исходящем журнале Nerve — по его retention |
| 2 | Resend | запросить удаление; логи доставки | метаданные доставки у Resend — по их политике (US) |
| 3 | Google | удалить события ассистента; для email-опции b отозвать Gmail OAuth grant отдельного agent account и удалить refresh token из exact household Fly secret namespace | события/письма, которые семья или получатели сохранили, остаются у них |
| 4 | Telegram | удалить сообщения бота, где это возможно; удалить бота household'а | копии в чатах у членов семьи и их собеседников — **вне нашего контроля**, сообщается заявителю прямо |
| 5 | Shared WhatsApp gateway | удалить sender→household binding, relay key и queued payload; delayed webhook получает tombstone deny | копии в WhatsApp у участников — вне нашего контроля; technical gateway metadata — до 90 дней по pilot policy |
| 6 | Dedicated Evolution (WhatsApp) | logout linked-device session, удалить instance, apikey и relay-HMAC | сообщения на телефонах участников — вне нашего контроля |
| 7 | Fly runtime resources/secrets | удалить Machine, volume, app и runtime namespace по exact registry refs; удалить Telegram/Nerve/OAuth/Evolution/relay secrets | `outcome_unknown` остаётся открытым DSAR step до reconcile |
| 8 | Control-plane secrets/data (S14) | удалить per-household ciphertext/provider refs/configs; platform-wide encryption/HMAC/Fly keys не удаляются вместе с одним household | consent/DSAR/tombstone exceptions — раздел 3 |
| 9 | Бэкапы (S4) | исключить runtime и logical S14 household records из ротации | копии исчезают в пределах окна **30 дней**; срок сообщается заявителю |
| 10 | Anthropic | отдельного удаления не требуется: промпты отдельно у нас не хранятся; у провайдера — по коммерческим условиям | по условиям Anthropic |
| 11 | Логи и метрики (S12) | ничего не делаем вручную | исчезают по TTL **30 дней**; содержания не несут |
| 12 | Legacy/operator records (S13) | удалить legacy config/DNS/escalation contact после сверки миграции | DSAR и incident evidence сохраняются по accountability policy |

## 3. Что сохраняется намеренно

- **Запись о самом запросе** (кто, когда, что сделано) — 3 года: без неё
  невозможно доказать, что запрос исполнен.
- **Consent receipt** — purpose, actor account, locale, text version/hash,
  accepted/revoked timestamps на 3 года; лишнее содержание/PII стирается.
- **Deletion tombstone** — keyed-HMAC household ID и completion status на 3 года,
  чтобы delayed callback не воскресил удалённый household.
- **Реестр инцидентов** — если household упоминался в инциденте, запись
  остаётся: это обязательная документация, а не наши данные.

Эти исключения перечисляются заявителю в ответе, а не умалчиваются.

## 4. Ответ заявителю

Шаблон обязан содержать: отдельно control-plane и runtime status; что удалено на
внешних поверхностях; каждый partial/unknown result; что остаётся и на какой
срок (бэкапы — 30 дней, логи — 30 дней, technical job metadata — до 90 дней,
consent/DSAR/tombstone — 3 года); что вне нашего контроля (копии в
мессенджерах, Gmail/Calendar/получатели, данные у провайдеров); контакт для
жалобы. Ответ `complete` запрещён, пока один обязательный cleanup unknown.

## 5. Экспорт перед удалением

Владельцу предлагается сделать account-level export в `app.abrolia.com` **до**
стирания. Control plane объединяет exportable account/onboarding records с
runtime export; token/session/bootstrap hashes и secrets исключены. Legacy
`hermes-cloud export` остаётся runtime-only диагностикой. Выгрузка отдаётся
файлом после fresh re-auth, а не текстом в чате.

Сроки S14 в этом runbook — configurable synthetic-pilot policy из
[`data-map.md`](data-map.md), ещё не production-обещание; до real-family gate их
подтверждают владелец и юрист.
