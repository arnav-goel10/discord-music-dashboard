import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import main


class TestMusicBotClose(unittest.IsolatedAsyncioTestCase):
    async def test_close_cleans_up_dj_resources(self):
        bot = object.__new__(main.MusicBot)
        resolver_mod = types.SimpleNamespace(resolver=types.SimpleNamespace(close=AsyncMock()))
        lyrics_mod = types.SimpleNamespace(lyrics_resolver=types.SimpleNamespace(close=AsyncMock()))
        dj_mod = types.SimpleNamespace(dj=types.SimpleNamespace(close=AsyncMock()))

        with patch.dict(
            sys.modules,
            {
                "utils.resolver": resolver_mod,
                "utils.lyrics": lyrics_mod,
                "utils.dj": dj_mod,
            },
            clear=False,
        ), patch.object(main._BotBase, "close", new=AsyncMock()) as mock_super_close:
            await main.MusicBot.close(bot)

        resolver_mod.resolver.close.assert_awaited_once()
        lyrics_mod.lyrics_resolver.close.assert_awaited_once()
        dj_mod.dj.close.assert_awaited_once()
        mock_super_close.assert_awaited_once()
