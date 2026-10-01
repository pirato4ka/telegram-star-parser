#!/usr/bin/env python3
"""Офлайн-сравнение скорости парсинга: поштучный обход против пакетного.

Скрипт **не подключается к Telegram**: он имитирует канал и сеть (задержка на
запрос настраивается) и прогоняет два алгоритма на одних и тех же данных.

* «Старый цикл» — как было раньше: пауза `[DELAY]` после **каждого** сообщения
  и по одному `get_entity` на каждого неизвестного донатора.
* «Новый цикл» — текущий `handlers.parse_channel`: сообщения пачками, пауза
  только между реальными запросами, донаторы — по 200 штук одним
  `users.getUsers`, несколько запросов параллельно.

Время измеряется по **виртуальным часам**: `asyncio.sleep` не ждёт по-настоящему,
а только добавляет время к счётчику, поэтому прогон на 100 000 постов считается
за секунды, а результат соответствует реальному времени с выбранной задержкой
сети.

Запуск:
    python benchmark.py
    python benchmark.py --count 50000 --latency 0.4 --stars-share 0.15
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from telethon import functions
from telethon.tl import types

from handlers import EntityCache, build_records_for_message, parse_channel
from models import StarRecord
from speed import SpeedProfile
from utils import random_delay

# --------------------------------------------------------------------------- #
# Виртуальное время
# --------------------------------------------------------------------------- #
_real_sleep = asyncio.sleep


class VirtualClock:
    """Заменяет `asyncio.sleep`: время «идёт» мгновенно, но учитывается."""

    def __init__(self) -> None:
        self.now = 0.0
        self.calls = 0

    async def sleep(self, seconds: float, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        if seconds and seconds > 0:
            self.now += float(seconds)
        await _real_sleep(0)

    def reset(self) -> None:
        self.now = 0.0
        self.calls = 0


# --------------------------------------------------------------------------- #
# Имитация канала и клиента Telegram
# --------------------------------------------------------------------------- #
def build_channel(count: int, stars_share: float, reactors: int, known_share: float,
                  seed: int = 20250304) -> tuple[list[Any], dict[tuple, types.User]]:
    """Генерирует сообщения канала и «кэш сессии» (известные access_hash)."""
    rng = random.Random(seed)
    messages: list[Any] = []
    known: dict[tuple, types.User] = {}
    donor_ids: list[int] = []

    for index in range(count):
        message_id = count - index
        if rng.random() < stars_share:
            peers = []
            for _ in range(rng.randint(1, max(1, reactors))):
                if not donor_ids or rng.random() < 0.35:
                    donor_ids.append(1000000 + rng.randint(1, 500000))
                user_id = rng.choice(donor_ids)
                peers.append(types.MessageReactor(count=rng.randint(1, 50),
                                                  peer_id=types.PeerUser(user_id)))
                if rng.random() < known_share:
                    known.setdefault(
                        ("user", user_id),
                        types.User(id=user_id, username=f"user{user_id}", first_name="Donor",
                                   access_hash=rng.randint(1, 10 ** 9)))
            reactions = types.MessageReactions(
                results=[types.ReactionCount(reaction=types.ReactionPaid(),
                                             count=sum(r.count for r in peers))],
                top_reactors=peers,
            )
        else:
            reactions = None
        messages.append(_FakeMessage(message_id, reactions))
    return messages, known


class _FakeMessage:
    """Минимум полей сообщения, которые использует парсер."""

    __slots__ = ("id", "message", "media", "poll", "fwd_from", "reactions")

    def __init__(self, message_id: int, reactions: Any) -> None:
        self.id = message_id
        self.message = "post"
        self.media = None
        self.poll = None
        self.fwd_from = None
        self.reactions = reactions


class _FakeSession:
    def __init__(self, known: dict[tuple, types.User]) -> None:
        self._known = known

    def get_input_entity(self, peer: Any) -> Any:
        if isinstance(peer, types.PeerUser):
            entity = self._known.get(("user", peer.user_id))
            if entity is None:
                raise ValueError(f"no input entity for {peer}")
            return types.InputPeerUser(peer.user_id, entity.access_hash)
        raise ValueError(f"no input entity for {peer}")


class SimulatedClient:
    """Клиент с настраиваемой задержкой сети (без `__call__` — как старая заглушка).

    Без «сырых» запросов `EntityCache` идёт прежним последовательным путём
    (`get_entity` по одному на донатора) — так работал старый цикл.
    """

    def __init__(self, messages: list[Any], known: dict[tuple, types.User],
                 latency: float, clock: VirtualClock) -> None:
        self.messages = messages
        self.known = known
        self.latency = latency
        self.clock = clock
        self.session = _FakeSession(known)
        self.requests = 0
        self.users_per_call: list[int] = []

    async def _latency(self) -> None:
        self.requests += 1
        await self.clock.sleep(self.latency)

    async def get_messages(self, entity: Any, limit: int = 1, **kwargs: Any) -> list[Any]:
        await self._latency()
        return self.messages[:limit]

    async def iter_messages(self, entity: Any, limit: Optional[int] = None,
                            offset_id: int = 0, wait_time: Optional[float] = None,
                            **kwargs: Any):
        if wait_time is None:
            wait_time = 1 if (limit or 0) > 3000 else 0
        selected = [m for m in self.messages if not offset_id or m.id < offset_id]
        if limit:
            selected = selected[:limit]
        await self._latency()                       # первая пачка истории (100 сообщений)
        for emitted, message in enumerate(selected, start=1):
            yield message
            if emitted % 100 == 0 and emitted < len(selected):
                if wait_time:
                    await self.clock.sleep(wait_time)   # пауза Telethon между пачками
                await self._latency()                   # следующая пачка истории

    async def get_entity(self, peer: Any) -> Any:
        """Поштучный запрос (старый путь): неизвестный пользователь — тоже запрос."""
        await self._latency()
        if isinstance(peer, types.PeerUser):
            entity = self.known.get(("user", peer.user_id))
            if entity is None:
                raise ValueError(f"Could not find the input entity for {peer}")
            return entity
        raise ValueError(f"Could not find the input entity for {peer}")


class BatchingClient(SimulatedClient):
    """То же, но поддерживает `await client(request)` — как настоящий TelegramClient."""

    async def __call__(self, request: Any, **kwargs: Any) -> Any:
        """Пакетный запрос `users.getUsers` (новый путь)."""
        await self._latency()
        if isinstance(request, functions.users.GetUsersRequest):
            self.users_per_call.append(len(request.id))
            users = []
            for input_user in request.id:
                entity = self.known.get(("user", input_user.user_id))
                users.append(entity if entity is not None
                               else types.UserEmpty(id=input_user.user_id))
            return users
        raise TypeError(f"не поддержан запрос {type(request).__name__}")


# --------------------------------------------------------------------------- #
# Два алгоритма
# --------------------------------------------------------------------------- #
async def legacy_parse(client: SimulatedClient, limit: int, delay: tuple[float, float],
                       records: list[StarRecord]) -> int:
    """Старый цикл: пауза после каждого сообщения + get_entity на каждого донатора."""
    cache = EntityCache()
    scanned = 0
    async for message in client.iter_messages(types.PeerChannel(1), limit=limit):
        scanned += 1
        records.extend(await build_records_for_message(client, message, "Channel", cache))
        await random_delay(*delay)
    return scanned


async def fast_parse(client: SimulatedClient, limit: int, delay: tuple[float, float],
                     records: list[StarRecord], speed: SpeedProfile) -> int:
    """Текущий пакетный цикл из `handlers.parse_channel`."""
    throttle = speed.throttle(delay)
    stats = await parse_channel(client, types.PeerChannel(1), limit, records,
                                delay=delay, speed=speed, throttle=throttle)
    return stats.scanned


@dataclass
class RunResult:
    name: str
    wall: float = 0.0
    virtual: float = 0.0
    requests: int = 0
    records: int = 0
    scanned: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


async def run_case(messages: list[Any], known: dict[tuple, types.User], limit: int,
                   latency: float, delay: tuple[float, float], speed: SpeedProfile,
                   clock: VirtualClock, legacy: bool) -> RunResult:
    client: SimulatedClient = SimulatedClient(messages, known, latency, clock) \
        if legacy else BatchingClient(messages, known, latency, clock)
    records: list[StarRecord] = []
    clock.reset()
    started = time.monotonic()
    if legacy:
        scanned = await legacy_parse(client, limit, delay, records)
    else:
        scanned = await fast_parse(client, limit, delay, records, speed)
    wall = time.monotonic() - started
    return RunResult(
        name="Старый цикл (поштучно)" if legacy else "Новый цикл (пачками)",
        wall=wall, virtual=clock.now, requests=client.requests,
        records=len(records), scanned=scanned,
        extra={"users_per_call": client.users_per_call},
    )


async def run_benchmark(args: argparse.Namespace) -> None:
    clock = VirtualClock()
    real_sleep = asyncio.sleep
    asyncio.sleep = clock.sleep          # виртуальное время для всего прогона
    quiet = open(os.devnull, "w", encoding="utf-8")   # прогресс-бар не нужен в отчёте
    stderr, sys.stderr = sys.stderr, quiet
    try:
        messages, known = build_channel(args.count, args.stars_share, args.reactors,
                                        args.known_share, seed=args.seed)
        delay = (args.delay_min, args.delay_max)
        speed = SpeedProfile(
            per_request_delay=not args.no_throttle,
            history_wait_time=None if args.history_wait_auto else 0.0,
            message_batch=args.message_batch,
            entity_batch=args.entity_batch,
            concurrency=args.concurrency,
            resolve_unknown_peers=not args.skip_unknown,
        )

        legacy = await run_case(messages, known, args.count, args.latency, delay,
                                speed, clock, legacy=True)
        fast = await run_case(messages, known, args.count, args.latency, delay,
                              speed, clock, legacy=False)
    finally:
        asyncio.sleep = real_sleep
        sys.stderr = stderr
        quiet.close()

    star_posts = sum(1 for message in messages if message.reactions is not None)
    donors = len({r for message in messages if message.reactions
                  for r in (getattr(x.peer_id, "user_id", None)
                            for x in message.reactions.top_reactors) if r})
    batches = len(fast.extra.get("users_per_call") or [])

    def fmt(seconds: float) -> str:
        seconds = int(round(seconds))
        if seconds < 60:
            return f"{seconds} с"
        minutes, sec = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} мин {sec} с"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} ч {minutes} мин"

    speedup = (legacy.virtual / fast.virtual) if fast.virtual else float("inf")
    print()
    print("Моделирование (без подключения к Telegram)")
    print(f"  постов: {args.count}, со звёздами: {star_posts} "
          f"({args.stars_share:.0%}), уникальных донаторов: {donors} "
          f"(в кэше сессии: {args.known_share:.0%})")
    print(f"  задержка сети на запрос: {args.latency} с, [DELAY]: "
          f"{args.delay_min}-{args.delay_max} с")
    print()
    print(f"{'':32}{'Старый цикл':>18}{'Новый цикл':>18}")
    print(f"{'Сетевых запросов':32}{legacy.requests:>18}{fast.requests:>18}")
    print(f"{'  из них пакетных (по 200)':32}{'—':>18}{batches:>18}")
    print(f"{'Виртуальное время прогона':32}{fmt(legacy.virtual):>18}"
          f"{fmt(fast.virtual):>18}")
    print(f"{'Реальное время расчёта (CPU)':32}{legacy.wall:>17.2f} с{fast.wall:>17.2f} с")
    print()
    print(f"Ускорение: {speedup:.1f}× "
          f"(запросов меньше в {legacy.requests / max(fast.requests, 1):.1f} раза)")
    print()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=10000, help="сколько постов моделировать")
    parser.add_argument("--stars-share", type=float, default=0.08,
                        help="доля постов со звёздами (0..1)")
    parser.add_argument("--reactors", type=int, default=3,
                        help="максимум отправителей звёзд на пост")
    parser.add_argument("--known-share", type=float, default=0.6,
                        help="доля донаторов, чей access_hash уже есть в кэше сессии")
    parser.add_argument("--latency", type=float, default=0.25,
                        help="задержка сети на один запрос, сек")
    parser.add_argument("--delay-min", type=float, default=0.05)
    parser.add_argument("--delay-max", type=float, default=0.1)
    parser.add_argument("--message-batch", type=int, default=500)
    parser.add_argument("--entity-batch", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--history-wait-auto", action="store_true",
                        help="в новом цикле оставить паузу Telethon между пачками истории")
    parser.add_argument("--no-throttle", action="store_true",
                        help="в новом цикле не выдерживать паузу между запросами")
    parser.add_argument("--skip-unknown", action="store_true",
                        help="не пробовать донаторов без access_hash (RESOLVE_UNKNOWN_PEERS=false)")
    parser.add_argument("--seed", type=int, default=20250304)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    asyncio.run(run_benchmark(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
