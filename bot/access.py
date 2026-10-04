"""Middleware проверки прав доступа по ALLOWED_USER_IDS."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from utils import get_logger

logger = get_logger("bot")


class AccessMiddleware(BaseMiddleware):
    """Проверяет user_id входящего события по списку разрешённых ID (whitelist).

    Если пользователь не в whitelist:
    - логирует user_id с уровнем INFO в bot.log
    - отвечает «Доступ запрещён»
    - прерывает цепочку обработки.
    """

    def __init__(self, allowed_user_ids: set[int]) -> None:
        super().__init__()
        self.allowed_user_ids = allowed_user_ids

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
        if user_id not in self.allowed_user_ids:
            logger.info("Попытка доступа неавторизованного пользователя user_id=%s (@%s, %s)",
                        user_id, getattr(from_user, "username", None), getattr(from_user, "full_name", None))
            if isinstance(event, Message):
                await event.answer("⛔ Доступ запрещён.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Доступ запрещён.", show_alert=True)
            return None

        return await handler(event, data)
