"""Формирование Excel-отчёта и безопасное имя файла."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from models import COLUMNS, EXCEL_SHEET_NAME, StarRecord
from utils import (
    ILLEGAL_FILENAME_CHARS,
    WINDOWS_RESERVED_NAMES,
    clean_cell_value,
    collapse_spaces,
    get_logger,
    remove_invisible,
)

logger = get_logger("exporter")

FALLBACK_CHANNEL_NAME = "channel"
DEFAULT_MAX_LENGTH = 100
MAX_SUFFIX_ATTEMPTS = 100

# Запрещённые в именах файлов символы (Windows) + управляющие.
ILLEGAL_CHARS_RE = re.compile(f"[{re.escape(ILLEGAL_FILENAME_CHARS)}\\u0000-\\u001f\\u007f-\\u009f]")

# Колонки, которые должны отображаться в Excel целыми числами (без экспоненциальной записи).
INTEGER_COLUMNS = ("current_message_id", "original_message_id", "reactor_id", "stars_count")

MIN_COLUMN_WIDTH = 10
MAX_COLUMN_WIDTH = 60


# --------------------------------------------------------------------------- #
# Имя файла
# --------------------------------------------------------------------------- #
def safe_filename(name: str, max_length: int = DEFAULT_MAX_LENGTH,
                  fallback: str = FALLBACK_CHANNEL_NAME) -> str:
    """Приводит название канала к безопасному имени файла для Windows.

    * удаляет невидимые символы и variation selectors (в т. ч. «скрытую» часть эмодзи);
    * заменяет `\\ / : * ? " < > |` и управляющие символы на `_`;
    * схлопывает пробелы, убирает пробелы/точки в конце;
    * обрезает до `max_length` символов (по границам символов, не по байтам);
    * для зарезервированных имён Windows (CON, NUL, COM1...) добавляет подчёркивание.
    """
    text = remove_invisible(name or "")
    text = ILLEGAL_CHARS_RE.sub("_", text)
    text = unicodedata.normalize("NFC", text)
    text = collapse_spaces(text)
    text = text.strip(" \t\r\n.")
    text = text.lstrip("-") if text.startswith("-") and len(text) > 1 else text

    if not text or not text.strip("_ ."):
        # Имя состоит только из мусорных символов (например "//" -> "__").
        return fallback

    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
        if not text:
            text = fallback

    # Имя не может заканчиваться точкой или пробелом (Windows).
    text = text.rstrip(" .")
    if not text:
        return fallback

    root = text.split(".")[0].upper()
    if root in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"

    return text or fallback


def build_result_filename(channel_name: str, when: Optional[datetime] = None,
                          max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Формирует имя файла: `YYYY-MM-DD_HH-MM-SS_<канал>.xlsx`."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    channel = safe_filename(channel_name, max_length=max_length)
    if len(channel) > max_length:
        channel = channel[:max_length].rstrip(" .") or FALLBACK_CHANNEL_NAME
    return f"{stamp}_{channel}.xlsx"


def unique_path(directory: Path, filename: str,
                max_attempts: int = MAX_SUFFIX_ATTEMPTS) -> Path:
    """Возвращает свободный путь: `name.xlsx`, `name_1.xlsx`, `name_2.xlsx`, ..."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(1, max_attempts + 1):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    # Крайний случай: добавляем метку времени.
    return directory / f"{stem}_{datetime.now().strftime('%H%M%S')}{suffix}"


# --------------------------------------------------------------------------- #
# Таблица
# --------------------------------------------------------------------------- #
def build_dataframe(records: Sequence[StarRecord]) -> pd.DataFrame:
    """Собирает DataFrame из записей (пустой — с правильными колонками)."""
    rows = [[clean_cell_value(value) for value in record.to_row()] for record in records]
    frame = pd.DataFrame(rows, columns=list(COLUMNS), dtype=object)
    return frame


def _autosize(worksheet, frame: pd.DataFrame) -> None:
    """Ширина колонок по содержимому (с ограничениями)."""
    for index, column in enumerate(frame.columns, start=1):
        values = frame[column].tolist()
        longest = max([len(str(column))] + [len(str(v)) for v in values if v is not None] or [0])
        width = min(max(longest + 2, MIN_COLUMN_WIDTH), MAX_COLUMN_WIDTH)
        worksheet.column_dimensions[get_column_letter(index)].width = width


def _style_worksheet(worksheet, frame: pd.DataFrame) -> None:
    """Жирная шапка, закреплённая первая строка, числовой формат для id, автофильтр."""
    header_font = Font(bold=True)
    for cell in worksheet[1]:
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")

    worksheet.freeze_panes = "A2"
    if len(frame.columns):
        last_column = get_column_letter(len(frame.columns))
        worksheet.auto_filter.ref = f"A1:{last_column}1"

    # id и количество звёзд — целыми числами, без экспоненциальной записи.
    for column_name in INTEGER_COLUMNS:
        if column_name not in frame.columns:
            continue
        index = list(frame.columns).index(column_name) + 1
        letter = get_column_letter(index)
        for cell in worksheet[letter][1:]:
            if cell.value is not None:
                cell.number_format = "0"

    # Ссылки на посты делаем кликабельными гиперссылками.
    if "post_link" in frame.columns:
        link_index = list(frame.columns).index("post_link") + 1
        link_letter = get_column_letter(link_index)
        link_font = Font(color="0563C1", underline="single")
        for cell in worksheet[link_letter][1:]:
            value = str(cell.value or "")
            if value.startswith(("https://", "http://")):
                cell.hyperlink = value
                cell.font = link_font

    _autosize(worksheet, frame)


def write_excel(records: Sequence[StarRecord], path: Path) -> None:
    """Записывает записи в .xlsx (лист `Stars`)."""
    frame = build_dataframe(records)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=EXCEL_SHEET_NAME, index=False)
        _style_worksheet(writer.sheets[EXCEL_SHEET_NAME], frame)


def export_records(
    records: Iterable[StarRecord],
    output_dir: Path,
    channel_name: str,
    filename_max_length: int = DEFAULT_MAX_LENGTH,
    create_empty_file: bool = False,
) -> Optional[Path]:
    """Сохраняет отчёт и возвращает путь к файлу (или None, если сохранять нечего).

    При `PermissionError`/`OSError` (файл открыт в Excel) файл сохраняется
    с суффиксом `_1`, `_2` и т. д.
    """
    records = list(records)
    if not records and not create_empty_file:
        logger.info("Звёзд не найдено — файл не создаётся.")
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = build_result_filename(channel_name, max_length=filename_max_length)
    path = unique_path(output_dir, filename)

    for attempt in range(MAX_SUFFIX_ATTEMPTS):
        try:
            write_excel(records, path)
            logger.info("Файл сохранён: %s (записей: %d)", path, len(records))
            return path
        except PermissionError as exc:
            logger.warning("Файл %s занят (%s) — пробуем другое имя.", path, exc)
        except OSError as exc:
            logger.warning("Ошибка записи %s (%s) — пробуем другое имя.", path, exc)

        stem, suffix = path.stem, path.suffix
        for index in range(1, MAX_SUFFIX_ATTEMPTS + 1):
            candidate = output_dir / f"{stem}_{index}{suffix}"
            if not candidate.exists():
                path = candidate
                break
        else:  # pragma: no cover - защитный вариант
            path = output_dir / f"{stem}_{datetime.now().strftime('%H%M%S')}{suffix}"
        _ = attempt

    logger.error("Не удалось сохранить файл после %d попыток.", MAX_SUFFIX_ATTEMPTS)
    return None


__all__ = [
    "build_dataframe", "build_result_filename", "export_records",
    "safe_filename", "unique_path", "write_excel",
]
