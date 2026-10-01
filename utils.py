"""Вспомогательные функции: логирование, задержки, работа с текстом и путями.

Модуль не зависит от остальных модулей проекта (кроме стандартной библиотеки),
поэтому его удобно использовать в тестах.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

LOG_FILE_NAME = "parser.log"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Символы, запрещённые в именах файлов Windows.
ILLEGAL_FILENAME_CHARS = r'\/:*?"<>|'

# Невидимые/управляющие символы и variation selectors (в т. ч. «скрытая» часть эмодзи).
INVISIBLE_CHARS_RE = re.compile(
    "["
    "\u0000-\u001f"              # управляющие C0
    "\u007f-\u009f"              # управляющие C1
    "\u00ad"                     # мягкий перенос
    "\u180e"                     # монгольский разделитель гласных
    "\u200b-\u200f"              # zero width space / joiner и пр.
    "\u2028\u2029"               # разделители строк/абзацев
    "\u202a-\u202e"              # LRE/PDF и пр.
    "\u2060-\u206f"              # word joiner, invisible operators
    "\ufeff"                     # BOM / ZWNBSP
    "\ufe00-\ufe0f"              # variation selectors 1-16
    "\U000e0020-\U000e007f"      # tag characters
    "]"
)

# Символы, которые openpyxl не умеет записывать в ячейку (кроме \t \n \r).
ILLEGAL_CELL_CHARS_RE = re.compile(r"[\000-\010\013\014\016-\037]")

# Зарезервированные имена устройств Windows.
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
# Пути
# --------------------------------------------------------------------------- #
def get_base_dir() -> Path:
    """Каталог приложения: рядом с main.py или с собранным .exe (PyInstaller)."""
    if getattr(sys, "frozen", False):  # pragma: no cover - только в сборке
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def reconfigure_console_encoding() -> None:
    """Включает UTF-8 в консоли Windows (чтобы кириллица не ломалась)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# Логирование
# --------------------------------------------------------------------------- #
def setup_logger(
    base_dir: Path,
    level: int = logging.INFO,
    console: bool = True,
    log_file_name: str = LOG_FILE_NAME,
) -> logging.Logger:
    """Настраивает корневой логгер: файл `parser.log` + краткий вывод в консоль.

    Повторные вызовы не создают дублирующих обработчиков.
    """
    logger = logging.getLogger("stars_parser")
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    log_path = Path(base_dir) / log_file_name
    want_file = str(log_path.resolve())
    has_file = any(
        isinstance(handler, logging.FileHandler)
        and str(Path(handler.baseFilename).resolve()) == want_file
        for handler in logger.handlers
    )
    if not has_file:
        try:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:  # нет прав на запись и т. п.
            logger.warning("Не удалось создать файл лога %s: %s", log_path, exc)

    if console and not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in logger.handlers
    ):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Возвращает логгер проекта (или его дочерний логгер)."""
    return logging.getLogger("stars_parser" if not name else f"stars_parser.{name}")


# --------------------------------------------------------------------------- #
# Задержки
# --------------------------------------------------------------------------- #
def random_interval(min_seconds: float, max_seconds: float) -> float:
    """Случайная задержка из диапазона [min, max] (при min > max значения меняются)."""
    low, high = float(min_seconds), float(max_seconds)
    if low > high:
        low, high = high, low
    if high <= 0:
        return 0.0
    return random.uniform(max(low, 0.0), high)


async def random_delay(min_seconds: float, max_seconds: float) -> float:
    """Асинхронная случайная задержка, возвращает фактическое время ожидания."""
    seconds = random_interval(min_seconds, max_seconds)
    if seconds:
        await asyncio.sleep(seconds)
    return seconds


# --------------------------------------------------------------------------- #
# Очистка текста
# --------------------------------------------------------------------------- #
def remove_invisible(text: str) -> str:
    """Удаляет невидимые символы и variation selectors."""
    if not text:
        return ""
    cleaned = INVISIBLE_CHARS_RE.sub("", text)
    return unicodedata.normalize("NFC", cleaned)


def clean_cell_value(value: Any) -> Any:
    """Подготавливает значение к записи в Excel (openpyxl не любит управляющие символы)."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    cleaned = ILLEGAL_CELL_CHARS_RE.sub("", remove_invisible(value))
    # Excel не сохраняет строки длиннее 32767 символов.
    return cleaned[:32000] if len(cleaned) > 32000 else cleaned


def collapse_spaces(text: str) -> str:
    """Схлопывает пробельные последовательности в один пробел."""
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Прочее
# --------------------------------------------------------------------------- #
def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Разбивает последовательность на части по `size` элементов."""
    bucket: list[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def mask_phone(phone: str) -> str:
    """Маскирует номер телефона для безопасного вывода/логов."""
    phone = (phone or "").strip()
    if len(phone) <= 4:
        return "*" * len(phone)
    return f"{phone[:3]}…{phone[-2:]}"


def format_seconds(seconds: float) -> str:
    """Человекочитаемое представление длительности."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин {sec} сек"
