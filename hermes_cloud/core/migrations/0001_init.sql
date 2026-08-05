-- Gate 0 схемы: долговечный ingress и журнал эффектов.
-- Без BEGIN/COMMIT: транзакцией управляет Database.migrate().
--
-- Идентификаторы — UUID-текст (переносится в Postgres как uuid), времена —
-- epoch-секунды REAL (в Postgres станут timestamptz). Автоинкрементов и
-- rowid-зависимостей нет сознательно.

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,              -- inject | nerve | gmail | whatsapp | telegram
    -- Глобально уникальный ключ дедупликации. Вызывающий обязан
    -- пространствовать его каналом: `eml:<Message-ID>`, `nerve:<message_id>`.
    external_id  TEXT NOT NULL UNIQUE,
    context_key  TEXT,                       -- сессия: FIFO соблюдается внутри неё
    raw          BLOB NOT NULL,              -- исходный payload, TTL 30 дней
    received_at  REAL NOT NULL,
    status       TEXT NOT NULL,              -- received|processing|done|failed|dlq
    lease_until  REAL,
    leased_by    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS events_status_received_at
    ON events (status, received_at);
CREATE INDEX IF NOT EXISTS events_context_status
    ON events (context_key, status);

-- Отложенная работа, порождённая событием (напоминание, повтор, проверка).
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    event_id     TEXT REFERENCES events (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,              -- JSON
    run_at       REAL NOT NULL,
    status       TEXT NOT NULL,              -- pending|processing|done|failed|dlq
    lease_until  REAL,
    leased_by    TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_status_run_at ON jobs (status, run_at);

-- Журнал эффектов: один ход модели = run_id, один tool_use = tool_use_id.
-- Пара (run_id, tool_use_id) уникальна — это и есть идемпотентность:
-- повтор того же tool_use возвращает прежний результат, а не делает второй
-- эффект (Фаза 2, п. 2; таблица заводится здесь, чтобы схема была цельной).
CREATE TABLE IF NOT EXISTS effects (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    tool_use_id  TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_sha  TEXT NOT NULL,
    status       TEXT NOT NULL,              -- planned|executing|done|failed|outcome_unknown
    receipt      TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (run_id, tool_use_id)
);

CREATE INDEX IF NOT EXISTS effects_status ON effects (status);
