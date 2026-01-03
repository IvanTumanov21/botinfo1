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
    # Фаза накопления (ослаблено для ловли пампов)
    "accumulation_range_mult": 5.0,    # Диапазон ≤ 5.0 × ATR (было 2.5)
    "accumulation_volume_ratio": 1.5,  # Объём ниже 150% среднего (было 0.8)
    
    # Импульс (breakout)
    "volume_breakout_mult": 1.5,       # Объём ≥ 1.5 × SMA20 (было 2.0)
    "max_candle_growth": 0.15,         # Рост свечи ≤ 15% (было 8%)
    "min_candle_growth": 0.02,         # Рост свечи ≥ 2% (было 0.5%)
    
    # Фильтры качества
    "max_rsi": 75,                     # RSI ≤ 75 (было 70)
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

# ================== ИНТЕРВАЛЫ СКАНИРОВАНИЯ ==================
SCAN_INTERVALS = {
    "universe_update_sec": 300,        # Обновление списка пар: 5 мин
    "signal_scan_sec": 60,             # Сканирование сигналов: 1 мин
    "position_check_sec": 30,          # Проверка позиций: 30 сек
}

# ================== ЛОГИРОВАНИЕ ==================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
