-- Выполняется один раз при создании тома БД. Миграция apps.core 0001 тоже делает
-- CREATE EXTENSION IF NOT EXISTS, это страховка для ручных подключений.
CREATE EXTENSION IF NOT EXISTS vector;
