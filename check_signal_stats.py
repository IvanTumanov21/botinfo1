"""
Проверка наличия сигналов за разные периоды
"""
import psycopg2
from datetime import datetime, timedelta
from src.config import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# Проверяем, сколько всего сигналов в таблице
cursor.execute("SELECT COUNT(*) FROM signals")
total_signals = cursor.fetchone()[0]
print(f"✅ Всего сигналов в БД: {total_signals}")

if total_signals == 0:
    print("\n❌ Таблица signals пуста!")
    print("   Сигналы ещё не генерировались или таблица создана недавно.")
    cursor.close()
    conn.close()
    exit()

# Проверяем дату первого и последнего сигнала
cursor.execute("""
    SELECT 
        MIN(created_at) as first_signal,
        MAX(created_at) as last_signal,
        COUNT(*) as count
    FROM signals
""")

first, last, count = cursor.fetchone()
print(f"\n📊 Первый сигнал: {first}")
print(f"📊 Последний сигнал: {last}")
print(f"📊 Всего сигналов: {count}")

# Проверяем за разные периоды
print("\n" + "=" * 70)
print("📈 СИГНАЛЫ ПО ПЕРИОДАМ (от последнего сигнала):")
print("=" * 70)

periods = [
    (1, "1 час"),
    (6, "6 часов"),
    (24, "24 часа"),
    (48, "48 часов"),
    (168, "7 дней"),
    (720, "30 дней"),
]

last_time = datetime.utcnow()

for hours, label in periods:
    time_ago = last_time - timedelta(hours=hours)
    cursor.execute("SELECT COUNT(*) FROM signals WHERE created_at >= %s", (time_ago,))
    count = cursor.fetchone()[0]
    print(f"  Последние {label:12} | {count} сигналов")

# Топ символов
print("\n" + "=" * 70)
print("🏆 ТОП-10 СИМВОЛОВ ПО КОЛИЧЕСТВУ СИГНАЛОВ:")
print("=" * 70)

cursor.execute("""
    SELECT symbol, COUNT(*) as count
    FROM signals
    GROUP BY symbol
    ORDER BY count DESC
    LIMIT 10
""")

for symbol, count in cursor.fetchall():
    print(f"  {symbol:15} | {count} сигналов")

# Статусы сигналов
print("\n" + "=" * 70)
print("📌 РАСПРЕДЕЛЕНИЕ ПО СТАТУСАМ:")
print("=" * 70)

cursor.execute("""
    SELECT status, COUNT(*) as count
    FROM signals
    GROUP BY status
    ORDER BY count DESC
""")

for status, count in cursor.fetchall():
    print(f"  {status:15} | {count} сигналов")

cursor.close()
conn.close()
