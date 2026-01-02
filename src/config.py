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
    "volume_sma": 20,
    "atr_period": 14,
    "rsi_period": 14,
    "lookback_candles": 20,    # Свечей для анализа накопления
}

# ================== УСЛОВИЯ СИГНАЛА ==================
SIGNAL_CONDITIONS = {
    # Фаза накопления
    "accumulation_range_mult": 2.5,    # Диапазон ≤ 2.5 × ATR
    "accumulation_volume_ratio": 0.8,  # Объём ниже 80% среднего
    
    # Импульс (breakout)
    "volume_breakout_mult": 2.0,       # Объём ≥ 2 × SMA20
    "max_candle_growth": 0.08,         # Рост свечи ≤ 8%
    "min_candle_growth": 0.005,        # Рост свечи ≥ 0.5%
    
    # Фильтры качества
    "max_rsi": 70,                     # RSI ≤ 70
    "max_spread": 0.006,               # Спред ≤ 0.6%
    "min_bid_ask_ratio": 0.8,          # bid/ask ≥ 0.8
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
    "signal_cooldown_hours": 6,        # 1 сигнал на пару в 6 часов
    "max_from_daily_low_pct": 0.10,    # Не покупать если +10% от лоя дня
    "btc_drop_threshold": -0.015,      # BTC падает -1.5% за 1ч → молчим
    "night_hours_utc": (0, 6),         # Ночь UTC → не торгуем
}

# ================== ИНТЕРВАЛЫ СКАНИРОВАНИЯ ==================
SCAN_INTERVALS = {
    "universe_update_sec": 300,        # Обновление списка пар: 5 мин
    "signal_scan_sec": 60,             # Сканирование сигналов: 1 мин
    "position_check_sec": 30,          # Проверка позиций: 30 сек
}

# ================== ЛОГИРОВАНИЕ ==================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
