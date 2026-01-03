"""
Проверка: какие пары близки к сигналу (показывают 2-3 из 4 условий)
"""
from src.config import SIGNAL_CONDITIONS, INDICATORS
from src.exchange.indicators import calculate_indicators, detect_breakout, detect_accumulation
from pybit.unified_trading import HTTP
import pandas as pd

session = HTTP(testnet=False)
tickers = session.get_tickers(category="spot")

print("=" * 80)
print("🔍 ПОИСК ПАР БЛИЗКИХ К СИГНАЛУ (соответствуют 2-3 условиям)")
print("=" * 80)
print()

# Требуемые условия:
print("Требуемые условия сигнала:")
print(f"  1. Volume ≥ {SIGNAL_CONDITIONS['volume_breakout_mult']}x")
print(f"  2. Growth {SIGNAL_CONDITIONS['min_candle_growth']*100:.1f}% - {SIGNAL_CONDITIONS['max_candle_growth']*100:.1f}%")
print(f"  3. RSI {SIGNAL_CONDITIONS.get('min_rsi', 0)} - {SIGNAL_CONDITIONS['max_rsi']}")
print(f"  4. EMA setup (EMA9>EMA21 + Price>EMA50)")
print()

try:
    # Получаем список пар
    usdt_pairs = [t for t in tickers["result"]["list"] if t["symbol"].endswith("USDT")]
    
    candidates = {
        'vol_ok': [],           # Объём OK
        'growth_ok': [],        # Рост OK
        'rsi_ok': [],          # RSI OK
        'ema_ok': [],          # EMA OK
        'partial': [],         # 2-3 условия
        'full': [],            # Все 4 условия
    }
    
    print(f"Проверяем {len(usdt_pairs)} пар...\n")
    
    for t in usdt_pairs[:50]:  # Проверяем первые 50 для быстроты
        symbol = t["symbol"]
        
        try:
            # Получаем исторические данные
            candles = session.get_kline(
                category="spot",
                symbol=symbol,
                interval="5",
                limit=120
            )
            
            if not candles["result"]["list"]:
                continue
            
            ohlcv = [[float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] 
                     for c in reversed(candles["result"]["list"])]
            
            df = calculate_indicators(ohlcv)
            if df is None or len(df) < 100:
                continue
            
            last = df.iloc[-1]
            
            # Проверяем каждое условие
            vol_ok = last['volume_ratio'] >= SIGNAL_CONDITIONS['volume_breakout_mult']
            growth_ok = (SIGNAL_CONDITIONS['min_candle_growth'] <= last['candle_growth'] <= 
                        SIGNAL_CONDITIONS['max_candle_growth'])
            rsi_ok = SIGNAL_CONDITIONS.get('min_rsi', 0) <= last['rsi14'] <= SIGNAL_CONDITIONS['max_rsi']
            ema_ok = (last['ema9'] > last['ema21']) and (last['close'] > last['ema50'])
            
            conditions_met = sum([vol_ok, growth_ok, rsi_ok, ema_ok])
            
            if conditions_met >= 2:  # Показываем те, что близки к сигналу
                info = {
                    'symbol': symbol,
                    'price': last['close'],
                    'vol': last['volume_ratio'],
                    'growth': last['candle_growth'] * 100,
                    'rsi': last['rsi14'],
                    'ema9': last['ema9'],
                    'ema21': last['ema21'],
                    'close': last['close'],
                    'ema50': last['ema50'],
                    'met': conditions_met,
                    'vol_ok': vol_ok,
                    'growth_ok': growth_ok,
                    'rsi_ok': rsi_ok,
                    'ema_ok': ema_ok,
                }
                candidates['partial'].append(info)
        except:
            continue
    
    if candidates['partial']:
        print("=" * 80)
        print(f"✅ НАЙДЕНО {len(candidates['partial'])} ПАР БЛИЗКИХ К СИГНАЛУ:\n")
        
        # Сортируем по количеству выполненных условий
        candidates['partial'].sort(key=lambda x: x['met'], reverse=True)
        
        for c in candidates['partial'][:10]:
            met_str = f"{c['met']}/4 условий"
            checks = ""
            checks += "✅Vol " if c['vol_ok'] else "❌Vol "
            checks += "✅Gr " if c['growth_ok'] else "❌Gr "
            checks += "✅RSI " if c['rsi_ok'] else "❌RSI "
            checks += "✅EMA" if c['ema_ok'] else "❌EMA"
            
            print(f"{c['symbol']:15} | {met_str:12} | {checks}")
            print(f"  Volume: {c['vol']:.1f}x | Growth: {c['growth']:+.1f}% | RSI: {c['rsi']:.0f} | EMA9>21: {c['ema9'] > c['ema21']}")
            print()
    else:
        print("❌ НЕТ ПАР, БЛИЗКИХ К СИГНАЛУ")
        print("   Рынок в консолидации или условия слишком строгие")
    
except Exception as e:
    print(f"❌ Ошибка: {e}")

print("=" * 80)
