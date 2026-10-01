"""Тесты сценария `main.run` без реального подключения к Telegram.

Проверяется оркестрация: конфиг -> «авторизация» -> канал -> парсинг -> Excel,
а также корректное завершение по Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import load_workbook  # noqa: E402
from telethon.tl import types  # noqa: E402

import main as main_module  # noqa: E402
from test_offline import (  # noqa: E402
    FakeClient, make_channel, make_message, make_paid_reactions, make_user,
)

CONFIG_TEMPLATE = """
[API]
ID_API=1
HASH_API=abc
PHONE=+79990000000
CLOUD_PASSWORD=

[DELAY]
MESSAGES_INTERVAL_MIN=0
MESSAGES_INTERVAL_MAX=0

[OUTPUT]
DIR=output
"""


class FlowClient(FakeClient):
    """Клиент с методами, которые нужны сценарию main."""

    async def get_entity(self, peer):
        if isinstance(peer, str):  # username канала из --channel
            return make_channel(1, title="Test Channel", username=peer.lstrip("@"))
        return await super().get_entity(peer)

    async def get_messages(self, entity, limit=1, **kwargs):
        return self.messages[:limit]

    async def disconnect(self):
        return None


class InterruptingClient(FlowClient):
    """Клиент, имитирующий Ctrl+C после первого сообщения."""

    async def iter_messages(self, entity, limit=None, offset_id=0, **kwargs):
        for message in self.messages:
            yield message
            raise KeyboardInterrupt


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        config=None, channel="@testchannel", count=3, output_dir=None,
        warmup=False, create_empty_file=False, log_level="ERROR",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMainFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "conf.ini").write_text(CONFIG_TEMPLATE, encoding="utf-8")
        self.original_base_dir = main_module.get_base_dir
        self.original_build_client = main_module.build_client
        self.original_authorize = main_module.authorize
        main_module.get_base_dir = lambda: self.tmp

    def tearDown(self):
        main_module.get_base_dir = self.original_base_dir
        main_module.build_client = self.original_build_client
        main_module.authorize = self.original_authorize

    def _patch_client(self, client):
        main_module.build_client = lambda config, base_dir: client
        main_module.authorize = self._fake_authorize

    @staticmethod
    async def _fake_authorize(client, config, max_attempts=3):
        return None

    def _messages(self):
        paid = make_paid_reactions([types.MessageReactor(count=4,
                                                         peer_id=types.PeerUser(42))])
        return [
            make_message(msg_id=30, text="stars", reactions=paid),
            make_message(msg_id=29, text="no stars"),
            make_message(msg_id=28, text="older"),
        ]

    def test_full_run_creates_excel(self):
        client = FlowClient(messages=self._messages(),
                            entities={("user", 42): make_user(42, "ivan")})
        self._patch_client(client)

        code = asyncio.run(main_module.run(make_args()))

        self.assertEqual(code, main_module.EXIT_OK)
        files = list((self.tmp / "output").glob("*.xlsx"))
        self.assertEqual(len(files), 1)
        sheet = load_workbook(files[0])["Stars"]
        self.assertEqual(sheet.max_row, 2)            # шапка + одна запись
        self.assertEqual(sheet.cell(row=2, column=4).value, 30)   # current_message_id
        self.assertEqual(sheet.cell(row=2, column=7).value, "ivan")
        self.assertEqual(sheet.cell(row=2, column=9).value, 4)

    def test_run_without_stars_prints_message(self):
        client = FlowClient(messages=[make_message(msg_id=1, text="no stars")])
        self._patch_client(client)

        code = asyncio.run(main_module.run(make_args(count=1)))

        self.assertEqual(code, main_module.EXIT_OK)
        self.assertEqual(list((self.tmp / "output").glob("*.xlsx")), [])

    def test_interrupt_saves_partial_data(self):
        client = InterruptingClient(messages=self._messages(),
                                    entities={("user", 42): make_user(42, "ivan")})
        self._patch_client(client)

        code = asyncio.run(main_module.run(make_args(count=10)))

        self.assertEqual(code, main_module.EXIT_INTERRUPTED)
        files = list((self.tmp / "output").glob("*.xlsx"))
        self.assertEqual(len(files), 1)
        sheet = load_workbook(files[0])["Stars"]
        self.assertEqual(sheet.max_row, 2)

    def test_output_dir_from_argument(self):
        client = FlowClient(messages=self._messages(),
                            entities={("user", 42): make_user(42, "ivan")})
        self._patch_client(client)
        custom = self.tmp / "custom dir"
        code = asyncio.run(main_module.run(make_args(output_dir=str(custom))))
        self.assertEqual(code, main_module.EXIT_OK)
        self.assertEqual(len(list(custom.glob("*.xlsx"))), 1)

    def test_prompt_count_validation(self):
        answers = iter(["0", "-5", "abc", " 12 "])
        original_input = builtins.input
        builtins.input = lambda *args, **kwargs: next(answers)
        try:
            self.assertEqual(main_module.prompt_count(), 12)
        finally:
            builtins.input = original_input

    def test_prompt_count_preset(self):
        self.assertEqual(main_module.prompt_count(7), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
