"""
Расчёт индикаторов для анализа
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from loguru import logger

from src.config import INDICATORS, SIGNAL_CONDITIONS


def calculate_indicators(ohlcv: List[List]) -> Optional[pd.DataFrame]:
    """
    Рассчитывает все индикаторы для DataFrame свечей.
    
    Входные данные: [[timestamp, open, high, low, close, volume], ...]
    
    Возвращает DataFrame с колонками:
    - timestamp, open, high, low, close, volume
    - ema7, ema14, ema28, ema100
    - volume_sma20
    - atr14
    - rsi14
    - high_20, low_20 (макс/мин за 20 свечей)
    """
    if not ohlcv or len(ohlcv) < INDICATORS["ema_trend"]:
        return None
    
    try:
        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        
        # Приводим типы
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
        
        # ================== EMA ==================
        df['ema7'] = df['close'].ewm(span=INDICATORS["ema_fast"], adjust=False).mean()
        df['ema14'] = df['close'].ewm(span=INDICATORS["ema_mid"], adjust=False).mean()
        df['ema28'] = df['close'].ewm(span=INDICATORS["ema_slow"], adjust=False).mean()
        df['ema100'] = df['close'].ewm(span=INDICATORS["ema_trend"], adjust=False).mean()
        
        # ================== Volume SMA ==================
        df['volume_sma20'] = df['volume'].rolling(window=INDICATORS["volume_sma"]).mean()
        
        # ================== ATR ==================
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            )
        )
        df['atr14'] = df['tr'].rolling(window=INDICATORS["atr_period"]).mean()
        
        # ================== RSI ==================
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=INDICATORS["rsi_period"]).mean()
        avg_loss = loss.rolling(window=INDICATORS["rsi_period"]).mean()
        rs = avg_gain / avg_loss
        df['rsi14'] = 100 - (100 / (1 + rs))
        
        # ================== High/Low за N свечей ==================
        lookback = INDICATORS["lookback_candles"]
        df['high_20'] = df['high'].rolling(window=lookback).max()
        df['low_20'] = df['low'].rolling(window=lookback).min()
        
        # ================== Дополнительные метрики ==================
        # Рост текущей свечи
        df['candle_growth'] = (df['close'] - df['open']) / df['open']
        
        # Объём относительно SMA
        df['volume_ratio'] = df['volume'] / df['volume_sma20']
        
        # EMA сплетение (разница между ними)
        df['ema_spread'] = (
            abs(df['ema7'] - df['ema14']) + 
            abs(df['ema14'] - df['ema28'])
        ) / df['close']
        
        # Наклон EMA100 (плоская или нет)
        df['ema100_slope'] = (df['ema100'] - df['ema100'].shift(5)) / df['ema100'].shift(5)
        
        return df
        
    except Exception as e:
        logger.error(f"Ошибка расчёта индикаторов: {e}")
        return None


def detect_accumulation(df: pd.DataFrame) -> Tuple[bool, Dict]:
    """
    Определяет фазу накопления за последние 20 свечей.
    
    УПРОЩЁННЫЕ условия (для ловли пампов):
    1. Диапазон цены ≤ 5.0 × ATR (смягчено)
    2. Объём за период ниже 150% среднего (смягчено)
    
    Возвращает:
    - bool: есть накопление или нет
    - dict: метрики накопления
    """
    if df is None or len(df) < INDICATORS["lookback_candles"]:
        return False, {}
    
    try:
        lookback = INDICATORS["lookback_candles"]
        recent = df.tail(lookback)
        last = df.iloc[-1]
        
        # 1. Диапазон цены (основной критерий)
        price_range = recent['high'].max() - recent['low'].min()
        atr = last['atr14']
        range_ratio = price_range / atr if atr > 0 else 999
        range_ok = range_ratio <= SIGNAL_CONDITIONS["accumulation_range_mult"]
        
        # 2. EMA сплетение (опционально - не блокирует)
        ema_spread = last['ema_spread']
        ema_tangled = ema_spread < 0.02  # смягчено до 2%
        
        # 3. EMA100 наклон (опционально - не блокирует)
        ema100_slope = abs(last['ema100_slope']) if pd.notna(last['ema100_slope']) else 0
        ema100_flat = ema100_slope < 0.02  # смягчено до 2%
        
        # 4. Объём за период (основной критерий)
        avg_volume_ratio = recent['volume_ratio'].mean()
        volume_low = avg_volume_ratio < SIGNAL_CONDITIONS["accumulation_volume_ratio"]
        
        # УПРОЩЁННЫЙ ИТОГ: только range и volume важны
        is_accumulation = range_ok and volume_low
        
        metrics = {
            "range_ratio": round(range_ratio, 2),
            "ema_spread": round(ema_spread * 100, 3),
            "ema100_slope": round(ema100_slope * 100, 3),
            "avg_volume_ratio": round(avg_volume_ratio, 2),
            "range_ok": range_ok,
            "ema_tangled": ema_tangled,
            "ema100_flat": ema100_flat,
            "volume_low": volume_low,
        }
        
        return is_accumulation, metrics
        
    except Exception as e:
        logger.error(f"Ошибка определения накопления: {e}")
        return False, {}


def detect_breakout(df: pd.DataFrame) -> Tuple[bool, Dict]:
    """
    Определяет импульсный пробой (breakout).
    
    УПРОЩЁННЫЕ условия (для ловли пампов):
    1. Рост текущей свечи в диапазоне 2-15%
    2. Объём ≥ 1.5 × SMA20
    3. RSI ≤ 75
    
    Возвращает:
    - bool: есть пробой или нет
    - dict: метрики пробоя
    """
    if df is None or len(df) < INDICATORS["lookback_candles"] + 2:
        return False, {}
    
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        
        # Предыдущий high_20 (без текущей свечи)
        lookback = INDICATORS["lookback_candles"]
        prev_high_20 = df['high'].iloc[-lookback-1:-1].max()
        
        # 1. Рост свечи в диапазоне
        candle_growth = last['candle_growth']
        growth_ok = (
            SIGNAL_CONDITIONS["min_candle_growth"] <= candle_growth <= 
            SIGNAL_CONDITIONS["max_candle_growth"]
        )
        
        # 2. Объём ≥ 1.5x (смягчено)
        volume_spike = last['volume_ratio'] >= SIGNAL_CONDITIONS["volume_breakout_mult"]
        
        # 3. RSI не перегрет
        rsi_ok = last['rsi14'] <= SIGNAL_CONDITIONS["max_rsi"]
        
        # Опциональные (для метрик, не блокируют):
        # Пробой High20
        breakout_high = last['close'] > prev_high_20
        
        # Закрытие выше EMA100
        above_ema100 = last['close'] > last['ema100']
        
        # EMA7 пересекает EMA14 снизу вверх
        ema_cross = (
            (prev['ema7'] <= prev['ema14'] or prev2['ema7'] <= prev2['ema14']) and
            last['ema7'] > last['ema14']
        )
        
        # УПРОЩЁННЫЙ ИТОГ: только growth, volume и RSI важны
        is_breakout = growth_ok and volume_spike and rsi_ok
        
        metrics = {
            "prev_high_20": prev_high_20,
            "current_close": last['close'],
            "ema100": last['ema100'],
            "volume_ratio": round(last['volume_ratio'], 2),
            "candle_growth": round(candle_growth * 100, 2),
            "rsi": round(last['rsi14'], 1),
            "breakout_high": breakout_high,
            "above_ema100": above_ema100,
            "ema_cross": ema_cross,
            "volume_spike": volume_spike,
            "growth_ok": growth_ok,
            "rsi_ok": rsi_ok,
        }
        
        return is_breakout, metrics
        
    except Exception as e:
        logger.error(f"Ошибка определения пробоя: {e}")
        return False, {}


def check_false_pump_filter(
    df: pd.DataFrame, 
    spread: float,
    bid_ask_ratio: float
) -> Tuple[bool, Dict]:
    """
    Фильтр ложных пампов.
    
    Условия:
    1. Рост свечи ≤ 6-8%
    2. RSI ≤ 70
    3. Спред ≤ 0.6%
    4. Стакан: bid/ask ≥ 0.8
    
    Возвращает:
    - bool: проходит фильтр или нет (True = сигнал валидный)
    - dict: метрики
    """
    if df is None or df.empty:
        return False, {}
    
    try:
        last = df.iloc[-1]
        
        # 1. Рост свечи
        candle_growth = last['candle_growth']
        growth_ok = (
            SIGNAL_CONDITIONS["min_candle_growth"] <= candle_growth <= 
            SIGNAL_CONDITIONS["max_candle_growth"]
        )
        
        # 2. RSI
        rsi = last['rsi14']
        rsi_ok = rsi <= SIGNAL_CONDITIONS["max_rsi"]
        
        # 3. Спред
        spread_ok = spread <= SIGNAL_CONDITIONS["max_spread"]
        
        # 4. Стакан
        orderbook_ok = bid_ask_ratio >= SIGNAL_CONDITIONS["min_bid_ask_ratio"]
        
        # Итог
        passed = growth_ok and rsi_ok and spread_ok and orderbook_ok
        
        metrics = {
            "candle_growth_pct": round(candle_growth * 100, 2),
            "rsi": round(rsi, 1),
            "spread_pct": round(spread * 100, 3),
            "bid_ask_ratio": round(bid_ask_ratio, 2),
            "growth_ok": growth_ok,
            "rsi_ok": rsi_ok,
            "spread_ok": spread_ok,
            "orderbook_ok": orderbook_ok,
        }
        
        return passed, metrics
        
    except Exception as e:
        logger.error(f"Ошибка фильтра ложных пампов: {e}")
        return False, {}


def calculate_levels(
    entry_price: float,
    atr: float,
    ema28: float,
    low_20: float
) -> Dict[str, float]:
    """
    Рассчитывает уровни входа, стопа и тейков.
    
    Entry: текущая цена ± небольшой буфер
    SL: под EMA28 или под low_20 (что ниже)
    TP1: +5%
    TP2: +10%
    TP3: +15% (или trailing)
    """
    # Entry zone
    entry_low = entry_price * 0.998
    entry_high = entry_price * 1.002
    
    # Stop Loss - под EMA28 с буфером
    sl_ema28 = ema28 * 0.995
    sl_low = low_20 * 0.995
    stop_loss = min(sl_ema28, sl_low)
    
    # Take Profits
    tp1 = entry_price * 1.05   # +5%
    tp2 = entry_price * 1.10   # +10%
    tp3 = entry_price * 1.15   # +15%
    
    # Риск/прибыль
    risk = entry_price - stop_loss
    reward1 = tp1 - entry_price
    rr_ratio = reward1 / risk if risk > 0 else 0
    
    return {
        "entry_low": round(entry_low, 6),
        "entry_high": round(entry_high, 6),
        "stop_loss": round(stop_loss, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "tp3": round(tp3, 6),
        "risk_pct": round((risk / entry_price) * 100, 2),
        "rr_ratio": round(rr_ratio, 2),
    }
