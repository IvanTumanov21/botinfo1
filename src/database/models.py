"""
SQLAlchemy модели для PostgreSQL
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, 
    Text, Enum, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


class SignalStatus(enum.Enum):
    """Статусы сигнала"""
    PENDING = "pending"       # Ожидает решения
    ACCEPTED = "accepted"     # Принят (нажали Купить)
    REJECTED = "rejected"     # Отклонён (нажали Пропустить)
    EXPIRED = "expired"       # Истёк (не приняли решение)
    EXECUTED = "executed"     # Исполнен (ордер создан)


class PositionStatus(enum.Enum):
    """Статусы позиции"""
    OPEN = "open"
    PARTIAL_TP1 = "partial_tp1"   # Закрыли 30% на TP1
    PARTIAL_TP2 = "partial_tp2"   # Закрыли ещё 30% на TP2
    CLOSED_TP = "closed_tp"       # Закрыта по TP
    CLOSED_SL = "closed_sl"       # Закрыта по SL
    CLOSED_MANUAL = "closed_manual"  # Закрыта вручную


class Signal(Base):
    """Сигналы на покупку"""
    __tablename__ = "signals"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Основные данные
    symbol = Column(String(20), nullable=False, index=True)
    price = Column(Float, nullable=False)
    
    # Метрики сигнала
    candle_growth_pct = Column(Float)        # Рост свечи %
    volume_ratio = Column(Float)             # Объём / SMA20
    spread_pct = Column(Float)               # Спред %
    rsi = Column(Float)                      # RSI
    
    # Структура EMA
    ema7 = Column(Float)
    ema14 = Column(Float)
    ema28 = Column(Float)
    ema100 = Column(Float)
    
    # Уровни
    entry_price = Column(Float)
    stop_loss = Column(Float)
    tp1 = Column(Float)
    tp2 = Column(Float)
    tp3 = Column(Float)
    
    # Накопление
    accumulation_detected = Column(Boolean, default=False)
    accumulation_range = Column(Float)       # Диапазон накопления
    
    # Статус и время
    status = Column(Enum(SignalStatus), default=SignalStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    decided_at = Column(DateTime, nullable=True)
    
    # Telegram
    message_id = Column(Integer, nullable=True)
    
    # Связь с позицией
    position = relationship("Position", back_populates="signal", uselist=False)
    
    __table_args__ = (
        Index("idx_signal_symbol_created", "symbol", "created_at"),
    )


class Position(Base):
    """Открытые и закрытые позиции"""
    __tablename__ = "positions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    
    # Основные данные
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), default="BUY")
    
    # Вход
    entry_price = Column(Float, nullable=False)
    entry_amount = Column(Float, nullable=False)      # Количество монет
    entry_value_usdt = Column(Float, nullable=False)  # В USDT
    entry_time = Column(DateTime, default=datetime.utcnow)
    
    # Текущее состояние
    current_amount = Column(Float)            # Оставшееся количество
    realized_pnl = Column(Float, default=0)   # Зафиксированный P&L
    
    # Уровни
    stop_loss = Column(Float)
    tp1 = Column(Float)
    tp2 = Column(Float)
    trailing_stop = Column(Float, nullable=True)
    max_price = Column(Float, nullable=True)  # Максимальная цена
    
    # Статус
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN)
    closed_at = Column(DateTime, nullable=True)
    close_price = Column(Float, nullable=True)
    close_reason = Column(String(50), nullable=True)
    
    # Итоговый P&L
    total_pnl_usdt = Column(Float, nullable=True)
    total_pnl_pct = Column(Float, nullable=True)
    
    # Связи
    signal = relationship("Signal", back_populates="position")
    trades = relationship("Trade", back_populates="position")
    
    __table_args__ = (
        Index("idx_position_status", "status"),
        Index("idx_position_symbol", "symbol"),
    )


class Trade(Base):
    """История всех сделок (покупки/продажи)"""
    __tablename__ = "trades"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id"), nullable=True)
    
    # Основные данные
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)    # BUY / SELL
    order_type = Column(String(20))              # MARKET / LIMIT
    
    # Исполнение
    price = Column(Float, nullable=False)
    amount = Column(Float, nullable=False)
    value_usdt = Column(Float, nullable=False)
    fee = Column(Float, default=0)
    fee_currency = Column(String(10))
    
    # Биржевые данные
    order_id = Column(String(50), nullable=True)
    exchange_trade_id = Column(String(50), nullable=True)
    
    # P&L для продаж
    pnl_usdt = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    
    # Причина сделки
    reason = Column(String(50))  # SIGNAL, TP1, TP2, TP3, SL, MANUAL
    
    # Время
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Связь
    position = relationship("Position", back_populates="trades")


class DailyStats(Base):
    """Дневная статистика для анти-FOMO"""
    __tablename__ = "daily_stats"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, nullable=False, unique=True, index=True)
    
    # Счётчики
    signals_sent = Column(Integer, default=0)
    signals_accepted = Column(Integer, default=0)
    signals_rejected = Column(Integer, default=0)
    
    # P&L
    trades_won = Column(Integer, default=0)
    trades_lost = Column(Integer, default=0)
    total_pnl_usdt = Column(Float, default=0)
    
    # Стопы
    stop_losses_today = Column(Integer, default=0)
    trading_paused = Column(Boolean, default=False)
    
    # Обновление
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SignalCooldown(Base):
    """Кулдауны для анти-FOMO (1 сигнал на пару в 6 часов)"""
    __tablename__ = "signal_cooldowns"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True, index=True)
    last_signal_at = Column(DateTime, nullable=False)
