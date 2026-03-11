import os
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from cogs.music import Music
from utils import dj as dj_module


class _DummyQueue:
    def __init__(self) -> None:
        self.is_empty = True
        self._items = []
        self.put_calls = 0

    def __len__(self) -> int:
        return len(self._items)

    async def put_wait(self, item) -> None:
        self.put_calls += 1
        self._items.append(item)
        self.is_empty = False


class _DummyChannel:
    def __init__(self) -> None:
        self.members = []


class _DummyVoiceClient:
    def __init__(self) -> None:
        self.channel = _DummyChannel()


class _DummyGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.voice_client = _DummyVoiceClient()


class _DummyPlayer:
    def __init__(self, guild_id: int) -> None:
        self.guild = _DummyGuild(guild_id)
        self.queue = _DummyQueue()
        self.playing = False
        self.current = None
        self.play_calls = 0

    async def play(self, _track, *, volume=None, replace=False) -> None:
        self.play_calls += 1
        self.playing = True
        self.current = _track


class _DummyMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _DummyHome:
    def __init__(self, message) -> None:
        self.message = message
        self.send_calls = 0

    async def send(self, **kwargs):
        self.send_calls += 1
        return self.message


class _BrokenDashboardView:
    uses_v2_layout = True

    def __init__(self) -> None:
        self.stopped = False

    def ensure_refresh_task_running(self, _reason: str) -> None:
        return None

    async def update_player_message(self, force: bool = False) -> None:
        raise RuntimeError("stuck dashboard")

    def stop(self) -> None:
        self.stopped = True


class _ReplacementDashboardView:
    uses_v2_layout = True

    def __init__(self, _player) -> None:
        self.message = None


class TestMusicDJReliability(unittest.IsolatedAsyncioTestCase):
    async def test_track_end_handles_invalid_dj_env_values(self):
        music = object.__new__(Music)
        scope_id = 123
        music._dj_track_info = {
            scope_id: {
                "title": "Song",
                "artist": "Artist",
                "duration_ms": 180_000,
                "source": "ai_dj",
                "model_version": "v2",
                "exploration": False,
                "rank_features": {},
                "candidate_sources": [],
                "audit_event_id": "audit-1",
            }
        }
        music._dj_track_start_at = {scope_id: time.time() - 12.0}

        player = _DummyPlayer(scope_id)
        with patch.dict(
            os.environ,
            {
                "DJ_SKIP_THRESHOLD_SECONDS": "not-a-number",
                "DJ_MIN_LISTEN_FOR_COMPLETION_PCT": "bad",
            },
            clear=False,
        ), patch.object(dj_module.dj, "record_listen", new=AsyncMock()) as mock_record:
            await music._dj_record_track_end(scope_id, player, "replaced")
            self.assertEqual(mock_record.await_count, 1)

    async def test_auto_queue_aborts_if_dj_turns_off_midflight(self):
        music = object.__new__(Music)
        scope_id = 456
        music._dj_auto_queue_tasks = {}
        music._dj_prequeue_tasks = {}
        music._manual_advance_until = {}
        music._player_scope_id = lambda player: Music._player_scope_id(player)
        music._effective_player_volume = types.MethodType(lambda self, _player: 15, music)

        async def _safe_search(self, _query: str, **_kwargs):
            return None

        music._safe_wavelink_search = types.MethodType(_safe_search, music)
        music._extract_candidates = types.MethodType(lambda self, _result: [], music)

        active = True

        async def _resolve(*_args, **_kwargs):
            nonlocal active
            active = False
            return object()

        music._resolve_playable_with_fallbacks = types.MethodType(_resolve, music)
        player = _DummyPlayer(scope_id)
        rec = dj_module.RecommendedTrack(
            title="Candidate",
            artist="Artist",
            score=0.9,
            model_version="v2",
            audit_event_id="audit-midflight",
        )

        async def _pop_next(_guild_id: int):
            return rec

        with patch.object(dj_module.dj, "is_active", side_effect=lambda _gid: active), patch.object(
            dj_module.dj,
            "pop_next_recommendation",
            side_effect=_pop_next,
        ), patch.object(
            dj_module.dj,
            "record_resolver_outcome",
            new=AsyncMock(),
        ), patch.object(
            dj_module.dj,
            "trigger_prefetch",
            new=lambda _gid: None,
        ):
            await music._dj_auto_queue_next(player)
            task = music._dj_auto_queue_tasks.get(scope_id)
            if task:
                await task

        self.assertEqual(player.play_calls, 0)
        self.assertEqual(player.queue.put_calls, 0)

    async def test_track_start_replaces_and_deletes_stuck_dashboard_message(self):
        music = object.__new__(Music)
        scope_id = 789
        music._awaiting_autoplay = {}
        music._dashboard_opening = set()
        music._dj_track_start_at = {}
        music._dj_track_info = {}
        music._player_scope_id = lambda player: Music._player_scope_id(player)
        music._reset_exception_streak = types.MethodType(lambda self, _player: None, music)

        async def _apply_preferred_volume(self, _player, force: bool = False):
            return None

        music._apply_preferred_volume = types.MethodType(_apply_preferred_volume, music)
        music._schedule_dj_prequeue = types.MethodType(lambda self, _player, _track: None, music)

        player = _DummyPlayer(scope_id)
        old_message = _DummyMessage(101)
        new_message = _DummyMessage(202)
        old_view = _BrokenDashboardView()
        player.ui_view = old_view
        player.ui_message = old_message
        player.home = _DummyHome(new_message)

        payload = types.SimpleNamespace(player=player, track=None)

        with patch("cogs.ui.SpotifyUI", _ReplacementDashboardView):
            await music.on_wavelink_track_start(payload)

        self.assertIs(player.ui_message, new_message)
        self.assertIs(player.ui_view.message, new_message)
        self.assertTrue(old_view.stopped)
        self.assertTrue(old_message.deleted)
        self.assertEqual(player.home.send_calls, 1)


if __name__ == "__main__":
    unittest.main()
