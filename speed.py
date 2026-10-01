"""Профиль скорости и троттлинг сетевых запросов к Telegram.

Главный принцип ускорения: паузы нужны только между **реальными запросами** к
Telegram, а не между сообщениями.

Почему это важно:

* история приходит пачками по 100 сообщений за один запрос
  (`messages.getHistory`), поэтому на 10 000 постов нужно ~100 запросов, а не
  10 000 пауз;
* список отправителей звёзд (`reactions.top_reactors`) лежит **внутри**
  сообщения — посты без звёзд не требуют ни одного обращения к сети;
* один и тот же донатор встречается в десятках постов, поэтому повторные
  `get_entity` убираются кэшем;
* пользователей можно получать пачкой: `users.getUsers` принимает до 200
  сущностей за один запрос.

`RequestThrottle` выдерживает минимальный интервал между запросами (а не между
сообщениями): если обработка пачки заняла больше интервала, программа не спит
вовсе. После `FloodWaitError` интервал автоматически увеличивается — это
защита от повторных блокировок при агрессивных настройках.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from utils import get_logger, random_interval

logger = get_logger("speed")

# Сколько сообщений накапливаем перед обработкой (чем больше пачка, тем больше
# уникальных отправителей попадает в один запрос users.getUsers и тем меньше
# всего запросов: на 10 000 постов при пачке 500 это ~20 запросов на донаторов).
DEFAULT_MESSAGE_BATCH = 500
MIN_MESSAGE_BATCH = 10
MAX_MESSAGE_BATCH = 1000

# Лимит API: users.getUsers / channels.getChannels — до 200 сущностей за запрос.
DEFAULT_ENTITY_BATCH = 200
MAX_ENTITY_BATCH = 200
# Каналы/чаты встречаются реже и запрос тяжелее — ограничиваем сильнее.
MAX_CHANNEL_BATCH = 100

# Сколько запросов к API можно держать в воздухе одновременно.
DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 10

# Потолок адаптивного увеличения паузы после FloodWait, сек.
MAX_THROTTLE_INTERVAL = 5.0
# Пауза, которая включается после FloodWait, если задержки были нулевыми.
FLOOD_DEFAULT_DELAY = (0.5, 1.0)
# Во сколько раз увеличиваем интервал после каждого FloodWait.
FLOOD_PENALTY_FACTOR = 2.0

# Размер пачки истории Telegram (сообщений на один запрос messages.getHistory).
HISTORY_CHUNK_SIZE = 100

# Размер пачки участников (участников на один запрос channels.getParticipants).
PARTICIPANTS_CHUNK_SIZE = 200


@dataclass(frozen=True)
class SpeedProfile:
    """Настройки ускорения (секция `[SPEED]` в `conf.ini`)."""

    # Пауза из [DELAY] применяется между запросами к API, а не между сообщениями.
    per_request_delay: bool = True
    # Пауза Telethon между пачками истории: None — «как в Telethon»
    # (1 с при limit > 3000), 0 — не ждать (темп задаёт RequestThrottle).
    history_wait_time: Optional[float] = 0.0
    # Сколько сообщений обрабатываем одной пачкой.
    message_batch: int = DEFAULT_MESSAGE_BATCH
    # Сколько сущностей получать одним запросом (лимит API — 200).
    entity_batch: int = DEFAULT_ENTITY_BATCH
    # Одновременных запросов к API.
    concurrency: int = DEFAULT_CONCURRENCY
    # Пытаться ли получить пользователей без access_hash в кэше сессии
    # (пачкой по `entity_batch`, почти всегда возвращает not_found).
    resolve_unknown_peers: bool = True

    def throttle(self, delay: Iterable[float]) -> "RequestThrottle":
        """Создаёт троттлинг под этот профиль и задержки из `[DELAY]`."""
        return RequestThrottle(delay, enabled=self.per_request_delay)

    def batch_size_for(self, limit: int) -> int:
        """Размер пачки сообщений для прогона: не больше `message_batch` и `limit`.

        Чем больше пачка, тем больше уникальных отправителей попадает в один
        запрос `users.getUsers`. Для коротких прогонов пачка равна самому
        лимиту — всё равно успевает обработаться мгновенно, зато запросов
        минимум, а при Ctrl+C в буфере не остаётся необработанных сообщений.
        """
        batch = max(MIN_MESSAGE_BATCH, min(int(self.message_batch), MAX_MESSAGE_BATCH))
        limit = int(limit or 0)
        if limit > 0:
            batch = min(batch, limit)
        return max(1, batch)

    def entity_batch_sizes(self) -> tuple[int, int]:
        """(размер пачки пользователей, размер пачки каналов/чатов)."""
        users = max(1, min(int(self.entity_batch), MAX_ENTITY_BATCH))
        channels = max(1, min(users, MAX_CHANNEL_BATCH))
        return users, channels


class RequestThrottle:
    """Минимальный интервал между **сетевыми запросами** к Telegram.

    `await throttle.wait()` вызывается непосредственно перед каждым запросом:
    если с предыдущего запроса прошло больше случайного интервала из диапазона
    `[min, max]`, ожидания не будет совсем.

    Дополнительно ведёт счётчик запросов (`requests`) и суммарное время
    ожидания (`slept`) — это видно в логе и в итоговой сводке.
    """

    def __init__(self,
                 delay: Iterable[float] = (0.05, 0.1),
                 enabled: bool = True,
                 max_interval: float = MAX_THROTTLE_INTERVAL) -> None:
        values = list(delay or (0.0, 0.0))
        while len(values) < 2:
            values.append(values[-1] if values else 0.0)
        low, high = float(values[0]), float(values[1])
        if low > high:
            low, high = high, low
        self._base_delay = (max(low, 0.0), max(high, 0.0))
        self._delay = self._base_delay
        self._max_interval = float(max_interval)
        self._enabled = bool(enabled) and self._base_delay[1] > 0
        self._lock: Optional[asyncio.Lock] = None
        self._last = 0.0
        self.requests = 0
        self.slept = 0.0
        self.penalties = 0

    # -- состояние ---------------------------------------------------------- #
    @property
    def delay(self) -> tuple[float, float]:
        """Текущий интервал между запросами (может расти после FloodWait)."""
        return self._delay

    @property
    def enabled(self) -> bool:
        return self._enabled

    def note(self, count: int = 1) -> None:
        """Учитывает запрос(ы), которые библиотека выполнила сама."""
        self.requests += max(0, int(count))

    def summary(self) -> str:
        """Короткая сводка для лога."""
        return (f"запросов к API: {self.requests}, "
                f"ожидание между запросами: {self.slept:.2f} с"
                + (f", увеличений паузы после FloodWait: {self.penalties}"
                   if self.penalties else ""))

    # -- ожидание ----------------------------------------------------------- #
    async def wait(self, count: int = 1) -> float:
        """Ждёт остаток интервала перед следующим запросом.

        Возвращает фактическое время ожидания (0.0, если ждать не нужно).
        """
        self.requests += max(0, int(count))
        if not self._enabled:
            return 0.0

        interval = random_interval(*self._delay)
        if interval <= 0:
            return 0.0

        loop = asyncio.get_running_loop()
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            sleep_for = interval - (loop.time() - self._last)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
                self.slept += sleep_for
            else:
                sleep_for = 0.0
            self._last = loop.time()
        return sleep_for

    # -- адаптация к FloodWait --------------------------------------------- #
    def penalize(self, factor: float = FLOOD_PENALTY_FACTOR) -> tuple[float, float]:
        """Увеличивает интервал после FloodWait (с потолком `max_interval`).

        Если задержки были нулевыми, включается щадящий интервал
        `FLOOD_DEFAULT_DELAY`: продолжать бить в API прежним темпом нельзя.
        """
        low, high = self._delay
        if high <= 0:
            low, high = FLOOD_DEFAULT_DELAY
        else:
            low, high = low * factor, high * factor
        cap = self._max_interval
        self._delay = (min(low, cap), min(high, cap))
        self._enabled = True
        self.penalties += 1
        logger.warning(
            "FloodWait: интервал между запросами увеличен до %.2f-%.2f с.",
            self._delay[0], self._delay[1],
        )
        return self._delay


def is_batch_client(client: Any) -> bool:
    """Поддерживает ли клиент «сырые» запросы (`await client(request)`).

    Настоящий `TelegramClient` вызывает запросы через `__call__`, поэтому ему
    доступен пакетный `users.getUsers` (до 200 сущностей за один запрос).
    Тестовые заглушки и упрощённые клиенты обычно умеют только `get_entity` —
    для них используется последовательный путь без изменения поведения.
    """
    return callable(client)


__all__ = [
    "DEFAULT_CONCURRENCY", "DEFAULT_ENTITY_BATCH", "DEFAULT_MESSAGE_BATCH",
    "FLOOD_DEFAULT_DELAY", "FLOOD_PENALTY_FACTOR", "HISTORY_CHUNK_SIZE",
    "MAX_CHANNEL_BATCH", "MAX_CONCURRENCY", "MAX_ENTITY_BATCH",
    "MAX_MESSAGE_BATCH", "MAX_THROTTLE_INTERVAL", "MIN_MESSAGE_BATCH",
    "PARTICIPANTS_CHUNK_SIZE",
    "RequestThrottle", "SpeedProfile", "is_batch_client",
]
