import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import wavelink
import logging
import os
import re
import json
import time
import datetime
from typing import cast, Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._player_action_locks: dict[int, asyncio.Lock] = {}
        self._manual_advance_until: dict[int, float] = {}
        self._exception_streak: dict[int, int] = {}
        self._exception_last_at: dict[int, float] = {}
        self._playlist_load_tasks: dict[int, asyncio.Task] = {}
        self._queue_busy_reason: dict[int, str] = {}
        self._awaiting_autoplay: dict[int, asyncio.Event] = {}
        self._resolved_links_cache_file = os.path.join(_DATA_DIR, "resolved_track_links.json")
        self._resolved_links_cache_enabled = (
            os.getenv("RESOLVED_TRACK_CACHE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        )
        ttl_raw = os.getenv("RESOLVED_TRACK_CACHE_TTL_SECONDS", "2592000").strip()
        try:
            self._resolved_links_cache_ttl_seconds = max(0, int(ttl_raw))
        except ValueError:
            self._resolved_links_cache_ttl_seconds = 2_592_000
        self._resolved_links_cache = self._load_resolved_links_cache()

    @staticmethod
    def _normalize_cache_key(raw: Optional[str]) -> str:
        if not raw:
            return ""
        return " ".join(str(raw).strip().lower().split())

    def _build_resolved_link_keys(
        self,
        *,
        query: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> list[str]:
        keys: list[str] = []

        def add(raw: Optional[str]) -> None:
            key = self._normalize_cache_key(raw)
            if key and key not in keys:
                keys.append(key)

        add(query)
        if title and artist:
            add(f"{title} - {artist}")
            add(f"{title} {artist}")

        return keys

    def _load_resolved_links_cache(self) -> dict[str, dict[str, object]]:
        if not self._resolved_links_cache_enabled:
            return {}

        if not os.path.exists(self._resolved_links_cache_file):
            return {}

        try:
            with open(self._resolved_links_cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            logging.warning("Failed reading resolved links cache: %s", exc)
            return {}

        if not isinstance(raw, dict):
            return {}

        parsed: dict[str, dict[str, object]] = {}
        now = int(time.time())
        changed = False

        for raw_key, raw_val in raw.items():
            key = self._normalize_cache_key(raw_key)
            if not key:
                changed = True
                continue

            uri = ""
            updated_at = now

            if isinstance(raw_val, str):
                uri = raw_val.strip()
                updated_at = now
                changed = True
            elif isinstance(raw_val, dict):
                uri = str(raw_val.get("uri", "")).strip()
                try:
                    updated_at = int(raw_val.get("updated_at", now))
                except (TypeError, ValueError):
                    updated_at = now
                    changed = True
            else:
                changed = True
                continue

            if not uri:
                changed = True
                continue

            if self._resolved_links_cache_ttl_seconds > 0 and (now - updated_at) > self._resolved_links_cache_ttl_seconds:
                changed = True
                continue

            parsed[key] = {"uri": uri, "updated_at": updated_at}

        if changed:
            self._resolved_links_cache = parsed
            self._save_resolved_links_cache()

        return parsed

    def _save_resolved_links_cache(self) -> None:
        if not self._resolved_links_cache_enabled:
            return

        try:
            os.makedirs(os.path.dirname(self._resolved_links_cache_file), exist_ok=True)
            with open(self._resolved_links_cache_file, "w", encoding="utf-8") as f:
                json.dump(self._resolved_links_cache, f, indent=2)
        except Exception as exc:
            logging.warning("Failed writing resolved links cache: %s", exc)

    def _get_cached_resolved_link(self, keys: list[str]) -> Optional[str]:
        if not self._resolved_links_cache_enabled or not keys:
            return None

        now = int(time.time())
        changed = False

        for key in keys:
            entry = self._resolved_links_cache.get(key)
            if not isinstance(entry, dict):
                continue

            uri = str(entry.get("uri", "")).strip()
            try:
                updated_at = int(entry.get("updated_at", 0))
            except (TypeError, ValueError):
                updated_at = 0

            if not uri:
                self._resolved_links_cache.pop(key, None)
                changed = True
                continue

            if self._resolved_links_cache_ttl_seconds > 0 and updated_at and (now - updated_at) > self._resolved_links_cache_ttl_seconds:
                self._resolved_links_cache.pop(key, None)
                changed = True
                continue

            if changed:
                self._save_resolved_links_cache()
            return uri

        if changed:
            self._save_resolved_links_cache()
        return None

    def _set_cached_resolved_link(self, keys: list[str], uri: Optional[str]) -> None:
        if not self._resolved_links_cache_enabled or not keys or not uri:
            return

        uri = uri.strip()
        if not uri:
            return

        entry = {
            "uri": uri,
            "updated_at": int(time.time()),
        }
        changed = False

        for key in keys:
            existing = self._resolved_links_cache.get(key)
            if not isinstance(existing, dict) or existing.get("uri") != uri:
                self._resolved_links_cache[key] = entry
                changed = True

        if changed:
            self._save_resolved_links_cache()

    def _remove_cached_resolved_link(self, keys: list[str]) -> None:
        if not self._resolved_links_cache_enabled or not keys:
            return

        changed = False
        for key in keys:
            if key in self._resolved_links_cache:
                self._resolved_links_cache.pop(key, None)
                changed = True

        if changed:
            self._save_resolved_links_cache()

    async def _open_dashboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await self._reply(interaction, "This command can only be used in a server.", ephemeral=True)
            return
        # Ack quickly to avoid 3s timeout when voice connect/edit work takes longer.
        if not await self._safe_defer(interaction, thinking=True):
            return

        player = cast(wavelink.Player, interaction.guild.voice_client)
        created_player = False

        if not player:
            try:
                player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
                created_player = True
            except AttributeError:
                await self._reply(interaction, "Join a voice channel first, then run `/music`.", ephemeral=True)
                return
            except discord.ClientException:
                await self._reply(interaction, "I was unable to join this voice channel.", ephemeral=True)
                return

        if created_player:
            try:
                await player.set_volume(30)
            except Exception as exc:
                logging.debug("Failed to set default volume on new dashboard player: %s", exc)

        player.home = interaction.channel
        player.autoplay = wavelink.AutoPlayMode.partial

        from .ui import SpotifyUI

        # Keep one active dashboard message per player.
        if getattr(player, "ui_view", None):
            try:
                player.ui_view.stop()
            except Exception as exc:
                logging.debug("Failed to stop previous dashboard view: %s", exc)
        if getattr(player, "ui_message", None):
            try:
                await player.ui_message.delete()
            except discord.NotFound:
                pass
            except Exception as exc:
                logging.debug("Failed to delete previous dashboard message: %s", exc)

        view = SpotifyUI(player)
        if getattr(view, "uses_v2_layout", False):
            msg = await interaction.followup.send(view=view, wait=True)
        else:
            embed = view.build_embed()
            msg = await interaction.followup.send(embed=embed, view=view, wait=True)

        player.ui_view = view
        player.ui_message = msg
        view.message = msg

    @staticmethod
    def _is_url(query: str) -> bool:
        q = query.strip().lower()
        return q.startswith("http://") or q.startswith("https://")

    @staticmethod
    def _parse_spotify_timestamp(raw_value: Optional[str]) -> Optional[float]:
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip()
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def _order_playlist_tracks_by_recent_add(self, tracks: list) -> list:
        with_timestamp: list[tuple[int, float, object]] = []
        without_timestamp: list[tuple[int, object]] = []

        for idx, track in enumerate(tracks):
            added_at = getattr(track, "added_at", None)
            ts = self._parse_spotify_timestamp(added_at)
            if ts is None:
                without_timestamp.append((idx, track))
                continue
            with_timestamp.append((idx, ts, track))

        # Newest additions first; stable sort preserves source order for ties.
        with_timestamp.sort(key=lambda item: item[1], reverse=True)

        ordered = [track for _, _, track in with_timestamp]
        ordered.extend(track for _, track in reversed(without_timestamp))
        return ordered

    @staticmethod
    def _fmt_duration(track: wavelink.Playable) -> str:
        ms = int(getattr(track, "length", 0) or 0)
        if ms <= 0:
            return "LIVE"
        total = ms // 1000
        m, s = divmod(total, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @staticmethod
    def _build_query_variants(query: str, *, title: Optional[str] = None, artist: Optional[str] = None) -> list[str]:
        variants: list[str] = []

        def add(raw: Optional[str]) -> None:
            if not raw:
                return
            cleaned = " ".join(str(raw).split()).strip()
            if cleaned and cleaned not in variants:
                variants.append(cleaned)

        add(query)

        if title and artist:
            add(f"{title} {artist}")
            add(f"{title} - {artist}")

            stripped_title = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", title).strip()
            if stripped_title and stripped_title != title:
                add(f"{stripped_title} {artist}")

        base_snapshot = list(variants)
        for item in base_snapshot:
            add(item.replace("’", "'"))
            add(item.replace("'", ""))
            add(item.replace("&", "and"))

        return variants[:6]

    @staticmethod
    def _extract_candidates(search_result) -> list[wavelink.Playable]:
        if not search_result:
            return []

        if isinstance(search_result, wavelink.Playlist):
            return list(search_result.tracks)

        return list(search_result)

    async def _resolve_playable_with_fallbacks(
        self,
        *,
        query: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        matcher=None,
    ) -> Optional[wavelink.Playable]:
        cache_keys = self._build_resolved_link_keys(query=query, title=title, artist=artist)
        cached_uri = self._get_cached_resolved_link(cache_keys)
        if cached_uri:
            cached_result = await self._safe_wavelink_search(cached_uri, suppress_errors=True)
            cached_candidates = self._extract_candidates(cached_result)
            if cached_candidates:
                return self._pick_playable_candidate(
                    cached_candidates,
                    title=title,
                    artist=artist,
                    matcher=matcher,
                ) or cached_candidates[0]
            self._remove_cached_resolved_link(cache_keys)

        variants = self._build_query_variants(query, title=title, artist=artist)
        if not variants:
            return None

        search_plan: list[tuple[Optional[wavelink.TrackSource], str]] = []
        primary = variants[0]
        search_plan.append((wavelink.TrackSource.YouTubeMusic, primary))

        for source in (wavelink.TrackSource.YouTubeMusic, wavelink.TrackSource.YouTube):
            for candidate_query in variants:
                if source == wavelink.TrackSource.YouTubeMusic and candidate_query == primary:
                    continue
                search_plan.append((source, candidate_query))

        for source, candidate_query in search_plan:
            kwargs = {"source": source} if source else {}
            result = await self._safe_wavelink_search(candidate_query, suppress_errors=True, **kwargs)
            candidates = self._extract_candidates(result)
            if not candidates:
                continue

            playable = self._pick_playable_candidate(
                candidates,
                title=title,
                artist=artist,
                matcher=matcher,
            ) or candidates[0]
            playable_uri = str(getattr(playable, "uri", "")).strip()
            if playable_uri:
                self._set_cached_resolved_link(cache_keys, playable_uri)
            return playable

        logging.warning(
            "Could not resolve playable track after fallbacks. query=%r title=%r artist=%r",
            query,
            title,
            artist,
        )
        return None

    def _pick_playable_candidate(
        self,
        candidates: list[wavelink.Playable],
        *,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        matcher=None,
    ) -> Optional[wavelink.Playable]:
        if not candidates:
            return None

        shortlist = list(candidates[:12])

        if matcher and title and artist:
            matched = matcher.get_best_match(title, artist, shortlist)
            if matched:
                return matched

        def quality_score(track: wavelink.Playable) -> int:
            score = 0
            t = str(getattr(track, "title", "")).lower()
            a = str(getattr(track, "author", "")).lower()
            length = int(getattr(track, "length", 0) or 0)

            # Prefer official/Topic sources for cleaner uploads.
            if " - topic" in a:
                score += 8
            if "vevo" in a:
                score += 4

            # Prefer pure audio uploads and penalize edited variants.
            if "official audio" in t or " audio" in t:
                score += 3
            if "official video" in t or "music video" in t:
                score -= 2

            for noisy in (
                "live",
                "cover",
                "karaoke",
                "instrumental",
                "acoustic",
                "remix",
                "8d",
                "slowed",
                "reverb",
                "sped up",
                "nightcore",
                "tiktok",
            ):
                if noisy in t:
                    score -= 8

            # Prefer normal song durations.
            if 120_000 <= length <= 480_000:
                score += 2
            elif length == 0:
                score -= 1

            return score

        return max(shortlist, key=quality_score)

    async def _resolve_spotify_tracks_parallel(self, tracks, *, matcher, concurrency: int = 4, progress_callback=None):
        concurrency = max(1, min(concurrency, 30))
        sem = asyncio.Semaphore(concurrency)
        completed = 0
        total = len(tracks)

        async def resolve_one(idx: int, track_obj):
            nonlocal completed
            try:
                async with sem:
                    playable = await self._resolve_playable_with_fallbacks(
                        query=track_obj.query,
                        title=track_obj.title,
                        artist=track_obj.artist,
                        matcher=matcher,
                    )
                return idx, playable
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.error("Background load failed for %s: %s", track_obj.query, exc)
                return idx, None
            finally:
                completed += 1
                if progress_callback:
                    asyncio.create_task(progress_callback(completed, total))

        resolved = await asyncio.gather(
            *(resolve_one(i, rt) for i, rt in enumerate(tracks)),
            return_exceptions=True,
        )

        ordered: list[tuple[int, wavelink.Playable]] = []
        failed = 0
        for item in resolved:
            if isinstance(item, Exception):
                logging.error("Background resolution task crashed: %s", item)
                failed += 1
                continue
            idx, playable = item
            if playable:
                ordered.append((idx, playable))
            else:
                failed += 1

        ordered.sort(key=lambda x: x[0])
        return [playable for _, playable in ordered], failed

    async def _reply(
        self,
        interaction: discord.Interaction,
        message: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        ephemeral: bool = False,
    ):
        if message is None and embed is None:
            return None
        try:
            if interaction.response.is_done():
                return await interaction.followup.send(message, embed=embed, ephemeral=ephemeral)
            return await interaction.response.send_message(message, embed=embed, ephemeral=ephemeral)
        except (discord.NotFound, discord.HTTPException) as exc:
            logging.warning("Failed to send interaction response: %s", exc)
            return None

    async def _safe_defer(
        self,
        interaction: discord.Interaction,
        *,
        thinking: bool = True,
        ephemeral: bool = False,
    ) -> bool:
        if interaction.response.is_done():
            return True

        try:
            await interaction.response.defer(thinking=thinking, ephemeral=ephemeral)
            return True
        except discord.InteractionResponded:
            return True
        except (discord.NotFound, discord.HTTPException) as exc:
            logging.warning("Failed to defer interaction (possibly expired): %s", exc)
            return False

    async def _safe_wavelink_search(self, query: str, *, suppress_errors: bool = False, **kwargs):
        try:
            return await wavelink.Playable.search(query, **kwargs)
        except asyncio.CancelledError:
            raise
        except wavelink.LavalinkLoadException as exc:
            if suppress_errors:
                logging.debug("Lavalink load failed for query '%s': %s", query, exc)
            else:
                logging.error("Lavalink load failed for query '%s': %s", query, exc)
            return None
        except Exception as exc:
            if suppress_errors:
                logging.debug("Unexpected Wavelink search failure for query '%s': %s", query, exc)
            else:
                logging.error("Unexpected Wavelink search failure for query '%s': %s", query, exc)
            return None

    async def _resume_or_start_next(self, player: wavelink.Player) -> str:
        if player.current:
            await player.pause(False)
            return "Resumed the music."

        if not player.queue.is_empty:
            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))
            return "No active track was playing, so I started the next song in queue."

        return "There is no paused track and the queue is empty."

    def _get_player_action_lock(self, player: wavelink.Player) -> asyncio.Lock:
        guild_id = self._player_scope_id(player)
        lock = self._player_action_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._player_action_locks[guild_id] = lock
        return lock

    @staticmethod
    def _player_scope_id(player: wavelink.Player) -> int:
        guild = getattr(player, "guild", None)
        if guild and getattr(guild, "id", None):
            return int(guild.id)
        guild_id = getattr(player, "guild_id", None)
        if guild_id:
            try:
                return int(guild_id)
            except (TypeError, ValueError):
                pass
        return id(player)

    def _cleanup_player_scope(self, player: wavelink.Player) -> None:
        scope_id = self._player_scope_id(player)
        self._player_action_locks.pop(scope_id, None)
        self._manual_advance_until.pop(scope_id, None)
        self._exception_streak.pop(scope_id, None)
        self._exception_last_at.pop(scope_id, None)
        event = self._awaiting_autoplay.pop(scope_id, None)
        if event:
            event.set()

    def _set_queue_busy(self, player: wavelink.Player, reason: str) -> None:
        self._queue_busy_reason[self._player_scope_id(player)] = reason

    def _clear_queue_busy(self, player: wavelink.Player) -> None:
        self._queue_busy_reason.pop(self._player_scope_id(player), None)

    def _queue_action_block_message(self, player: wavelink.Player) -> Optional[str]:
        scope_id = self._player_scope_id(player)
        task = self._playlist_load_tasks.get(scope_id)
        if task and task.done():
            self._playlist_load_tasks.pop(scope_id, None)
            self._queue_busy_reason.pop(scope_id, None)
        return self._queue_busy_reason.get(scope_id)

    async def _cancel_playlist_loading(self, player: wavelink.Player) -> None:
        scope_id = self._player_scope_id(player)
        task = self._playlist_load_tasks.pop(scope_id, None)
        self._queue_busy_reason.pop(scope_id, None)
        if not task or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logging.debug("Background playlist loader ended with error after cancel: %s", exc)

    def _reset_exception_streak(self, player: wavelink.Player) -> None:
        scope_id = self._player_scope_id(player)
        self._exception_streak[scope_id] = 0
        self._exception_last_at.pop(scope_id, None)

    def _register_exception_and_get_streak(self, player: wavelink.Player) -> int:
        scope_id = self._player_scope_id(player)
        now = time.time()
        last_at = self._exception_last_at.get(scope_id, 0.0)
        if now - last_at > 20.0:
            streak = 1
        else:
            streak = self._exception_streak.get(scope_id, 0) + 1

        self._exception_last_at[scope_id] = now
        self._exception_streak[scope_id] = streak
        return streak

    @staticmethod
    def _track_identity(track: Optional[wavelink.Playable]) -> str:
        if not track:
            return ""
        return str(
            getattr(track, "identifier", None)
            or getattr(track, "uri", None)
            or getattr(track, "title", None)
            or ""
        )

    async def _force_start_next_from_queue(self, player: wavelink.Player) -> bool:
        if player.queue.is_empty:
            return False

        next_track = player.queue.get()
        await player.play(
            next_track,
            replace=True,
            volume=int(getattr(player, "volume", 30) or 30),
        )
        return True

    async def _skip_or_start_next(self, player: wavelink.Player) -> str:
        blocked_reason = self._queue_action_block_message(player)
        if blocked_reason:
            return f"⏳ {blocked_reason}. Please wait a moment."

        lock = self._get_player_action_lock(player)
        async with lock:
            async def _ensure_not_paused() -> None:
                if not bool(getattr(player, "paused", False)):
                    return
                try:
                    await player.pause(False)
                except Exception as exc:
                    logging.debug("Failed to clear paused state after skip: %s", exc)

            previous_id = self._track_identity(player.current)
            had_current = bool(previous_id)

            if had_current:
                try:
                    await player.skip(force=True)
                except Exception as exc:
                    logging.warning("Skip failed, attempting queue fallback: %s", exc)

                # Give Lavalink a brief moment to transition track state.
                await asyncio.sleep(0.35)

            current_id = self._track_identity(player.current)

            # Success path: current track changed or playback moved on.
            if had_current and current_id and current_id != previous_id:
                await _ensure_not_paused()
                return "Skipped the current song."
            if had_current and not current_id and player.playing:
                await _ensure_not_paused()
                return "Skipped the current song."

            if await self._force_start_next_from_queue(player):
                await _ensure_not_paused()
                self._manual_advance_until[self._player_scope_id(player)] = time.time() + 2.0
                return "Skipped to the next queued song."

            if had_current and current_id == previous_id:
                # No queue left; best-effort stop to clear stale current state.
                try:
                    await player.stop(force=True)
                except Exception:
                    pass

            return "Queue is empty."

    def _player_for_guild_id(self, guild_id: int) -> Optional[wavelink.Player]:
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return None
        voice_client = guild.voice_client
        if isinstance(voice_client, wavelink.Player):
            return voice_client
        return None

    async def _apply_filter_preset(self, player: wavelink.Player, preset: str) -> str:
        preset = str(preset or "none").strip().lower()
        pretty = {
            "none": "None",
            "bass_boost": "Bass Boost",
            "treble_boost": "Treble Boost",
            "nightcore": "Nightcore",
            "vaporwave": "Vaporwave",
            "karaoke": "Karaoke",
            "eight_d": "8D",
            "low_pass": "LowPass",
        }
        if preset not in pretty:
            preset = "none"

        filters = wavelink.Filters()

        if preset == "bass_boost":
            filters.equalizer.set(
                bands=[
                    {"band": 0, "gain": 0.20},
                    {"band": 1, "gain": 0.16},
                    {"band": 2, "gain": 0.10},
                    {"band": 3, "gain": 0.0},
                    {"band": 4, "gain": -0.04},
                    {"band": 5, "gain": -0.08},
                ]
            )
        elif preset == "treble_boost":
            filters.equalizer.set(
                bands=[
                    {"band": 0, "gain": -0.08},
                    {"band": 1, "gain": -0.05},
                    {"band": 2, "gain": 0.0},
                    {"band": 3, "gain": 0.06},
                    {"band": 4, "gain": 0.12},
                    {"band": 5, "gain": 0.16},
                ]
            )
        elif preset == "nightcore":
            filters.timescale.set(speed=1.15, pitch=1.12, rate=1.0)
        elif preset == "vaporwave":
            filters.timescale.set(speed=0.88, pitch=0.88, rate=1.0)
        elif preset == "karaoke":
            filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
        elif preset == "eight_d":
            filters.rotation.set(rotation_hz=0.2)
        elif preset == "low_pass":
            filters.low_pass.set(smoothing=20.0)

        await player.set_filters(filters)
        setattr(player, "ui_filter_preset", preset)
        return pretty[preset]

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        command_name = interaction.command.name if interaction.command else "unknown"
        logging.error("App command error in /%s: %s", command_name, error, exc_info=error)
        message = f"An error occurred: {error}"
        await self._reply(interaction, message, ephemeral=True)
        
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        print(f"Wavelink Node connected: {payload.node!r} | Resumed: {payload.resumed}")

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        from .ui import SpotifyUI
        player: wavelink.Player = payload.player
        if not player:
            return
        player.autoplay = wavelink.AutoPlayMode.partial
        self._reset_exception_streak(player)

        # Signal any pending track_end fallback that Lavalink handled auto-advance,
        # so it doesn't also pop from the queue.
        event = self._awaiting_autoplay.get(self._player_scope_id(player))
        if event:
            event.set()

        # If an active dashboard already exists, refresh it in-place to avoid
        # posting a new message into the channel on every track change.
        existing_view = getattr(player, "ui_view", None)
        existing_message = getattr(player, "ui_message", None)
        if existing_view and existing_message:
            try:
                await existing_view.update_player_message(force=True)
                return
            except Exception as exc:
                logging.debug("Could not refresh existing dashboard on track start: %s", exc)

        # No existing dashboard — stop any stale view and create a fresh one.
        if existing_view:
            try:
                existing_view.stop()
            except Exception:
                pass

        view = SpotifyUI(player)
        player.ui_view = view

        if getattr(player, "home", None):
            if getattr(view, "uses_v2_layout", False):
                msg = await player.home.send(view=view)
            else:
                embed = view.build_embed()
                msg = await player.home.send(embed=embed, view=view)
            player.ui_message = msg
            view.message = msg

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player: wavelink.Player = payload.player
        if not player:
            return

        scope_id = self._player_scope_id(player)
        if self._manual_advance_until.get(scope_id, 0.0) > time.time():
            # A manual skip/replace path already advanced playback. Avoid double-pop.
            pass
        else:
            reason = str(getattr(payload, "reason", "") or "").lower()
            # Only fallback-advance when a track naturally finishes.
            # Load failures are handled by on_wavelink_track_exception.
            if reason == "finished":
                # Wait for on_wavelink_track_start to signal that Lavalink handled
                # auto-advance on its own. If it does within the timeout we skip the
                # fallback entirely, eliminating the double-pop race. If Lavalink is
                # slow or drops the ball, the TimeoutError branch takes over.
                event = asyncio.Event()
                self._awaiting_autoplay[scope_id] = event
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.5)
                    # Lavalink fired track_start — it handled the advance.
                except asyncio.TimeoutError:
                    # Lavalink didn't auto-advance; kick the queue if still idle.
                    if not player.playing and not player.current and not player.queue.is_empty:
                        try:
                            await self._force_start_next_from_queue(player)
                        except Exception as exc:
                            logging.debug("Failed to force-start next track on track_end: %s", exc)
                except asyncio.CancelledError:
                    raise
                finally:
                    self._awaiting_autoplay.pop(scope_id, None)

        if getattr(player, "ui_view", None):
            try:
                await player.ui_view.update_player_message()
            except Exception as exc:
                logging.debug("Failed to refresh player UI on track end: %s", exc)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player: wavelink.Player = payload.player
        if not player:
            return

        streak = self._register_exception_and_get_streak(player)

        exc = payload.exception
        message = exc.get("message", "Unknown playback error")
        cause = exc.get("cause", "Unknown cause")
        logging.error(
            "Track exception for '%s' (streak=%s): %s | %s",
            payload.track.title,
            streak,
            message,
            cause,
        )

        if getattr(player, "home", None) and streak == 1:
            try:
                await player.home.send(
                    f"⚠️ Playback failed for **{payload.track.title}**. "
                    f"Reason: `{message}`. Trying the next track..."
                )
            except Exception:
                pass

        # Hard-stop auto-advance bursts to avoid skip storms / request floods.
        if streak >= 5:
            if getattr(player, "home", None):
                try:
                    await player.home.send(
                        "⚠️ Too many consecutive playback failures. "
                        "Auto-skip has been paused to avoid request/rate-limit storms."
                    )
                except Exception:
                    pass
            if getattr(player, "ui_view", None):
                try:
                    await player.ui_view.update_player_message(force=True)
                except Exception:
                    pass
            return

        if not player.queue.is_empty:
            try:
                await asyncio.sleep(0.8)
                next_track = player.queue.get()
                self._manual_advance_until[self._player_scope_id(player)] = time.time() + 2.0
                await player.play(
                    next_track,
                    replace=True,
                    volume=int(getattr(player, "volume", 30) or 30),
                )
            except Exception as exc:
                logging.error("Failed to continue playback after track exception: %s", exc)
        elif getattr(player, "ui_view", None):
            try:
                await player.ui_view.update_player_message()
            except Exception:
                pass

    @app_commands.command(name="music", description="Open the interactive music dashboard")
    async def music(self, interaction: discord.Interaction):
        await self._open_dashboard(interaction)

    @app_commands.command(name="help", description="Show music bot help and control guide")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Music Bot Help",
            description="Control everything from **/music** dashboard.",
            color=0x2B2D31,
        )
        embed.add_field(
            name="Commands",
            value="`/music` open dashboard\n`/stop` disconnect bot\n`/help` this help panel",
            inline=False,
        )
        embed.add_field(
            name="Dashboard",
            value=(
                "Playback: Restart, Pause, Skip, Shuffle, Loop\n"
                "Queue: Add, Queue (with Clear Queue), Lists\n"
                "Audio: Set Volume + Filter popup"
            ),
            inline=False,
        )
        embed.add_field(
            name="Notes",
            value="Progress bar updates continuously. Spotify playlist loading continues in background.",
            inline=False,
        )
        await self._reply(interaction, embed=embed)

    @app_commands.command(name="stop", description="Stop playback and disconnect the bot")
    async def stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await self._reply(interaction, "This command can only be used in a server.", ephemeral=True)
            return

        player = cast(wavelink.Player, interaction.guild.voice_client)
        if not player:
            await self._reply(interaction, "I'm not connected to voice.", ephemeral=True)
            return

        if getattr(player, "ui_view", None):
            try:
                player.ui_view.stop()
            except Exception:
                pass

        await self._cancel_playlist_loading(player)
        self._cleanup_player_scope(player)

        try:
            await player.disconnect()
        except Exception as exc:
            await self._reply(interaction, f"Failed to disconnect cleanly: {exc}", ephemeral=True)
            return

        await self._reply(interaction, "Stopped playback and disconnected from voice.")

    async def _execute_play(
        self,
        interaction: discord.Interaction,
        query: str,
        index: int = None,
        force_cache: bool = False,
        playlist_order: str = "recent",
    ):
        if not interaction.guild:
            await self._reply(interaction, "This command can only be used in a server.", ephemeral=True)
            return

        # Acknowledge immediately to avoid hitting Discord's 3-second interaction timeout.
        if not await self._safe_defer(interaction, ephemeral=True):
            return

        player: wavelink.Player
        player = cast(wavelink.Player, interaction.guild.voice_client)
        created_player = False

        if not player:
            try:
                player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
                created_player = True
            except AttributeError:
                await self._reply(interaction, "Please join a voice channel first!", ephemeral=True)
                return
            except discord.ClientException:
                await self._reply(interaction, "I was unable to join this voice channel.", ephemeral=True)
                return

        if created_player:
            try:
                await player.set_volume(30)
            except Exception as exc:
                logging.debug("Failed to set default volume on new playback player: %s", exc)

        player.home = interaction.channel
        # Ensure queue tracks continue automatically without needing manual /resume.
        player.autoplay = wavelink.AutoPlayMode.partial
        await self._cancel_playlist_loading(player)
        playlist_order = str(playlist_order or "recent").strip().lower()
        if playlist_order not in {"recent", "spotify"}:
            playlist_order = "recent"

        # Check if URL is Spotify
        is_spotify = query.startswith("https://open.spotify.com/")

        if is_spotify:
            _SUPPORTED = ("/track/", "/playlist/", "/album/")
            if not any(part in query for part in _SUPPORTED):
                self._clear_queue_busy(player)
                return await interaction.followup.send(
                    "❌ Unsupported Spotify link. Only tracks, playlists, and albums are supported."
                )

            from utils.resolver import resolver
            from utils.matcher import matcher
            # Handle playlist vs single track
            if "/playlist/" in query or "/album/" in query:
                is_playlist_link = "/playlist/" in query
                use_recent_order = is_playlist_link and playlist_order == "recent"
                order_label = "Recently Added" if use_recent_order else "Spotify Order"
                self._set_queue_busy(player, "Queue is still loading tracks in the background")
                playlist_status_message: Optional[discord.WebhookMessage] = None
                initial_status: str
                if force_cache:
                    if resolver.has_cached_playlist(query):
                        initial_status = "⏳ Fetching playlist instantly from the **Local Hard-Drive Snapshot**..."
                    else:
                        initial_status = "⏳ No local snapshot found. Refreshing Spotify data..."
                else:
                    initial_status = "⏳ Analyzing Spotify collection..."

                try:
                    playlist_status_message = await interaction.followup.send(
                        initial_status,
                        ephemeral=True,
                        wait=True,
                    )
                except Exception as exc:
                    logging.warning("Failed to send playlist status message: %s", exc)
                    
                # Note: resolver uses cache by default where possible.
                # The dashboard Lists manager passes force_cache=True so existing
                # local snapshots can be used immediately even when older than 7 days.
                tracks = await resolver.get_playlist_tracks(query, force_cache_bypass=force_cache)
                if (
                    use_recent_order
                    and tracks
                    and all(not getattr(track, "added_at", None) for track in tracks)
                ):
                    # Older local snapshots may not include `added_at`. Refresh once so
                    # "Recently Added" can use real Spotify timestamps when available.
                    refreshed_tracks = await resolver.get_playlist_tracks(
                        query,
                        force_update=True,
                        force_cache_bypass=False,
                    )
                    if refreshed_tracks:
                        tracks = refreshed_tracks

                if not tracks:
                    failure_code = resolver.last_failure_code
                    failure_msg = resolver.last_failure_message

                    if failure_code == "spotify_playlist_forbidden":
                        logging.warning("Spotify playlist extraction blocked: %s", failure_msg)
                        self._clear_queue_busy(player)
                        return await interaction.followup.send(
                            "❌ Spotify API returned **403 Forbidden** for playlist items. "
                            "Root cause: this app/token is not allowed to read that playlist in current Spotify quota/auth mode. "
                            "Primary fix: use Spotify user OAuth for a playlist owner/collaborator account "
                            "(scopes: `playlist-read-private` and `playlist-read-collaborative`) "
                            "or move the Spotify app to **Extended Quota Mode**."
                        )

                    if failure_code in {
                        "spotify_user_refresh_failed",
                        "spotify_user_auth_missing",
                        "spotify_app_auth_missing",
                        "spotify_credentials_missing",
                    }:
                        logging.warning("Spotify auth setup issue: %s", failure_msg)
                        self._clear_queue_busy(player)
                        return await interaction.followup.send(
                            "❌ Spotify authentication is not configured correctly for playlist extraction. "
                            "Check your Spotify credentials/user token setup and try again."
                        )

                    logging.warning("Spotify resolver returned no tracks for %s. Trying Lavalink direct load fallback.", query)
                    direct_load = await self._safe_wavelink_search(query)
                    if isinstance(direct_load, wavelink.Playlist):
                        ordered_tracks = list(direct_load.tracks)
                        fallback_order_label = order_label
                        if use_recent_order:
                            # Direct Lavalink playlist loads don't include Spotify `added_at`,
                            # so we cannot compute true "recently added" ordering here.
                            # We reverse the order because usually newer tracks are added at the end.
                            ordered_tracks.reverse()
                            fallback_order_label = "Newest First (Approximate)"

                        if index is not None:
                            added = 0
                            for track in ordered_tracks:
                                safe_idx = max(0, min(index + added, player.queue.count))
                                player.queue.put_at(safe_idx, track)
                                added += 1
                        else:
                            for track in ordered_tracks:
                                await player.queue.put_wait(track)
                            added = len(ordered_tracks)

                        await interaction.followup.send(
                            f"⚠️ Spotify API metadata was unavailable. "
                            f"Loaded **{direct_load.name}** directly ({added} songs, {fallback_order_label})."
                        )

                        if not player.playing and not player.queue.is_empty:
                            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))

                        if getattr(player, "ui_view", None):
                            await player.ui_view.update_player_message()
                        self._clear_queue_busy(player)
                        return

                    if direct_load:
                        fallback_track: wavelink.Playable = direct_load[0]
                        if index is not None:
                            safe_idx = max(0, min(index, player.queue.count))
                            player.queue.put_at(safe_idx, fallback_track)
                        else:
                            await player.queue.put_wait(fallback_track)

                        await interaction.followup.send(
                            f"⚠️ Spotify playlist metadata was unavailable. Added fallback track **{fallback_track.title}**."
                        )

                        if not player.playing and not player.queue.is_empty:
                            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))
                        self._clear_queue_busy(player)
                        return

                    self._clear_queue_busy(player)
                    return await interaction.followup.send(
                        "❌ Could not extract tracks from that Spotify link. "
                        "If Lavalink Spotify lookup is enabled, restart Lavalink so updated credentials/config are loaded."
                    )
                
                # "Recently Added" for Spotify playlists must use actual added timestamps.
                if use_recent_order:
                    tracks = self._order_playlist_tracks_by_recent_add(tracks)
                
                # Load first track immediately
                first_track = tracks.pop(0)
                # Query YouTube Music for higher official audio quality and score top candidates.
                first_playable = await self._resolve_playable_with_fallbacks(
                    query=first_track.query,
                    title=first_track.title,
                    artist=first_track.artist,
                    matcher=matcher,
                )
                if not first_playable:
                    self._clear_queue_busy(player)
                    return await interaction.followup.send("❌ Could not find the first track's audio.")
                
                if index is not None:
                    safe_idx = max(0, min(index, player.queue.count))
                    player.queue.put_at(safe_idx, first_playable)
                else:
                    await player.queue.put_wait(first_playable)

                remaining_count = len(tracks)
                if remaining_count > 0:
                    success_status = (
                        f"✅ Fast-loaded **{first_playable.title}** ({order_label}). "
                        f"Resolving {remaining_count} remaining tracks in parallel..."
                    )
                else:
                    success_status = f"✅ Fast-loaded **{first_playable.title}** ({order_label})."

                if playlist_status_message:
                    try:
                        await playlist_status_message.edit(content=success_status)
                    except Exception as exc:
                        logging.debug("Failed to edit playlist status message: %s", exc)
                        await interaction.followup.send(success_status, ephemeral=True)
                else:
                    await interaction.followup.send(success_status, ephemeral=True)
                
                if not player.playing:
                    await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))

                if not tracks:
                    self._clear_queue_busy(player)
                    if getattr(player, "ui_view", None):
                        await player.ui_view.update_player_message(force=True)
                    return

                # Start background task to resolve and enqueue remaining tracks in parallel,
                # while preserving the original playlist order.
                async def load_remaining(track_list, current_player, start_idx):
                    scope_id = self._player_scope_id(current_player)
                    concurrency_raw = os.getenv("SPOTIFY_BACKGROUND_SEARCH_CONCURRENCY", "4")
                    try:
                        concurrency = int(concurrency_raw)
                    except ValueError:
                        concurrency = 4

                    last_update = time.time()
                    async def on_progress(completed: int, total: int):
                        nonlocal last_update
                        now = time.time()
                        if completed == total or now - last_update > 2.0:
                            last_update = now
                            if playlist_status_message:
                                try:
                                    status = (
                                        f"✅ Fast-loaded **{first_playable.title}**.\n"
                                        f"⏳ Resolving {total} remaining tracks... ({completed}/{total})"
                                    )
                                    await playlist_status_message.edit(content=status)
                                except Exception:
                                    pass

                    try:
                        playables, failed_count = await self._resolve_spotify_tracks_parallel(
                            track_list,
                            matcher=matcher,
                            concurrency=concurrency,
                            progress_callback=on_progress,
                        )

                        if start_idx is not None:
                            insert_base = max(0, start_idx + 1)
                            for offset, playable in enumerate(playables):
                                safe_idx = max(0, min(insert_base + offset, current_player.queue.count))
                                current_player.queue.put_at(safe_idx, playable)
                        else:
                            for playable in playables:
                                await current_player.queue.put_wait(playable)

                        if failed_count and getattr(current_player, "home", None):
                            try:
                                await current_player.home.send(
                                    f"⚠️ Queue enrichment completed with partial failures: "
                                    f"added {len(playables)}/{len(track_list)} tracks "
                                    f"(failed: {failed_count})."
                                )
                            except Exception:
                                pass
                    except asyncio.CancelledError:
                        logging.info("Background queue enrichment cancelled for scope %s", scope_id)
                        raise
                    finally:
                        tracked_task = self._playlist_load_tasks.get(scope_id)
                        if tracked_task is asyncio.current_task():
                            self._playlist_load_tasks.pop(scope_id, None)
                        self._queue_busy_reason.pop(scope_id, None)

                        if getattr(current_player, "ui_view", None):
                            try:
                                await current_player.ui_view.update_player_message(force=True)
                            except Exception:
                                pass

                task = asyncio.create_task(load_remaining(tracks, player, index))
                self._playlist_load_tasks[self._player_scope_id(player)] = task
                
            else:
                # Single spotify track
                self._clear_queue_busy(player)
                track_info = await resolver.get_single_track(query)
                if not track_info:
                    # Fallback to Odesli
                    track_info = await resolver.resolve_via_odesli(query)
                    
                if not track_info:
                    return await interaction.followup.send("❌ Could not resolve Spotify track.")
                    
                playable = await self._resolve_playable_with_fallbacks(
                    query=track_info.query,
                    title=track_info.title,
                    artist=track_info.artist,
                    matcher=matcher,
                )
                if not playable:
                    return await interaction.followup.send("❌ Could not find audio for this Spotify track.")
                
                if index is not None:
                    # Clamp index to avoid OOB crashes
                    safe_idx = max(0, min(index, player.queue.count))
                    player.queue.put_at(safe_idx, playable)
                else:
                    await player.queue.put_wait(playable)
                
                dur = self._fmt_duration(playable)
                msg = f"✅ Added **{playable.title}** `({dur})`"
                if index is not None:
                    actual = safe_idx
                    msg += f" — playing next" if actual == 0 else f" at position **{actual}** in queue"
                    if actual != index:
                        msg += f" *(requested {index}, clamped to {actual})*"
                else:
                    msg += " to the queue"
                await interaction.followup.send(msg)

                if not player.playing:
                    await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))
                
                if getattr(player, "ui_view", None):
                    await player.ui_view.update_player_message()
                    
                return # Don't fall through
        else:
            self._clear_queue_busy(player)
                
        if not is_spotify:
            # Normal Search
            if self._is_url(query):
                tracks: wavelink.Search = await self._safe_wavelink_search(query)
            else:
                # Prefer YouTube Music search for cleaner audio sources by default.
                tracks: wavelink.Search = await self._safe_wavelink_search(
                    query,
                    source=wavelink.TrackSource.YouTubeMusic,
                )
            if not tracks:
                await interaction.followup.send(f"Could not find any songs for `{query}`.")
                return

            if isinstance(tracks, wavelink.Playlist):
                reversed_tracks = list(reversed(tracks.tracks))
                if index is not None:
                    added = 0
                    for track in reversed_tracks:
                        safe_idx = max(0, min(index + added, player.queue.count))
                        player.queue.put_at(safe_idx, track)
                        added += 1
                else:
                    for track in reversed_tracks:
                        await player.queue.put_wait(track)
                    added = len(reversed_tracks)
                await interaction.followup.send(f"Added the playlist **{tracks.name}** ({added} songs, Newest First) to the queue.")
            else:
                track: wavelink.Playable = self._pick_playable_candidate(list(tracks)) or tracks[0]
                if index is not None:
                    safe_idx = max(0, min(index, player.queue.count))
                    player.queue.put_at(safe_idx, track)
                else:
                    await player.queue.put_wait(track)
                
                dur = self._fmt_duration(track)
                msg = f"Added **{track.title}** `({dur})`"
                if index is not None:
                    actual = safe_idx
                    msg += f" — playing next" if actual == 0 else f" at position **{actual}** in queue"
                    if actual != index:
                        msg += f" *(requested {index}, clamped to {actual})*"
                else:
                    msg += " to the queue"
                await interaction.followup.send(msg)

            if not player.playing:
                await player.play(player.queue.get(), volume=int(getattr(player, "volume", 30) or 30))
                
            if getattr(player, "ui_view", None):
                await player.ui_view.update_player_message()
            
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
