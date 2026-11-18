"""Поиск донора по id / username по всем Excel-отчётам в каталоге `output/`.

Модуль рассчитан на то, что отчётов много и они большие, поэтому:

* файлы читаются один раз и складываются в SQLite-индекс
  (`output/.donor_index.sqlite`) — повторный поиск не открывает Excel заново,
  а делает выборку по индексу (`reactor_id`, `username_lc`);
* при поиске пересматриваются только новые/изменённые файлы (сравнение
  `mtime` + размер), удалённые файлы вычищаются из индекса;
* поддерживаются как текущий формат листа «Донаты» (6 колонок), так и старые
  форматы: лист `Stars` из CLI-версии (10 колонок) и лист «Сводка» с русскими
  шапками («id», «username», «звёзд всего», «постов», «каналы»).

Результат поиска агрегируется по каналам и сохраняется в файл
`<username_или_id>_парсинг.xlsx` рядом с исходными отчётами.
"""

from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from exporter import safe_filename, unique_path
from utils import clean_cell_value, get_logger

logger = get_logger("donor_search")

INDEX_DB_NAME = ".donor_index.sqlite"
REPORT_SUFFIX = "парсинг"

DONATIONS_SHEET_NAME = "Найдено"
CHANNELS_SHEET_NAME = "По каналам"
INFO_SHEET_NAME = "Инфо"

# Колонки отчёта поиска: сначала ровно те же поля, что и в листе «Донаты».
REPORT_COLUMNS = (
    "reactor_id",
    "reactor_username",
    "stars_count",
    "post_link",
    "reactor_type",
    "original_message_id",
    "current_channel",
    "source",
)

CHANNEL_COLUMNS = ("канал", "донатов", "звёзд", "файлов")

INTEGER_REPORT_COLUMNS = ("reactor_id", "stars_count", "original_message_id")

# Служебные листы, в которых не может быть донатов.
SKIPPED_SHEETS = {"инфо", "info"}

# Значения, которые не являются username (заглушки парсера).
USERNAME_PLACEHOLDERS = {"", "-", "—", "отсутствует", "not_found", "(анонимы)", "аноним"}

MAX_HEADER_SCAN_ROWS = 10
MAX_REPORT_CHANNELS_IN_TEXT = 15

# Каноническое поле -> варианты написания шапки (в нормализованном виде).
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "reactor_id": (
        "reactorid", "id", "userid", "senderid", "fromid", "donorid",
        "telegramid", "ид", "идотправителя", "иддонатера",
    ),
    "reactor_username": (
        "reactorusername", "username", "user", "login", "handle", "nickname",
        "ник", "юзернейм",
    ),
    "stars_count": (
        "starscount", "stars", "starstotal", "starsamount", "звезды", "звезд",
        "звездвсего", "звездотправлено", "количество",
    ),
    "post_link": ("postlink", "link", "url", "posturl", "ссылка", "ссылканапост"),
    "reactor_type": ("reactortype", "type", "тип"),
    "original_message_id": (
        "originalmessageid", "messageid", "msgid", "postid",
        "idпоста", "idсообщения",
    ),
    "current_channel": (
        "currentchannel", "channel", "channels", "channelusername",
        "канал", "каналы",
    ),
}

_ALIAS_TO_FIELD: dict[str, str] = {
    alias: canonical
    for canonical, aliases in HEADER_ALIASES.items()
    for alias in aliases
}

# https://t.me/channel/123, https://t.me/s/channel/123, t.me/channel/123
_POST_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?t\.me/(?:s/)?([A-Za-z0-9_]+)/\d+")



# --------------------------------------------------------------------------- #
# Модели результата
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class DonorRow:
    """Одна найденная строка (донат) из сохранённого отчёта."""

    reactor_id: Optional[int]
    reactor_username: str
    stars_count: int
    post_link: str
    reactor_type: str
    original_message_id: Optional[int]
    current_channel: str
    source: str  # «файл.xlsx / лист»

    def to_dict(self) -> dict[str, Any]:
        return {
            "reactor_id": self.reactor_id,
            "reactor_username": self.reactor_username,
            "stars_count": self.stars_count,
            "post_link": self.post_link,
            "reactor_type": self.reactor_type,
            "original_message_id": self.original_message_id,
            "current_channel": self.current_channel,
            "source": self.source,
        }


@dataclass(slots=True)
class ChannelStat:
    """Сводка по одному каналу для найденного донора."""

    channel: str
    donations: int = 0
    stars: int = 0
    files: set[str] = field(default_factory=set)

    @property
    def files_count(self) -> int:
        return len(self.files)


@dataclass(slots=True)
class DonorSearchResult:
    """Результат поиска донора по всем отчётам."""

    query: str
    query_kind: str  # "id" | "username"
    query_value: str
    rows: list[DonorRow] = field(default_factory=list)
    channels: list[ChannelStat] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def total_stars(self) -> int:
        return sum(row.stars_count for row in self.rows)

    @property
    def files_with_hits(self) -> int:
        return len({row.source.split(" / ")[0] for row in self.rows})

    @property
    def report_name(self) -> str:
        """Имя для файла отчёта: username, если он есть, иначе id."""
        for row in self.rows:
            if row.reactor_username:
                return row.reactor_username
        return self.query_value


# --------------------------------------------------------------------------- #
# Разбор запроса
# --------------------------------------------------------------------------- #
def parse_donor_query(raw: str) -> tuple[str, Any]:
    """Разбирает запрос пользователя в пару («id» | «username», значение).

    Поддерживаются формы `123456789`, `id123456789`, `@username`, `username`
    и ссылка на профиль `https://t.me/username`.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустой запрос: пришлите id или @username донора.")

    if "t.me/" in text.lower():
        text = text.rstrip("/").rsplit("/", 1)[-1]

    text = text.strip().lstrip("@").strip()
    if not text:
        raise ValueError("Пустой запрос: пришлите id или @username донора.")

    lowered = text.lower()
    if lowered.startswith("id") and text[2:].strip().isdigit():
        text = text[2:].strip()
        lowered = text

    if lowered.isdigit():
        value = int(lowered)
        if value <= 0:
            raise ValueError("id должен быть положительным числом.")
        return "id", value

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", text):
        raise ValueError(
            "Не похоже ни на id, ни на username. Примеры: 123456789 или @username."
        )
    return "username", lowered


# --------------------------------------------------------------------------- #
# Разбор Excel
# --------------------------------------------------------------------------- #
def _norm_header(value: Any) -> str:
    """Нормализует шапку: нижний регистр, без пробелов/подчёркиваний, ё -> е."""
    text = str(value or "").strip().lower().replace("ё", "е")
    return "".join(ch for ch in text if ch.isalnum())


def _map_headers(values: Iterable[Any]) -> dict[str, int]:
    """Сопоставляет шапку листа каноническим полям: поле -> индекс колонки."""
    mapping: dict[str, int] = {}
    for index, value in enumerate(values):
        canonical = _ALIAS_TO_FIELD.get(_norm_header(value))
        if canonical and canonical not in mapping:
            mapping[canonical] = index
    if "reactor_id" not in mapping and "reactor_username" not in mapping:
        return {}
    if len(mapping) < 2:
        return {}
    return mapping


def _to_int(value: Any) -> Optional[int]:
    """Приводит значение ячейки к int (Excel может отдать 123.0 или '123')."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "")
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    try:
        return int(text)
    except ValueError:
        return None


def _clean_username(value: Any) -> str:
    """Username без '@'; служебные значения («отсутствует», not_found) -> пусто."""
    text = str(value or "").strip().lstrip("@").strip()
    if text.lower() in USERNAME_PLACEHOLDERS:
        return ""
    return text


def _clean_channel(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in ("-", "—"):
        return ""
    # В старом листе «Сводка» каналы перечислены через запятую — берём первый.
    return text.split(",")[0].strip()


def channel_from_link(link: str) -> str:
    """Канал из ссылки на пост: https://t.me/some_channel/123 -> @some_channel."""
    match = _POST_LINK_RE.search(str(link or ""))
    if not match:
        return ""
    return f"@{match.group(1)}"


def _build_hit(raw: tuple, mapping: dict[str, int], source: str) -> Optional[DonorRow]:
    """Превращает строку листа в DonorRow (или None, если донора в ней нет)."""
    def value(field_name: str) -> Any:
        index = mapping.get(field_name)
        if index is None or index >= len(raw):
            return None
        return raw[index]

    reactor_id = _to_int(value("reactor_id"))
    username = _clean_username(value("reactor_username"))
    if reactor_id is None and not username:
        return None

    post_link = str(value("post_link") or "").strip()
    channel = _clean_channel(value("current_channel")) or channel_from_link(post_link)
    reactor_type = str(value("reactor_type") or "").strip()
    if not reactor_type:
        reactor_type = "user" if reactor_id is not None else "unknown"

    return DonorRow(
        reactor_id=reactor_id,
        reactor_username=username,
        stars_count=_to_int(value("stars_count")) or 0,
        post_link=post_link,
        reactor_type=reactor_type,
        original_message_id=_to_int(value("original_message_id")),
        current_channel=channel,
        source=source,
    )


def _sheet_priority(sheet_name: str) -> int:
    """Чем меньше число, тем «точнее» лист: «Донаты» важнее «Сводки»."""
    name = sheet_name.strip().lower()
    if name in ("донаты", "донат", "donations", "donation"):
        return 0
    if name in ("stars", "звёзды", "звезды"):
        return 1
    return 2


def _extract_sheet_rows(sheet) -> tuple[dict[str, int], list[tuple]]:
    """Находит шапку листа и возвращает (mapping, строки данных)."""
    mapping: dict[str, int] = {}
    rows: list[tuple] = []
    header_found = False
    scanned = 0

    for raw in sheet.iter_rows(values_only=True):
        if raw is None or not any(cell not in (None, "") for cell in raw):
            continue
        if not header_found:
            scanned += 1
            candidate = _map_headers(raw)
            if candidate:
                mapping = candidate
                header_found = True
            elif scanned >= MAX_HEADER_SCAN_ROWS:
                return {}, []
            continue
        rows.append(raw)

    if not header_found:
        return {}, []
    return mapping, rows


def _scan_workbook(path: Path) -> list[DonorRow]:
    """Читает один .xlsx и возвращает строки доноров из самого точного листа.

    Дубли не считаются дважды: если в файле есть лист «Донаты», лист «Сводка»
    того же файла игнорируется (там те же донаты, но уже просуммированные).
    """
    hits: list[DonorRow] = []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        best_priority: Optional[int] = None
        for sheet in workbook.worksheets:
            title = str(sheet.title)
            if title.strip().lower() in SKIPPED_SHEETS:
                continue
            priority = _sheet_priority(title)
            if best_priority is not None and priority > best_priority:
                continue
            mapping, rows = _extract_sheet_rows(sheet)
            if not mapping:
                continue
            if best_priority is None or priority < best_priority:
                best_priority = priority
                hits.clear()
            source = f"{path.name} / {title}"
            for raw in rows:
                hit = _build_hit(raw, mapping, source)
                if hit is not None:
                    hits.append(hit)
    finally:
        workbook.close()
    return hits


# --------------------------------------------------------------------------- #
# SQLite-индекс
# --------------------------------------------------------------------------- #
def _index_path(output_dir: Path) -> Path:
    return output_dir / INDEX_DB_NAME


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            sheet TEXT NOT NULL,
            reactor_id INTEGER,
            username_lc TEXT,
            username TEXT,
            stars INTEGER NOT NULL DEFAULT 0,
            channel TEXT NOT NULL DEFAULT '',
            post_link TEXT NOT NULL DEFAULT '',
            reactor_type TEXT NOT NULL DEFAULT '',
            original_message_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_rows_reactor_id ON rows(reactor_id);
        CREATE INDEX IF NOT EXISTS idx_rows_username ON rows(username_lc);
        CREATE INDEX IF NOT EXISTS idx_rows_file ON rows(file_path);
        """
    )
    return connection


def _reindex_file(connection: sqlite3.Connection, path: Path,
                  mtime: float, size: int) -> int:
    """Переиндексирует один файл. Ошибки чтения не роняют поиск."""
    key = str(path.resolve())
    connection.execute("DELETE FROM rows WHERE file_path = ?", (key,))
    count = 0
    try:
        for hit in _scan_workbook(path):
            connection.execute(
                "INSERT INTO rows (file_path, sheet, reactor_id, username_lc, username,"
                " stars, channel, post_link, reactor_type, original_message_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    hit.source.split(" / ", 1)[-1],
                    hit.reactor_id,
                    hit.reactor_username.lower() if hit.reactor_username else None,
                    hit.reactor_username,
                    hit.stars_count,
                    hit.current_channel,
                    hit.post_link,
                    hit.reactor_type,
                    hit.original_message_id,
                ),
            )
            count += 1
    except Exception as exc:  # noqa: BLE001 - битый/занятый файл не должен ломать поиск
        logger.warning("Не удалось проиндексировать %s: %s", path.name, exc)
        connection.execute("DELETE FROM rows WHERE file_path = ?", (key,))
        count = 0

    connection.execute(
        "INSERT OR REPLACE INTO files (path, mtime, size, indexed_at) VALUES (?, ?, ?, ?)",
        (key, mtime, size, datetime.now().isoformat(timespec="seconds")),
    )
    return count


def refresh_index(output_dir: Path) -> int:
    """Синхронизирует индекс с каталогом; возвращает число .xlsx на диске."""
    files = sorted(
        path for path in output_dir.glob("*.xlsx")
        if path.is_file()
        and not path.name.startswith("~$")
        # Отчёты самого поиска (`<...>_парсинг.xlsx`) лежат в том же каталоге,
        # но индексировать их нельзя — иначе совпадения начнут дублироваться.
        and not path.stem.endswith(REPORT_SUFFIX)
    )
    connection = _connect(_index_path(output_dir))
    try:
        known = {
            path_key: (mtime, size)
            for path_key, mtime, size in connection.execute("SELECT path, mtime, size FROM files")
        }
        on_disk: set[str] = set()
        for path in files:
            key = str(path.resolve())
            on_disk.add(key)
            stat = path.stat()
            if known.get(key) == (stat.st_mtime, stat.st_size):
                continue
            added = _reindex_file(connection, path, stat.st_mtime, stat.st_size)
            logger.info("Проиндексирован %s: строк %d", path.name, added)
        for stale in set(known) - on_disk:
            connection.execute("DELETE FROM rows WHERE file_path = ?", (stale,))
            connection.execute("DELETE FROM files WHERE path = ?", (stale,))
        connection.commit()
    finally:
        connection.close()
    return len(files)


# --------------------------------------------------------------------------- #
# Поиск
# --------------------------------------------------------------------------- #
_SELECT_ROWS = (
    "SELECT file_path, sheet, reactor_id, username, stars, channel, post_link,"
    " reactor_type, original_message_id FROM rows"
)


def _fetch_rows(connection: sqlite3.Connection, sql: str, params: tuple) -> list[DonorRow]:
    rows: list[DonorRow] = []
    for file_path, sheet, reactor_id, username, stars, channel, link, rtype, orig_id in connection.execute(sql, params):
        rows.append(
            DonorRow(
                reactor_id=reactor_id,
                reactor_username=str(username or ""),
                stars_count=int(stars or 0),
                post_link=str(link or ""),
                reactor_type=str(rtype or ""),
                original_message_id=orig_id,
                current_channel=str(channel or ""),
                source=f"{Path(file_path).name} / {sheet}",
            )
        )
    return rows


def _aggregate_channels(rows: Iterable[DonorRow]) -> list[ChannelStat]:
    stats: dict[str, ChannelStat] = {}
    for row in rows:
        channel = row.current_channel or "не определён"
        stat = stats.get(channel)
        if stat is None:
            stat = ChannelStat(channel=channel)
            stats[channel] = stat
        stat.donations += 1
        stat.stars += row.stars_count
        stat.files.add(row.source.split(" / ")[0])
    return sorted(stats.values(), key=lambda item: (-item.donations, -item.stars, item.channel))


def search_donor(query: str, output_dir: Path | str = Path("output")) -> DonorSearchResult:
    """Ищет донора по id или username во всех сохранённых отчётах.

    Первый вызов индексирует все .xlsx, последующие — только новые/изменённые
    файлы, а сам поиск идёт по SQLite-индексу (без чтения Excel).
    """
    kind, value = parse_donor_query(query)
    result = DonorSearchResult(query=query.strip(), query_kind=kind, query_value=str(value))

    out_dir = Path(output_dir)
    if not out_dir.exists():
        return result

    result.files_scanned = refresh_index(out_dir)

    connection = _connect(_index_path(out_dir))
    try:
        if kind == "id":
            rows = _fetch_rows(connection, f"{_SELECT_ROWS} WHERE reactor_id = ?", (int(value),))
        else:
            rows = _fetch_rows(connection, f"{_SELECT_ROWS} WHERE username_lc = ?",
                               (str(value).lower(),))
    finally:
        connection.close()

    rows.sort(key=lambda row: (row.current_channel, -row.stars_count,
                               (row.reactor_username or "").lower(), row.post_link))
    result.rows = rows
    result.channels = _aggregate_channels(rows)
    return result


# --------------------------------------------------------------------------- #
# Отчёт
# --------------------------------------------------------------------------- #
def _style_sheet(worksheet, frame: pd.DataFrame) -> None:
    """Жирная шапка, закреплённая строка, автофильтр, целые числа, ссылки."""
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="left", vertical="center")

    worksheet.freeze_panes = "A2"
    headers = list(frame.columns)
    if headers:
        worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    for column_name in INTEGER_REPORT_COLUMNS:
        if column_name not in headers:
            continue
        letter = get_column_letter(headers.index(column_name) + 1)
        for cell in worksheet[letter][1:]:
            if cell.value is None or str(cell.value).strip() == "":
                continue
            try:
                cell.value = int(cell.value)
                cell.number_format = "0"
            except (ValueError, TypeError):
                pass

    if "post_link" in headers:
        letter = get_column_letter(headers.index("post_link") + 1)
        link_font = Font(color="0563C1", underline="single")
        for cell in worksheet[letter][1:]:
            link = str(cell.value or "")
            if link.startswith(("https://", "http://")):
                cell.hyperlink = link
                cell.font = link_font

    for index, column_name in enumerate(headers, start=1):
        width = len(str(column_name))
        for value in frame[column_name].tolist():
            if value is not None:
                width = max(width, len(str(value)))
        worksheet.column_dimensions[get_column_letter(index)].width = min(max(width + 2, 10), 60)


def build_donor_report(result: DonorSearchResult,
                       output_dir: Path | str = Path("output")) -> Optional[Path]:
    """Сохраняет отчёт `<username_или_id>_парсинг.xlsx` и возвращает путь."""
    if not result.rows:
        return None

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = safe_filename(result.report_name, max_length=60, fallback=str(result.query_value))
    path = unique_path(out_dir, f"{base}_{REPORT_SUFFIX}.xlsx")

    rows = [row.to_dict() for row in result.rows]
    frame = pd.DataFrame(
        [[clean_cell_value(row[name]) for name in REPORT_COLUMNS] for row in rows],
        columns=list(REPORT_COLUMNS),
    )

    channels_frame = pd.DataFrame(
        [
            [stat.channel, stat.donations, stat.stars, stat.files_count]
            for stat in result.channels
        ],
        columns=list(CHANNEL_COLUMNS),
    )

    info_frame = pd.DataFrame(
        [
            ["Запрос", result.query],
            ["Тип запроса", "id" if result.query_kind == "id" else "username"],
            ["Найдено строк", result.total_rows],
            ["Всего звёзд", result.total_stars],
            ["Каналов", len(result.channels)],
            ["Просканировано файлов", result.files_scanned],
            ["Файлов с совпадениями", result.files_with_hits],
            ["Дата формирования", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["Примечание", "Источник — ранее сохранённые Excel-отчёты (включая старые форматы)"],
        ],
        columns=["Параметр", "Значение"],
    )

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=DONATIONS_SHEET_NAME, index=False)
        _style_sheet(writer.sheets[DONATIONS_SHEET_NAME], frame)

        channels_frame.to_excel(writer, sheet_name=CHANNELS_SHEET_NAME, index=False)
        _style_sheet(writer.sheets[CHANNELS_SHEET_NAME], channels_frame)

        info_frame.to_excel(writer, sheet_name=INFO_SHEET_NAME, index=False)
        _style_sheet(writer.sheets[INFO_SHEET_NAME], info_frame)

    logger.info(
        "Отчёт по донору сохранён: %s (строк: %d, каналов: %d)",
        path, result.total_rows, len(result.channels),
    )
    return path


def format_search_summary(result: DonorSearchResult) -> str:
    """Текстовая сводка для чата: всего найдено и разбивка по каналам.

    Текст уходит в Telegram с `parse_mode="HTML"`, поэтому запрос и названия
    каналов экранируются: в username/названии канала может встретиться `&`/`<`.
    """
    query = html.escape(result.query)
    if not result.rows:
        return (
            f"🔎 Донор <code>{query}</code> не найден в сохранённых отчётах.\n"
            f"<i>Проверено файлов: {result.files_scanned}</i>"
        )

    lines = [
        "🔎 <b>Донор найден</b>",
        f"Запрос: <code>{query}</code>",
        f"Строк: <b>{result.total_rows}</b> · звёзд: <b>{result.total_stars}</b> · "
        f"каналов: <b>{len(result.channels)}</b> · файлов: <b>{result.files_with_hits}</b>",
        "",
        "<b>По каналам</b>",
    ]
    for stat in result.channels[:MAX_REPORT_CHANNELS_IN_TEXT]:
        lines.append(
            f"• {html.escape(stat.channel)} — <b>{stat.donations}</b> донатов, {stat.stars} звёзд"
        )
    if len(result.channels) > MAX_REPORT_CHANNELS_IN_TEXT:
        lines.append(
            f"<i>…и ещё {len(result.channels) - MAX_REPORT_CHANNELS_IN_TEXT} каналов в файле</i>"
        )
    return "\n".join(lines)


__all__ = [
    "CHANNELS_SHEET_NAME", "DONATIONS_SHEET_NAME", "INDEX_DB_NAME", "INFO_SHEET_NAME",
    "ChannelStat", "DonorRow", "DonorSearchResult", "build_donor_report",
    "channel_from_link", "format_search_summary", "parse_donor_query",
    "refresh_index", "search_donor",
]
