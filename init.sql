-- Инициализация PostgreSQL
-- Создаётся автоматически при первом запуске контейнера

-- Расширения
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Индексы будут созданы через SQLAlchemy
-- Этот файл для дополнительных настроек если нужно

-- Вывод для лога
DO $$
BEGIN
    RAISE NOTICE '✅ PostgreSQL инициализирован для Breakout Bot';
END $$;
