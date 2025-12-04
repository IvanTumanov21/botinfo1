import os
import sqlite3
import signal
import io
import json
import time
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time

import asyncio

import ccxt
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ================== БАЗОВЫЕ НАСТРОЙКИ ==================

matplotlib.use("Agg")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "botinfo.db"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("API_KEY")
SECRET = os.getenv("SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_CHAT_ID_ENV:
    raise RuntimeError("TELEGRAM_CHAT_ID не задан. Проверь .env")

try:
    TELEGRAM_CHAT_ID = int(TELEGRAM_CHAT_ID_ENV)
except ValueError:
    raise RuntimeError("TELEGRAM_CHAT_ID должен быть числом (ID твоего Telegram).")

MOSCOW_OFFSET_HOURS = 3


def check_env():
    missing = []
    if not API_KEY:
        missing.append("API_KEY")
    if not SECRET:
        missing.append("SECRET")
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not TELEGRAM_CHAT_ID_ENV:
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_utc);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);")

    conn.commit()
    conn.close()


def log_trade(
    trade_type: str,
    symbol: str,
    price: float,
    amount: float,
    usd_value: float,
    pnl_pct: float = 0.0,
    pnl_usd: float = 0.0,
):
    """
    Пишем сделку в SQLite (botinfo.db, таблица trades).
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    time_utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    cur.execute(
        """
        INSERT INTO trades (type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trade_type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc),
    )
    conn.commit()
    conn.close()


def _get_window_utc_for_msk_day(offset_days: int = 0):
    """
    Возвращает (start_utc, end_utc) для дня по МСК.

    offset_days = 0 -> сегодня
    offset_days = -1 -> вчера
    """
    now_utc = datetime.utcnow()
    now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)
    target_date = now_msk.date() + timedelta(days=offset_days)
    start_msk = datetime.combine(target_date, dt_time(0, 0, 1))
    end_msk = datetime.combine(target_date, dt_time(23, 59, 59))

    start_utc = start_msk - timedelta(hours=MOSCOW_OFFSET_HOURS)
    end_utc = end_msk - timedelta(hours=MOSCOW_OFFSET_HOURS)
    return start_utc, end_utc


def _get_window_utc_for_last_days(days: int):
    """
    Последние N дней по МСК, до текущего момента.
    """
    now_utc = datetime.utcnow()
    now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)
    end_msk = now_msk
    start_msk = end_msk - timedelta(days=days)
    start_utc = start_msk - timedelta(hours=MOSCOW_OFFSET_HOURS)
    end_utc = end_msk - timedelta(hours=MOSCOW_OFFSET_HOURS)
    return start_utc, end_utc


def fetch_trades_window(start_utc: datetime, end_utc: datetime):
    """
    Забрать сделки из БД за окно [start_utc, end_utc].
    Возвращает список dict.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    s = start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    e = end_utc.strftime("%Y-%m-%dT%H:%M:%S")

    cur.execute(
        """
        SELECT id, type, symbol, price, amount, usd_value, pnl_pct, pnl_usd, time_utc
        FROM trades
        WHERE time_utc BETWEEN ? AND ?
        ORDER BY time_utc ASC
        """,
        (s, e),
    )
    rows = cur.fetchall()
    conn.close()

    return [dict(r) for r in rows]


SELL_KEYWORDS = [
    "SELL",
    "STOP",
    "TAKE",
    "FORCE",
    "MANUAL_POSITION_CLOSE",
    "MANUAL_EXTERNAL_CLOSE",
]


def get_pnl_today():
    """
    P&L за сегодня (МСК) по всем монетам, из SQLite.
    """
    start_utc, end_utc = _get_window_utc_for_msk_day(0)
    trades = fetch_trades_window(start_utc, end_utc)

    sells = [
        t
        for t in trades
        if any(kw in t["type"] for kw in SELL_KEYWORDS)
    ]
    if not sells:
        return 0.0, 0.0

    total_pct = sum(float(t["pnl_pct"]) for t in sells)
    total_usd = sum(float(t["pnl_usd"]) for t in sells)
    return total_pct, total_usd


def get_pnl_today_per_symbol(symbols):
    """
    P&L за сегодня (МСК) по каждой монете + итого, из SQLite.
    """
    start_utc, end_utc = _get_window_utc_for_msk_day(0)
    trades = fetch_trades_window(start_utc, end_utc)

    sells = [
        t
        for t in trades
        if any(kw in t["type"] for kw in SELL_KEYWORDS)
    ]

    per_symbol = {sym: (0.0, 0.0) for sym in symbols}
    if not sells:
        return per_symbol, 0.0, 0.0

    total_pct = 0.0
    total_usd = 0.0

    for sym in symbols:
        sym_sells = [t for t in sells if t["symbol"] == sym]
        if sym_sells:
            spct = sum(float(t["pnl_pct"]) for t in sym_sells)
            susd = sum(float(t["pnl_usd"]) for t in sym_sells)
            per_symbol[sym] = (spct, susd)
            total_pct += spct
            total_usd += susd

    return per_symbol, total_pct, total_usd


def format_trades_report(trades, title: str) -> str:
    if not trades:
        return f"📊 {title}\n\nСделок в этом периоде не было."

    buys = [t for t in trades if "BUY" in t["type"]]
    sells = [
        t for t in trades if any(kw in t["type"] for kw in SELL_KEYWORDS)
    ]

    total_pct = sum(float(t["pnl_pct"]) for t in sells)
    total_usd = sum(float(t["pnl_usd"]) for t in sells)

    lines = [
        f"📊 <b>{title}</b>\n",
        f"✅ Покупок: {len(buys)}",
        f"📤 Закрытий: {len(sells)}",
        f"📈 Общий P&L: <b>{total_pct:+.2f}%</b>",
        f"💰 В USDT: <b>${total_usd:+.2f}</b>",
        "",
        "<b>Детали сделок:</b>",
    ]

    for t in trades:
        t_type = t["type"]
        price = float(t["price"])
        amount = float(t["amount"])
        usd_value = float(t["usd_value"])
        pnl_pct = float(t["pnl_pct"])
        pnl_usd = float(t["pnl_usd"])

        ts = t.get("time_utc") or ""
        try:
            dt = datetime.fromisoformat(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_str = ts

        if any(kw in t_type for kw in SELL_KEYWORDS):
            sign = "+" if pnl_pct > 0 else ""
            lines.append(
                f"{ts_str} | {t_type} {t['symbol']} @ {price:,.4f} | "
                f"{sign}{pnl_pct:.2f}% | ${pnl_usd:+.2f}"
            )
        else:
            lines.append(
                f"{ts_str} | {t_type} {t['symbol']} @ {price:,.4f} | "
                f"{amount:.6f} | ${usd_value:.2f}"
            )

    return "\n".join(lines)


# ================== ТОРГОВЫЕ НАСТРОЙКИ ==================

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

COINS = {
    "BTC": "BTC/USDT",
    "ETH": "ETH/USDT",
    "SOL": "SOL/USDT",
    "XRP": "XRP/USDT",
}
SYMBOLS = list(COINS.values())
DEFAULT_SYMBOL = COINS["BTC"]

PAIR_URL_TEMPLATE = "https://bingx.com/en/spot/{pair}"

# ================== СОСТОЯНИЕ ==================

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
    COINS["SOL"]: 3.0,
    COINS["XRP"]: 3.0,
}

current_prices = {sym: 0.0 for sym in SYMBOLS}
last_price_update_time = 0

manual_seen_trade_ids = set()

running = True

# ================== УТИЛИТЫ ==================


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
    }
    with open(BASE_DIR / "state.json", "w") as f:
        json.dump(state, f)


def load_state():
    global positions, last_price, STRATEGY_CONFIG
    global day_open_price, day_open_msk_date, ACTIVE_COINS, TRADE_DEPOSITS
    global manual_seen_trade_ids

    state_path = BASE_DIR / "state.json"
    if not state_path.exists():
        return

    try:
        with open(state_path) as f:
            state = json.load(f)

        saved_positions = state.get("positions", {})
        for sym in SYMBOLS:
            if sym in saved_positions:
                positions[sym] = saved_positions[sym]

        saved_last_price = state.get("last_price", {})
        for sym in SYMBOLS:
            if sym in saved_last_price:
                last_price[sym] = saved_last_price[sym]

        saved_cfg = state.get("strategy_config", {})
        for k, v in saved_cfg.items():
            if k in STRATEGY_CONFIG:
                STRATEGY_CONFIG[k] = v

        saved_open = state.get("day_open_price", {})
        for sym in SYMBOLS:
            if sym in saved_open:
                day_open_price[sym] = saved_open[sym]

        saved_dates = state.get("day_open_msk_date", {})
        for sym in SYMBOLS:
            if sym in saved_dates:
                day_open_msk_date[sym] = saved_dates[sym]

        saved_active = state.get("active_coins", {})
        for sym in SYMBOLS:
            if sym in saved_active:
                ACTIVE_COINS[sym] = saved_active[sym]

        saved_deps = state.get("trade_deposits", {})
        for sym in SYMBOLS:
            if sym in saved_deps:
                TRADE_DEPOSITS[sym] = float(saved_deps[sym])

        saved_ids = state.get("manual_seen_trade_ids", [])
        if isinstance(saved_ids, list):
            manual_seen_trade_ids = set(saved_ids)

    except Exception as e:
        print(f"Ошибка загрузки state.json: {e}")


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
    return 0.0


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
            sl = pos["entry_price"] * (1 - STRATEGY_CONFIG["sl_pct"])
            tp = pos["entry_price"] * (1 + STRATEGY_CONFIG["tp_pct"])
            plt.axhline(
                y=pos["entry_price"],
                linestyle="--",
                label=f'Entry: {pos["entry_price"]:,.2f}',
            )
            plt.axhline(y=sl, linestyle="--", label=f"SL: {sl:,.2f}")
            plt.axhline(y=tp, linestyle="-.", label=f"TP: {tp:,.2f}")
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
        return None


# ================== TELEGRAM ==================

async def send_telegram(text, photo=None, reply_markup=None):
    try:
        bot = Application.builder().token(TELEGRAM_TOKEN).build().bot
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
        print(f"TG: {text[:80]}...")
    except Exception as e:
        print(f"TG Error: {e}")


def build_main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Отчёт", callback_data="report_today"),
                InlineKeyboardButton("💰 P&L", callback_data="pnl"),
            ],
            [
                InlineKeyboardButton("📊 Рынок", callback_data="market"),
                InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            ],
            [
                InlineKeyboardButton("📜 История", callback_data="history_menu"),
                InlineKeyboardButton(
                    "🚪 Выход из сделок", callback_data="positions_menu"
                ),
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

    status_lines = []
    for code, symbol in COINS.items():
        pos = positions[symbol]
        circle = "🟢" if pos["in_position"] else "🔴"
        if pos["in_position"]:
            status_lines.append(
                f"{circle} {code}: в позиции @ {pos['entry_price']:,.4f}"
            )
        else:
            status_lines.append(f"{circle} {code}: нет позиции")
    status_text = "\n".join(status_lines)

    text = (
        f"💼 <b>Текущий баланс</b>\n"
        f"USDT: {usdt:,.2f}\n"
        f"BTC: {btc:.6f}\n"
        f"ETH: {eth:.6f}\n"
        f"SOL: {sol:.6f}\n"
        f"XRP: {xrp:.2f}\n\n"
        f"📌 <b>Статус позиций</b>\n"
        f"{status_text}\n\n"
        f"📊 <b>P&L за сегодня (МСК)</b>\n"
        f"В %: {pnl_pct:+.2f}%\n"
        f"В USDT: ${pnl_usd:+.2f}"
    )

    await update.message.reply_text(
        text,
        reply_markup=build_main_keyboard(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    query = update.callback_query
    ex = get_exchange()
    balance = ex.fetch_balance()
    usdt = float(balance.get("USDT", {}).get("free", 0))
    btc = float(balance.get("BTC", {}).get("free", 0))
    eth = float(balance.get("ETH", {}).get("free", 0))
    sol = float(balance.get("SOL", {}).get("free", 0))
    xrp = float(balance.get("XRP", {}).get("free", 0))

    pnl_pct, pnl_usd = get_pnl_today()

    status_lines = []
    for code, symbol in COINS.items():
        pos = positions[symbol]
        circle = "🟢" if pos["in_position"] else "🔴"
        if pos["in_position"]:
            status_lines.append(
                f"{circle} {code}: в позиции @ {pos['entry_price']:,.4f}"
            )
        else:
            status_lines.append(f"{circle} {code}: нет позиции")
    status_text = "\n".join(status_lines)

    text = (
        f"💼 <b>Текущий баланс</b>\n"
        f"USDT: {usdt:,.2f}\n"
        f"BTC: {btc:.6f}\n"
        f"ETH: {eth:.6f}\n"
        f"SOL: {sol:.6f}\n"
        f"XRP: {xrp:.2f}\n\n"
        f"📌 <b>Статус позиций</b>\n"
        f"{status_text}\n\n"
        f"📊 <b>P&L за сегодня (МСК)</b>\n"
        f"В %: {pnl_pct:+.2f}%\n"
        f"В USDT: ${pnl_usd:+.2f}"
    )

    await query.edit_message_text(
        text,
        reply_markup=build_main_keyboard(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ================== MARKET ==================

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
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
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
            vol_ratio = (
                df["volume"].iloc[-1] / avg_vol_24h if avg_vol_24h > 0 else 0.0
            )

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
                msg, parse_mode="HTML", disable_web_page_preview=True
            )
        else:
            await update.callback_query.message.reply_text(
                msg, parse_mode="HTML", disable_web_page_preview=True
            )

    except Exception as e:
        error_msg = f"❌ Ошибка анализа: {e}"
        if update.message:
            await update.message.reply_text(error_msg)
        else:
            await update.callback_query.message.reply_text(error_msg)


# ================== REPORT / P&L / HISTORY ==================

async def handle_report_today(query):
    start_utc, end_utc = _get_window_utc_for_msk_day(0)
    trades = fetch_trades_window(start_utc, end_utc)
    if not trades:
        await query.message.reply_text("📊 Сделок за сегодня (МСК) не было")
        return

    text = format_trades_report(trades, "Отчёт за сегодня (МСК)")
    await query.message.reply_text(text, parse_mode="HTML")


async def show_pnl_per_symbol(query):
    per_symbol, total_pct, total_usd = get_pnl_today_per_symbol(SYMBOLS)

    lines = ["💰 <b>P&L за сегодня (МСК)</b>\n", "<b>По монетам:</b>"]
    for code, symbol in COINS.items():
        spct, susd = per_symbol.get(symbol, (0.0, 0.0))
        spct_disp = spct if abs(spct) >= 0.01 else 0.0
        susd_disp = susd if abs(susd) >= 0.01 else 0.0
        lines.append(f"{code}: {spct_disp:+.2f}% | ${susd_disp:+.2f}")

    total_pct_disp = total_pct if abs(total_pct) >= 0.01 else 0.0
    total_usd_disp = total_usd if abs(total_usd) >= 0.01 else 0.0
    lines.append(f"\n<b>Итого:</b> {total_pct_disp:+.2f}% | ${total_usd_disp:+.2f}")

    msg = "\n".join(lines)
    await query.message.reply_text(msg, parse_mode="HTML")


async def show_history_menu(message):
    text = (
        "📜 <b>История сделок</b>\n\n"
        "Выбери период:\n"
        "• Сегодня (МСК)\n"
        "• Вчера (МСК)\n"
        "• Последние 7 дней\n"
        "• Последние 30 дней\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("Сегодня", callback_data="hist_today"),
            InlineKeyboardButton("Вчера", callback_data="hist_yesterday"),
        ],
        [
            InlineKeyboardButton("7 дней", callback_data="hist_7d"),
            InlineKeyboardButton("30 дней", callback_data="hist_30d"),
        ],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    await message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_history_period(query, period_key: str):
    if period_key == "today":
        start_utc, end_utc = _get_window_utc_for_msk_day(0)
        title = "История за сегодня (МСК)"
    elif period_key == "yesterday":
        start_utc, end_utc = _get_window_utc_for_msk_day(-1)
        title = "История за вчера (МСК)"
    elif period_key == "7d":
        start_utc, end_utc = _get_window_utc_for_last_days(7)
        title = "История за последние 7 дней"
    elif period_key == "30d":
        start_utc, end_utc = _get_window_utc_for_last_days(30)
        title = "История за последние 30 дней"
    else:
        await query.message.reply_text("Неизвестный период")
        return

    trades = fetch_trades_window(start_utc, end_utc)
    text = format_trades_report(trades, title)
    await query.message.reply_text(text, parse_mode="HTML")


# ================== SETTINGS ==================

async def show_settings_root(message):
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
        [InlineKeyboardButton("⚙️ Общие настройки", callback_data="settings_trading")],
        [InlineKeyboardButton("💰 Депозиты", callback_data="settings_deposits")],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

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
        f"⚙️ <b>Общие настройки торговли</b>\n\n"
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
        [
            InlineKeyboardButton("BTC Вкл/Выкл", callback_data="coin_BTC_toggle"),
            InlineKeyboardButton("ETH Вкл/Выкл", callback_data="coin_ETH_toggle"),
        ],
        [
            InlineKeyboardButton("SOL Вкл/Выкл", callback_data="coin_SOL_toggle"),
            InlineKeyboardButton("XRP Вкл/Выкл", callback_data="coin_XRP_toggle"),
        ],
        [InlineKeyboardButton("🔄 Авто-торговля", callback_data="set_auto_toggle")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="set_notify_toggle")],
        [
            InlineKeyboardButton("⏱ -1 мин", callback_data="set_price_int_dec"),
            InlineKeyboardButton("⏱ +1 мин", callback_data="set_price_int_inc"),
        ],
        [
            InlineKeyboardButton("SL -0.1%", callback_data="set_sl_dec"),
            InlineKeyboardButton("SL +0.1%", callback_data="set_sl_inc"),
        ],
        [
            InlineKeyboardButton("TP -0.1%", callback_data="set_tp_dec"),
            InlineKeyboardButton("TP +0.1%", callback_data="set_tp_inc"),
        ],
        [
            InlineKeyboardButton("RSImin -5", callback_data="set_rsi_min_dec"),
            InlineKeyboardButton("RSImin +5", callback_data="set_rsi_min_inc"),
        ],
        [
            InlineKeyboardButton("RSImax -5", callback_data="set_rsi_max_dec"),
            InlineKeyboardButton("RSImax +5", callback_data="set_rsi_max_inc"),
        ],
        [
            InlineKeyboardButton("Объём -0.1x", callback_data="set_vol_dec"),
            InlineKeyboardButton("Объём +0.1x", callback_data="set_vol_inc"),
        ],
        [
            InlineKeyboardButton("ATR -0.1%", callback_data="set_atr_dec"),
            InlineKeyboardButton("ATR +0.1%", callback_data="set_atr_inc"),
        ],
        [
            InlineKeyboardButton("Интервал -10с", callback_data="set_min_int_dec"),
            InlineKeyboardButton("Интервал +10с", callback_data="set_min_int_inc"),
        ],
        [
            InlineKeyboardButton("Мин. ордер -1$", callback_data="set_min_order_dec"),
            InlineKeyboardButton("Мин. ордер +1$", callback_data="set_min_order_inc"),
        ],
        [InlineKeyboardButton("⏪ Назад", callback_data="settings_root")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def show_deposit_settings_menu(message):
    lines = ["💰 <b>Настройки депозита</b>\n"]
    for code, sym in COINS.items():
        dep = TRADE_DEPOSITS.get(sym, 0.0)
        lines.append(f"{code}: {dep:.2f} USDT")
    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("BTC -1$", callback_data="dep_BTC_dec"),
            InlineKeyboardButton("BTC +1$", callback_data="dep_BTC_inc"),
        ],
        [
            InlineKeyboardButton("ETH -1$", callback_data="dep_ETH_dec"),
            InlineKeyboardButton("ETH +1$", callback_data="dep_ETH_inc"),
        ],
        [
            InlineKeyboardButton("SOL -1$", callback_data="dep_SOL_dec"),
            InlineKeyboardButton("SOL +1$", callback_data="dep_SOL_inc"),
        ],
        [
            InlineKeyboardButton("XRP -1$", callback_data="dep_XRP_dec"),
            InlineKeyboardButton("XRP +1$", callback_data="dep_XRP_inc"),
        ],
        [InlineKeyboardButton("⏪ Назад", callback_data="settings_root")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_settings_change(query: Update, context: ContextTypes.DEFAULT_TYPE):
    global STRATEGY_CONFIG
    data = query.data

    # Монеты Вкл/Выкл
    if data.startswith("coin_") and data.endswith("_toggle"):
        code = data.split("_")[1]
        symbol = COINS.get(code)
        if symbol:
            ACTIVE_COINS[symbol] = not ACTIVE_COINS.get(symbol, True)
            await query.answer(
                f"{code}: {'ВКЛ' if ACTIVE_COINS[symbol] else 'ВЫКЛ'}"
            )
            save_state()
            await show_trading_settings_menu(query.message)
        return

    # Депозиты
    if data.startswith("dep_"):
        _, code, act = data.split("_")
        symbol = COINS.get(code)
        if symbol:
            cur = TRADE_DEPOSITS.get(symbol, 0.0)
            if act == "inc":
                cur = min(cur + 1.0, 10000.0)
            else:
                cur = max(cur - 1.0, 1.0)
            TRADE_DEPOSITS[symbol] = cur
            await query.answer(f"{code} депозит: {cur:.2f} USDT")
            save_state()
            await show_deposit_settings_menu(query.message)
        return

    cfg = STRATEGY_CONFIG

    if data == "set_auto_toggle":
        cfg["auto_enabled"] = not cfg["auto_enabled"]
        await query.answer(
            f"Авто-торговля: {'ВКЛ' if cfg['auto_enabled'] else 'ВЫКЛ'}"
        )
    elif data == "set_notify_toggle":
        cfg["notifications_enabled"] = not cfg["notifications_enabled"]
        await query.answer(
            f"Уведомления: {'ВКЛ' if cfg['notifications_enabled'] else 'ВЫКЛ'}"
        )
    elif data == "set_price_int_dec":
        cfg["price_update_interval_sec"] = max(
            60, cfg["price_update_interval_sec"] - 60
        )
        await query.answer(
            f"Интервал цен: {cfg['price_update_interval_sec']//60} мин"
        )
    elif data == "set_price_int_inc":
        cfg["price_update_interval_sec"] = min(
            3600, cfg["price_update_interval_sec"] + 60
        )
        await query.answer(
            f"Интервал цен: {cfg['price_update_interval_sec']//60} мин"
        )
    elif data == "set_sl_dec":
        cfg["sl_pct"] = max(0.005, cfg["sl_pct"] - 0.001)
        await query.answer(f"SL: {cfg['sl_pct']*100:.1f}%")
    elif data == "set_sl_inc":
        cfg["sl_pct"] = min(0.1, cfg["sl_pct"] + 0.001)
        await query.answer(f"SL: {cfg['sl_pct']*100:.1f}%")
    elif data == "set_tp_dec":
        cfg["tp_pct"] = max(0.01, cfg["tp_pct"] - 0.001)
        await query.answer(f"TP: {cfg['tp_pct']*100:.1f}%")
    elif data == "set_tp_inc":
        cfg["tp_pct"] = min(0.2, cfg["tp_pct"] + 0.001)
        await query.answer(f"TP: {cfg['tp_pct']*100:.1f}%")
    elif data == "set_rsi_min_dec":
        cfg["rsi_min"] = max(10, cfg["rsi_min"] - 5)
        await query.answer(f"RSI Min: {cfg['rsi_min']}")
    elif data == "set_rsi_min_inc":
        cfg["rsi_min"] = min(90, cfg["rsi_min"] + 5)
        await query.answer(f"RSI Min: {cfg['rsi_min']}")
    elif data == "set_rsi_max_dec":
        cfg["rsi_max"] = max(10, cfg["rsi_max"] - 5)
        await query.answer(f"RSI Max: {cfg['rsi_max']}")
    elif data == "set_rsi_max_inc":
        cfg["rsi_max"] = min(90, cfg["rsi_max"] + 5)
        await query.answer(f"RSI Max: {cfg['rsi_max']}")
    elif data == "set_vol_dec":
        cfg["volume_mult"] = max(1.0, cfg["volume_mult"] - 0.1)
        await query.answer(f"Объём: {cfg['volume_mult']:.1f}x")
    elif data == "set_vol_inc":
        cfg["volume_mult"] = min(5.0, cfg["volume_mult"] + 0.1)
        await query.answer(f"Объём: {cfg['volume_mult']:.1f}x")
    elif data == "set_atr_dec":
        cfg["atr_threshold_pct"] = max(0.001, cfg["atr_threshold_pct"] - 0.001)
        await query.answer(f"ATR порог: {cfg['atr_threshold_pct']*100:.1f}%")
    elif data == "set_atr_inc":
        cfg["atr_threshold_pct"] = min(0.05, cfg["atr_threshold_pct"] + 0.001)
        await query.answer(f"ATR порог: {cfg['atr_threshold_pct']*100:.1f}%")
    elif data == "set_min_int_dec":
        cfg["min_interval_sec"] = max(0, cfg["min_interval_sec"] - 10)
        await query.answer(f"Интервал: {cfg['min_interval_sec']} сек")
    elif data == "set_min_int_inc":
        cfg["min_interval_sec"] = min(3600, cfg["min_interval_sec"] + 10)
        await query.answer(f"Интервал: {cfg['min_interval_sec']} сек")
    elif data == "set_min_order_dec":
        cfg["min_order_usd"] = max(1, cfg["min_order_usd"] - 1)
        await query.answer(f"Мин. ордер: ${cfg['min_order_usd']}")
    elif data == "set_min_order_inc":
        cfg["min_order_usd"] = min(1000, cfg["min_order_usd"] + 1)
        await query.answer(f"Мин. ордер: ${cfg['min_order_usd']}")

    save_state()
    await show_trading_settings_menu(query.message)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return
    keyboard = [
        [InlineKeyboardButton("⚙️ Общие настройки", callback_data="settings_trading")],
        [InlineKeyboardButton("💰 Депозиты", callback_data="settings_deposits")],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    await update.message.reply_text(
        "⚙️ <b>Настройки</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ================== ТОРГОВАЯ ЛОГИКА ==================

async def execute_trade(symbol, signal, price, ex):
    global positions, last_trade_time, TRADE_DEPOSITS

    pos = positions[symbol]
    base = symbol.split("/")[0]

    if signal == "BUY" and not pos["in_position"]:
        balance = ex.fetch_balance()
        usdt = float(balance.get("USDT", {}).get("free", 0))

        dep_usd = TRADE_DEPOSITS.get(symbol, 0.0)
        amount_usd = max(dep_usd, STRATEGY_CONFIG["min_order_usd"])

        if usdt < amount_usd:
            print(f"[{symbol}] Недостаточно USDT: {usdt:.2f} < {amount_usd:.2f}")
            return

        amount = amount_usd / price
        amount_str = ex.amount_to_precision(symbol, max(amount, 0.000001))

        try:
            order = ex.create_market_buy_order(symbol, amount_str)
            filled = float(order.get("filled", 0))
            avg_price = float(order.get("average") or price)
            if filled > 0:
                usd_value = filled * avg_price
                pos.update(
                    {
                        "in_position": True,
                        "entry_price": avg_price,
                        "amount": filled,
                        "buy_time": datetime.utcnow().isoformat(),
                    }
                )
                save_state()

                log_trade(
                    "AUTO_BUY",
                    symbol,
                    avg_price,
                    filled,
                    usd_value,
                    pnl_pct=0.0,
                    pnl_usd=0.0,
                )

                chart = plot_mini_chart(symbol, ex.fetch_ohlcv(symbol, "1m", limit=50))
                msg = (
                    f"✅ <b>АВТО-ПОКУПКА</b>\n"
                    f"{symbol} @ <b>{avg_price:,.4f}</b>\n"
                    f"Объём: {filled:.6f} {base}\n"
                    f"Объём (USDT): {usd_value:.2f}\n"
                    f"SL: {avg_price * (1 - STRATEGY_CONFIG['sl_pct']):.2f} | "
                    f"TP: {avg_price * (1 + STRATEGY_CONFIG['tp_pct']):.2f}\n\n"
                    f"/start"
                )
                await send_telegram(msg, photo=chart)
                print(f"[{symbol}] AUTO BUY: {filled:.6f} @ {avg_price:,.4f}")
                last_trade_time[symbol] = time.time()
        except Exception as e:
            print(f"[{symbol}] BUY Error: {e}")

    elif signal == "SELL" and pos["in_position"]:
        try:
            base_balance = ex.fetch_balance().get(base, {})
            avail = float(base_balance.get("free") or 0.0)
            sell_amount = min(pos["amount"], avail) * 0.999

            if sell_amount <= 0:
                print(f"[{symbol}] SELL: баланс 0, помечаем как закрытую")
                pos["in_position"] = False
                save_state()
                await send_telegram(
                    f"⚠️ <b>Не удалось продать {symbol}</b>\n"
                    f"Баланс базовой монеты = 0.\n"
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
                    pos["in_position"] = False
                    save_state()
                    await send_telegram(
                        f"⚠️ <b>SELL ошибка по {symbol}</b>\n"
                        f"Биржа пишет, что баланса не хватает.\n"
                        f"Проверь позицию вручную.\n\n"
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
                usd_value = filled * avg_price
                pnl_pct = (avg_price / entry_price - 1) * 100
                pnl_usd = (avg_price - entry_price) * filled

                log_trade(
                    "AUTO_SELL",
                    symbol,
                    avg_price,
                    -filled,
                    usd_value,
                    pnl_pct=pnl_pct,
                    pnl_usd=pnl_usd,
                )

                pos["in_position"] = False
                save_state()

                msg = (
                    f"✅ <b>АВТО-ПРОДАЖА</b>\n"
                    f"{symbol} @ <b>{avg_price:,.4f}</b>\n"
                    f"Объём: {filled:.6f} {base}\n"
                    f"Объём (USDT): {usd_value:.2f}\n"
                    f"P&L: <b>{pnl_pct:+.2f}%</b> | <b>${pnl_usd:+.2f}</b>\n\n"
                    f"/start"
                )
                await send_telegram(msg)
                print(f"[{symbol}] AUTO SELL: P&L {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
                last_trade_time[symbol] = time.time()
        except Exception as e:
            print(f"[{symbol}] SELL Error (outer): {e}")


async def send_all_price_update(ex):
    global last_price_update_time
    if not STRATEGY_CONFIG["notifications_enabled"]:
        return

    now = time.time()
    if now - last_price_update_time < STRATEGY_CONFIG["price_update_interval_sec"]:
        return

    lines = ["🔔 <b>Обновление цен</b>\n"]

    for code, symbol in COINS.items():
        try:
            ticker = ex.fetch_ticker(symbol)
            price = float(ticker["last"])
            current_prices[symbol] = price

            if day_open_price[symbol] and day_open_price[symbol] > 0:
                change_pct = (
                    (price - day_open_price[symbol]) / day_open_price[symbol] * 100
                )
            else:
                change_pct = 0.0

            arrow = "📈" if change_pct >= 0 else "📉"
            change_str = (
                f"{change_pct:+.2f}%"
                if abs(change_pct) >= 0.01
                else f"{change_pct:+.4f}%"
            )

            pair_code = symbol.replace("/", "")
            pair_url = PAIR_URL_TEMPLATE.format(pair=pair_code)
            pair_link = f'<a href="{pair_url}">{symbol}</a>'

            lines.append(f"{pair_link}: {price:,.4f} {arrow} ({change_str})")

        except Exception as e:
            lines.append(f"{symbol}: ошибка цены ({e})")

    pnl_pct, pnl_usd = get_pnl_today()
    pnl_pct_disp = pnl_pct if abs(pnl_pct) >= 0.01 else 0.0
    pnl_usd_disp = pnl_usd if abs(pnl_usd) >= 0.01 else 0.0

    lines.append("\n📊 <b>P&L за сегодня (МСК)</b>")
    lines.append(f"В %: <b>{pnl_pct_disp:+.2f}%</b>")
    lines.append(f"В USDT: <b>${pnl_usd_disp:+.2f}</b>")
    lines.append("\n/start")

    msg = "\n".join(lines)
    await send_telegram(msg)

    last_price_update_time = now
    save_state()


async def detect_manual_trades(ex):
    """
    Проверяем сделки на бирже, находим новые и присылаем сообщение.
    НЕ трогаем positions — только уведомляем.
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
                continue

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
    Если бот считает, что в позиции, а реального баланса почти нет — считаем, что
    позиция закрыта вручную и оцениваем P&L.
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
                "MANUAL_EXTERNAL_CLOSE",
                symbol,
                close_price,
                -amount,
                close_price * amount,
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
            )

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


def generate_signal(symbol, current, df, ex):
    cfg = STRATEGY_CONFIG
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
    global running, day_open_price, day_open_msk_date, last_price, price_initialized
    global last_price_update_time, current_prices

    print("Торговый цикл запущен")

    try:
        ex = get_exchange()
        for symbol in SYMBOLS:
            try:
                ticker = ex.fetch_ticker(symbol)
                last_price[symbol] = float(ticker["last"])
                price_initialized[symbol] = True
                print(f"Стартовая цена {symbol}: {last_price[symbol]:,.4f}")
            except Exception as e:
                print(f"[{symbol}] Ошибка инициализации цены: {e}")
        save_state()
    except Exception as e:
        print(f"Ошибка инициализации: {e}")

    while running:
        try:
            ex = get_exchange()

            now_utc = datetime.utcnow()
            now_msk = now_utc + timedelta(hours=MOSCOW_OFFSET_HOURS)
            today_msk_str = now_msk.strftime("%Y-%m-%d")
            current_ts = time.time()

            for symbol in SYMBOLS:
                # цена открытия дня МСК
                if (
                    day_open_msk_date[symbol] != today_msk_str
                    or day_open_price[symbol] == 0.0
                ):
                    try:
                        ohlcv_day = ex.fetch_ohlcv(symbol, "1m", limit=1)
                        if ohlcv_day:
                            day_open_price[symbol] = float(ohlcv_day[0][1])
                            day_open_msk_date[symbol] = today_msk_str
                            print(
                                f"[{symbol}] Цена открытия дня МСК {today_msk_str}: {day_open_price[symbol]:.4f}"
                            )
                            save_state()
                    except Exception as e:
                        print(f"[{symbol}] Ошибка цены открытия дня: {e}")

            for symbol in SYMBOLS:
                if not ACTIVE_COINS.get(symbol, True):
                    continue

                try:
                    ohlcv = ex.fetch_ohlcv(symbol, "1m", limit=50)
                except Exception as e:
                    print(f"[{symbol}] Ошибка fetch_ohlcv: {e}")
                    continue

                current, df = calculate_indicators(ohlcv)
                current_price = float(current["close"])
                current_prices[symbol] = current_price
                last_price[symbol] = current_price

                if not price_initialized[symbol]:
                    price_initialized[symbol] = True
                    save_state()

                if STRATEGY_CONFIG["auto_enabled"]:
                    sig = generate_signal(symbol, current, df, ex)
                    if sig:
                        last_trade_time[symbol] = current_ts
                        await execute_trade(symbol, sig, current_price, ex)

            # ручные сделки + сверка
            await detect_manual_trades(ex)
            await reconcile_positions(ex)

            await send_all_price_update(ex)
            await asyncio.sleep(60)

        except Exception as e:
            print(f"Trading Error: {e}")
            await asyncio.sleep(60)


# ================== ПОЗИЦИИ ==================

async def show_positions_menu(message):
    ex = get_exchange()
    lines = ["📌 <b>Позиции по монетам</b>\n"]
    for code, symbol in COINS.items():
        pos = positions[symbol]
        base = symbol.split("/")[0]
        if pos["in_position"]:
            try:
                ticker = ex.fetch_ticker(symbol)
                price = float(ticker["last"])
            except Exception:
                price = pos["entry_price"] or 0.0

            usd_val = pos["amount"] * price
            lines.append(
                f"{code}: 🟢 в позиции @ {pos['entry_price']:,.4f}, "
                f"{pos['amount']:.6f} {base} (~{usd_val:.2f} USDT)"
            )
        else:
            lines.append(f"{code}: 🔴 нет позиции")

    text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("Закрыть BTC", callback_data="close_BTC"),
            InlineKeyboardButton("Закрыть ETH", callback_data="close_ETH"),
        ],
        [
            InlineKeyboardButton("Закрыть SOL", callback_data="close_SOL"),
            InlineKeyboardButton("Закрыть XRP", callback_data="close_XRP"),
        ],
        [InlineKeyboardButton("❗ Закрыть ВСЕ", callback_data="close_all")],
        [InlineKeyboardButton("⏪ Назад", callback_data="back_to_main")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def close_single_position(coin_code: str, query):
    symbol = COINS.get(coin_code)
    if not symbol:
        await query.message.reply_text("Неизвестная монета")
        return

    pos = positions[symbol]
    if not pos["in_position"]:
        await query.message.reply_text(f"{coin_code}: позиции нет")
        return

    ex = get_exchange()
    base = symbol.split("/")[0]

    try:
        amount_str = ex.amount_to_precision(symbol, pos["amount"])
        order = ex.create_market_sell_order(symbol, amount_str)
        avg_price = float(order.get("average") or ex.fetch_ticker(symbol)["last"])
        filled = float(order.get("filled", 0))

        if filled > 0:
            entry_price = pos["entry_price"]
            pnl_pct = (avg_price / entry_price - 1) * 100
            pnl_usd = (avg_price - entry_price) * filled
            usd_value = avg_price * filled

            log_trade(
                "MANUAL_POSITION_CLOSE",
                symbol,
                avg_price,
                -filled,
                usd_value,
                pnl_pct=pnl_pct,
                pnl_usd=pnl_usd,
            )

            positions[symbol]["in_position"] = False
            save_state()

            msg = (
                f"✅ <b>Ручное закрытие позиции</b>\n"
                f"{symbol} @ {avg_price:,.4f}\n"
                f"Объём: {filled:.6f} {base} (~{usd_value:.2f} USDT)\n"
                f"P&L: {pnl_pct:+.2f}% | ${pnl_usd:+.2f}\n\n"
                f"/start"
            )
            await query.message.reply_text(msg, parse_mode="HTML")
        else:
            await query.message.reply_text("❌ Не удалось закрыть позицию (fill=0)")
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка закрытия {symbol}: {e}")


async def close_all_positions(query):
    for code in COINS.keys():
        if positions[COINS[code]]["in_position"]:
            await close_single_position(code, query)


# ================== BUTTON HANDLER ==================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized_user(update):
        return

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "report_today":
        await handle_report_today(query)
    elif data == "pnl":
        await show_pnl_per_symbol(query)
    elif data == "market":
        await cmd_market(update, context)
    elif data == "settings":
        await show_settings_root(query.message)
    elif data == "settings_trading":
        await show_trading_settings_menu(query.message)
    elif data == "settings_deposits":
        await show_deposit_settings_menu(query.message)
    elif data == "settings_root":
        await show_settings_root(query.message)
    elif data == "back_to_main":
        await start_from_callback(update, context)
    elif data == "positions_menu":
        await show_positions_menu(query.message)
    elif data == "close_all":
        await close_all_positions(query)
    elif data.startswith("close_"):
        _, code = data.split("_", 1)
        await close_single_position(code, query)
    elif data == "history_menu":
        await show_history_menu(query.message)
    elif data == "hist_today":
        await handle_history_period(query, "today")
    elif data == "hist_yesterday":
        await handle_history_period(query, "yesterday")
    elif data == "hist_7d":
        await handle_history_period(query, "7d")
    elif data == "hist_30d":
        await handle_history_period(query, "30d")
    elif data.startswith(("coin_", "dep_", "set_")):
        await handle_settings_change(query, context)


# ================== MAIN ==================

def signal_handler(sig, frame):
    global running
    print("Останавливаем бота...")
    running = False


async def async_main():
    global running

    check_env()
    init_db()
    load_state()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

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
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("Завершение по запросу пользователя")
