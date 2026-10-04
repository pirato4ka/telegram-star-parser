"""Рендеринг таблиц в текст, разбивка на страницы, экранирование HTML."""

from __future__ import annotations

import html
from typing import Sequence

from bot.aggregate import DonorAggregate
from models import ParseStats

MAX_MESSAGE_LENGTH = 3500
MAX_ROWS_PER_PAGE = 35


def format_channel_header(channels: Sequence[str]) -> str:
    """Форматирует заголовок каналов: сокращает если длинный."""
    if not channels:
        return ""
    if len(channels) <= 3:
        return ", ".join(f"<b>{html.escape(c)}</b>" for c in channels)
    first_three = ", ".join(f"<b>{html.escape(c)}</b>" for c in channels[:3])
    return f"{len(channels)} каналов: {first_three}, …"


def format_donors_table(
    donors: list[DonorAggregate],
    anon_donor: DonorAggregate | None,
    channel_names: list[str],
    threshold: int,
    only_above_threshold: bool,
    stats_by_channel: dict[str, ParseStats] | None = None,
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
    mode_text = f"(>{threshold} звёзд)" if only_above_threshold else "(все)"
    title = f"<b>Топ донатеров {mode_text}, канал(ы):</b> {header_channels}\n\n"

    # Шапка таблицы
    # #  username          id           звёзд   постов
    table_header = (
        f"{'#':<3} {'username':<16} {'id':<12} {'звёзд':<7} {'постов':<6}\n"
        f"{'-'*48}\n"
    )

    # Формируем строки для каждого донатера
    row_lines: list[str] = []
    for d in donors:
        u_name = d.reactor_username or ""
        if len(u_name) > 15:
            u_name = u_name[:14] + "…"
        u_id = str(d.reactor_id) if d.reactor_id is not None else ""
        if len(u_id) > 11:
            u_id = u_id[:10] + "…"
        line = f"{d.rank:<3} {u_name:<16} {u_id:<12} {d.stars_total:<7} {d.posts_count:<6}"
        row_lines.append(line)

    if anon_donor is not None:
        anon_line = f"    {'(анонимы)':<16} {'':<12} {anon_donor.stars_total:<7} {anon_donor.posts_count:<6}"
        row_lines.append(anon_line)

    # Итоги
    summary_lines = [
        f"\n<b>Всего:</b> {total_named_count} донатера, {total_stars} звёзд (включая анонимов)",
    ]

    # Метрики полноты
    if stats_by_channel:
        total_scanned = sum(s.scanned for s in stats_by_channel.values())
        total_requested = sum(s.requested for s in stats_by_channel.values())
        total_errors = sum(s.errors for s in stats_by_channel.values())
        total_unparsed = sum(s.unparsed for s in stats_by_channel.values())
        total_not_found = sum(s.not_found for s in stats_by_channel.values())
        total_failed = total_errors + total_unparsed
        anon_count = anon_donor.posts_count if anon_donor else 0

        summary_lines.append(
            f"<b>Обработано:</b> {total_scanned} из {total_requested}. "
            f"<b>Не удалось спарсить:</b> {total_failed}."
        )
        summary_lines.append(
            f"<b>Не расшифровано:</b> {total_not_found}. "
            f"<b>Анонимов:</b> {anon_count}."
        )
        if total_failed > 0:
            summary_lines.append("⚠️ <b>Внимание:</b> результат неполный (подробности в parser.log).")

    summary_block = "\n".join(summary_lines)

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
        header = f"{title[:-2]}{page_indicator}\n\n" if idx == 1 else f"<b>Топ донатеров</b>{page_indicator}\n\n"

        code_block = "<pre>\n" + html.escape(table_header + "\n".join(page_rows)) + "\n</pre>"
        msg = header + code_block

        if idx == total_pages:
            msg += "\n" + summary_block

        messages.append(msg)

    return messages


def format_queue_summary(
    stats_by_channel: dict[str, ParseStats],
    channel_outcomes: dict[str, str],
) -> str:
    """Формирует текстовую сводку по очереди каналов (аналог main.print_queue_summary)."""
    lines = ["<b>Сводка по каналам:</b>"]
    for ch, outcome in channel_outcomes.items():
        st = stats_by_channel.get(ch)
        scanned_str = f"обработано: {st.scanned}" if st else "не начат"
        lines.append(f"• <b>{html.escape(ch)}</b> — {html.escape(outcome)} ({scanned_str})")
    return "\n".join(lines)
