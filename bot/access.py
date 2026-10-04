"""Middleware проверки прав доступа (whitelist) и уведомления администраторов."""

from __future__ import annotations

import html
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from bot.user_store import UserStore
from utils import get_logger

logger = get_logger("bot")

# Не чаще одного уведомления на пользователя в этом интервале (сек).
ADMIN_NOTIFY_COOLDOWN = 600

DENIED_MESSAGE = "⛔ Доступ запрещён."


class AccessMiddleware(BaseMiddleware):
    """Проверяет user_id входящего события по списку разрешённых ID.

    Кто имеет доступ:
    * администраторы (`[BOT] ADMIN_IDS`);
    * статический whitelist из `conf.ini` (`[BOT] ALLOWED_USER_IDS`);
    * пользователи, которых админ добавил по id прямо в боте (`users.json`).

    Незнакомому пользователю отправляется «Доступ запрещён», а администраторам —
    уведомление с кнопкой «Добавить доступ», чтобы не искать id вручную.
    """

    def __init__(
        self,
        allowed_user_ids: set[int],
        *,
        admins: Optional[set[int]] = None,
        store: Optional[UserStore] = None,
        notify_admins: bool = True,
    ) -> None:
        super().__init__()
        self.static_allowed_user_ids = set(allowed_user_ids or set())
        self.admins = set(admins or set())
        self.store = store
        self.notify_admins = bool(notify_admins)
        self._last_notified: dict[int, float] = {}

    # -- права -------------------------------------------------------------- #
    def allowed_ids(self) -> set[int]:
        """Полный список разрешённых id с учётом добавленных в боте."""
        allowed = set(self.static_allowed_user_ids) | set(self.admins)
        if self.store is not None:
            allowed |= self.store.ids
        return allowed

    def is_allowed(self, user_id: int) -> bool:
        return int(user_id) in self.allowed_ids()

    # -- middleware --------------------------------------------------------- #
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if not from_user:
            return await handler(event, data)

        user_id = from_user.id
        if not self.is_allowed(user_id):
            logger.info(
                "Попытка доступа неавторизованного пользователя user_id=%s (@%s, %s)",
                user_id, getattr(from_user, "username", None),
                getattr(from_user, "full_name", None),
            )
            try:
                if isinstance(event, Message):
                    await event.answer(DENIED_MESSAGE)
                elif isinstance(event, CallbackQuery):
                    await event.answer(DENIED_MESSAGE, show_alert=True)
            except Exception as exc:  # ответ недоступен (например, сообщение удалено)
                logger.debug("Не удалось ответить пользователю %s: %s", user_id, exc)
            await self._notify_admins(event, from_user, data)
            return None

        return await handler(event, data)

    async def _notify_admins(
        self, event: TelegramObject, from_user: Any, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Сообщает администраторам, что незнакомый пользователь просит доступ."""
        if not self.notify_admins or not self.admins:
            return

        user_id = int(from_user.id)
        now = time.monotonic()
        last = self._last_notified.get(user_id)
        # Раньше «не отправляли» сравнивалось с 0.0, а time.monotonic() на свежем
        # сервере меньше кулдауна — уведомления админам не уходили вовсе.
        if last is not None and now - last < ADMIN_NOTIFY_COOLDOWN:
            return
        self._last_notified[user_id] = now

        # В aiogram Bot приходит в data; event.bot доступен не всегда.
        bot = (data or {}).get("bot") or getattr(event, "bot", None)
        if bot is None:
            return

        username = getattr(from_user, "username", None)
        full_name = getattr(from_user, "full_name", "") or ""
        text = (
            "🔔 <b>Запрос доступа к боту</b>\n\n"
            f"Пользователь: <b>{html.escape(full_name or 'без имени')}</b>\n"
            f"Username: {('@' + html.escape(str(username))) if username else '—'}\n"
            f"ID: <code>{user_id}</code>\n\n"
            "Добавить его в whitelist?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Добавить доступ", callback_data=f"adm:add:{user_id}"),
            InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"adm:skip:{user_id}"),
        ]])

        for admin_id in sorted(self.admins):
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as exc:  # админ мог не начать диалог с ботом
                logger.debug("Не удалось уведомить администратора %s: %s", admin_id, exc)


__all__ = ["AccessMiddleware", "DENIED_MESSAGE"]
