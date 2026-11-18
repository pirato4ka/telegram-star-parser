"""Тесты поиска донатера по сохранённым Excel-отчётам (bot.donor_search)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl
import pytest

from bot.config_bot import BotConfig
from bot.donor_search import (
    CHANNELS_SHEET_NAME,
    DONATIONS_SHEET_NAME,
    INDEX_DB_NAME,
    REPORT_COLUMNS,
    ChannelStat,
    DonorRow,
    DonorSearchResult,
    build_donor_report,
    channel_from_link,
    format_search_summary,
    parse_donor_query,
    search_donor,
)

DONATION_HEADERS = [
    "reactor_id", "reactor_username", "stars_count",
    "post_link", "reactor_type", "original_message_id",
]


def _write_current_report(path: Path) -> None:
    """Файл текущего формата: лист «Донаты» (6 колонок) + служебный «Инфо»."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Донаты"
    sheet.append(DONATION_HEADERS)
    sheet.append([111, "alice", 50, "https://t.me/chan1/10", "user", 10])
    sheet.append([222, "bob", 30, "https://t.me/chan2/20", "user", 20])
    sheet.append([111, "alice", 70, "https://t.me/chan1/11", "user", 11])
    # Анонимный донат: id нет — в индексе он есть, но по id/username не найдётся
    sheet.append([None, "", 5, "https://t.me/chan1/12", "anonymous", 12])

    workbook.create_sheet("Инфо").append(["Всего донатов", 4])
    workbook.save(path)


def _write_legacy_report(path: Path) -> None:
    """Старый формат CLI-версии: лист «Сводка» с русскими шапками."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Сводка"
    sheet.append(["id", "username", "звёзд всего", "постов", "каналы"])
    sheet.append([111, "@alice", 90, 2, "legacy_chan"])
    sheet.append([333, "carol", 10, 1, "legacy_chan"])
    workbook.save(path)


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    _write_current_report(out / "2026-01-01_chan1_donors.xlsx")
    _write_legacy_report(out / "old_export.xlsx")
    return out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("111222333", ("id", 111222333)),
        ("  id987654321 ", ("id", 987654321)),
        ("@Alice", ("username", "alice")),
        ("alice", ("username", "alice")),
        ("https://t.me/alice/", ("username", "alice")),
    ],
)
def test_parse_donor_query_ok(raw, expected):
    assert parse_donor_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "@", "ab", "0", "-5", "плохой ник!"])
def test_parse_donor_query_errors(raw):
    with pytest.raises(ValueError):
        parse_donor_query(raw)


def test_channel_from_link():
    assert channel_from_link("https://t.me/chan1/10") == "@chan1"
    assert channel_from_link("https://t.me/s/chan2/20") == "@chan2"
    assert channel_from_link("t.me/chan3/30") == "@chan3"
    assert channel_from_link("https://t.me/chan1") == ""
    assert channel_from_link(None) == ""


def test_search_by_id_reads_current_and_legacy_formats(output_dir: Path):
    result = search_donor("111", output_dir)

    assert result.query_kind == "id"
    assert result.query_value == "111"
    assert result.files_scanned == 2
    assert result.total_rows == 3
    assert result.total_stars == 50 + 70 + 90
    assert result.files_with_hits == 2

    by_channel = {stat.channel: stat for stat in result.channels}
    assert by_channel["@chan1"].donations == 2
    assert by_channel["@chan1"].stars == 120
    assert by_channel["legacy_chan"].donations == 1
    assert by_channel["legacy_chan"].stars == 90
    # сортировка: сначала канал с большим числом донатов
    assert result.channels[0].channel == "@chan1"


def test_search_by_username_is_case_insensitive(output_dir: Path):
    result = search_donor("@ALICE", output_dir)

    assert result.query_kind == "username"
    assert result.total_rows == 3
    assert {row.reactor_username for row in result.rows} == {"alice"}
    assert result.report_name == "alice"


def test_search_unknown_donor_is_empty(output_dir: Path):
    result = search_donor("999999", output_dir)

    assert result.rows == []
    assert result.channels == []
    assert "не найден" in format_search_summary(result)
    assert build_donor_report(result, output_dir) is None


def test_search_uses_index_and_picks_up_changes(output_dir: Path):
    assert search_donor("222", output_dir).total_rows == 1
    assert (output_dir / INDEX_DB_NAME).exists()

    # Повторный поиск по неизменённым файлам даёт тот же результат
    assert search_donor("222", output_dir).total_rows == 1

    # Дописываем новый донат в текущий отчёт — индекс должен обновиться
    path = output_dir / "2026-01-01_chan1_donors.xlsx"
    workbook = openpyxl.load_workbook(path)
    workbook["Донаты"].append([222, "bob", 15, "https://t.me/chan3/33", "user", 33])
    workbook.save(path)

    result = search_donor("222", output_dir)
    assert result.total_rows == 2
    assert {stat.channel for stat in result.channels} == {"@chan2", "@chan3"}


def test_build_donor_report_creates_three_sheets(output_dir: Path):
    result = search_donor("111", output_dir)
    report_path = build_donor_report(result, output_dir)

    assert report_path is not None and report_path.exists()
    assert report_path.name == "alice_парсинг.xlsx"

    workbook = openpyxl.load_workbook(report_path)
    assert workbook.sheetnames == [DONATIONS_SHEET_NAME, CHANNELS_SHEET_NAME, "Инфо"]

    rows = list(workbook[DONATIONS_SHEET_NAME].iter_rows(values_only=True))
    assert list(rows[0]) == list(REPORT_COLUMNS)
    assert len(rows) == 1 + 3
    # stars_count записан числом (внутри канала строки отсортированы по убыванию звёзд)
    stars = [row[2] for row in rows[1:4]]
    assert all(isinstance(value, int) for value in stars)
    assert sorted(stars, reverse=True) == [90, 70, 50]

    channel_rows = list(workbook[CHANNELS_SHEET_NAME].iter_rows(values_only=True))
    assert list(channel_rows[0]) == ["канал", "донатов", "звёзд", "файлов"]


class _FakeStatusMessage:
    def __init__(self) -> None:
        self.texts = []

    async def edit_text(self, text, **kwargs):
        self.texts.append(text)
        return self


class _FakeMessage:
    """Минимальная заглушка aiogram.Message для проверки хендлера."""

    def __init__(self) -> None:
        self.answers = []
        self.documents = []
        self.status = None

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.status = _FakeStatusMessage()
        return self.status

    async def answer_document(self, document, caption=None, **kwargs):
        self.documents.append(document)


def test_handler_sends_report(output_dir: Path, monkeypatch):
    from bot.handlers_bot import _run_donor_search

    monkeypatch.chdir(output_dir.parent)
    message = _FakeMessage()

    asyncio.run(_run_donor_search(message, "@alice", BotConfig(token="test")))

    assert message.status is not None
    assert "Донатер найден" in message.status.texts[-1]
    assert len(message.documents) == 1
    assert Path(message.documents[0].path).name == "alice_парсинг.xlsx"


def test_search_report_is_not_reindexed(output_dir: Path):
    """Отчёт самого поиска (`*_парсинг.xlsx`) в индекс не попадает — дублей нет."""
    first = search_donor("111", output_dir)
    build_donor_report(first, output_dir)

    report_path = output_dir / "alice_парсинг.xlsx"
    assert report_path.exists()

    second = search_donor("111", output_dir)
    assert len(second.rows) == len(first.rows)
    assert second.total_stars == first.total_stars


def test_summary_escapes_html():
    """Сводка уходит с parse_mode=HTML — запрос и канал должны быть экранированы."""
    row = DonorRow(
        reactor_id=1, reactor_username="a", stars_count=5,
        post_link="https://t.me/c/1", reactor_type="user", original_message_id=1,
        current_channel="Канал & <b>", source="f.xlsx / Донаты",
    )
    result = DonorSearchResult(query="a<&", query_kind="username", query_value="a")
    result.rows = [row]
    result.channels = [ChannelStat(channel="Канал & <b>", donations=1, stars=5,
                                   files={"f.xlsx"})]

    text = format_search_summary(result)
    assert "a&lt;&amp;" in text
    assert "Канал &amp; &lt;b&gt;" in text
    assert "<b>Донатер найден</b>" in text


def test_handler_reports_invalid_query(output_dir: Path, monkeypatch):
    from bot.handlers_bot import _run_donor_search

    monkeypatch.chdir(output_dir.parent)
    message = _FakeMessage()

    asyncio.run(_run_donor_search(message, "ab", BotConfig(token="test")))

    assert message.answers and message.answers[0].startswith("⚠️")
    assert message.documents == []


