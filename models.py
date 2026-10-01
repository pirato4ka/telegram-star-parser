"""Модели данных: одна строка будущего Excel == одна пара «пост + отправитель звёзд»."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Optional

# Порядок колонок в лице `Stars` (совпадает с ТЗ, раздел 6).
COLUMNS: tuple[str, ...] = (
    "message_type",
    "reactor_type",
    "current_channel",
    "current_message_id",
    "original_channel",
    "original_message_id",
    "reactor_username",
    "reactor_id",
    "stars_count",
)

EXCEL_SHEET_NAME = "Stars"

# Значения колонки reactor_type.
REACTOR_USER = "user"
REACTOR_CHANNEL = "channel"
REACTOR_ANONYMOUS = "anonymous"
REACTOR_UNKNOWN = "unknown"

# Значение, когда сущность не удалось получить (нет access_hash в кэше сессии).
NOT_FOUND = "not_found"

# Значения колонки message_type.
MESSAGE_TYPES = (
    "text", "photo", "video", "document", "audio", "voice",
    "poll", "sticker", "gif", "webpage", "other",
)

# Признак пересылки: `video` -> `video(forward)`.
FORWARD_SUFFIX = "(forward)"


@dataclass(slots=True)
class StarRecord:
    """Одна запись о звёздах, отправленных конкретным отправителем на конкретный пост."""

    message_type: str
    reactor_type: str
    current_channel: str
    current_message_id: int
    original_channel: str
    original_message_id: Optional[int]
    reactor_username: str
    reactor_id: Optional[int]
    stars_count: int

    def to_dict(self) -> dict[str, Any]:
        """Словарь в порядке `COLUMNS`."""
        return {name: getattr(self, name) for name in COLUMNS}

    def to_row(self) -> list[Any]:
        """Список значений в порядке `COLUMNS`."""
        return [getattr(self, name) for name in COLUMNS]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StarRecord":
        """Создаёт запись из словаря (для тестов и обратной совместимости)."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(slots=True)
class ParseStats:
    """Статистика одного прохода по каналу."""

    requested: int = 0
    scanned: int = 0           # просмотрено сообщений (включая служебные)
    skipped_service: int = 0   # пропущено служебных сообщений
    with_stars: int = 0        # постов, где найдены звёзды
    reactors: int = 0          # всего записей (пар «пост + отправитель»)
    errors: int = 0            # ошибок, которые удалось пережить
    interrupted: bool = False  # парсинг прерван пользователем (Ctrl+C)

    def as_text(self) -> str:
        """Краткая сводка для консоли."""
        lines = [
            f"Проанализировано постов: {self.scanned}",
            f"Пропущено служебных сообщений: {self.skipped_service}",
            f"Постов со звёздами: {self.with_stars}",
            f"Записей в таблице: {self.reactors}",
        ]
        if self.errors:
            lines.append(f"Ошибок при обработке (см. parser.log): {self.errors}")
        if self.interrupted:
            lines.append("Внимание: парсинг прерван пользователем, сохранены собранные данные.")
        return "\n".join(lines)


__all__ = [
    "COLUMNS", "EXCEL_SHEET_NAME", "FORWARD_SUFFIX", "MESSAGE_TYPES", "NOT_FOUND",
    "ParseStats", "REACTOR_ANONYMOUS", "REACTOR_CHANNEL", "REACTOR_UNKNOWN",
    "REACTOR_USER", "StarRecord",
]
