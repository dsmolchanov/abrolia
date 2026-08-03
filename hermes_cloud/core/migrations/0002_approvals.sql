-- Staged-подтверждения и порождённые ими артефакты Фазы 1.
-- Без BEGIN/COMMIT: транзакцией управляет Database.migrate().

-- Предложение, ожидающее подтверждения человека. Код хранится хэшем: утечка
-- базы не должна давать возможность подтвердить чужое действие.
CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    event_id     TEXT REFERENCES events (id) ON DELETE SET NULL,
    kind         TEXT NOT NULL,              -- reminder | ics | …
    payload      TEXT NOT NULL,              -- JSON предложения
    payload_sha  TEXT NOT NULL,              -- подтверждение привязано к payload
    code_sha     TEXT NOT NULL,              -- sha256 одноразового кода
    chat         TEXT NOT NULL,              -- точный чат происхождения
    thread       INTEGER,                    -- и точный тред
    context_key  TEXT,
    actor        TEXT NOT NULL,              -- кто инициировал предложение
    claimed_by   TEXT,                       -- кто подтвердил
    status       TEXT NOT NULL,              -- staged|claimed|executing|done|failed|cancelled|expired
    last_error   TEXT,
    receipt      TEXT,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS approvals_chat_status ON approvals (chat, status);
CREATE INDEX IF NOT EXISTS approvals_status_expires ON approvals (status, expires_at);

-- Неудачные попытки ввода кода: rate-limit per (actor, chat) из донора.
-- Без него код из 64 бит всё равно перебираем при бесконечных попытках.
CREATE TABLE IF NOT EXISTS approval_attempts (
    id           TEXT PRIMARY KEY,
    actor        TEXT NOT NULL,
    chat         TEXT NOT NULL,
    attempted_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS approval_attempts_actor_chat
    ON approval_attempts (actor, chat, attempted_at);

-- Напоминания: порт донорского ReminderStore поверх транзакционного слоя.
CREATE TABLE IF NOT EXISTS reminders (
    id           TEXT PRIMARY KEY,
    approval_id  TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    chat         TEXT NOT NULL,
    thread       INTEGER,
    text         TEXT NOT NULL,
    due_at       REAL NOT NULL,
    status       TEXT NOT NULL,              -- pending|delivering|done|cancelled
    lease_until  REAL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS reminders_status_due ON reminders (status, due_at);
