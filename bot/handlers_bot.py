"""Хендлеры aiogram 3.x: команды и FSM wizard для парсинга."""

from __future__ import annotations

import asyncio
import html
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telethon import TelegramClient

from bot.aggregate import aggregate_star_records
from bot.config_bot import BotConfig
from bot.formatting import format_donors_table, format_queue_summary
from bot.queue_service import (
    QueueService,
    ParseTask,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TASK_TYPE_PARSE,
    TASK_TYPE_WARMUP,
)
from exporter import (
    build_result_filename,
    export_donors_summary,
    export_records,
    safe_filename,
    unique_path,
)
from handlers import (
    EntityCache,
    channel_title,
    get_last_message_id,
    parse_channel,
    parse_channels_input,
    resolve_channel,
    warmup_participants,
)
from models import ParseStats, StarRecord
from utils import format_seconds, get_logger

logger = get_logger("bot")

router = Router()


class WizardState(StatesGroup):
    CHANNELS = State()
    VALIDATE = State()
    SCOPE = State()
    SCOPE_N = State()
    SCOPE_YEARS = State()
    MODE = State()
    RUN = State()


# Управление таймаутами бездействия
_user_idle_tasks: dict[int, asyncio.Task] = {}


def _reset_user_idle_timer(user_id: int, state: FSMContext, bot: Bot, timeout_sec: int) -> None:
    if user_id in _user_idle_tasks:
        _user_idle_tasks[user_id].cancel()

    async def _timer():
        try:
            await asyncio.sleep(timeout_sec)
            curr = await state.get_state()
            if curr and curr != WizardState.RUN:
                await state.clear()
                try:
                    await bot.send_message(
                        user_id,
                        "⏳ Время сессии истекло из-за бездействия. Начните заново с /parse."
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    _user_idle_tasks[user_id] = asyncio.create_task(_timer())


def _cancel_user_idle_timer(user_id: int) -> None:
    if user_id in _user_idle_tasks:
        _user_idle_tasks[user_id].cancel()
        del _user_idle_tasks[user_id]


# --------------------------------------------------------------------------- #
# Команды /start, /help, /cancel, /queue, /status, /warmup
# --------------------------------------------------------------------------- #
@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start_help(message: Message, state: FSMContext, bot_config: BotConfig) -> None:
    text = (
        "👋 <b>Бот для сбора звёзд (Paid Reactions)</b>\n\n"
        "<b>Доступные команды:</b>\n"
        "• /parse [каналы] — запустить мастер парсинга каналов\n"
        "• /status — прогресс вашей текущей задачи\n"
        "• /queue — общая очередь задач\n"
        "• /cancel — отменить активный опрос или задачу парсинга\n"
        "• /warmup &lt;канал&gt; — прогреть кэш участников группы обсуждения\n"
        "• /help — эта справка\n"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, queue_service: QueueService) -> None:
    user_id = message.from_user.id
    _cancel_user_idle_timer(user_id)
    curr_state = await state.get_state()

    active_task = queue_service.get_user_active_task(user_id)
    if active_task:
        await queue_service.cancel_task(active_task)
        await message.answer("🛑 Запрос на отмену задачи отправлен. Данные сохраняются...")
        return

    if curr_state:
        await state.clear()
        await message.answer("❌ Диалог парсинга отменён.")
    else:
        await message.answer("Нет активных диалогов или задач для отмены.")


@router.message(Command("status"))
async def cmd_status(message: Message, queue_service: QueueService) -> None:
    user_id = message.from_user.id
    task = queue_service.get_user_active_task(user_id)
    if not task:
        await message.answer("У вас нет активных задач.")
        return

    if task.status == STATUS_PENDING:
        pos = queue_service.get_task_position(task)
        await message.answer(
            f"⏳ Ваша задача в очереди. Позиция: <b>{pos}</b>\n"
            f"Каналы: {html.escape(', '.join(task.channels))}\n"
            f"Для отмены используйте /cancel",
            parse_mode="HTML",
        )
    elif task.status == STATUS_RUNNING:
        flood_text = ""
        if task.flood_wait_until:
            wait_sec = max(0, int((task.flood_wait_until - datetime.now(timezone.utc)).total_seconds()))
            flood_text = f"\n⚠️ Ожидание FloodWait: {wait_sec} сек."

        tot = f"/{task.total_hint}" if task.total_hint else ""
        await message.answer(
            f"🔄 <b>Задача выполняется</b>\n"
            f"Канал: <b>{html.escape(task.current_channel)}</b>\n"
            f"Обработано: {task.scanned}{tot} (со звёздами: {task.with_stars}){flood_text}\n"
            f"Для отмены используйте /cancel",
            parse_mode="HTML",
        )


@router.message(Command("queue"))
async def cmd_queue(message: Message, queue_service: QueueService) -> None:
    tasks = queue_service.get_all_active_tasks()
    if not tasks:
        await message.answer("Очередь задач пуста.")
        return

    lines = ["<b>Текущая очередь задач:</b>"]
    for idx, t in enumerate(tasks, start=1):
        status_sym = "🔄 выполняется" if t.status == STATUS_RUNNING else "⏳ ожидает"
        lines.append(
            f"{idx}. {html.escape(t.user_display)} — {status_sym}\n"
            f"   Тип: {t.task_type}, Каналы: {html.escape(', '.join(t.channels))}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("warmup"))
async def cmd_warmup(
    message: Message,
    state: FSMContext,
    queue_service: QueueService,
    telethon_client: TelegramClient,
    bot_config: BotConfig,
) -> None:
    args = message.text.partition(" ")[2].strip()
    if not args:
        await message.answer("Использование: /warmup &lt;канал&gt;")
        return

    user_id = message.from_user.id
    if queue_service.get_user_active_task(user_id):
        await message.answer("У вас уже есть активная задача в очереди или выполнении.")
        return

    task_id = str(uuid.uuid4())[:8]
    user_display = f"@{message.from_user.username}" if message.from_user.username else f"id{user_id}"

    task = ParseTask(
        task_id=task_id,
        user_id=user_id,
        user_display=user_display,
        task_type=TASK_TYPE_WARMUP,
        channels=[args],
        scope_desc="warmup",
        coro_func=_run_warmup_task,
        coro_args=(message.bot, message.chat.id, telethon_client, args),
    )
    pos = await queue_service.add_task(task)
    await message.answer(f"Задача прогрева кэша добавлена в очередь (позиция: {pos}).")


async def _run_warmup_task(
    task: ParseTask,
    bot: Bot,
    chat_id: int,
    client: TelegramClient,
    channel_str: str,
) -> None:
    status_msg = await bot.send_message(chat_id, f"Начинаем прогрев кэша для {html.escape(channel_str)}...")
    try:
        resolved, title = await resolve_channel(client, channel_str)
        count = await warmup_participants(client, resolved)
        await status_msg.edit_text(f"✅ Прогрев кэша для <b>{html.escape(title)}</b> завершён. Закэшировано: {count} участников.", parse_mode="HTML")
    except asyncio.CancelledError:
        await status_msg.edit_text("🛑 Прогрев кэша отменён.")
        raise
    except Exception as exc:
        await status_msg.edit_text(f"❌ Ошибка прогрева кэша: {html.escape(str(exc))}")


# --------------------------------------------------------------------------- #
# Wizard /parse
# --------------------------------------------------------------------------- #
@router.message(Command("parse"))
async def cmd_parse(
    message: Message,
    state: FSMContext,
    queue_service: QueueService,
    telethon_client: TelegramClient,
    bot_config: BotConfig,
) -> None:
    user_id = message.from_user.id

    # Проверка: есть ли активная задача в очереди/выполнении
    active_task = queue_service.get_user_active_task(user_id)
    if active_task:
        pos = queue_service.get_task_position(active_task)
        await message.answer(
            f"⚠️ У вас уже есть активная задача (статус: {active_task.status}, позиция: {pos}).\n"
            f"Дождитесь завершения или используйте /cancel для отмены."
        )
        return

    curr_state = await state.get_state()
    if curr_state:
        await message.answer("Предыдущий диалог отменён, начинаем заново.")
        await state.clear()

    _reset_user_idle_timer(user_id, state, message.bot, bot_config.wizard_idle_timeout)

    args = message.text.partition(" ")[2].strip()
    if args:
        # Сразу передаем список каналов
        await _handle_channels_input(message, state, args, telethon_client, bot_config)
    else:
        await state.set_state(WizardState.CHANNELS)
        await message.answer(
            "📋 <b>Шаг 1: Каналы</b>\n\n"
            "Пришлите список каналов для парсинга (через запятую, точку с запятой или с новой строки):\n"
            "<i>Пример: @channel1, t.me/channel2, -1001234567890</i>",
            parse_mode="HTML",
        )


@router.message(WizardState.CHANNELS)
async def process_channels_msg(
    message: Message,
    state: FSMContext,
    telethon_client: TelegramClient,
    bot_config: BotConfig,
) -> None:
    user_id = message.from_user.id
    _reset_user_idle_timer(user_id, state, message.bot, bot_config.wizard_idle_timeout)
    await _handle_channels_input(message, state, message.text or "", telethon_client, bot_config)


async def _handle_channels_input(
    message: Message,
    state: FSMContext,
    raw_text: str,
    telethon_client: TelegramClient,
    bot_config: BotConfig,
) -> None:
    try:
        channels_raw = parse_channels_input(raw_text)
    except Exception as exc:
        await message.answer(
            f"Некорректный формат списка каналов: {exc}\n"
            "Пожалуйста, пришлите список заново (например: @durov, t.me/telegram):"
        )
        return

    if not channels_raw:
        await message.answer("Не удалось распознать каналы. Пришлите список заново:")
        return

    if len(channels_raw) > bot_config.max_channels_per_request:
        await message.answer(
            f"❌ Превышен лимит каналов в одном запросе ({len(channels_raw)} > {bot_config.max_channels_per_request}).\n"
            f"Пожалуйста, разбейте список на несколько запросов."
        )
        return

    validating_msg = await message.answer("⏳ Проверяем доступность каналов...")

    resolved_channels: list[dict] = []
    has_unresolved = False

    for item in channels_raw:
        item_str = str(item)
        try:
            entity, title = await resolve_channel(telethon_client, item)
            last_id = await get_last_message_id(telethon_client, entity)
            resolved_channels.append({
                "raw": item_str,
                "title": title,
                "found": True,
                "last_id": last_id,
            })
        except Exception:
            resolved_channels.append({
                "raw": item_str,
                "title": item_str,
                "found": False,
                "last_id": None,
            })
            has_unresolved = True

    await state.update_data(
        channels_meta=resolved_channels,
        all_channels_raw=channels_raw,
    )

    # Строим таблицу
    lines = ["<b>Результаты проверки каналов:</b>"]
    for idx, c in enumerate(resolved_channels, start=1):
        if c["found"]:
            approx = f"≈ {c['last_id']}" if c["last_id"] else "неизвестно"
            lines.append(f"{idx}. <b>{html.escape(c['title'])}</b> — найден, всего постов {approx}")
        else:
            lines.append(f"{idx}. <b>{html.escape(c['raw'])}</b> — ❌ НЕ найден / приватный")

    lines.append(
        "\n<i>Примечание: число постов приблизительное (по id последнего сообщения), "
        "т.к. Telegram API не отдаёт точный счётчик постов канала. "
        "Статус «НЕ найден / приватный» означает, что канал не существует, неверно указан, "
        "либо текущий аккаунт Telethon не состоит в этом приватном канале.</i>"
    )

    found_count = sum(1 for c in resolved_channels if c["found"])
    if found_count == 0:
        await state.set_state(WizardState.CHANNELS)
        await validating_msg.edit_text(
            "\n".join(lines) + "\n\n❌ <b>Не получен ни один канал.</b> Введите список каналов заново:",
            parse_mode="HTML",
        )
        return

    if has_unresolved:
        await state.set_state(WizardState.VALIDATE)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Убрать ненайденные и продолжить", callback_data="val_continue"),
            ],
            [
                InlineKeyboardButton(text="Ввести заново", callback_data="val_restart"),
            ]
        ])
        await validating_msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    else:
        # Все каналы найдены -> сразу к SCOPE
        await validating_msg.edit_text("\n".join(lines), parse_mode="HTML")
        await _show_scope_step(message, state)


@router.callback_query(WizardState.VALIDATE, F.data == "val_restart")
async def cb_validate_restart(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.clear()
    await state.set_state(WizardState.CHANNELS)
    await callback.message.edit_text(
        "📋 <b>Шаг 1: Каналы</b>\n\nПришлите список каналов для парсинга заново:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(WizardState.VALIDATE, F.data == "val_continue")
async def cb_validate_continue(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    data = await state.get_data()
    meta = data.get("channels_meta", [])
    filtered = [c for c in meta if c["found"]]
    await state.update_data(channels_meta=filtered)
    await callback.answer()
    await _show_scope_step(callback.message, state)


async def _show_scope_step(message: Message, state: FSMContext) -> None:
    await state.set_state(WizardState.SCOPE)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Все посты", callback_data="scope_all"),
            InlineKeyboardButton(text="Последние N", callback_data="scope_n"),
        ],
        [
            InlineKeyboardButton(text="По годам", callback_data="scope_years"),
        ]
    ])
    text = (
        "📊 <b>Шаг 2: Диапазон парсинга</b>\n\n"
        "Выберите, какие посты анализировать (выбор применяется одинаково ко всем каналам):"
    )
    if isinstance(message, Message) and message.from_user and message.from_user.is_bot:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(WizardState.SCOPE, F.data == "scope_all")
async def cb_scope_all(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.update_data(scope_type="all", limit=1_000_000, start_date=None, end_date=None, scope_desc="Все посты")
    await callback.answer()
    await _show_mode_step(callback.message, state, bot_config)


@router.callback_query(WizardState.SCOPE, F.data == "scope_n")
async def cb_scope_n(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.set_state(WizardState.SCOPE_N)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="scope_back")]
    ])
    await callback.message.edit_text(
        "Введите количество последних постов для анализа (целое число от 1 до 1 000 000):",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(WizardState.SCOPE_N, F.data == "scope_back")
@router.callback_query(WizardState.SCOPE_YEARS, F.data == "scope_back")
async def cb_scope_back(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await _show_scope_step(callback.message, state)
    await callback.answer()


@router.message(WizardState.SCOPE_N)
async def process_scope_n_msg(message: Message, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(message.from_user.id, state, message.bot, bot_config.wizard_idle_timeout)
    val = (message.text or "").strip()
    try:
        n = int(val)
        if n <= 0 or n > 1_000_000:
            raise ValueError()
    except (ValueError, TypeError):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="scope_back")]
        ])
        await message.answer(
            "Некорректное значение. Введите целое положительное число до 1 000 000 (например: 500):",
            reply_markup=kb,
        )
        return

    await state.update_data(
        scope_type="n",
        limit=n,
        start_date=None,
        end_date=None,
        scope_desc=f"Последние {n}",
    )
    await _show_mode_step(message, state, bot_config)


@router.callback_query(WizardState.SCOPE, F.data == "scope_years")
async def cb_scope_years(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.set_state(WizardState.SCOPE_YEARS)
    data = await state.get_data()
    selected_years = data.get("selected_years", [])
    kb = _build_years_keyboard(selected_years)
    await callback.message.edit_text(
        "📅 <b>Выберите годы для парсинга (мульти-выбор):</b>\n\n"
        "<i>Внимание: годы считаются по UTC.</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


def _build_years_keyboard(selected: list[int]) -> InlineKeyboardMarkup:
    current_year = datetime.now(timezone.utc).year
    buttons = []
    row = []
    for y in range(2015, current_year + 1):
        mark = "✅ " if y in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{y}", callback_data=f"year_toggle_{y}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="scope_back"),
        InlineKeyboardButton(text="Готово", callback_data="year_done"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(WizardState.SCOPE_YEARS, F.data.startswith("year_toggle_"))
async def cb_toggle_year(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    year = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_years", []))
    if year in selected:
        selected.remove(year)
    else:
        selected.append(year)
    await state.update_data(selected_years=selected)
    kb = _build_years_keyboard(selected)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(WizardState.SCOPE_YEARS, F.data == "year_done")
async def cb_year_done(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    data = await state.get_data()
    selected: list[int] = list(data.get("selected_years", []))
    if not selected:
        await callback.answer("Выберите хотя бы один год!", show_alert=True)
        return

    min_year = min(selected)
    max_year = max(selected)
    start_date = datetime(min_year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(max_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

    years_str = ", ".join(str(y) for y in sorted(selected))
    await state.update_data(
        scope_type="years",
        limit=1_000_000,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        selected_years=list(selected),
        scope_desc=f"Годы: {years_str} (UTC)",
    )
    await callback.answer()
    await _show_mode_step(callback.message, state, bot_config)


async def _show_mode_step(message: Message, state: FSMContext, bot_config: BotConfig) -> None:
    await state.set_state(WizardState.MODE)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Все, кто донатил", callback_data="mode_all"),
        ],
        [
            InlineKeyboardButton(text=f"Только > {bot_config.donor_threshold}", callback_data="mode_threshold"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode_back"),
        ]
    ])
    text = "🎯 <b>Шаг 3: Формат ответа</b>\n\nВыберите, кого включать в отчёт:"
    if isinstance(message, Message) and message.from_user and message.from_user.is_bot:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(WizardState.MODE, F.data == "mode_back")
async def cb_mode_back(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await _show_scope_step(callback.message, state)
    await callback.answer()


@router.callback_query(WizardState.MODE, F.data.in_(["mode_all", "mode_threshold"]))
async def cb_mode_select(
    callback: CallbackQuery,
    state: FSMContext,
    queue_service: QueueService,
    telethon_client: TelegramClient,
    bot_config: BotConfig,
) -> None:
    user_id = callback.from_user.id
    _cancel_user_idle_timer(user_id)

    only_above = (callback.data == "mode_threshold")
    await state.update_data(only_above_threshold=only_above)

    data = await state.get_data()
    meta = data.get("channels_meta", [])
    channel_names = [c["raw"] for c in meta]
    scope_desc = data.get("scope_desc", "Все")

    await state.set_state(WizardState.RUN)
    await callback.answer()

    task_id = str(uuid.uuid4())[:8]
    user_display = f"@{callback.from_user.username}" if callback.from_user.username else f"id{user_id}"

    status_msg = await callback.message.edit_text(
        "⏳ Задача поставлена в очередь...",
        parse_mode="HTML",
    )

    parse_task = ParseTask(
        task_id=task_id,
        user_id=user_id,
        user_display=user_display,
        task_type=TASK_TYPE_PARSE,
        channels=channel_names,
        scope_desc=scope_desc,
        coro_func=_run_parsing_task,
        coro_args=(
            callback.bot,
            callback.message.chat.id,
            status_msg.message_id,
            telethon_client,
            bot_config,
            data,
            state,
        ),
    )

    pos = await queue_service.add_task(parse_task)
    if pos > 1:
        await status_msg.edit_text(f"⏳ Ваша задача в очереди. Позиция: <b>{pos}</b>", parse_mode="HTML")


async def _run_parsing_task(
    task: ParseTask,
    bot: Bot,
    chat_id: int,
    status_msg_id: int,
    client: TelegramClient,
    bot_config: BotConfig,
    wizard_data: dict,
    state: FSMContext,
) -> None:
    meta = wizard_data.get("channels_meta", [])
    channels_raw = [c["raw"] for c in meta]
    limit = int(wizard_data.get("limit", 1_000_000))
    s_date_str = wizard_data.get("start_date")
    e_date_str = wizard_data.get("end_date")
    start_date = datetime.fromisoformat(s_date_str) if s_date_str else None
    end_date = datetime.fromisoformat(e_date_str) if e_date_str else None
    scope_desc = wizard_data.get("scope_desc", "Все посты")
    only_above = wizard_data.get("only_above_threshold", False)
    selected_years_raw = wizard_data.get("selected_years")
    selected_years_set = set(selected_years_raw) if selected_years_raw else None

    all_records: list[StarRecord] = []
    stats_by_channel: dict[str, ParseStats] = {}
    channel_outcomes: dict[str, str] = {}

    last_edit_time = 0.0

    async def _progress_cb(ch: str, scanned: int, with_stars: int, tot: Optional[int], flood_until: Optional[datetime]) -> None:
        nonlocal last_edit_time
        task.current_channel = ch
        task.scanned = scanned
        task.with_stars = with_stars
        task.total_hint = tot
        task.flood_wait_until = flood_until

        now = time.monotonic()
        if now - last_edit_time < bot_config.progress_edit_interval:
            return
        last_edit_time = now

        flood_str = ""
        if flood_until:
            sec_left = max(0, int((flood_until - datetime.now(timezone.utc)).total_seconds()))
            flood_str = f"\n⚠️ Ожидание Telegram FloodWait: {sec_left} сек."

        tot_str = f"/{tot}" if tot else ""
        text = (
            f"🔄 <b>Парсинг канала:</b> {html.escape(ch)}\n"
            f"Просмотрено: {scanned}{tot_str} (со звёздами: {with_stars}){flood_str}\n"
            f"<i>Для отмены используйте /cancel</i>"
        )
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg_id, parse_mode="HTML")
        except TelegramRetryAfter as retry:
            await asyncio.sleep(retry.retry_after)
        except TelegramBadRequest:
            pass
        except Exception:
            pass

    interrupted = False

    for ch_item in channels_raw:
        if task.cancelled_by_user:
            interrupted = True
            channel_outcomes[str(ch_item)] = "отменено"
            break

        task.current_channel = str(ch_item)
        try:
            entity, title = await resolve_channel(client, ch_item)
        except Exception as exc:
            logger.error("Не удалось разрешить канал %s: %s", ch_item, exc)
            channel_outcomes[str(ch_item)] = f"ошибка: {exc}"
            continue

        ch_stats = ParseStats(requested=limit)
        stats_by_channel[title] = ch_stats

        try:
            await parse_channel(
                client=client,
                entity=entity,
                limit=limit,
                records=all_records,
                stats=ch_stats,
                start_date=start_date,
                end_date=end_date,
                progress_callback=_progress_cb,
                selected_years=selected_years_set,
            )
            channel_outcomes[title] = "успешно"
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Парсинг прерван для канала %s", title)
            ch_stats.interrupted = True
            interrupted = True
            channel_outcomes[title] = "отменено оператором"
            break
        except Exception as exc:
            logger.exception("Ошибка при парсинге канала %s: %s", title, exc)
            ch_stats.interrupted = True
            interrupted = True
            channel_outcomes[title] = f"ошибка: {exc}"
            break

    # Обработка результатов
    await _send_final_results(
        bot=bot,
        chat_id=chat_id,
        status_msg_id=status_msg_id,
        all_records=all_records,
        stats_by_channel=stats_by_channel,
        channel_outcomes=channel_outcomes,
        scope_desc=scope_desc,
        only_above_threshold=only_above,
        bot_config=bot_config,
        interrupted=interrupted,
        channels_meta={c["raw"]: c for c in meta},
    )

    await state.clear()


async def _send_final_results(
    bot: Bot,
    chat_id: int,
    status_msg_id: int,
    all_records: list[StarRecord],
    stats_by_channel: dict[str, ParseStats],
    channel_outcomes: dict[str, str],
    scope_desc: str,
    only_above_threshold: bool,
    bot_config: BotConfig,
    interrupted: bool,
    channels_meta: dict,
) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
    except Exception:
        pass

    # Агрегация донатеров
    donors, anon_summary = aggregate_star_records(
        records=all_records,
        threshold=bot_config.donor_threshold,
        only_above_threshold=only_above_threshold,
    )

    # 1. Excel-файл со сводкой
    # Имя файла: YYYY-MM-DD_HH-MM-SS_<каналы>_donors.xlsx
    first_ch = list(stats_by_channel.keys())[0] if stats_by_channel else "channels"
    if len(stats_by_channel) > 1:
        first_ch = f"{first_ch}_and_{len(stats_by_channel)-1}_more"
    filename = build_result_filename(f"{first_ch}_donors")
    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)
    target_xlsx_path = str(unique_path(out_dir, filename))

    saved_xlsx_path = export_donors_summary(
        donors=donors,
        channels_meta=channels_meta,
        stats_by_channel=stats_by_channel,
        requested_scope=scope_desc,
        output_path=target_xlsx_path,
    )

    # Детализированный Excel, если DETAILED_EXCEL=true
    detailed_path: Optional[Path] = None
    if bot_config.detailed_excel and all_records:
        detailed_path = export_records(
            records=all_records,
            output_dir=out_dir,
            channel_name=first_ch,
        )

    # 2. Текстовая таблица донатеров в чат
    ch_names = list(stats_by_channel.keys()) if stats_by_channel else list(channels_meta.keys())
    messages = format_donors_table(
        donors=donors,
        anon_donor=anon_summary,
        channel_names=ch_names,
        threshold=bot_config.donor_threshold,
        only_above_threshold=only_above_threshold,
        stats_by_channel=stats_by_channel,
    )

    if interrupted:
        header_prefix = "⚠️ <b>Парсинг был прерван/отменён. Результат частичный.</b>\n\n"
        messages[0] = header_prefix + messages[0]

    for msg in messages:
        await bot.send_message(chat_id, msg, parse_mode="HTML")

    # 3. Сводка по каналам
    q_summary = format_queue_summary(stats_by_channel, channel_outcomes)
    await bot.send_message(chat_id, q_summary, parse_mode="HTML")

    # 4. Отправка Excel файла
    file_size = Path(saved_xlsx_path).stat().st_size if Path(saved_xlsx_path).exists() else 0
    max_file_size = 50 * 1024 * 1024  # 50 MB

    if bot_config.send_files and file_size <= max_file_size and file_size > 0:
        await bot.send_document(
            chat_id,
            FSInputFile(saved_xlsx_path),
            caption=f"📊 Сводный отчёт: {html.escape(scope_desc)}",
            parse_mode="HTML",
        )
    else:
        abs_p = str(Path(saved_xlsx_path).resolve())
        reason = "размер превышает 50 МБ" if file_size > max_file_size else "отключена отправка файлов"
        await bot.send_message(
            chat_id,
            f"📁 Файл сохранён на сервере ({reason}):\n<code>{html.escape(abs_p)}</code>",
            parse_mode="HTML",
        )

    if detailed_path and detailed_path.exists():
        det_size = detailed_path.stat().st_size
        if bot_config.send_files and det_size <= max_file_size:
            await bot.send_document(
                chat_id,
                FSInputFile(str(detailed_path)),
                caption="📋 Детализированный построчный отчёт",
            )
        else:
            await bot.send_message(
                chat_id,
                f"📁 Детализированный файл:\n<code>{html.escape(str(detailed_path.resolve()))}</code>",
                parse_mode="HTML",
            )
