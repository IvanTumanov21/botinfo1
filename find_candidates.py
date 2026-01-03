#!/usr/bin/env python3
"""Поиск кандидатов на сигнал за последние часы"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime

# Настройки
MIN_GROWTH = 0.02
MAX_GROWTH = 0.15
VOLUME_MULT = 1.5
MAX_RSI = 75
ACC_RANGE_MULT = 5.0
ACC_VOL_RATIO = 1.5

ex = ccxt.bybit({'options': {'defaultType': 'spot'}})
ex.load_markets()

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
pairs = [p[0] for p in pairs[:80]]

print(f"Анализ {len(pairs)} пар за последние 4 часа...")
print()

candidates = []

for sym in pairs:
    try:
        ohlcv = ex.fetch_ohlcv(sym, "5m", limit=60)
        if len(ohlcv) < 45:
            continue
            
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df["growth"] = (df["c"] - df["o"]) / df["o"]
        df["vol_sma"] = df["v"].rolling(20).mean()
        df["vol_ratio"] = df["v"] / df["vol_sma"]
        
        delta = df["c"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 0.0001)))
        
        df["tr"] = np.maximum(
            df["h"] - df["l"],
            np.maximum(
                abs(df["h"] - df["c"].shift(1)),
                abs(df["l"] - df["c"].shift(1))
            )
        )
        df["atr"] = df["tr"].rolling(14).mean()
        
        # Проверяем последние 48 свечей (4 часа)
        for i in range(-48, 0):
            if i < -len(df) + 25:
                continue
            row = df.iloc[i]
            acc = df.iloc[i-20:i]
            
            if len(acc) < 20:
                continue
                
            acc_atr = acc["atr"].mean()
            rng = (acc["h"].max() - acc["l"].min()) / acc_atr if acc_atr > 0 else 999
            acc_vol = acc["vol_ratio"].mean()
            
            growth = row["growth"]
            vol_r = row["vol_ratio"]
            rsi = row["rsi"]
            
            # Все условия
            g_ok = MIN_GROWTH <= growth <= MAX_GROWTH
            v_ok = vol_r >= VOLUME_MULT
            r_ok = rsi <= MAX_RSI
            a_ok = rng <= ACC_RANGE_MULT
            av_ok = acc_vol <= ACC_VOL_RATIO
            
            if g_ok and v_ok and r_ok and a_ok and av_ok:
                candidates.append({
                    "sym": sym,
                    "time": str(row["ts"]),
                    "growth": growth * 100,
                    "vol": vol_r,
                    "rsi": rsi,
                    "acc_rng": rng,
                    "acc_vol": acc_vol
                })
                
    except Exception as e:
        pass

print("=" * 60)
print(f"НАЙДЕНО КАНДИДАТОВ НА СИГНАЛ: {len(candidates)}")
print("=" * 60)

if candidates:
    # Группируем по символу
    by_sym = {}
    for c in candidates:
        sym = c["sym"]
        if sym not in by_sym:
            by_sym[sym] = []
        by_sym[sym].append(c)
    
    for sym, items in by_sym.items():
        print(f"\n{sym}:")
        for c in items[-5:]:  # последние 5
            t = c["time"]
            g = c["growth"]
            v = c["vol"]
            r = c["rsi"]
            print(f"  ✅ {t} | +{g:.1f}% | vol:{v:.1f}x | RSI:{r:.0f}")
else:
    print("\nЗа последние 4 часа кандидатов не было")
