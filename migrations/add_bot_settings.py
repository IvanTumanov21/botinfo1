"""
Миграция: добавление таблицы bot_settings для хранения настроек бота
"""
from sqlalchemy import create_engine, text
from loguru import logger
import os
from dotenv import load_dotenv

load_dotenv()

def run_migration():
    """Создаёт таблицу bot_settings"""
    DATABASE_URL = os.getenv("DATABASE_URL")
    engine = create_engine(DATABASE_URL)
    
    logger.info("🔄 Начинаем миграцию: добавление таблицы bot_settings")
    
    with engine.connect() as conn:
        # Создаём таблицу bot_settings
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                id SERIAL PRIMARY KEY,
                key VARCHAR(50) NOT NULL UNIQUE,
                value VARCHAR(200) NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        
        # Создаём индекс по ключу
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_bot_settings_key ON bot_settings(key)
        """))
        
        conn.commit()
        logger.info("✅ Таблица bot_settings создана")
    
    logger.info("✅ Миграция завершена успешно")

if __name__ == "__main__":
    run_migration()
