import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import wavelink
import logging
import os
import re
import json
import sqlite3
import time
import datetime
import difflib
import math
import textwrap
from typing import cast, Optional, Any

from utils.patched_player import DAVECompatiblePlayer

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


class LyricsPaginatorView(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed], *, owner_id: int):
        super().__init__(timeout=180)
        self.embeds = embeds
        self.owner_id = int(owner_id)
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self) -> None:
        has_many = len(self.embeds) > 1
        self.prev_page.disabled = not has_many or self.index <= 0
        self.next_page.disabled = not has_many or self.index >= len(self.embeds) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0)) == self.owner_id:
            return True
        try:
            await interaction.response.send_message(
                "Only the person who requested these lyrics can use this paginator.",
                ephemeral=True,
            )
        except Exception:
            pass
        return False

    async def on_timeout(self) -> None:
        self.prev_page.disabled = True
        self.next_page.disabled = True
        self.close.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Prev")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index > 0:
            self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Next")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index < len(self.embeds) - 1:
            self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except Exception:
            pass


class Music(commands.Cog):
    _TRACK_VERSION_PATTERNS: dict[str, tuple[str, ...]] = {
        "live": (r"\blive\b", r"\bunplugged\b", r"\bsession\b"),
        "cover": (r"\bcover\b",),
        "karaoke": (r"\bkaraoke\b",),
        "instrumental": (r"\binstrumental\b",),
        "acoustic": (r"\bacoustic\b", r"\bstripped\b"),
        "remix": (r"\bremix\b", r"\brework\b", r"\bvip\b", r"\bedit\b", r"\boriginal mix\b", r"\bextended mix\b"),
        "nightcore": (r"\bnightcore\b",),
        "slowed": (r"\bslowed\b", r"\breverb\b", r"\bchopped\b", r"\bsped up\b"),
        "tiktok": (r"\btiktok\b",),
    }

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._player_action_locks: dict[int, asyncio.Lock] = {}
        self._manual_advance_until: dict[int, float] = {}
        self._exception_streak: dict[int, int] = {}
        self._exception_last_at: dict[int, float] = {}
        self._playlist_load_tasks: dict[int, asyncio.Task] = {}
        self._queue_busy_reason: dict[int, str] = {}
        self._awaiting_autoplay: dict[int, asyncio.Event] = {}
        self._cache_db_path = os.path.join(_DATA_DIR, "music_bot.db")
        self._resolved_links_cache_enabled = (
            os.getenv("RESOLVED_TRACK_CACHE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        )
        ttl_raw = os.getenv("RESOLVED_TRACK_CACHE_TTL_SECONDS", "2592000").strip()
        try:
            self._resolved_links_cache_ttl_seconds = max(0, int(ttl_raw))
        except ValueError:
            self._resolved_links_cache_ttl_seconds = 2_592_000
        self._ensure_resolved_links_cache_table()
        self._resolved_links_cache = self._load_resolved_links_cache()
        self._resolver_rrf_k = self._env_int("TRACK_RESOLVER_RRF_K", 20, minimum=1)
        self._resolver_rrf_scale = self._env_float("TRACK_RESOLVER_RRF_SCALE", 320.0, minimum=0.0)
        self._resolver_per_query_limit = self._env_int("TRACK_RESOLVER_PER_QUERY_LIMIT", 12, minimum=1)
        self._resolver_max_merged_candidates = self._env_int("TRACK_RESOLVER_MAX_CANDIDATES", 60, minimum=15)
        self._resolver_min_cache_score = self._env_float("TRACK_RESOLVER_MIN_CACHE_SCORE", 58.0)
        self._resolver_min_cache_margin = self._env_float("TRACK_RESOLVER_MIN_CACHE_MARGIN", 5.0, minimum=0.0)
        self._ml_reranker_enabled = self._env_bool("TRACK_RESOLVER_ML_RERANK_ENABLED", False)
        self._ml_reranker_model_name = os.getenv(
            "TRACK_RESOLVER_ML_RERANK_MODEL",
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
        ).strip() or "cross-encoder/ms-marco-MiniLM-L-6-v2"
        self._ml_reranker_weight = self._env_float("TRACK_RESOLVER_ML_RERANK_WEIGHT", 22.0, minimum=0.0)
        self._ml_reranker_max_candidates = self._env_int(
            "TRACK_RESOLVER_ML_MAX_CANDIDATES",
            30,
            minimum=5,
            maximum=60,
        )
        self._ml_reranker: Any = None
        self._ml_reranker_load_failed = False
        self._ml_reranker_lock = asyncio.Lock()
        self._live_lyrics_tasks: dict[int, asyncio.Task] = {}

    def cog_unload(self):
        for task in list(self._live_lyrics_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._live_lyrics_tasks.clear()

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(
        name: str,
        default: int,
        *,
        minimum: Optional[int] = None,
        maximum: Optional[int] = None,
    ) -> int:
        raw = (os.getenv(name) or "").strip()
        try:
            value = int(raw) if raw else default
        except ValueError:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    @staticmethod
    def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
        raw = (os.getenv(name) or "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

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
        duration_ms: Optional[int] = None,
        isrc: Optional[str] = None,
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
        if title:
            add(title)
        if artist:
            add(artist)
        if duration_ms and int(duration_ms) > 0:
            add(f"duration:{int(duration_ms) // 1000}")
        if isrc:
            add(f"isrc:{str(isrc).strip().upper()}")

        return keys

    def _resolved_links_db_connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._cache_db_path), exist_ok=True)
        conn = sqlite3.connect(self._cache_db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_resolved_links_cache_table(self) -> None:
        if not self._resolved_links_cache_enabled:
            return
        try:
            with self._resolved_links_db_connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS resolved_track_cache (
                        cache_key TEXT PRIMARY KEY,
                        uri TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_resolved_track_cache_updated_at
                    ON resolved_track_cache (updated_at)
                    """
                )
                conn.commit()
        except Exception as exc:
            logging.warning("Failed ensuring resolved_track_cache table: %s", exc)

    def _delete_resolved_links_entries(self, keys: list[str]) -> None:
        if not keys:
            return
        try:
            with self._resolved_links_db_connect() as conn:
                conn.executemany(
                    "DELETE FROM resolved_track_cache WHERE cache_key = ?",
                    [(key,) for key in keys],
                )
                conn.commit()
        except Exception as exc:
            logging.warning("Failed deleting resolved link cache entries: %s", exc)

    def _upsert_resolved_links_entries(self, rows: list[tuple[str, str, int]]) -> None:
        if not rows:
            return
        try:
            with self._resolved_links_db_connect() as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO resolved_track_cache (cache_key, uri, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
        except Exception as exc:
            logging.warning("Failed writing resolved link cache entries: %s", exc)

    def _load_resolved_links_cache(self) -> dict[str, dict[str, object]]:
        if not self._resolved_links_cache_enabled:
            return {}

        parsed: dict[str, dict[str, object]] = {}
        now = int(time.time())
        stale_keys: list[str] = []
        try:
            with self._resolved_links_db_connect() as conn:
                rows = conn.execute("SELECT cache_key, uri, updated_at FROM resolved_track_cache").fetchall()
        except Exception as exc:
            logging.warning("Failed reading resolved link cache from SQLite: %s", exc)
            return {}

        for row in rows:
            key = self._normalize_cache_key(str(row["cache_key"] or ""))
            uri = str(row["uri"] or "").strip()
            try:
                updated_at = int(row["updated_at"] or now)
            except (TypeError, ValueError):
                updated_at = now
            if not key or not uri:
                if key:
                    stale_keys.append(key)
                continue
            if self._resolved_links_cache_ttl_seconds > 0 and (now - updated_at) > self._resolved_links_cache_ttl_seconds:
                stale_keys.append(key)
                continue
            parsed[key] = {"uri": uri, "updated_at": updated_at}
        if stale_keys:
            self._delete_resolved_links_entries(stale_keys)

        return parsed

    def _save_resolved_links_cache(self) -> None:
        if not self._resolved_links_cache_enabled:
            return
        rows: list[tuple[str, str, int]] = []
        for key, entry in self._resolved_links_cache.items():
            if not isinstance(entry, dict):
                continue
            uri = str(entry.get("uri", "")).strip()
            if not uri:
                continue
            try:
                updated_at = int(entry.get("updated_at", int(time.time())))
            except (TypeError, ValueError):
                updated_at = int(time.time())
            rows.append((key, uri, updated_at))
        self._upsert_resolved_links_entries(rows)

    def _get_cached_resolved_link(self, keys: list[str]) -> Optional[str]:
        if not self._resolved_links_cache_enabled or not keys:
            return None

        now = int(time.time())
        stale_keys: list[str] = []

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
                stale_keys.append(key)
                continue

            if self._resolved_links_cache_ttl_seconds > 0 and updated_at and (now - updated_at) > self._resolved_links_cache_ttl_seconds:
                self._resolved_links_cache.pop(key, None)
                stale_keys.append(key)
                continue

            if stale_keys:
                self._delete_resolved_links_entries(stale_keys)
            return uri

        if stale_keys:
            self._delete_resolved_links_entries(stale_keys)
        return None

    def _set_cached_resolved_link(self, keys: list[str], uri: Optional[str]) -> None:
        if not self._resolved_links_cache_enabled or not keys or not uri:
            return

        uri = uri.strip()
        if not uri:
            return

        now = int(time.time())
        entry = {
            "uri": uri,
            "updated_at": now,
        }
        rows: list[tuple[str, str, int]] = []
        for key in keys:
            existing = self._resolved_links_cache.get(key)
            if not isinstance(existing, dict) or existing.get("uri") != uri:
                self._resolved_links_cache[key] = entry
                rows.append((key, uri, now))
        if rows:
            self._upsert_resolved_links_entries(rows)

    def _remove_cached_resolved_link(self, keys: list[str]) -> None:
        if not self._resolved_links_cache_enabled or not keys:
            return

        deleted_keys: list[str] = []
        for key in keys:
            if key in self._resolved_links_cache:
                self._resolved_links_cache.pop(key, None)
                deleted_keys.append(key)
        if deleted_keys:
            self._delete_resolved_links_entries(deleted_keys)

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
                player = await interaction.user.voice.channel.connect(cls=DAVECompatiblePlayer)
                created_player = True
            except AttributeError:
                await self._reply(interaction, "Join a voice channel first, then run `/music`.", ephemeral=True)
                return
            except discord.ClientException:
                await self._reply(interaction, "I was unable to join this voice channel.", ephemeral=True)
                return

        if created_player:
            try:
                await player.set_volume(15)
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
    def _split_lyrics_query(raw: str) -> tuple[str, Optional[str]]:
        query = " ".join(str(raw or "").split()).strip()
        if not query:
            return "", None

        separators = [" - ", " — ", " by "]
        for separator in separators:
            if separator in query:
                left, right = query.split(separator, 1)
                title = left.strip()
                artist = right.strip()
                if title and artist:
                    return title, artist
        return query, None

    @staticmethod
    def _extract_spotify_track_id(*values: Optional[str]) -> Optional[str]:
        for value in values:
            if not value:
                continue
            text = str(value).strip()
            match = re.search(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})", text)
            if match:
                return match.group(1)
            if re.fullmatch(r"[A-Za-z0-9]{22}", text):
                return text
        return None

    @staticmethod
    def _extract_track_album_name(track: Any) -> Optional[str]:
        source_album = getattr(track, "_source_album", None)
        if isinstance(source_album, str) and source_album.strip():
            return source_album.strip()

        album = getattr(track, "album", None)

        if isinstance(album, str) and album.strip():
            return album.strip()

        if isinstance(album, dict):
            for key in ("name", "title"):
                value = album.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        album_name = getattr(album, "name", None)
        if isinstance(album_name, str) and album_name.strip():
            return album_name.strip()

        extras = getattr(track, "extras", None)
        if isinstance(extras, dict):
            for key in ("album", "album_name", "spotify_album"):
                value = extras.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        else:
            for key in ("album", "album_name", "spotify_album"):
                value = getattr(extras, key, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        return None

    @staticmethod
    def _extract_track_spotify_id(track: Any) -> Optional[str]:
        annotated_id = Music._extract_spotify_track_id(getattr(track, "_spotify_track_id", None))
        if annotated_id:
            return annotated_id

        direct = Music._extract_spotify_track_id(
            getattr(track, "uri", None),
            getattr(track, "identifier", None),
        )
        if direct:
            return direct

        source_raw = str(getattr(track, "source", "") or getattr(track, "source_name", "")).strip().lower()
        identifier = str(getattr(track, "identifier", "") or "").strip()
        if "spotify" in source_raw and re.fullmatch(r"[A-Za-z0-9]{22}", identifier):
            return identifier

        extras = getattr(track, "extras", None)
        if isinstance(extras, dict):
            return Music._extract_spotify_track_id(
                extras.get("spotify_track_id"),
                extras.get("spotify_id"),
                extras.get("track_id"),
            )
        return Music._extract_spotify_track_id(
            getattr(extras, "spotify_track_id", None),
            getattr(extras, "spotify_id", None),
            getattr(extras, "track_id", None),
        )

    @staticmethod
    def _extract_track_source_title(track: Any) -> Optional[str]:
        value = getattr(track, "_source_title", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _extract_track_source_artist(track: Any) -> Optional[str]:
        value = getattr(track, "_source_artist", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @classmethod
    def _lyrics_metadata_match(
        cls,
        *,
        expected_title: Optional[str],
        expected_artist: Optional[str],
        actual_title: Optional[str],
        actual_artist: Optional[str],
    ) -> bool:
        if not expected_title or not actual_title:
            return False

        title_sim = cls._text_similarity(expected_title, actual_title)
        title_overlap = cls._token_overlap(expected_title, actual_title)
        title_f1 = cls._token_f1(expected_title, actual_title)
        title_ok = (
            title_sim >= 0.72
            or title_overlap >= 0.80
            or title_f1 >= 0.80
            or (title_sim >= 0.62 and (title_overlap >= 0.62 or title_f1 >= 0.62))
        )
        if not title_ok:
            return False

        if not expected_artist:
            return True

        artist_sim = cls._text_similarity(expected_artist, actual_artist)
        artist_overlap = cls._token_overlap(expected_artist, actual_artist)
        artist_set = cls._artist_similarity(expected_artist, actual_artist)
        norm_expected_artist = cls._normalize_match_string(expected_artist)
        norm_actual_artist = cls._normalize_match_string(actual_artist)
        artist_ok = (
            artist_sim >= 0.62
            or artist_overlap >= 0.65
            or artist_set >= 0.45
            or (
                norm_expected_artist
                and norm_actual_artist
                and (
                    norm_expected_artist in norm_actual_artist
                    or norm_actual_artist in norm_expected_artist
                )
            )
        )
        return artist_ok

    @staticmethod
    def _annotate_playable_metadata(
        playable: Optional[wavelink.Playable],
        *,
        album: Optional[str] = None,
        spotify_track_id: Optional[str] = None,
        source_title: Optional[str] = None,
        source_artist: Optional[str] = None,
    ) -> None:
        if not playable:
            return
        if isinstance(album, str) and album.strip():
            try:
                setattr(playable, "_source_album", album.strip())
            except Exception:
                pass
        if isinstance(spotify_track_id, str) and spotify_track_id.strip():
            try:
                setattr(playable, "_spotify_track_id", spotify_track_id.strip())
            except Exception:
                pass
        if isinstance(source_title, str) and source_title.strip():
            try:
                setattr(playable, "_source_title", source_title.strip())
            except Exception:
                pass
        if isinstance(source_artist, str) and source_artist.strip():
            try:
                setattr(playable, "_source_artist", source_artist.strip())
            except Exception:
                pass

    @staticmethod
    def _split_text_pages(text: str, max_chars: int = 3500) -> list[str]:
        if not text:
            return []
        max_chars = max(800, min(3900, int(max_chars)))
        lines = text.splitlines()
        pages: list[str] = []
        current: list[str] = []
        current_len = 0

        for raw_line in lines:
            line = raw_line.rstrip()
            extra = len(line) + 1
            if current and current_len + extra > max_chars:
                pages.append("\n".join(current).strip())
                current = [line]
                current_len = len(line) + 1
            else:
                current.append(line)
                current_len += extra

        if current:
            pages.append("\n".join(current).strip())

        if not pages:
            pages = [text[:max_chars]]
        return pages

    @staticmethod
    def _parse_synced_lyrics_lines(raw: Optional[str]) -> list[tuple[int, str]]:
        text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            return []

        parsed: list[tuple[int, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            tags = re.findall(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]", line)
            if not tags:
                continue
            lyric_text = re.sub(r"(?:\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\])+", "", line).strip()
            if not lyric_text:
                continue

            for mm_raw, ss_raw, ms_raw in tags:
                mm = int(mm_raw)
                ss = int(ss_raw)
                frac = (ms_raw or "").ljust(3, "0")[:3]
                ms = int(frac) if frac else 0
                start_ms = (mm * 60 + ss) * 1000 + ms
                parsed.append((max(0, start_ms), lyric_text))

        parsed.sort(key=lambda item: item[0])
        deduped: list[tuple[int, str]] = []
        for start_ms, line in parsed:
            if deduped and deduped[-1][0] == start_ms and deduped[-1][1] == line:
                continue
            deduped.append((start_ms, line))
        return deduped

    @staticmethod
    def _find_active_synced_index(lines: list[tuple[int, str]], position_ms: int) -> int:
        if not lines:
            return -1
        idx = -1
        for i, (start_ms, _) in enumerate(lines):
            if start_ms <= position_ms:
                idx = i
            else:
                break
        return idx

    @staticmethod
    def _display_player_position_ms(player: Optional[wavelink.Player], *, anchor_ms: int, anchor_at: float) -> int:
        if not player:
            return max(0, anchor_ms)
        base = int(getattr(player, "position", 0) or 0)
        if base > anchor_ms:
            return max(0, base)
        if bool(getattr(player, "paused", False)) or not bool(getattr(player, "playing", False)):
            return max(0, anchor_ms)
        elapsed = int((time.monotonic() - anchor_at) * 1000)
        return max(0, anchor_ms + elapsed)

    def _build_live_lyrics_embed(
        self,
        *,
        title: str,
        artist: Optional[str],
        lines: list[tuple[int, str]],
        active_index: int,
    ) -> discord.Embed:
        safe_title = discord.utils.escape_markdown(title or "Unknown Title", as_needed=True)
        safe_artist = discord.utils.escape_markdown(artist or "Unknown Artist", as_needed=True)
        heading = f"{safe_title} - {safe_artist}" if safe_artist else safe_title

        window_size = 15
        # Keep a strict fixed-size viewport to avoid frame-to-frame jitter.
        # Long lyric lines can wrap, but the rendered viewport remains fixed.
        width = 57
        content_width = width - 2
        reset = "\u001b[0m"
        active_color = "\u001b[1;36m"

        def _clip_ellipsis(text: str, limit: int) -> str:
            if len(text) <= limit:
                return text
            if limit <= 1:
                return text[:limit]
            return text[: limit - 1].rstrip() + "…"

        def _center_visible(text: str, target: int) -> str:
            if len(text) >= target:
                return text
            total_pad = target - len(text)
            left = total_pad // 2
            right = total_pad - left
            return f"{' ' * left}{text}{' ' * right}"

        def _wrap_clean(text: str, limit: int) -> list[str]:
            if limit <= 1:
                return [text[:1]] if text else [""]
            wrapped = textwrap.wrap(
                text,
                width=limit,
                break_long_words=True,
                break_on_hyphens=False,
            )
            return wrapped or [""]

        def _format_row(text: str, *, is_active: bool) -> str:
            clean = " ".join(text.split())
            if is_active:
                inner = content_width - 4  # "▶ " + text + " ◀"
                clipped = _clip_ellipsis(clean, inner)
                centered = _center_visible(clipped, inner)
                return f"{active_color} ▶ {centered} ◀ {reset}"
            clipped = _clip_ellipsis(clean, content_width)
            centered = _center_visible(clipped, content_width)
            return f" {centered} "

        wrapped_rows: list[tuple[str, bool]] = []
        for idx, (_, line) in enumerate(lines):
            text = discord.utils.escape_markdown(line, as_needed=True)
            clean = " ".join(text.split())
            if idx == active_index:
                inner = content_width - 4
                for chunk_idx, chunk in enumerate(_wrap_clean(clean, inner)):
                    centered = _center_visible(_clip_ellipsis(chunk, inner), inner)
                    if chunk_idx == 0:
                        row = f"{active_color} ▶ {centered} ◀ {reset}"
                    else:
                        row = f"{active_color}   {centered}   {reset}"
                    wrapped_rows.append((row, True))
            else:
                for chunk in _wrap_clean(clean, content_width):
                    wrapped_rows.append((_format_row(chunk, is_active=False), False))

        if not wrapped_rows:
            wrapped_rows.append((_format_row("No synced line available yet", is_active=False), False))

        active_row_idx = next((i for i, (_, is_active_row) in enumerate(wrapped_rows) if is_active_row), None)
        if active_row_idx is None:
            start = 0
        else:
            start = max(0, active_row_idx - (window_size // 2))
        max_start = max(0, len(wrapped_rows) - window_size)
        start = min(start, max_start)
        selected = wrapped_rows[start : start + window_size]
        rows = [row for row, _ in selected]

        blank_row = _format_row("", is_active=False)
        if len(rows) < window_size:
            missing = window_size - len(rows)
            top_pad = missing // 2
            bottom_pad = missing - top_pad
            rows = ([blank_row] * top_pad) + rows + ([blank_row] * bottom_pad)

        desc = "```ansi\n" + "\n".join(rows) + "\n```"
        embed = discord.Embed(title=heading, description=desc)
        return embed

    def _build_lyrics_embeds(
        self,
        *,
        title: str,
        artist: Optional[str],
        lyrics: str,
        source: str,
        synced: bool = False,
        source_url: Optional[str] = None,
    ) -> list[discord.Embed]:
        safe_title = discord.utils.escape_markdown(title or "Unknown Title", as_needed=True)
        safe_artist = discord.utils.escape_markdown(artist or "Unknown Artist", as_needed=True)
        heading = f"{safe_title} - {safe_artist}" if safe_artist else safe_title
        pages = self._split_text_pages(lyrics, max_chars=3800)
        embeds: list[discord.Embed] = []
        total_pages = len(pages)

        for idx, chunk in enumerate(pages, start=1):
            title_text = heading if total_pages == 1 else f"{heading} ({idx}/{total_pages})"
            embed = discord.Embed(title=title_text, description=chunk or "_(No lyrics text)_")
            embeds.append(embed)

        return embeds

    async def _send_lyrics(
        self,
        interaction: discord.Interaction,
        *,
        query: Optional[str] = None,
        ephemeral: bool = False,
    ) -> None:
        player: Optional[wavelink.Player] = None
        title = ""
        artist: Optional[str] = None
        duration_ms: Optional[int] = None
        album: Optional[str] = None
        spotify_track_id: Optional[str] = None
        current_title: Optional[str] = None
        current_artist: Optional[str] = None
        source_title: Optional[str] = None
        source_artist: Optional[str] = None

        if query and query.strip():
            raw_query = " ".join(str(query).split()).strip()
            spotify_track_id = self._extract_spotify_track_id(raw_query)
            title, artist = self._split_lyrics_query(raw_query)
        else:
            if not interaction.guild:
                await self._reply(interaction, "Provide a song query when used outside a server.", ephemeral=True)
                return

            player = cast(Optional[wavelink.Player], interaction.guild.voice_client)
            current = getattr(player, "current", None) if player else None
            if not current:
                await self._reply(
                    interaction,
                    "No current track. Start playback first, then use the 🎤 Lyrics button in `/music`.",
                    ephemeral=True,
                )
                return

            current_title = str(getattr(current, "title", "") or "").strip()
            current_artist = str(getattr(current, "author", "") or "").strip()
            current_artist = re.sub(r"\s*-\s*topic\s*$", "", current_artist, flags=re.IGNORECASE).strip() or None
            source_title = self._extract_track_source_title(current)
            source_artist = self._extract_track_source_artist(current)
            source_artist = re.sub(r"\s*-\s*topic\s*$", "", str(source_artist or ""), flags=re.IGNORECASE).strip() or None

            if source_title and not self._lyrics_metadata_match(
                expected_title=source_title,
                expected_artist=source_artist,
                actual_title=current_title,
                actual_artist=current_artist,
            ):
                safe_source_title = discord.utils.escape_markdown(source_title, as_needed=True)
                safe_source_artist = discord.utils.escape_markdown(source_artist or "Unknown Artist", as_needed=True)
                safe_current_title = discord.utils.escape_markdown(current_title or "Unknown Title", as_needed=True)
                safe_current_artist = discord.utils.escape_markdown(current_artist or "Unknown Artist", as_needed=True)
                await self._reply(
                    interaction,
                    (
                        "I detected a resolver mismatch, so I stopped lyrics to avoid showing the wrong song.\n"
                        f"Expected: **{safe_source_title}** — **{safe_source_artist}**\n"
                        f"Now playing: **{safe_current_title}** — **{safe_current_artist}**\n"
                        "Skip/requeue this track, then try lyrics again."
                    ),
                    ephemeral=True,
                )
                return

            title = source_title or current_title
            artist = source_artist or current_artist
            album = self._extract_track_album_name(current)
            spotify_track_id = self._extract_track_spotify_id(current)
            try:
                duration_ms = int(getattr(current, "length", 0) or 0)
            except (TypeError, ValueError):
                duration_ms = None

        if not title:
            await self._reply(interaction, "Please provide a valid song title.", ephemeral=True)
            return

        if not await self._safe_defer(interaction, thinking=True, ephemeral=ephemeral):
            return

        if spotify_track_id and (not artist or self._is_url(title)):
            try:
                from utils.resolver import resolver as spotify_resolver

                track_info = await spotify_resolver.get_single_track(f"https://open.spotify.com/track/{spotify_track_id}")
            except Exception as exc:
                logging.debug("Failed Spotify metadata pre-resolve for lyrics (%s): %s", spotify_track_id, exc)
                track_info = None

            if track_info:
                title = str(getattr(track_info, "title", "") or "").strip() or title
                artist = str(getattr(track_info, "artist", "") or "").strip() or artist
                album = str(getattr(track_info, "album", "") or "").strip() or album
                try:
                    meta_duration = int(getattr(track_info, "duration_ms", 0) or 0)
                    if meta_duration > 0:
                        duration_ms = meta_duration
                except (TypeError, ValueError):
                    pass

        from utils.lyrics import lyrics_resolver

        result = await lyrics_resolver.get_lyrics(
            title=title,
            artist=artist,
            duration_ms=duration_ms,
            album=album,
            spotify_track_id=spotify_track_id,
        )
        if not result:
            hint = f" for **{title}**" + (f" by **{artist}**" if artist else "")
            await interaction.followup.send(
                f"Sorry, I couldn't find lyrics{hint} 😭",
                ephemeral=ephemeral,
            )
            return

        if not self._lyrics_metadata_match(
            expected_title=title,
            expected_artist=artist,
            actual_title=result.title,
            actual_artist=result.artist,
        ):
            safe_expected_title = discord.utils.escape_markdown(title or "Unknown Title", as_needed=True)
            safe_expected_artist = discord.utils.escape_markdown(artist or "Unknown Artist", as_needed=True)
            safe_result_title = discord.utils.escape_markdown(result.title or "Unknown Title", as_needed=True)
            safe_result_artist = discord.utils.escape_markdown(result.artist or "Unknown Artist", as_needed=True)
            await interaction.followup.send(
                (
                    "Lyrics lookup returned a different song, so I skipped it.\n"
                    f"Requested: **{safe_expected_title}** — **{safe_expected_artist}**\n"
                    f"Found: **{safe_result_title}** — **{safe_result_artist}**"
                ),
                ephemeral=ephemeral,
            )
            return

        synced_lines = self._parse_synced_lyrics_lines(result.synced_lyrics or result.lyrics)
        current_track = getattr(player, "current", None) if player else None
        if player and current_track and synced_lines and len(synced_lines) > 1 and not query:
            track_key = self._track_identity(current_track)
            anchor_ms = int(getattr(player, "position", 0) or 0)
            anchor_at = time.monotonic()
            active_index = self._find_active_synced_index(synced_lines, anchor_ms)
            live_embed = self._build_live_lyrics_embed(
                title=result.title,
                artist=result.artist,
                lines=synced_lines,
                active_index=active_index,
            )
            sent = await interaction.followup.send(embed=live_embed, ephemeral=ephemeral, wait=True)

            task = asyncio.create_task(
                self._run_live_lyrics_updates(
                    message=sent,
                    player=player,
                    track_key=track_key,
                    lines=synced_lines,
                    title=result.title,
                    artist=result.artist,
                    anchor_ms=anchor_ms,
                    anchor_at=anchor_at,
                    initial_index=active_index,
                )
            )
            self._live_lyrics_tasks[id(sent)] = task
            return

        embeds = self._build_lyrics_embeds(
            title=result.title,
            artist=result.artist,
            lyrics=result.lyrics,
            source=result.source,
            synced=result.synced,
            source_url=result.source_url,
        )

        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0], ephemeral=ephemeral)
            return

        view = LyricsPaginatorView(embeds, owner_id=int(interaction.user.id))
        sent = await interaction.followup.send(embed=embeds[0], view=view, ephemeral=ephemeral, wait=True)
        view.message = sent

    async def _send_current_lyrics(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = True,
    ) -> None:
        await self._send_lyrics(interaction, query=None, ephemeral=ephemeral)

    async def _run_live_lyrics_updates(
        self,
        *,
        message: discord.Message,
        player: wavelink.Player,
        track_key: str,
        lines: list[tuple[int, str]],
        title: str,
        artist: Optional[str],
        anchor_ms: int,
        anchor_at: float,
        initial_index: int = -2,
    ) -> None:
        last_idx = initial_index
        poll_interval = 0.4
        try:
            while True:
                current_track = getattr(player, "current", None)
                if not current_track or self._track_identity(current_track) != track_key:
                    break

                base_pos = int(getattr(player, "position", 0) or 0)
                if base_pos > anchor_ms:
                    anchor_ms = base_pos
                    anchor_at = time.monotonic()

                position_ms = self._display_player_position_ms(
                    player,
                    anchor_ms=anchor_ms,
                    anchor_at=anchor_at,
                )
                idx = self._find_active_synced_index(lines, position_ms)
                # Clamp forward jumps to avoid jarring viewport shifts when
                # Lavalink reports a transient position spike.
                if last_idx >= 0 and idx > last_idx + 3:
                    idx = last_idx + 1
                if idx != last_idx:
                    last_idx = idx
                    embed = self._build_live_lyrics_embed(
                        title=title,
                        artist=artist,
                        lines=lines,
                        active_index=idx,
                    )
                    try:
                        await message.edit(embed=embed)
                    except discord.NotFound:
                        break
                    except discord.HTTPException:
                        break
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            raise
        finally:
            self._live_lyrics_tasks.pop(id(message), None)

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
            add(f"{artist} - {title}")
            add(f"\"{title}\" \"{artist}\"")

            stripped_title = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", title).strip()
            if stripped_title and stripped_title != title:
                add(f"{stripped_title} {artist}")
                add(f"\"{stripped_title}\" \"{artist}\"")

        base_snapshot = list(variants)
        for item in base_snapshot:
            add(item.replace("’", "'"))
            add(item.replace("'", ""))
            add(item.replace("&", "and"))
            add(item.replace("-", " "))

        return variants[:10]

    @staticmethod
    def _normalize_match_string(raw: Optional[str]) -> str:
        text = str(raw or "").lower()
        text = text.replace("’", "'")
        text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
        text = re.sub(
            r"\b(official|audio|video|lyrics?|lyric|remastered|remaster|hd|4k|hq|visualizer)\b",
            " ",
            text,
        )
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return " ".join(text.split())

    @staticmethod
    def _text_similarity(a: Optional[str], b: Optional[str]) -> float:
        left = Music._normalize_match_string(a)
        right = Music._normalize_match_string(b)
        if not left or not right:
            return 0.0
        return difflib.SequenceMatcher(None, left, right).ratio()

    @staticmethod
    def _token_overlap(a: Optional[str], b: Optional[str]) -> float:
        left_tokens = set(Music._normalize_match_string(a).split())
        right_tokens = set(Music._normalize_match_string(b).split())
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens), 1)

    @staticmethod
    def _token_f1(a: Optional[str], b: Optional[str]) -> float:
        left_tokens = set(Music._normalize_match_string(a).split())
        right_tokens = set(Music._normalize_match_string(b).split())
        if not left_tokens or not right_tokens:
            return 0.0
        overlap = len(left_tokens & right_tokens)
        precision = overlap / len(right_tokens)
        recall = overlap / len(left_tokens)
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @staticmethod
    def _char_ngram_cosine(a: Optional[str], b: Optional[str], n: int = 3) -> float:
        left = Music._normalize_match_string(a).replace(" ", "")
        right = Music._normalize_match_string(b).replace(" ", "")
        if len(left) < n or len(right) < n:
            return 0.0

        left_counts: dict[str, int] = {}
        right_counts: dict[str, int] = {}
        for i in range(len(left) - n + 1):
            gram = left[i : i + n]
            left_counts[gram] = left_counts.get(gram, 0) + 1
        for i in range(len(right) - n + 1):
            gram = right[i : i + n]
            right_counts[gram] = right_counts.get(gram, 0) + 1

        overlap = set(left_counts) & set(right_counts)
        if not overlap:
            return 0.0

        dot = sum(left_counts[g] * right_counts[g] for g in overlap)
        left_norm = math.sqrt(sum(v * v for v in left_counts.values()))
        right_norm = math.sqrt(sum(v * v for v in right_counts.values()))
        if left_norm <= 0 or right_norm <= 0:
            return 0.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _split_artist_tokens(raw: Optional[str]) -> set[str]:
        text = Music._normalize_match_string(raw)
        if not text:
            return set()
        parts = re.split(r"\b(?:feat|ft|featuring|with|and|x)\b|[,&/+]", text)
        tokens = {p.strip() for p in parts if p and p.strip()}
        return {token for token in tokens if len(token) > 1}

    @staticmethod
    def _artist_similarity(expected: Optional[str], actual: Optional[str]) -> float:
        expected_tokens = Music._split_artist_tokens(expected)
        actual_tokens = Music._split_artist_tokens(actual)
        if not expected_tokens or not actual_tokens:
            return 0.0
        overlap = len(expected_tokens & actual_tokens)
        return overlap / len(expected_tokens | actual_tokens)

    @classmethod
    def _extract_version_tags(cls, raw: Optional[str]) -> set[str]:
        text = cls._normalize_match_string(raw)
        if not text:
            return set()
        tags: set[str] = set()
        for key, patterns in cls._TRACK_VERSION_PATTERNS.items():
            if any(re.search(pattern, text) for pattern in patterns):
                tags.add(key)
        return tags

    @staticmethod
    def _source_rrf_weight(source: Optional[wavelink.TrackSource]) -> float:
        if source == wavelink.TrackSource.YouTubeMusic:
            return 1.0
        if source == wavelink.TrackSource.YouTube:
            return 0.9
        if source == wavelink.TrackSource.SoundCloud:
            return 0.8
        return 0.72

    @staticmethod
    def _query_variant_weight(index: int) -> float:
        if index <= 0:
            return 1.0
        return max(0.55, 1.0 - (0.07 * index))

    @staticmethod
    def _candidate_key(track: wavelink.Playable) -> str:
        identifier = (
            getattr(track, "identifier", None)
            or getattr(track, "uri", None)
            or f"{getattr(track, 'title', '')}|{getattr(track, 'author', '')}|{getattr(track, 'length', 0)}"
        )
        return str(identifier).strip().lower()

    @staticmethod
    def _sigmoid(x: float) -> float:
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 0.0 if x < 0 else 1.0

    async def _get_ml_reranker(self):
        if not self._ml_reranker_enabled or self._ml_reranker_load_failed:
            return None
        if self._ml_reranker is not None:
            return self._ml_reranker

        async with self._ml_reranker_lock:
            if self._ml_reranker is not None:
                return self._ml_reranker
            if self._ml_reranker_load_failed:
                return None

            try:
                from sentence_transformers import CrossEncoder  # type: ignore
            except Exception as exc:
                self._ml_reranker_load_failed = True
                logging.warning(
                    "ML reranker enabled but `sentence-transformers` is unavailable: %s",
                    exc,
                )
                return None

            try:
                self._ml_reranker = await asyncio.to_thread(CrossEncoder, self._ml_reranker_model_name)
                logging.info("Loaded ML reranker model: %s", self._ml_reranker_model_name)
            except Exception as exc:
                self._ml_reranker_load_failed = True
                logging.warning(
                    "Failed to load ML reranker model `%s`: %s",
                    self._ml_reranker_model_name,
                    exc,
                )
                return None

        return self._ml_reranker

    async def _compute_ml_rerank_scores(
        self,
        *,
        title: Optional[str],
        artist: Optional[str],
        candidates: list[wavelink.Playable],
    ) -> dict[str, float]:
        if not self._ml_reranker_enabled:
            return {}
        query = " ".join(part for part in [title, artist] if part and str(part).strip()).strip()
        if not query or not candidates:
            return {}

        model = await self._get_ml_reranker()
        if model is None:
            return {}

        shortlist = candidates[: self._ml_reranker_max_candidates]

        def _predict() -> list[float]:
            pairs = [
                (
                    query,
                    " | ".join(
                        p
                        for p in [
                            str(getattr(track, "title", "") or ""),
                            str(getattr(track, "author", "") or ""),
                            self._fmt_duration(track),
                        ]
                        if p
                    ),
                )
                for track in shortlist
            ]
            raw = model.predict(pairs)
            return [float(item) for item in list(raw)]

        try:
            raw_scores = await asyncio.to_thread(_predict)
        except Exception as exc:
            logging.warning("ML reranker prediction failed; falling back to heuristic scoring: %s", exc)
            return {}

        normalized: dict[str, float] = {}
        for track, raw_score in zip(shortlist, raw_scores):
            normalized[self._candidate_key(track)] = self._sigmoid(raw_score)
        return normalized

    def _score_candidate(
        self,
        track: wavelink.Playable,
        *,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        duration_ms: Optional[int] = None,
        isrc: Optional[str] = None,
        retrieval_score: float = 0.0,
        ml_score: float = 0.0,
    ) -> float:
        score = 0.0
        candidate_title = str(getattr(track, "title", "") or "")
        candidate_author = str(getattr(track, "author", "") or "")

        if title:
            score += self._text_similarity(title, candidate_title) * 42
            score += self._token_overlap(title, candidate_title) * 22
            score += self._token_f1(title, candidate_title) * 22
            score += self._char_ngram_cosine(title, candidate_title) * 14

        if artist:
            score += self._text_similarity(artist, candidate_author) * 22
            score += self._token_overlap(artist, candidate_author) * 15
            score += self._artist_similarity(artist, candidate_author) * 28
            norm_artist = self._normalize_match_string(artist)
            norm_author = self._normalize_match_string(candidate_author)
            if norm_artist and norm_author and (norm_artist in norm_author or norm_author in norm_artist):
                score += 8
            if self._artist_similarity(artist, candidate_author) <= 0 and self._token_overlap(artist, candidate_author) < 0.15:
                score -= 16

        expected_isrc = str(isrc or "").strip().upper()
        candidate_isrc = str(getattr(track, "isrc", "") or "").strip().upper()
        if expected_isrc:
            if candidate_isrc and candidate_isrc == expected_isrc:
                score += 140
            elif candidate_isrc and candidate_isrc != expected_isrc:
                score -= 48

        try:
            expected_duration = int(duration_ms) if duration_ms is not None else 0
        except (TypeError, ValueError):
            expected_duration = 0
        candidate_duration = int(getattr(track, "length", 0) or 0)
        if expected_duration > 0 and candidate_duration > 0:
            delta = abs(candidate_duration - expected_duration)
            if delta <= 2000:
                score += 22
            elif delta <= 5000:
                score += 16
            elif delta <= 10000:
                score += 10
            elif delta <= 20000:
                score += 3
            elif delta >= 60000:
                score -= 25
            elif delta >= 45000:
                score -= 14
        elif candidate_duration <= 0:
            score -= 3

        # Source/quality hints.
        candidate_author_lower = candidate_author.lower()
        if " - topic" in candidate_author_lower:
            score += 6
        if "vevo" in candidate_author_lower:
            score += 4

        requested_tags = self._extract_version_tags(title)
        candidate_tags = self._extract_version_tags(candidate_title)
        # Version labels should never be a hard penalty signal.
        if requested_tags:
            score += 9 * len(requested_tags & candidate_tags)

        if retrieval_score > 0:
            score += retrieval_score * self._resolver_rrf_scale
        if ml_score > 0:
            score += ml_score * self._ml_reranker_weight

        return score

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
        duration_ms: Optional[int] = None,
        isrc: Optional[str] = None,
        matcher=None,
    ) -> Optional[wavelink.Playable]:
        cache_keys = self._build_resolved_link_keys(
            query=query,
            title=title,
            artist=artist,
            duration_ms=duration_ms,
            isrc=isrc,
        )
        cached_uri = self._get_cached_resolved_link(cache_keys)
        if cached_uri:
            cached_result = await self._safe_wavelink_search(cached_uri, suppress_errors=True)
            cached_candidates = self._extract_candidates(cached_result)
            if cached_candidates:
                return self._pick_playable_candidate(
                    cached_candidates,
                    title=title,
                    artist=artist,
                    duration_ms=duration_ms,
                    isrc=isrc,
                    matcher=matcher,
                ) or cached_candidates[0]
            self._remove_cached_resolved_link(cache_keys)

        variants = self._build_query_variants(query, title=title, artist=artist)
        if not variants:
            return None

        sources = (
            wavelink.TrackSource.YouTubeMusic,
            wavelink.TrackSource.YouTube,
            wavelink.TrackSource.SoundCloud,
        )
        search_plan: list[tuple[Optional[wavelink.TrackSource], str, int]] = []
        seen_plan: set[tuple[Optional[wavelink.TrackSource], str]] = set()

        for source in sources:
            for variant_idx, candidate_query in enumerate(variants):
                plan_key = (source, candidate_query)
                if plan_key in seen_plan:
                    continue
                seen_plan.add(plan_key)
                search_plan.append((source, candidate_query, variant_idx))

        # Final generic pass lets Lavalink choose any enabled source manager.
        for variant_idx, candidate_query in enumerate(variants):
            plan_key = (None, candidate_query)
            if plan_key in seen_plan:
                continue
            seen_plan.add(plan_key)
            search_plan.append((None, candidate_query, variant_idx))

        merged_candidates: dict[str, wavelink.Playable] = {}
        retrieval_scores: dict[str, float] = {}
        for source, candidate_query, variant_idx in search_plan:
            kwargs = {"source": source} if source else {}
            result = await self._safe_wavelink_search(candidate_query, suppress_errors=True, **kwargs)
            candidates = self._extract_candidates(result)
            if not candidates:
                continue

            source_weight = self._source_rrf_weight(source)
            query_weight = self._query_variant_weight(variant_idx)
            for rank, candidate in enumerate(candidates[: self._resolver_per_query_limit], start=1):
                key = self._candidate_key(candidate)
                if key not in merged_candidates:
                    merged_candidates[key] = candidate
                retrieval_scores[key] = retrieval_scores.get(key, 0.0) + (
                    (source_weight * query_weight) / (self._resolver_rrf_k + rank)
                )

            # Don't over-query once we already have a broad candidate set.
            if len(merged_candidates) >= self._resolver_max_merged_candidates:
                break

        if merged_candidates:
            merged = list(merged_candidates.values())
            ml_scores = await self._compute_ml_rerank_scores(
                title=title,
                artist=artist,
                candidates=merged,
            )
            playable, top_score, second_score = self._pick_playable_candidate(
                merged,
                title=title,
                artist=artist,
                duration_ms=duration_ms,
                isrc=isrc,
                matcher=matcher,
                retrieval_scores=retrieval_scores,
                ml_scores=ml_scores,
                return_scores=True,
            )
            if playable is None:
                playable = next(iter(merged_candidates.values()))
                top_score = float("-inf")
                second_score = float("-inf")
            playable_uri = str(getattr(playable, "uri", "")).strip()
            margin = top_score - second_score if second_score != float("-inf") else top_score
            if (
                playable_uri
                and top_score >= self._resolver_min_cache_score
                and margin >= self._resolver_min_cache_margin
            ):
                self._set_cached_resolved_link(cache_keys, playable_uri)
            elif playable_uri:
                logging.debug(
                    "Skipping resolver cache write due to low confidence: score=%.2f margin=%.2f query=%r",
                    top_score,
                    margin,
                    query,
                )
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
        duration_ms: Optional[int] = None,
        isrc: Optional[str] = None,
        matcher=None,
        retrieval_scores: Optional[dict[str, float]] = None,
        ml_scores: Optional[dict[str, float]] = None,
        return_scores: bool = False,
    ):
        if not candidates:
            if return_scores:
                return None, float("-inf"), float("-inf")
            return None

        shortlist = list(candidates[:25])

        matcher_boost_id = ""
        if matcher and title and artist:
            matched = matcher.get_best_match(title, artist, shortlist)
            if matched:
                matcher_boost_id = self._track_identity(matched)

        scored: list[tuple[float, wavelink.Playable]] = []
        for track in shortlist:
            candidate_key = self._candidate_key(track)
            score = self._score_candidate(
                track,
                title=title,
                artist=artist,
                duration_ms=duration_ms,
                isrc=isrc,
                retrieval_score=(retrieval_scores or {}).get(candidate_key, 0.0),
                ml_score=(ml_scores or {}).get(candidate_key, 0.0),
            )
            if matcher_boost_id and self._track_identity(track) == matcher_boost_id:
                score += 7.5
            scored.append((score, track))

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_track = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else float("-inf")

        if return_scores:
            return best_track, best_score, second_score
        return best_track

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
                        duration_ms=getattr(track_obj, "duration_ms", None),
                        isrc=getattr(track_obj, "isrc", None),
                        matcher=matcher,
                    )
                self._annotate_playable_metadata(
                    playable,
                    album=getattr(track_obj, "album", None),
                    spotify_track_id=getattr(track_obj, "spotify_track_id", None),
                    source_title=getattr(track_obj, "title", None),
                    source_artist=getattr(track_obj, "artist", None),
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
            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))
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
        self._cleanup_scope_id(scope_id)

    def _cleanup_scope_id(self, scope_id: int) -> None:
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
        await self._cancel_playlist_loading_by_scope_id(scope_id)

    async def _cancel_playlist_loading_by_scope_id(self, scope_id: int) -> None:
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

    @staticmethod
    def _is_stale_lavalink_session_error(exc: Exception) -> bool:
        return isinstance(exc, wavelink.LavalinkException) and getattr(exc, "status", None) == 404

    async def _force_start_next_from_queue(self, player: wavelink.Player) -> bool:
        if player.queue.is_empty:
            return False

        next_track = player.queue.get()
        await player.play(
            next_track,
            replace=True,
            volume=int(getattr(player, "volume", 15)),
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
                    if self._is_stale_lavalink_session_error(exc):
                        logging.warning("Skip aborted due to stale Lavalink session: %s", exc)
                        return "Audio session expired. Use `/music` to re-open the player."
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

            try:
                force_started = await self._force_start_next_from_queue(player)
            except Exception as exc:
                if self._is_stale_lavalink_session_error(exc):
                    logging.warning("Queue fallback aborted due to stale Lavalink session: %s", exc)
                    return "Audio session expired. Use `/music` to re-open the player."
                raise

            if force_started:
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
    async def on_wavelink_websocket_closed(self, payload: wavelink.WebsocketClosedEventPayload):
        player: Optional[wavelink.Player] = payload.player
        guild_id = player.guild.id if player and player.guild else "unknown"
        logging.warning(
            "Discord voice websocket closed for guild=%s code=%s by_remote=%s reason=%s",
            guild_id,
            payload.code,
            payload.by_remote,
            payload.reason or "",
        )

        if not player or not player.guild:
            return

        # DAVE-related close codes can require a fresh VOICE_STATE re-dispatch.
        if payload.code not in {1006, 4014, 4015, 4017}:
            return

        try:
            channel = getattr(player, "channel", None)
            if channel is not None:
                await player.guild.change_voice_state(channel=channel)
        except Exception as exc:
            logging.debug("Failed to re-dispatch guild voice state after websocket close: %s", exc)

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        print(f"Wavelink Node connected: {payload.node!r} | Resumed: {payload.resumed}")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        bot_user = self.bot.user
        if not bot_user or member.id != bot_user.id:
            return

        guild = getattr(member, "guild", None)
        if not guild or before.channel is None or after.channel is not None:
            return

        scope_id = int(guild.id)
        player = self._player_for_guild_id(scope_id)

        if player:
            ui_message = getattr(player, "ui_message", None)
            if ui_message:
                try:
                    await ui_message.delete()
                except Exception:
                    pass
                finally:
                    player.ui_message = None

            ui_view = getattr(player, "ui_view", None)
            if ui_view:
                try:
                    ui_view.stop()
                except Exception:
                    pass
                finally:
                    player.ui_view = None

            await self._cancel_playlist_loading(player)
            self._cleanup_player_scope(player)
            return

        await self._cancel_playlist_loading_by_scope_id(scope_id)
        self._cleanup_scope_id(scope_id)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        scope_id = int(guild.id)
        await self._cancel_playlist_loading_by_scope_id(scope_id)
        self._cleanup_scope_id(scope_id)

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
                    volume=int(getattr(player, "volume", 15)),
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
                "Queue: Add, Queue (with Clear Queue), Lists, Lyrics\n"
                "Audio: Set Volume + Filter popup\n"
                "Lyrics: 🎤 button opens live-synced lyrics for current track (when timestamps exist)"
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

        ui_message = getattr(player, "ui_message", None)
        if ui_message:
            try:
                await ui_message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                logging.debug("Could not delete dashboard message on stop: %s", exc)
            finally:
                player.ui_message = None

        if getattr(player, "ui_view", None):
            try:
                player.ui_view.stop()
            except Exception:
                pass
            finally:
                player.ui_view = None

        await self._cancel_playlist_loading(player)
        self._cleanup_player_scope(player)

        try:
            await player.disconnect()
        except Exception as exc:
            await self._reply(interaction, f"Failed to disconnect cleanly: {exc}", ephemeral=True)
            return

        await self._reply(interaction, "Stopped playback and disconnected from voice.", ephemeral=True)

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
                player = await interaction.user.voice.channel.connect(cls=DAVECompatiblePlayer)
                created_player = True
            except AttributeError:
                await self._reply(interaction, "Please join a voice channel first!", ephemeral=True)
                return
            except discord.ClientException:
                await self._reply(interaction, "I was unable to join this voice channel.", ephemeral=True)
                return

        if created_player:
            try:
                await player.set_volume(15)
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
                            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))

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
                            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))
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
                    duration_ms=getattr(first_track, "duration_ms", None),
                    isrc=getattr(first_track, "isrc", None),
                    matcher=matcher,
                )
                if not first_playable:
                    self._clear_queue_busy(player)
                    return await interaction.followup.send("❌ Could not find the first track's audio.")
                self._annotate_playable_metadata(
                    first_playable,
                    album=getattr(first_track, "album", None),
                    spotify_track_id=getattr(first_track, "spotify_track_id", None),
                    source_title=getattr(first_track, "title", None),
                    source_artist=getattr(first_track, "artist", None),
                )
                
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
                    await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))

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
                    duration_ms=getattr(track_info, "duration_ms", None),
                    isrc=getattr(track_info, "isrc", None),
                    matcher=matcher,
                )
                if not playable:
                    return await interaction.followup.send("❌ Could not find audio for this Spotify track.")
                self._annotate_playable_metadata(
                    playable,
                    album=getattr(track_info, "album", None),
                    spotify_track_id=getattr(track_info, "spotify_track_id", None),
                    source_title=getattr(track_info, "title", None),
                    source_artist=getattr(track_info, "artist", None),
                )
                
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
                    await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))
                
                if getattr(player, "ui_view", None):
                    await player.ui_view.update_player_message()
                    
                return # Don't fall through
        else:
            self._clear_queue_busy(player)
                
        if not is_spotify:
            # Normal Search
            if self._is_url(query):
                tracks: wavelink.Search = await self._safe_wavelink_search(query)
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
                    track: wavelink.Playable = self._pick_playable_candidate(
                        list(tracks),
                        title=query,
                        artist=None,
                    ) or tracks[0]
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
            else:
                # Multi-source smart resolver for plain-text queries.
                track = await self._resolve_playable_with_fallbacks(
                    query=query,
                    title=query,
                    artist=None,
                )
                if not track:
                    await interaction.followup.send(f"Could not find any songs for `{query}`.")
                    return

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
                await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))
                
            if getattr(player, "ui_view", None):
                await player.ui_view.update_player_message()
            
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
