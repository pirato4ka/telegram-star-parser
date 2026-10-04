"""Точка входа для запуска бота."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from telethon import TelegramClient

from bot.access import AccessMiddleware
from bot.config_bot import BotConfigError, load_bot_config
from bot.handlers_bot import router
from bot.queue_service import QueueService
from client import build_client
from config import ConfigError, load_config
from utils import get_base_dir, setup_logger

logger = setup_logger(get_base_dir(), log_file_name="bot.log")


async def main() -> None:
    base_dir = get_base_dir()

    # 1. Загрузка конфигурации бота
    try:
        bot_config = load_bot_config(base_dir / "conf.ini")
    except BotConfigError as exc:
        sys.stderr.write(f"Ошибка конфигурации бота: {exc}\n")
        sys.exit(1)

    # 2. Загрузка конфигурации ядра Telethon
    try:
        app_config = load_config(base_dir)
    except ConfigError as exc:
        sys.stderr.write(f"Ошибка конфигурации ядра: {exc}\n")
        sys.exit(1)

    # 3. Создание Telethon-клиента (единый loop)
    client = build_client(app_config, base_dir)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.critical(
                "Сессия Telethon не авторизована! Требуется интерактивная авторизация через CLI (main.py)."
            )
            sys.stderr.write("Критическая ошибка: Сессия Telethon не авторизована. Запустите сначала main.py.\n")
            sys.exit(1)
    except Exception as exc:
        logger.critical("Не удалось подключить Telethon-клиент: %s", exc)
        sys.stderr.write(f"Ошибка подключения Telethon: {exc}\n")
        sys.exit(1)

    logger.info("Telethon-клиент успешно подключен.")

    # 4. Инициализация aiogram
    bot = Bot(token=bot_config.token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Очередь задач
    queue_service = QueueService()
    queue_service.start_worker()

    # Передача контекста в хендлеры и middleware
    dp["bot_config"] = bot_config
    dp["app_config"] = app_config
    dp["telethon_client"] = client
    dp["queue_service"] = queue_service

    # Middleware доступа
    access_middleware = AccessMiddleware(bot_config.allowed_user_ids)
    dp.message.middleware(access_middleware)
    dp.callback_query.middleware(access_middleware)

    dp.include_router(router)

    logger.info("Бот запускается... Whitelist пользователей: %d", len(bot_config.allowed_user_ids))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
