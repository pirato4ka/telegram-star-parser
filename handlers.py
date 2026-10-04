"""Парсинг сообщений канала и определение отправителей звёзд (Paid Reactions)."""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Optional, Sequence, Union

from telethon import TelegramClient, functions, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.errors.rpcerrorlist import (
    ChannelInvalidError,
    ChannelPrivateError,
    PeerIdInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl import types
from tqdm import tqdm

from models import (
    FORWARD_SUFFIX,
    NOT_FOUND,
    REACTOR_ANONYMOUS,
    REACTOR_CHANNEL,
    REACTOR_UNKNOWN,
    REACTOR_USER,
    ParseStats,
    StarRecord,
)
from speed import (
    DEFAULT_CONCURRENCY,
    DEFAULT_ENTITY_BATCH,
    DEFAULT_MESSAGE_BATCH,
    HISTORY_CHUNK_SIZE,
    MAX_CHANNEL_BATCH,
    MAX_ENTITY_BATCH,
    PARTICIPANTS_CHUNK_SIZE,
    RequestThrottle,
    SpeedProfile,
    is_batch_client,
)
from utils import (
    chunked,
    clean_cell_value,
    format_seconds,
    get_logger,
    remove_invisible,
)

logger = get_logger("handlers")

# Ошибки получения сущности: программа должна продолжать работу с доступными данными.
ENTITY_ERRORS = (
    ValueError,          # "Could not find the input entity ..." (нет access_hash в кэше)
    TypeError,
    PeerIdInvalidError,
    ChannelPrivateError,
    ChannelInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    RPCError,
    asyncio.TimeoutError,
)

CHANNEL_ERRORS = (
    ValueError,
    TypeError,
    PeerIdInvalidError,
    ChannelPrivateError,
    ChannelInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    RPCError,
    asyncio.TimeoutError,
)

# «Пустые»/недоступные сущности в ответе API: получить их не удалось.
# Набор зависит от версии слоя MTProto, поэтому берём только существующие типы.
_EMPTY_ENTITIES = tuple(
    getattr(types, name)
    for name in ("UserEmpty", "ChatEmpty", "ChatForbidden",
                 "ChannelEmpty", "ChannelForbidden")
    if isinstance(getattr(types, name, None), type)
)

# Пытаться ли получить отправителей, которых нет в кэше сессии (пачкой, hash=0).
DEFAULT_RESOLVE_UNKNOWN = True

PROGRESS_DESCRIPTION = "Парсинг сообщений"
WARMUP_DESCRIPTION = "Прогрев кэша участников"

_INVITE_RE = re.compile(r"^[+A-Za-z0-9_-]{10,}$")
_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.IGNORECASE
)

# Разделители списка каналов: запятая (основной), точка с запятой, перевод строки.
# В ссылках и username запятых не бывает, поэтому делить по ним безопасно.
CHANNEL_SEPARATORS_RE = re.compile(r"[,;\n\r]+")


class ChannelResolutionError(Exception):
    """Канал не найден, удалён, приватен или введён некорректно."""


# --------------------------------------------------------------------------- #
# Канал: разбор ввода и получение сущности
# --------------------------------------------------------------------------- #
def parse_channel_input(raw: str) -> Union[str, int]:
    """Приводит ввод пользователя к значению, понятному `client.get_entity`.

    Поддерживается: `username`, `@username`, `t.me/username`, `t.me/c/<id>`,
    `t.me/s/<username>`, `t.me/+invite`, числовой id и `-100...`.
    """
    text = (raw or "").strip().strip('"\'').strip()
    if not text:
        raise ChannelResolutionError("Пустой ввод. Укажите username или id канала.")

    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)  # убираем схему ссылки

    match = _LINK_RE.match(text)
    if match:
        rest = match.group(1).strip("/").split("?")[0].split("#")[0]
        parts = [part for part in rest.split("/") if part]
        if not parts:
            raise ChannelResolutionError(f"Не удалось разобрать ссылку: {raw!r}")
        # t.me/s/username, t.me/c/<id>
        is_channel_link = parts[0].lower() == "c"
        if parts[0].lower() in ("s", "c") and len(parts) > 1:
            parts = parts[1:]
        text = parts[0]
        if not text:
            raise ChannelResolutionError(f"Не удалось разобрать ссылку: {raw!r}")
        # t.me/c/<id> — «сырой» id канала, превращаем в -100<id>
        if is_channel_link and re.fullmatch(r"\d+", text):
            digits = text[3:] if text.startswith("100") and len(text) > 11 else text
            return utils.get_peer_id(types.PeerChannel(int(digits)))
        # t.me/joinchat/<hash> — пригласительная ссылка
        if parts[0].lower() in ("joinchat", "join") and len(parts) > 1:
            return f"https://t.me/joinchat/{parts[1]}"
    else:
        text = text.lstrip("@")

    # Пригласительная ссылка: +hash / t.me/+hash
    if text.startswith("+"):
        text = text[1:]
    if _INVITE_RE.match(text) and len(text) >= 16 and not text.lstrip("-").isdigit():
        return f"https://t.me/+{text}"

    # Числовой id: 1234567890 или -1001234567890
    if re.fullmatch(r"-?\d+", text):
        return int(text)

    if not text:
        raise ChannelResolutionError(f"Не удалось разобрать ввод: {raw!r}")
    return text


def split_channel_list(raw: str) -> list[str]:
    """Разбивает ввод пользователя на отдельные каналы.

    Разделители: запятая (основной), точка с запятой и перевод строки — удобно
    вставлять список каналов из блокнота. Пустые элементы отбрасываются,
    порядок ввода сохраняется.
    """
    parts: list[str] = []
    for part in CHANNEL_SEPARATORS_RE.split(raw or ""):
        part = part.strip().strip('"\'').strip()
        if part:
            parts.append(part)
    return parts


def parse_channels_input(raw: str) -> list[Union[str, int]]:
    """Разбирает несколько каналов, перечисленных через запятую.

    Пример: `@durov, t.me/telegram, -1001234567890`. Каждый элемент разбирается
    правилами `parse_channel_input`. Пустые элементы отбрасываются, повторы
    (без учёта регистра username) учитываются один раз, порядок ввода
    сохраняется — он же порядок очереди парсинга.
    """
    parts = split_channel_list(raw)
    if not parts:
        raise ChannelResolutionError(
            "Пустой ввод. Укажите каналы через запятую: @durov, t.me/telegram"
        )

    values: list[Union[str, int]] = []
    seen: set[Union[str, int]] = set()
    for part in parts:
        try:
            value = parse_channel_input(part)
        except ChannelResolutionError as exc:
            raise ChannelResolutionError(f"{exc} (элемент списка: {part!r})") from exc
        key = value.lower() if isinstance(value, str) else value
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def channel_input_text(value: Union[str, int]) -> str:
    """Как показать разобранный канал в консоли: username — с `@`, ссылки/id — как есть."""
    if isinstance(value, str) and not value.lower().startswith(("http://", "https://")):
        return f"@{value.lstrip('@')}"
    return str(value)


async def resolve_channel(client: TelegramClient, value: Union[str, int]) -> Any:
    """Возвращает сущность канала, пробуя несколько интерпретаций числового id."""
    candidates: list[Union[str, int]] = [value]

    if isinstance(value, int):
        # Положительный id обычно означает «сырой» id канала -> пробуем -100<id>.
        if value > 0:
            candidates.append(utils.get_peer_id(types.PeerChannel(value)))
        elif not str(value).startswith("-100"):
            candidates.append(utils.get_peer_id(types.PeerChannel(abs(value))))

    last_error: Optional[BaseException] = None
    for candidate in candidates:
        try:
            entity = await client.get_entity(candidate)
        except CHANNEL_ERRORS as exc:
            last_error = exc
            logger.debug("get_entity(%r) не удался: %s", candidate, exc)
            continue
        if entity is None:
            last_error = ChannelResolutionError(f"Сущность {candidate!r} не найдена.")
            continue
        if isinstance(entity, types.User):
            raise ChannelResolutionError(
                f"{value!r} — это пользователь, а не канал. Укажите канал/чат."
            )
        return entity

    raise ChannelResolutionError(f"Канал {value!r} не найден или недоступен: {last_error}")


def channel_title(entity: Any) -> str:
    """Название канала для колонки `current_channel` и имени файла."""
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return clean_cell_value(remove_invisible(str(title)))
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    return str(getattr(entity, "id", "") or "")


def build_post_link(entity: Any, message_id: int) -> str:
    """Строит прямую ссылку на сообщение публичного или приватного канала."""
    if not message_id or entity is None:
        return ""

    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"

    channel_id = getattr(entity, "id", None)
    if channel_id is not None:
        return f"https://t.me/c/{channel_id}/{message_id}"
    return ""


async def get_last_message_id(client: TelegramClient, entity: Any,
                              throttle: Optional[RequestThrottle] = None) -> Optional[int]:
    """id последнего сообщения канала (None, если получить не удалось)."""
    try:
        if throttle is not None:
            await throttle.wait()
        messages = await client.get_messages(entity, limit=1)
    except (FloodWaitError, RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось получить последнее сообщение: %s", exc)
        return None
    if messages:
        return int(getattr(messages[0], "id", 0) or 0) or None
    return None


# --------------------------------------------------------------------------- #
# Сетевые запросы: троттлинг и обработка FloodWait
# --------------------------------------------------------------------------- #
async def send_request(client: Any, request: Any,
                       throttle: Optional[RequestThrottle] = None) -> Any:
    """Один запрос к Telegram: пауза по троттлингу + одна попытка после FloodWait.

    Пауза выдерживается **перед** запросом, поэтому темп не зависит от того,
    сколько сообщений было обработано между запросами. При `FloodWaitError`
    интервал троттлинга увеличивается (адаптивная защита) и запрос повторяется.
    """
    if throttle is not None:
        await throttle.wait()
    try:
        return await client(request)
    except FloodWaitError as exc:
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
        if throttle is not None:
            throttle.penalize()
        logger.warning("FloodWait при запросе %s: ожидание %s сек.",
                       type(request).__name__, wait_seconds)
        tqdm.write(f"FloodWait: Telegram просит подождать {format_seconds(wait_seconds)}. "
                   "Продолжаем автоматически...")
        await asyncio.sleep(wait_seconds)
        if throttle is not None:
            await throttle.wait()
        return await client(request)


async def local_input_peer(client: Any, peer: Any) -> Optional[Any]:
    """`InputPeer` из локальных кэшей Telethon **без** сетевого запроса.

    `client.get_entity(peer)` для неизвестного пользователя уходит в сеть
    (`users.getUsers` с `access_hash=0`) и почти всегда возвращает ошибку.
    Здесь проверяются только кэш в памяти и кэш сессии, поэтому операция
    бесплатна: если `access_hash` неизвестен — возвращаем `None`.
    """
    if peer is None:
        return None
    try:
        return utils.get_input_peer(peer)   # уже InputPeer или полная сущность
    except (TypeError, ValueError):
        pass

    memory_cache = getattr(client, "_mb_entity_cache", None)
    if memory_cache is not None:
        try:
            entry = memory_cache.get(utils.get_peer_id(peer, add_mark=False))
        except Exception:  # noqa: BLE001 - кэш Telethon может отличаться в разных версиях
            entry = None
        if entry is not None:
            try:
                return entry._as_input_peer()
            except Exception:  # noqa: BLE001
                pass

    session = getattr(client, "session", None)
    if session is not None:
        try:
            return await utils.maybe_async(session.get_input_entity(peer))
        except Exception:  # noqa: BLE001 - ValueError: сущности нет в кэше сессии
            return None
    return None


# --------------------------------------------------------------------------- #
# Кэш сущностей
# --------------------------------------------------------------------------- #
def peer_key(peer: Any) -> tuple:
    """Ключ кэша для Peer/InputPeer."""
    if isinstance(peer, types.PeerUser):
        return ("user", peer.user_id)
    if isinstance(peer, types.PeerChannel):
        return ("channel", peer.channel_id)
    if isinstance(peer, types.PeerChat):
        return ("chat", peer.chat_id)
    if isinstance(peer, (types.InputPeerUser, types.InputPeerSelf)):
        return ("user", getattr(peer, "user_id", 0))
    if isinstance(peer, types.InputPeerChannel):
        return ("channel", peer.channel_id)
    if isinstance(peer, types.InputPeerChat):
        return ("chat", peer.chat_id)
    return ("other", str(getattr(peer, "__class__", type(peer)).__name__), str(peer))


class EntityCache:
    """Кэш сущностей: исключает повторные запросы `get_entity` и повторные ошибки.

    Умеет разрешать **много** пиров минимальным числом запросов
    (`resolve_many`): `users.getUsers` принимает до 200 сущностей за раз,
    поэтому вместо «один запрос на одного неизвестного донатора» получается
    «один запрос на 200 донаторов». Отрицательный результат (`not_found`)
    тоже кэшируется — повторных попыток по тому же пользователю не будет.
    """

    def __init__(self,
                 entity_batch: int = 0,
                 concurrency: int = 0,
                 resolve_unknown_peers: Optional[bool] = None) -> None:
        self._entities: dict[tuple, Optional[Any]] = {}
        self.entity_batch = int(entity_batch or DEFAULT_ENTITY_BATCH)
        self.concurrency = max(1, int(concurrency or DEFAULT_CONCURRENCY))
        self.resolve_unknown_peers = (
            DEFAULT_RESOLVE_UNKNOWN if resolve_unknown_peers is None
            else bool(resolve_unknown_peers)
        )
        # Счётчики для итоговой сводки.
        self.hits = 0
        self.misses = 0
        self.requests = 0
        self.not_found = 0

    def configure(self, speed: Optional[SpeedProfile]) -> None:
        """Применяет настройки скорости из `[SPEED]`."""
        if speed is None:
            return
        self.entity_batch = max(1, int(speed.entity_batch))
        self.concurrency = max(1, int(speed.concurrency))
        self.resolve_unknown_peers = bool(speed.resolve_unknown_peers)

    # -- один пир ----------------------------------------------------------- #
    async def resolve(self, client: TelegramClient, peer: Any,
                      throttle: Optional[RequestThrottle] = None) -> Optional[Any]:
        """Возвращает сущность или None (ошибка получения кэшируется)."""
        if peer is None:
            return None
        key = peer_key(peer)
        if key in self._entities:
            self.hits += 1
            return self._entities[key]

        self.misses += 1
        if throttle is not None:
            await throttle.wait()
        try:
            entity = await client.get_entity(peer)
            self.requests += 1
        except FloodWaitError as exc:
            # Ждём и пробуем ещё раз: иначе теряем данные об отправителе.
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
            if throttle is not None:
                throttle.penalize()
            logger.warning("FloodWait при получении сущности %s: ожидание %s сек.",
                           key, wait_seconds)
            tqdm.write(f"FloodWait: ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)
            try:
                if throttle is not None:
                    await throttle.wait()
                entity = await client.get_entity(peer)
                self.requests += 1
            except ENTITY_ERRORS as exc_retry:
                logger.warning("Не удалось получить сущность %s после FloodWait: %s",
                               key, exc_retry)
                entity = None
        except ENTITY_ERRORS as exc:
            self.requests += 1
            logger.debug("Не удалось получить сущность %s: %s", key, exc)
            entity = None
        except Exception as exc:  # защита: любая ошибка не должна ронять программу
            logger.exception("Непредвиденная ошибка при получении сущности %s: %s", key, exc)
            entity = None

        if entity is None:
            self.not_found += 1
        self._entities[key] = entity
        return entity

    # -- много пиров одним-двумя запросами ---------------------------------- #
    async def resolve_many(self, client: TelegramClient, peers: Iterable[Any],
                           throttle: Optional[RequestThrottle] = None
                           ) -> dict[tuple, Optional[Any]]:
        """Разрешает список пиров минимальным числом сетевых запросов.

        Пиры из кэша (в том числе `not_found`) возвращаются сразу. Остальные
        группируются в пачки по типу и запрашиваются одновременно
        (`concurrency` запросов в воздухе), каждая пачка — один запрос.
        """
        resolved: dict[tuple, Optional[Any]] = {}
        todo: list[tuple[tuple, Any]] = []
        seen: set[tuple] = set()
        for peer in peers or []:
            if peer is None:
                continue
            key = peer_key(peer)
            if key in self._entities:
                self.hits += 1
                resolved[key] = self._entities[key]
                continue
            if key in seen:
                continue
            seen.add(key)
            todo.append((key, peer))

        if not todo:
            return resolved

        if not is_batch_client(client) or self.entity_batch <= 0:
            # Клиент без поддержки «сырых» запросов: прежний последовательный путь.
            for key, peer in todo:
                resolved[key] = await self.resolve(client, peer, throttle=throttle)
            return resolved

        self.misses += len(todo)
        users_batch, channels_batch = self._batch_sizes()
        groups = await self._group_peers(client, todo)

        tasks: list[Any] = []
        # Известные (с access_hash) и неизвестные пиры запрашиваются раздельно:
        # ошибка на «пробном» hash=0 не должна портить заведомо рабочую пачку.
        for chunk in chunked(groups["users"], users_batch):
            tasks.append(self._fetch(client, functions.users.GetUsersRequest(
                [input_peer for _, input_peer in chunk]), chunk, "user", throttle))
        for chunk in chunked(groups["channels"], channels_batch):
            tasks.append(self._fetch(client, functions.channels.GetChannelsRequest(
                [input_peer for _, input_peer in chunk]), chunk, "channel", throttle))
        for chunk in chunked(groups["chats"], channels_batch):
            tasks.append(self._fetch(client, functions.messages.GetChatsRequest(
                [chat_id for _, chat_id in chunk]), chunk, "chat", throttle))
        for chunk in chunked(groups["unknown_users"], users_batch):
            tasks.append(self._fetch(client, functions.users.GetUsersRequest(
                [input_peer for _, input_peer in chunk]), chunk, "user (без кэша)", throttle))
        for chunk in chunked(groups["unknown_channels"], channels_batch):
            tasks.append(self._fetch(client, functions.channels.GetChannelsRequest(
                [input_peer for _, input_peer in chunk]), chunk, "channel (без кэша)", throttle))

        fetched: dict[tuple, Optional[Any]] = {}
        if tasks:
            results = await self._gather(tasks)
            for result in results:
                if isinstance(result, dict):
                    fetched.update(result)

        found_now = 0
        for key, _peer in todo:
            entity = fetched.get(key)
            if entity is None:
                self.not_found += 1
            else:
                found_now += 1
            self._entities[key] = entity
            resolved[key] = entity

        # Одна строка на пачку вместо предупреждения на каждого донатора:
        # подробные причины недоступны с --log-level DEBUG.
        logger.info(
            "Отправители: новых %d (запросов %d), расшифровано %d, not_found %d",
            len(todo), len(tasks), found_now, len(todo) - found_now,
        )
        return resolved

    async def _gather(self, tasks: Sequence[Any]) -> list[Any]:
        """Параллельный запуск запросов с ограничением `concurrency`."""
        if len(tasks) <= 1 or self.concurrency <= 1:
            return [await task for task in tasks]

        semaphore = asyncio.Semaphore(self.concurrency)

        async def limited(task: Any) -> Any:
            async with semaphore:
                return await task

        results = await asyncio.gather(*(limited(task) for task in tasks),
                                       return_exceptions=True)
        for result in results:
            # Прерывание пользователя и отмену задачи глотать нельзя.
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
        return list(results)

    def _batch_sizes(self) -> tuple[int, int]:
        users = max(1, min(int(self.entity_batch), MAX_ENTITY_BATCH))
        channels = max(1, min(users, MAX_CHANNEL_BATCH))
        return users, channels

    async def _group_peers(self, client: TelegramClient,
                           todo: list[tuple[tuple, Any]]) -> dict[str, list[Any]]:
        """Раскладывает пиры по типам, используя только локальные кэши Telethon."""
        groups: dict[str, list[Any]] = {"users": [], "channels": [], "chats": [],
                                        "unknown_users": [], "unknown_channels": []}
        for key, peer in todo:
            input_peer = await local_input_peer(client, peer)
            if isinstance(input_peer, types.InputPeerUser):
                groups["users"].append(
                    (key, types.InputUser(input_peer.user_id, input_peer.access_hash)))
            elif isinstance(input_peer, types.InputPeerChannel):
                groups["channels"].append(
                    (key, types.InputChannel(input_peer.channel_id, input_peer.access_hash)))
            elif isinstance(input_peer, types.InputPeerChat):
                groups["chats"].append((key, input_peer.chat_id))
            elif isinstance(peer, types.PeerChat):
                # Для обычных чатов access_hash не нужен.
                groups["chats"].append((key, peer.chat_id))
            elif isinstance(peer, types.PeerUser) and self.resolve_unknown_peers:
                # access_hash в кэше нет: пробуем пачкой с hash=0 (как Telethon,
                # но один запрос на 200 пользователей вместо 200 запросов).
                groups["unknown_users"].append((key, types.InputUser(peer.user_id, 0)))
            elif isinstance(peer, types.PeerChannel) and self.resolve_unknown_peers:
                groups["unknown_channels"].append((key, types.InputChannel(peer.channel_id, 0)))
            # Иначе: сущность недоступна, запрос к сети ничего не даст.
        return groups

    async def _fetch(self, client: TelegramClient, request: Any,
                     chunk: Sequence[tuple], kind: str,
                     throttle: Optional[RequestThrottle]) -> dict[tuple, Optional[Any]]:
        """Один пакетный запрос; возвращает {ключ кэша: сущность или None}."""
        try:
            response = await send_request(client, request, throttle)
        except FloodWaitError as exc:
            logger.warning("FloodWait при пакетном получении %s не преодолен: %s", kind, exc)
            return {}
        except ENTITY_ERRORS as exc:
            logger.warning("Не удалось получить пачку %s (%d шт.) одним запросом: %s",
                           kind, len(chunk), exc)
            return {}
        except Exception as exc:  # noqa: BLE001 - пакетный запрос не должен ронять парсинг
            logger.exception("Непредвиденная ошибка пакетного запроса %s: %s", kind, exc)
            return {}
        finally:
            self.requests += 1

        by_id: dict[int, Any] = {}
        items = getattr(response, "chats", None)
        if items is None:
            items = response if isinstance(response, (list, tuple)) else []
        for item in items or []:
            if item is None or isinstance(item, _EMPTY_ENTITIES):
                continue
            identifier = getattr(item, "id", None)
            if identifier is None:
                continue
            by_id[int(identifier)] = item

        result: dict[tuple, Optional[Any]] = {}
        for key, input_peer in chunk:
            identifier = getattr(input_peer, "user_id", None)
            if identifier is None:
                identifier = getattr(input_peer, "channel_id", None)
            if identifier is None:
                identifier = input_peer  # chat_id передаётся числом
            result[key] = by_id.get(int(identifier))
        return result

    def __len__(self) -> int:
        return len(self._entities)


def entity_display_name(entity: Any) -> str:
    """Имя для колонки `reactor_username`: username, иначе имя/название."""
    if entity is None:
        return NOT_FOUND

    username = getattr(entity, "username", None)
    if username:
        return clean_cell_value(remove_invisible(str(username)))

    if isinstance(entity, types.User):
        if getattr(entity, "deleted", False):
            return NOT_FOUND
        parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        name = " ".join(part for part in parts if part).strip()
        return clean_cell_value(remove_invisible(name)) or NOT_FOUND

    title = getattr(entity, "title", None)
    if title:
        return clean_cell_value(remove_invisible(str(title)))

    identifier = getattr(entity, "id", None)
    return str(identifier) if identifier is not None else NOT_FOUND


def _peer_fallback_id(peer: Any) -> str:
    """Текстовый id пира, если сущность получить не удалось."""
    if peer is None:
        return ""
    try:
        return str(utils.get_peer_id(peer))
    except (TypeError, ValueError):
        return str(getattr(peer, "user_id", None) or getattr(peer, "channel_id", "") or "")


# --------------------------------------------------------------------------- #
# Тип сообщения и пересылки
# --------------------------------------------------------------------------- #
def detect_message_type(message: Any) -> str:
    """Определяет тип сообщения по содержимому (см. `models.MESSAGE_TYPES`)."""
    if getattr(message, "poll", None) is not None:
        return "poll"

    media = getattr(message, "media", None)

    if isinstance(media, types.MessageMediaWebPage):
        return "webpage"
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        kinds = {type(attribute) for attribute in (getattr(document, "attributes", None) or [])}
        if types.DocumentAttributeSticker in kinds:
            return "sticker"
        if types.DocumentAttributeAnimated in kinds:
            return "gif"
        if types.DocumentAttributeVideo in kinds:
            return "video"
        if types.DocumentAttributeAudio in kinds:
            for attribute in getattr(document, "attributes", None) or []:
                if isinstance(attribute, types.DocumentAttributeAudio):
                    return "voice" if getattr(attribute, "voice", False) else "audio"
            return "audio"
        return "document"

    if media is not None:
        # Геолокация, контакты, игры, инвойсы, dice и прочее.
        return "other"

    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    return "text" if str(text).strip() else "other"


def message_type_with_forward(base_type: str, is_forward: bool) -> str:
    """Добавляет признак пересылки: `video` -> `video(forward)`."""
    return f"{base_type}{FORWARD_SUFFIX}" if is_forward else base_type


async def resolve_forward_source(client: TelegramClient, message: Any,
                                 cache: EntityCache,
                                 throttle: Optional[RequestThrottle] = None
                                 ) -> tuple[str, Optional[int]]:
    """Возвращает (канал-источник, id сообщения-источника) для пересланного поста."""
    forward = getattr(message, "fwd_from", None)
    if forward is None:
        return "", None

    source_id = getattr(forward, "channel_post", None)
    if source_id is None:
        source_id = getattr(forward, "saved_from_msg_id", None)

    peer = getattr(forward, "from_id", None) or getattr(forward, "saved_from_peer", None)
    name = clean_cell_value(remove_invisible(getattr(forward, "from_name", "") or ""))

    if peer is not None:
        entity = await cache.resolve(client, peer, throttle=throttle)
        if entity is not None:
            name = entity_display_name(entity) or name
        elif not name:
            name = _peer_fallback_id(peer)

    return name or "", source_id


# --------------------------------------------------------------------------- #
# Звёзды (Paid Reactions)
# --------------------------------------------------------------------------- #
def has_paid_reactions(reactions: Any) -> bool:
    """Проверяет, есть ли на сообщении платные реакции (звёзды)."""
    if reactions is None:
        return False
    for result in getattr(reactions, "results", None) or []:
        if isinstance(getattr(result, "reaction", None), types.ReactionPaid):
            return True
    return False


def get_paid_reactions_total_count(reactions: Any) -> int:
    """Возвращает суммарное количество звёзд из ReactionPaid в results."""
    if reactions is None:
        return 0
    total = 0
    for result in getattr(reactions, "results", None) or []:
        if isinstance(getattr(result, "reaction", None), types.ReactionPaid):
            total += int(getattr(result, "count", 0) or 0)
    return total


def iter_paid_reactors(reactions: Any) -> Iterable[types.MessageReactor]:
    """Возвращает отправителей платных реакций (top_reactors)."""
    if reactions is None:
        return []
    reactors = getattr(reactions, "top_reactors", None) or []
    return [reactor for reactor in reactors if reactor is not None]


def collect_message_peers(message: Any) -> list[Any]:
    """Пиры, которые понадобятся для обработки сообщения.

    Список совпадает с тем, что запрашивают `resolve_reactor` и
    `resolve_forward_source`, поэтому их можно получить заранее одним пакетным
    запросом (`EntityCache.resolve_many`), а само сообщение обрабатывать уже
    без обращений к сети.
    """
    peers: list[Any] = []
    for reactor in iter_paid_reactors(getattr(message, "reactions", None)):
        if getattr(reactor, "anonymous", False):
            continue
        peer = getattr(reactor, "peer_id", None)
        if peer is not None:
            peers.append(peer)

    forward = getattr(message, "fwd_from", None)
    if forward is not None:
        peer = getattr(forward, "from_id", None) or getattr(forward, "saved_from_peer", None)
        if peer is not None:
            peers.append(peer)
    return peers


async def resolve_reactor(
    client: TelegramClient, reactor: types.MessageReactor, cache: EntityCache,
    throttle: Optional[RequestThrottle] = None,
) -> tuple[str, Optional[int], str]:
    """Определяет тип, id и имя отправителя звёзд.

    Возвращает `(reactor_type, reactor_id, reactor_username)`.
    """
    peer = getattr(reactor, "peer_id", None)
    if getattr(reactor, "anonymous", False) or peer is None:
        return REACTOR_ANONYMOUS, None, ""

    if isinstance(peer, types.PeerUser):
        entity = await cache.resolve(client, peer, throttle=throttle)
        if isinstance(entity, types.User):
            if getattr(entity, "deleted", False):
                return REACTOR_USER, peer.user_id, NOT_FOUND
            return REACTOR_USER, entity.id, entity_display_name(entity)
        if entity is not None:  # на всякий случай: канал/чат
            return REACTOR_CHANNEL, getattr(entity, "id", peer.user_id), entity_display_name(entity)
        # Нет access_hash в кэше сессии — оставляем только id.
        return REACTOR_USER, peer.user_id, NOT_FOUND

    if isinstance(peer, (types.PeerChannel, types.PeerChat)):
        raw_id = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None)
        entity = await cache.resolve(client, peer, throttle=throttle)
        if entity is None:
            return REACTOR_CHANNEL, raw_id, NOT_FOUND
        identifier = getattr(entity, "id", None) or raw_id
        return REACTOR_CHANNEL, identifier, entity_display_name(entity)

    logger.warning("Неизвестный тип peer_id у реактора: %r", peer)
    return REACTOR_UNKNOWN, None, ""


async def build_records_for_message(
    client: TelegramClient,
    message: Any,
    channel_name: str,
    cache: EntityCache,
    channel_entity: Any = None,
    throttle: Optional[RequestThrottle] = None,
) -> list[StarRecord]:
    """Формирует записи по одному сообщению (пустой список, если звёзд нет).

    Сетевых запросов не делает, если все пиры уже лежат в `cache`
    (см. `EntityCache.resolve_many` — он наполняет кэш пачкой заранее).
    """
    reactions = getattr(message, "reactions", None)
    if not has_paid_reactions(reactions):
        return []

    base_type = detect_message_type(message)
    is_forward = getattr(message, "fwd_from", None) is not None
    message_type = message_type_with_forward(base_type, is_forward)
    current_id = int(getattr(message, "id", 0) or 0)
    post_link = build_post_link(channel_entity, current_id)

    original_channel, original_id = channel_name, current_id
    if is_forward:
        source_name, source_id = await resolve_forward_source(client, message, cache,
                                                              throttle=throttle)
        original_channel = source_name or channel_name
        original_id = source_id if source_id is not None else current_id

    reactors = iter_paid_reactors(reactions)
    if not reactors:
        # Платные реакции есть, но список top_reactors пуст (все реакции скрыты/анонимны)
        total_stars = get_paid_reactions_total_count(reactions)
        if total_stars > 0:
            return [
                StarRecord(
                    message_type=message_type,
                    reactor_type=REACTOR_ANONYMOUS,
                    current_channel=channel_name,
                    current_message_id=current_id,
                    post_link=post_link,
                    original_channel=original_channel,
                    original_message_id=original_id,
                    reactor_username="",
                    reactor_id=None,
                    stars_count=total_stars,
                )
            ]
        return []

    records: list[StarRecord] = []
    for reactor in reactors:
        try:
            reactor_type, reactor_id, reactor_name = await resolve_reactor(
                client, reactor, cache, throttle=throttle
            )
        except Exception as exc:  # ошибка по одному реактору не роняет программу
            logger.exception("Ошибка обработки реактора в сообщении %s: %s", current_id, exc)
            reactor_type, reactor_id, reactor_name = REACTOR_UNKNOWN, None, ""

        records.append(
            StarRecord(
                message_type=message_type,
                reactor_type=reactor_type,
                current_channel=channel_name,
                current_message_id=current_id,
                post_link=post_link,
                original_channel=original_channel,
                original_message_id=original_id,
                reactor_username=reactor_name,
                reactor_id=reactor_id,
                stars_count=int(getattr(reactor, "count", 0) or 0),
            )
        )
    return records


# --------------------------------------------------------------------------- #
# Обход сообщений: пачками, а не по одному
# --------------------------------------------------------------------------- #
async def iter_message_batches(
    client: TelegramClient,
    entity: Any,
    limit: int,
    batch_size: int = DEFAULT_MESSAGE_BATCH,
    *,
    wait_time: Optional[float] = 0.0,
    throttle: Optional[RequestThrottle] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    stats: Optional[ParseStats] = None,
    flood_callback: Optional[Callable[[datetime | None], Awaitable[None]]] = None,
    selected_years: Optional[set[int]] = None,
) -> AsyncIterator[list[Any]]:
    """Отдаёт до `limit` последних сообщений **пачками** по `batch_size`.

    Пачка — единица ускорения: все её сообщения обрабатываются вместе, поэтому
    отправители звёзд запрашиваются одним-двумя запросами на пачку, а пауза
    выдерживается только между реальными обращениями к API.

    * `wait_time` передаётся в `client.iter_messages`: `0` — не ждать между
      пачками истории (темп задаёт `throttle`), `None` — решение Telethon
      (1 с между пачками при `limit > 3000`);
    * `throttle` отмечает один запрос истории на `HISTORY_CHUNK_SIZE` (100)
      сообщений — именно столько Telegram отдаёт за один запрос;
    * при `FloodWaitError` сначала отдаются уже полученные сообщения, затем
      ожидание `seconds + 1` и продолжение с того же места (без повторов);
    * при прерывании (Ctrl+C) накопленная пачка тоже отдаётся, чтобы собранные
      данные не потерялись.
    """
    remaining = int(limit)
    offset_id = 0
    batch_size = max(1, int(batch_size))
    buffer: list[Any] = []
    since_request = 0

    # Нормализуем границы дат к UTC
    s_date = start_date
    if s_date is not None:
        if s_date.tzinfo is None:
            s_date = s_date.replace(tzinfo=timezone.utc)
        else:
            s_date = s_date.astimezone(timezone.utc)

    e_date = end_date
    if e_date is not None:
        if e_date.tzinfo is None:
            e_date = e_date.replace(tzinfo=timezone.utc)
        else:
            e_date = e_date.astimezone(timezone.utc)

    offset_date = (e_date + timedelta(seconds=1)) if e_date is not None else None

    while remaining > 0:
        if throttle is not None:
            # Первый запрос истории этой итерации (дальше — по 100 сообщений).
            await throttle.wait()
            since_request = 0
        try:
            iter_kwargs: dict[str, Any] = {
                "limit": remaining,
                "offset_id": offset_id,
                "wait_time": wait_time,
            }
            if offset_date is not None and offset_id == 0:
                iter_kwargs["offset_date"] = offset_date

            async for message in client.iter_messages(entity, **iter_kwargs):
                offset_id = int(getattr(message, "id", 0) or 0)
                m_date = getattr(message, "date", None)
                if m_date is not None:
                    if m_date.tzinfo is None:
                        m_date = m_date.replace(tzinfo=timezone.utc)
                    else:
                        m_date = m_date.astimezone(timezone.utc)

                # Если сообщение новее end_date (например, если offset_date не отсек его)
                if e_date is not None and m_date is not None and m_date > e_date:
                    continue

                # Если дата сообщения раньше нижней границы периода
                if s_date is not None and m_date is not None and m_date < s_date:
                    if buffer:
                        yield buffer
                        buffer = []
                    return

                # Если заданы конкретные выбранные годы, пропускаем сообщения вне объединения лет,
                # не останавливая сканирование (т.к. более старые годы могут быть выбраны)
                if selected_years is not None and m_date is not None and m_date.year not in selected_years:
                    continue

                remaining -= 1
                buffer.append(message)
                since_request += 1
                if throttle is not None and since_request >= HISTORY_CHUNK_SIZE:
                    since_request = 0
                    await throttle.wait()
                if len(buffer) >= batch_size:
                    yield buffer
                    buffer = []
                if remaining <= 0:
                    break
            if buffer:
                yield buffer
            return
        except FloodWaitError as exc:
            if buffer:
                yield buffer
                buffer = []
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
            if throttle is not None:
                throttle.penalize()
            logger.warning("FloodWait при получении сообщений: ожидание %s сек.", wait_seconds)
            tqdm.write(
                f"FloodWait: Telegram просит подождать {format_seconds(wait_seconds)}. "
                "Продолжаем автоматически..."
            )
            flood_until = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            if flood_callback is not None:
                try:
                    await flood_callback(flood_until)
                except Exception:
                    pass
            try:
                await asyncio.sleep(wait_seconds)
            finally:
                if flood_callback is not None:
                    try:
                        await flood_callback(None)
                    except Exception:
                        pass
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C: отдаём то, что уже получили, и пробрасываем прерывание.
            if buffer:
                yield buffer
                buffer = []
            raise
        except (RPCError, ValueError, TypeError) as exc:
            if buffer:
                yield buffer
                buffer = []
            if stats is not None and remaining > 0:
                stats.unparsed += remaining
            logger.error("Ошибка при получении сообщений: %s", exc)
            tqdm.write(f"Ошибка при получении сообщений: {exc}")
            return


async def iter_messages_safe(
    client: TelegramClient,
    entity: Any,
    limit: int,
    *,
    wait_time: Optional[float] = 0.0,
    throttle: Optional[RequestThrottle] = None,
    batch_size: int = DEFAULT_MESSAGE_BATCH,
) -> AsyncIterator[Any]:
    """Отдаёт до `limit` последних сообщений по одному (обёртка над пачками).

    При `FloodWaitError` ждёт `seconds + 1` и продолжает с того же места
    (без повторной обработки уже отданных сообщений).
    """
    async for batch in iter_message_batches(
        client, entity, limit, batch_size, wait_time=wait_time, throttle=throttle
    ):
        for message in batch:
            yield message


async def warmup_participants(client: TelegramClient, entity: Any, limit: int = 10000,
                              throttle: Optional[RequestThrottle] = None,
                              show_progress: bool = True) -> int:
    """Подгружает участников связанной группы обсуждения в кэш сессии.

    Это повышает долю найденных username (нужен access_hash в кэше).
    Возвращает количество закэшированных участников.

    Участники приходят по `PARTICIPANTS_CHUNK_SIZE` (200) штук за запрос,
    поэтому пауза выдерживается раз в 200 участников — этого достаточно, чтобы
    не получить `FloodWait` на прогреве из десятков запросов подряд.
    """
    if not limit:
        return 0
    try:
        full = await send_request(client, functions.channels.GetFullChannelRequest(entity),
                                  throttle=throttle)
    except FloodWaitError as exc:
        logger.warning("FloodWait при получении информации о канале: %s", exc)
        return 0
    except (RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось получить информацию о канале для прогрева кэша: %s", exc)
        return 0

    linked_chat_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
    if not linked_chat_id:
        logger.info("У канала нет связанной группы обсуждения — прогрев кэша пропущен.")
        return 0

    cached = 0
    since_request = 0
    if throttle is not None:
        # Первую пачку участников Telethon запрашивает сам, до нашего счётчика.
        throttle.note()
    progress = tqdm(total=int(limit), desc=WARMUP_DESCRIPTION, unit="участн.",
                    dynamic_ncols=True, leave=False) if show_progress else None
    try:
        async for _ in client.iter_participants(linked_chat_id, limit=limit):
            cached += 1
            since_request += 1
            if progress is not None:
                progress.update(1)
            if since_request >= PARTICIPANTS_CHUNK_SIZE:
                since_request = 0
                if throttle is not None:
                    await throttle.wait()
    except FloodWaitError as exc:
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
        if throttle is not None:
            throttle.penalize()
        logger.warning("FloodWait при загрузке участников: ожидание %s сек.", wait_seconds)
        if progress is not None:
            progress.write(f"FloodWait: ожидание {format_seconds(wait_seconds)}...")
        await asyncio.sleep(wait_seconds)
    except (RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось загрузить участников группы обсуждения: %s", exc)
    finally:
        if progress is not None:
            progress.close()

    logger.info("Прогрев кэша: загружено участников — %d", cached)
    return cached


# --------------------------------------------------------------------------- #
# Обработка пачки сообщений
# --------------------------------------------------------------------------- #
async def process_message_batch(
    client: TelegramClient,
    messages: Sequence[Any],
    channel_name: str,
    cache: EntityCache,
    records: list[StarRecord],
    stats: ParseStats,
    channel_entity: Any = None,
    throttle: Optional[RequestThrottle] = None,
    progress: Optional[tqdm] = None,
) -> list[StarRecord]:
    """Обрабатывает пачку сообщений и добавляет найденные записи в `records`.

    Порядок работы (в нём и заключается ускорение):

    1. сообщения без платных реакций отсеиваются **без единого запроса** —
       `reactions` уже лежит внутри сообщения;
    2. все отправители звёзд и источники пересылок всей пачки собираются в один
       список и разрешаются пакетно (`users.getUsers` до 200 сущностей за
       запрос, несколько запросов параллельно);
    3. записи строятся уже локально, из кэша, без обращений к сети.

    Возвращает записи, добавленные на этой пачке.
    """
    added: list[StarRecord] = []
    star_messages: list[Any] = []
    peers: list[Any] = []

    for message in messages:
        stats.scanned += 1
        if isinstance(message, types.MessageService):
            stats.skipped_service += 1
            continue
        if not has_paid_reactions(getattr(message, "reactions", None)):
            continue
        star_messages.append(message)
        peers.extend(collect_message_peers(message))

    if peers:
        await cache.resolve_many(client, peers, throttle=throttle)

    for message in star_messages:
        try:
            found = await build_records_for_message(
                client, message, channel_name, cache,
                channel_entity=channel_entity, throttle=throttle,
            )
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
            if throttle is not None:
                throttle.penalize()
            logger.warning("FloodWait при обработке сообщения: ожидание %s сек.", wait_seconds)
            tqdm.write(f"FloodWait: ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)
            found = []
        except Exception as exc:  # ошибка на одном сообщении не останавливает парсинг
            stats.errors += 1
            logger.exception(
                "Ошибка обработки сообщения id=%s: %s",
                getattr(message, "id", "?"), exc,
            )
            found = []

        if found:
            stats.with_stars += 1
            stats.reactors += len(found)
            stats.not_found += sum(
                1 for record in found if record.reactor_username == NOT_FOUND
            )
            records.extend(found)
            added.extend(found)

    if progress is not None:
        progress.update(len(messages))
        if stats.with_stars:
            progress.set_postfix_str(f"со звёздами: {stats.with_stars}", refresh=False)
    return added


# --------------------------------------------------------------------------- #
# Парсинг канала
# --------------------------------------------------------------------------- #
async def parse_channel(
    client: TelegramClient,
    entity: Any,
    limit: int,
    records: list[StarRecord],
    delay: tuple[float, float] = (0.05, 0.1),
    cache: Optional[EntityCache] = None,
    stats: Optional[ParseStats] = None,
    *,
    speed: Optional[SpeedProfile] = None,
    throttle: Optional[RequestThrottle] = None,
    batch_size: Optional[int] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    progress_callback: Optional[Callable[[str, int, int, Optional[int], Optional[datetime]], Awaitable[None]]] = None,
    selected_years: Optional[set[int]] = None,
) -> ParseStats:
    """Проходит по `limit` последним сообщениям канала и собирает записи о звёздах.

    Сообщения обрабатываются пачками (`speed.message_batch`): на пачку уходит
    один запрос истории и, при наличии звёзд, один-два запроса на всех
    отправителей сразу. Задержка из `[DELAY]` применяется **только между
    реальными запросами** к API, поэтому посты без звёзд и уже известные
    пользователи обрабатываются без ожидания вовсе.

    Записи добавляются в переданный список `records`, поэтому при прерывании
    (Ctrl+C) уже собранные данные можно сохранить. Если передан объект `stats`,
    он заполняется по ходу работы (удобно для частичного результата).
    """
    speed = speed if speed is not None else SpeedProfile()
    if throttle is None:
        throttle = RequestThrottle(delay, enabled=speed.per_request_delay)
    if stats is None:
        stats = ParseStats(requested=int(limit))
    else:
        stats.requested = int(limit)
    if cache is None:
        cache = EntityCache(entity_batch=speed.entity_batch,
                            concurrency=speed.concurrency,
                            resolve_unknown_peers=speed.resolve_unknown_peers)
    else:
        cache.configure(speed)

    channel_name = channel_title(entity)
    chunk = int(batch_size or speed.batch_size_for(limit))
    started = time.monotonic()
    # Один троттлинг может обслуживать очередь из нескольких каналов, поэтому
    # в статистику прохода берутся только запросы/ожидания этого прохода.
    requests_before = throttle.requests
    slept_before = throttle.slept

    current_flood_until: Optional[datetime] = None

    async def _on_flood(until: Optional[datetime]) -> None:
        nonlocal current_flood_until
        current_flood_until = until
        if progress_callback is not None:
            try:
                await progress_callback(
                    channel_name, stats.scanned, stats.with_stars,
                    stats.requested, current_flood_until
                )
            except Exception:
                pass

    last_reported_scanned = 0

    with tqdm(total=max(limit, 0), desc=PROGRESS_DESCRIPTION, unit="сообщ.",
              dynamic_ncols=True, leave=True) as progress:
        source = iter_message_batches(
            client, entity, limit, chunk,
            wait_time=speed.history_wait_time, throttle=throttle,
            start_date=start_date, end_date=end_date, stats=stats,
            flood_callback=_on_flood, selected_years=selected_years,
        )
        async with aclosing(source):
            async for messages in source:
                await process_message_batch(
                    client, messages, channel_name, cache, records, stats,
                    channel_entity=entity, throttle=throttle, progress=progress,
                )
                if progress_callback is not None and (stats.scanned - last_reported_scanned >= 20 or stats.scanned >= limit):
                    last_reported_scanned = stats.scanned
                    try:
                        await progress_callback(
                            channel_name, stats.scanned, stats.with_stars,
                            stats.requested, current_flood_until
                        )
                    except Exception:
                        pass

    if progress_callback is not None and stats.scanned != last_reported_scanned:
        try:
            await progress_callback(
                channel_name, stats.scanned, stats.with_stars,
                stats.requested, current_flood_until
            )
        except Exception:
            pass

    stats.elapsed = time.monotonic() - started
    stats.api_requests = max(0, throttle.requests - requests_before)
    stats.waited = max(0.0, throttle.slept - slept_before)

    rate = stats.scanned / stats.elapsed if stats.elapsed else 0.0
    logger.info(
        "Парсинг завершён: просмотрено %d, со звёздами %d, записей %d, ошибок %d, %s "
        "(%.1f сообщ./с, пауз на %.2f с)",
        stats.scanned, stats.with_stars, stats.reactors, stats.errors,
        throttle.summary(), rate, stats.waited,
    )
    return stats


__all__ = [
    "ChannelResolutionError", "EntityCache", "PROGRESS_DESCRIPTION",
    "WARMUP_DESCRIPTION", "build_post_link", "build_records_for_message",
    "channel_input_text", "channel_title", "collect_message_peers",
    "detect_message_type", "get_last_message_id",
    "get_paid_reactions_total_count", "has_paid_reactions",
    "iter_message_batches", "iter_messages_safe", "iter_paid_reactors",
    "local_input_peer", "message_type_with_forward", "parse_channel",
    "parse_channel_input", "parse_channels_input", "process_message_batch",
    "resolve_channel", "resolve_forward_source", "resolve_reactor",
    "send_request", "split_channel_list", "warmup_participants",
]
