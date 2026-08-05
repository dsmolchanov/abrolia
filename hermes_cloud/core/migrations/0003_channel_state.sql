-- Состояние канала: offset апдейтов Telegram и подобные курсоры.
-- Без BEGIN/COMMIT: транзакцией управляет Database.migrate().
--
-- Хранится в базе, а не в памяти, чтобы перезапуск не переигрывал уже
-- обработанные нажатия. Повторная обработка безопасна (код одноразовый), но
-- «безопасно» и «правильно» — разные вещи.
CREATE TABLE IF NOT EXISTS channel_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
