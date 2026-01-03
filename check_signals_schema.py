"""
Проверка структуры таблицы signals
"""
import psycopg2
from src.config import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

cursor.execute("""
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_name = 'signals'
    ORDER BY ordinal_position
""")

columns = cursor.fetchall()

print("Колонки в таблице signals:")
for col, dtype in columns:
    print(f"  - {col}: {dtype}")

# Получаем пример записи
cursor.execute("SELECT * FROM signals LIMIT 1")
row = cursor.fetchone()
if row:
    print(f"\nПример записи:")
    print(f"  {row}")

cursor.close()
conn.close()
