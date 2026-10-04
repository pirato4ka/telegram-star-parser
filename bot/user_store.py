"""Хранилище пользователей бота, добавленных администратором.

Whitelist из `conf.ini` (`[BOT] ALLOWED_USER_IDS`) — «старый» способ доступа.
Дополнительно администратор может добавлять и удалять пользователей прямо в
боте по id; такие пользователи хранятся в `users.json` рядом с приложением.

Формат файла::

    {
      "version": 1,
      "users": {
        "123456789": {
          "username": "vasya",
          "full_name": "Вася",
          "added_by": 111222333,
          "added_at": "2026-10-04T12:00:00+00:00"
        }
      }
    }

Запись атомарная (временный файл + `os.replace`), поэтому падение в момент
сохранения не портит уже добавленных пользователей.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from utils import get_logger

logger = get_logger("bot")

USERS_FILE_NAME = "users.json"
STORE_VERSION = 1


@dataclass(slots=True)
class BotUser:
    """Пользователь, добавленный администратором."""

    user_id: int
    username: str = ""
    full_name: str = ""
    added_by: Optional[int] = None
    added_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def title(self) -> str:
        """Человекочитаемое представление для списков в боте."""
        if self.username:
            return f"@{self.username.lstrip('@')}"
        if self.full_name:
            return self.full_name
        return f"id{self.user_id}"


class UserStore:
    """Список пользователей, которым админ открыл доступ к боту."""

    def __init__(self, path: Path | str = USERS_FILE_NAME) -> None:
        self.path = Path(path)
        self._users: dict[int, BotUser] = {}
        self.load()

    # -- чтение/запись ------------------------------------------------------ #
    def load(self) -> None:
        """Читает файл с диска. Повреждённый файл не роняет бота."""
        self._users = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("Не удалось прочитать %s (%s) — список добавленных пользователей пуст.",
                         self.path, exc)
            return

        users = raw.get("users", raw) if isinstance(raw, dict) else {}
        if not isinstance(users, dict):
            logger.warning("Файл %s имеет неожиданный формат — список пользователей пуст.", self.path)
            return

        for key, value in users.items():
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                logger.warning("Пропущен некорректный id пользователя в %s: %r", self.path, key)
                continue
            data = value if isinstance(value, dict) else {}
            self._users[user_id] = BotUser(
                user_id=user_id,
                username=str(data.get("username") or ""),
                full_name=str(data.get("full_name") or ""),
                added_by=data.get("added_by"),
                added_at=str(data.get("added_at") or ""),
            )
        logger.info("Загружено добавленных пользователей: %d", len(self._users))

    def save(self) -> None:
        """Сохраняет список на диск (атомарно)."""
        payload = {
            "version": STORE_VERSION,
            "users": {str(uid): asdict(user) for uid, user in sorted(self._users.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name, suffix=".tmp", delete=False,
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False, indent=2)
                tmp_path = tmp.name
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.error("Не удалось сохранить %s: %s", self.path, exc)

    # -- доступ ------------------------------------------------------------- #
    @property
    def ids(self) -> set[int]:
        """id всех добавленных пользователей."""
        return set(self._users)

    def __len__(self) -> int:
        return len(self._users)

    def __contains__(self, user_id: object) -> bool:
        try:
            return int(user_id) in self._users  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    def get(self, user_id: int) -> Optional[BotUser]:
        return self._users.get(int(user_id))

    def all(self) -> list[BotUser]:
        """Список пользователей: сначала добавленные раньше."""
        return sorted(self._users.values(), key=lambda u: (u.added_at, u.user_id))

    # -- изменение ---------------------------------------------------------- #
    def add(
        self,
        user_id: int,
        *,
        username: str = "",
        full_name: str = "",
        added_by: Optional[int] = None,
    ) -> tuple[bool, BotUser]:
        """Добавляет пользователя. Возвращает `(создан_заново, запись)`."""
        user_id = int(user_id)
        existing = self._users.get(user_id)
        created = existing is None
        user = existing or BotUser(user_id=user_id, added_by=added_by)
        if username:
            user.username = username.lstrip("@")
        if full_name:
            user.full_name = full_name
        if added_by is not None:
            user.added_by = int(added_by)
        if created:
            user.added_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._users[user_id] = user
        self.save()
        logger.info("Пользователь %s добавлен в whitelist (кем: %s)", user_id, added_by)
        return created, user

    def remove(self, user_id: int) -> bool:
        """Удаляет пользователя. `True`, если он был в списке."""
        user_id = int(user_id)
        if user_id not in self._users:
            return False
        del self._users[user_id]
        self.save()
        logger.info("Пользователь %s удалён из whitelist.", user_id)
        return True

    def extend(self, user_ids: Iterable[int], *, added_by: Optional[int] = None) -> int:
        """Добавляет сразу несколько id (миграция из conf.ini). Возвращает число добавленных."""
        added = 0
        for user_id in user_ids:
            try:
                created, _ = self.add(int(user_id), added_by=added_by)
            except (TypeError, ValueError):
                continue
            added += 1 if created else 0
        return added


__all__ = ["BotUser", "USERS_FILE_NAME", "UserStore"]
