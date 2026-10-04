"""Рендеринг таблиц в текст, разбивка на страницы, экранирование HTML."""

from __future__ import annotations

import html
from typing import Sequence

from bot.aggregate import DonorAggregate
from models import ParseStats, USERNAME_MISSING

MAX_MESSAGE_LENGTH = 3500
MAX_ROWS_PER_PAGE = 35

# Разделитель в «моноширинной» таблице; столбцы подобраны под ширину телефона.
_DIVIDER = "-" * 46


def plural(number: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение: plural(1, ("донатер", "донатера", "донатеров"))."""
    value = abs(int(number)) % 100
    if 11 <= value <= 19:
        return forms[2]
    value %= 10
    if value == 1:
        return forms[0]
    if 2 <= value <= 4:
        return forms[1]
    return forms[2]


def format_channel_header(channels: Sequence[str]) -> str:
    """Форматирует заголовок каналов: сокращает если длинный."""
    if not channels:
        return ""
    if len(channels) <= 3:
        return ", ".join(f"<b>{html.escape(c)}</b>" for c in channels)
    first_three = ", ".join(f"<b>{html.escape(c)}</b>" for c in channels[:3])
    return f"{len(channels)} каналов: {first_three}, …"


def _truncate(value: str, width: int) -> str:
    """Обрезает строку до ширины колонки, добавляя многоточие."""
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "…"


def format_donors_table(
    donors: list[DonorAggregate],
    anon_donor: DonorAggregate | None,
    channel_names: list[str],
    threshold: int,
    only_above_threshold: bool,
    stats_by_channel: dict[str, ParseStats] | None = None,
    unlimited: bool = False,
) -> list[str]:
    """Форматирует агрегированные данные донатеров в текстовые блоки сообщений (HTML).

    При превышении лимита строк/длины разбивает на страницы с пометкой (стр. N/M)
    и сквозной нумерацией строк.
    """
    total_named_count = len(donors)
    total_stars = sum(d.stars_total for d in donors)
    if anon_donor is not None:
        total_stars += anon_donor.stars_total

    header_channels = format_channel_header(channel_names)
    mode_text = f"(больше {threshold} звёзд)" if only_above_threshold else "(все)"
    title = f"<b>🏆 Топ донатеров {mode_text}</b>\n<b>Каналы:</b> {header_channels}\n\n"

    # Шапка таблицы: #  username         id            звёзд   постов
    table_header = (
        f"{'#':<3} {'username':<16} {'id':<13} {'звёзд':<7} {'постов':<6}\n"
        f"{_DIVIDER}\n"
    )

    # Формируем строки для каждого донатера
    row_lines: list[str] = []
    for d in donors:
        username = _truncate(str(d.reactor_username or USERNAME_MISSING), 15)
        user_id = _truncate(str(d.reactor_id) if d.reactor_id is not None else "", 12)
        line = f"{d.rank:<3} {username:<16} {user_id:<13} {d.stars_total:<7} {d.posts_count:<6}"
        row_lines.append(line)

    if anon_donor is not None:
        anon_line = (
            f"{'':<3} {'(анонимы)':<16} {'':<13} "
            f"{anon_donor.stars_total:<7} {anon_donor.posts_count:<6}"
        )
        row_lines.append(anon_line)

    summary_block = _format_summary(
        total_named_count=total_named_count,
        total_stars=total_stars,
        anon_donor=anon_donor,
        stats_by_channel=stats_by_channel,
        unlimited=unlimited,
    )

    if not row_lines:
        empty_msg = (
            f"{title}"
            f"<i>Нет донатеров, удовлетворяющих условиям.</i>\n\n"
            f"{summary_block}"
        )
        return [empty_msg]

    # Разбивка на страницы
    pages: list[list[str]] = []
    current_page: list[str] = []
    current_len = 0

    for line in row_lines:
        line_len = len(line) + 1
        if len(current_page) >= MAX_ROWS_PER_PAGE or (current_len + line_len > 2500):
            pages.append(current_page)
            current_page = [line]
            current_len = line_len
        else:
            current_page.append(line)
            current_len += line_len

    if current_page:
        pages.append(current_page)

    total_pages = len(pages)
    messages: list[str] = []

    for idx, page_rows in enumerate(pages, start=1):
        page_indicator = f" <i>(стр. {idx}/{total_pages})</i>" if total_pages > 1 else ""
        header = f"{title}{page_indicator}\n" if idx == 1 else f"<b>🏆 Топ донатеров</b>{page_indicator}\n\n"

        code_block = "<pre>\n" + html.escape(table_header + "\n".join(page_rows)) + "\n</pre>"
        msg = header + code_block

        if idx == total_pages:
            msg += "\n" + summary_block

        messages.append(msg)

    return messages


def _format_summary(
    total_named_count: int,
    total_stars: int,
    anon_donor: DonorAggregate | None,
    stats_by_channel: dict[str, ParseStats] | None,
    unlimited: bool = False,
) -> str:
    """Блок итогов: донатеры, звёзды, полнота парсинга.

    Число постов в канале заранее неизвестно, поэтому «из N» показывается
    только тогда, когда N действительно известно (а не технический лимит
    в 1 000 000, из-за которого сводка выглядела сломанной).
    """
    donor_word = plural(total_named_count, ("донатер", "донатера", "донатеров"))
    star_word = plural(total_stars, ("звезда", "звезды", "звёзд"))
    lines = [
        f"<b>Всего:</b> {total_named_count} {donor_word}, "
        f"{total_stars} {star_word} (включая анонимов)",
    ]

    if stats_by_channel:
        total_scanned = sum(s.scanned for s in stats_by_channel.values())
        total_errors = sum(s.errors for s in stats_by_channel.values())
        total_unparsed = sum(s.unparsed for s in stats_by_channel.values())
        total_not_found = sum(s.not_found for s in stats_by_channel.values())
        total_with_stars = sum(s.with_stars for s in stats_by_channel.values())
        total_failed = total_errors + total_unparsed
        anon_count = anon_donor.entries if anon_donor else 0

        requested_values = [s.requested for s in stats_by_channel.values()]
        # План показываем только если он известен по всем каналам и не является
        # техническим лимитом «всех постов» (иначе в сводке висело
        # «Обработано: 6957 из 1000000»).
        known_total = (
            sum(v for v in requested_values if v)
            if not unlimited and requested_values and all(v for v in requested_values)
            else 0
        )
        processed = str(total_scanned)
        if known_total and known_total != total_scanned:
            processed = f"{total_scanned} из {known_total}"

        lines.append(f"<b>Обработано:</b> {processed}. <b>Не удалось спарсить:</b> {total_failed}.")
        lines.append(
            f"<b>Не расшифровано:</b> {total_not_found}. "
            f"<b>Анонимных донатов:</b> {anon_count}."
        )
        lines.append(f"<b>Постов со звёздами:</b> {total_with_stars}.")
        if total_failed > 0:
            lines.append("⚠️ <b>Внимание:</b> результат неполный (подробности в parser.log).")

    return "\n".join(lines)


def format_queue_summary(
    stats_by_channel: dict[str, ParseStats],
    channel_outcomes: dict[str, str],
) -> str:
    """Формирует текстовую сводку по очереди каналов (аналог main.print_queue_summary)."""
    lines = ["<b>📡 Сводка по каналам:</b>"]
    for ch, outcome in channel_outcomes.items():
        st = stats_by_channel.get(ch)
        scanned_str = f"обработано: {st.scanned}" if st else "не начат"
        if st and st.with_stars:
            scanned_str += f", со звёздами: {st.with_stars}"
        lines.append(f"• <b>{html.escape(ch)}</b> — {html.escape(outcome)} ({scanned_str})")
    return "\n".join(lines)
