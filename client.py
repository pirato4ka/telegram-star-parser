"""Создание и авторизация `TelegramClient` (пользовательский аккаунт, MTProto)."""

from __future__ import annotations

import asyncio
import getpass
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.tl import types

from config import AppConfig, session_file_path
from utils import format_seconds, get_logger, mask_phone

logger = get_logger("client")

# Ошибки уровня сети/соединения (в т. ч. asyncio.IncompleteReadError -> EOFError).
CONNECTION_ERRORS = (
    OSError,
    EOFError,
    ConnectionError,
    asyncio.TimeoutError,
    TimeoutError,
)
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 5


class AuthError(Exception):
    """Не удалось авторизоваться — дальнейшая работа невозможна."""


class ConnectionFailureError(AuthError):
    """Не удалось установить соединение с Telegram (нет интернета / заблокирован MTProto)."""


# --------------------------------------------------------------------------- #
# Создание клиента
# --------------------------------------------------------------------------- #
def build_client(config: AppConfig, base_dir: Path) -> TelegramClient:
    """Создаёт `TelegramClient` с «отпечатком» устройства из [SESSION]."""
    session_path = session_file_path(base_dir, config)

    proxy = config.proxy.as_tuple()
    if proxy:
        logger.info("Используется прокси: %s://%s:%s", config.proxy.proxy_type,
                    config.proxy.addr, config.proxy.port)

    client = TelegramClient(
        str(session_path),
        api_id=config.api.api_id,
        api_hash=config.api.api_hash,
        device_model=config.session.device_model,
        system_version=config.session.system_version,
        app_version=config.session.app_version,
        lang_code=config.session.lang_code,
        system_lang_code=config.session.lang_code,
        # FloodWait обрабатываем сами: ждём и повторяем запрос с выводом в консоль.
        flood_sleep_threshold=0,
        connection_retries=3,
        retry_delay=1,
        request_retries=3,
        timeout=30,
        proxy=proxy,
    )
    logger.debug("TelegramClient создан, сессия: %s.session", session_path.name)
    return client


# --------------------------------------------------------------------------- #
# Подключение
# --------------------------------------------------------------------------- #
async def connect(client: TelegramClient, attempts: int = CONNECT_ATTEMPTS) -> None:
    """Подключается к Telegram с повторными попытками при проблемах с сетью."""
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            await client.connect()
            if await client.is_connected():
                return
            last_error = RuntimeError("не удалось установить соединение")
        except CONNECTION_ERRORS as exc:
            last_error = exc
        except RPCError as exc:
            last_error = exc

        if attempt < attempts:
            logger.warning(
                "Ошибка подключения (%s). Повтор через %s... (попытка %d из %d)",
                last_error, CONNECT_RETRY_DELAY, attempt, attempts,
            )
            print(f"Нет соединения с Telegram ({last_error}). "
                  f"Повтор через {CONNECT_RETRY_DELAY} сек... ({attempt}/{attempts})")
            await asyncio.sleep(CONNECT_RETRY_DELAY)

    raise ConnectionFailureError(
        f"Не удалось подключиться к Telegram после {attempts} попыток: {last_error}.\n"
        "Проверьте интернет-соединение: MTProto (порт 443) должен быть доступен, "
        "при необходимости настройте прокси в conf.ini / окружении."
    )


# --------------------------------------------------------------------------- #
# Авторизация
# --------------------------------------------------------------------------- #
async def _sign_in_with_password(client: TelegramClient, config: AppConfig,
                                 max_attempts: int) -> None:
    """Ввод облачного пароля (2FA): сначала из конфига, затем вручную."""
    print("\nУ аккаунта включена двухфакторная аутентификация (облачный пароль).")
    for attempt in range(1, max_attempts + 1):
        password = config.api.cloud_password if attempt == 1 and config.api.cloud_password else None
        if not password:
            password = getpass.getpass("Введите облачный пароль (2FA): ")
        if not password:
            print("Пароль не может быть пустым.")
            continue
        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            remaining = max_attempts - attempt
            print("Неверный облачный пароль."
                  + (f" Осталось попыток: {remaining}." if remaining else ""))
        except FloodWaitError as exc:
            wait_seconds = exc.seconds + 1
            print(f"Слишком много попыток. Ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)

    raise AuthError("Не удалось пройти проверку облачного пароля (2FA).")


async def authorize(client: TelegramClient, config: AppConfig,
                    max_attempts: int = 3) -> types.User:
    """Авторизует пользователя: по готовой сессии или по коду из Telegram.

    Возвращает объект авторизованного пользователя.
    """
    await connect(client)

    if await client.is_user_authorized():
        me = await client.get_me()
        name = _user_name(me)
        print(f"Используется сохранённая сессия: {name} (id={getattr(me, 'id', '?')})")
        logger.info("Авторизация по сохранённой сессии: id=%s", getattr(me, "id", "?"))
        return me

    phone = config.api.phone
    print(f"Требуется вход в аккаунт {mask_phone(phone)}.")

    for attempt in range(1, max_attempts + 1):
        remaining = max_attempts - attempt
        try:
            sent = await client.send_code_request(phone)
        except (PhoneNumberInvalidError, PhoneNumberBannedError, PhoneNumberFloodError,
                ApiIdInvalidError) as exc:
            raise AuthError(f"Не удалось отправить код на {mask_phone(phone)}: {exc}") from exc
        except FloodWaitError as exc:
            wait_seconds = exc.seconds + 1
            print(f"Слишком много запросов кода. Ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)
            continue

        code = input("Введите код из Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            await _sign_in_with_password(client, config, max_attempts)
        except (PhoneCodeInvalidError, PhoneCodeEmptyError) as exc:
            print(f"Неверный код.{f' Осталось попыток: {remaining}.' if remaining else ''}")
            logger.warning("Неверный код авторизации: %s", exc)
            continue
        except PhoneCodeExpiredError:
            print("Срок действия кода истёк. Запрашиваем новый код...")
            continue
        except FloodWaitError as exc:
            wait_seconds = exc.seconds + 1
            print(f"Слишком много попыток входа. Ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)
            continue

        if await client.is_user_authorized():
            break
    else:
        raise AuthError("Не удалось авторизоваться: превышено количество попыток ввода кода.")

    me = await client.get_me()
    print(f"Авторизация выполнена: {_user_name(me)} (id={getattr(me, 'id', '?')})")
    logger.info("Авторизация выполнена успешно, сессия сохранена.")
    return me


def _user_name(user: Optional[types.User]) -> str:
    """Имя пользователя для вывода в консоль."""
    if user is None:
        return "неизвестный пользователь"
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    name = " ".join(part for part in parts if part).strip()
    return name or str(getattr(user, "id", "?"))


__all__ = ["AuthError", "ConnectionFailureError", "authorize", "build_client",
           "connect"]
