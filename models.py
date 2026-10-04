"""Модели данных: одна строка будущего Excel == одна пара «пост + отправитель звёзд»."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any, Optional

from utils import format_seconds

# Порядок колонок в листе `Stars` (совпадает с ТЗ, раздел 6 + ссылка на пост).
COLUMNS: tuple[str, ...] = (
    "message_type",
    "reactor_type",
    "current_channel",
    "current_message_id",
    "post_link",
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

# Значение колонки `username`, когда у донатера username отсутствует.
# По ТЗ: «в колонке username писать его при наличии, при отсутствии писать отсутствует».
USERNAME_MISSING = "отсутствует"

# Подпись строки с анонимными донатами (у них username не может быть известен).
ANONYMOUS_LABEL = "(анонимы)"

# Telegram-username: 5-32 символа, латиница/цифры/подчёркивание, обязательна буква.
USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")


def looks_like_username(value: Any) -> bool:
    """Похоже ли значение на настоящий Telegram-username (а не на имя/not_found)."""
    text = str(value or "").strip()
    if not text or text in (NOT_FOUND, USERNAME_MISSING, ANONYMOUS_LABEL):
        return False
    return bool(USERNAME_RE.match(text))

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
    post_link: str = ""
    # Есть ли у отправителя настоящий username (а не имя/фамилия/not_found).
    # В колонки Excel не попадает: используется для подстановки
    # `USERNAME_MISSING` («отсутствует») в отчётах.
    reactor_has_username: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Словарь в порядке `COLUMNS`."""
        return {name: getattr(self, name) for name in COLUMNS}

    def to_row(self) -> list[Any]:
        """Список значений в порядке `COLUMNS`."""
        return [getattr(self, name) for name in COLUMNS]

    def username_for_report(self) -> str:
        """Username для отчётов: он сам либо «отсутствует»; анонимы — отдельная строка."""
        if self.reactor_type == REACTOR_ANONYMOUS:
            return USERNAME_MISSING
        if self.reactor_has_username or looks_like_username(self.reactor_username):
            value = str(self.reactor_username).strip()
            return value
        return USERNAME_MISSING

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StarRecord":
        """Создаёт запись из словаря (для тестов и обратной совместимости)."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(slots=True)
class ParseStats:
    """Статистика одного прохода по каналу."""

    # Сколько постов планировалось обработать. `None` — «все посты» / число
    # заранее неизвестно (тогда в отчётах пишется только факт, без «из N»).
    requested: Optional[int] = None
    scanned: int = 0           # просмотрено сообщений (включая служебные)
    skipped_service: int = 0   # пропущено служебных сообщений
    with_stars: int = 0        # постов, где найдены звёзды
    reactors: int = 0          # всего записей (пар «пост + отправитель»)
    errors: int = 0            # ошибок, которые удалось пережить
    unparsed: int = 0          # не удалось прочитать из истории (RPC/сетевые обрывы)
    interrupted: bool = False  # парсинг прерван пользователем (Ctrl+C)
    exhausted: bool = False    # история канала закончилась раньше лимита
    # --- скорость (заполняются при парсинге) ---
    api_requests: int = 0      # сетевых запросов к Telegram за прогон
    not_found: int = 0         # записей, где отправителя не удалось расшифровать
    elapsed: float = 0.0       # время парсинга, сек
    waited: float = 0.0        # суммарное ожидание между запросами, сек

    @property
    def rate(self) -> float:
        """Скорость парсинга, сообщений в секунду."""
        return self.scanned / self.elapsed if self.elapsed > 0 else 0.0

    def set_requested(self, limit: int, unlimited: bool = False) -> None:
        """Задаёт план по количеству постов.

        `unlimited=True` (режим «все посты» / «по годам») означает, что заранее
        известно только то, что постов «сколько есть» — в отчёте не должно
        появляться «из 1 000 000».
        """
        self.requested = None if unlimited else int(limit)

    def finalize_requested(self) -> None:
        """Уточняет план по факту: если история канала исчерпана, число известно точно.

        Без этого при выборе «все посты» в отчёте висело «Обработано: 6957 из 1000000»,
        не привязанное к реальному количеству постов в канале.
        """
        if self.exhausted and not self.unparsed and not self.interrupted:
            self.requested = self.scanned

    def as_text(self) -> str:
        """Краткая сводка для консоли."""
        processed = f"Проанализировано постов: {self.scanned}"
        if self.requested and self.requested != self.scanned:
            processed += f" из {self.requested}"
        lines = [
            processed,
            f"Пропущено служебных сообщений: {self.skipped_service}",
            f"Постов со звёздами: {self.with_stars}",
            f"Записей в таблице: {self.reactors}",
        ]
        if self.not_found:
            lines.append(
                f"Не расшифровано отправителей (not_found): {self.not_found} "
                "(помогает прогрев кэша: --warmup)"
            )
        if self.errors:
            lines.append(f"Ошибок при обработке (см. parser.log): {self.errors}")
        if self.elapsed > 0:
            lines.append(
                f"Время парсинга: {format_seconds(self.elapsed)} "
                f"({self.rate:.0f} сообщ./с, сетевых запросов: {self.api_requests}, "
                f"ожидание между запросами: {format_seconds(self.waited)})"
            )
        if self.interrupted:
            lines.append("Внимание: парсинг прерван пользователем, сохранены собранные данные.")
        return "\n".join(lines)


__all__ = [
    "ANONYMOUS_LABEL", "COLUMNS", "EXCEL_SHEET_NAME", "FORWARD_SUFFIX",
    "MESSAGE_TYPES", "NOT_FOUND", "ParseStats", "REACTOR_ANONYMOUS",
    "REACTOR_CHANNEL", "REACTOR_UNKNOWN", "REACTOR_USER", "StarRecord",
    "USERNAME_MISSING", "looks_like_username",
]
