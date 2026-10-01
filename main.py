#!/usr/bin/env python3
"""Консольное приложение для парсинга звёзд (платных реакций) в постах Telegram-канала.

Запуск:
    python main.py
    python main.py --channel @durov --count 200
    python main.py --channel "@durov, t.me/telegram, -1001234567890" -n 500

Несколько каналов перечисляются через запятую: они становятся в очередь и
обрабатываются по очереди, для каждого сохраняется свой Excel-файл.

Рядом с приложением должен лежать `conf.ini` (см. `conf.example.ini`).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from telethon import TelegramClient

from client import AuthError, ConnectionFailureError, authorize, build_client
from config import AppConfig, ConfigError, ensure_output_dir, load_config
from exporter import export_records
from handlers import (
    ChannelResolutionError,
    EntityCache,
    channel_input_text,
    channel_title,
    get_last_message_id,
    parse_channel,
    parse_channels_input,
    resolve_channel,
    warmup_participants,
)
from models import ParseStats, StarRecord
from speed import RequestThrottle
from utils import get_base_dir, get_logger, reconfigure_console_encoding, setup_logger

APP_TITLE = "Парсер звёзд (Paid Reactions) в постах Telegram-канала"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_ERROR = 3
EXIT_CONNECTION_ERROR = 4
EXIT_INTERRUPTED = 130

PROMPT_CHANNEL = "Введите наименование/id канала (или несколько через запятую): "
PROMPT_COUNT = "Введите количество последних постов для анализа: "

# Итог обработки одного канала из очереди.
STATUS_DONE = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"


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
                        help="канал: @username, ссылка t.me/..., числовой id (-100...); "
                             "несколько каналов — через запятую (обрабатываются по очереди)")
    parser.add_argument("-n", "--count", type=int, default=None,
                        help="сколько последних постов анализировать (для каждого канала)")
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
# Очередь каналов
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ChannelTask:
    """Канал в очереди: как его ввели, что получилось после разбора и сущность."""

    value: Union[str, int]
    entity: Any = None
    name: str = ""

    @property
    def label(self) -> str:
        """Название для консоли: title канала, иначе username/ссылка/id."""
        return self.name or channel_input_text(self.value)


@dataclass(slots=True)
class ChannelOutcome:
    """Результат обработки одного канала из очереди."""

    task: ChannelTask
    status: str = STATUS_DONE
    records: int = 0
    scanned: int = 0
    with_stars: int = 0
    path: Optional[Path] = None
    error: str = ""


# --------------------------------------------------------------------------- #
# Интерактивный ввод
# --------------------------------------------------------------------------- #
async def prompt_channels(client: TelegramClient,
                          preset: Optional[str] = None) -> list[ChannelTask]:
    """Запрашивает канал(ы) и возвращает готовую очередь на парсинг.

    Каналы перечисляются через запятую: `@durov, t.me/telegram, -1001234567890`.
    Каждый канал разбирается и сразу получается его сущность. Каналы, которые не
    удалось получить, в очередь не попадают — об этом сообщается, а остальные
    продолжают обрабатываться. Если не получен ни один канал, ввод запрашивается
    заново (синтаксическая ошибка в списке тоже приводит к повторному вводу).
    """
    logger = get_logger("main")
    while True:
        raw = preset if preset is not None else input(PROMPT_CHANNEL).strip()
        preset = None
        if not raw:
            print("Пустой ввод. Пример: @durov, t.me/telegram или -1001234567890")
            continue

        try:
            values = parse_channels_input(raw)
        except ChannelResolutionError as exc:
            print(f"{exc} Попробуйте снова.")
            continue

        tasks: list[ChannelTask] = []
        failed: list[tuple[str, str]] = []
        for value in values:
            try:
                entity = await resolve_channel(client, value)
            except ChannelResolutionError as exc:
                failed.append((channel_input_text(value), str(exc)))
                continue
            except Exception as exc:  # сеть и прочие нештатные ситуации
                logger.exception("Ошибка поиска канала %r: %s", value, exc)
                failed.append((channel_input_text(value), str(exc)))
                continue
            tasks.append(ChannelTask(value=value, entity=entity, name=channel_title(entity)))

        for name, error in failed:
            print(f"Канал {name} пропущен: {error}")
        if not tasks:
            print("Не удалось получить ни один канал. Попробуйте снова.")
            continue
        if failed:
            print(f"В очереди останется {len(tasks)} из {len(values)} каналов.")
        if len(tasks) > 1:
            names = ", ".join(task.label for task in tasks)
            print(f"В очереди {len(tasks)} каналов: {names}")
        return tasks


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
# Один канал из очереди
# --------------------------------------------------------------------------- #
async def parse_one_channel(
    client: TelegramClient,
    task: ChannelTask,
    count: int,
    config: AppConfig,
    args: argparse.Namespace,
    throttle: RequestThrottle,
    records: list[StarRecord],
    stats: ParseStats,
) -> ParseStats:
    """Парсит один канал очереди: последний id -> прогрев кэша -> сообщения.

    Список `records` и объект `stats` принадлежат вызывающему коду (очереди) и
    заполняются по ходу работы — поэтому при прерывании (Ctrl+C) уже собранные
    данные этого канала можно сохранить.
    """
    stats.requested = int(count)

    last_id = await get_last_message_id(client, task.entity, throttle=throttle)
    print(f"(id последнего сообщения: {last_id if last_id is not None else 'не определён'})")

    cache = EntityCache(entity_batch=config.speed.entity_batch,
                        concurrency=config.speed.concurrency,
                        resolve_unknown_peers=config.speed.resolve_unknown_peers)
    warmup = args.warmup if args.warmup is not None else config.parser.warmup_participants
    if warmup:
        print(f"Прогрев кэша: загружаем до {config.parser.warmup_limit} участников "
              "группы обсуждения...")
        cached = await warmup_participants(client, task.entity, config.parser.warmup_limit,
                                           throttle=throttle)
        print(f"В кэш сессии загружено участников: {cached}")

    print(f"\nАнализируем {count} последних постов канала «{task.label}»...\n")
    return await parse_channel(
        client, task.entity, count, records,
        delay=config.delay.as_tuple(),
        cache=cache,
        stats=stats,
        speed=config.speed,
        throttle=throttle,
    )


# --------------------------------------------------------------------------- #
# Сохранение результата
# --------------------------------------------------------------------------- #
def save_channel(
    records: list[StarRecord],
    stats: ParseStats,
    channel_name: str,
    config: AppConfig,
    args: argparse.Namespace,
    base_dir: Path,
) -> Optional[Path]:
    """Сохраняет Excel одного канала и печатает итог прохода. Возвращает путь."""
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
    return path


# --------------------------------------------------------------------------- #
# Очередь: последовательная обработка каналов
# --------------------------------------------------------------------------- #
async def parse_queue(
    client: TelegramClient,
    tasks: list[ChannelTask],
    count: int,
    config: AppConfig,
    args: argparse.Namespace,
    throttle: RequestThrottle,
    base_dir: Path,
    outcomes: Optional[list[ChannelOutcome]] = None,
) -> tuple[list[ChannelOutcome], bool]:
    """Обрабатывает каналы по очереди; возвращает итоги и признак прерывания.

    Ошибка одного канала не останавливает очередь: он помечается как `error`,
    а обработка продолжается со следующего. Ctrl+C сохраняет данные текущего
    канала и останавливает очередь (необработанные каналы помечаются).

    Итоги дописываются в переданный список `outcomes`, поэтому прогресс очереди
    виден вызывающему коду даже при непредвиденном прерывании.
    """
    logger = get_logger("main")
    if outcomes is None:
        outcomes = []
    interrupted = False
    total = len(tasks)

    for index, task in enumerate(tasks):
        outcome = ChannelOutcome(task=task)
        records: list[StarRecord] = []
        stats = ParseStats(requested=count)
        if total > 1:
            print(f"\n===== Канал {index + 1} из {total}: «{task.label}» =====")

        try:
            stats = await parse_one_channel(client, task, count, config, args,
                                            throttle, records, stats)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C: asyncio отменяет задачу, поэтому ловим и отмену тоже —
            # иначе собранные данные не будут сохранены.
            stats.interrupted = True
            interrupted = True
            print("\nПарсинг прерван пользователем (Ctrl+C). Сохраняем собранные данные...")
            logger.warning("Парсинг прерван пользователем. Канал %s, записей: %d",
                           task.label, len(records))
            outcome.status = STATUS_CANCELLED
            outcome.path = save_channel(records, stats, task.label, config, args, base_dir)
            outcome.records, outcome.scanned = len(records), stats.scanned
            outcome.with_stars = stats.with_stars
            outcomes.append(outcome)
            for pending in tasks[index + 1:]:
                outcomes.append(ChannelOutcome(
                    task=pending, status=STATUS_CANCELLED,
                    error="не обработан: очередь прервана пользователем",
                ))
            break
        except Exception as exc:  # канал не остановит остальные в очереди
            logger.exception("Ошибка парсинга канала %s: %s", task.label, exc)
            print(f"\nНе удалось обработать канал «{task.label}»: {exc}. "
                  "Переходим к следующему.")
            outcome.status = STATUS_ERROR
            outcome.error = str(exc)
            outcomes.append(outcome)
            continue

        outcome.path = save_channel(records, stats, task.label, config, args, base_dir)
        outcome.records, outcome.scanned = len(records), stats.scanned
        outcome.with_stars = stats.with_stars
        outcomes.append(outcome)

    return outcomes, interrupted


def print_queue_summary(outcomes: list[ChannelOutcome], count: int) -> None:
    """Сводка по очереди каналов (печатается, если каналов больше одного)."""
    if len(outcomes) <= 1:
        return

    done = [o for o in outcomes if o.status == STATUS_DONE]
    failed = [o for o in outcomes if o.status == STATUS_ERROR]
    cancelled = [o for o in outcomes if o.status == STATUS_CANCELLED]

    print(f"\n===== Итог по очереди: {len(outcomes)} каналов по {count} постов =====")
    for index, outcome in enumerate(outcomes, 1):
        label = outcome.task.label
        if outcome.status == STATUS_ERROR:
            print(f"  {index}. «{label}» — ошибка: {outcome.error}")
        elif outcome.status == STATUS_CANCELLED:
            print(f"  {index}. «{label}» — {outcome.error or 'прервано'}")
        else:
            saved = f", файл: {outcome.path.name}" if outcome.path else ", звёзд не найдено"
            print(f"  {index}. «{label}» — постов: {outcome.scanned}, "
                  f"со звёздами: {outcome.with_stars}, записей: {outcome.records}{saved}")

    files = [o for o in done if o.path]
    print(f"Готово: {len(done)} из {len(outcomes)}, файлов: {len(files)}, "
          f"записей всего: {sum(o.records for o in outcomes)}")
    if failed:
        print(f"Ошибок: {len(failed)} — " + ", ".join(o.task.label for o in failed))
    if cancelled:
        print(f"Не обработано: {len(cancelled)} — " + ", ".join(o.task.label for o in cancelled))


def queue_exit_code(outcomes: list[ChannelOutcome], interrupted: bool) -> int:
    """Код завершения: 130 при Ctrl+C, 1 если очередь не дала ни одного канала."""
    if interrupted:
        return EXIT_INTERRUPTED
    if outcomes and all(o.status == STATUS_ERROR for o in outcomes):
        return EXIT_ERROR
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Оркестрация
# --------------------------------------------------------------------------- #
async def run(args: argparse.Namespace) -> int:
    """Основной сценарий: конфиг -> авторизация -> очередь каналов -> Excel."""
    logger = get_logger("main")
    base_dir = get_base_dir()

    try:
        config = load_config(base_dir, args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации:\n{exc}")
        return EXIT_CONFIG_ERROR

    print(APP_TITLE)
    print(f"Конфигурация: {config.config_path}")

    outcomes: list[ChannelOutcome] = []
    interrupted = False
    count = 0
    client: Optional[TelegramClient] = None
    # Пауза из [DELAY] применяется между реальными запросами к API, а не между
    # сообщениями: посты без звёзд и уже известные донаторы не ждут вовсе.
    # Троттлинг один на всю очередь, поэтому накопленный после FloodWait интервал
    # сохраняется и для следующих каналов.
    throttle = config.speed.throttle(config.delay.as_tuple())

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

        tasks = await prompt_channels(client, args.channel)

        if args.count is not None:
            if args.count <= 0:
                print("Количество постов (--count) должно быть больше 0.")
                return EXIT_CONFIG_ERROR
            count = args.count
        else:
            count = prompt_count()
        if len(tasks) > 1:
            print(f"Каждый канал анализируем по {count} последних постов.")

        outcomes, interrupted = await parse_queue(client, tasks, count, config, args,
                                                  throttle, base_dir, outcomes)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl+C до начала/в промежутке между каналами: данные уже сохранены
        # внутри parse_queue, здесь остаётся корректно завершиться.
        interrupted = True
        print("\nРабота прервана пользователем (Ctrl+C).")
        logger.warning("Работа прервана пользователем.")
    finally:
        if client is not None:
            try:
                res = client.disconnect()
                if inspect.isawaitable(res):
                    await res
            except Exception:  # pragma: no cover - disconnect не должен ломать выход
                logger.debug("Ошибка при отключении клиента", exc_info=True)

    print_queue_summary(outcomes, count=count)
    return queue_exit_code(outcomes, interrupted)


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
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
