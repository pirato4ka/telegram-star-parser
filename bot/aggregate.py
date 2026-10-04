"""Модели и агрегация записей StarRecord -> DonorAggregate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from models import NOT_FOUND, REACTOR_ANONYMOUS, StarRecord


@dataclass(slots=True)
class DonorAggregate:
    """Агрегированные данные по одному донатеру."""

    rank: int = 0
    reactor_username: str = ""
    reactor_id: int | None = None
    reactor_type: str = "user"
    stars_total: int = 0
    posts_count: int = 0
    channels: list[str] = field(default_factory=list)


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
    anon_posts: set[tuple[str, int]] = set()
    anon_channels: set[str] = set()

    for r in records:
        if r.reactor_type == REACTOR_ANONYMOUS or r.reactor_id is None:
            anon_stars += r.stars_count
            anon_posts.add((r.current_channel, r.current_message_id))
            if r.current_channel:
                anon_channels.add(r.current_channel)
            continue

        rid = r.reactor_id
        if rid not in donors_by_id:
            # определяем reactor_username и reactor_type
            # username / имя либо not_found
            r_type = r.reactor_type
            uname = r.reactor_username or ""
            if uname == NOT_FOUND:
                display_name = f"id{rid} (not_found)"
            else:
                display_name = uname or f"id{rid}"

            donors_by_id[rid] = {
                "id": rid,
                "username": display_name,
                "raw_username": uname,
                "type": r_type,
                "stars": 0,
                "posts": set(),
                "channels": set(),
                "is_not_found": (uname == NOT_FOUND),
            }

        entry = donors_by_id[rid]
        entry["stars"] += r.stars_count
        entry["posts"].add((r.current_channel, r.current_message_id))
        if r.current_channel:
            entry["channels"].add(r.current_channel)
        # Если ранее было not_found, но встретилось расшифрованное имя
        if entry["is_not_found"] and r.reactor_username and r.reactor_username != NOT_FOUND:
            entry["username"] = r.reactor_username
            entry["raw_username"] = r.reactor_username
            entry["type"] = r.reactor_type
            entry["is_not_found"] = False

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
            reactor_username="(анонимы)",
            reactor_id=None,
            reactor_type=REACTOR_ANONYMOUS,
            stars_total=anon_stars,
            posts_count=len(anon_posts),
            channels=sorted(anon_channels),
        )

    return combined, anon_aggregate
