import asyncio
import time
import unittest

import discord

from cogs.ui import SpotifyUI


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


if __name__ == "__main__":
    unittest.main()
