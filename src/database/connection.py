"""
Подключение к PostgreSQL через SQLAlchemy
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

from src.config import DATABASE_URL

# Создаём движок
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # Проверка соединения перед использованием
    echo=False,
)

# Фабрика сессий
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db() -> Session:
    """
    Контекстный менеджер для работы с БД.
    Использование:
        with get_db() as db:
            db.query(...)
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Создаёт все таблицы в БД"""
    from .models import Base
    Base.metadata.create_all(bind=engine)
    print("✅ База данных инициализирована")
