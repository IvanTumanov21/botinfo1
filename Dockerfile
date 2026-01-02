FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY src/ ./src/
COPY main_new.py ./main.py

# Создаём директорию для логов
RUN mkdir -p /app/logs

# Запуск
CMD ["python", "-u", "main.py"]
