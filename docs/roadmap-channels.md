# Channel Roadmap: baseline v3, post-MVP каналы и голос

> Статус: обновлён под v3-решение владельца 2026-08-04. MVP onboarding идёт
> `email identity → WhatsApp identity → preferred communication channel`.
> Telegram рекомендован, WhatsApp Beta доступен после шага 2, минимальный
> authenticated Abrolia Web — третья primary-альтернатива; verified recovery
> email владельца остаётся fallback и никогда не совпадает с agent inbox.

## Контекст

Telegram в Западной/Южной Европе — не массовый канал: доминирует WhatsApp (примерно IT/ES ~90% пользователей мессенджеров, NL/DE ~80–85%, FR ~65%; Telegram — сильный второй в ES/IT и диаспорах). Ассистент, живущий только в Telegram, отрезает большинство местных семей.

Архитектурный инвариант, который делает добавление каналов дешёвым:
`RunContext + context_key + source channel` — единственный интерфейс транспорта
к ядру. Ответ на входящий ход остаётся в verified source channel; household
primary управляет proactive/digest. Permanent failure может дать короткую
ссылку на verified recovery email, но `outcome_unknown` никогда не дублируется.

## MVP baseline (не roadmap)

### WhatsApp identity: два Beta-режима

- **Shared Abrolia number** — quick start для verified взрослых и только
  семейного диалога. Exact sender binding идёт через узкий gateway без
  model/tools/household provider secrets; внешний школьный/group contour закрыт.
- **Dedicated family number** — рекомендуемый full contour: отдельная SIM/eSIM,
  QR linked-device, Evolution per household, per-instance apikey,
  per-household relay-HMAC и отдельный informed-risk consent.
- Оба режима Beta; все исходящие staged. Official WhatsApp Business — отдельный
  eligibility/legal workstream, не обещанный глобальный GA default.
- Сессии: в WhatsApp нет топиков — эквивалент это отдельные групповые чаты с ассистентом («Школа», «Дом», «Поездки») → каждый маппится на свой `context_key`; fallback — контекст-инференс в основном чате.
- Ограничение: номер должен пройти WhatsApp-регистрацию — VoIP-номера обычно отбраковываются (см. «Ловушка номеров» ниже).

### Primary communication cards

- **Telegram** — selected/recommended default, одноразовый deep link и семейная группа.
- **WhatsApp** — Beta, enabled только после verified step 2.
- **Abrolia Web** — authenticated chat/PWA с opt-in push, без полного vault.

Email не является primary-card. Recovery email account owner получает только
fallback notification; при Web без push — ссылку без sensitive content. Agent
mailbox из шага 1 запрещён как fallback, чтобы не создать self-ingestion loop.

## Post-MVP order

### MVP+1: Email как диалоговый канал

- У нас уже три **agent email identities** (`@abrolia.com`, separate agent Gmail
  OAuth, family domain). Post-MVP работа делает agent inbox полноценным
  диалоговым transport: письмо ассистенту = ход, ответ = compose в thread.
- Это не означает подключение личного Gmail, не превращает recovery fallback в
  agent inbox и не добавляет email в MVP primary cards.
- Охват 100%, нулевая инфраструктурная стоимость, «работает у любой бабушки»; сессия = email-thread → `context_key`.

### Privacy-тир: Signal

- Ниша по охвату, но наш ICP (privacy-aware технические семьи) на Signal оверindexed; «ассистент живёт в Signal» — сильный маркетинговый сигнал для privacy-позиционирования.
- Доступ неофициальный (`signal-cli` / libsignal) — те же оговорки informed consent, что и у WhatsApp-контура.

### GA: RCS Business Messaging

- Android-дефолт (Google Messages), официальный канал без ban-рисков — «зелёный» охват без WhatsApp.
- Через агрегаторов (Sinch / Infobip / Vonage); верификация бренда per-country — процесс долгий, поэтому только к GA.

### Не делаем

- **iMessage** — закрыт для ботов.
- **Facebook Messenger** — падающий охват, не наш ICP.
- **Viber** — только при экспансии в Грецию/Балканы/CEE.

## Голос (post-MVP): звонки для бронирований и простых операций

### Сценарий

Исходящие звонки от имени семьи: бронирование ресторана, запись к врачу/парикмахеру, уточнение у школы. Входящая «секретарская» линия — позже.

### Как получаем европейские номера

- **Провайдеры**: Telnyx (фаворит: дешевле, хорошее EU-покрытие, Voice API + SIP + media streaming), Twilio, Vonage, Bird (ex-MessageBird). Номер ≈ €1–3/мес + поминутная тарификация.
- **Регуляторный KYC per-country** («regulatory bundles»): геономера DE/IT/ES требуют подтверждение локального адреса и ID; онбординг страны — дни-недели. Планировать закупку номеров по странам заранее, до запуска голосовой фичи в рынке.
- **Модель закупки**: НЕ номер-на-household, а **один outbound-номер на страну рынка** — для исходящих звонков этого достаточно; household-идентичность передаётся содержанием звонка, не caller ID.

### Архитектура звонка

```
Telnyx/Twilio SIP → media stream → STT ⇄ модель ⇄ TTS (или realtime-voice API)
```

- Звонок инициируется только из staged-действия (тот же approval-контур: карточка «позвонить в ресторан X, забронировать на 4 в 19:00» → ✅).
- **Art. 50 AI Act (действует с 02.08.2026)**: ассистент обязан представиться как AI в начале звонка — фиксированная фраза в начале каждого сценария.
- Результат звонка — журналируемый effect (запись согласия на запись разговора — по праву страны; в DE по умолчанию нельзя без согласия обеих сторон).

### Ловушка номеров: голос ≠ WhatsApp

WhatsApp-регистрация отбраковывает большинство VoIP-номеров — Twilio/Telnyx-номер обычно **нельзя** спарить с WhatsApp. Следствие: это две разные закупки:

| Назначение | Тип номера | Источник |
|---|---|---|
| Голосовые звонки | VoIP, геономер страны | Telnyx/Twilio + regulatory bundle |
| WhatsApp-идентичность ассистента | реальный mobile/eSIM | eSIM-провайдеры или второй SIM семьи |

## Влияние на основной план

- MVP обязан закрепить `RunContext + context_key + source channel`, strict
  identity binding и household primary/fallback routing до подключения real adapters.
- Shared/dedicated WhatsApp и minimal Web уже входят в v3 MVP. Email-dialog,
  Signal, RCS и voice остаются отдельными post-MVP plans после pilot go-signals.
