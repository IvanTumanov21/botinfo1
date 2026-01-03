#!/usr/bin/env python3
"""Сравнение нашей стратегии с EMA+Volume подходом"""

import ccxt
import pandas as pd
import numpy as np

ex = ccxt.bybit({'options': {'defaultType': 'spot'}})
ex.load_markets()

# НАШИ настройки
OUR_MIN_GROWTH = 0.02
OUR_MAX_GROWTH = 0.15
OUR_VOLUME_MULT = 1.5
OUR_MAX_RSI = 75

# ИХ настройки
THEIR_VOLUME_MULT = 3.0
THEIR_MAX_GROWTH = 0.05  # < 5%
THEIR_MIN_RSI = 50
THEIR_MAX_RSI = 70

tickers = ex.fetch_tickers()
pairs = []
for s, t in tickers.items():
    if not s.endswith("/USDT") or ":" in s:
        continue
    price = t.get("last", 0) or 0
    vol = t.get("quoteVolume", 0) or 0
    if 0.0005 <= price <= 1.0 and vol >= 200000:
        pairs.append((s, vol))

pairs.sort(key=lambda x: -x[1])
pairs = [p[0] for p in pairs[:60]]

print("Сравнение стратегий на последних 3 часах:")
print("=" * 70)

our_signals = []
their_signals = []

for sym in pairs:
    try:
        ohlcv = ex.fetch_ohlcv(sym, "5m", limit=50)
        if len(ohlcv) < 40:
            continue
            
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        
        # Индикаторы
        df["growth"] = (df["c"] - df["o"]) / df["o"]
        df["vol_sma20"] = df["v"].rolling(20).mean()
        df["vol_ratio"] = df["v"] / df["vol_sma20"]
        
        # EMA
        df["ema9"] = df["c"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()
        df["ema50"] = df["c"].ewm(span=50, adjust=False).mean()
        
        # RSI
        delta = df["c"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 0.0001)))
        
        # ATR
        df["tr"] = np.maximum(
            df["h"] - df["l"],
            np.maximum(
                abs(df["h"] - df["c"].shift(1)),
                abs(df["l"] - df["c"].shift(1))
            )
        )
        df["atr"] = df["tr"].rolling(14).mean()
        
        # Проверяем последние 36 свечей (3 часа)
        for i in range(-36, 0):
            if i < -len(df) + 25:
                continue
            row = df.iloc[i]
            
            # НАША стратегия
            our_pass = (
                OUR_MIN_GROWTH <= row["growth"] <= OUR_MAX_GROWTH and
                row["vol_ratio"] >= OUR_VOLUME_MULT and
                row["rsi"] <= OUR_MAX_RSI
            )
            
            # ИХ стратегия
            their_pass = (
                row["vol_ratio"] >= THEIR_VOLUME_MULT and
                row["ema9"] > row["ema21"] and
                row["c"] > row["ema50"] and
                THEIR_MIN_RSI <= row["rsi"] <= THEIR_MAX_RSI and
                row["growth"] < THEIR_MAX_GROWTH and
                row["growth"] > 0
            )
            
            if our_pass:
                our_signals.append({
                    "sym": sym,
                    "time": str(row["ts"]),
                    "growth": row["growth"] * 100,
                    "vol": row["vol_ratio"],
                    "rsi": row["rsi"]
                })
            
            if their_pass:
                their_signals.append({
                    "sym": sym,
                    "time": str(row["ts"]),
                    "growth": row["growth"] * 100,
                    "vol": row["vol_ratio"],
                    "rsi": row["rsi"]
                })
                
    except Exception as e:
        pass

print(f"\n📊 РЕЗУЛЬТАТЫ ЗА 3 ЧАСА:\n")
print(f"НАША стратегия:    {len(our_signals)} сигналов")
print(f"ИХ стратегия:      {len(their_signals)} сигналов")
print("=" * 70)

if their_signals:
    print(f"\n✅ ИХ СТРАТЕГИЯ (Volume 3x, EMA setup, рост <5%):")
    print("-" * 70)
    by_sym = {}
    for s in their_signals:
        sym = s["sym"]
        if sym not in by_sym:
            by_sym[sym] = []
        by_sym[sym].append(s)
    
    for sym, items in list(by_sym.items())[:10]:
        print(f"\n{sym}:")
        for s in items[-3:]:
            print(f"  {s['time']} | +{s['growth']:.1f}% | vol:{s['vol']:.1f}x | RSI:{s['rsi']:.0f}")
else:
    print("\n❌ ИХ стратегия: сигналов нет")

if our_signals:
    print(f"\n\n📊 НАША СТРАТЕГИЯ (Volume 1.5x, рост 2-15%):")
    print("-" * 70)
    by_sym = {}
    for s in our_signals:
        sym = s["sym"]
        if sym not in by_sym:
            by_sym[sym] = []
        by_sym[sym].append(s)
    
    for sym, items in list(by_sym.items())[:10]:
        print(f"\n{sym}:")
        for s in items[-3:]:
            print(f"  {s['time']} | +{s['growth']:.1f}% | vol:{s['vol']:.1f}x | RSI:{s['rsi']:.0f}")
else:
    print("\n\n❌ НАША стратегия: сигналов нет")

print("\n" + "=" * 70)
print("ВЫВОД:")
if len(their_signals) > len(our_signals):
    print("✅ ИХ стратегия находит БОЛЬШЕ ранних сигналов")
    print("   Объём 3x и EMA setup работают лучше")
elif len(their_signals) < len(our_signals):
    print("⚠️  НАША стратегия находит больше, но ПОЗЖЕ")
    print("   Входим когда памп уже идёт (рост 2-15%)")
else:
    print("📊 Примерно одинаково")
