-- Операционная онтология: прогон извлечения, провенанс, обязательства, память.
-- Без BEGIN/COMMIT: транзакцией управляет Database.migrate().
--
-- Разделение, заданное планом («Модель данных»): журналы (`events`, `jobs`,
-- `effects`) остаются append-only и не переписываются; квалифицированные
-- утверждения — только там, где живут противоречия, то есть здесь.

-- Один прогон модели по одному событию. Нужен, чтобы у каждого утверждения был
-- проверяемый ответ на вопрос «откуда это взялось и чем именно получено».
CREATE TABLE IF NOT EXISTS extraction_runs (
    id            TEXT PRIMARY KEY,
    event_id      TEXT REFERENCES events (id) ON DELETE SET NULL,
    model         TEXT NOT NULL,
    prompt_sha    TEXT NOT NULL,              -- версия промпта: сравнимость прогонов
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS extraction_runs_event ON extraction_runs (event_id);

-- Провенанс, переживающий retention: метаданные и офсеты, **никогда цитаты**.
-- Цитата рендерится из живого `events.raw`; после его TTL остаётся проверяемый
-- след без содержимого третьих лиц (data map, р. 2).
CREATE TABLE IF NOT EXISTS evidence_refs (
    id                TEXT PRIMARY KEY,
    extraction_run_id TEXT NOT NULL REFERENCES extraction_runs (id) ON DELETE CASCADE,
    event_id          TEXT REFERENCES events (id) ON DELETE SET NULL,
    sender_domain     TEXT,                   -- домен, не адрес: адрес — персональные данные
    message_date      TEXT,
    content_sha       TEXT NOT NULL,          -- по нему видно, тот ли это текст
    span_start        INTEGER,                -- офсеты в тексте письма, не сам текст
    span_end          INTEGER,
    created_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS evidence_refs_run ON evidence_refs (extraction_run_id);
CREATE INDEX IF NOT EXISTS evidence_refs_event ON evidence_refs (event_id);

-- Обязательство семьи: тонкий связующий слой между извлечением и порождёнными
-- артефактами. Донорские сторы (`reminders`, позже todos/gcal) не заменяет —
-- ссылается на них.
--
-- Инварианты (план, Фаза 2, п. 3b) держатся кодом в `core/commitments.py`:
-- модель пишет только `candidate`; `confirmed` возникает исключительно через
-- подтверждение человека; `superseded` не удаляется — цепочка версий читается
-- по `supersedes`; `confidence` влияет только на рендер карточки.
CREATE TABLE IF NOT EXISTS commitments (
    id                TEXT PRIMARY KEY,
    extraction_run_id TEXT REFERENCES extraction_runs (id) ON DELETE SET NULL,
    kind              TEXT NOT NULL,          -- payment | event | task
    payload           TEXT NOT NULL,          -- JSON: суть обязательства
    status            TEXT NOT NULL,          -- candidate|confirmed|rejected|superseded
    supersedes        TEXT REFERENCES commitments (id) ON DELETE SET NULL,
    confidence        REAL,
    observed_at       REAL,                   -- когда факт случился по письму
    recorded_at       REAL NOT NULL,          -- когда мы о нём узнали
    approval_id       TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    reminder_id       TEXT REFERENCES reminders (id) ON DELETE SET NULL,
    calendar_event_id TEXT,                   -- детерминированный id события (Фаза 4)
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS commitments_status_kind ON commitments (status, kind);
CREATE INDEX IF NOT EXISTS commitments_supersedes ON commitments (supersedes);
CREATE INDEX IF NOT EXISTS commitments_run ON commitments (extraction_run_id);

-- Память семьи — тоже утверждения, а не свободный текст: у записи есть статус,
-- время наблюдения и цепочка версий. `valid_from/to` — только у routines и
-- preferences: у факта нет «срока действия», у распорядка есть.
CREATE TABLE IF NOT EXISTS memory_statements (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,          -- fact | routine | preference
    subject           TEXT,                   -- о ком/о чём
    text              TEXT NOT NULL,
    status            TEXT NOT NULL,          -- candidate|confirmed|rejected|superseded
    supersedes        TEXT REFERENCES memory_statements (id) ON DELETE SET NULL,
    extraction_run_id TEXT REFERENCES extraction_runs (id) ON DELETE SET NULL,
    actor             TEXT,                   -- кто предложил запись
    approval_id       TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    observed_at       REAL,
    recorded_at       REAL NOT NULL,
    valid_from        REAL,
    valid_to          REAL,
    updated_at        REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS memory_status_kind ON memory_statements (status, kind);
CREATE INDEX IF NOT EXISTS memory_supersedes ON memory_statements (supersedes);
