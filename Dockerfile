FROM python:3.11-slim

# Отключаем буферизацию логов и запись pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Устанавливаем системные зависимости, если понадобятся для сборки
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем только requirements.txt для кэширования слоев
COPY requirements.txt .

# Устанавливаем зависимости с жестким контролем памяти
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код проекта
COPY . .

# Создаем папку под выходные файлы
RUN mkdir -p output

# Точка входа для запуска бота
CMD ["python", "-m", "bot.bot_main"]
