# DPIA (черновик)

Оценка воздействия на защиту данных, ст. 35 GDPR. Черновик инженера; финализация
с юристом — до подключения первой реальной семьи.

**Статус: DPIA считается обязательной, а не добровольной.** Совпадают минимум
три критерия WP248: обработка данных **уязвимых субъектов** (дети),
**данных субъектов, не являющихся пользователями** (учителя, другие родители),
и **инновационное применение технологии** (LLM к личной переписке); к этому
добавляется обработка **особых категорий** (здоровье, религия в письмах школ).
По методике EDPB двух критериев обычно достаточно, чтобы обработка считалась
высокорисковой и требовала DPIA (ст. 35(1),(3)). Ранее этот документ называл
DPIA добровольной — формулировка снята ревизией Gate −1.

Осталось проверить юристу: национальный список надзорного органа по месту
учреждения контролёра (ст. 35(4)) и необходимость предварительной консультации
по ст. 36, если остаточный риск останется высоким.

## 1. Описание обработки

- **Что:** invite-only Abrolia account → metadata-only onboarding
  `email identity → WhatsApp identity → preferred channel` → dedicated runtime;
  затем входящее письмо/фото/голос/сообщение → извлечение обязательства LLM →
  карточка → после подтверждения человеком — календарь, задача, напоминание или
  исходящее письмо.
- **Кто:** оператор (контролёр), семья (пользователи), третьи лица в контенте.
- **Масштаб:** пилот 5–20 семей; multi-household control plane хранит только
  account/onboarding/provisioning metadata, assistant runtime остаётся
  физически dedicated per household.
- **Технологии:** Claude API (без обучения на данных), отдельные SQLite на EU
  volumes для control plane и runtime, Nerve (`@abrolia.com`/домен семьи),
  отдельный Gmail агента через OAuth/Gmail API, shared/dedicated WhatsApp,
  Telegram и минимальный authenticated Abrolia Web.
- **Правовые основания:** [`lawful-bases.md`](lawful-bases.md).
- **Данные и сроки:** [`data-map.md`](data-map.md).

## 2. Необходимость и соразмерность

| Вопрос | Ответ |
|---|---|
| Достигается ли цель менее интрузивно? | Нет: чтобы понять переданное ассистенту письмо, его нужно прочитать. Уменьшаем объём **хранения** (сырьё — 30 дней) и исключаем доступ к личному Gmail: OAuth подключает только отдельный agent account; варианты Nerve получают только письма agent inbox |
| Минимизация | Control plane не хранит family content; direct identifiers/provider refs шифруются, lookup — keyed HMAC; runtime извлекает операционный минимум; логи без содержимого; нет обогащения/межсемейной аналитики |
| Контроль пользователя | Ничего наружу без явного подтверждения; память — staged; каждый channel binding проверяется; OAuth grant/WhatsApp disconnect отзываются; единый `/export`/`/delete` охватывает account metadata и runtime |
| Хранение | Retention-матрица + ежедневная джоба; бэкапы — окно 30 дней |
| Точность | Извлечение проверяется человеком в карточке; порог confidence влияет только на рендер, не на исполнение |

## 3. Риски и меры

| # | Риск | Кто страдает | Оценка без мер | Меры | Остаточный риск |
|---|---|---|---|---|---|
| R1 | Ассистент выполняет действие наружу, которого человек не хотел (галлюцинация, ошибка извлечения) | семья, получатель | высокий | staged approval на каждое наружное действие; карточка показывает получателя отдельной строкой; payload-sha связывает подтверждение с фактическим payload; kill-switch | низкий |
| R2 | **Prompt injection** из содержимого письма/вложения → действие или утечка | семья, третьи лица | высокий | контент помечен как untrusted; модель без shell и без прямой отправки; каждый tool проверяет caps; всё мутирующее — через подтверждение; инъекционный корпус в тестах (Фаза 1) | средний → низкий |
| R3 | Доступ постороннего через Telegram/WhatsApp/Web binding | семья | высокий | одноразовый owner-authorized challenge; actor/household вычисляются сервером; `RunContext` на каждый ход; unknown actor = ноль tools; Web требует authenticated session | низкий |
| R4 | Утечка одной семьи в другую через metadata control plane или shared WhatsApp gateway | семьи | высокий | membership-scoped query на каждом API route; caller-supplied household не авторизует; dedicated runtimes/volumes/keys; shared sender exact-mapped к одному household, unknown/ambiguous deny; cross-household tests | низкий |
| R5 | Данные третьих лиц (учителя, другие родители) обрабатываются без их ведома | третьи лица | средний | LIA, TTL 30 дней на сырьё, отсутствие вторичного использования, публичный notice, право возражения | средний (принят) |
| R6 | Данные детей в переписке | дети | средний | [`minors.md`](minors.md): минимизация, отсутствие профилей детей, TTL, запрет функций «по ребёнку» | средний → низкий |
| R7 | Особые категории (здоровье, религия) в письмах | третьи лица, дети | высокий | не извлекаем и не индексируем такие атрибуты; короткий TTL; до выбора email identity семья принимает versioned запрет на отправку таких материалов и при ошибке запрашивает удаление — **но это меры, а не условие ст. 9(2)** | **высокий, блокирующий**: acknowledgement не переносит ответственность и не предотвращает обработку случайно доставленного материала; до выбора условия ст. 9(2) реальные данные не подключаются ([`lawful-bases.md`](lawful-bases.md), р. 3) |
| R8 | Международные передачи (US) | все | средний | DPA/SCC/TIA в [`processors.md`](processors.md); честная формулировка residency; `eu-strict` как fail-closed upgrade | средний (раскрыт) |
| R9 | Компрометация runtime/control-plane secrets | семья, все pilot households | высокий | раздельные Fly secret namespaces; field-encryption и lookup-HMAC keys раздельны; browser/job JSON/DB/log никогда не получают Fly/provider/bootstrap plaintext; gitleaks и ротация | низкий → средний для control-plane blast radius |
| R10 | Потеря данных | семья | средний | SQLite WAL + fsync-before-ACK, бэкапы с тестом восстановления | низкий |
| R11a | **Shared WhatsApp:** sender ошибочно маршрутизирован в чужой household; общий номер создаёт ложное ожидание внешнего школьного контура | семьи | высокий | только заранее verified номера взрослых; exact/unique sender binding; unknown/ambiguous deny; семейный диалог only; отдельный channel notice; durable ingress удаляется после подтверждённой relay-доставки | низкий → средний |
| R11b | **Dedicated WhatsApp:** блокировка выделенного номера и доступ linked-device сессии ко всей его переписке | семья, контакты номера | высокий | отдельный risk consent до QR; рекомендация отдельной SIM/eSIM; per-instance keys; общий TTL; staged approval на любую отправку; disconnect | средний (принят сознательно) |
| R12 | Dedicated Gmail OAuth связан не с тем аккаунтом, grant украден или scopes шире необходимого | семья и корреспонденты agent inbox | высокий | только отдельный Gmail агента; `state` one-time и связан с owner-session+household; account chooser + повторное подтверждение адреса; `gmail.readonly` + `gmail.send`; refresh token envelope-encrypted; disconnect revoke; verification/CASA fail-closed | средний → низкий после gate |
| R13 | Ошибочно широкий или неполный `/delete` между control plane, runtime и providers | семья | высокий | fresh account re-auth, membership/CSRF/Origin; durable delete intent; runtime+control-plane orchestration; keyed-HMAC tombstone; partial/`outcome_unknown` не выдаётся за complete; честное сообщение о внешних копиях | средний (раскрыт) |
| R14 | Runaway-цикл модели → расход и лишняя обработка | семья | низкий | лимиты итераций/времени/токенов, дневной cost-cap с деградацией | низкий |
| R15 | Account takeover, magic-link replay, session fixation/CSRF | account owner, household | высокий | 15-минутный one-time hash-only link; fragment→first-party POST; opaque hash-only session, rotation, Secure/HttpOnly/SameSite cookie; Origin+CSRF; destructive fresh re-auth | низкий |
| R16 | IDOR через household UUID/slug/API body | семьи | высокий | household всегда выводится из session membership; UUID/slug только routing hint; A×B×route deny/404 matrix | низкий |
| R17 | Bootstrap/provisioning replay создаёт чужой или второй runtime | семьи, оператор | высокий | durable intent до effect; stable resource identity/ensure+inspect; token bound to runtime+household+revision/hash, hash-only/one-time; `outcome_unknown` без blind retry; activation receipt | низкий |
| R18 | Компрометация metadata control plane | все pilot accounts | высокий | он не принимает family messages и не читает runtime SQLite; encrypted direct IDs/provider refs; least-privilege deploy; separate backup/key; incident response и global provisioning kill-switch | средний |

## 4. Отдельный Gmail агента через OAuth (email-опция b)

Abrolia не подключает существующий личный Gmail и не запрашивает пароль. Если
семья выбирает Google, она сначала создаёт **отдельный Google-аккаунт для
ассистента**, затем возвращается в onboarding и связывает именно его.

**Как ограничиваем:**

1. **Отдельная identity.** Recovery/contact email Abrolia-account и agent Gmail
   — разные адреса. Service валидирует, что agent inbox не совпадает с recovery
   fallback, чтобы исключить self-ingestion loop.
2. **OAuth binding.** One-time `state` связан одновременно с owner session и
   household. Google account chooser всегда показывается; после callback UI
   ещё раз показывает выбранный адрес и требует явного подтверждения.
3. **Минимальные scopes.** Только `gmail.readonly` и `gmail.send`: без Drive,
   contacts, settings и удаления писем. Ингест идёт через Gmail history/cursor,
   исходящие — compose после staged approval.
4. **Секрет.** Refresh token передаётся напрямую в ранний household Fly secret
   namespace через `SecretSink`, отсутствует в browser/control-plane DB/API/job
   JSON/runtime manifest/logs и недоступен модели. Disconnect отзывает grant и
   удаляет token material.
5. **Limited Use.** Контекстное disclosure показывается непосредственно перед
   OAuth. Gmail user data используется только для работы и улучшения
   пользовательской email-функции, без рекламы, продажи, credit decisions или
   общего обучения моделей; human access ограничен разрешёнными политикой
   support/security/legal/operations случаями.
6. **Launch gate.** Gmail restricted scopes требуют OAuth verification и CASA.
   До их закрытия real adapter fail-closed; Phase 1 использует только `.test`
   fake. Исторический IMAP poller остаётся внутренним synthetic test seam и не
   является пользовательским или production-путём.

**Позиция:** default — `@abrolia.com`; dedicated Gmail — второй выбор; домен
семьи — третий. Личный Google-адрес не предлагается ни как shortcut, ни как
fallback.

## 5. Вывод

**Для синтетических данных** остаточный риск приемлем при реализованных мерах.

**Данные владельца-теста — реальные данные.** Его семейная переписка содержит
тех же учителей, других родителей и детей, поэтому она подпадает под те же
предпосылки, что и данные клиентов (в первую очередь — условие ст. 9(2)), плюс
завершённую trust foundation. Свой dedicated agent inbox не является
облегчённым режимом.

**Для реальных семей — нет**, пока не закрыты все условия ниже; это не
пожелания, а предпосылки:

1. Выбрано и задокументировано условие ст. 9(2) для особых категорий у детей и
   третьих лиц (R7) — без него обработка запрещена ст. 9(1).
2. Завершены runtime trust foundation и control-plane account/session,
   provisioning, retention, export/delete и backup/restore controls — до этого
   не подключается ни один реальный inbox или channel adapter.
3. Подписаны DPA и оформлены трансферные механизмы с процессорами, которым это
   применимо: P1, P2, P4, P8, P9. P3 — наша же система (внутренняя запись в
   ст. 30 и документированная передача в US, отдельный DPA не требуется), P5 —
   самостоятельный контролёр по своим условиям, доступ выдаёт семья
   ([`processors.md`](processors.md)). Уведомления описывают фактическое
   состояние, а не намерение.
4. Gmail OAuth verification/CASA закрыты до включения dedicated Gmail real path.
5. Shared WhatsApp получает отдельный channel notice receipt; dedicated QR —
   дополнительный informed-risk receipt. Все исходящие — staged approval.
6. Заполнены реквизиты контролёра, адрес для запросов субъектов и надзорный
   орган; notice содержит полный набор ст. 13/14.
7. DPIA проверена юристом на предмет ст. 35(4) и необходимости консультации по
   ст. 36.

Пересмотр DPIA: при смене модели/провайдера, добавлении канала, выходе за 20
семей, любом инциденте категории «утечка».
