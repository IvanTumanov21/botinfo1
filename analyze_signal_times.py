"""
Анализ времени сигналов за последние 48 часов (по МСК) из БД
"""
import psycopg2
from datetime import datetime, timedelta
from collections import defaultdict
from src.config import DATABASE_URL

def analyze_signal_times():
    """Анализ времени сигналов за последние 48 часов"""
    
    print("=" * 70)
    print("🕐 АНАЛИЗ ВРЕМЕНИ СИГНАЛОВ ЗА ПОСЛЕДНИЕ 48 ЧАСОВ (МСК)")
    print("=" * 70)
    print()
    
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    # Получаем сигналы за последние 48 часов из таблицы signals
    time_ago = datetime.utcnow() - timedelta(hours=48)
    
    cursor.execute("""
        SELECT 
            symbol,
            created_at,
            price,
            volume_ratio,
            rsi,
            candle_growth_pct,
            status
        FROM signals
        WHERE created_at >= %s
        ORDER BY created_at
    """, (time_ago,))
    
    rows = cursor.fetchall()
    
    if not rows:
        cursor.close()
        conn.close()
        print("❌ За последние 48 часов сигналов не найдено в БД")
        print("\nВозможно:")
        print("  • Рынок в консолидации")
        print("  • Бот только запущен и ещё не накопил данных")
        print("  • Настройки слишком строгие")
        return
    
    print(f"📊 Найдено {len(rows)} сигналов за последние 48 часов\n")
    
    signals_by_hour = defaultdict(list)  # Сигналы по часам МСК
    signals_by_day_hour = defaultdict(int)  # Счётчик по часам суток
    all_signals = []
    
    for row in rows:
        symbol, created_at, price, volume_ratio, rsi, candle_growth_pct, status = row
        
        # Конвертируем в МСК
        signal_time_msk = created_at + timedelta(hours=3)
        
        signal = {
            'symbol': symbol,
            'time_utc': created_at,
            'time_msk': signal_time_msk,
            'hour_msk': signal_time_msk.hour,
            'growth': candle_growth_pct if candle_growth_pct else 0,  # уже в процентах
            'volume': volume_ratio if volume_ratio else 0,
            'rsi': rsi if rsi else 0,
            'price': price,
            'status': status,
        }
        
        all_signals.append(signal)
        signals_by_hour[signal_time_msk.strftime('%Y-%m-%d %H:00')].append(signal)
        signals_by_day_hour[signal_time_msk.hour] += 1
    
    cursor.close()
    conn.close()
    
    # Сортируем по времени
    all_signals.sort(key=lambda x: x['time_msk'])
    
    print("=" * 70)
    print(f"✅ НАЙДЕНО: {len(all_signals)} сигналов за 48 часов")
    print("=" * 70)
    print()
    
    # Группировка по часам
    print("📅 СИГНАЛЫ ПО ЧАСАМ (МСК):")
    print("-" * 70)
    for hour_key in sorted(signals_by_hour.keys()):
        signals = signals_by_hour[hour_key]
        print(f"\n{hour_key} МСК | {len(signals)} сигналов:")
        for sig in signals:
            status_icon = "✅" if sig['status'] == 'approved' else "⏳" if sig['status'] == 'pending' else "❌"
            print(f"  {sig['time_msk'].strftime('%H:%M')} | {sig['symbol']:15} | "
                  f"+{sig['growth']:.1f}% | vol:{sig['volume']:.1f}x | "
                  f"RSI:{sig['rsi']:.0f} | ${sig['price']:.6f} | {status_icon}")
    
    # Распределение по часам суток
    print("\n" + "=" * 70)
    print("⏰ РАСПРЕДЕЛЕНИЕ ПО ЧАСАМ СУТОК (МСК):")
    print("-" * 70)
    
    # Визуализация
    max_count = max(signals_by_day_hour.values()) if signals_by_day_hour else 1
    for hour in range(24):
        count = signals_by_day_hour[hour]
        bar_length = int((count / max_count) * 40) if max_count > 0 else 0
        bar = "█" * bar_length
        print(f"{hour:02d}:00 | {count:3d} | {bar}")
    
    # Топ-3 активных часа
    print("\n" + "=" * 70)
    print("🔥 ТОП-3 АКТИВНЫХ ЧАСА (МСК):")
    print("-" * 70)
    top_hours = sorted(signals_by_day_hour.items(), key=lambda x: x[1], reverse=True)[:3]
    for hour, count in top_hours:
        percent = (count / len(all_signals) * 100) if all_signals else 0
        print(f"  {hour:02d}:00 - {hour+1:02d}:00 | {count} сигналов ({percent:.1f}%)")
    
    # Анализ активности по дням
    print("\n" + "=" * 70)
    print("📆 АКТИВНОСТЬ ПО ДАТАМ:")
    print("-" * 70)
    signals_by_date = defaultdict(int)
    for sig in all_signals:
        date_str = sig['time_msk'].strftime('%Y-%m-%d')
        signals_by_date[date_str] += 1
    
    for date_str in sorted(signals_by_date.keys()):
        count = signals_by_date[date_str]
        print(f"  {date_str} | {count} сигналов")
    
    print("\n" + "=" * 70)
    print("💡 ВЫВОД:")
    avg_per_hour = len(all_signals) / 48
    print(f"  • Средняя частота: {avg_per_hour:.1f} сигналов/час")
    if top_hours:
        print(f"  • Пиковая активность: {top_hours[0][0]:02d}:00-{top_hours[0][0]+1:02d}:00 МСК ({top_hours[0][1]} сигналов)")
    print(f"  • Всего за 48ч: {len(all_signals)} сигналов")
    print(f"  • Уникальных символов: {len(set(s['symbol'] for s in all_signals))}")
    print("=" * 70)

if __name__ == "__main__":
    analyze_signal_times()
