"""Конфигурация бота: загрузка и валидация секции [BOT] из conf.ini и окружения."""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from utils import get_logger

logger = get_logger("bot")

CONFIG_FILE_NAME = "conf.ini"

DEFAULT_MAX_CONCURRENT_TASKS = 1
DEFAULT_PROGRESS_EDIT_INTERVAL = 2
DEFAULT_DONOR_THRESHOLD = 80
DEFAULT_SEND_FILES = True
DEFAULT_DETAILED_EXCEL = False
DEFAULT_MAX_CHANNELS_PER_REQUEST = 20
DEFAULT_WIZARD_IDLE_TIMEOUT = 600


class BotConfigError(Exception):
    """Ошибка конфигурации бота."""


@dataclass(slots=True)
class BotConfig:
    token: str
    allowed_user_ids: set[int] = field(default_factory=set)
    max_concurrent_tasks: int = 1
    progress_edit_interval: int = DEFAULT_PROGRESS_EDIT_INTERVAL
    donor_threshold: int = DEFAULT_DONOR_THRESHOLD
    send_files: bool = DEFAULT_SEND_FILES
    detailed_excel: bool = DEFAULT_DETAILED_EXCEL
    max_channels_per_request: int = DEFAULT_MAX_CHANNELS_PER_REQUEST
    wizard_idle_timeout: int = DEFAULT_WIZARD_IDLE_TIMEOUT


def _parse_bool(value: str, default: bool = False) -> bool:
    val = value.strip().lower()
    if val in ("true", "1", "yes", "y", "on"):
        return True
    if val in ("false", "0", "no", "n", "off"):
        return False
    return default


def _parse_int(value: str, key_name: str) -> int:
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        raise BotConfigError(f"Параметр [BOT] {key_name} должен быть целым числом, получено: {value!r}")


def load_bot_config(config_path: Path | str | None = None) -> BotConfig:
    """Загружает секцию [BOT] из файла conf.ini и проверяет BOT_TOKEN в env."""
    path = Path(config_path) if config_path else Path(CONFIG_FILE_NAME)

    parser = configparser.ConfigParser(interpolation=None)
    if path.is_file():
        try:
            parser.read(path, encoding="utf-8")
        except Exception as exc:
            raise BotConfigError(f"Ошибка чтения файла конфигурации: {exc}")

    bot_sec = parser["BOT"] if "BOT" in parser else {}

    # 1. TOKEN: BOT_TOKEN env имеет приоритет над conf.ini
    env_token = os.environ.get("BOT_TOKEN", "").strip()
    ini_token = bot_sec.get("TOKEN", "").strip() if "TOKEN" in bot_sec else ""
    token = env_token or ini_token

    if not token:
        sys.stderr.write("Критическая ошибка: TOKEN не задан в [BOT] conf.ini и переменная BOT_TOKEN отсутствует.\n")
        raise BotConfigError("TOKEN обязателен для запуска бота.")

    # 2. ALLOWED_USER_IDS
    raw_allowed = bot_sec.get("ALLOWED_USER_IDS", "").strip() if "ALLOWED_USER_IDS" in bot_sec else ""
    allowed_ids: set[int] = set()
    if raw_allowed:
        for part in raw_allowed.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                allowed_ids.add(int(part))
            except ValueError:
                raise BotConfigError(f"Параметр [BOT] ALLOWED_USER_IDS содержит некорректный id: {part!r}")

    # 3. MAX_CONCURRENT_TASKS (любое значение трактуется как 1; >1 -> warning)
    raw_tasks = bot_sec.get("MAX_CONCURRENT_TASKS", "1") if "MAX_CONCURRENT_TASKS" in bot_sec else "1"
    tasks_val = _parse_int(raw_tasks, "MAX_CONCURRENT_TASKS")
    if tasks_val > 1:
        logger.warning("Параметр MAX_CONCURRENT_TASKS=%d > 1, но в версии v3 поддерживается только 1. Установлено 1.", tasks_val)
    max_concurrent_tasks = 1

    # 4. PROGRESS_EDIT_INTERVAL
    raw_interval = bot_sec.get("PROGRESS_EDIT_INTERVAL", str(DEFAULT_PROGRESS_EDIT_INTERVAL)) if "PROGRESS_EDIT_INTERVAL" in bot_sec else str(DEFAULT_PROGRESS_EDIT_INTERVAL)
    progress_edit_interval = _parse_int(raw_interval, "PROGRESS_EDIT_INTERVAL")
    if progress_edit_interval <= 0:
        progress_edit_interval = DEFAULT_PROGRESS_EDIT_INTERVAL

    # 5. DONOR_THRESHOLD
    raw_threshold = bot_sec.get("DONOR_THRESHOLD", str(DEFAULT_DONOR_THRESHOLD)) if "DONOR_THRESHOLD" in bot_sec else str(DEFAULT_DONOR_THRESHOLD)
    donor_threshold = _parse_int(raw_threshold, "DONOR_THRESHOLD")

    # 6. SEND_FILES
    raw_send = bot_sec.get("SEND_FILES", str(DEFAULT_SEND_FILES)) if "SEND_FILES" in bot_sec else str(DEFAULT_SEND_FILES)
    send_files = _parse_bool(raw_send, DEFAULT_SEND_FILES)

    # 7. DETAILED_EXCEL
    raw_detailed = bot_sec.get("DETAILED_EXCEL", str(DEFAULT_DETAILED_EXCEL)) if "DETAILED_EXCEL" in bot_sec else str(DEFAULT_DETAILED_EXCEL)
    detailed_excel = _parse_bool(raw_detailed, DEFAULT_DETAILED_EXCEL)

    # 8. MAX_CHANNELS_PER_REQUEST
    raw_max_channels = bot_sec.get("MAX_CHANNELS_PER_REQUEST", str(DEFAULT_MAX_CHANNELS_PER_REQUEST)) if "MAX_CHANNELS_PER_REQUEST" in bot_sec else str(DEFAULT_MAX_CHANNELS_PER_REQUEST)
    max_channels_per_request = _parse_int(raw_max_channels, "MAX_CHANNELS_PER_REQUEST")

    # 9. WIZARD_IDLE_TIMEOUT
    raw_timeout = bot_sec.get("WIZARD_IDLE_TIMEOUT", str(DEFAULT_WIZARD_IDLE_TIMEOUT)) if "WIZARD_IDLE_TIMEOUT" in bot_sec else str(DEFAULT_WIZARD_IDLE_TIMEOUT)
    wizard_idle_timeout = _parse_int(raw_timeout, "WIZARD_IDLE_TIMEOUT")

    return BotConfig(
        token=token,
        allowed_user_ids=allowed_ids,
        max_concurrent_tasks=max_concurrent_tasks,
        progress_edit_interval=progress_edit_interval,
        donor_threshold=donor_threshold,
        send_files=send_files,
        detailed_excel=detailed_excel,
        max_channels_per_request=max_channels_per_request,
        wizard_idle_timeout=wizard_idle_timeout,
    )
