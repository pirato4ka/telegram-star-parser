"""Тесты ускорения: троттлинг запросов, пакетное разрешение сущностей, пачки истории.

Запуск (без Telegram и без сети):
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from telethon import functions  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402
from telethon.errors.rpcerrorlist import UserIdInvalidError  # noqa: E402
from telethon.tl import types  # noqa: E402

from config import ConfigError, load_config  # noqa: E402
from handlers import (  # noqa: E402
    EntityCache,
    collect_message_peers,
    iter_message_batches,
    local_input_peer,
    parse_channel,
    warmup_participants,
)
from models import NOT_FOUND, REACTOR_ANONYMOUS, REACTOR_USER, ParseStats, StarRecord  # noqa: E402
from speed import RequestThrottle, SpeedProfile  # noqa: E402
from test_offline import (  # noqa: E402
    FakeClient, make_channel, make_message, make_paid_reactions, make_user,
)


# --------------------------------------------------------------------------- #
# Заглушки: клиент с «сырыми» запросами и кэшем сессии (как настоящий Telethon)
# --------------------------------------------------------------------------- #
class FakeSession:
    """Мини-замена `client.session`: отдаёт InputPeer только для известных сущностей."""

    def __init__(self, entities: dict[tuple, Any]):
        self._entities = entities

    def get_input_entity(self, peer: Any) -> Any:
        if isinstance(peer, types.PeerUser):
            key = ("user", peer.user_id)
            entity = self._entities.get(key)
            if entity is None:
                raise ValueError(f"Could not find the input entity for {peer}")
            return types.InputPeerUser(peer.user_id, getattr(entity, "access_hash", 1))
        if isinstance(peer, types.PeerChannel):
            key = ("channel", peer.channel_id)
            entity = self._entities.get(key)
            if entity is None:
                raise ValueError(f"Could not find the input entity for {peer}")
            return types.InputPeerChannel(peer.channel_id, getattr(entity, "access_hash", 1))
        if isinstance(peer, types.PeerChat):
            return types.InputPeerChat(peer.chat_id)
        raise ValueError(f"Could not find the input entity for {peer}")


class RawCallClient(FakeClient):
    """Клиент, который умеет `await client(request)` — как настоящий TelegramClient.

    Считает число «сырых» запросов, чтобы можно было проверить экономию.
    """

    def __init__(self, messages=None, entities=None, missing=(), flood_once=False,
                 raw_error: Optional[Exception] = None):
        super().__init__(messages=messages, entities=entities, missing=missing,
                         flood_once=flood_once)
        self.session = FakeSession(self.entities)
        self.raw_calls = 0
        self.raw_error = raw_error
        self.users_per_call: list[int] = []

    async def __call__(self, request: Any, **kwargs: Any) -> Any:
        self.raw_calls += 1
        if self.raw_error is not None:
            error, self.raw_error = self.raw_error, None
            raise error
        if self.flood_once:
            self.flood_once = False
            raise FloodWaitError(request=request, capture=0)

        if isinstance(request, functions.users.GetUsersRequest):
            self.users_per_call.append(len(request.id))
            users = []
            for input_user in request.id:
                entity = self.entities.get(("user", input_user.user_id))
                users.append(entity if entity is not None else types.UserEmpty(
                    id=input_user.user_id))
            return users
        if isinstance(request, functions.channels.GetChannelsRequest):
            chats = []
            for input_channel in request.id:
                entity = self.entities.get(("channel", input_channel.channel_id))
                if entity is None:
                    raise UserIdInvalidError(request=request)
                chats.append(entity)
            return types.messages.Chats(chats=chats)
        if isinstance(request, functions.messages.GetChatsRequest):
            return types.messages.Chats(chats=[])
        if isinstance(request, functions.channels.GetFullChannelRequest):
            # Достаточно linked_chat_id — только его и использует warmup_participants.
            return SimpleNamespace(full_chat=SimpleNamespace(linked_chat_id=777))
        raise TypeError(f"Не поддержан запрос {type(request).__name__}")


def reactor(user_id: Optional[int] = None, count: int = 1, anonymous: bool = False):
    peer = types.PeerUser(user_id) if user_id is not None else None
    return types.MessageReactor(count=count, peer_id=peer, anonymous=anonymous)


# --------------------------------------------------------------------------- #
# Троттлинг: пауза между запросами, а не между сообщениями
# --------------------------------------------------------------------------- #
class TestRequestThrottle(unittest.TestCase):
    def test_zero_delay_disables_sleeping(self):
        throttle = RequestThrottle((0, 0))
        self.assertFalse(throttle.enabled)

        async def scenario():
            started = time.monotonic()
            for _ in range(1000):
                await throttle.wait()
            return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        self.assertEqual(throttle.requests, 1000)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(throttle.slept, 0.0)

    def test_wait_paces_requests(self):
        throttle = RequestThrottle((0.05, 0.05))

        async def scenario():
            started = time.monotonic()
            for _ in range(3):
                await throttle.wait()
            return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        # Первый запрос — без ожидания, два следующих — по 0.05 с.
        self.assertGreaterEqual(elapsed, 0.09)
        self.assertLess(elapsed, 0.5)

    def test_no_sleep_when_work_is_slow_enough(self):
        """Если обработка заняла больше интервала — поток не спит вовсе."""
        throttle = RequestThrottle((0.05, 0.05))

        async def scenario():
            await throttle.wait()
            await asyncio.sleep(0.2)      # «медленная» обработка пачки
            return await throttle.wait()  # ждать уже нечего

        self.assertEqual(asyncio.run(scenario()), 0.0)
        self.assertEqual(throttle.slept, 0.0)

    def test_penalize_increases_interval(self):
        throttle = RequestThrottle((0.1, 0.2))
        low, high = throttle.penalize()
        self.assertAlmostEqual(low, 0.2)
        self.assertAlmostEqual(high, 0.4)
        self.assertEqual(throttle.penalties, 1)
        # Потолок не превышается.
        for _ in range(10):
            throttle.penalize()
        self.assertLessEqual(throttle.delay[1], 5.0)

    def test_penalize_enables_pacing_after_zero_delay(self):
        throttle = RequestThrottle((0, 0))
        self.assertFalse(throttle.enabled)
        throttle.penalize()
        self.assertTrue(throttle.enabled)
        self.assertGreater(throttle.delay[1], 0.0)

    def test_summary_mentions_requests(self):
        throttle = RequestThrottle((0, 0))
        asyncio.run(throttle.wait(3))
        self.assertIn("3", throttle.summary())


# --------------------------------------------------------------------------- #
# Профиль скорости
# --------------------------------------------------------------------------- #
class TestSpeedProfile(unittest.TestCase):
    def test_batch_size_limited_by_profile(self):
        self.assertEqual(SpeedProfile(message_batch=200).batch_size_for(10000), 200)

    def test_batch_size_small_for_short_runs(self):
        # Короткий прогон обрабатывается одной пачкой: минимум запросов.
        self.assertEqual(SpeedProfile(message_batch=200).batch_size_for(30), 30)
        self.assertEqual(SpeedProfile(message_batch=200).batch_size_for(3), 3)
        self.assertEqual(SpeedProfile(message_batch=50).batch_size_for(10000), 50)

    def test_entity_batch_sizes_capped(self):
        users, channels = SpeedProfile(entity_batch=1000).entity_batch_sizes()
        self.assertEqual(users, 200)      # лимит users.getUsers
        self.assertLessEqual(channels, 100)

    def test_throttle_follows_per_request_delay(self):
        self.assertTrue(SpeedProfile().throttle((0.05, 0.1)).enabled)
        self.assertFalse(SpeedProfile(per_request_delay=False).throttle((0.05, 0.1)).enabled)


# --------------------------------------------------------------------------- #
# Локальный InputPeer без сетевого запроса
# --------------------------------------------------------------------------- #
class TestLocalInputPeer(unittest.TestCase):
    def test_known_user_from_session_cache(self):
        client = RawCallClient(entities={("user", 42): make_user(42, "ivan")})
        input_peer = asyncio.run(local_input_peer(client, types.PeerUser(42)))
        self.assertIsInstance(input_peer, types.InputPeerUser)
        self.assertEqual(client.raw_calls, 0)      # сеть не трогали

    def test_unknown_user_returns_none(self):
        client = RawCallClient()
        self.assertIsNone(asyncio.run(local_input_peer(client, types.PeerUser(999))))
        self.assertEqual(client.raw_calls, 0)

    def test_input_peer_passthrough(self):
        client = RawCallClient()
        peer = types.InputPeerUser(1, 2)
        self.assertIs(asyncio.run(local_input_peer(client, peer)), peer)


# --------------------------------------------------------------------------- #
# Пакетное разрешение сущностей
# --------------------------------------------------------------------------- #
class TestResolveMany(unittest.TestCase):
    def setUp(self):
        self.cache = EntityCache()

    def _entities(self, count):
        return {("user", index): make_user(index, f"user{index}") for index in range(count)}

    def test_one_request_per_200_users(self):
        entities = self._entities(250)
        client = RawCallClient(entities=entities)
        peers = [types.PeerUser(index) for index in range(250)]

        resolved = asyncio.run(self.cache.resolve_many(client, peers))

        self.assertEqual(len(resolved), 250)
        self.assertEqual(client.raw_calls, 2)            # 200 + 50 одним запросом каждая
        self.assertEqual(client.users_per_call, [200, 50])
        self.assertEqual(client.entity_calls, 0)         # get_entity не использовался
        self.assertEqual(resolved[("user", 7)].username, "user7")

    def test_duplicates_do_not_add_requests(self):
        client = RawCallClient(entities=self._entities(5))
        peers = [types.PeerUser(index % 5) for index in range(100)]

        asyncio.run(self.cache.resolve_many(client, peers))
        first_calls = client.raw_calls
        asyncio.run(self.cache.resolve_many(client, peers))   # всё из кэша

        self.assertEqual(first_calls, 1)
        self.assertEqual(client.raw_calls, 1)
        self.assertEqual(self.cache.misses, 5)
        self.assertGreaterEqual(self.cache.hits, 100)

    def test_missing_users_are_cached_as_not_found(self):
        client = RawCallClient(entities={("user", 1): make_user(1, "one")})
        peers = [types.PeerUser(1), types.PeerUser(2), types.PeerUser(2)]

        resolved = asyncio.run(self.cache.resolve_many(client, peers))
        calls_after_first = client.raw_calls
        resolved_again = asyncio.run(self.cache.resolve_many(client, peers))

        self.assertEqual(resolved[("user", 1)].username, "one")
        self.assertIsNone(resolved[("user", 2)])
        self.assertEqual(client.raw_calls, calls_after_first)   # повторных запросов нет
        self.assertIsNone(resolved_again[("user", 2)])

    def test_unknown_peers_without_hash_are_batched(self):
        """Пользователей без access_hash пробуем пачкой (hash=0), а не по одному."""
        client = RawCallClient()
        peers = [types.PeerUser(index) for index in range(500)]

        resolved = asyncio.run(self.cache.resolve_many(client, peers))

        self.assertEqual(len(resolved), 500)
        self.assertTrue(all(value is None for value in resolved.values()))
        self.assertEqual(client.raw_calls, 3)      # 200 + 200 + 100
        self.assertEqual(client.entity_calls, 0)

    def test_unknown_peers_can_be_skipped(self):
        cache = EntityCache(resolve_unknown_peers=False)
        client = RawCallClient()
        peers = [types.PeerUser(index) for index in range(500)]

        resolved = asyncio.run(cache.resolve_many(client, peers))

        self.assertEqual(client.raw_calls, 0)      # ни одного запроса к сети
        self.assertTrue(all(value is None for value in resolved.values()))
        self.assertEqual(cache.not_found, 500)

    def test_channel_peers_resolved_too(self):
        client = RawCallClient(entities={("channel", 555): make_channel(555, "News", "news")})
        peers = [types.PeerChannel(555), types.PeerUser(1)]

        resolved = asyncio.run(self.cache.resolve_many(client, peers))

        self.assertEqual(resolved[("channel", 555)].username, "news")
        self.assertIsNone(resolved[("user", 1)])
        self.assertEqual(client.raw_calls, 2)      # один запрос на каналы, один на пользователей

    def test_fallback_for_clients_without_raw_requests(self):
        """Клиент без `__call__` (заглушка) обрабатывается прежним путём."""
        client = FakeClient(entities={("user", 42): make_user(42, "ivan")})
        cache = EntityCache()

        resolved = asyncio.run(cache.resolve_many(client, [types.PeerUser(42)]))

        self.assertEqual(resolved[("user", 42)].username, "ivan")
        self.assertEqual(client.entity_calls, 1)

    def test_request_error_marks_peers_not_found(self):
        client = RawCallClient(entities=self._entities(3),
                               raw_error=UserIdInvalidError(request=None))
        peers = [types.PeerUser(index) for index in range(3)]

        resolved = asyncio.run(self.cache.resolve_many(client, peers))

        self.assertTrue(all(value is None for value in resolved.values()))
        self.assertEqual(client.raw_calls, 1)

    def test_flood_wait_retries_and_penalizes(self):
        client = RawCallClient(entities=self._entities(3), flood_once=True)
        throttle = RequestThrottle((0.01, 0.01))
        peers = [types.PeerUser(index) for index in range(3)]

        started = time.monotonic()
        resolved = asyncio.run(self.cache.resolve_many(client, peers, throttle=throttle))
        elapsed = time.monotonic() - started

        self.assertEqual(resolved[("user", 0)].username, "user0")
        self.assertEqual(throttle.penalties, 1)          # интервал увеличен после FloodWait
        self.assertGreater(elapsed, 0.9)                 # выждали FloodWait (1 сек)

    def test_concurrency_limits_parallel_requests(self):
        cache = EntityCache(concurrency=2)
        client = RawCallClient(entities=self._entities(1000))
        peers = [types.PeerUser(index) for index in range(1000)]

        resolved = asyncio.run(cache.resolve_many(client, peers))

        self.assertEqual(len(resolved), 1000)
        self.assertEqual(client.raw_calls, 5)            # 1000 / 200
        self.assertEqual(client.users_per_call, [200] * 5)


# --------------------------------------------------------------------------- #
# Пачки истории
# --------------------------------------------------------------------------- #
class TestIterMessageBatches(unittest.TestCase):
    def _messages(self, count, with_stars_every=0):
        messages = []
        for index in range(count):
            reactions = None
            if with_stars_every and index % with_stars_every == 0:
                reactions = make_paid_reactions([reactor(user_id=index + 1, count=2)])
            messages.append(make_message(msg_id=count - index, text=f"post {index}",
                                         reactions=reactions))
        return messages

    def _collect(self, client, limit, batch_size, throttle=None, wait_time=0.0):
        async def scenario():
            batches = []
            async for batch in iter_message_batches(client, types.PeerChannel(1), limit,
                                                    batch_size, wait_time=wait_time,
                                                    throttle=throttle):
                batches.append(batch)
            return batches

        return asyncio.run(scenario())

    def test_batches_are_full_except_last(self):
        client = FakeClient(messages=self._messages(250))
        batches = self._collect(client, 250, 100)
        self.assertEqual([len(batch) for batch in batches], [100, 100, 50])

    def test_limit_respected(self):
        client = FakeClient(messages=self._messages(250))
        batches = self._collect(client, 30, 100)
        self.assertEqual(sum(len(batch) for batch in batches), 30)

    def test_wait_time_is_passed_to_telethon(self):
        seen = {}

        class SpyClient(FakeClient):
            async def iter_messages(self, entity, limit=None, offset_id=0, **kwargs):
                seen.update(kwargs)
                async for message in super().iter_messages(entity, limit=limit,
                                                           offset_id=offset_id):
                    yield message

        client = SpyClient(messages=self._messages(5))
        self._collect(client, 5, 100, wait_time=0)
        self.assertEqual(seen.get("wait_time"), 0)

    def test_throttle_counts_history_requests(self):
        client = FakeClient(messages=self._messages(350))
        throttle = RequestThrottle((0, 0))
        self._collect(client, 350, 1000, throttle=throttle)
        # 4 запроса истории: первый + по одному на каждые 100 сообщений.
        self.assertEqual(throttle.requests, 4)

    def test_partial_batch_is_flushed_on_interrupt(self):
        class InterruptingClient(FakeClient):
            async def iter_messages(self, entity, limit=None, offset_id=0, **kwargs):
                for index, message in enumerate(self.messages):
                    yield message
                    if index == 1:
                        raise KeyboardInterrupt

        client = InterruptingClient(messages=self._messages(10))
        batches = []

        async def scenario():
            source = iter_message_batches(client, types.PeerChannel(1), 10, 100)
            try:
                async for batch in source:
                    batches.append(batch)
            except KeyboardInterrupt:
                return "interrupted"
            return "finished"

        self.assertEqual(asyncio.run(scenario()), "interrupted")
        self.assertEqual(sum(len(batch) for batch in batches), 2)   # данные не потеряны

    def test_flood_wait_resumes_without_duplicates(self):
        client = FakeClient(messages=self._messages(5), flood_once=True)
        batches = self._collect(client, 5, 100)
        ids = [message.id for batch in batches for message in batch]
        self.assertEqual(client.iter_calls, 2)
        self.assertEqual(len(ids), len(set(ids)))      # без повторов
        self.assertEqual(len(ids), 5)


# --------------------------------------------------------------------------- #
# Парсинг канала: ускорение
# --------------------------------------------------------------------------- #
class TestParseChannelSpeed(unittest.TestCase):
    def _star_messages(self, count, unique_users=True):
        messages = []
        for index in range(count):
            user_id = (index + 1) if unique_users else 7
            reactions = make_paid_reactions([
                reactor(user_id=user_id, count=2),
                reactor(anonymous=True, count=1),
            ])
            messages.append(make_message(msg_id=count - index, text=f"post {index}",
                                         reactions=reactions))
        return messages

    def test_no_delay_for_posts_without_stars(self):
        """Посты без звёзд не требуют запросов — задержка не накапливается."""
        messages = [make_message(msg_id=1000 - index, text="no stars")
                    for index in range(300)]
        client = FakeClient(messages=messages)
        records: list[StarRecord] = []

        started = time.monotonic()
        stats = asyncio.run(parse_channel(
            client, types.PeerChannel(1), 300, records,
            delay=(0.3, 0.3),                 # по-старому: 300 × 0.3 = 90 секунд
            speed=SpeedProfile(message_batch=100),
        ))
        elapsed = time.monotonic() - started

        self.assertEqual(stats.scanned, 300)
        self.assertEqual(records, [])
        self.assertLess(elapsed, 3.0)         # 3 запроса истории × 0.3 с ≈ 0.9 с
        self.assertLess(stats.waited, 3.0)

    def test_entities_resolved_in_batches(self):
        messages = self._star_messages(50)
        entities = {("user", index): make_user(index, f"user{index}")
                    for index in range(1, 51)}
        client = RawCallClient(messages=messages, entities=entities)
        records: list[StarRecord] = []

        stats = asyncio.run(parse_channel(
            client, types.PeerChannel(1), 50, records, delay=(0, 0),
            speed=SpeedProfile(message_batch=50),
        ))

        self.assertEqual(stats.scanned, 50)
        self.assertEqual(stats.with_stars, 50)
        self.assertEqual(len(records), 100)                 # 2 отправителя на пост
        self.assertEqual(stats.reactors, 100)
        self.assertEqual(client.entity_calls, 0)            # без поштучных get_entity
        self.assertEqual(client.raw_calls, 1)               # 50 пользователей за один запрос
        self.assertEqual([r.reactor_username for r in records if r.reactor_type == REACTOR_USER],
                         [f"user{index}" for index in range(1, 51)])
        self.assertEqual(sum(1 for r in records if r.reactor_type == REACTOR_ANONYMOUS), 50)

    def test_service_messages_and_errors_counted(self):
        messages = [
            self._star_messages(1)[0],
            types.MessageService(id=2, peer_id=types.PeerChannel(1),
                                 date=datetime(2024, 1, 1)),
            make_message(msg_id=1, text="plain"),
        ]
        client = RawCallClient(messages=messages, entities={("user", 1): make_user(1, "one")})
        records: list[StarRecord] = []

        stats = asyncio.run(parse_channel(client, types.PeerChannel(1), 10, records,
                                          delay=(0, 0)))

        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.skipped_service, 1)
        self.assertEqual(stats.with_stars, 1)
        self.assertEqual(stats.errors, 0)
        # В счётчик попадают и запросы истории, и пакетные запросы сущностей.
        self.assertEqual(stats.api_requests, client.raw_calls + client.iter_calls)

    def test_not_found_counter(self):
        messages = self._star_messages(3)
        client = RawCallClient(messages=messages)       # ни одной сущности в кэше
        records: list[StarRecord] = []

        stats = asyncio.run(parse_channel(client, types.PeerChannel(1), 3, records,
                                          delay=(0, 0)))

        self.assertEqual(stats.not_found, 3)
        self.assertEqual([r.reactor_username for r in records
                          if r.reactor_type == REACTOR_USER], [NOT_FOUND] * 3)
        self.assertEqual([r.reactor_id for r in records
                          if r.reactor_type == REACTOR_USER], [1, 2, 3])

    def test_forward_source_resolved_in_same_batch(self):
        forward = types.MessageFwdHeader(date=datetime(2024, 1, 1),
                                        from_id=types.PeerChannel(555), channel_post=777)
        message = make_message(
            msg_id=200, text="repost", fwd_from=forward,
            reactions=make_paid_reactions([reactor(user_id=42, count=5)]),
        )
        client = RawCallClient(
            messages=[message],
            entities={("user", 42): make_user(42, "ivan"),
                      ("channel", 555): make_channel(555, "Source", "src")},
        )
        records: list[StarRecord] = []

        asyncio.run(parse_channel(client, types.PeerChannel(1), 1, records, delay=(0, 0)))

        self.assertEqual(records[0].original_channel, "src")
        self.assertEqual(records[0].original_message_id, 777)
        self.assertEqual(records[0].reactor_username, "ivan")
        self.assertEqual(client.raw_calls, 2)          # пользователи + каналы

    def test_elapsed_and_rate_are_reported(self):
        client = FakeClient(messages=[make_message(msg_id=1, text="hi")])
        stats = asyncio.run(parse_channel(client, types.PeerChannel(1), 1, [], delay=(0, 0)))
        self.assertGreater(stats.elapsed, 0.0)
        self.assertGreater(stats.rate, 0.0)
        self.assertIn("Время парсинга", stats.as_text())


# --------------------------------------------------------------------------- #
# Сбор пиров сообщения
# --------------------------------------------------------------------------- #
class TestCollectMessagePeers(unittest.TestCase):
    def test_reactors_and_forward(self):
        forward = types.MessageFwdHeader(date=datetime(2024, 1, 1),
                                        from_id=types.PeerChannel(9))
        message = make_message(
            msg_id=1, text="x", fwd_from=forward,
            reactions=make_paid_reactions([reactor(user_id=1), reactor(anonymous=True),
                                           reactor(user_id=2)]),
        )
        peers = collect_message_peers(message)
        self.assertEqual([getattr(p, "user_id", None) or getattr(p, "channel_id", None)
                          for p in peers], [1, 2, 9])

    def test_message_without_stars(self):
        self.assertEqual(collect_message_peers(make_message(msg_id=1, text="x")), [])


# --------------------------------------------------------------------------- #
# Прогрев кэша участников
# --------------------------------------------------------------------------- #
class TestWarmup(unittest.TestCase):
    def test_warmup_paces_requests(self):
        class ParticipantsClient(RawCallClient):
            def __init__(self, total=450, **kwargs):
                super().__init__(**kwargs)
                self.total = total
                self.participants_seen = 0

            async def iter_participants(self, entity, limit=None, **kwargs):
                for index in range(min(self.total, limit or self.total)):
                    self.participants_seen += 1
                    yield make_user(index + 1, f"user{index}")

        client = ParticipantsClient(total=450, entities={})
        throttle = RequestThrottle((0, 0))

        cached = asyncio.run(warmup_participants(client, make_channel(1), 450,
                                                throttle=throttle, show_progress=False))

        self.assertEqual(cached, 450)
        # GetFullChannel + 3 пачки участников по 200 штук.
        self.assertEqual(throttle.requests, 4)

    def test_warmup_without_limit(self):
        client = RawCallClient()
        self.assertEqual(asyncio.run(warmup_participants(client, make_channel(1), 0)), 0)
        self.assertEqual(client.raw_calls, 0)


# --------------------------------------------------------------------------- #
# Конфигурация [SPEED]
# --------------------------------------------------------------------------- #
SPEED_CONFIG = """
[API]
ID_API=1
HASH_API=abc
PHONE=+79990000000

[DELAY]
MESSAGES_INTERVAL_MIN=0.05
MESSAGES_INTERVAL_MAX=0.1

[SPEED]
PER_REQUEST_DELAY=false
HISTORY_WAIT_TIME=auto
MESSAGE_BATCH=50
ENTITY_BATCH=500
CONCURRENCY=7
RESOLVE_UNKNOWN_PEERS=false
"""


class TestSpeedConfig(unittest.TestCase):
    def _load(self, text: str):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "conf.ini").write_text(text, encoding="utf-8")
            return load_config(path, path / "conf.ini")

    def test_defaults_are_fast(self):
        config = self._load(SPEED_CONFIG.split("[SPEED]")[0])
        self.assertTrue(config.speed.per_request_delay)
        self.assertEqual(config.speed.history_wait_time, 0.0)
        self.assertEqual(config.speed.message_batch, 500)
        self.assertEqual(config.speed.entity_batch, 200)
        self.assertEqual(config.speed.concurrency, 3)
        self.assertTrue(config.speed.resolve_unknown_peers)

    def test_section_is_parsed(self):
        config = self._load(SPEED_CONFIG)
        self.assertFalse(config.speed.per_request_delay)
        self.assertIsNone(config.speed.history_wait_time)      # auto -> решение Telethon
        self.assertEqual(config.speed.message_batch, 50)
        self.assertFalse(config.speed.resolve_unknown_peers)

    def test_values_are_clamped(self):
        config = self._load(SPEED_CONFIG)
        self.assertEqual(config.speed.entity_batch, 200)       # лимит API
        self.assertEqual(config.speed.concurrency, 7)

    def test_history_wait_time_number(self):
        config = self._load(SPEED_CONFIG.replace("HISTORY_WAIT_TIME=auto",
                                                 "HISTORY_WAIT_TIME=1.5"))
        self.assertEqual(config.speed.history_wait_time, 1.5)

    def test_invalid_history_wait_time(self):
        with self.assertRaises(ConfigError):
            self._load(SPEED_CONFIG.replace("HISTORY_WAIT_TIME=auto",
                                            "HISTORY_WAIT_TIME=быстро"))

    def test_negative_history_wait_time(self):
        with self.assertRaises(ConfigError):
            self._load(SPEED_CONFIG.replace("HISTORY_WAIT_TIME=auto",
                                            "HISTORY_WAIT_TIME=-1"))


class TestStatsText(unittest.TestCase):
    def test_not_found_hint(self):
        stats = ParseStats(scanned=10, reactors=2, not_found=2)
        self.assertIn("not_found", stats.as_text())

    def test_no_speed_section_when_not_measured(self):
        self.assertNotIn("Время парсинга", ParseStats(scanned=1).as_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
