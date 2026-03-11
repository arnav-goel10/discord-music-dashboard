import asyncio
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

import discord

from cogs.ui import (
    ConfirmClearView,
    ConfirmDeleteSavedPlaylistView,
    ConfirmRemoveTracksView,
    QueuePaginatorView,
    SavedPlaylistManagerView,
    SpotifyUI,
    _MAX_SAVED_PLAYLISTS,
    _sanitize_saved_playlist_items,
)
from utils.resolver import ResolvedTrack


class _DummyQueue:
    def __init__(self, *, is_empty: bool = True, count: int = 0, mode: str = "normal"):
        self.is_empty = is_empty
        self.count = count
        self.mode = mode


class _DummyGuild:
    def __init__(self):
        self.id = 123
        self.voice_client = None


class _DummyPlayer:
    def __init__(self):
        self.guild = _DummyGuild()
        self.queue = _DummyQueue(is_empty=True, count=0, mode="normal")
        self.current = None
        self.paused = False
        self.playing = False
        self.position = 0
        self.volume = 25
        self.ui_filter_preset = "none"


class _SleepyMessage:
    def __init__(self, delay: float):
        self.delay = delay
        self.id = 999

    async def edit(self, **kwargs):
        await asyncio.sleep(self.delay)
        return None


class _UnknownMessage:
    def __init__(self):
        self.id = 1000

    async def edit(self, **kwargs):
        class _Resp:
            status = 404
            reason = "Not Found"
            headers = {}

        raise discord.HTTPException(_Resp(), {"code": 10008, "message": "Unknown Message"})


class _ReplaceableMessage:
    def __init__(self, message_id: int, *, edit_error: Exception | None = None):
        self.id = message_id
        self._edit_error = edit_error
        self.deleted = False

    async def edit(self, **kwargs):
        if self._edit_error is not None:
            raise self._edit_error
        return None

    async def delete(self):
        self.deleted = True


class _DummyHome:
    def __init__(self, message):
        self.message = message
        self.send_count = 0

    async def send(self, **kwargs):
        self.send_count += 1
        return self.message


def _make_ui_stub() -> tuple[SpotifyUI, _DummyPlayer]:
    player = _DummyPlayer()
    ui = object.__new__(SpotifyUI)
    ui.player = player
    ui.message = object()
    ui._stopped = False
    ui._render_lock = asyncio.Lock()
    ui._rebind_lock = asyncio.Lock()
    ui._last_rebind_attempt_at = 0.0
    ui._has_ever_bound_message = True
    ui._voice_gap_started_at = None
    ui._consecutive_voice_gap_ticks = 0
    ui._consecutive_refresh_failures = 0
    ui._last_refresh_failure_at = 0.0
    ui._last_successful_render_at = time.monotonic()
    ui._last_rendered_track_key = "none"
    ui._last_stale_recovery_at = 0.0
    ui._refresh_restart_count = 0
    ui._use_v2_layout = True
    ui._last_render_key = None
    ui._refresh_interval = 0.01
    ui._voice_gap_stop_seconds = 0.5
    ui._stale_seconds = 60.0
    ui._stale_cooldown_seconds = 1.0
    ui._refresh_fail_rebind_threshold = 3
    ui._edit_timeout_seconds = 0.01
    ui._refresh_task = None
    ui._sync_components = lambda: None
    ui._render_state_key = lambda: ("static",)
    return ui, player


class TestDashboardRefreshHardening(unittest.IsolatedAsyncioTestCase):
    async def test_transient_voice_gap_does_not_stop_loop(self):
        ui, player = _make_ui_stub()
        player.guild.voice_client = None

        async def _update():
            return None

        ui.update_player_message = _update  # type: ignore[assignment]
        task = asyncio.create_task(ui._auto_refresh_loop())
        await asyncio.sleep(0.06)
        player.guild.voice_client = object()
        await asyncio.sleep(0.03)
        self.assertFalse(task.done(), "Refresh loop should survive short voice-client gaps.")
        task.cancel()
        await task

    async def test_persistent_idle_disconnect_stops_loop(self):
        ui, player = _make_ui_stub()
        player.guild.voice_client = None
        player.current = None
        player.queue = _DummyQueue(is_empty=True, count=0, mode="normal")
        ui._voice_gap_stop_seconds = 0.03

        async def _update():
            return None

        ui.update_player_message = _update  # type: ignore[assignment]
        task = asyncio.create_task(ui._auto_refresh_loop())
        await asyncio.sleep(0.09)
        self.assertTrue(task.done(), "Refresh loop should stop after sustained idle disconnect.")

    async def test_repeated_timeouts_force_rebind(self):
        ui, _ = _make_ui_stub()
        ui.message = _SleepyMessage(delay=0.05)
        rebind_count = 0

        async def _rebind():
            nonlocal rebind_count
            rebind_count += 1

        ui._rebind_dashboard_message = _rebind  # type: ignore[assignment]
        for _ in range(3):
            await ui.update_player_message(force=True)

        self.assertEqual(rebind_count, 1)
        self.assertIsNone(ui.message)

    async def test_unknown_message_http_error_triggers_rebind(self):
        ui, _ = _make_ui_stub()
        ui.message = _UnknownMessage()
        rebind_count = 0

        async def _rebind():
            nonlocal rebind_count
            rebind_count += 1

        ui._rebind_dashboard_message = _rebind  # type: ignore[assignment]
        await ui.update_player_message(force=True)
        self.assertEqual(rebind_count, 1)
        self.assertIsNone(ui.message)

    async def test_ensure_refresh_task_running_restarts_once(self):
        ui, _ = _make_ui_stub()
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        ui._refresh_task = done

        async def _loop():
            await asyncio.sleep(0.05)

        ui._auto_refresh_loop = _loop  # type: ignore[assignment]
        first = ui.ensure_refresh_task_running("test-restart")
        running_task = ui._refresh_task
        second = ui.ensure_refresh_task_running("already-running")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(ui._refresh_restart_count, 1)
        self.assertIs(running_task, ui._refresh_task)
        await running_task

    async def test_rebind_deletes_superseded_player_dashboard_message(self):
        ui, player = _make_ui_stub()
        ui.message = None
        old_message = _ReplaceableMessage(41, edit_error=RuntimeError("stuck"))
        new_message = _ReplaceableMessage(42)
        player.ui_message = old_message
        player.home = _DummyHome(new_message)
        ui.build_embed = lambda: object()  # type: ignore[assignment]
        ui._dashboard_scope_label = lambda: "guild=123"  # type: ignore[assignment]
        ui._track_key = lambda _track: "none"  # type: ignore[assignment]

        await ui._rebind_dashboard_message()

        self.assertIs(ui.message, new_message)
        self.assertIs(player.ui_message, new_message)
        self.assertTrue(old_message.deleted)
        self.assertEqual(player.home.send_count, 1)


class TestQueueMenuControls(unittest.IsolatedAsyncioTestCase):
    async def test_queue_paginator_keeps_only_primary_controls(self):
        player = _DummyPlayer()
        view = QueuePaginatorView(lines=["Track 1"], player=player)

        labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]

        self.assertEqual(labels, ["◁", "▷", "Remove Track", "Clear Queue"])
        self.assertNotIn("Close", labels)

    async def test_queue_confirmation_views_do_not_include_cancel(self):
        player = _DummyPlayer()
        paginator = QueuePaginatorView(lines=["Track 1"], player=player)

        remove_view = ConfirmRemoveTracksView(paginator, [1])
        clear_view = ConfirmClearView(paginator)

        remove_labels = [item.label for item in remove_view.children if isinstance(item, discord.ui.Button)]
        clear_labels = [item.label for item in clear_view.children if isinstance(item, discord.ui.Button)]

        self.assertEqual(remove_labels, ["Yes, remove them"])
        self.assertEqual(clear_labels, ["Yes, clear it"])


class TestSavedPlaylistManager(unittest.IsolatedAsyncioTestCase):
    def test_saved_playlist_limit_is_one(self):
        self.assertEqual(_MAX_SAVED_PLAYLISTS, 1)
        sanitized = _sanitize_saved_playlist_items(
            [
                {"url": "https://open.spotify.com/playlist/older123", "saved_at": 1},
                {"url": "https://open.spotify.com/playlist/newer123", "saved_at": 2},
            ]
        )

        self.assertEqual(
            sanitized,
            [{"url": "https://open.spotify.com/playlist/newer123", "saved_at": 2}],
        )

    async def test_recent_order_preview_shows_newest_first(self):
        dashboard = types.SimpleNamespace(_saved_playlist_order="recent")
        tracks = [
            ResolvedTrack("Older Track", "Artist", added_at="2024-01-01T00:00:00Z"),
            ResolvedTrack("Newest Track", "Artist", added_at="2024-03-01T00:00:00Z"),
            ResolvedTrack("Middle Track", "Artist", added_at="2024-02-01T00:00:00Z"),
        ]

        with patch("cogs.ui._load_saved_playlists", return_value=[{"url": "https://open.spotify.com/playlist/test123", "saved_at": 1}]):
            view = SavedPlaylistManagerView(dashboard, owner_user_id=1)

        with patch("utils.resolver.resolver.get_playlist_tracks", new=AsyncMock(return_value=tracks)):
            await view.refresh_preview(force_update=True)

        content = view._build_content()
        self.assertIn("Preview: **Recently Added**", content)
        self.assertLess(content.index("1. Newest Track - Artist"), content.index("2. Middle Track - Artist"))
        self.assertLess(content.index("2. Middle Track - Artist"), content.index("3. Older Track - Artist"))

    async def test_saved_playlist_buttons_use_icon_labels(self):
        dashboard = types.SimpleNamespace(_saved_playlist_order="recent")

        with patch("cogs.ui._load_saved_playlists", return_value=[{"url": "https://open.spotify.com/playlist/test123", "saved_at": 1}]):
            view = SavedPlaylistManagerView(dashboard, owner_user_id=1)

        labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]

        self.assertIn("▶", labels)
        self.assertIn("✎", labels)
        self.assertIn("＋", labels)
        self.assertIn("🗑", labels)

    async def test_play_selected_requests_fresh_refresh_for_recent_order(self):
        dashboard = types.SimpleNamespace(_saved_playlist_order="recent")
        music_cog = types.SimpleNamespace(_execute_play=AsyncMock())
        interaction = types.SimpleNamespace()

        with patch("cogs.ui._load_saved_playlists", return_value=[{"url": "https://open.spotify.com/playlist/test123", "saved_at": 1}]):
            view = SavedPlaylistManagerView(dashboard, owner_user_id=1)

        with patch("cogs.ui._get_music_cog", return_value=music_cog):
            play_button = next(
                item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "▶"
            )
            await play_button.callback(interaction)

        music_cog._execute_play.assert_awaited_once()
        _, kwargs = music_cog._execute_play.await_args
        self.assertEqual(kwargs["playlist_order"], "recent")
        self.assertTrue(kwargs["force_cache"])
        self.assertTrue(kwargs["force_playlist_refresh"])

    async def test_delete_selected_requires_confirmation(self):
        dashboard = types.SimpleNamespace(_saved_playlist_order="recent")
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(response=response)

        with patch("cogs.ui._load_saved_playlists", return_value=[{"url": "https://open.spotify.com/playlist/test123", "saved_at": 1}]):
            view = SavedPlaylistManagerView(dashboard, owner_user_id=1)

        delete_button = next(
            item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "🗑"
        )
        await delete_button.callback(interaction)

        response.send_message.assert_awaited_once()
        _, kwargs = response.send_message.await_args
        self.assertEqual(kwargs["ephemeral"], True)
        self.assertIsInstance(kwargs["view"], ConfirmDeleteSavedPlaylistView)


if __name__ == "__main__":
    unittest.main()
