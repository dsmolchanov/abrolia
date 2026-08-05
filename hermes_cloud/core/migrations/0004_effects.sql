-- Журнал эффектов доводится до рабочего вида Фазы 2.
-- Без BEGIN/COMMIT: транзакцией управляет Database.migrate().
--
-- В 0001 таблица заведена наперёд, чтобы схема была цельной, и оказалась
-- слишком узкой: нет связи с подтверждением, нет аренды, нет счётчика попыток,
-- а `payload_sha` объявлен обязательным — хотя у эффекта, порождённого
-- подтверждением, хэш payload'а берётся из самого подтверждения и может
-- отсутствовать. Рядов в ней нет ни в одной установке (её никто не писал), но
-- переносим их всё равно: миграция, которая молча теряет данные, однажды
-- потеряет их не молча.
--
-- Смысл ключа не меняется: `(run_id, tool_use_id)` уникальна, и это и есть
-- идемпотентность — повтор того же `tool_use_id` возвращает прежний результат.

ALTER TABLE effects RENAME TO effects_v1;
DROP INDEX IF EXISTS effects_status;

CREATE TABLE effects (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,              -- ход: id подтверждения или id прогона модели
    tool_use_id  TEXT NOT NULL,              -- ключ идемпотентности внутри хода
    kind         TEXT NOT NULL,              -- reminder | ics | email | …
    approval_id  TEXT REFERENCES approvals (id) ON DELETE SET NULL,
    payload_sha  TEXT,                       -- что именно исполнялось
    status       TEXT NOT NULL,              -- pending|done|failed|outcome_unknown
    result       TEXT,                       -- результат для повторной выдачи
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    lease_until  REAL,                       -- аренда исполнителя
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);

INSERT INTO effects (
    id, run_id, tool_use_id, kind, approval_id, payload_sha,
    status, result, error, attempts, lease_until, created_at, updated_at
)
SELECT
    id, run_id, tool_use_id, kind, NULL, payload_sha,
    CASE status WHEN 'planned' THEN 'pending' WHEN 'executing' THEN 'pending' ELSE status END,
    receipt, NULL, 0, NULL, created_at, updated_at
FROM effects_v1;

DROP TABLE effects_v1;

-- Ровно один эффект на (ход, tool_use_id).
CREATE UNIQUE INDEX IF NOT EXISTS effects_run_tool
    ON effects (run_id, tool_use_id);

CREATE INDEX IF NOT EXISTS effects_status_lease ON effects (status, lease_until);
CREATE INDEX IF NOT EXISTS effects_approval ON effects (approval_id);
