import os
import sqlite3
import psutil
from pathlib import Path
from datetime import datetime, timezone

from collections import deque
from dotenv import load_dotenv

# ================== ПУТИ И ENV ==================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "botinfo.db"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("API_KEY")
SECRET = os.getenv("SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ================== ПРОВЕРКА ENV ==================

def check_env():
    missing = []
    if not API_KEY:
        missing.append("API_KEY")
    if not SECRET:
        missing.append("SECRET")
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Не заполнены переменные окружения: "
            + ", ".join(missing)
            + "\nПроверь файл .env"
        )


# ================== БАЗА ДАННЫХ (SQLite) ==================

def init_db():
    """
    Создаёт файл базы данных botinfo.db и таблицу trades, если её ещё нет.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица сделок (аналог trades.csv, но в SQL)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            usd_value REAL NOT NULL,
            pnl_pct REAL NOT NULL DEFAULT 0.0,
            pnl_usd REAL NOT NULL DEFAULT 0.0,
            time_utc TEXT NOT NULL
        );
        """
    )

    # Индексы для быстрых выборок по дате и символу
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);")

    conn.commit()
    conn.close()


def migrate_csv_to_db():
    """
    Одноразовая миграция старого trades.csv в таблицу trades.
    Выполняется только если таблица пустая и CSV существует.
    """
    csv_path = BASE_DIR / "trades.csv"
    if not csv_path.exists():
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM trades;")
    existing_rows = cur.fetchone()[0]
    if existing_rows > 0:
        conn.close()
        return

    try:
        import pandas as pd  # локальный импорт, чтобы не падать, если pandas отсутствует
        df = pd.read_csv(csv_path, engine="python", on_bad_lines="skip")
    except Exception:
        conn.close()
        return

    required_cols = {"type", "symbol", "price", "amount", "usd_value"}
    if not required_cols.issubset(df.columns):
        conn.close()
        return

    if "pnl_pct" not in df.columns:
        df["pnl_pct"] = 0.0
    if "pnl_usd" not in df.columns:
        df["pnl_usd"] = 0.0

    if "time" in df.columns:
        df["time_utc"] = pd.to_datetime(df["time"], errors="coerce")
    else:
        df["time_utc"] = pd.NaT

    df["time_utc"] = df["time_utc"].fillna(pd.Timestamp.utcnow())

    rows = [
        (
            row["type"],
            row["symbol"],
            float(row["price"]),
            float(row["amount"]),
            float(row["usd_value"]),
            float(row.get("pnl_pct", 0.0)),
            float(row.get("pnl_usd", 0.0)),
            row["time_utc"].isoformat(),
        )
        for _, row in df.iterrows()
    ]

    cur.executemany(
        """
        INSERT INTO trades (type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def load_trades_dataframe():
    """
    Возвращает DataFrame с колонкой time (UTC) из таблицы trades.
    """
    if not DB_PATH.exists():
        return pd.DataFrame(
            columns=[
                "type",
                "symbol",
                "price",
                "amount",
                "usd_value",
                "pnl_pct",
                "pnl_usd",
                "time",
            ]
        )

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc
        FROM trades
        """,
        conn,
        parse_dates=["time_utc"],
    )
    conn.close()

    if df.empty:
        return df

    df = df.rename(columns={"time_utc": "time"})
    return df


def log_trade_sql(
    trade_type: str,
    symbol: str,
    price: float,
    amount: float,
    usd_value: float,
    pnl_pct: float = 0.0,
    pnl_usd: float = 0.0,
):
    """
    Пример функции записи сделки в базу.
    Ты потом сможешь вызывать её из своего торгового кода
    вместо/вместе с log_trade в CSV.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    time_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    cur.execute(
        """
        INSERT INTO trades (type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trade_type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc),
    )

    conn.commit()
    conn.close()


# ================== МЕСТО ДЛЯ ТВОЕГО БОТА ==================
import asyncio
import json
import os
import time
from datetime import datetime, timedelta
import signal
import io

import ccxt
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from dotenv import load_dotenv

matplotlib.use("Agg")

# ================== ENV ==================
load_dotenv()

API_KEY = os.getenv("API_KEY")
SECRET = os.getenv("SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
chat_id_env = os.getenv("TELEGRAM_CHAT_ID")

if not chat_id_env:
    raise RuntimeError("TELEGRAM_CHAT_ID не задан. Проверь .env или переменные окружения.")

TELEGRAM_CHAT_ID = int(chat_id_env)

# ================== CONFIG ==================
STRATEGY_CONFIG = {
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_min": 50,
    "rsi_max": 70,
    "volume_mult": 1.5,
    "price_growth_min": 0.0005,
    "sl_pct": 0.02,
    "tp_pct": 0.04,
    "min_interval_sec": 300,
    "auto_enabled": True,
    "notifications_enabled": True,
    "atr_threshold_pct": 0.01,
    "fib_buy_level": 0.382,
    "fib_sell_level": 0.618,
    "min_order_usd": 5,
    "price_update_interval_sec": 300,  # интервал уведомлений о ценах (сек)
}

MOSCOW_OFFSET_HOURS = 3

COINS = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "SOL": "SOL/USDT",
    "XRP": "XRP/USDT",
}
SYMBOLS = list(COINS.values())
DEFAULT_SYMBOL = COINS["BTC"]

PAIR_URL_TEMPLATE = "https://bingx.com/en/spot/{pair}"

SELL_KEYWORDS = [
    "SELL",
    "STOP",
    "TAKE",
    "FORCE",
    "MANUAL_SELL",
    "MANUAL_POSITION_CLOSE",
    "MANUAL_EXTERNAL_CLOSE",
    "AUTO_SELL",
]
SELL_PATTERN = "|".join(SELL_KEYWORDS)

# ================== АДАПТИВНАЯ СТРАТЕГИЯ ПО МОНЕТАМ ==================
# Автоадаптация “мягкости/жесткости” условий по каждой монете отдельно.
ADAPTIVE_STATE = {}  # symbol -> dict
GLOBAL_START_MODE = "normal"  # "soft" | "normal" | "hard"

MAX_RISK_LEVEL = 3
MIN_RISK_LEVEL = -3
MIN_TRADES_BEFORE_AUTO = 5


def init_adaptive_state():
    """
    Инициализирует адаптацию для всех символов. Вызывать после load_state().
    """
    global ADAPTIVE_STATE
    if not ADAPTIVE_STATE:
        ADAPTIVE_STATE = {}
        for symbol in SYMBOLS:
            ADAPTIVE_STATE[symbol] = {
                "risk_level": 0,
                "manual_mode": "normal",
                "trades_in_manual_mode": 0,
                "last_pnls": deque(maxlen=20),
            }
    else:
        # Пополняем отсутствующие символы
        for symbol in SYMBOLS:
            ADAPTIVE_STATE.setdefault(
                symbol,
                {
                    "risk_level": 0,
                    "manual_mode": "normal",
                    "trades_in_manual_mode": 0,
                    "last_pnls": deque(maxlen=20),
                },
            )


def _apply_manual_preset(preset: str):
    """
    Включает ручной пресет ("soft" / "normal" / "hard") для ВСЕХ монет.
    После MIN_TRADES_BEFORE_AUTO сделок по монете включается автоадаптация.
    """
    global GLOBAL_START_MODE
    GLOBAL_START_MODE = preset

    for symbol in SYMBOLS:
        st = ADAPTIVE_STATE.setdefault(
            symbol,
            {
                "risk_level": 0,
                "manual_mode": "normal",
                "trades_in_manual_mode": 0,
                "last_pnls": deque(maxlen=20),
            },
        )
        st["manual_mode"] = preset
        st["trades_in_manual_mode"] = 0
        st["last_pnls"] = deque(maxlen=20)

        if preset == "soft":
            st["risk_level"] = -1
        elif preset == "hard":
            st["risk_level"] = 1
        else:
            st["risk_level"] = 0


def set_soft_start_mode():
    _apply_manual_preset("soft")


def set_normal_start_mode():
    _apply_manual_preset("normal")


def set_hard_start_mode():
    _apply_manual_preset("hard")


def adaptive_on_trade(symbol: str, trade_type: str, pnl_pct: float):
    """
    Вызывается при закрытии сделки (SELL-типы). Корректирует risk_level.
    """
    if symbol not in ADAPTIVE_STATE:
        return

    st = ADAPTIVE_STATE[symbol]
    st["last_pnls"].append(pnl_pct)

    if not any(kw in trade_type for kw in SELL_KEYWORDS):
        return

    if st["manual_mode"] is not None:
        st["trades_in_manual_mode"] += 1
        if st["trades_in_manual_mode"] >= MIN_TRADES_BEFORE_AUTO:
            st["manual_mode"] = None  # переходим в автоадаптацию
        return

    rl = st.get("risk_level", 0)

    if pnl_pct <= -0.3:
        rl = min(rl + 1, MAX_RISK_LEVEL)
    elif pnl_pct >= 0.5:
        rl = max(rl - 1, MIN_RISK_LEVEL)

    if len(st["last_pnls"]) >= 5:
        last5 = list(st["last_pnls"])[-5:]
        avg5 = sum(last5) / len(last5)
        if avg5 < -0.2:
            rl = min(rl + 1, MAX_RISK_LEVEL)
        elif avg5 > 0.5:
            rl = max(rl - 1, MIN_RISK_LEVEL)

    st["risk_level"] = rl


def get_symbol_config(symbol: str) -> dict:
    """
    Возвращает базовый конфиг с учётом risk_level монеты.
    """
    base = dict(STRATEGY_CONFIG)
    st = ADAPTIVE_STATE.get(symbol)
    if not st:
        return base

    rl = max(MIN_RISK_LEVEL, min(MAX_RISK_LEVEL, st.get("risk_level", 0)))
    risk_factor = 1.0 - 0.1 * rl
    entry_factor = 1.0 + 0.2 * (-rl)

    base["sl_pct"] = max(0.003, min(0.1, base["sl_pct"] * risk_factor))
    base["tp_pct"] = max(0.005, min(0.25, base["tp_pct"] * (1.0 + 0.05 * (-rl))))

    base["volume_mult"] = max(1.0, base["volume_mult"] / max(0.5, entry_factor))
    base["price_growth_min"] = max(
        0.0001, base["price_growth_min"] / max(0.5, entry_factor)
    )

    base_min_int = base["min_interval_sec"]
    if rl > 0:
        base["min_interval_sec"] = int(base_min_int * (1.0 + 0.2 * rl))
    elif rl < 0:
        base["min_interval_sec"] = max(30, int(base_min_int * (1.0 + 0.1 * rl)))

    rsi_min_new = max(10, min(80, base["rsi_min"] - 3 * rl))
    rsi_max_new = max(20, min(90, base["rsi_max"] + 3 * (-rl)))
    if rsi_max_new <= rsi_min_new:
        rsi_max_new = rsi_min_new + 5

    base["rsi_min"] = rsi_min_new
    base["rsi_max"] = rsi_max_new

    return base

# ================== STATE ==================
positions = {
    sym: {"in_position": False, "entry_price": 0.0, "amount": 0.0, "buy_time": None}
    for sym in SYMBOLS
}

last_price = {sym: 0.0 for sym in SYMBOLS}
price_initialized = {sym: False for sym in SYMBOLS}
last_trade_time = {sym: 0 for sym in SYMBOLS}

day_open_price = {sym: 0.0 for sym in SYMBOLS}
day_open_msk_date = {sym: "" for sym in SYMBOLS}

ACTIVE_COINS = {sym: True for sym in SYMBOLS}

TRADE_DEPOSITS = {
    COINS["BTC"]: 10.0,
    COINS["ETH"]: 10.0,
    COINS["SOL"]: 10.0,
    COINS["XRP"]: 10.0,
}

current_prices = {sym: 0.0 for sym in SYMBOLS}
last_price_update_time = 0

manual_seen_trade_ids = set()

running = True

# ================== UTILS ==================
def is_authorized_user(update: Update) -> bool:
    user_id = None
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    return user_id == TELEGRAM_CHAT_ID if user_id else False


def get_exchange():
    ex = ccxt.bingx(
        {
            "apiKey": API_KEY,
            "secret": SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
    ex.load_markets()
    return ex


def save_state():
    state = {
        "positions": positions,
        "last_price": last_price,
        "strategy_config": STRATEGY_CONFIG,
        "day_open_price": day_open_price,
        "day_open_msk_date": day_open_msk_date,
        "active_coins": ACTIVE_COINS,
        "trade_deposits": TRADE_DEPOSITS,
        "manual_seen_trade_ids": list(manual_seen_trade_ids),
        "adaptive_state": {
            sym: {
                "risk_level": st.get("risk_level", 0),
                "manual_mode": st.get("manual_mode"),
                "trades_in_manual_mode": st.get("trades_in_manual_mode", 0),
                "last_pnls": list(st.get("last_pnls", [])),
            }
            for sym, st in ADAPTIVE_STATE.items()
        },
        "global_start_mode": GLOBAL_START_MODE,
    }
    with open("state.json", "w") as f:
        json.dump(state, f)


def load_state():
    global positions, last_price, STRATEGY_CONFIG
    global day_open_price, day_open_msk_date, ACTIVE_COINS, TRADE_DEPOSITS
    global manual_seen_trade_ids, ADAPTIVE_STATE, GLOBAL_START_MODE

    if os.path.exists("state.json"):
        try:
            with open("state.json") as f:
                state = json.load(f)

            if isinstance(state, dict) and "positions" in state:
                saved_positions = state.get("positions", {})
                for sym in SYMBOLS:
                    if sym in saved_positions:
                        positions[sym] = saved_positions[sym]

                saved_last_price = state.get("last_price", {})
                if isinstance(saved_last_price, dict):
                    for sym in SYMBOLS:
                        if sym in saved_last_price:
                            last_price[sym] = saved_last_price[sym]

                if "strategy_config" in state:
                    saved_config = state["strategy_config"]
                    for key in saved_config:
                        if key in STRATEGY_CONFIG:
                            STRATEGY_CONFIG[key] = saved_config[key]

                saved_day_open_price = state.get("day_open_price", {})
                if isinstance(saved_day_open_price, dict):
                    for sym in SYMBOLS:
                        if sym in saved_day_open_price:
                            day_open_price[sym] = saved_day_open_price[sym]

                saved_day_open_msk_date = state.get("day_open_msk_date", {})
                if isinstance(saved_day_open_msk_date, dict):
                    for sym in SYMBOLS:
                        if sym in saved_day_open_msk_date:
                            day_open_msk_date[sym] = saved_day_open_msk_date[sym]

                saved_active = state.get("active_coins", {})
                if isinstance(saved_active, dict):
                    for sym in SYMBOLS:
                        if sym in saved_active:
                            ACTIVE_COINS[sym] = saved_active[sym]

                saved_deposits = state.get("trade_deposits", {})
                if isinstance(saved_deposits, dict):
                    for sym in SYMBOLS:
                        if sym in saved_deposits:
                            TRADE_DEPOSITS[sym] = float(saved_deposits[sym])

                saved_manual_ids = state.get("manual_seen_trade_ids", [])
                if isinstance(saved_manual_ids, list):
                    manual_seen_trade_ids = set(saved_manual_ids)

                saved_adapt = state.get("adaptive_state", {})
                if isinstance(saved_adapt, dict):
                    for sym in SYMBOLS:
                        raw = saved_adapt.get(sym, {})
                        ADAPTIVE_STATE[sym] = {
                            "risk_level": int(raw.get("risk_level", 0)),
                            "manual_mode": raw.get("manual_mode", "normal"),
                            "trades_in_manual_mode": int(
                                raw.get("trades_in_manual_mode", 0)
                            ),
                            "last_pnls": deque(raw.get("last_pnls", []), maxlen=20),
                        }

                GLOBAL_START_MODE = state.get("global_start_mode", "normal")

        except Exception:
            positions.clear()
            for sym in SYMBOLS:
                positions[sym] = {
                    "in_position": False,
                    "entry_price": 0.0,
                    "amount": 0.0,
                    "buy_time": None,
                }
                last_price[sym] = 0.0


def log_trade(trade_data):
    """
    Запись сделки в SQLite и фиксация результата для адаптации.
    Ожидает словарь с ключами type, symbol, price, amount, usd_value и опционально pnl_pct, pnl_usd.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO trades (type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_data.get("type"),
            trade_data.get("symbol"),
            float(trade_data.get("price", 0.0)),
            float(trade_data.get("amount", 0.0)),
            float(trade_data.get("usd_value", 0.0)),
            float(trade_data.get("pnl_pct", 0.0)),
            float(trade_data.get("pnl_usd", 0.0)),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def calculate_indicators(ohlcv):
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))
    df["avg_volume"] = df["volume"].rolling(20).mean()
    return df.iloc[-1], df


def calculate_atr(df, period=14):
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    tr_list = []
    for i in range(1, len(high)):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        tr_list.append(tr)
    if len(tr_list) >= period:
        return sum(tr_list[-period:]) / period
    return 0


def get_market_context(symbol, current_close, cfg, ex):
    try:
        ohlcv_24h = ex.fetch_ohlcv(symbol, "1m", limit=1440)
        df_24h = pd.DataFrame(
            ohlcv_24h, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df_24h["high"] = df_24h["high"].astype(float)
        df_24h["low"] = df_24h["low"].astype(float)
        df_24h["close"] = df_24h["close"].astype(float)

        resistance = df_24h["high"].max()
        support = df_24h["low"].min()
        atr_14 = calculate_atr(df_24h, 14)

        if atr_14 > current_close * cfg["atr_threshold_pct"]:
            print(f"[{symbol}] ATR too high: {atr_14:.2f}")
            return {
                "trade_allowed": False,
                "support": support,
                "resistance": resistance,
                "fib_382": None,
                "fib_618": None,
                "atr_14": atr_14,
            }

        diff = resistance - support
        fib_382 = support + diff * cfg["fib_buy_level"]
        fib_618 = support + diff * cfg["fib_sell_level"]

        return {
            "trade_allowed": True,
            "support": support,
            "resistance": resistance,
            "fib_382": fib_382,
            "fib_618": fib_618,
            "atr_14": atr_14,
        }

    except Exception as e:
        print(f"[{symbol}] Market context error: {e}")
        return None


def get_pnl_today():
    df = load_trades_dataframe()
    if df.empty:
        return 0.0, 0.0

    try:
        if "pnl_pct" not in df.columns:
            df["pnl_pct"] = 0.0
        if "pnl_usd" not in df.columns:
            df["pnl_usd"] = 0.0

        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])

        now_utc = datetime.now(timezone.utc)
        now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)

        today_msk_start = now_msk.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tomorrow_msk_start = today_msk_start + timedelta(days=1)

        today_utc_start = today_msk_start - timedelta(hours=MOSCOW_OFFSET_HOURS)
        tomorrow_utc_start = tomorrow_msk_start - timedelta(
            hours=MOSCOW_OFFSET_HOURS
        )

        today = df[
            (df["time"] >= today_utc_start) & (df["time"] < tomorrow_utc_start)
        ]

        sells = today[today["type"].str.contains(SELL_PATTERN, na=False)]

        total_pnl_pct = sells["pnl_pct"].sum() if not sells.empty else 0.0
        total_usd = sells["pnl_usd"].sum() if not sells.empty else 0.0

        return total_pnl_pct, total_usd

    except Exception as e:
        print(f"P&L Error: {e}")
        return 0.0, 0.0


def get_pnl_today_per_symbol():
    df = load_trades_dataframe()
    if df.empty:
        return {}, 0.0, 0.0

    try:
        required_cols = {"time", "type", "symbol"}
        if not required_cols.issubset(df.columns):
            return {}, 0.0, 0.0

        if "pnl_pct" not in df.columns:
            df["pnl_pct"] = 0.0
        if "pnl_usd" not in df.columns:
            df["pnl_usd"] = 0.0

        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])

        now_utc = datetime.now(timezone.utc)
        now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)

        today_msk_start = now_msk.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tomorrow_msk_start = today_msk_start + timedelta(days=1)

        today_utc_start = today_msk_start - timedelta(hours=MOSCOW_OFFSET_HOURS)
        tomorrow_utc_start = tomorrow_msk_start - timedelta(
            hours=MOSCOW_OFFSET_HOURS
        )

        today = df[
            (df["time"] >= today_utc_start) & (df["time"] < tomorrow_utc_start)
        ]

        sells = today[today["type"].str.contains(SELL_PATTERN, na=False)]

        if sells.empty:
            return {}, 0.0, 0.0

        grouped = sells.groupby("symbol")[["pnl_pct", "pnl_usd"]].sum()

        per_symbol = {}
        for sym, row in grouped.iterrows():
            per_symbol[sym] = (float(row["pnl_pct"]), float(row["pnl_usd"]))

        total_pnl_pct = float(grouped["pnl_pct"].sum())
        total_usd = float(grouped["pnl_usd"].sum())

        return per_symbol, total_pnl_pct, total_usd

    except Exception as e:
        print(f"P&L per symbol Error: {e}")
        return {}, 0.0, 0.0


# ================== СТАТИСТИКА ЭФФЕКТИВНОСТИ ==================

def get_trading_statistics():
    """
    Рассчитывает статистику эффективности торговли:
    - Winrate (% прибыльных сделок)
    - Average P&L per trade
    - Max Drawdown
    - Sharpe Ratio (упрощенный)
    """
    df = load_trades_dataframe()
    if df.empty:
        return None
    
    try:
        # Фильтруем только SELL сделки (они имеют P&L)
        sells = df[df["type"].str.contains(SELL_PATTERN, na=False)].copy()
        
        if sells.empty or len(sells) < 2:
            return None
        
        # Winrate - процент прибыльных сделок
        profitable_trades = len(sells[sells["pnl_usd"] > 0])
        total_trades = len(sells)
        winrate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0.0
        
        # Average P&L per trade
        avg_pnl_usd = sells["pnl_usd"].mean()
        avg_pnl_pct = sells["pnl_pct"].mean()
        
        # Max Drawdown - максимальная просадка от пика
        sells["cumulative_pnl"] = sells["pnl_usd"].cumsum()
        running_max = sells["cumulative_pnl"].expanding().max()
        drawdown = sells["cumulative_pnl"] - running_max
        max_drawdown = abs(drawdown.min())
        
        # Sharpe Ratio (упрощенный) - отношение средней доходности к волатильности
        returns_std = sells["pnl_pct"].std()
        sharpe_ratio = (avg_pnl_pct / returns_std) if returns_std > 0 else 0.0
        
        # Максимальная прибыль и убыток
        max_profit = sells["pnl_usd"].max()
        max_loss = sells["pnl_usd"].min()
        
        # Общий P&L
        total_pnl_usd = sells["pnl_usd"].sum()
        
        return {
            "total_trades": total_trades,
            "winrate": winrate,
            "profitable_trades": profitable_trades,
            "losing_trades": total_trades - profitable_trades,
            "avg_pnl_usd": avg_pnl_usd,
            "avg_pnl_pct": avg_pnl_pct,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "max_profit": max_profit,
            "max_loss": max_loss,
            "total_pnl_usd": total_pnl_usd,
        }
    
    except Exception as e:
        print(f"Statistics Error: {e}")
        return None


def log_memory_usage():
    """Логирует текущее использование памяти процесса"""
    try:
        process = psutil.Process()
        mem_info = process.memory_info()
        mem_mb = mem_info.rss / 1024 / 1024  # в мегабайтах
        
        # Логируем только если памяти много
        if mem_mb > 200:
            print(f"⚠️ Память: {mem_mb:.1f} MB")
        
        return mem_mb
    except Exception as e:
        print(f"Memory monitoring error: {e}")
        return 0.0


def plot_mini_chart(symbol, ohlcv):
    try:
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("date", inplace=True)
        df = df.astype(float).tail(30)
        plt.figure(figsize=(6, 3))
        plt.plot(df.index, df["close"], linewidth=2)

        pos = positions[symbol]
        if pos["in_position"]:
            plt.axhline(
                y=pos["entry_price"],
                linestyle="--",
                label=f'Entry: {pos["entry_price"]:,.2f}',
            )
            plt.axhline(
                y=pos["entry_price"] * (1 - STRATEGY_CONFIG["sl_pct"]),
                linestyle="--",
                label=f'SL: {pos["entry_price"] * (1 - STRATEGY_CONFIG["sl_pct"]):,.2f}',
            )
            plt.axhline(
                y=pos["entry_price"] * (1 + STRATEGY_CONFIG["tp_pct"]),
                linestyle="-.",
                label=f'TP: {pos["entry_price"] * (1 + STRATEGY_CONFIG["tp_pct"]):,.2f}',
            )
            plt.legend(loc="upper left", fontsize=7)
        plt.title(f"{symbol} | {len(df)}m", fontsize=10)
        plt.grid(alpha=0.3)
        plt.xticks(rotation=45, fontsize=6)
        plt.yticks(fontsize=8)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        plt.close()
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Plot Error ({symbol}): {e}")
        return


# ================== TELEGRAM SEND ==================
def with_start_button(markup=None):
    start_btn = InlineKeyboardButton("🏠 /start", callback_data="back_to_main")
    if markup is None:
        return InlineKeyboardMarkup([[start_btn]])
    if isinstance(markup, InlineKeyboardMarkup):
        kb = [list(row) for row in markup.inline_keyboard]
        if not any(btn.text == start_btn.text for row in kb for btn in row):
            kb.append([start_btn])
        return InlineKeyboardMarkup(kb)
    return markup


async def send_telegram(text, photo=None, reply_markup=None):
    try:
        bot = Application.builder().token(TELEGRAM_TOKEN).build().bot
        reply_markup = with_start_button(reply_markup)
        if photo:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        print(f"TG: {text[:60]}...")
    except Exception as e:
        print(f"TG Error: {e}")


# ================== MARKET (КОРОТКИЙ) ==================
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    try:
        ex = get_exchange()
        sections = []

        for code, symbol in COINS.items():
            try:
                ohlcv = ex.fetch_ohlcv(symbol, "1m", limit=1440)
            except Exception as e:
                sections.append(f"❌ {symbol}: ошибка данных ({e})")
                continue

            df = pd.DataFrame(
                ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["close"] = df["close"].astype(float)
            df["volume"] = df["volume"].astype(float)

            current_price = df["close"].iloc[-1]
            open_24h = df["open"].iloc[0]
            change_24h_pct = (current_price - open_24h) / open_24h * 100

            df["ema50"] = df["close"].ewm(span=50).mean()
            df["ema200"] = df["close"].ewm(span=200).mean()
            ema50 = df["ema50"].iloc[-1]
            ema200 = df["ema200"].iloc[-1]
            if ema50 > ema200:
                trend = "📈 Рост"
            elif ema50 < ema200:
                trend = "📉 Падение"
            else:
                trend = "➡️ Боковик"

            delta = df["close"].diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / avg_loss
            df["rsi"] = 100 - (100 / (1 + rs))
            current_rsi = df["rsi"].iloc[-1]

            vol_24h = df["volume"].sum()
            avg_vol_24h = df["volume"].mean()
            vol_ratio = df["volume"].iloc[-1] / avg_vol_24h if avg_vol_24h > 0 else 0.0

            support = df["low"].min()
            resistance = df["high"].max()

            pair_code = symbol.replace("/", "")
            pair_url = PAIR_URL_TEMPLATE.format(pair=pair_code)
            pair_link = f'<a href="{pair_url}">{symbol}</a>'

            sec = (
                f"📊 <b>Анализ ({pair_link})</b>\n"
                f"Цена: <b>{current_price:,.4f}</b> ({change_24h_pct:+.2f}%)\n"
                f"Тренд: {trend}\n"
                f"RSI(14): <b>{current_rsi:.1f}</b>\n"
                f"Объём (24ч): {vol_24h:,.0f} | Текущий: {vol_ratio:.1f}x\n"
                f"Поддержка: <b>{support:,.2f}</b>\n"
                f"Сопротивление: <b>{resistance:,.2f}</b>"
            )

            sections.append(sec)

        msg = "\n\n".join(sections) + "\n\n/start"

        if update.message:
            await update.message.reply_text(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=with_start_button(),
            )
        else:
            await update.callback_query.message.reply_text(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=with_start_button(),
            )

    except Exception as e:
        error_msg = f"❌ Ошибка анализа: {e}"
        if update.message:
            await update.message.reply_text(
                error_msg, reply_markup=with_start_button()
            )
        else:
            await update.callback_query.message.reply_text(
                error_msg, reply_markup=with_start_button()
            )


# ================== START / MAIN MENU ==================
def build_main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Отчёт", callback_data="report"),
                InlineKeyboardButton("💰 P&L", callback_data="pnl"),
            ],
            [
                InlineKeyboardButton("📊 Рынок", callback_data="market"),
                InlineKeyboardButton("📈 Статистика", callback_data="statistics"),
            ],
            [
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
                InlineKeyboardButton("🚪 Выход из сделок", callback_data="positions_menu"),
            ],
            [
                InlineKeyboardButton("⬇️ Мягче старт", callback_data="risk_soft"),
                InlineKeyboardButton("⬆️ Жёстче старт", callback_data="risk_hard"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    ex = get_exchange()
    balance = ex.fetch_balance()
    usdt = float(balance.get("USDT", {}).get("free", 0))
    btc = float(balance.get("BTC", {}).get("free", 0))
    eth = float(balance.get("ETH", {}).get("free", 0))
    sol = float(balance.get("SOL", {}).get("free", 0))
    xrp = float(balance.get("XRP", {}).get("free", 0))
    pnl_pct, pnl_usd = get_pnl_today()

    pos_lines = []
    for code, sym in COINS.items():
        pos = positions[sym]
        risk = ADAPTIVE_STATE.get(sym, {}).get("risk_level", 0)
        adapt_label = f" | риск {risk:+d}"
        if pos["in_position"]:
            pos_lines.append(
                f"🟢 {code}: {pos['amount']:.6f} @ {pos['entry_price']:,.4f}{adapt_label}"
            )
        else:
            pos_lines.append(f"🔴 {code}: нет позиции{adapt_label}")
    positions_block = "\n".join(pos_lines)

    text = (
        f"💼 <b>Текущий баланс</b>\n"
        f"USDT: {usdt:,.2f}\n"
        f"BTC: {btc:.6f}\n"
        f"ETH: {eth:.6f}\n"
        f"SOL: {sol:.6f}\n"
        f"XRP: {xrp:.2f}\n\n"
        f"{positions_block}\n\n"
        f"📊 <b>P&L за день (МСК)</b>\n"
        f"В %: {pnl_pct:+.2f}%\n"
        f"В USDT: ${pnl_usd:+.2f}"
    )

    await update.message.reply_text(
        text,
        reply_markup=with_start_button(build_main_keyboard()),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    query = update.callback_query
    await query.answer()

    ex = get_exchange()
    balance = ex.fetch_balance()
    usdt = float(balance.get("USDT", {}).get("free", 0))
    btc = float(balance.get("BTC", {}).get("free", 0))
    eth = float(balance.get("ETH", {}).get("free", 0))
    sol = float(balance.get("SOL", {}).get("free", 0))
    xrp = float(balance.get("XRP", {}).get("free", 0))
    pnl_pct, pnl_usd = get_pnl_today()

    pos_lines = []
    for code, sym in COINS.items():
        pos = positions[sym]
        risk = ADAPTIVE_STATE.get(sym, {}).get("risk_level", 0)
        adapt_label = f" | риск {risk:+d}"
        if pos["in_position"]:
            pos_lines.append(
                f"🟢 {code}: {pos['amount']:.6f} @ {pos['entry_price']:,.4f}{adapt_label}"
            )
        else:
            pos_lines.append(f"🔴 {code}: нет позиции{adapt_label}")
    positions_block = "\n".join(pos_lines)

    text = (
        f"💼 <b>Текущий баланс</b>\n"
        f"USDT: {usdt:,.2f}\n"
        f"BTC: {btc:.6f}\n"
        f"ETH: {eth:.6f}\n"
        f"SOL: {sol:.6f}\n"
        f"XRP: {xrp:.2f}\n\n"
        f"{positions_block}\n\n"
        f"📊 <b>P&L за день (МСК)</b>\n"
        f"В %: {pnl_pct:+.2f}%\n"
        f"В USDT: ${pnl_usd:+.2f}"
    )

    try:
        await query.edit_message_text(
            text,
            reply_markup=with_start_button(build_main_keyboard()),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        # Игнорируем ошибку "Message is not modified"
        if "message is not modified" not in str(e).lower():
            print(f"Edit message error: {e}")


# ================== REPORT / PNL BUTTONS ==================
async def handle_report(query, day: str | None = None):
    try:
        df = load_trades_dataframe()
        if df.empty:
            await query.message.reply_text(
                "📊 Нет данных для отчёта", reply_markup=with_start_button()
            )
            return
        if "time" not in df.columns or "type" not in df.columns:
            await query.message.reply_text(
                "❌ Некорректный формат таблицы trades",
                reply_markup=with_start_button(),
            )
            return

        if "pnl_pct" not in df.columns:
            df["pnl_pct"] = 0.0
        if "pnl_usd" not in df.columns:
            df["pnl_usd"] = 0.0

        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        df["date"] = df["time"].dt.date

        dates = sorted(df["date"].unique(), reverse=True)
        if not dates:
            await query.message.reply_text(
                "📊 Нет данных для отчёта", reply_markup=with_start_button()
            )
            return

        if day is None:
            btn_rows = []
            for d in dates[:7]:
                btn_rows.append(
                    [InlineKeyboardButton(str(d), callback_data=f"report_day_{d}")]
                )
            btn_rows.append([InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")])
            await query.message.reply_text(
                "Выбери день для отчёта:",
                reply_markup=with_start_button(InlineKeyboardMarkup(btn_rows)),
            )
            return

        try:
            selected_date = datetime.fromisoformat(day).date()
        except Exception:
            await query.message.reply_text(
                "❌ Неверный формат даты", reply_markup=with_start_button()
            )
            return

        day_df = df[df["date"] == selected_date]
        if day_df.empty:
            await query.message.reply_text(
                f"📊 Сделок {selected_date} не найдено",
                reply_markup=with_start_button(),
            )
            return

        lines = [f"📊 <b>Отчёт {selected_date} (МСК)</b>\n"]
        total_pct = 0.0
        total_usd = 0.0

        for code, sym in COINS.items():
            s_df = day_df[day_df["symbol"] == sym]
            buys = s_df[s_df["type"].str.contains("BUY", na=False)]
            sells = s_df[s_df["type"].str.contains(SELL_PATTERN, na=False)]
            pnl_pct = sells["pnl_pct"].sum() if not sells.empty else 0.0
            pnl_usd = sells["pnl_usd"].sum() if not sells.empty else 0.0
            total_pct += pnl_pct
            total_usd += pnl_usd
            lines.append(
                f"{code}: BUY {len(buys)} | SELL {len(sells)} | P&L {pnl_pct:+.2f}% | ${pnl_usd:+.2f}"
            )

        lines.append(f"\nИтого: {total_pct:+.2f}% | ${total_usd:+.2f}")

        keyboard = [
            [InlineKeyboardButton(str(d), callback_data=f"report_day_{d}")]
            for d in dates[:7]
        ]
        keyboard.append([InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")])

        await query.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=with_start_button(InlineKeyboardMarkup(keyboard)),
        )
    except Exception as e:
        await query.message.reply_text(
            f"❌ Ошибка отчёта: {e}", reply_markup=with_start_button()
        )


async def show_pnl_per_symbol(query):
    per_symbol, total_pct, total_usd = get_pnl_today_per_symbol()

    lines = ["💰 <b>P&L за сегодня (МСК)</b>\n"]
    if not per_symbol:
        lines.append("По монетам: сделок сегодня ещё не было.")
    else:
        lines.append("<b>По монетам:</b>")
        for code, symbol in COINS.items():
            spct, susd = per_symbol.get(symbol, (0.0, 0.0))
            spct_disp = spct if abs(spct) >= 0.01 else 0.0
            susd_disp = susd if abs(susd) >= 0.01 else 0.0
            lines.append(
                f"{code}: {spct_disp:+.2f}% | ${susd_disp:+.2f}"
            )

    total_pct_disp = total_pct if abs(total_pct) >= 0.01 else 0.0
    total_usd_disp = total_usd if abs(total_usd) >= 0.01 else 0.0
    lines.append(
        f"\n<b>Итого:</b> {total_pct_disp:+.2f}% | ${total_usd_disp:+.2f}"
    )

    msg = "\n".join(lines)
    await query.message.reply_text(
        msg, parse_mode="HTML", reply_markup=with_start_button()
    )


async def show_statistics(query):
    """Показывает статистику эффективности торговли"""
    stats = get_trading_statistics()
    
    if stats is None:
        await query.message.reply_text(
            "📊 <b>Статистика</b>\n\n"
            "Недостаточно данных для расчёта статистики.\n"
            "Минимум 2 завершённых сделки необходимо.",
            parse_mode="HTML",
            reply_markup=with_start_button(),
        )
        return
    
    msg = (
        f"📈 <b>Статистика эффективности</b>\n\n"
        f"<b>Общие показатели:</b>\n"
        f"Всего сделок: {stats['total_trades']}\n"
        f"Прибыльных: {stats['profitable_trades']} ✅\n"
        f"Убыточных: {stats['losing_trades']} ❌\n"
        f"Winrate: <b>{stats['winrate']:.1f}%</b>\n\n"
        f"<b>Прибыльность:</b>\n"
        f"Средняя сделка: {stats['avg_pnl_pct']:+.2f}% | ${stats['avg_pnl_usd']:+.2f}\n"
        f"Общий P&L: <b>${stats['total_pnl_usd']:+.2f}</b>\n\n"
        f"<b>Риски:</b>\n"
        f"Макс. прибыль: ${stats['max_profit']:+.2f} 🟢\n"
        f"Макс. убыток: ${stats['max_loss']:+.2f} 🔴\n"
        f"Макс. просадка: ${stats['max_drawdown']:.2f}\n\n"
        f"<b>Эффективность:</b>\n"
        f"Sharpe Ratio: {stats['sharpe_ratio']:.2f}\n"
        f"<i>(>1.0 = хорошо, >2.0 = отлично)</i>"
    )
    
    await query.message.reply_text(
        msg, parse_mode="HTML", reply_markup=with_start_button()
    )


# ================== SETTINGS MENUS ==================
async def show_settings_menu(message):
    cfg = STRATEGY_CONFIG

    coin_status_lines = []
    for code, sym in COINS.items():
        coin_status_lines.append(
            f"{code}: {'✅ ВКЛ' if ACTIVE_COINS.get(sym, True) else '❌ ВЫКЛ'}"
        )
    coins_text = "\n".join(coin_status_lines)

    text = (
        f"⚙️ <b>Настройки стратегии</b>\n\n"
        f"<b>Монеты в авто-торговле:</b>\n{coins_text}\n\n"
        f"Авто-торговля (общая): {'✅ ВКЛ' if cfg['auto_enabled'] else '❌ ВЫКЛ'}\n"
        f"Уведомления о ценах: {'✅ ВКЛ' if cfg['notifications_enabled'] else '❌ ВЫКЛ'}\n"
        f"Интервал цен: {cfg['price_update_interval_sec']//60} мин\n"
        f"SL: {cfg['sl_pct']*100:.1f}% | TP: {cfg['tp_pct']*100:.1f}%\n"
        f"RSI: {cfg['rsi_min']}-{cfg['rsi_max']}\n"
        f"Объём > {cfg['volume_mult']:.1f}x\n"
        f"Интервал между сделками: {cfg['min_interval_sec']} сек\n"
        f"ATR порог: {cfg['atr_threshold_pct']*100:.1f}%\n"
        f"Fib Buy: {cfg['fib_buy_level']*100:.1f}%\n"
        f"Fib Sell: {cfg['fib_sell_level']*100:.1f}%\n"
        f"Мин. ордер: ${cfg['min_order_usd']}"
    )

    keyboard = [
        [InlineKeyboardButton("⚙️ Настройки торговли", callback_data="settings_trading")],
        [InlineKeyboardButton("💰 Настройки депозита", callback_data="settings_deposits")],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    reply_markup = with_start_button(InlineKeyboardMarkup(keyboard))

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def show_trading_settings_menu(message):
    cfg = STRATEGY_CONFIG

    coin_status_lines = []
    for code, sym in COINS.items():
        coin_status_lines.append(
            f"{code}: {'✅ ВКЛ' if ACTIVE_COINS.get(sym, True) else '❌ ВЫКЛ'}"
        )
    coins_text = "\n".join(coin_status_lines)

    text = (
        f"⚙️ <b>Настройки торговли</b>\n\n"
        f"<b>Монеты в авто-торговле:</b>\n{coins_text}\n\n"
        f"Авто-торговля (общая): {'✅ ВКЛ' if cfg['auto_enabled'] else '❌ ВЫКЛ'}\n"
        f"Уведомления о ценах: {'✅ ВКЛ' if cfg['notifications_enabled'] else '❌ ВЫКЛ'}\n"
        f"Интервал цен: {cfg['price_update_interval_sec']//60} мин\n\n"
        f"SL: {cfg['sl_pct']*100:.1f}% | TP: {cfg['tp_pct']*100:.1f}%\n"
        f"RSI: {cfg['rsi_min']}-{cfg['rsi_max']}\n"
        f"Объём > {cfg['volume_mult']:.1f}x\n"
        f"ATR порог: {cfg['atr_threshold_pct']*100:.1f}%\n"
        f"Интервал между сделками: {cfg['min_interval_sec']} сек\n"
        f"Мин. ордер: ${cfg['min_order_usd']}"
    )

    keyboard = [
        [
            InlineKeyboardButton("BTC авто", callback_data="toggle_coin_BTC"),
            InlineKeyboardButton("ETH авто", callback_data="toggle_coin_ETH"),
        ],
        [
            InlineKeyboardButton("SOL авто", callback_data="toggle_coin_SOL"),
            InlineKeyboardButton("XRP авто", callback_data="toggle_coin_XRP"),
        ],
        [InlineKeyboardButton("🔄 Вкл/Выкл авто", callback_data="set_auto_toggle")],
        [
            InlineKeyboardButton(
                "🔔 Вкл/Выкл уведомления", callback_data="set_notify_toggle"
            )
        ],
        [
            InlineKeyboardButton("⏱ -1 мин", callback_data="set_price_int_dec"),
            InlineKeyboardButton("⏱ +1 мин", callback_data="set_price_int_inc"),
        ],
        [
            InlineKeyboardButton("📉 SL -0.1%", callback_data="set_sl_dec"),
            InlineKeyboardButton("📈 SL +0.1%", callback_data="set_sl_inc"),
        ],
        [
            InlineKeyboardButton("📉 TP -0.1%", callback_data="set_tp_dec"),
            InlineKeyboardButton("📈 TP +0.1%", callback_data="set_tp_inc"),
        ],
        [
            InlineKeyboardButton("📉 RSI Min", callback_data="set_rsi_min_dec"),
            InlineKeyboardButton("📈 RSI Min", callback_data="set_rsi_min_inc"),
        ],
        [
            InlineKeyboardButton("📉 RSI Max", callback_data="set_rsi_max_dec"),
            InlineKeyboardButton("📈 RSI Max", callback_data="set_rsi_max_inc"),
        ],
        [
            InlineKeyboardButton("📉 Объём", callback_data="set_vol_dec"),
            InlineKeyboardButton("📈 Объём", callback_data="set_vol_inc"),
        ],
        [
            InlineKeyboardButton("📉 ATR 0.1%", callback_data="set_atr_dec"),
            InlineKeyboardButton("📈 ATR 0.1%", callback_data="set_atr_inc"),
        ],
        [
            InlineKeyboardButton(
                "📉 Interval", callback_data="set_min_interval_sec_dec"
            ),
            InlineKeyboardButton(
                "📈 Interval", callback_data="set_min_interval_sec_inc"
            ),
        ],
        [
            InlineKeyboardButton(
                "📉 Мин. ордер", callback_data="set_min_order_usd_dec"
            ),
            InlineKeyboardButton(
                "📈 Мин. ордер", callback_data="set_min_order_usd_inc"
            ),
        ],
        [InlineKeyboardButton("⏪ Назад", callback_data="settings_back")],
    ]
    reply_markup = with_start_button(InlineKeyboardMarkup(keyboard))

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def show_deposit_settings_menu(message):
    dep_lines = []
    for code, sym in COINS.items():
        dep = TRADE_DEPOSITS.get(sym, 0.0)
        dep_lines.append(f"{code}: {dep:.0f} USDT")
    deps_text = "\n".join(dep_lines)

    text = (
        f"💰 <b>Настройки депозитов</b>\n\n"
        f"<b>Депозит на монету:</b>\n{deps_text}\n\n"
        f"Кнопками можно менять депозит на +/- 1 USDT для каждой пары."
    )

    keyboard = [
        [
            InlineKeyboardButton("BTC -1", callback_data="dep_BTC_dec"),
            InlineKeyboardButton("BTC +1", callback_data="dep_BTC_inc"),
        ],
        [
            InlineKeyboardButton("ETH -1", callback_data="dep_ETH_dec"),
            InlineKeyboardButton("ETH +1", callback_data="dep_ETH_inc"),
        ],
        [
            InlineKeyboardButton("SOL -1", callback_data="dep_SOL_dec"),
            InlineKeyboardButton("SOL +1", callback_data="dep_SOL_inc"),
        ],
        [
            InlineKeyboardButton("XRP -1", callback_data="dep_XRP_dec"),
            InlineKeyboardButton("XRP +1", callback_data="dep_XRP_inc"),
        ],
        [InlineKeyboardButton("⏪ Назад", callback_data="settings_back")],
    ]
    reply_markup = with_start_button(InlineKeyboardMarkup(keyboard))

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_settings_change(query, context):
    global STRATEGY_CONFIG, ACTIVE_COINS, TRADE_DEPOSITS
    data = query.data

    if data.startswith("dep_"):
        _, code, action = data.split("_")
        symbol = COINS.get(code)
        if not symbol:
            await query.answer("Неизвестная монета")
            return

        cur = TRADE_DEPOSITS.get(symbol, 0.0)
        if action == "dec":
            cur = max(1.0, cur - 1.0)
        elif action == "inc":
            cur = min(100000.0, cur + 1.0)

        TRADE_DEPOSITS[symbol] = cur
        await query.answer(f"{code} депо: {cur:.0f} USDT")
        save_state()
        await show_deposit_settings_menu(query.message)
        return

    if data.startswith("toggle_coin_"):
        coin_code = data.split("_", 2)[2]
        symbol = COINS.get(coin_code)
        if symbol:
            ACTIVE_COINS[symbol] = not ACTIVE_COINS.get(symbol, True)
            await query.answer(
                f"{coin_code}: {'ВКЛ' if ACTIVE_COINS[symbol] else 'ВЫКЛ'}"
            )
        save_state()
        await show_trading_settings_menu(query.message)
        return

    if data == "set_auto_toggle":
        STRATEGY_CONFIG["auto_enabled"] = not STRATEGY_CONFIG["auto_enabled"]
        await query.answer(
            f"Авто-торговля: {'ВКЛ' if STRATEGY_CONFIG['auto_enabled'] else 'ВЫКЛ'}"
        )

    elif data == "set_notify_toggle":
        STRATEGY_CONFIG["notifications_enabled"] = not STRATEGY_CONFIG[
            "notifications_enabled"
        ]
        await query.answer(
            f"Уведомления: {'ВКЛ' if STRATEGY_CONFIG['notifications_enabled'] else 'ВЫКЛ'}"
        )

    elif data == "set_price_int_dec":
        STRATEGY_CONFIG["price_update_interval_sec"] = max(
            60, STRATEGY_CONFIG["price_update_interval_sec"] - 60
        )
        await query.answer(
            f"Интервал цен: {STRATEGY_CONFIG['price_update_interval_sec']//60} мин"
        )

    elif data == "set_price_int_inc":
        STRATEGY_CONFIG["price_update_interval_sec"] = min(
            3600, STRATEGY_CONFIG["price_update_interval_sec"] + 60
        )
        await query.answer(
            f"Интервал цен: {STRATEGY_CONFIG['price_update_interval_sec']//60} мин"
        )

    elif data == "set_sl_dec":
        STRATEGY_CONFIG["sl_pct"] = max(0.005, STRATEGY_CONFIG["sl_pct"] - 0.001)
        await query.answer(f"SL: {STRATEGY_CONFIG['sl_pct']*100:.1f}%")
    elif data == "set_sl_inc":
        STRATEGY_CONFIG["sl_pct"] = min(0.1, STRATEGY_CONFIG["sl_pct"] + 0.001)
        await query.answer(f"SL: {STRATEGY_CONFIG['sl_pct']*100:.1f}%")

    elif data == "set_tp_dec":
        STRATEGY_CONFIG["tp_pct"] = max(0.01, STRATEGY_CONFIG["tp_pct"] - 0.001)
        await query.answer(f"TP: {STRATEGY_CONFIG['tp_pct']*100:.1f}%")
    elif data == "set_tp_inc":
        STRATEGY_CONFIG["tp_pct"] = min(0.2, STRATEGY_CONFIG["tp_pct"] + 0.001)
        await query.answer(f"TP: {STRATEGY_CONFIG['tp_pct']*100:.1f}%")

    elif data == "set_rsi_min_dec":
        STRATEGY_CONFIG["rsi_min"] = max(30, STRATEGY_CONFIG["rsi_min"] - 5)
        await query.answer(f"RSI Min: {STRATEGY_CONFIG['rsi_min']}")
    elif data == "set_rsi_min_inc":
        STRATEGY_CONFIG["rsi_min"] = min(90, STRATEGY_CONFIG["rsi_min"] + 5)
        await query.answer(f"RSI Min: {STRATEGY_CONFIG['rsi_min']}")
    elif data == "set_rsi_max_dec":
        STRATEGY_CONFIG["rsi_max"] = max(30, STRATEGY_CONFIG["rsi_max"] - 5)
        await query.answer(f"RSI Max: {STRATEGY_CONFIG['rsi_max']}")
    elif data == "set_rsi_max_inc":
        STRATEGY_CONFIG["rsi_max"] = min(90, STRATEGY_CONFIG["rsi_max"] + 5)
        await query.answer(f"RSI Max: {STRATEGY_CONFIG['rsi_max']}")

    elif data == "set_vol_dec":
        STRATEGY_CONFIG["volume_mult"] = max(1.0, STRATEGY_CONFIG["volume_mult"] - 0.1)
        await query.answer(f"Объём: {STRATEGY_CONFIG['volume_mult']:.1f}x")
    elif data == "set_vol_inc":
        STRATEGY_CONFIG["volume_mult"] = min(5.0, STRATEGY_CONFIG["volume_mult"] + 0.1)
        await query.answer(f"Объём: {STRATEGY_CONFIG['volume_mult']:.1f}x")

    elif data == "set_atr_dec":
        STRATEGY_CONFIG["atr_threshold_pct"] = max(
            0.001, STRATEGY_CONFIG["atr_threshold_pct"] - 0.001
        )
        await query.answer(
            f"ATR порог: {STRATEGY_CONFIG['atr_threshold_pct']*100:.1f}%"
        )
    elif data == "set_atr_inc":
        STRATEGY_CONFIG["atr_threshold_pct"] = min(
            0.05, STRATEGY_CONFIG["atr_threshold_pct"] + 0.001
        )
        await query.answer(
            f"ATR порог: {STRATEGY_CONFIG['atr_threshold_pct']*100:.1f}%"
        )

    elif data == "set_min_interval_sec_dec":
        STRATEGY_CONFIG["min_interval_sec"] = max(
            0, STRATEGY_CONFIG["min_interval_sec"] - 10
        )
        await query.answer(f"Интервал сделок: {STRATEGY_CONFIG['min_interval_sec']} сек")
    elif data == "set_min_interval_sec_inc":
        STRATEGY_CONFIG["min_interval_sec"] = min(
            300, STRATEGY_CONFIG["min_interval_sec"] + 10
        )
        await query.answer(f"Интервал сделок: {STRATEGY_CONFIG['min_interval_sec']} сек")

    elif data == "set_min_order_usd_dec":
        STRATEGY_CONFIG["min_order_usd"] = max(1, STRATEGY_CONFIG["min_order_usd"] - 1)
        await query.answer(f"Мин. ордер: ${STRATEGY_CONFIG['min_order_usd']}")
    elif data == "set_min_order_usd_inc":
        STRATEGY_CONFIG["min_order_usd"] = min(
            100, STRATEGY_CONFIG["min_order_usd"] + 1
        )
        await query.answer(f"Мин. ордер: ${STRATEGY_CONFIG['min_order_usd']}")

    save_state()
    await show_trading_settings_menu(query.message)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    keyboard = [
        [InlineKeyboardButton("⚙️ Настройки торговли", callback_data="settings_trading")],
        [InlineKeyboardButton("💰 Настройки депозита", callback_data="settings_deposits")],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    await update.message.reply_text(
        "⚙️ <b>Настройки</b>",
        reply_markup=with_start_button(InlineKeyboardMarkup(keyboard)),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ================== AUTO-TRADING ==================
async def execute_trade(symbol, signal, price, ex, cfg):
    global positions, last_trade_time, TRADE_DEPOSITS

    pos = positions[symbol]

    if signal == "BUY" and not pos["in_position"]:
        balance = ex.fetch_balance()
        usdt = float(balance.get("USDT", {}).get("free", 0))

        amount_usd = TRADE_DEPOSITS.get(symbol, cfg["min_order_usd"])
        amount_usd = max(amount_usd, cfg["min_order_usd"])

        if usdt < amount_usd:
            print(
                f"[{symbol}] Недостаточно USDT: {usdt:,.2f} < {amount_usd:.2f}"
            )
            return

        amount = amount_usd / price
        amount_str = ex.amount_to_precision(symbol, max(amount, 0.000001))

        try:
            order = ex.create_market_buy_order(symbol, amount_str)
            filled = float(order.get("filled", 0))
            avg_price = float(order.get("average") or price)
            if filled > 0:
                usd_spent = filled * avg_price

                pos.update(
                    {
                        "in_position": True,
                        "entry_price": avg_price,
                        "amount": filled,
                        "buy_time": datetime.now(timezone.utc).isoformat(),
                    }
                )
                save_state()
                log_trade(
                    {
                        "type": "AUTO_BUY",
                        "symbol": symbol,
                        "price": avg_price,
                        "amount": filled,
                        "usd_value": usd_spent,
                        "pnl_pct": 0.0,
                        "pnl_usd": 0.0,
                    }
                )
                chart = plot_mini_chart(symbol, ex.fetch_ohlcv(symbol, "1m", limit=50))
                msg = (
                    f"✅ <b>АВТО-ПОКУПКА</b>\n"
                    f"{symbol} @ <b>{avg_price:,.4f}</b>\n"
                    f"Объём (USDT): {usd_spent:.2f}\n"
                    f"Объём: {filled:.6f}\n"
                    f"SL: {avg_price * (1 - cfg['sl_pct']):.4f} | "
                    f"TP: {avg_price * (1 + cfg['tp_pct']):.4f}"
                )
                await send_telegram(msg, photo=chart)
                print(f"[{symbol}] AUTO BUY: {filled:.6f} @ {avg_price:,.4f}")
                last_trade_time[symbol] = time.time()
        except Exception as e:
            print(f"[{symbol}] BUY Error: {e}")

    elif signal == "SELL" and pos["in_position"]:
        try:
            # базовая монета (BTC, ETH, SOL, XRP)
            base_code = symbol.split("/")[0]
            balance = ex.fetch_balance()
            base_info = balance.get(base_code, {})
            avail_amount = float(base_info.get("free") or 0)

            # продаём по фактическому доступному балансу, но не больше, чем думает бот
            sell_amount = min(pos["amount"], avail_amount) * 0.999  # небольшой запас

            if sell_amount <= 0:
                print(f"[{symbol}] SELL: нет доступного баланса, помечаем позицию как закрытую")
                pos["in_position"] = False
                save_state()
                await send_telegram(
                    f"⚠️ <b>Не удалось продать {symbol}</b>\n"
                    f"Биржа показывает нулевой доступный баланс.\n"
                    f"Позиция помечена как закрытая в боте.\n\n"
                    f"/start"
                )
                return

            amount_str = ex.amount_to_precision(symbol, sell_amount)

            try:
                order = ex.create_market_sell_order(symbol, amount_str)
            except Exception as e:
                msg = str(e)
                if "balance not enough" in msg:
                    # чтобы не зациклиться: помечаем как закрытую и предупреждаем
                    pos["in_position"] = False
                    save_state()
                    await send_telegram(
                        f"⚠️ <b>SELL ошибка по {symbol}</b>\n"
                        f"Биржа пишет, что баланса не хватает для продажи.\n"
                        f"Проверь позицию вручную на бирже.\n\n"
                        f"/start"
                    )
                    print(f"[{symbol}] SELL balance error: {msg}")
                    return
                else:
                    print(f"[{symbol}] SELL Error: {msg}")
                    return

            avg_price = float(order.get("average") or price)
            filled = float(order.get("filled", 0))
            if filled > 0:
                entry_price = pos["entry_price"]
                usd_received = filled * avg_price
                pnl_pct = (avg_price / entry_price - 1) * 100
                pnl_usd = usd_received - filled * entry_price

                log_trade(
                    {
                        "type": "AUTO_SELL",
                        "symbol": symbol,
                        "price": avg_price,
                        "amount": -filled,
                        "usd_value": usd_received,
                        "pnl_pct": pnl_pct,
                        "pnl_usd": pnl_usd,
                    }
                )
                adaptive_on_trade(symbol, "AUTO_SELL", pnl_pct)
                pos["in_position"] = False
                save_state()
                msg = (
                    f"✅ <b>АВТО-ПРОДАЖА</b>\n"
                    f"{symbol} @ <b>{avg_price:,.4f}</b>\n"
                    f"Объём (USDT): {usd_received:.2f}\n"
                    f"P&L: <b>{pnl_pct:+.2f}%</b> | <b>${pnl_usd:+.2f}</b>"
                )
                await send_telegram(msg)
                print(f"[{symbol}] AUTO SELL: P&L {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
                last_trade_time[symbol] = time.time()
        except Exception as e:
            print(f"[{symbol}] SELL Error (outer): {e}")


async def send_all_price_update(current_prices_dict):
    global last_price, price_initialized, day_open_price

    msg_lines = ["🔔 <b>Обновление цен</b>\n"]

    for symbol in SYMBOLS:
        price = current_prices_dict.get(symbol)
        if not price:
            continue

        pair_code = symbol.replace("/", "")
        pair_url = PAIR_URL_TEMPLATE.format(pair=pair_code)
        pair_link = f'<a href="{pair_url}">{symbol}</a>'

        if day_open_price[symbol] and day_open_price[symbol] > 0:
            change_pct = (price - day_open_price[symbol]) / day_open_price[symbol] * 100
        else:
            if last_price[symbol] == 0:
                change_pct = 0.0
            else:
                change_pct = (price - last_price[symbol]) / last_price[symbol] * 100

        arrow = "📈" if change_pct >= 0 else "📉"
        change_str = (
            f"{change_pct:+.2f}%" if abs(change_pct) >= 0.01 else f"{change_pct:+.4f}%"
        )

        msg_lines.append(f"{pair_link}: <b>{price:,.4f}</b> {arrow} ({change_str})")

        last_price[symbol] = price
        price_initialized[symbol] = True

    total_pct, total_usd = get_pnl_today()
    pnl_pct_display = total_pct if abs(total_pct) >= 0.01 else 0.00
    pnl_usd_display = total_usd if abs(total_usd) >= 0.01 else 0.00

    msg_lines.append(
        "\n📊 <b>P&L за сегодня (МСК)</b>\n"
        f"В %: <b>{pnl_pct_display:+.2f}%</b>\n"
        f"В USDT: <b>${pnl_usd_display:+.2f}</b>\n\n"
        f"/start"
    )

    msg = "\n".join(msg_lines)
    await send_telegram(msg)
    save_state()


async def detect_manual_trades(ex):
    """
    Проверяем сделки на бирже, находим новые и присылаем сообщение.
    НЕ трогаем positions и таблицу trades — это просто мониторинг.
    """
    global manual_seen_trade_ids

    for symbol in SYMBOLS:
        try:
            trades = ex.fetch_my_trades(symbol, limit=20)
        except Exception as e:
            print(f"[{symbol}] fetch_my_trades error: {e}")
            continue

        if not trades:
            continue

        try:
            ticker = ex.fetch_ticker(symbol)
            cur_price = float(ticker["last"])
        except Exception:
            cur_price = None

        for t in trades:
            tid = t.get("id") or t.get("tradeId")
            if tid is None:
                continue

            if tid in manual_seen_trade_ids:
                continue  # уже видели

            manual_seen_trade_ids.add(tid)

            ts = t.get("timestamp")
            is_bot_like = False
            if ts is not None and last_trade_time.get(symbol):
                if abs(ts / 1000 - last_trade_time[symbol]) < 10:
                    is_bot_like = True

            if is_bot_like:
                continue

            side = (t.get("side") or "").upper()
            price = float(t.get("price") or 0.0)
            amount = float(t.get("amount") or 0.0)
            if price <= 0 or amount <= 0:
                continue

            usd_value = price * amount

            pnl_pct = 0.0
            if cur_price and cur_price > 0:
                if side == "BUY":
                    pnl_pct = (cur_price / price - 1) * 100
                elif side == "SELL":
                    pnl_pct = (price / cur_price - 1) * 100

            pair_code = symbol.replace("/", "")
            pair_url = PAIR_URL_TEMPLATE.format(pair=pair_code)
            pair_link = f'<a href="{pair_url}">{symbol}</a>'

            msg_lines = [
                "🔎 <b>Обнаружена сделка на бирже</b>",
                f"{pair_link} {side} @ <b>{price:,.4f}</b>",
                f"Объём: {amount:.6f} (~{usd_value:.2f} USDT)",
            ]
            if cur_price:
                msg_lines.append(f"Текущая цена: <b>{cur_price:,.4f}</b>")
                msg_lines.append(f"Текущий результат: <b>{pnl_pct:+.2f}%</b>")
            msg_lines.append("")
            msg_lines.append("/start")

            msg = "\n".join(msg_lines)
            await send_telegram(msg)

    save_state()


async def reconcile_positions(ex):
    """
    Синхронизация: если бот думает, что в позиции, но на бирже монеты почти нет —
    считаем, что позиция закрыта вручную на бирже.
    """
    try:
        balance = ex.fetch_balance()
    except Exception as e:
        print(f"reconcile_positions: fetch_balance error: {e}")
        return

    for code, symbol in COINS.items():
        pos = positions[symbol]
        if not pos["in_position"]:
            continue

        base_info = balance.get(code, {})
        base_free = float(base_info.get("free", 0) or 0)
        base_used = float(base_info.get("used", 0) or 0)
        total_base = base_free + base_used

        # если реальный объём на бирже меньше 10% от того, что считает бот — считаем, что позиция закрыта вручную
        if total_base + 1e-8 < pos["amount"] * 0.1:
            try:
                ticker = ex.fetch_ticker(symbol)
                close_price = float(ticker["last"])
            except Exception:
                close_price = pos["entry_price"]

            entry_price = pos["entry_price"]
            amount = pos["amount"]

            pnl_pct = (close_price / entry_price - 1) * 100
            pnl_usd = (close_price - entry_price) * amount

            log_trade(
                {
                    "type": "MANUAL_EXTERNAL_CLOSE",
                    "symbol": symbol,
                    "price": close_price,
                    "amount": -amount,
                    "usd_value": close_price * amount,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                }
            )
            adaptive_on_trade(symbol, "MANUAL_EXTERNAL_CLOSE", pnl_pct)

            pos["in_position"] = False
            save_state()

            msg = (
                f"⚠️ <b>Позиция бота закрыта вручную на бирже</b>\n"
                f"{symbol}\n"
                f"Вход: {entry_price:,.4f}\n"
                f"Расчётная цена выхода: {close_price:,.4f}\n"
                f"Оценочный P&L: <b>{pnl_pct:+.2f}%</b> | <b>${pnl_usd:+.2f}</b>\n\n"
                f"/start"
            )
            await send_telegram(msg)


def generate_signal(symbol, current, df, ex, cfg=None):
    if cfg is None:
        cfg = get_symbol_config(symbol)
    now = time.time()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if now - last_trade_time[symbol] < cfg["min_interval_sec"]:
        return None

    pos = positions[symbol]

    sl_price = pos["entry_price"] * (1 - cfg["sl_pct"]) if pos["in_position"] else None
    tp_price = pos["entry_price"] * (1 + cfg["tp_pct"]) if pos["in_position"] else None

    basic_buy = (
        (not pos["in_position"])
        and last["close"] > last["ema9"] > last["ema21"]
        and cfg["rsi_min"] < last["rsi"] < cfg["rsi_max"]
        and last["volume"] > last["avg_volume"] * cfg["volume_mult"]
        and last["close"] > prev["close"] * (1 + cfg["price_growth_min"])
    )

    basic_sell = (
        pos["in_position"]
        and cfg["auto_enabled"]
        and (
            (sl_price is not None and last["close"] <= sl_price)
            or (tp_price is not None and last["close"] >= tp_price)
            or last["rsi"] > 75
            or last["close"] < last["ema9"] * 0.999
        )
    )

    market_ctx = get_market_context(symbol, last["close"], cfg, ex)
    if market_ctx is None:
        if basic_buy:
            return "BUY"
        if basic_sell:
            return "SELL"
        return None

    if not market_ctx["trade_allowed"]:
        return None

    support = market_ctx["support"]
    resistance = market_ctx["resistance"]
    fib_382 = market_ctx["fib_382"]
    fib_618 = market_ctx["fib_618"]
    atr_14 = market_ctx["atr_14"]

    level_buy = (
        not pos["in_position"]
        and support * 0.999 <= last["close"] <= support * 1.001
        and last["rsi"] > 40
    )

    level_sell = (
        pos["in_position"]
        and resistance * 0.999 <= last["close"] <= resistance * 1.001
        and last["rsi"] > 60
    )

    fib_buy = (
        not pos["in_position"]
        and fib_382 is not None
        and atr_14 > 0
        and abs(last["close"] - fib_382) < atr_14
        and last["close"] > last["open"]
    )

    fib_sell = (
        pos["in_position"]
        and fib_618 is not None
        and atr_14 > 0
        and abs(last["close"] - fib_618) < atr_14
        and last["close"] < last["open"]
    )

    if level_buy or fib_buy:
        return "BUY"

    if level_sell or fib_sell:
        return "SELL"

    if basic_buy:
        return "BUY"

    if basic_sell:
        return "SELL"

    return None


async def trading_loop():
    global last_price, price_initialized, last_trade_time, running
    global day_open_price, day_open_msk_date
    global last_price_update_time, current_prices

    print("Торговый цикл запущен")

    try:
        ex = get_exchange()
        for sym in SYMBOLS:
            ticker = ex.fetch_ticker(sym)
            last_price[sym] = ticker["last"]
            price_initialized[sym] = True
            print(f"Стартовая цена {sym}: {last_price[sym]:,.4f}")
        save_state()
    except Exception as e:
        print(f"Ошибка инициализации: {e}")

    while running:
        try:
            ex = get_exchange()
            now_utc = datetime.now(timezone.utc)
            now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)
            today_msk_str = now_msk.strftime("%Y-%m-%d")
            current_time = time.time()

            for symbol in SYMBOLS:
                if not ACTIVE_COINS.get(symbol, True):
                    continue

                ohlcv = ex.fetch_ohlcv(symbol, "1m", limit=50)
                current, df = calculate_indicators(ohlcv)
                current_price = current["close"]

                current_prices[symbol] = current_price

                if day_open_msk_date[symbol] != today_msk_str or day_open_price[symbol] == 0.0:
                    day_open_price[symbol] = current_price
                    day_open_msk_date[symbol] = today_msk_str
                    print(
                        f"[{symbol}] Цена открытия дня МСК {today_msk_str}: {day_open_price[symbol]:.4f}"
                    )
                    save_state()

                cfg = get_symbol_config(symbol)
                signal = generate_signal(symbol, current, df, ex, cfg)
                if signal and cfg.get("auto_enabled", True):
                    last_trade_time[symbol] = current_time
                    await execute_trade(symbol, signal, current_price, ex, cfg)

            # Детектор ручных (биржевых) сделок
            await detect_manual_trades(ex)

            # Сверяем позиции бота с реальным балансом на бирже
            await reconcile_positions(ex)

            if STRATEGY_CONFIG["notifications_enabled"]:
                if current_time - last_price_update_time > STRATEGY_CONFIG["price_update_interval_sec"]:
                    await send_all_price_update(current_prices)
                    last_price_update_time = current_time

            # Мониторинг памяти каждые 5 минут
            if int(current_time) % 300 == 0:
                log_memory_usage()

            await asyncio.sleep(60)

        except Exception as e:
            print(f"Trading Error: {e}")
            await asyncio.sleep(60)


# ================== POSITIONS MENU (ВЫХОД ИЗ СДЕЛОК) ==================
async def show_positions_menu(message):
    ex = get_exchange()
    lines = ["🚪 <b>Управление позициями</b>\n"]

    for code, symbol in COINS.items():
        pos = positions[symbol]
        if pos["in_position"]:
            try:
                ticker = ex.fetch_ticker(symbol)
                price = float(ticker["last"])
            except Exception:
                price = pos["entry_price"] or 0.0

            usd_val = pos["amount"] * price
            lines.append(
                f"{code}: <b>В позиции</b>\n"
                f"  Вход: {pos['entry_price']:.4f}\n"
                f"  Кол-во: {pos['amount']:.6f} (~{usd_val:.2f} USDT)"
            )
        else:
            lines.append(f"{code}: нет открытой позиции")

    text = "\n".join(lines)

    keyboard = []
    row = []
    for code, symbol in COINS.items():
        if positions[symbol]["in_position"]:
            row.append(
                InlineKeyboardButton(f"Закрыть {code}", callback_data=f"close_{code}")
            )
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row:
        keyboard.append(row)

    keyboard.append(
        [InlineKeyboardButton("❗ Закрыть все сделки", callback_data="close_all")]
    )
    keyboard.append([InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")])

    reply_markup = with_start_button(InlineKeyboardMarkup(keyboard))

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def close_single_position(query, coin_code: str):
    symbol = COINS.get(coin_code)
    if not symbol:
        await query.answer("Неизвестная монета")
        return

    pos = positions[symbol]
    if not pos["in_position"]:
        await query.message.reply_text(
            f"❌ По {coin_code} нет открытой позиции",
            reply_markup=with_start_button(),
        )
        return

    try:
        ex = get_exchange()
        amount_str = ex.amount_to_precision(symbol, pos["amount"])
        order = ex.create_market_sell_order(symbol, amount_str)
        avg_price = float(order.get("average") or ex.fetch_ticker(symbol)["last"])
        filled = float(order.get("filled", 0))

        if filled > 0:
            entry_price = pos["entry_price"]
            usd_received = filled * avg_price
            pnl_pct = (avg_price / entry_price - 1) * 100
            pnl_usd = usd_received - filled * entry_price

            log_trade(
                {
                    "type": "MANUAL_SELL",
                    "symbol": symbol,
                    "price": avg_price,
                    "amount": -filled,
                    "usd_value": usd_received,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                }
            )
            adaptive_on_trade(symbol, "MANUAL_SELL", pnl_pct)
            pos["in_position"] = False
            save_state()

            msg = (
                f"✅ <b>Ручное закрытие позиции</b>\n"
                f"{symbol} @ <b>{avg_price:,.4f}</b>\n"
                f"Объём (USDT): {usd_received:.2f}\n"
                f"P&L: <b>{pnl_pct:+.2f}%</b> | <b>${pnl_usd:+.2f}</b>"
            )
            await query.message.reply_text(
                msg, parse_mode="HTML", reply_markup=with_start_button()
            )
        else:
            await query.message.reply_text(
                f"❌ Не удалось закрыть позицию по {symbol}",
                reply_markup=with_start_button(),
            )
    except Exception as e:
        await query.message.reply_text(
            f"❌ Ошибка закрытия {symbol}: {e}",
            reply_markup=with_start_button(),
        )


async def close_all_positions(query):
    any_closed = False
    for code, symbol in COINS.items():
        if positions[symbol]["in_position"]:
            await close_single_position(query, code)
            any_closed = True

    if not any_closed:
        await query.message.reply_text(
            "Нет открытых позиций по всем монетам.",
            reply_markup=with_start_button(),
        )


# ================== BUTTON HANDLER ==================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "report":
        await handle_report(query)
    elif data == "risk_soft":
        set_soft_start_mode()
        await query.message.reply_text(
            "Режим входа: <b>МЯГКИЙ</b>\n"
            f"Первые {MIN_TRADES_BEFORE_AUTO} сделок по монете будут мягче, затем автоадаптация.",
            parse_mode="HTML",
            reply_markup=with_start_button(),
        )
    elif data == "risk_hard":
        set_hard_start_mode()
        await query.message.reply_text(
            "Режим входа: <b>ЖЁСТКИЙ</b>\n"
            f"Первые {MIN_TRADES_BEFORE_AUTO} сделок по монете будут осторожнее, затем автоадаптация.",
            parse_mode="HTML",
            reply_markup=with_start_button(),
        )
    elif data.startswith("report_day_"):
        await handle_report(query, data.split("report_day_", 1)[1])
    elif data == "pnl":
        await show_pnl_per_symbol(query)
    elif data == "statistics":
        await show_statistics(query)
    elif data == "market":
        await cmd_market(update, context)
    elif data == "settings":
        await show_settings_menu(query.message)
    elif data == "settings_trading":
        await show_trading_settings_menu(query.message)
    elif data == "settings_deposits":
        await show_deposit_settings_menu(query.message)
    elif data == "settings_back":
        await show_settings_menu(query.message)
    elif data == "back_to_main":
        await start_from_callback(update, context)
    elif data == "positions_menu":
        await show_positions_menu(query.message)
    elif data.startswith("close_"):
        code = data.split("_", 1)[1]
        await close_single_position(query, code)
    elif data == "close_all":
        await close_all_positions(query)
    elif data.startswith(("set_", "toggle_coin_", "dep_")):
        await handle_settings_change(query, context)


# ================== MAIN LOOP ==================
def signal_handler(sig, frame):
    global running
    print("Останавливаем бота...")
    running = False


async def main():
    global running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    check_env()
    init_db()
    migrate_csv_to_db()
    load_state()
    init_adaptive_state()
    await send_telegram("✅ <b>Торговый бот запущен!</b>\n/start для управления")
    print("Стартовое сообщение отправлено")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    trading_task = asyncio.create_task(trading_loop())

    print("Бот полностью запущен. Для остановки Ctrl+C")
    while running:
        await asyncio.sleep(1)

    trading_task.cancel()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    print("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Завершение по запросу пользователя")
