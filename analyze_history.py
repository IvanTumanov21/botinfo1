#!/usr/bin/env python3
"""Анализ исторических данных для проверки настроек сигналов"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime

def main():
    ex = ccxt.bybit({'options': {'defaultType': 'spot'}})
    ex.load_markets()

    # Текущие настройки
    CONFIG_CURRENT = {
        "accumulation_range_mult": 2.5,
        "accumulation_volume_ratio": 0.8,
        "volume_breakout_mult": 2.0,
        "max_candle_growth": 0.08,
        "min_candle_growth": 0.005,
        "max_rsi": 70
    }

    # Ослабленные настройки
    CONFIG_RELAXED = {
        "accumulation_range_mult": 5.0,
        "accumulation_volume_ratio": 1.5,
        "volume_breakout_mult": 1.5,
        "max_candle_growth": 0.15,
        "min_candle_growth": 0.02,
        "max_rsi": 75
    }

    # Получаем пары
    tickers = ex.fetch_tickers()
    filtered = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT") or ":" in sym:
            continue
        price = t.get("last", 0) or 0
        vol = t.get("quoteVolume", 0) or 0
        if 0.0005 <= price <= 1.0 and vol >= 200000:
            filtered.append((sym, vol))

    filtered.sort(key=lambda x: -x[1])
    pairs = [p[0] for p in filtered[:50]]
    print(f"Анализ {len(pairs)} пар за ~42 часа...")
    print()

    def check_signals(ohlcv, cfg):
        if len(ohlcv) < 50:
            return []
        df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df["growth"] = (df["c"] - df["o"]) / df["o"]
        df["vol_sma"] = df["v"].rolling(20).mean()
        df["vol_ratio"] = df["v"] / df["vol_sma"]
        df["tr"] = np.maximum(
            df["h"] - df["l"],
            np.maximum(
                abs(df["h"] - df["c"].shift(1)),
                abs(df["l"] - df["c"].shift(1))
            )
        )
        df["atr"] = df["tr"].rolling(14).mean()
        delta = df["c"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, 0.0001)))

        signals = []
        for idx in range(40, len(df)):
            row = df.iloc[idx]
            if not (cfg["min_candle_growth"] <= row["growth"] <= cfg["max_candle_growth"]):
                continue
            if row["vol_ratio"] < cfg["volume_breakout_mult"]:
                continue
            if row["rsi"] > cfg["max_rsi"]:
                continue
            acc = df.iloc[idx - 20:idx]
            atr_mean = acc["atr"].mean()
            rng = (acc["h"].max() - acc["l"].min()) / atr_mean if atr_mean > 0 else 999
            if rng > cfg["accumulation_range_mult"]:
                continue
            acc_vol = acc["v"].mean()
            prev_vol = df.iloc[idx - 40:idx - 20]["v"].mean()
            vol_acc = acc_vol / prev_vol if prev_vol > 0 else 999
            if vol_acc > cfg["accumulation_volume_ratio"]:
                continue
            signals.append({
                "time": str(row["ts"]),
                "growth": row["growth"] * 100,
                "vol_ratio": row["vol_ratio"],
                "rsi": row["rsi"],
                "symbol": ""
            })
        return signals

    current_signals = []
    relaxed_signals = []

    for i, sym in enumerate(pairs):
        try:
            ohlcv = ex.fetch_ohlcv(sym, "5m", limit=500)
            
            curr = check_signals(ohlcv, CONFIG_CURRENT)
            for s in curr:
                s["symbol"] = sym
                current_signals.append(s)
            
            relax = check_signals(ohlcv, CONFIG_RELAXED)
            for s in relax:
                s["symbol"] = sym
                relaxed_signals.append(s)
        except Exception as e:
            pass
        
        if (i + 1) % 10 == 0:
            print(f"Проверено {i + 1}/{len(pairs)}...")

    print()
    print("=" * 60)
    print(f"ТЕКУЩИЕ настройки: {len(current_signals)} сигналов за ~42ч")
    print(f"ОСЛАБЛЕННЫЕ настройки: {len(relaxed_signals)} сигналов за ~42ч")
    print("=" * 60)
    print()

    if current_signals:
        print("Сигналы с ТЕКУЩИМИ настройками:")
        for s in current_signals[:15]:
            print(f"  {s['symbol']:15} | {s['time']} | +{s['growth']:.1f}%")
    else:
        print("С ТЕКУЩИМИ настройками: 0 сигналов!")
        print(">>> НАСТРОЙКИ СЛИШКОМ СТРОГИЕ <<<")

    print()
    if relaxed_signals:
        print("Примеры с ОСЛАБЛЕННЫМИ настройками:")
        for s in relaxed_signals[:20]:
            print(f"  {s['symbol']:15} | {s['time']} | +{s['growth']:.1f}%")

    print()
    print("=" * 60)
    print("РЕКОМЕНДУЕМЫЕ НАСТРОЙКИ:")
    print("  accumulation_range_mult: 5.0  (было 2.5)")
    print("  accumulation_volume_ratio: 1.5  (было 0.8)")
    print("  volume_breakout_mult: 1.5  (было 2.0)")
    print("  max_candle_growth: 0.15  (было 0.08)")
    print("  min_candle_growth: 0.02  (было 0.005)")
    print("=" * 60)

if __name__ == "__main__":
    main()
