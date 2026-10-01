#!/usr/bin/env python3
"""Консольное приложение для парсинга звёзд (платных реакций) в постах Telegram-канала.

Запуск:
    python main.py
    python main.py --channel @durov --count 200

Рядом с приложением должен лежать `conf.ini` (см. `conf.example.ini`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

from client import AuthError, ConnectionFailureError, authorize, build_client
from config import AppConfig, ConfigError, ensure_output_dir, load_config
from exporter import export_records
from handlers import (
    ChannelResolutionError,
    EntityCache,
    channel_title,
    get_last_message_id,
    parse_channel,
    parse_channel_input,
    resolve_channel,
    warmup_participants,
)
from models import ParseStats, StarRecord
from utils import get_base_dir, get_logger, reconfigure_console_encoding, setup_logger

APP_TITLE = "Парсер звёзд (Paid Reactions) в постах Telegram-канала"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3
EXIT_CONNECTION_ERROR = 4
EXIT_INTERRUPTED = 130

PROMPT_CHANNEL = "Введите наименование/id канала: "
PROMPT_COUNT = "Введите количество последних постов для анализа: "


# --------------------------------------------------------------------------- #
# Аргументы командной строки
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    """Описание аргументов командной строки (все — необязательные)."""
    parser = argparse.ArgumentParser(
        prog="stars_parser",
        description=APP_TITLE,
        add_help=True,
    )
    parser.add_argument("-c", "--config", type=Path, default=None,
                        help="путь к conf.ini (по умолчанию — рядом с приложением)")
    parser.add_argument("--channel", default=None,
                        help="канал: @username, ссылка t.me/..., числовой id (-100...)")
    parser.add_argument("-n", "--count", type=int, default=None,
                        help="сколько последних постов анализировать")
    parser.add_argument("--output-dir", default=None,
                        help="каталог для Excel (по умолчанию из [OUTPUT] DIR)")
    parser.add_argument("--warmup", action="store_true", default=None,
                        help="подгрузить участников группы обсуждения в кэш сессии")
    parser.add_argument("--no-warmup", dest="warmup", action="store_false",
                        help="отключить прогрев кэша участников")
    parser.add_argument("--create-empty-file", action="store_true", default=False,
                        help="создавать xlsx с шапкой, даже если звёзд не найдено")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="уровень логирования (по умолчанию INFO)")
    return parser


# --------------------------------------------------------------------------- #
# Интерактивный ввод
# --------------------------------------------------------------------------- #
async def prompt_channel(client: TelegramClient, preset: Optional[str] = None) -> object:
    """Запрашивает канал, пока не получит корректную сущность."""
    while True:
        raw = preset if preset is not None else input(PROMPT_CHANNEL).strip()
        preset = None
        if not raw:
            print("Пустой ввод. Пример: @channelname, t.me/channelname или -1001234567890")
            continue
        try:
            value = parse_channel_input(raw)
        except ChannelResolutionError as exc:
            print(f"{exc} Попробуйте снова.")
            continue
        try:
            entity = await resolve_channel(client, value)
        except ChannelResolutionError as exc:
            print(f"{exc} Попробуйте снова.")
            continue
        except Exception as exc:  # сеть и прочие нештатные ситуации
            get_logger("main").exception("Ошибка поиска канала %r: %s", raw, exc)
            print(f"Не удалось получить канал ({exc}). Попробуйте снова.")
            continue
        return entity


def prompt_count(preset: Optional[int] = None) -> int:
    """Запрашивает количество постов (целое число больше нуля)."""
    while True:
        raw = str(preset) if preset is not None else input(PROMPT_COUNT).strip()
        preset = None
        raw = raw.replace(" ", "").replace("_", "")
        try:
            count = int(raw)
        except ValueError:
            print("Нужно целое число больше 0. Пример: 100")
            continue
        if count <= 0:
            print("Количество постов должно быть больше 0.")
            continue
        return count


# --------------------------------------------------------------------------- #
# Оркестрация
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    """Основной сценарий: конфиг -> авторизация -> канал -> парсинг -> Excel."""
    logger = get_logger("main")
    base_dir = get_base_dir()

    try:
        config = load_config(base_dir, args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации:\n{exc}")
        return EXIT_CONFIG_ERROR

    print(APP_TITLE)
    print(f"Конфигурация: {config.config_path}")

    records: list[StarRecord] = []
    stats = ParseStats()
    client: Optional[TelegramClient] = None
    channel_name = "channel"

    try:
        client = build_client(config, base_dir)
        try:
            await authorize(client, config, max_attempts=config.parser.max_attempts)
        except ConnectionFailureError as exc:
            print(f"Ошибка подключения: {exc}")
            return EXIT_CONNECTION_ERROR
        except AuthError as exc:
            print(f"Ошибка авторизации: {exc}")
            return EXIT_AUTH_ERROR

        entity = await prompt_channel(client, args.channel)
        channel_name = channel_title(entity)
        last_id = await get_last_message_id(client, entity)
        print(f"(id последнего сообщения: {last_id if last_id is not None else 'не определён'})")

        if args.count is not None:
            if args.count <= 0:
                print("Количество постов (--count) должно быть больше 0.")
                return EXIT_CONFIG_ERROR
            count = args.count
        else:
            count = prompt_count()
        stats.requested = count

        cache = EntityCache()
        warmup = args.warmup if args.warmup is not None else config.parser.warmup_participants
        if warmup:
            print("Прогрев кэша: загружаем участников группы обсуждения...")
            cached = await warmup_participants(client, entity, config.parser.warmup_limit)
            print(f"В кэш сессии загружено участников: {cached}")

        print(f"\nАнализируем {count} последних постов канала «{channel_name}»...\n")
        stats = await parse_channel(
            client, entity, count, records,
            delay=config.delay.as_tuple(),
            cache=cache,
            stats=stats,
        )
    except KeyboardInterrupt:
        stats.interrupted = True
        print("\nПарсинг прерван пользователем (Ctrl+C). Сохраняем собранные данные...")
        logger.warning("Парсинг прерван пользователем. Собрано записей: %d", len(records))
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # pragma: no cover - disconnect не должен ломать выход
                logger.debug("Ошибка при отключении клиента", exc_info=True)

    return save_results(records, stats, channel_name, config, args, base_dir)


def save_results(
    records: list[StarRecord],
    stats: ParseStats,
    channel_name: str,
    config: AppConfig,
    args: argparse.Namespace,
    base_dir: Path,
) -> int:
    """Сохраняет Excel и печатает итог. Возвращает код завершения."""
    logger = get_logger("main")
    output_dir_value = args.output_dir if args.output_dir else config.output.directory
    try:
        output_dir = ensure_output_dir(base_dir, output_dir_value)
    except ConfigError as exc:
        logger.warning(
            "Не удалось использовать каталог %s (%s) — сохраняем рядом с приложением.",
            output_dir_value, exc,
        )
        output_dir = Path(base_dir)

    create_empty = bool(args.create_empty_file or config.output.create_empty_file)

    path = export_records(
        records,
        output_dir=output_dir,
        channel_name=channel_name,
        filename_max_length=config.output.filename_max_length,
        create_empty_file=create_empty,
    )

    print()
    if path is None:
        print("Звёзды не найдены")
    else:
        print(f"Файл сохранён: {path}")

    if stats.interrupted and stats.scanned == 0:
        print("Данных для сохранения нет.")
    else:
        print(stats.as_text())

    if stats.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    """Точка входа консольного приложения."""
    reconfigure_console_encoding()
    args = build_arg_parser().parse_args(argv)

    base_dir = get_base_dir()
    setup_logger(base_dir, level=getattr(logging, args.log_level))

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # Ctrl+C до/во время подготовки
        print("\nРабота прервана.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # последний рубеж: показываем ошибку вместо traceback
        get_logger("main").exception("Непредвиденная ошибка: %s", exc)
        print(f"Непредвиденная ошибка: {exc}. Подробности — в parser.log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
