"""Очередь задач, семафор, прогресс и отмена."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

from utils import get_logger

logger = get_logger("bot")

TASK_TYPE_PARSE = "parse"
TASK_TYPE_WARMUP = "warmup"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


@dataclass
class ParseTask:
    task_id: str
    user_id: int
    user_display: str
    task_type: str  # parse или warmup
    channels: list[str]
    scope_desc: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: str = STATUS_PENDING

    # Для исполнения
    coro_func: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None
    coro_args: tuple = ()
    coro_kwargs: dict = field(default_factory=dict)

    # Управление отменой
    asyncio_task: Optional[asyncio.Task] = None
    cancelled_by_user: bool = False

    # Прогресс
    current_channel: str = ""
    scanned: int = 0
    with_stars: int = 0
    total_hint: Optional[int] = None
    flood_wait_until: Optional[datetime] = None


class QueueService:
    """Сервис управления FIFO-очередью задач с семафором = 1."""

    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(1)
        self.tasks: list[ParseTask] = []
        self._queue: asyncio.Queue[ParseTask] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def start_worker(self) -> None:
        """Запускает фоновый воркер обработки очереди."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    def stop_worker(self) -> None:
        """Останавливает фоновый воркер."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    async def _worker_loop(self) -> None:
        while True:
            task = await self._queue.get()
            if task.status == STATUS_CANCELLED:
                self._queue.task_done()
                continue

            async with self.semaphore:
                if task.status == STATUS_CANCELLED:
                    self._queue.task_done()
                    continue

                task.status = STATUS_RUNNING
                logger.info("Старт выполнения задачи %s (тип: %s, пользователь: %s)",
                            task.task_id, task.task_type, task.user_display)

                loop = asyncio.get_running_loop()
                # Создаем задачу asyncio
                if task.coro_func is not None:
                    asyncio_task = loop.create_task(
                        task.coro_func(task, *task.coro_args, **task.coro_kwargs)
                    )
                    task.asyncio_task = asyncio_task
                    try:
                        await asyncio_task
                        if task.status == STATUS_RUNNING:
                            task.status = STATUS_COMPLETED
                    except asyncio.CancelledError:
                        task.status = STATUS_CANCELLED
                        logger.info("Задача %s отменена.", task.task_id)
                    except Exception as exc:
                        task.status = STATUS_FAILED
                        logger.exception("Ошибка при выполнении задачи %s: %s", task.task_id, exc)
                    finally:
                        task.asyncio_task = None
                else:
                    task.status = STATUS_COMPLETED

                self._queue.task_done()

    async def add_task(self, task: ParseTask) -> int:
        """Добавляет задачу в очередь и возвращает её позицию (1-based)."""
        async with self._lock:
            self.tasks.append(task)
            await self._queue.put(task)
            # Считаем позицию среди не завершенных
            pos = sum(1 for t in self.tasks if t.status in (STATUS_PENDING, STATUS_RUNNING))
            return pos

    def get_user_active_task(self, user_id: int) -> Optional[ParseTask]:
        """Возвращает активную (pending или running) задачу пользователя."""
        for t in self.tasks:
            if t.user_id == user_id and t.status in (STATUS_PENDING, STATUS_RUNNING):
                return t
        return None

    def get_task_position(self, task: ParseTask) -> int:
        """Возвращает позицию задачи в очереди среди ожидающих."""
        pos = 1
        for t in self.tasks:
            if t.task_id == task.task_id:
                return pos
            if t.status == STATUS_PENDING:
                pos += 1
        return pos

    def get_all_active_tasks(self) -> list[ParseTask]:
        """Все задачи со статусом running или pending."""
        return [t for t in self.tasks if t.status in (STATUS_PENDING, STATUS_RUNNING)]

    async def cancel_task(self, task: ParseTask) -> bool:
        """Отменяет задачу (если pending — просто отменяет, если running — cancel asyncio_task)."""
        task.cancelled_by_user = True
        if task.status == STATUS_PENDING:
            task.status = STATUS_CANCELLED
            return True
        elif task.status == STATUS_RUNNING:
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
            return True
        return False
