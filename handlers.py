"""Парсинг сообщений канала и определение отправителей звёзд (Paid Reactions)."""

from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator, Iterable, Optional, Union

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
from utils import clean_cell_value, format_seconds, get_logger, random_delay, remove_invisible

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

PROGRESS_DESCRIPTION = "Парсинг сообщений"

_INVITE_RE = re.compile(r"^[+A-Za-z0-9_-]{10,}$")
_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(.+)$", re.IGNORECASE
)


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


async def get_last_message_id(client: TelegramClient, entity: Any) -> Optional[int]:
    """id последнего сообщения канала (None, если получить не удалось)."""
    try:
        messages = await client.get_messages(entity, limit=1)
    except (FloodWaitError, RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось получить последнее сообщение: %s", exc)
        return None
    if messages:
        return int(getattr(messages[0], "id", 0) or 0) or None
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
    """Кэш сущностей: исключает повторные запросы `get_entity` и повторные ошибки."""

    def __init__(self) -> None:
        self._entities: dict[tuple, Optional[Any]] = {}

    async def resolve(self, client: TelegramClient, peer: Any) -> Optional[Any]:
        """Возвращает сущность или None (ошибка получения кэшируется)."""
        if peer is None:
            return None
        key = peer_key(peer)
        if key in self._entities:
            return self._entities[key]

        try:
            entity = await client.get_entity(peer)
        except FloodWaitError as exc:
            # Ждём и пробуем ещё раз: иначе теряем данные об отправителе.
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
            logger.warning("FloodWait при получении сущности %s: ожидание %s сек.",
                           key, wait_seconds)
            tqdm.write(f"FloodWait: ожидание {format_seconds(wait_seconds)}...")
            await asyncio.sleep(wait_seconds)
            try:
                entity = await client.get_entity(peer)
            except ENTITY_ERRORS as exc_retry:
                logger.warning("Не удалось получить сущность %s после FloodWait: %s",
                               key, exc_retry)
                entity = None
        except ENTITY_ERRORS as exc:
            logger.warning("Не удалось получить сущность %s: %s", key, exc)
            entity = None
        except Exception as exc:  # защита: любая ошибка не должна ронять программу
            logger.exception("Непредвиденная ошибка при получении сущности %s: %s", key, exc)
            entity = None
        self._entities[key] = entity
        return entity

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
                                 cache: EntityCache) -> tuple[str, Optional[int]]:
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
        entity = await cache.resolve(client, peer)
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


async def resolve_reactor(
    client: TelegramClient, reactor: types.MessageReactor, cache: EntityCache
) -> tuple[str, Optional[int], str]:
    """Определяет тип, id и имя отправителя звёзд.

    Возвращает `(reactor_type, reactor_id, reactor_username)`.
    """
    peer = getattr(reactor, "peer_id", None)
    if getattr(reactor, "anonymous", False) or peer is None:
        return REACTOR_ANONYMOUS, None, ""

    if isinstance(peer, types.PeerUser):
        entity = await cache.resolve(client, peer)
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
        entity = await cache.resolve(client, peer)
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
) -> list[StarRecord]:
    """Формирует записи по одному сообщению (пустой список, если звёзд нет)."""
    reactions = getattr(message, "reactions", None)
    if not has_paid_reactions(reactions):
        return []

    base_type = detect_message_type(message)
    is_forward = getattr(message, "fwd_from", None) is not None
    message_type = message_type_with_forward(base_type, is_forward)
    current_id = int(getattr(message, "id", 0) or 0)

    original_channel, original_id = channel_name, current_id
    if is_forward:
        source_name, source_id = await resolve_forward_source(client, message, cache)
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
            reactor_type, reactor_id, reactor_name = await resolve_reactor(client, reactor, cache)
        except Exception as exc:  # ошибка по одному реактору не роняет программу
            logger.exception("Ошибка обработки реактора в сообщении %s: %s", current_id, exc)
            reactor_type, reactor_id, reactor_name = REACTOR_UNKNOWN, None, ""

        records.append(
            StarRecord(
                message_type=message_type,
                reactor_type=reactor_type,
                current_channel=channel_name,
                current_message_id=current_id,
                original_channel=original_channel,
                original_message_id=original_id,
                reactor_username=reactor_name,
                reactor_id=reactor_id,
                stars_count=int(getattr(reactor, "count", 0) or 0),
            )
        )
    return records


# --------------------------------------------------------------------------- #
# Обход сообщений
# --------------------------------------------------------------------------- #
async def iter_messages_safe(
    client: TelegramClient, entity: Any, limit: int
) -> AsyncIterator[Any]:
    """Отдаёт до `limit` последних сообщений, обрабатывая `FloodWaitError`.

    При `FloodWaitError` ждёт `seconds + 1` и продолжает с того же места
    (без повторной обработки уже отданных сообщений).
    """
    remaining = int(limit)
    offset_id = 0

    while remaining > 0:
        try:
            async for message in client.iter_messages(entity, limit=remaining, offset_id=offset_id):
                offset_id = int(getattr(message, "id", 0) or 0)
                remaining -= 1
                yield message
                if remaining <= 0:
                    break
            return
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
            logger.warning("FloodWait при получении сообщений: ожидание %s сек.", wait_seconds)
            tqdm.write(
                f"FloodWait: Telegram просит подождать {format_seconds(wait_seconds)}. "
                "Продолжаем автоматически..."
            )
            await asyncio.sleep(wait_seconds)
        except (RPCError, ValueError, TypeError) as exc:
            logger.error("Ошибка при получении сообщений: %s", exc)
            tqdm.write(f"Ошибка при получении сообщений: {exc}")
            return


async def warmup_participants(client: TelegramClient, entity: Any,
                              limit: int = 10000) -> int:
    """Подгружает участников связанной группы обсуждения в кэш сессии.

    Это повышает долю найденных username (нужен access_hash в кэше).
    Возвращает количество закэшированных участников.
    """
    if not limit:
        return 0
    try:
        full = await client(functions.channels.GetFullChannelRequest(entity))
    except (FloodWaitError, RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось получить информацию о канале для прогрева кэша: %s", exc)
        return 0

    linked_chat_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
    if not linked_chat_id:
        logger.info("У канала нет связанной группы обсуждения — прогрев кэша пропущен.")
        return 0

    cached = 0
    try:
        async for _ in client.iter_participants(linked_chat_id, limit=limit):
            cached += 1
    except FloodWaitError as exc:
        wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
        logger.warning("FloodWait при загрузке участников: ожидание %s сек.", wait_seconds)
    except (RPCError, ValueError, TypeError) as exc:
        logger.warning("Не удалось загрузить участников группы обсуждения: %s", exc)

    logger.info("Прогрев кэша: загружено участников — %d", cached)
    return cached


async def parse_channel(
    client: TelegramClient,
    entity: Any,
    limit: int,
    records: list[StarRecord],
    delay: tuple[float, float] = (0.05, 0.1),
    cache: Optional[EntityCache] = None,
    stats: Optional[ParseStats] = None,
) -> ParseStats:
    """Проходит по `limit` последним сообщениям канала и собирает записи о звёздах.

    Записи добавляются в переданный список `records`, поэтому при прерывании
    (Ctrl+C) уже собранные данные можно сохранить. Если передан объект `stats`,
    он заполняется по ходу работы (удобно для частичного результата).
    """
    if stats is None:
        stats = ParseStats(requested=int(limit))
    else:
        stats.requested = int(limit)
    channel_name = channel_title(entity)
    cache = cache if cache is not None else EntityCache()
    delay_min, delay_max = delay

    with tqdm(total=max(limit, 0), desc=PROGRESS_DESCRIPTION, unit="сообщ.",
              dynamic_ncols=True, leave=True) as progress:
        async for message in iter_messages_safe(client, entity, limit):
            stats.scanned += 1
            progress.update(1)

            if isinstance(message, types.MessageService):
                stats.skipped_service += 1
                await random_delay(delay_min, delay_max)
                continue

            try:
                found = await build_records_for_message(client, message, channel_name, cache)
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                wait_seconds = int(getattr(exc, "seconds", 0) or 0) + 1
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
                records.extend(found)
                progress.set_postfix_str(f"со звёздами: {stats.with_stars}", refresh=False)

            await random_delay(delay_min, delay_max)

    logger.info(
        "Парсинг завершён: просмотрено %d, со звёздами %d, записей %d, ошибок %d",
        stats.scanned, stats.with_stars, stats.reactors, stats.errors,
    )
    return stats


__all__ = [
    "ChannelResolutionError", "EntityCache", "PROGRESS_DESCRIPTION",
    "build_records_for_message", "channel_title", "detect_message_type",
    "get_last_message_id", "get_paid_reactions_total_count",
    "has_paid_reactions", "iter_messages_safe", "iter_paid_reactors",
    "message_type_with_forward", "parse_channel", "parse_channel_input",
    "resolve_channel", "resolve_forward_source", "resolve_reactor",
    "warmup_participants",
]
