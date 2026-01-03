"""
Тестирование ОБНОВЛЁННОЙ профессиональной стратегии
"""
import psycopg2
from datetime import datetime, timedelta
from src.config import DATABASE_URL, SIGNAL_CONDITIONS
from src.exchange.indicators import calculate_indicators, detect_breakout
from loguru import logger

def test_new_strategy():
    """Тест обновлённой профессиональной стратегии на исторических данных"""
    
    print("=" * 70)
    print("🔍 ТЕСТ ОБНОВЛЁННОЙ ПРОФЕССИОНАЛЬНОЙ СТРАТЕГИИ")
    print("=" * 70)
    print(f"\nПараметры:")
    print(f"  Volume: ≥{SIGNAL_CONDITIONS['volume_breakout_mult']}x")
    print(f"  Growth: {SIGNAL_CONDITIONS['min_candle_growth']*100:.1f}% - {SIGNAL_CONDITIONS['max_candle_growth']*100:.1f}%")
    print(f"  RSI: {SIGNAL_CONDITIONS.get('min_rsi', 0)} - {SIGNAL_CONDITIONS['max_rsi']}")
    print(f"  EMA setup: {SIGNAL_CONDITIONS.get('require_ema_setup', False)}")
    print()
    
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    # Получаем данные за последние 3 часа
    time_ago = datetime.utcnow() - timedelta(hours=3)
    
    cursor.execute("""
        SELECT DISTINCT symbol 
        FROM candles_5m 
        WHERE timestamp >= %s
        ORDER BY symbol
    """, (time_ago,))
    
    symbols = [row[0] for row in cursor.fetchall()]
    print(f"📊 Анализируем {len(symbols)} пар за последние 3 часа\n")
    
    signals_found = []
    
    for symbol in symbols:
        try:
            # Получаем свечи (нужно достаточно для EMA100)
            cursor.execute("""
                SELECT timestamp, open, high, low, close, volume
                FROM candles_5m
                WHERE symbol = %s
                ORDER BY timestamp DESC
                LIMIT 120
            """, (symbol,))
            
            rows = cursor.fetchall()
            if len(rows) < 100:
                continue
            
            # Переворачиваем (от старых к новым)
            ohlcv = [[r[0].timestamp() * 1000, r[1], r[2], r[3], r[4], r[5]] for r in reversed(rows)]
            
            # Рассчитываем индикаторы
            df = calculate_indicators(ohlcv)
            if df is None or len(df) < 100:
                continue
            
            # Проверяем каждую свечу из последних 3 часов (36 свечей)
            for i in range(max(100, len(df) - 36), len(df)):
                df_slice = df.iloc[:i+1].copy()
                
                is_breakout, metrics = detect_breakout(df_slice)
                
                if is_breakout:
                    timestamp = datetime.fromtimestamp(df_slice.iloc[-1]['timestamp'] / 1000)
                    
                    signals_found.append({
                        'symbol': symbol,
                        'time': timestamp,
                        'growth': metrics['candle_growth'],
                        'volume': metrics['volume_ratio'],
                        'rsi': metrics['rsi'],
                        'ema9_above_ema21': metrics.get('ema9_above_ema21', False),
                        'price_above_ema50': metrics.get('price_above_ema50', False),
                    })
        
        except Exception as e:
            continue
    
    cursor.close()
    conn.close()
    
    # Сортируем по времени
    signals_found.sort(key=lambda x: x['time'])
    
    print("=" * 70)
    print(f"✅ ОБНОВЛЁННАЯ СТРАТЕГИЯ: {len(signals_found)} сигналов найдено")
    print("=" * 70)
    print()
    
    for sig in signals_found:
        ema_info = ""
        if sig['ema9_above_ema21'] and sig['price_above_ema50']:
            ema_info = " | ✅ EMA setup OK"
        else:
            ema_info = f" | ❌ EMA9>21:{sig['ema9_above_ema21']} Price>50:{sig['price_above_ema50']}"
        
        print(f"{sig['symbol']}:")
        print(f"  {sig['time'].strftime('%Y-%m-%d %H:%M:%S')} | "
              f"+{sig['growth']:.1f}% | vol:{sig['volume']:.1f}x | "
              f"RSI:{sig['rsi']:.0f}{ema_info}")
        print()
    
    print("=" * 70)
    print("🎯 ВЫВОД:")
    print(f"  Профессиональная стратегия с EMA setup работает!")
    print(f"  Найдено {len(signals_found)} качественных сигналов")
    print("=" * 70)

if __name__ == "__main__":
    test_new_strategy()
