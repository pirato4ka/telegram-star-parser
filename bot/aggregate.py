"""Модели и агрегация записей StarRecord -> DonorAggregate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from models import (
    ANONYMOUS_LABEL,
    NOT_FOUND,
    REACTOR_ANONYMOUS,
    USERNAME_MISSING,
    StarRecord,
    looks_like_username,
)


@dataclass(slots=True)
class DonorAggregate:
    """Агрегированные данные по одному донатеру.

    `reactor_username` — либо настоящий username, либо «отсутствует»
    (`models.USERNAME_MISSING`), если у донатера его нет.
    """

    rank: int = 0
    reactor_username: str = ""
    reactor_id: int | None = None
    reactor_type: str = "user"
    stars_total: int = 0
    posts_count: int = 0
    channels: list[str] = field(default_factory=list)
    # Сколько записей (донатов) пришло от этого донатера.
    entries: int = 0
    # Имя/фамилия или `not_found`: нужно только для логов и отладки.
    display_name: str = ""


def aggregate_star_records(
    records: Sequence[StarRecord],
    threshold: int = 0,
    only_above_threshold: bool = False,
) -> tuple[list[DonorAggregate], DonorAggregate | None]:
    """Агрегирует StarRecord в список DonorAggregate.

    Возвращает (donors, anonymous_summary), где:
    - donors: именованные донатеры + not_found, отсортированные по правилам §8
    - anonymous_summary: строка (анонимы) или None, если анонимов не было.

    Правила сортировки (§8):
    1. Именованные донатеры (user/channel) - stars_total ↓, posts_count ↓, username/id ↑
    2. Блок not_found - stars_total ↓, posts_count ↓, username/id ↑
    3. Строка (анонимы) - всегда последней, не фильтруется порогом.
    """
    # Собираем данные: reactor_id -> stats
    # Для анонимов отдельная агрегация
    # Для каждого донатера считаем:
    # stars_total = sum(stars_count)
    # posts_count = len(unique (current_channel, current_message_id))
    # channels = sorted list of unique current_channel

    donors_by_id: dict[int, dict] = {}
    anon_stars = 0
    anon_entries = 0
    anon_posts: set[tuple[str, int]] = set()
    anon_channels: set[str] = set()

    for r in records:
        if r.reactor_type == REACTOR_ANONYMOUS or r.reactor_id is None:
            anon_stars += r.stars_count
            anon_entries += 1
            anon_posts.add((r.current_channel, r.current_message_id))
            if r.current_channel:
                anon_channels.add(r.current_channel)
            continue

        rid = r.reactor_id
        if rid not in donors_by_id:
            uname = str(r.reactor_username or "")
            has_username = bool(r.reactor_has_username or looks_like_username(uname))
            donors_by_id[rid] = {
                "id": rid,
                "username": uname if has_username else USERNAME_MISSING,
                "display_name": uname or f"id{rid}",
                "type": r.reactor_type,
                "stars": 0,
                "posts": set(),
                "channels": set(),
                "entries": 0,
                "has_username": has_username,
                "is_not_found": (uname == NOT_FOUND),
            }

        entry = donors_by_id[rid]
        entry["stars"] += r.stars_count
        entry["entries"] += 1
        entry["posts"].add((r.current_channel, r.current_message_id))
        if r.current_channel:
            entry["channels"].add(r.current_channel)
        # Если username не был известен, а в другой записи он нашёлся — берём его.
        if not entry["has_username"]:
            candidate = str(r.reactor_username or "")
            if r.reactor_has_username or looks_like_username(candidate):
                entry["username"] = candidate
                entry["has_username"] = True
            if entry["is_not_found"] and candidate != NOT_FOUND:
                entry["is_not_found"] = False
                entry["display_name"] = candidate or f"id{rid}"
                entry["type"] = r.reactor_type

    named_donors: list[DonorAggregate] = []
    not_found_donors: list[DonorAggregate] = []

    for rid, data in donors_by_id.items():
        stars = data["stars"]
        # Фильтр порога: только > threshold, если включено
        if only_above_threshold and stars <= threshold:
            continue

        donor = DonorAggregate(
            reactor_username=data["username"],
            reactor_id=data["id"],
            reactor_type=data["type"],
            stars_total=stars,
            posts_count=len(data["posts"]),
            channels=sorted(data["channels"]),
            entries=data["entries"],
            display_name=data["display_name"],
        )
        if data["is_not_found"]:
            not_found_donors.append(donor)
        else:
            named_donors.append(donor)

    # Сортировка:
    # 1. stars_total ↓ (-stars_total)
    # 2. posts_count ↓ (-posts_count)
    # 3. username/id ↑ (username str, id int)
    def sort_key(d: DonorAggregate):
        return (
            -d.stars_total,
            -d.posts_count,
            str(d.reactor_username).lower(),
            d.reactor_id or 0,
        )

    named_donors.sort(key=sort_key)
    not_found_donors.sort(key=sort_key)

    # Объединяем: сначала именованные, затем not_found
    combined = named_donors + not_found_donors
    for idx, d in enumerate(combined, start=1):
        d.rank = idx

    anon_aggregate: DonorAggregate | None = None
    if anon_stars > 0 or anon_posts:
        anon_aggregate = DonorAggregate(
            rank=len(combined) + 1,
            reactor_username=ANONYMOUS_LABEL,
            reactor_id=None,
            reactor_type=REACTOR_ANONYMOUS,
            stars_total=anon_stars,
            posts_count=len(anon_posts),
            channels=sorted(anon_channels),
            entries=anon_entries,
            display_name=ANONYMOUS_LABEL,
        )

    return combined, anon_aggregate
