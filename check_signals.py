#!/usr/bin/env python3
"""Детальный анализ условий сигнала для топовых пар"""

import ccxt
import pandas as pd
import numpy as np

# Новые настройки
MIN_GROWTH = 0.02  # 2%
MAX_GROWTH = 0.15  # 15%
VOLUME_MULT = 1.5
MAX_RSI = 75
ACC_RANGE_MULT = 5.0
ACC_VOL_RATIO = 1.5

ex = ccxt.bybit({'options': {'defaultType': 'spot'}})
ex.load_markets()

# Топ растущих пар
tickers = ex.fetch_tickers()
filtered = []
for s, t in tickers.items():
    if not s.endswith('/USDT') or ':' in s:
        continue
    price = t.get('last', 0) or 0
    vol = t.get('quoteVolume', 0) or 0
    ch = t.get('percentage', 0) or 0
    if 0.0005 <= price <= 1.0 and vol >= 200000 and ch > 10:
        filtered.append((s, ch, vol))

filtered.sort(key=lambda x: -x[1])

print("=== Детальный анализ топ растущих пар ===\n")

for sym, ch24, vol in filtered[:8]:
    try:
        ohlcv = ex.fetch_ohlcv(sym, "5m", limit=60)
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
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

        print(f"{'='*50}")
        print(f"{sym} | 24h: +{ch24:.1f}% | Vol: ${vol/1e6:.1f}M")
        print(f"{'='*50}")
        
        # Ищем свечи, которые могли бы быть сигналами
        signal_found = False
        for i in range(-15, 0):
            row = df.iloc[i]
            acc = df.iloc[i-20:i]
            acc_atr = df.iloc[i-20:i]["atr"].mean()
            rng = (acc["h"].max() - acc["l"].min()) / acc_atr if acc_atr > 0 else 999
            
            acc_vol = acc["v"].mean()
            prev_vol = df.iloc[i-40:i-20]["v"].mean() if (i-40) >= 0 else acc_vol
            vol_acc = acc_vol / prev_vol if prev_vol > 0 else 999
            
            growth = row["growth"]
            vol_r = row["vol_ratio"]
            rsi = row["rsi"]
            
            growth_ok = MIN_GROWTH <= growth <= MAX_GROWTH
            vol_ok = vol_r >= VOLUME_MULT
            rsi_ok = rsi <= MAX_RSI
            acc_range_ok = rng <= ACC_RANGE_MULT
            acc_vol_ok = vol_acc <= ACC_VOL_RATIO
            
            all_ok = growth_ok and vol_ok and rsi_ok and acc_range_ok and acc_vol_ok
            
            if growth > 0.01 or vol_r > 1.2:  # Показываем интересные свечи
                status = "✅ SIGNAL!" if all_ok else "❌"
                reasons = []
                if not growth_ok:
                    reasons.append(f"growth:{growth*100:.1f}%")
                if not vol_ok:
                    reasons.append(f"vol:{vol_r:.1f}x")
                if not rsi_ok:
                    reasons.append(f"RSI:{rsi:.0f}")
                if not acc_range_ok:
                    reasons.append(f"acc_rng:{rng:.1f}")
                if not acc_vol_ok:
                    reasons.append(f"acc_vol:{vol_acc:.1f}")
                    
                print(f"  {status} g:{growth*100:+5.1f}% vol:{vol_r:4.1f}x RSI:{rsi:4.0f} | {', '.join(reasons) if reasons else 'OK'}")
                if all_ok:
                    signal_found = True
        
        if not signal_found:
            print("  Нет подходящих свечей за последние 15 штук")
        print()
        
    except Exception as e:
        print(f"  Ошибка: {e}\n")
