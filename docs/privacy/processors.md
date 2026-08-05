# Реестр процессоров и международных передач

> **Текущее фактическое состояние: ни один DPA не подписан, ни один трансферный
> механизм не оформлен.** Поэтому система работает только на синтетических
> данных. Всё, что ниже помечено ⏳, — это работа, а не достигнутое состояние;
> уведомления о конфиденциальности и README обязаны описывать именно это
> состояние, а не намерение (дефект, найденный ревизией Gate −1).

Статусы: ✅ подписано/подтверждено · ⏳ не оформлено, в работе · ❌ невозможно в
этом контуре · n/a неприменимо.

## 1. Реестр (ст. 30(1)(d), 30(2))

| # | Поставщик | Юрлицо / роль | Операция, в которой участвует | Данные | Локация | Ст. 28 (DPA) | Механизм передачи | TIA | Доп. меры | Уровень |
|---|---|---|---|---|---|---|---|---|---|---|
| P1 | **Anthropic** (Claude API, коммерческие условия) | TBD (US) — процессор | C1, C3, C5a: понимание содержимого | контент писем и сообщений в промптах, ответы | `global`/`us` | ⏳ | ⏳ SCC модуль 2 (C→P) | ⏳ | шифрование в транзите; отсутствие обучения на данных по договору; минимизация промпта | обязательный |
| P2 | **Fly.io** | TBD (US, инстансы в NL) — процессор | dedicated runtimes; metadata-only Abrolia control plane; shared-WA gateway; volumes, Machines, secrets | runtime S1–S3/S10; encrypted S14 account/onboarding/provisioning metadata; transient S15 | `ams` (NL, EU); provider control plane — US | ⏳ | ⏳ SCC модуль 2 + оценка доступа из US к control plane | ⏳ | disk encryption; field encryption/HMAC in S14; dedicated runtime isolation; separate secret namespaces | обязательный |
| P3 | **Nerve** (email control plane, инстанс того же владельца) | то же юрлицо, что и оператор → **не третье лицо**, а часть систем контролёра | C1, C2, C5/C5b: `@abrolia.com` и domain-family agent inbox (email options a/c) | письма, треды, вложения, provisioning refs | Fly `iad` (US) | внутренняя запись в ст. 30, DPA не требуется, но требуется документирование трансфера | ⏳ (внутригрупповая передача в US) | ⏳ | перенос инстанса в EU — заявленный upgrade-путь | обязательный для a/c |
| P4 | **Resend** | TBD (US) — субпроцессор P3 | C2: доставка исходящих из Nerve-managed inbox | адреса, тема, тело, логи доставки для email options a/c | US (метаданные — в US независимо от sending region) | ⏳ (через P3) | ⏳ SCC | ⏳ | минимизация тела; отказ от лишних метаданных невозможен — раскрывается семье | обязательный для a/c; Gmail option b использует Google API, не Resend |
| P5 | **Google** (Calendar; отдельный Gmail агента option b) | Google Ireland Ltd (EU) для consumer account — самостоятельный контролёр по своим terms; API-access выдаёт семья | C2 (calendar), C5a/C5b (agent inbox) | calendar events; dedicated agent Gmail messages; OAuth grant | глобально | n/a (не наш processor) | Google's own | n/a | `gmail.readonly` + `gmail.send`, one-time state/account confirmation, encrypted refresh token, revoke; verification/CASA fail-closed | обязателен для calendar; Gmail b — optional/gated |
| P6 | **Telegram** | самостоятельный оператор мессенджера | одна из primary-channel cards | сообщения и карточки | вне EU | ❌ (договора нет) | n/a | n/a | раскрытие в notice; verified binding; выбор за семьёй | опциональный, default |
| P7a | **WhatsApp / Meta — shared Abrolia number** | вне договорных отношений | Beta quick-start family dialogue | сообщения verified взрослых; sender binding | вне EU; gateway `ams` | ❌ | n/a | n/a | отдельный channel notice; exact unique sender routing; no external school/group contour | опциональный, real adapter disabled |
| P7b | **WhatsApp / Meta — dedicated Evolution** | вне договорных отношений | Beta full contour через linked-device отдельной SIM/eSIM | сообщения dedicated номера, session metadata | вне EU; Evolution `ams` | ❌ | n/a | n/a | отдельный informed-risk consent, per-instance key, disconnect | опциональный, real adapter disabled |
| P8 | **Провайдер object storage для бэкапов** | **TBD — назвать до реальных данных** | C8 | зашифрованные снапшоты БД | EU (требование) | ⏳ | n/a при хранении в EU | n/a | шифрование per-household ключом до выгрузки | обязательный |
| P9 | **Провайдер логов/алёртов** | **TBD — назвать до реальных данных** | C7 | логи без содержимого, метрики | EU (требование) | ⏳ | n/a при хранении в EU | n/a | запрет контента в логах — правило ревью | обязательный |
| P10 | **Vertex AI EU** (Google Cloud) | Google — процессор, только в режиме `eu-strict` | C1, C3 | промпты/ответы | EU | не активирован | n/a | n/a | — | upgrade-путь |
| P11 | **Web Push provider — TBD** | определить до включения push для real families | optional Abrolia Web notifications | endpoint/subscription metadata; уведомление без sensitive content | TBD | ⏳ | ⏳ | ⏳ | Phase 1 fake only; Web chat работает без push; fail-closed до записи в реестр | optional, disabled |

Изменение реестра — отдельным PR **до** первого вызова нового сервиса из кода
(правило в [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md)).

## 2. Что должно быть сделано до первой реальной семьи

1. Назвать юрлица P1, P2, P4, P8, P9 и адреса их представителей в ЕС; назвать
   P11 до включения real Web Push либо оставить push выключенным.
2. Подписать DPA (ст. 28(3)) с P1, P2, P4, P8, P9 (и P11, если включён); для P3 — внутренняя запись
   и документирование трансфера в US.
3. Оформить трансферный механизм: SCC модуль 2 (контролёр → процессор) для
   P1, P2, P4; зафиксировать перечень субпроцессоров и порядок уведомления о
   их смене.
4. Провести TIA по каждому трансферу в США (доступ властей, практика
   поставщика, эффективность доп. мер) и записать вывод.
5. Для Gmail option b пройти OAuth verification/CASA; до этого real adapter
   fail-closed, исторический IMAP seam не является обходом.
6. Shared/dedicated WhatsApp включать только после отдельных notice/risk
   receipts и market-specific eligibility/legal review; official Business
   Platform не считается автоматическим GA-путём.
7. Только после этого — заменить формулировки в notice/README на фактические.

Пока пункты 1–4 не закрыты, действует правило: **только синтетические данные**
— ящик владельца тоже реальный (см. [`dpia.md`](dpia.md), р. 5, и
[`lawful-bases.md`](lawful-bases.md), р. 3).

## 3. Residency: что мы обещаем и чего не обещаем

**Обещаем:** приложение, база и вложения — в EU (Fly `ams`); международные
передачи ограничены перечисленными выше поставщиками и задокументированы;
данные не используются для обучения моделей; изоляция per-household.

**Не обещаем:** «все данные обрабатываются только в EU». Сегодня это неверно:
first-party Claude API даёт `inference_geo` `global`/`us`; Nerve-инстанс — в
`iad`; Resend хранит метаданные в US; Google/Telegram/WhatsApp глобальны.
Формулировка «EU processing» без оговорок
из README и материалов исключена сознательно (Gate −1, п. 6).

**Также не обещаем**, пока не выполнен раздел 2, что передачи «покрыты
стандартными договорными условиями»: это состояние ещё не достигнуто.

**Конфигурация:**

| Режим | Смысл | Поведение |
|---|---|---|
| `eu-app` (пилот) | EU-hosted приложение + документированные международные передачи | по умолчанию |
| `eu-strict` | инференс тоже в EU | требует клиента Vertex AI EU; при его отсутствии процесс **падает на старте**, а не деградирует молча |

## 4. Критерии допуска нового поставщика

Любой новый поставщик получает данные не только семьи, но и третьих лиц из
писем (учителя, другие родители, дети). Критерии: наличие DPA, оформленный
механизм передачи, отсутствие использования данных для обучения/рекламы,
определённый срок хранения, возможность удаления по запросу, перечень
субпроцессоров и уведомление об их изменении.
