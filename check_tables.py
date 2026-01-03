"""
Проверка таблиц в БД
"""
import psycopg2
from src.config import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
tables = cursor.fetchall()

print("Таблицы в БД:")
for t in tables:
    print(f"  - {t[0]}")

cursor.close()
conn.close()
