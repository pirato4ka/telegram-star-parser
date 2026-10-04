"""Хендлеры aiogram 3.x: команды, админ-панель доступа и FSM wizard для парсинга."""

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
from bot.user_store import UserStore
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

# Технический предел обхода в режиме «все посты» (в отчёты не попадает).
UNLIMITED_LIMIT = 1_000_000

BOT_TITLE = "👋 <b>Бот для пробива донатеров звёзд в Telegram</b>"


class WizardState(StatesGroup):
    CHANNELS = State()
    VALIDATE = State()
    SCOPE = State()
    SCOPE_N = State()
    SCOPE_YEARS = State()
    MODE = State()
    RUN = State()


class AdminState(StatesGroup):
    ADD_USER = State()
    DEL_USER = State()


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
# Вспомогательные функции: права администратора и клавиатуры
# --------------------------------------------------------------------------- #
def is_admin(user_id: int, bot_config: BotConfig) -> bool:
    """Является ли пользователь администратором (может выдавать доступ)."""
    return int(user_id) in set(bot_config.admin_ids or set())


def _main_keyboard(admin: bool) -> InlineKeyboardMarkup:
    """Клавиатура главного меню."""
    rows = [
        [InlineKeyboardButton(text="🚀 Запустить парсинг", callback_data="wiz:start")],
        [
            InlineKeyboardButton(text="📈 Моя задача", callback_data="wiz:status"),
            InlineKeyboardButton(text="🕒 Очередь", callback_data="wiz:queue"),
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="wiz:help")],
    ]
    if admin:
        rows.append([InlineKeyboardButton(text="🛠 Доступ к боту", callback_data="adm:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_keyboard(callback_data: str = "wiz:cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Отмена", callback_data=callback_data)]
    ])


def _help_text(admin: bool = False) -> str:
    """Справка бота. Строка «/help — эта справка» намеренно не дублируется."""
    lines = [
        "❓ <b>Справка</b>",
        "",
        "Бот проходит по последним постам канала и собирает, кто и сколько звёзд "
        "(Paid Reactions) отправил. В Excel каждый донат — отдельной строкой со "
        "ссылкой на пост.",
        "",
        "<b>Как это работает</b>",
        "1️⃣ присылаете каналы — @username, ссылку или id",
        "2️⃣ выбираете диапазон постов: все, последние N или по годам",
        "3️⃣ выбираете, кого показывать: всех донатеров или только крупных",
        "4️⃣ получаете таблицу и Excel-файл",
        "",
        "<b>Команды</b>",
        "• /parse [каналы] — запустить парсинг",
        "• /status — прогресс вашей задачи",
        "• /queue — очередь задач",
        "• /cancel — отменить диалог или задачу",
        "• /warmup &lt;канал&gt; — прогреть кэш участников группы обсуждения",
    ]
    if admin:
        lines += [
            "",
            "<b>Администратору</b>",
            "• /admin — панель доступа к боту",
            "• /users — кто имеет доступ",
            "• /adduser &lt;id&gt; [@username] — добавить пользователя",
            "• /deluser &lt;id&gt; — убрать пользователя",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Команды /start, /help, /cancel, /queue, /status, /warmup
# --------------------------------------------------------------------------- #
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot_config: BotConfig) -> None:
    admin = is_admin(message.from_user.id, bot_config)
    if await state.get_state():
        await state.clear()
    text = (
        f"{BOT_TITLE}\n\n"
        "Собираю, кто и сколько звёзд отправил на посты канала, и отдаю готовый "
        "Excel: <b>каждый донат отдельной строкой</b> — канал, ссылка на пост, "
        "username, id и количество звёзд.\n\n"
        "Нажмите «🚀 Запустить парсинг» или пришлите /parse."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_main_keyboard(admin))


@router.message(Command("help"))
async def cmd_help(message: Message, bot_config: BotConfig) -> None:
    await message.answer(
        _help_text(is_admin(message.from_user.id, bot_config)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "wiz:help")
async def cb_help(callback: CallbackQuery, bot_config: BotConfig) -> None:
    await callback.answer()
    try:
        await callback.message.edit_text(
            _help_text(is_admin(callback.from_user.id, bot_config)),
            parse_mode="HTML",
            reply_markup=_main_keyboard(is_admin(callback.from_user.id, bot_config)),
        )
    except TelegramBadRequest:
        await callback.message.answer(_help_text(is_admin(callback.from_user.id, bot_config)),
                                      parse_mode="HTML")


@router.callback_query(F.data == "wiz:start")
async def cb_wizard_start(
    callback: CallbackQuery,
    state: FSMContext,
    queue_service: QueueService,
    bot_config: BotConfig,
) -> None:
    """Кнопка «🚀 Запустить парсинг» в главном меню."""
    user_id = callback.from_user.id
    active_task = queue_service.get_user_active_task(user_id)
    if active_task:
        await callback.answer()
        await callback.message.answer(
            "⚠️ У вас уже есть активная задача. Дождитесь завершения "
            "или отмените её командой /cancel."
        )
        return

    await state.clear()
    await state.set_state(WizardState.CHANNELS)
    _reset_user_idle_timer(user_id, state, callback.bot, bot_config.wizard_idle_timeout)
    await callback.answer()
    await callback.message.answer(
        _channels_step_text(),
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
    )


@router.callback_query(F.data == "wiz:cancel")
async def cb_wizard_cancel(
    callback: CallbackQuery, state: FSMContext, bot_config: BotConfig
) -> None:
    _cancel_user_idle_timer(callback.from_user.id)
    await state.clear()
    await callback.answer("Отменено")
    text = "❌ Действие отменено. Начать заново — /parse."
    keyboard = _main_keyboard(is_admin(callback.from_user.id, bot_config))
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=keyboard)


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


def _progress_bar(done: int, total: int, width: int = 14) -> str:
    """Полоска прогресса для сообщений со статусом: ▰▰▰▱▱▱▱ 42%."""
    if total <= 0:
        return ""
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    bar = "▰" * filled + "▱" * (width - filled)
    return f"{bar} {int(round(ratio * 100))}%"


@router.message(Command("status"))
async def cmd_status(message: Message, queue_service: QueueService) -> None:
    await _answer_status(message, message.from_user.id, queue_service)


@router.callback_query(F.data == "wiz:status")
async def cb_status(callback: CallbackQuery, queue_service: QueueService) -> None:
    await callback.answer()
    await _answer_status(callback.message, callback.from_user.id, queue_service)


async def _answer_status(message: Message, user_id: int, queue_service: QueueService) -> None:
    task = queue_service.get_user_active_task(user_id)
    if not task:
        await message.answer("У вас нет активных задач. Запустить парсинг — /parse.")
        return

    if task.status == STATUS_PENDING:
        pos = queue_service.get_task_position(task)
        await message.answer(
            f"⏳ <b>Задача в очереди</b>\n"
            f"Позиция: <b>{pos}</b>\n"
            f"Каналы: {html.escape(', '.join(task.channels))}\n"
            f"Диапазон: {html.escape(task.scope_desc)}\n"
            f"<i>Отмена — /cancel</i>",
            parse_mode="HTML",
        )
    elif task.status == STATUS_RUNNING:
        flood_text = ""
        if task.flood_wait_until:
            wait_sec = max(0, int((task.flood_wait_until - datetime.now(timezone.utc)).total_seconds()))
            flood_text = f"\n⚠️ Ожидание FloodWait: {wait_sec} сек."

        tail = f"/{task.total_hint}" if task.total_hint else ""
        bar = _progress_bar(task.scanned, task.total_hint or 0)
        bar_line = f"\n{bar}" if bar else ""
        await message.answer(
            f"🔄 <b>Задача выполняется</b>{bar_line}\n"
            f"Канал: <b>{html.escape(task.current_channel)}</b>\n"
            f"Обработано: {task.scanned}{tail} (со звёздами: {task.with_stars}){flood_text}\n"
            f"<i>Отмена — /cancel</i>",
            parse_mode="HTML",
        )


@router.message(Command("queue"))
async def cmd_queue(message: Message, queue_service: QueueService) -> None:
    await _answer_queue(message, queue_service)


@router.callback_query(F.data == "wiz:queue")
async def cb_queue(callback: CallbackQuery, queue_service: QueueService) -> None:
    await callback.answer()
    await _answer_queue(callback.message, queue_service)


async def _answer_queue(message: Message, queue_service: QueueService) -> None:
    tasks = queue_service.get_all_active_tasks()
    if not tasks:
        await message.answer("🕒 Очередь задач пуста.")
        return

    lines = ["<b>🕒 Очередь задач</b>", ""]
    for idx, t in enumerate(tasks, start=1):
        status_sym = "🔄 выполняется" if t.status == STATUS_RUNNING else "⏳ ожидает"
        lines.append(
            f"{idx}. {html.escape(t.user_display)} — {status_sym}\n"
            f"   {html.escape(', '.join(t.channels))} · {html.escape(t.scope_desc)}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------------------------- #
# Админ-панель: добавление и удаление пользователей по id
# --------------------------------------------------------------------------- #
def _access_panel_text(store: Optional[UserStore], bot_config: BotConfig) -> str:
    added = store.all() if store else []
    lines = [
        "🛠 <b>Доступ к боту</b>",
        "",
        f"Администраторы: <b>{len(bot_config.admin_ids)}</b>",
        f"Из conf.ini (ALLOWED_USER_IDS): <b>{len(bot_config.allowed_user_ids)}</b>",
        f"Добавлено через бота: <b>{len(added)}</b>",
    ]
    if added:
        lines.append("")
        lines.append("<b>Добавленные пользователи:</b>")
        for user in added[:20]:
            lines.append(f"• <code>{user.user_id}</code> — {html.escape(user.title())}")
        if len(added) > 20:
            lines.append(f"… и ещё {len(added) - 20}")
    return "\n".join(lines)


def _access_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Список доступа", callback_data="adm:list"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:panel"),
        ],
        [InlineKeyboardButton(text="➕ Добавить по id", callback_data="adm:add")],
        [InlineKeyboardButton(text="➖ Удалить по id", callback_data="adm:del")],
    ])


async def _require_admin_message(message: Message, bot_config: BotConfig) -> bool:
    if is_admin(message.from_user.id, bot_config):
        return True
    await message.answer("⛔ Команда доступна только администратору.")
    return False


async def _require_admin_callback(callback: CallbackQuery, bot_config: BotConfig) -> bool:
    if is_admin(callback.from_user.id, bot_config):
        return True
    await callback.answer("⛔ Только для администратора", show_alert=True)
    return False


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    await state.clear()
    await message.answer(
        _access_panel_text(user_store, bot_config),
        parse_mode="HTML",
        reply_markup=_access_panel_keyboard(),
    )


@router.callback_query(F.data == "adm:panel")
async def cb_admin_panel(
    callback: CallbackQuery,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_callback(callback, bot_config):
        return
    await state.clear()
    await callback.answer()
    text = _access_panel_text(user_store, bot_config)
    try:
        await callback.message.edit_text(text, parse_mode="HTML",
                                         reply_markup=_access_panel_keyboard())
    except TelegramBadRequest:
        await callback.message.answer(text, parse_mode="HTML",
                                      reply_markup=_access_panel_keyboard())


def _users_text(bot_config: BotConfig, store: Optional[UserStore]) -> str:
    """Полный список тех, у кого есть доступ к боту."""
    added = store.all() if store else []
    lines = ["👥 <b>Кто имеет доступ к боту</b>", "", "<b>Администраторы:</b>"]
    lines.extend(f"• <code>{uid}</code>" for uid in sorted(bot_config.admin_ids) or ["• —"])
    lines.append("")
    lines.append("<b>Из conf.ini (ALLOWED_USER_IDS):</b>")
    lines.extend(f"• <code>{uid}</code>" for uid in sorted(bot_config.allowed_user_ids) or ["• —"])
    lines.append("")
    lines.append("<b>Добавлены через бота:</b>")
    if added:
        for user in added:
            lines.append(f"• <code>{user.user_id}</code> — {html.escape(user.title())}")
    else:
        lines.append("• —")
    return "\n".join(lines)


@router.message(Command("users"))
async def cmd_users(
    message: Message,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    await message.answer(_users_text(bot_config, user_store), parse_mode="HTML")


@router.callback_query(F.data == "adm:list")
async def cb_users_list(callback: CallbackQuery, bot_config: BotConfig,
                        user_store: Optional[UserStore] = None) -> None:
    if not await _require_admin_callback(callback, bot_config):
        return
    await callback.answer()
    await callback.message.answer(_users_text(bot_config, user_store), parse_mode="HTML")


def _parse_user_ids(raw: str) -> tuple[list[int], str]:
    """Разбирает «123456789 @vasya» в (список id, username/комментарий)."""
    raw = (raw or "").replace(",", " ").replace(";", " ").split()
    ids: list[int] = []
    note_parts: list[str] = []
    for token in raw:
        cleaned = token.strip().lstrip("@")
        if cleaned.lstrip("-").isdigit():
            ids.append(int(cleaned))
        else:
            note_parts.append(token.lstrip("@"))
    return ids, " ".join(note_parts)


async def _add_user(
    bot: Bot, user_id: int, *, note: str, added_by: int, store: Optional[UserStore]
) -> str:
    """Добавляет пользователя и пытается сообщить ему о доступе."""
    if store is None:
        return "❌ Хранилище пользователей недоступно."
    created, user = store.add(user_id, username=note, added_by=added_by)
    if created:
        try:
            await bot.send_message(
                user_id,
                f"{BOT_TITLE}\n\n✅ Вам открыт доступ к боту. Запустите парсинг командой /parse.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(False),
            )
        except Exception as exc:
            logger.info("Не удалось уведомить пользователя %s о доступе: %s", user_id, exc)
    title = html.escape(user.title())
    if created:
        return f"✅ Пользователь <code>{user_id}</code> ({title}) добавлен."
    return f"ℹ️ Пользователь <code>{user_id}</code> уже был в списке — данные обновлены."


async def _add_users_from_text(message: Message, raw: str, bot_config: BotConfig,
                               store: Optional[UserStore], state: FSMContext) -> None:
    ids, note = _parse_user_ids(raw)
    if not ids:
        await message.answer(
            "Не вижу id. Пришлите числовой id пользователя, например:\n"
            "<code>123456789</code> или <code>123456789, 987654321</code>",
            parse_mode="HTML",
        )
        return
    if len(ids) > 50:
        await message.answer("Слишком много id за раз (максимум 50).")
        return

    results = []
    for user_id in ids:
        if user_id in (bot_config.admin_ids | bot_config.allowed_user_ids):
            results.append(f"ℹ️ <code>{user_id}</code> уже в whitelist из conf.ini.")
            continue
        results.append(await _add_user(message.bot, user_id, note=note,
                                       added_by=message.from_user.id, store=store))
    await state.clear()
    await message.answer(
        "\n".join(results) + "\n\n" + _access_panel_text(store, bot_config),
        parse_mode="HTML",
        reply_markup=_access_panel_keyboard(),
    )


@router.message(Command("adduser"))
async def cmd_adduser(
    message: Message,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    raw = message.text.partition(" ")[2].strip()
    if not raw:
        await state.set_state(AdminState.ADD_USER)
        await message.answer(
            "➕ <b>Добавление пользователя</b>\n\n"
            "Пришлите id пользователя (можно несколько через запятую).\n"
            "Необязательно: рядом можно указать @username для пометки.\n\n"
            "<i>Пример: 123456789 @vasya</i>",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard("adm:panel"),
        )
        return
    await _add_users_from_text(message, raw, bot_config, user_store, state)


@router.callback_query(F.data == "adm:add")
async def cb_admin_add(
    callback: CallbackQuery,
    state: FSMContext,
    bot_config: BotConfig,
) -> None:
    if not await _require_admin_callback(callback, bot_config):
        return
    await state.set_state(AdminState.ADD_USER)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Добавление пользователя</b>\n\n"
        "Пришлите id пользователя (можно несколько через запятую).\n"
        "Необязательно: рядом можно указать @username для пометки.\n\n"
        "<i>Пример: 123456789 @vasya</i>",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard("adm:panel"),
    )


@router.message(AdminState.ADD_USER)
async def process_admin_add(
    message: Message,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    await _add_users_from_text(message, message.text or "", bot_config, user_store, state)


@router.message(Command("deluser"))
async def cmd_deluser(
    message: Message,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    raw = message.text.partition(" ")[2].strip()
    if not raw:
        await state.set_state(AdminState.DEL_USER)
        await message.answer(
            "➖ <b>Удаление пользователя</b>\n\nПришлите id, которому нужно закрыть доступ:",
            parse_mode="HTML",
            reply_markup=_cancel_keyboard("adm:panel"),
        )
        return
    await _remove_users_from_text(message, raw, bot_config, user_store, state)


@router.callback_query(F.data == "adm:del")
async def cb_admin_del(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    if not await _require_admin_callback(callback, bot_config):
        return
    await state.set_state(AdminState.DEL_USER)
    await callback.answer()
    await callback.message.answer(
        "➖ <b>Удаление пользователя</b>\n\nПришлите id, которому нужно закрыть доступ:",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard("adm:panel"),
    )


async def _remove_users_from_text(
    message: Message, raw: str, bot_config: BotConfig,
    store: Optional[UserStore], state: FSMContext,
) -> None:
    ids, _note = _parse_user_ids(raw)
    if not ids:
        await message.answer("Не вижу числовой id. Пример: <code>123456789</code>",
                             parse_mode="HTML")
        return
    results = []
    for user_id in ids:
        if user_id in bot_config.admin_ids:
            results.append(f"🚫 <code>{user_id}</code> — администратор, удалить нельзя.")
            continue
        if user_id in bot_config.allowed_user_ids:
            results.append(
                f"ℹ️ <code>{user_id}</code> задан в conf.ini (ALLOWED_USER_IDS) — "
                "уберите его там, чтобы закрыть доступ."
            )
            continue
        if store is not None and store.remove(user_id):
            results.append(f"✅ Доступ для <code>{user_id}</code> закрыт.")
        else:
            results.append(f"ℹ️ <code>{user_id}</code> не найден в списке добавленных.")
    await state.clear()
    await message.answer(
        "\n".join(results) + "\n\n" + _access_panel_text(store, bot_config),
        parse_mode="HTML",
        reply_markup=_access_panel_keyboard(),
    )


@router.message(AdminState.DEL_USER)
async def process_admin_del(
    message: Message,
    state: FSMContext,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    if not await _require_admin_message(message, bot_config):
        return
    await _remove_users_from_text(message, message.text or "", bot_config, user_store, state)


@router.callback_query(F.data.startswith("adm:add:"))
async def cb_admin_quick_add(
    callback: CallbackQuery,
    bot_config: BotConfig,
    user_store: Optional[UserStore] = None,
) -> None:
    """Быстрое добавление из уведомления о запросе доступа."""
    if not await _require_admin_callback(callback, bot_config):
        return
    try:
        user_id = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный id", show_alert=True)
        return

    created = user_store is not None and user_id not in user_store
    await _add_user(callback.bot, user_id, note="",
                    added_by=callback.from_user.id, store=user_store)
    await callback.answer("Доступ открыт" if created else "Уже в списке")
    try:
        await callback.message.edit_text(
            f"🔔 Запрос доступа от <code>{user_id}</code> обработан: "
            + ("доступ открыт ✅" if created else "пользователь уже был в списке ℹ️"),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("adm:skip:"))
async def cb_admin_quick_skip(callback: CallbackQuery, bot_config: BotConfig) -> None:
    if not await _require_admin_callback(callback, bot_config):
        return
    await callback.answer("Отклонено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


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
        await message.answer("Использование: /warmup &lt;канал&gt;", parse_mode="HTML")
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
    task.current_channel = channel_str
    status_msg = await bot.send_message(
        chat_id,
        f"Начинаем прогрев кэша для {html.escape(channel_str)}...",
        parse_mode="HTML",
    )
    try:
        resolved = await resolve_channel(client, channel_str)
        title = channel_title(resolved)
        count = await warmup_participants(client, resolved)
        await status_msg.edit_text(
            f"✅ Прогрев кэша для <b>{html.escape(title)}</b> завершён. "
            f"Закэшировано: {count} участников.",
            parse_mode="HTML",
        )
    except asyncio.CancelledError:
        await status_msg.edit_text("🛑 Прогрев кэша отменён.")
        raise
    except Exception as exc:
        await status_msg.edit_text(
            f"❌ Ошибка прогрева кэша: {html.escape(str(exc))}",
            parse_mode="HTML",
        )


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
            _channels_step_text(),
            parse_mode="HTML",
            reply_markup=_cancel_keyboard(),
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
            entity = await resolve_channel(telethon_client, item)
            title = channel_title(entity)
            last_id = await get_last_message_id(telethon_client, entity)
            resolved_channels.append({
                "raw": item_str,
                "title": title,
                "found": True,
                "last_id": last_id,
            })
        except Exception as exc:
            logger.warning("Не удалось разрешить канал %r: %s", item, exc)
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
        _channels_step_text(),
        parse_mode="HTML",
        reply_markup=_cancel_keyboard(),
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


def _channels_step_text() -> str:
    """Текст шага 1: приём списка каналов."""
    return (
        "📋 <b>Шаг 1 из 3 · Каналы</b>\n\n"
        "Пришлите каналы для парсинга — один или несколько, через запятую, "
        "точку с запятой или с новой строки.\n"
        "<i>Например: @some_channel, t.me/another, my_channel</i>"
    )


async def _show_scope_step(message: Message, state: FSMContext) -> None:
    await state.set_state(WizardState.SCOPE)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Все посты", callback_data="scope_all"),
            InlineKeyboardButton(text="Последние N", callback_data="scope_n"),
        ],
        [
            InlineKeyboardButton(text="По годам", callback_data="scope_years"),
        ],
        [
            InlineKeyboardButton(text="⏹ Отмена", callback_data="wiz:cancel"),
        ],
    ])
    text = (
        "📊 <b>Шаг 2 из 3 · Диапазон постов</b>\n\n"
        "Сколько последних постов проверять в каждом канале?\n\n"
        "• <b>Все посты</b> — от последнего до самого первого\n"
        "• <b>Последние N</b> — только свежие посты\n"
        "• <b>По годам</b> — выбранные годы целиком"
    )
    if isinstance(message, Message) and message.from_user and message.from_user.is_bot:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(WizardState.SCOPE, F.data == "scope_all")
async def cb_scope_all(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.update_data(
        scope_type="all",
        limit=UNLIMITED_LIMIT,
        unlimited=True,
        start_date=None,
        end_date=None,
        scope_desc="Все посты",
    )
    await callback.answer()
    await _show_mode_step(callback.message, state, bot_config)


@router.callback_query(WizardState.SCOPE, F.data == "scope_n")
async def cb_scope_n(callback: CallbackQuery, state: FSMContext, bot_config: BotConfig) -> None:
    _reset_user_idle_timer(callback.from_user.id, state, callback.bot, bot_config.wizard_idle_timeout)
    await state.set_state(WizardState.SCOPE_N)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="scope_back"),
            InlineKeyboardButton(text="⏹ Отмена", callback_data="wiz:cancel"),
        ]
    ])
    await callback.message.edit_text(
        "🔢 <b>Сколько последних постов проверить?</b>\n\n"
        "Пришлите целое число от 1 до 1 000 000 (например: 500).",
        parse_mode="HTML",
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
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="scope_back"),
                InlineKeyboardButton(text="⏹ Отмена", callback_data="wiz:cancel"),
            ]
        ])
        await message.answer(
            "Нужно целое число от 1 до 1 000 000. Попробуйте ещё раз (например: 500).",
            reply_markup=kb,
        )
        return

    await state.update_data(
        scope_type="n",
        limit=n,
        unlimited=False,
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
        limit=UNLIMITED_LIMIT,
        unlimited=True,
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
            InlineKeyboardButton(text="⏹ Отмена", callback_data="wiz:cancel"),
        ]
    ])
    text = (
        "🎯 <b>Шаг 3 из 3 · Кого показывать</b>\n\n"
        "В отчёт попадут все, кто отправлял звёзды, либо только крупные донатеры:"
    )
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
    limit = int(wizard_data.get("limit", UNLIMITED_LIMIT))
    unlimited = bool(wizard_data.get("unlimited"))
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

        bar = _progress_bar(scanned, tot or 0)
        bar_line = f"{bar}\n" if bar else ""
        tot_str = f" из {tot}" if tot else ""
        text = (
            f"🔄 <b>Парсинг канала:</b> {html.escape(ch)}\n"
            f"{bar_line}"
            f"Обработано постов: <b>{scanned}</b>{tot_str} (со звёздами: {with_stars}){flood_str}\n"
            f"<i>Отмена — /cancel</i>"
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

    # Сообщаем, что задача реально взята в работу: иначе сообщение может
    # оставаться «в очереди», пока не наберётся первая пачка просмотренных постов.
    try:
        await bot.edit_message_text(
            "🔄 <b>Парсинг запущен.</b>\n<i>Для отмены используйте /cancel</i>",
            chat_id=chat_id,
            message_id=status_msg_id,
            parse_mode="HTML",
        )
    except Exception:
        pass

    for ch_item in channels_raw:
        if task.cancelled_by_user:
            interrupted = True
            channel_outcomes[str(ch_item)] = "отменено"
            break

        task.current_channel = str(ch_item)
        try:
            entity = await resolve_channel(client, ch_item)
            title = channel_title(entity)
        except Exception as exc:
            logger.error("Не удалось разрешить канал %s: %s", ch_item, exc)
            channel_outcomes[str(ch_item)] = f"ошибка: {exc}"
            continue

        ch_stats = ParseStats()
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
                unlimited=unlimited,
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
        unlimited=unlimited,
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
    unlimited: bool = False,
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
        records=all_records,
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
        unlimited=unlimited,
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
