"""
Конфигурация бота - все настройки в одном месте
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ================== BYBIT API ==================
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET = os.getenv("BYBIT_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# ================== TELEGRAM ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# ================== DATABASE ==================
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bot:botpassword@localhost:5432/breakout_bot"
)

# ================== ФИЛЬТРЫ АКТИВОВ ==================
ASSET_FILTERS = {
    "min_price": 0.0005,           # Минимальная цена USDT
    "max_price": 1.0,              # Максимальная цена USDT
    "min_volume_24h": 200_000,     # Минимальный оборот за 24ч
    "excluded_bases": [            # Исключённые базовые активы
        "BTC", "ETH", "USDT", "USDC", "BUSD", "DAI", 
        "WBTC", "WETH", "STETH", "TUSD"
    ],
}

# ================== ТАЙМФРЕЙМЫ ==================
TIMEFRAMES = {
    "main": "5",       # Основной (5 минут)
    "confirm": "1",    # Подтверждение (1 минута)
    "context": "15",   # Контекст (15 минут)
}

# ================== ИНДИКАТОРЫ ==================
INDICATORS = {
    "ema_fast": 7,
    "ema_mid": 14,
    "ema_slow": 28,
    "ema_trend": 100,
    "ema9": 9,                  # EMA9 для профессиональной стратегии
    "ema21": 21,                # EMA21 для профессиональной стратегии
    "ema50": 50,                # EMA50 для профессиональной стратегии
    "volume_sma": 20,
    "atr_period": 14,
    "rsi_period": 14,
    "lookback_candles": 20,    # Свечей для анализа накопления
}

# ================== УСЛОВИЯ СИГНАЛА ==================
SIGNAL_CONDITIONS = {
    # Фаза накопления (ослаблено для ловли пампов)
    "accumulation_range_mult": 5.0,    # Диапазон ≤ 5.0 × ATR (было 2.5)
    "accumulation_volume_ratio": 1.5,  # Объём ниже 150% среднего (было 0.8)
    
    # Импульс (breakout) - ПРОФЕССИОНАЛЬНАЯ СТРАТЕГИЯ
    "volume_breakout_mult": 3.0,       # Объём ≥ 3.0 × SMA20 (аномалия!)
    "max_candle_growth": 0.08,         # Рост свечи ≤ 8% (ранний вход)
    "min_candle_growth": 0.005,        # Рост свечи ≥ 0.5% (очень ранний вход)
    
    # Фильтры качества - ПРОФЕССИОНАЛЬНАЯ СТРАТЕГИЯ
    "min_rsi": 50,                     # RSI ≥ 50 (импульсная фаза)
    "max_rsi": 70,                     # RSI ≤ 70 (не перегрет)
    "require_ema_setup": True,         # Требовать EMA структуру (EMA9>EMA21, Price>EMA50)
    "max_spread": 0.008,               # Спред ≤ 0.8%
    "min_bid_ask_ratio": 0.7,          # bid/ask ≥ 0.7
}

# ================== РИСК-МЕНЕДЖМЕНТ ==================
RISK_MANAGEMENT = {
    "position_size_pct": 0.10,         # 10% депозита на сделку
    "max_risk_per_trade": 0.01,        # Риск ≤ 1% депозита
    "max_positions": 3,                # Макс 3 активных позиции
    "max_daily_losses": 2,             # Макс 2 стопа в день → пауза
    
    # Take Profit уровни
    "tp1_pct": 0.05,                   # TP1 +5%
    "tp1_close_pct": 0.30,             # Закрыть 30% на TP1
    "tp2_pct": 0.10,                   # TP2 +10%
    "tp2_close_pct": 0.30,             # Закрыть 30% на TP2
    "tp3_trailing": True,              # TP3 = trailing stop
    
    # Stop Loss
    "sl_below_ema28": True,            # SL под EMA28
    "sl_buffer_pct": 0.005,            # Буфер 0.5% под уровень
}

# ================== АНТИ-FOMO ЗАЩИТА ==================
ANTI_FOMO = {
    "signal_cooldown_hours": 2,        # 1 сигнал на пару в 2 часа (было 6)
    "max_from_daily_low_pct": 0.20,    # Не покупать если +20% от лоя дня (было 10%)
    "btc_drop_threshold": -0.03,       # BTC падает -3% за 1ч → молчим (было -1.5%)
    "night_hours_utc": (2, 5),         # Ночь UTC → не торгуем (сужено)
}

# ================== ПРЕДСИГНАЛЫ (пары близкие к сигналу) ==================
PRESIGNALS = {
    "enabled": True,                   # Включить/отключить уведомления о предсигналах
    "check_interval_minutes": 5,       # Проверять каждые 5 минут
    "min_conditions_met": 2,           # Минимум 2 условия из 4 для уведомления
    "condition_weights": {             # Вес каждого условия (для будущего)
        "volume": 1.0,
        "growth": 1.0,
        "rsi": 1.0,
        "ema_setup": 1.0,
    }
}

# ================== ИНТЕРВАЛЫ СКАНИРОВАНИЯ ==================
SCAN_INTERVALS = {
    "universe_update_sec": 300,        # Обновление списка пар: 5 мин
    "signal_scan_sec": 60,             # Сканирование сигналов: 1 мин
    "position_check_sec": 30,          # Проверка позиций: 30 сек
    "presignal_check_sec": 300,        # Проверка предсигналов: 5 мин
}

# ================== ЛОГИРОВАНИЕ ==================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
