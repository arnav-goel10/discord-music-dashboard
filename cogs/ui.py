import os
import logging
import asyncio
import time
import datetime
import re
import sqlite3
import discord
import wavelink
from typing import Optional, Any

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_PLAYLIST_DB_PATH = os.path.join(_DATA_DIR, "music_bot.db")
_SAVED_PLAYLIST_DB_READY = False
_MAX_SAVED_PLAYLISTS = 1
_PLAYBAR_LEFT_PAD = "\u2002\u2002"

_HAS_LAYOUT_V2 = all(
    hasattr(discord.ui, name)
    for name in ("LayoutView", "Container", "Section", "TextDisplay", "Thumbnail", "ActionRow")
)
_DashboardBaseView = discord.ui.LayoutView if _HAS_LAYOUT_V2 else discord.ui.View


def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        value = float(default)
    else:
        try:
            value = float(raw)
        except ValueError:
            value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    return value


def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        value = int(default)
    else:
        try:
            value = int(raw)
        except ValueError:
            value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    return value


def _fmt_ms(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _progress_bar_blocks(position_ms: int, length_ms: int, *, width: int = 20) -> str:
    width = max(1, int(width))
    if length_ms <= 0:
        return "[" + ("░" * width) + "]"

    ratio = max(0.0, min(1.0, position_ms / length_ms))
    # Smooth per-cell progression: ░ -> ▒ -> ▓ -> █
    if ratio >= 1.0 or position_ms >= length_ms:
        return "[" + ("█" * width) + "]"

    exact = ratio * width
    full = int(exact)
    remainder = exact - full

    partial = ""
    if full < width:
        if remainder >= (2 / 3):
            partial = "█"
        elif remainder >= (1 / 3):
            partial = "▓"
        elif remainder > 0:
            partial = "▒"

    if partial == "█":
        full += 1
        partial = ""

    empty = max(0, width - full - (1 if partial else 0))
    return "[" + ("█" * full) + partial + ("░" * empty) + "]"


def _clip(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _display_artist_name(raw_artist: Any) -> str:
    artist = str(raw_artist or "").strip()
    if not artist:
        return "Unknown Artist"
    # Keep matching logic unchanged; only clean UI labels.
    artist = re.sub(r"\s*-\s*topic\s*$", "", artist, flags=re.IGNORECASE).strip()
    return artist or "Unknown Artist"


def _track_link_text(track: wavelink.Playable, *, max_label_len: int = 90) -> str:
    title = str(getattr(track, "title", "Unknown Title"))
    artist = _display_artist_name(getattr(track, "author", "Unknown Artist"))
    label = _clip(f"{title} - {artist}", max_label_len)
    label = discord.utils.escape_markdown(label, as_needed=True)
    label = label.replace("[", "(").replace("]", ")")
    uri = getattr(track, "uri", None)
    return f"[{label}]({uri})" if uri else label


def _track_duration_label(track: wavelink.Playable) -> str:
    length = int(getattr(track, "length", 0) or 0)
    if length <= 0:
        return "LIVE"
    return _fmt_ms(length)


def build_queue_lines(player: wavelink.Player) -> list[str]:
    lines: list[str] = []
    current = player.current
    if current:
        lines.append(f"**Now:** {_track_link_text(current)} `({_track_duration_label(current)})`")

    for i, track in enumerate(player.queue, start=1):
        lines.append(f"**{i}.** {_track_link_text(track)} `({_track_duration_label(track)})`")

    if not lines:
        lines.append("Queue is empty.")
    return lines


def _get_music_cog(interaction: discord.Interaction):
    music_cog = interaction.client.get_cog("Music")
    if not music_cog or not hasattr(music_cog, "_execute_play"):
        return None
    return music_cog


def _normalize_spotify_playlist_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url.startswith("https://open.spotify.com/playlist/"):
        return ""
    return url.split("?", 1)[0]


def _playlist_token_from_url(url: str) -> str:
    match = re.search(r"/playlist/([A-Za-z0-9]+)", url)
    if not match:
        return "unknown"
    token = match.group(1)
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-4:]}"


def _db_connect() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_PLAYLIST_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _normalize_scope_id(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


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


def _order_playlist_preview_tracks(tracks: list[Any], play_order: str) -> tuple[list[Any], str]:
    if str(play_order or "recent").strip().lower() != "recent":
        return list(tracks), "Spotify Order"

    with_timestamp: list[tuple[int, float, Any]] = []
    without_timestamp: list[tuple[int, Any]] = []

    for idx, track in enumerate(tracks):
        ts = _parse_spotify_timestamp(getattr(track, "added_at", None))
        if ts is None:
            without_timestamp.append((idx, track))
            continue
        with_timestamp.append((idx, ts, track))

    with_timestamp.sort(key=lambda item: item[1], reverse=True)

    ordered = [track for _, _, track in with_timestamp]
    ordered.extend(track for _, track in reversed(without_timestamp))

    order_label = "Recently Added"
    if ordered and not with_timestamp:
        order_label = "Recently Added (Approximate)"
    return ordered, order_label


def _saved_playlist_title() -> str:
    return "Saved playlist" if _MAX_SAVED_PLAYLISTS == 1 else "Saved playlists"


def _saved_playlist_limit_label() -> str:
    return "saved playlist" if _MAX_SAVED_PLAYLISTS == 1 else "saved playlists"


def _sanitize_saved_playlist_items(raw_items: Any) -> list[dict[str, Any]]:
    now_ts = int(time.time())
    parsed: list[dict[str, Any]] = []

    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = _normalize_spotify_playlist_url(str(item.get("url", "")))
            if not url:
                continue
            try:
                saved_at = int(item.get("saved_at", now_ts))
            except (TypeError, ValueError):
                saved_at = now_ts
            parsed.append({"url": url, "saved_at": saved_at})

    deduped: dict[str, dict[str, Any]] = {}
    for item in parsed:
        existing = deduped.get(item["url"])
        if not existing or int(item["saved_at"]) > int(existing["saved_at"]):
            deduped[item["url"]] = item

    ordered = sorted(deduped.values(), key=lambda x: int(x["saved_at"]), reverse=True)
    return ordered[:_MAX_SAVED_PLAYLISTS]


def _ensure_saved_playlists_storage() -> None:
    global _SAVED_PLAYLIST_DB_READY
    if _SAVED_PLAYLIST_DB_READY:
        return

    with _db_connect() as conn:
        # Drop legacy per-guild table if it exists.
        conn.execute("DROP TABLE IF EXISTS saved_playlists")
        conn.execute("DROP TABLE IF EXISTS saved_playlists_v2")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_saved_playlists (
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, url)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_saved_playlists_saved_at
            ON user_saved_playlists (user_id, saved_at DESC)
            """
        )
        conn.commit()

    _SAVED_PLAYLIST_DB_READY = True


def _load_saved_playlists(user_id: int) -> list[dict[str, Any]]:
    user_scope = _normalize_scope_id(user_id)
    _ensure_saved_playlists_storage()
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT url, saved_at
            FROM user_saved_playlists
            WHERE user_id = ?
            ORDER BY saved_at DESC
            LIMIT ?
            """,
            (user_scope, _MAX_SAVED_PLAYLISTS),
        ).fetchall()

    parsed = []
    for row in rows:
        url = _normalize_spotify_playlist_url(str(row["url"]))
        if not url:
            continue
        parsed.append({"url": url, "saved_at": int(row["saved_at"])})
    return _sanitize_saved_playlist_items(parsed)


def _save_saved_playlists(user_id: int, playlists: list[dict[str, Any]]) -> None:
    user_scope = _normalize_scope_id(user_id)
    _ensure_saved_playlists_storage()
    cleaned = _sanitize_saved_playlist_items(playlists)
    with _db_connect() as conn:
        conn.execute(
            "DELETE FROM user_saved_playlists WHERE user_id = ?",
            (user_scope,),
        )
        conn.executemany(
            "INSERT INTO user_saved_playlists (user_id, url, saved_at) VALUES (?, ?, ?)",
            [(user_scope, item["url"], int(item["saved_at"])) for item in cleaned],
        )
        conn.commit()


def _upsert_saved_playlist(url: str, user_id: int) -> tuple[list[dict[str, Any]], bool]:
    playlists = _load_saved_playlists(user_id)
    playlists = [item for item in playlists if item.get("url") != url]
    playlists.insert(0, {"url": url, "saved_at": int(time.time())})
    trimmed = len(playlists) > _MAX_SAVED_PLAYLISTS
    playlists = playlists[:_MAX_SAVED_PLAYLISTS]
    _save_saved_playlists(user_id, playlists)
    return playlists, trimmed


class AddTrackModal(discord.ui.Modal, title="Add Song to Queue"):
    query = discord.ui.TextInput(
        label="Song name or URL",
        placeholder="e.g. so easy to fall in love or a Spotify/YouTube URL",
        required=True,
        max_length=200,
    )
    index = discord.ui.TextInput(
        label="Queue position (optional)",
        placeholder="Leave blank for end. Use 0 for next.",
        required=False,
        max_length=6,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_query = str(self.query.value).strip()
        if not raw_query:
            await interaction.response.send_message("Query cannot be empty.")
            return

        parsed_index: Optional[int] = None
        raw_index = str(self.index.value).strip()
        if raw_index:
            try:
                parsed_index = max(0, int(raw_index))
            except ValueError:
                await interaction.response.send_message("Queue position must be a number.")
                return

        music_cog = _get_music_cog(interaction)
        if not music_cog:
            await interaction.response.send_message(
                "Music controller is not available right now.",
            )
            return

        await music_cog._execute_play(
            interaction,
            raw_query,
            index=parsed_index,
            force_cache=False,
        )


class SavePlaylistModal(discord.ui.Modal, title="Save Spotify Playlist"):
    playlist_url = discord.ui.TextInput(
        label="Spotify playlist URL",
        placeholder="https://open.spotify.com/playlist/...",
        required=True,
        max_length=220,
    )

    def __init__(self, parent_view: Optional["SavedPlaylistManagerView"] = None):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = _normalize_spotify_playlist_url(str(self.playlist_url.value))
        if not url:
            await interaction.response.send_message(
                "Please provide a valid Spotify playlist URL.",
                ephemeral=True,
            )
            return

        owner_user_id = self.parent_view.owner_user_id if self.parent_view else int(interaction.user.id)
        playlists, trimmed = _upsert_saved_playlist(url, owner_user_id)
        token = _playlist_token_from_url(url)
        if trimmed:
            message = (
                f"Saved playlist `{token}`. Kept most recent {_MAX_SAVED_PLAYLISTS} "
                f"{_saved_playlist_limit_label()} (older one was removed)."
            )
        else:
            message = f"Saved playlist `{token}`."

        await interaction.response.send_message(message, ephemeral=True)

        if self.parent_view:
            self.parent_view.playlists = playlists
            self.parent_view.selected_index = 0
            await self.parent_view.refresh_preview(force_update=self.parent_view.play_order == "recent")
            await self.parent_view.refresh_message()


class EditSavedPlaylistModal(discord.ui.Modal, title="Edit Saved Playlist URL"):
    playlist_url = discord.ui.TextInput(
        label="Spotify playlist URL",
        placeholder="https://open.spotify.com/playlist/...",
        required=True,
        max_length=220,
    )

    def __init__(self, parent_view: "SavedPlaylistManagerView", index: int):
        super().__init__()
        self.parent_view = parent_view
        self.index = index
        current_url = ""
        if 0 <= index < len(parent_view.playlists):
            current_url = str(parent_view.playlists[index].get("url", ""))
        self.playlist_url.default = current_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.index < 0 or self.index >= len(self.parent_view.playlists):
            await interaction.response.send_message("Selected playlist is no longer available.", ephemeral=True)
            return

        url = _normalize_spotify_playlist_url(str(self.playlist_url.value))
        if not url:
            await interaction.response.send_message(
                "Please provide a valid Spotify playlist URL.",
                ephemeral=True,
            )
            return

        playlists = list(self.parent_view.playlists)
        existing_saved_at = int(playlists[self.index].get("saved_at", int(time.time())))
        playlists[self.index] = {"url": url, "saved_at": existing_saved_at}

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in playlists:
            item_url = _normalize_spotify_playlist_url(str(item.get("url", "")))
            if not item_url or item_url in seen:
                continue
            seen.add(item_url)
            deduped.append(
                {
                    "url": item_url,
                    "saved_at": int(item.get("saved_at", int(time.time()))),
                }
            )

        deduped = deduped[:_MAX_SAVED_PLAYLISTS]
        _save_saved_playlists(self.parent_view.owner_user_id, deduped)

        self.parent_view.playlists = deduped
        self.parent_view.selected_index = min(self.index, len(deduped) - 1) if deduped else -1

        await interaction.response.send_message("Saved playlist URL updated.", ephemeral=True)
        await self.parent_view.refresh_preview(force_update=self.parent_view.play_order == "recent")
        await self.parent_view.refresh_message()


class SavedPlaylistManagerView(discord.ui.View):
    def __init__(self, dashboard: "SpotifyUI", owner_user_id: int):
        super().__init__(timeout=180)
        self.dashboard = dashboard
        self.owner_user_id = int(owner_user_id)
        self.playlists: list[dict[str, Any]] = _load_saved_playlists(self.owner_user_id)
        self.selected_index: int = 0 if self.playlists else -1
        default_order = str(getattr(self.dashboard, "_saved_playlist_order", "recent") or "recent")
        self.play_order = default_order if default_order in {"recent", "spotify"} else "recent"
        self.preview_tracks: list[Any] = []
        self.preview_order_label = self._order_label()
        self.preview_note = "Preview unavailable until playlist metadata loads."
        self.message: Optional[discord.Message] = None
        self._sync_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0)) == self.owner_user_id:
            return True
        try:
            await interaction.response.send_message(
                "Only the person who opened this playlist manager can use it.",
                ephemeral=True,
            )
        except Exception:
            pass
        return False

    def _order_label(self) -> str:
        return "Recently Added" if self.play_order == "recent" else "Spotify Order"

    def _selected_url(self) -> str:
        if 0 <= self.selected_index < len(self.playlists):
            return str(self.playlists[self.selected_index].get("url", ""))
        return ""

    def _build_content(self) -> str:
        lines = [
            f"{_saved_playlist_title()} (`{len(self.playlists)}/{_MAX_SAVED_PLAYLISTS}`)",
            "",
        ]
        if not self.playlists:
            lines.append("No saved playlist yet. Use **Add** below.")
        else:
            for idx, item in enumerate(self.playlists, start=1):
                url = str(item.get("url", ""))
                token = _playlist_token_from_url(url)
                marker = "▶️" if idx - 1 == self.selected_index else "•"
                lines.append(f"{marker} **{idx}.** [`{token}`]({url})")
        lines.append("")
        lines.append(f"Current order: **{self._order_label()}**")
        if self.selected_index >= 0:
            lines.append("")
            lines.append(f"Preview: **{self.preview_order_label}**")
            if self.preview_tracks:
                for idx, track in enumerate(self.preview_tracks, start=1):
                    title = str(getattr(track, "title", "Unknown Title") or "Unknown Title")
                    artist = _display_artist_name(getattr(track, "artist", "Unknown Artist"))
                    preview_label = discord.utils.escape_markdown(_clip(f"{title} - {artist}", 80), as_needed=True)
                    lines.append(f"{idx}. {preview_label}")
            else:
                lines.append(self.preview_note)
        return "\n".join(lines)

    def _sync_components(self) -> None:
        options: list[discord.SelectOption] = []
        for idx, item in enumerate(self.playlists):
            token = _playlist_token_from_url(str(item.get("url", "")))
            options.append(
                discord.SelectOption(
                    label=f"Playlist {idx + 1}",
                    description=f"spotify:{token}",
                    value=str(idx),
                    default=idx == self.selected_index,
                )
            )

        if not options:
            options = [discord.SelectOption(label="No saved playlists", value="-1", default=True)]
            self.playlist_select.disabled = True
            self.play_selected.disabled = True
            self.edit_selected.disabled = True
            self.delete_selected.disabled = True
            self.selected_index = -1
        else:
            self.playlist_select.disabled = False
            self.play_selected.disabled = False
            self.edit_selected.disabled = False
            self.delete_selected.disabled = False
            if self.selected_index < 0 or self.selected_index >= len(self.playlists):
                self.selected_index = 0
            for option in options:
                option.default = option.value == str(self.selected_index)

        self.playlist_select.options = options
        self.toggle_order.label = f"Order: {self._order_label()}"

    async def refresh_message(self) -> None:
        self._sync_components()
        if not self.message:
            return
        try:
            await self.message.edit(content=self._build_content(), view=self)
        except Exception:
            pass

    async def refresh_preview(self, *, force_update: bool = False) -> None:
        url = self._selected_url()
        self.preview_tracks = []
        self.preview_order_label = self._order_label()
        self.preview_note = "Preview unavailable until playlist metadata loads."
        if not url:
            self.preview_note = "No saved playlist selected."
            return

        try:
            from utils.resolver import resolver
        except Exception:
            self.preview_note = "Track preview is temporarily unavailable."
            return

        try:
            tracks = []
            if force_update:
                tracks = await resolver.get_playlist_tracks(url, force_update=True, force_cache_bypass=False)
                if not tracks:
                    tracks = await resolver.get_playlist_tracks(url, force_update=False, force_cache_bypass=True)
            else:
                tracks = await resolver.get_playlist_tracks(url, force_update=False, force_cache_bypass=True)
                if not tracks:
                    tracks = await resolver.get_playlist_tracks(url, force_update=False, force_cache_bypass=False)
            if not tracks and not force_update:
                tracks = await resolver.get_playlist_tracks(url, force_update=True, force_cache_bypass=False)
        except Exception as exc:
            logging.debug("Failed refreshing saved playlist preview for %s: %s", url, exc)
            self.preview_note = "Track preview is temporarily unavailable."
            return

        if not tracks:
            self.preview_note = "Track preview unavailable for this playlist right now."
            return

        ordered_tracks, order_label = _order_playlist_preview_tracks(tracks, self.play_order)
        self.preview_tracks = ordered_tracks[:5]
        self.preview_order_label = order_label
        if not self.preview_tracks:
            self.preview_note = "No tracks available for preview."

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.select(
        placeholder="Select a saved playlist",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label="No saved playlists", value="-1")],
        row=0,
    )
    async def playlist_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        raw_value = str(select.values[0]) if select.values else "-1"
        try:
            selected_index = int(raw_value)
        except ValueError:
            selected_index = -1

        if selected_index < 0 or selected_index >= len(self.playlists):
            await interaction.response.send_message("That saved playlist is no longer available.", ephemeral=True)
            return

        self.selected_index = selected_index
        self._sync_components()
        await self.refresh_preview(force_update=self.play_order == "recent")
        await interaction.response.edit_message(content=self._build_content(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="Order: Recently Added", row=1)
    async def toggle_order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.play_order = "spotify" if self.play_order == "recent" else "recent"
        setattr(self.dashboard, "_saved_playlist_order", self.play_order)
        self._sync_components()
        await self.refresh_preview(force_update=self.play_order == "recent")
        await interaction.response.edit_message(content=self._build_content(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.success, emoji=None, label="▶", row=1)
    async def play_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        url = self._selected_url()
        if not url:
            await interaction.response.send_message("No saved playlist selected.", ephemeral=True)
            return

        music_cog = _get_music_cog(interaction)
        if not music_cog:
            await interaction.response.send_message("Music controller is not available.", ephemeral=True)
            return

        try:
            await music_cog._execute_play(
                interaction,
                url,
                index=None,
                force_cache=True,
                playlist_order=self.play_order,
                force_playlist_refresh=self.play_order == "recent",
            )
        except Exception as exc:
            logging.exception("Saved playlist play failed: %s", exc)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Failed to load saved playlist. Check logs and try again.", ephemeral=True)
                else:
                    await interaction.response.send_message("Failed to load saved playlist. Check logs and try again.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="✎", row=2)
    async def edit_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_index < 0 or self.selected_index >= len(self.playlists):
            await interaction.response.send_message("No saved playlist selected.", ephemeral=True)
            return
        await interaction.response.send_modal(EditSavedPlaylistModal(self, self.selected_index))

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji=None, label="＋", row=2)
    async def save_playlist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SavePlaylistModal(parent_view=self))

    @discord.ui.button(style=discord.ButtonStyle.danger, emoji=None, label="🗑", row=2)
    async def delete_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_index < 0 or self.selected_index >= len(self.playlists):
            await interaction.response.send_message("No saved playlist selected.", ephemeral=True)
            return

        target_url = str(self.playlists[self.selected_index].get("url", ""))
        await interaction.response.send_message(
            f"Delete `{_playlist_token_from_url(target_url)}`?",
            view=ConfirmDeleteSavedPlaylistView(self, target_url),
            ephemeral=True,
        )


class ConfirmDeleteSavedPlaylistView(discord.ui.View):
    def __init__(self, parent_view: "SavedPlaylistManagerView", target_url: str):
        super().__init__(timeout=30)
        self.parent_view = parent_view
        self.target_url = _normalize_spotify_playlist_url(target_url)

    async def on_timeout(self) -> None:
        self.stop()

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        delete_index = -1
        for idx, item in enumerate(self.parent_view.playlists):
            if _normalize_spotify_playlist_url(str(item.get("url", ""))) == self.target_url:
                delete_index = idx
                break

        if delete_index < 0:
            await interaction.response.edit_message(
                content="Selected playlist is no longer available.",
                view=None,
            )
            self.stop()
            return

        removed = self.parent_view.playlists.pop(delete_index)
        _save_saved_playlists(self.parent_view.owner_user_id, self.parent_view.playlists)

        if self.parent_view.playlists:
            self.parent_view.selected_index = min(delete_index, len(self.parent_view.playlists) - 1)
        else:
            self.parent_view.selected_index = -1

        self.parent_view._sync_components()
        await self.parent_view.refresh_preview(force_update=self.parent_view.play_order == "recent")
        await self.parent_view.refresh_message()
        await interaction.response.edit_message(
            content=f"Deleted `{_playlist_token_from_url(str(removed.get('url', '')))}`.",
            view=None,
        )
        self.stop()


class VolumeModal(discord.ui.Modal, title="Set Volume"):
    volume = discord.ui.TextInput(
        label="Volume (0-100)",
        placeholder="e.g. 35",
        required=True,
        max_length=3,
    )

    def __init__(self, parent_view: "SpotifyUI", *, current_volume: int = 15):
        super().__init__()
        self.parent_view = parent_view
        safe_volume = max(0, min(100, int(current_volume)))
        self.volume.default = str(safe_volume)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.volume.value).strip()
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message("Volume must be a number between 0 and 100.")
            return

        value = max(0, min(100, value))
        player = self.parent_view._live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player right now.")
            return

        await player.set_volume(value)
        music_cog = _get_music_cog(interaction)
        if music_cog and hasattr(music_cog, "_remember_player_volume"):
            try:
                music_cog._remember_player_volume(player, value)
            except Exception:
                pass
        await interaction.response.send_message(f"Volume set to **{value}%**.", ephemeral=True)
        await self.parent_view.update_player_message(force=True)


class AudioFilterPickerView(discord.ui.View):
    def __init__(self, parent_view: "SpotifyUI"):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.message: Optional[discord.Message] = None
        self._sync_components()

    def _sync_components(self) -> None:
        active_filter = self.parent_view._active_filter_value()
        for option in self.filter_select.options:
            option.default = option.value == active_filter
        self.filter_select.placeholder = f"Audio Filter ({self.parent_view._active_filter_name()})"

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass
        self.stop()

    @discord.ui.select(
        placeholder="Audio Filter",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="None", value="none", emoji="🚫"),
            discord.SelectOption(label="Bass Boost", value="bass_boost", emoji="🔊"),
            discord.SelectOption(label="Treble Boost", value="treble_boost", emoji="🎚️"),
            discord.SelectOption(label="Nightcore", value="nightcore", emoji="⚡"),
            discord.SelectOption(label="Vaporwave", value="vaporwave", emoji="🌊"),
            discord.SelectOption(label="Karaoke", value="karaoke", emoji="🎤"),
            discord.SelectOption(label="8D", value="eight_d", emoji="🌀"),
            discord.SelectOption(label="LowPass", value="low_pass", emoji="🎧"),
        ],
        row=0,
    )
    async def filter_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        player = await self.parent_view._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player right now.", ephemeral=True)
            return

        preset = str(select.values[0]) if select.values else "none"
        music_cog = _get_music_cog(interaction)
        if not music_cog:
            await interaction.response.send_message("Music controller unavailable.", ephemeral=True)
            return

        try:
            label = await music_cog._apply_filter_preset(player, preset)
        except Exception as exc:
            logging.error("Failed applying audio filter preset (%s): %s", preset, exc)
            await interaction.response.send_message(
                "Failed to apply filter preset. Try selecting fewer filters.",
                ephemeral=True,
            )
            return

        self._sync_components()
        await interaction.response.edit_message(content=f"Filter applied: **{label}**.", view=self)
        await self.parent_view.update_player_message(force=True)


class RemoveTrackModal(discord.ui.Modal, title="Remove Track from Queue"):
    position = discord.ui.TextInput(
        label="Queue positions / slices to remove",
        placeholder="e.g. 3,5,9 or :5 or 2:6",
        required=True,
        max_length=120,
    )

    def __init__(self, parent_view: "QueuePaginatorView"):
        super().__init__()
        self.parent_view = parent_view

    @staticmethod
    def _parse_positions(raw: str, queue_count: int) -> tuple[list[int], bool]:
        tokens = [token.strip() for token in raw.split(",")]
        if not tokens or any(not token for token in tokens):
            raise ValueError("Use positions like `1,2,3` or slices like `:5`, `2:6`, `5:`.")

        selected: set[int] = set()
        used_slice = False

        for token in tokens:
            if ":" in token:
                used_slice = True
                if not re.fullmatch(r"\d*:\d*", token):
                    raise ValueError(f"Invalid slice `{token}`. Use `start:end`, `:end`, or `start:`.")
                left, right = token.split(":", 1)
                start = int(left) if left else 1
                end = int(right) if right else queue_count

                if start < 1:
                    raise ValueError(
                        "Current track cannot be removed here. Queue positions start at 1 (the next track)."
                    )
                if end < 1:
                    raise ValueError("Slice end must be 1 or greater.")
                if start > end:
                    raise ValueError(f"Invalid slice `{token}`: start must be <= end.")

                start = min(start, queue_count)
                end = min(end, queue_count)
                if start <= end:
                    selected.update(range(start, end + 1))
                continue

            try:
                pos = int(token)
            except ValueError:
                raise ValueError(f"Invalid position `{token}`. Use numbers like `1,2,3`.") from None

            if pos < 1:
                raise ValueError(
                    "Current track cannot be removed here. Queue positions start at 1 (the next track)."
                )
            if pos > queue_count:
                raise ValueError(f"Position `{pos}` is out of range. Queue has {queue_count} tracks.")
            selected.add(pos)

        positions = sorted(selected)
        if not positions:
            raise ValueError("No matching queue tracks were selected.")
        return positions, used_slice

    async def _remove_positions(self, interaction: discord.Interaction, positions: list[int]) -> bool:
        player = self.parent_view.player
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return False

        queue_count = player.queue.count
        positions = sorted({p for p in positions if 1 <= p <= queue_count}, reverse=True)
        if not positions:
            await interaction.response.send_message("Those queue positions no longer exist.", ephemeral=True)
            return False

        try:
            tracks = list(player.queue)
            for pos in positions:
                tracks.pop(pos - 1)
            player.queue.clear()
            for t in tracks:
                player.queue.put(t)
        except Exception as exc:
            logging.warning("Failed to remove queue positions %s: %s", positions, exc)
            await interaction.response.send_message("Failed to remove selected tracks.", ephemeral=True)
            return False

        self.parent_view.lines = build_queue_lines(player)
        self.parent_view.page_index = min(
            self.parent_view.page_index, max(0, self.parent_view.total_pages - 1)
        )
        await interaction.response.edit_message(
            embed=self.parent_view.build_embed(), view=self.parent_view
        )

        ui_view = getattr(player, "ui_view", None)
        if ui_view:
            try:
                await ui_view.update_player_message(force=True)
            except Exception:
                pass
        return True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.position.value).strip()

        player = self.parent_view.player
        if not player or player.queue.is_empty:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        queue_count = player.queue.count
        try:
            positions, used_slice = self._parse_positions(raw, queue_count)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if used_slice:
            preview = ",".join(str(p) for p in positions[:12])
            if len(positions) > 12:
                preview += ",..."
            await interaction.response.send_message(
                (
                    f"Remove **{len(positions)}** tracks at positions `{preview}`?\n"
                    "Current track is protected and cannot be removed here."
                ),
                view=ConfirmRemoveTracksView(self.parent_view, positions),
                ephemeral=True,
            )
            return

        await self._remove_positions(interaction, positions)


class ConfirmRemoveTracksView(discord.ui.View):
    def __init__(self, paginator: "QueuePaginatorView", positions: list[int]):
        super().__init__(timeout=30)
        self.paginator = paginator
        self.positions = sorted(set(positions))

    async def on_timeout(self) -> None:
        self.stop()

    @discord.ui.button(label="Yes, remove them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.paginator.player
        if not player or player.queue.is_empty:
            await interaction.response.edit_message(content="Queue is already empty.", view=None)
            self.stop()
            return

        queue_count = player.queue.count
        valid_positions = sorted(
            {p for p in self.positions if 1 <= p <= queue_count},
            reverse=True,
        )
        if not valid_positions:
            await interaction.response.edit_message(
                content="Those queue positions no longer exist.",
                view=None,
            )
            self.stop()
            return

        try:
            tracks = list(player.queue)
            for pos in valid_positions:
                tracks.pop(pos - 1)
            player.queue.clear()
            for t in tracks:
                player.queue.put(t)
        except Exception as exc:
            logging.warning("Failed to remove queue positions %s: %s", valid_positions, exc)
            await interaction.response.edit_message(content="Failed to remove selected tracks.", view=None)
            self.stop()
            return

        self.paginator.lines = build_queue_lines(player)
        self.paginator.page_index = min(
            self.paginator.page_index, max(0, self.paginator.total_pages - 1)
        )

        if self.paginator.message:
            try:
                await self.paginator.message.edit(
                    embed=self.paginator.build_embed(), view=self.paginator
                )
            except Exception:
                pass

        ui_view = getattr(player, "ui_view", None)
        if ui_view:
            try:
                await ui_view.update_player_message(force=True)
            except Exception:
                pass

        await interaction.response.edit_message(
            content=f"Removed **{len(valid_positions)}** tracks from the queue.",
            view=None,
        )
        self.stop()

class ConfirmClearView(discord.ui.View):
    def __init__(self, paginator: "QueuePaginatorView"):
        super().__init__(timeout=30)
        self.paginator = paginator

    async def on_timeout(self) -> None:
        self.stop()

    @discord.ui.button(label="Yes, clear it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.paginator.player
        if not player or player.queue.is_empty:
            await interaction.response.edit_message(content="Queue is already empty.", view=None)
            self.stop()
            return

        count = player.queue.count
        player.queue.clear()
        self.paginator.lines = build_queue_lines(player)
        self.paginator.page_index = 0

        if self.paginator.message:
            try:
                await self.paginator.message.edit(
                    embed=self.paginator.build_embed(), view=self.paginator
                )
            except Exception:
                pass

        ui_view = getattr(player, "ui_view", None)
        if ui_view:
            try:
                await ui_view.update_player_message(force=True)
            except Exception:
                pass

        await interaction.response.edit_message(
            content=f"Cleared **{count}** tracks from the queue.", view=None
        )
        self.stop()

class QueuePaginatorView(discord.ui.View):
    def __init__(
        self,
        lines: list[str],
        *,
        title: str = "Queue",
        page_size: int = 10,
        player: Optional[wavelink.Player] = None,
    ):
        super().__init__(timeout=300)
        self.title = title
        self.lines = lines
        self.page_size = max(1, page_size)
        self.page_index = 0
        self.player = player
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.lines) + self.page_size - 1) // self.page_size)

    def _slice(self) -> list[str]:
        start = self.page_index * self.page_size
        end = start + self.page_size
        return self.lines[start:end]

    def _sync_buttons(self) -> None:
        queue_empty = not self.player or self.player.queue.is_empty
        self.prev_page.disabled = self.page_index <= 0
        self.next_page.disabled = self.page_index >= self.total_pages - 1
        self.remove_track.disabled = queue_empty
        self.clear_queue.disabled = queue_empty

    def build_embed(self) -> discord.Embed:
        self._sync_buttons()
        embed = discord.Embed(title=self.title, color=0x2B2D31)
        embed.description = "\n".join(self._slice()) if self.lines else "Queue is empty."
        embed.set_footer(text=f"Page {self.page_index + 1}/{self.total_pages}")
        return embed

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                pass
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logging.exception("Queue paginator interaction error on %s: %s", type(item).__name__, error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Queue interaction failed. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("Queue interaction failed. Please try again.", ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="◁", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page_index > 0:
            self.page_index -= 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▷", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page_index < self.total_pages - 1:
            self.page_index += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Remove Track", style=discord.ButtonStyle.secondary)
    async def remove_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.player or self.player.queue.is_empty:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveTrackModal(self))

    @discord.ui.button(label="Clear Queue", style=discord.ButtonStyle.danger)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.player or self.player.queue.is_empty:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return

        count = self.player.queue.count
        await interaction.response.send_message(
            f"Clear all **{count}** tracks from the queue?",
            view=ConfirmClearView(self),
            ephemeral=True,
        )


class SpotifyUI(_DashboardBaseView):
    def __init__(self, player: wavelink.Player):
        super().__init__(timeout=None)
        self.player = player
        self.message: Optional[discord.Message] = None
        self._stopped = False
        self._render_lock = asyncio.Lock()
        self._rebind_lock = asyncio.Lock()
        self._last_rebind_attempt_at: float = 0.0
        self._has_ever_bound_message: bool = False
        self._voice_gap_started_at: Optional[float] = None
        self._consecutive_voice_gap_ticks: int = 0
        self._consecutive_refresh_failures: int = 0
        self._last_refresh_failure_at: float = 0.0
        self._last_successful_render_at: float = time.monotonic()
        self._last_rendered_track_key: str = self._track_key(player.current)
        self._last_stale_recovery_at: float = 0.0
        self._refresh_restart_count: int = 0
        self._use_v2_layout = _HAS_LAYOUT_V2
        self._v2_header_container = None
        self._v2_section = None
        self._v2_title_text = None
        self._v2_meta_text = None
        self._v2_progress_text = None
        self._v2_fallback_accessory = None
        self._v2_thumb_url: str = ""
        # (track_key, position_ms, monotonic_time) — assigned atomically to avoid torn reads
        self._position_anchor: tuple[str, int, float] = (
            self._track_key(player.current),
            int(getattr(player, "position", 0) or 0),
            time.monotonic(),
        )
        self._refresh_interval = _env_float("UI_REFRESH_INTERVAL_SECONDS", 0.70, minimum=0.55)
        self._voice_gap_stop_seconds = _env_float("UI_DASHBOARD_VOICE_GAP_STOP_SECONDS", 30.0, minimum=5.0)
        self._stale_seconds = _env_float("UI_DASHBOARD_STALE_SECONDS", 12.0, minimum=3.0)
        self._stale_cooldown_seconds = _env_float("UI_DASHBOARD_STALE_COOLDOWN_SECONDS", 15.0, minimum=3.0)
        self._refresh_fail_rebind_threshold = _env_int(
            "UI_DASHBOARD_REFRESH_FAIL_REBIND_THRESHOLD", 3, minimum=1
        )
        self._edit_timeout_seconds = _env_float("UI_DASHBOARD_EDIT_TIMEOUT_SECONDS", 8.0, minimum=1.0)
        self._setup_controls_for_layout_if_needed()
        self._setup_v2_layout_shell()
        self._reorder_fallback_controls_if_needed()
        self._refresh_task: Optional[asyncio.Task] = None
        self._last_render_key: Optional[tuple] = None
        self._sync_components()
        self.ensure_refresh_task_running("init")

    def _dashboard_scope_label(self) -> str:
        guild = getattr(self.player, "guild", None)
        guild_id = getattr(guild, "id", None)
        msg_id = getattr(self.message, "id", None)
        return f"guild={guild_id} msg={msg_id}"

    def ensure_refresh_task_running(self, reason: str) -> bool:
        if self._stopped:
            return False
        if self._refresh_task and not self._refresh_task.done():
            return False
        if self._refresh_task and self._refresh_task.done():
            self._refresh_restart_count += 1
            logging.info(
                "dashboard_loop_restart reason=%s restart_count=%s %s",
                reason,
                self._refresh_restart_count,
                self._dashboard_scope_label(),
            )
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        return True

    @property
    def uses_v2_layout(self) -> bool:
        return self._use_v2_layout

    def _loop_visual(self) -> tuple[str, str, discord.ButtonStyle]:
        if self.player.queue.mode == wavelink.QueueMode.loop:
            return "🔂", "", discord.ButtonStyle.primary
        if self.player.queue.mode == wavelink.QueueMode.loop_all:
            return "🔁", "", discord.ButtonStyle.success
        return "🔁", "", discord.ButtonStyle.secondary

    def _setup_v2_layout_shell(self) -> None:
        if not self._use_v2_layout:
            return

        text_display_cls = getattr(discord.ui, "TextDisplay", None)
        section_cls = getattr(discord.ui, "Section", None)
        container_cls = getattr(discord.ui, "Container", None)
        thumbnail_cls = getattr(discord.ui, "Thumbnail", None)
        if not all(
            (
                text_display_cls,
                section_cls,
                container_cls,
                thumbnail_cls,
            )
        ):
            self._use_v2_layout = False
            return

        existing_items = list(self.children)
        self.clear_items()

        self._v2_title_text = text_display_cls("### Music Dashboard")
        self._v2_meta_text = text_display_cls("Queueing controls are below.")
        self._v2_progress_text = text_display_cls("`0:00 / 0:00`\n────────────────────")
        fallback_cover_url = self._default_cover_url()
        self._v2_fallback_accessory = thumbnail_cls(media=fallback_cover_url)
        self._v2_section = section_cls(
            self._v2_title_text,
            self._v2_meta_text,
            self._v2_progress_text,
            accessory=self._v2_fallback_accessory,
        )
        self._v2_header_container = container_cls(
            self._v2_section,
        )

        self.add_item(self._v2_header_container)
        for item in existing_items:
            self.add_item(item)

    def _default_cover_url(self) -> str:
        client = getattr(self.player, "client", None)
        bot_user = getattr(client, "user", None)
        display_avatar = getattr(bot_user, "display_avatar", None)
        avatar_url = getattr(display_avatar, "url", None)
        if isinstance(avatar_url, str) and avatar_url.strip():
            return avatar_url
        return "https://cdn.discordapp.com/embed/avatars/0.png"

    def _setup_controls_for_layout_if_needed(self) -> None:
        if not self._use_v2_layout:
            return

        # LayoutView does not materialize decorator-defined controls, so create them explicitly.
        self.previous_track = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="|◁", row=0
        )
        self.pause_resume = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="||", row=0
        )
        self.skip_track = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="▷|", row=0
        )
        self.shuffle_queue = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="🔀", label="", row=1
        )
        self.toggle_loop = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="🔁", label="", row=0
        )
        self.add_track = discord.ui.Button(
            style=discord.ButtonStyle.success, emoji="➕", label="", row=1
        )
        self.open_lyrics = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="🎤", label="", row=1
        )
        self.show_queue = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="Q", row=1
        )
        self.manage_lists = discord.ui.Button(
            style=discord.ButtonStyle.primary, emoji="💾", label="", row=2
        )
        self.volume_set = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="🔊", label="", row=0
        )
        self.filter_popup = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="Fx", row=1
        )
        self.toggle_dj = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="DJ", row=2
        )

        self.previous_track.callback = self._manual_previous_track
        self.pause_resume.callback = self._manual_pause_resume
        self.skip_track.callback = self._manual_skip_track
        self.shuffle_queue.callback = self._manual_shuffle_queue
        self.toggle_loop.callback = self._manual_toggle_loop
        self.add_track.callback = self._manual_add_track
        self.open_lyrics.callback = self._manual_open_lyrics
        self.show_queue.callback = self._manual_show_queue
        self.manage_lists.callback = self._manual_manage_lists
        self.volume_set.callback = self._manual_volume_set
        self.filter_popup.callback = self._manual_filter_popup
        self.toggle_dj.callback = self._manual_toggle_dj

        action_row_cls = getattr(discord.ui, "ActionRow", None)
        if not action_row_cls:
            self._use_v2_layout = False
            return

        self._v2_controls_row_primary = action_row_cls(
            self.volume_set,
            self.previous_track,
            self.pause_resume,
            self.skip_track,
            self.toggle_loop,
        )
        self._v2_controls_row_queue = action_row_cls(
            self.shuffle_queue,
            self.filter_popup,
            self.open_lyrics,
            self.show_queue,
            self.add_track,
        )
        self._v2_controls_row_library = action_row_cls(
            self.manage_lists,
            self.toggle_dj,
        )

        self.add_item(self._v2_controls_row_primary)
        self.add_item(self._v2_controls_row_queue)
        self.add_item(self._v2_controls_row_library)

    def _reorder_fallback_controls_if_needed(self) -> None:
        if self._use_v2_layout:
            return

        # Keep fallback View button ordering aligned with the V2 layout.
        ordered_controls = [
            self.volume_set,
            self.previous_track,
            self.pause_resume,
            self.skip_track,
            self.toggle_loop,
            self.shuffle_queue,
            self.filter_popup,
            self.open_lyrics,
            self.show_queue,
            self.add_track,
            self.manage_lists,
            self.toggle_dj,
        ]

        self.clear_items()
        for control in ordered_controls:
            self.add_item(control)

    async def _manual_previous_track(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.previous_track(self, interaction, self.previous_track)

    async def _manual_pause_resume(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.pause_resume(self, interaction, self.pause_resume)

    async def _manual_skip_track(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.skip_track(self, interaction, self.skip_track)

    async def _manual_shuffle_queue(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.shuffle_queue(self, interaction, self.shuffle_queue)

    async def _manual_toggle_loop(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.toggle_loop(self, interaction, self.toggle_loop)

    async def _manual_add_track(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.add_track(self, interaction, self.add_track)

    async def _manual_open_lyrics(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.open_lyrics(self, interaction, self.open_lyrics)

    async def _manual_show_queue(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.show_queue(self, interaction, self.show_queue)

    async def _manual_manage_lists(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.manage_lists(self, interaction, self.manage_lists)

    async def _manual_volume_set(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.volume_set(self, interaction, self.volume_set)

    async def _manual_filter_popup(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.filter_popup(self, interaction, self.filter_popup)

    async def _manual_toggle_dj(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.toggle_ai_dj(self, interaction, self.toggle_dj)

    def _sync_v2_dashboard_card(self) -> None:
        if not self._use_v2_layout or not all(
            (
                self._v2_header_container,
                self._v2_section,
                self._v2_title_text,
                self._v2_meta_text,
                self._v2_progress_text,
            )
        ):
            return

        track = self.player.current
        if not track:
            self._v2_title_text.content = "### Music Dashboard"
            self._v2_meta_text.content = "\u200b"
            self._v2_progress_text.content = (
                f"{_PLAYBAR_LEFT_PAD}`{_progress_bar_blocks(0, 1, width=32)}  0:00 / 0:00`"
            )
            try:
                if self._v2_fallback_accessory:
                    self._v2_section.accessory = self._v2_fallback_accessory
            except Exception:
                pass
            self._v2_thumb_url = ""
            return

        position = self._display_position_ms()
        length = int(getattr(track, "length", 0) or 0)
        block_bar = _progress_bar_blocks(position, length, width=32)
        timer_text = f"{_fmt_ms(position)} / {_fmt_ms(length)}" if length > 0 else "Live Stream"

        song_artist_label = _clip(
            f"{track.title} — {_display_artist_name(getattr(track, 'author', 'Unknown Artist'))}",
            96,
        )
        track_url = str(getattr(track, "uri", "") or "").strip()
        if track_url:
            safe_label = song_artist_label.replace("[", "(").replace("]", ")")
            self._v2_title_text.content = f"### [{safe_label}]({track_url})"
        else:
            self._v2_title_text.content = f"### {song_artist_label}"
        # AI DJ indicator for v2 layout
        dj_meta = "\u200b"
        from utils.dj import dj as ai_dj
        guild = getattr(self.player, "guild", None)
        guild_id_v2 = getattr(guild, "id", 0) if guild else 0
        if guild_id_v2 and ai_dj.is_active(guild_id_v2):
            dj_reason = getattr(track, "_dj_reason", None)
            explore_flag = bool(getattr(track, "_dj_exploration", False))
            model_ver = str(getattr(track, "_dj_model_version", "") or "").strip()
            badge = " | Explore" if explore_flag else ""
            model_txt = f" [{model_ver.upper()}]" if model_ver else ""
            if dj_reason:
                dj_meta = f"`AI DJ{model_txt}`{badge} | {dj_reason}"
            else:
                dj_meta = f"`AI DJ{model_txt}`{badge} | Curating your queue"
        self._v2_meta_text.content = dj_meta
        self._v2_progress_text.content = f"{_PLAYBAR_LEFT_PAD}`{block_bar}  {timer_text}`"
        artwork_url = str(getattr(track, "artwork", "") or "").strip()
        thumbnail_cls = getattr(discord.ui, "Thumbnail", None)
        if artwork_url == self._v2_thumb_url:
            return

        try:
            if artwork_url and thumbnail_cls:
                self._v2_section.accessory = thumbnail_cls(media=artwork_url)
            elif self._v2_fallback_accessory:
                self._v2_section.accessory = self._v2_fallback_accessory
            self._v2_thumb_url = artwork_url
        except Exception:
            pass

    def _sync_components(self) -> None:
        has_current = bool(self.player.current)
        queue_count = self.player.queue.count

        if has_current and self.player.paused:
            self.pause_resume.emoji = None
            self.pause_resume.label = "▷"
            self.pause_resume.style = discord.ButtonStyle.primary
        elif has_current:
            self.pause_resume.emoji = None
            self.pause_resume.label = "||"
            self.pause_resume.style = discord.ButtonStyle.secondary
        elif queue_count > 0:
            self.pause_resume.emoji = None
            self.pause_resume.label = "▷"
            self.pause_resume.style = discord.ButtonStyle.primary
        else:
            self.pause_resume.emoji = None
            self.pause_resume.label = "||"
            self.pause_resume.style = discord.ButtonStyle.secondary

        loop_emoji, loop_label, loop_style = self._loop_visual()
        self.toggle_loop.emoji = loop_emoji
        self.toggle_loop.label = loop_label
        self.toggle_loop.style = loop_style

        self.pause_resume.disabled = not has_current and queue_count == 0
        self.previous_track.disabled = not has_current
        self.skip_track.disabled = not has_current and queue_count == 0
        self.shuffle_queue.disabled = queue_count < 2
        self.open_lyrics.disabled = not has_current

        # AI DJ button sync
        from utils.dj import dj as ai_dj
        guild = getattr(self.player, "guild", None)
        guild_id = getattr(guild, "id", 0) if guild else 0
        dj_active = ai_dj.is_active(guild_id) if guild_id else False
        if dj_active:
            self.toggle_dj.style = discord.ButtonStyle.success
            self.toggle_dj.label = "DJ"
        else:
            self.toggle_dj.style = discord.ButtonStyle.secondary
            self.toggle_dj.label = "DJ"

        self._sync_v2_dashboard_card()

    @staticmethod
    def _track_key(track: Optional[wavelink.Playable]) -> str:
        if not track:
            return "none"
        return str(
            getattr(track, "identifier", None)
            or getattr(track, "uri", None)
            or getattr(track, "title", None)
            or "none"
        )

    def _reset_position_anchor(self, position_ms: Optional[int] = None) -> None:
        now = time.monotonic()
        if position_ms is None:
            position_ms = int(getattr(self.player, "position", 0) or 0)
        # Atomic tuple assignment to avoid torn reads from the refresh loop
        self._position_anchor = (self._track_key(self.player.current), max(0, int(position_ms)), now)

    def _display_position_ms(self) -> int:
        track = self.player.current
        if not track:
            self._position_anchor = ("none", 0, time.monotonic())
            return 0

        track_key = self._track_key(track)
        base = int(getattr(self.player, "position", 0) or 0)
        now = time.monotonic()

        # Read anchor atomically
        anchor_key, anchor_ms, anchor_at = self._position_anchor

        if track_key != anchor_key:
            self._position_anchor = (track_key, base, now)
            return base

        # Keep anchor in sync whenever Lavalink gives a newer authoritative position.
        if base > anchor_ms:
            self._position_anchor = (anchor_key, base, now)
            anchor_ms, anchor_at = base, now
        # Also trust explicit backward seeks/restarts from Lavalink.
        elif base + 800 < anchor_ms:
            self._position_anchor = (anchor_key, base, now)
            anchor_ms, anchor_at = base, now

        if bool(getattr(self.player, "paused", False)) or not bool(getattr(self.player, "playing", False)):
            return base

        estimated = anchor_ms + int((now - anchor_at) * 1000)
        if estimated < base:
            estimated = base

        length = int(getattr(track, "length", 0) or 0)
        if length > 0:
            return max(0, min(estimated, length))
        return max(0, estimated)

    def _render_state_key(self) -> tuple:
        current = self.player.current
        track_key = self._track_key(current)
        position_bucket = self._display_position_ms() // 250
        # AI DJ active state
        from utils.dj import dj as ai_dj
        guild = getattr(self.player, "guild", None)
        guild_id = getattr(guild, "id", 0) if guild else 0
        dj_active = ai_dj.is_active(guild_id) if guild_id else False
        return (
            track_key,
            position_bucket,
            bool(getattr(self.player, "paused", False)),
            int(getattr(self.player, "volume", 15)),
            int(self.player.queue.count),
            str(self.player.queue.mode),
            self._active_filter_name(),
            dj_active,
        )

    def _is_persistently_disconnected(self) -> bool:
        guild = getattr(self.player, "guild", None)
        voice_client = getattr(guild, "voice_client", None) if guild else None
        now = time.monotonic()
        if voice_client:
            self._voice_gap_started_at = None
            self._consecutive_voice_gap_ticks = 0
            return False

        self._consecutive_voice_gap_ticks += 1
        if self._voice_gap_started_at is None:
            self._voice_gap_started_at = now

        queue_obj = getattr(self.player, "queue", None)
        queue_empty = bool(getattr(queue_obj, "is_empty", True))
        idle = not bool(getattr(self.player, "current", None)) and queue_empty
        if not idle:
            return False

        gap_elapsed = now - float(self._voice_gap_started_at or now)
        return gap_elapsed >= self._voice_gap_stop_seconds

    async def _maybe_recover_stale_render(self) -> None:
        track = getattr(self.player, "current", None)
        if not track or bool(getattr(self.player, "paused", False)):
            return

        now = time.monotonic()
        if now - self._last_successful_render_at < self._stale_seconds:
            return
        if now - self._last_stale_recovery_at < self._stale_cooldown_seconds:
            return

        self._last_stale_recovery_at = now
        logging.warning(
            "dashboard_stale_watchdog_trigger stale_for=%.2fs %s",
            now - self._last_successful_render_at,
            self._dashboard_scope_label(),
        )
        try:
            await self.update_player_message(force=True)
        except Exception as exc:
            logging.warning("dashboard_stale_watchdog_update_failed error=%s %s", exc, self._dashboard_scope_label())

    async def _auto_refresh_loop(self) -> None:
        exit_reason = "unknown"
        logging.info("dashboard_loop_start interval=%.2f %s", self._refresh_interval, self._dashboard_scope_label())
        try:
            while True:
                await asyncio.sleep(self._refresh_interval)
                if self._is_persistently_disconnected():
                    exit_reason = "persistent_disconnect_idle"
                    break
                if not self.message:
                    # Don't self-heal before the dashboard has ever been posted.
                    # During initial creation, message send/edit can take longer than
                    # one refresh tick; rebinding here would create a duplicate panel.
                    if not self._has_ever_bound_message:
                        continue
                    # Self-heal if the dashboard message disappeared.
                    await self._rebind_dashboard_message()
                    continue
                if not self._has_ever_bound_message:
                    self._has_ever_bound_message = True
                try:
                    await self.update_player_message()
                except Exception as exc:
                    logging.warning("dashboard_auto_refresh_error error=%s %s", exc, self._dashboard_scope_label())
                await self._maybe_recover_stale_render()
        except asyncio.CancelledError:
            exit_reason = "cancelled"
        except Exception as exc:
            exit_reason = "exception"
            logging.exception("dashboard_loop_crash error=%s %s", exc, self._dashboard_scope_label())
        finally:
            logging.info("dashboard_loop_stop reason=%s %s", exit_reason, self._dashboard_scope_label())

    async def _rebind_dashboard_message(self) -> None:
        async with self._rebind_lock:
            if self.message:
                return

            now = time.monotonic()
            if now - self._last_rebind_attempt_at < 8.0:
                return
            self._last_rebind_attempt_at = now

            # If player still points to a valid dashboard message, recover it
            # instead of creating a second dashboard.
            existing_message = getattr(self.player, "ui_message", None)
            if existing_message:
                try:
                    if self._use_v2_layout:
                        await existing_message.edit(view=self)
                    else:
                        await existing_message.edit(embed=self.build_embed(), view=self)
                    self.message = existing_message
                    self._has_ever_bound_message = True
                    self._last_render_key = None
                    self._consecutive_refresh_failures = 0
                    self._last_successful_render_at = time.monotonic()
                    self._last_rendered_track_key = self._track_key(getattr(self.player, "current", None))
                    logging.info("dashboard_rebind_success mode=reuse_existing %s", self._dashboard_scope_label())
                    return
                except discord.NotFound:
                    pass
                except discord.HTTPException as exc:
                    logging.warning("dashboard_rebind_failure mode=reuse_existing error=%s %s", exc, self._dashboard_scope_label())
                except Exception as exc:
                    logging.warning(
                        "dashboard_rebind_failure mode=reuse_existing_unexpected error=%s %s",
                        exc,
                        self._dashboard_scope_label(),
                    )

            home = getattr(self.player, "home", None)
            if not home:
                return

            try:
                if self._use_v2_layout:
                    msg = await home.send(view=self)
                else:
                    msg = await home.send(embed=self.build_embed(), view=self)
            except Exception as exc:
                logging.warning("dashboard_rebind_failure mode=send_new error=%s %s", exc, self._dashboard_scope_label())
                return

            old_message = self.message
            player_message = getattr(self.player, "ui_message", None)
            self.message = msg
            self._has_ever_bound_message = True
            self._last_render_key = None
            self._consecutive_refresh_failures = 0
            self._last_successful_render_at = time.monotonic()
            self._last_rendered_track_key = self._track_key(getattr(self.player, "current", None))
            try:
                setattr(self.player, "ui_message", msg)
            except Exception:
                pass
            logging.info("dashboard_rebind_success mode=send_new %s", self._dashboard_scope_label())

            stale_messages = []
            for candidate in (old_message, player_message):
                if not candidate:
                    continue
                if getattr(candidate, "id", None) == getattr(msg, "id", None):
                    continue
                if any(getattr(existing, "id", None) == getattr(candidate, "id", None) for existing in stale_messages):
                    continue
                stale_messages.append(candidate)
            for stale_message in stale_messages:
                try:
                    await stale_message.delete()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stopped = True
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        super().stop()

    def _live_player(self, interaction: discord.Interaction) -> Optional[wavelink.Player]:
        if not interaction.guild:
            return None
        voice_client = interaction.guild.voice_client
        if isinstance(voice_client, wavelink.Player):
            self.player = voice_client
            return voice_client
        return None

    async def _require_live_player(
        self,
        interaction: discord.Interaction,
    ) -> Optional[wavelink.Player]:
        return self._live_player(interaction)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False

        channel = getattr(self.player, "channel", None)
        # If the player is currently disconnected, still allow controls like Add/Lists.
        if channel is None:
            return True

        member_voice = interaction.user.voice.channel if interaction.user.voice else None
        if channel and member_voice and member_voice.id == channel.id:
            return True

        try:
            message = "Join my voice channel to use these controls."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        if isinstance(error, wavelink.LavalinkException) and error.status == 404:
            # Session expired or websocket desynced; player needs a fresh voice handshake.
            logging.warning("Dashboard: stale Lavalink session on %s (404). Player needs to be re-opened.", type(item).__name__)
            msg = "The audio session expired. Please use `/music` to re-open the player."
        else:
            logging.exception("Dashboard interaction error on %s: %s", type(item).__name__, error)
            msg = "That control failed. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    async def update_player_message(self, *, force: bool = False) -> None:
        if not self.message:
            return

        should_rebind = False
        async with self._render_lock:
            if not self.message:
                return
            self._has_ever_bound_message = True

            new_key = self._render_state_key()
            if not force and self._last_render_key == new_key:
                return

            self._sync_components()
            try:
                if self._use_v2_layout:
                    await asyncio.wait_for(self.message.edit(view=self), timeout=self._edit_timeout_seconds)
                else:
                    embed = self.build_embed()
                    await asyncio.wait_for(
                        self.message.edit(embed=embed, view=self), timeout=self._edit_timeout_seconds
                    )
                self._last_render_key = new_key
                self._consecutive_refresh_failures = 0
                self._last_refresh_failure_at = 0.0
                self._last_successful_render_at = time.monotonic()
                self._last_rendered_track_key = self._track_key(getattr(self.player, "current", None))
            except asyncio.TimeoutError:
                self._last_render_key = None
                self._consecutive_refresh_failures += 1
                self._last_refresh_failure_at = time.monotonic()
                logging.warning(
                    "dashboard_render_timeout fail_count=%s timeout=%.2fs %s",
                    self._consecutive_refresh_failures,
                    self._edit_timeout_seconds,
                    self._dashboard_scope_label(),
                )
            except discord.NotFound:
                self.message = None
                self._last_render_key = None
                self._consecutive_refresh_failures += 1
                self._last_refresh_failure_at = time.monotonic()
                logging.warning(
                    "dashboard_render_missing_message fail_count=%s %s",
                    self._consecutive_refresh_failures,
                    self._dashboard_scope_label(),
                )
                should_rebind = True
            except discord.HTTPException as exc:
                self._last_render_key = None
                self._consecutive_refresh_failures += 1
                self._last_refresh_failure_at = time.monotonic()
                if int(getattr(exc, "code", 0) or 0) == 10008:
                    # Unknown message: dashboard was removed. Trigger rebind.
                    self.message = None
                    should_rebind = True
                    logging.warning(
                        "dashboard_render_http_unknown_message fail_count=%s error=%s %s",
                        self._consecutive_refresh_failures,
                        exc,
                        self._dashboard_scope_label(),
                    )
                else:
                    logging.warning(
                        "dashboard_render_http_error fail_count=%s error=%s %s",
                        self._consecutive_refresh_failures,
                        exc,
                        self._dashboard_scope_label(),
                    )

            if (
                self._consecutive_refresh_failures >= self._refresh_fail_rebind_threshold
                and not should_rebind
            ):
                should_rebind = True
                self.message = None
                logging.warning(
                    "dashboard_rebind_forced fail_count=%s threshold=%s %s",
                    self._consecutive_refresh_failures,
                    self._refresh_fail_rebind_threshold,
                    self._dashboard_scope_label(),
                )

        if should_rebind:
            await self._rebind_dashboard_message()

    def _active_filter_name(self) -> str:
        preset_names = {
            "none": "None",
            "bass_boost": "Bass Boost",
            "treble_boost": "Treble Boost",
            "nightcore": "Nightcore",
            "vaporwave": "Vaporwave",
            "karaoke": "Karaoke",
            "eight_d": "8D",
            "low_pass": "LowPass",
        }
        preset = self._active_filter_value()
        if preset in preset_names:
            return preset_names[preset]

        return "None"

    def _active_filter_value(self) -> str:
        value = str(getattr(self.player, "ui_filter_preset", "none") or "none")
        allowed = {
            "none",
            "bass_boost",
            "treble_boost",
            "nightcore",
            "vaporwave",
            "karaoke",
            "eight_d",
            "low_pass",
        }
        if value in allowed:
            return value
        return "none"

    def build_embed(self) -> discord.Embed:
        track = self.player.current

        if not track:
            embed = discord.Embed(
                title="Music Dashboard",
                description=(
                    "### Ready\n\n"
                    "Use **➕ Add** to queue any song or URL.\n"
                    "Use **🎵 Lists** to save/play your Spotify playlists."
                ),
                color=discord.Color.blurple(),
            )
            return embed

        track_url = track.uri if getattr(track, "uri", None) else None
        song_artist_label = _clip(
            f"{track.title} - {_display_artist_name(getattr(track, 'author', 'Unknown Artist'))}",
            110,
        )
        if track_url:
            title_line = f"[{song_artist_label}]({track_url})"
        else:
            title_line = song_artist_label

        position = self._display_position_ms()
        length = int(getattr(track, "length", 0) or 0)
        block_bar = _progress_bar_blocks(position, length, width=32)
        timer_text = f"{_fmt_ms(position)} / {_fmt_ms(length)}" if length > 0 else "Live Stream"

        # AI DJ indicator
        dj_line = ""
        from utils.dj import dj as ai_dj
        guild = getattr(self.player, "guild", None)
        guild_id = getattr(guild, "id", 0) if guild else 0
        if guild_id and ai_dj.is_active(guild_id):
            dj_reason = getattr(track, "_dj_reason", None)
            explore_flag = bool(getattr(track, "_dj_exploration", False))
            model_ver = str(getattr(track, "_dj_model_version", "") or "").strip()
            badge = " | Explore" if explore_flag else ""
            model_txt = f" [{model_ver.upper()}]" if model_ver else ""
            if dj_reason:
                dj_line = f"\n`AI DJ{model_txt}`{badge} | {dj_reason}"
            else:
                dj_line = f"\n`AI DJ{model_txt}`{badge} | Curating your queue"

        embed = discord.Embed(
            title="Music Dashboard",
            description=(
                f"### Now Playing\n"
                f"{title_line}\n\n"
                f"{_PLAYBAR_LEFT_PAD}`{block_bar}  {timer_text}`"
                f"{dj_line}"
            ),
            color=0x2B2D31,
        )

        artwork_url = str(getattr(track, "artwork", "") or "").strip()
        if artwork_url:
            embed.set_image(url=artwork_url)
        else:
            # Force a deterministic fallback so Discord clients don't keep showing stale artwork.
            embed.set_image(url=self._default_cover_url())
        return embed

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="|◁", row=0)
    async def previous_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return
        await interaction.response.defer()
        if player.current:
            await player.seek(0)
            self._reset_position_anchor(0)
        await self.update_player_message(force=True)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="||", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return
        await interaction.response.defer()
        if player.current:
            if player.paused:
                await player.pause(False)
            else:
                await player.pause(True)
        elif not player.queue.is_empty:
            volume_to_use = int(getattr(player, "volume", 15))
            music_cog = _get_music_cog(interaction)
            if music_cog and hasattr(music_cog, "_effective_player_volume"):
                try:
                    volume_to_use = music_cog._effective_player_volume(player)
                except Exception:
                    pass
            await player.play(player.queue.get(), volume=volume_to_use)
            if player.paused:
                try:
                    await player.pause(False)
                except Exception:
                    pass
        await asyncio.sleep(0.15)
        await self.update_player_message(force=True)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="▷|", row=0)
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return
        music_cog = _get_music_cog(interaction)
        if not music_cog or not hasattr(music_cog, "_skip_or_start_next"):
            await interaction.response.send_message("Skip is temporarily unavailable.")
            return

        block_reason = None
        if hasattr(music_cog, "_queue_action_block_message"):
            block_reason = music_cog._queue_action_block_message(player)
        if block_reason:
            await interaction.response.send_message(
                f"⏳ {block_reason}. Please wait a moment.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            await music_cog._skip_or_start_next(player)
        except wavelink.LavalinkException:
            raise  # Let on_error handle stale-session 404s with a proper message.
        except Exception as exc:
            logging.warning("Dashboard skip failed unexpectedly: %s", exc)
        await asyncio.sleep(0.15)
        await self.update_player_message(force=True)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🔀", label="", row=1)
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return
        music_cog = _get_music_cog(interaction)
        block_reason = None
        if music_cog and hasattr(music_cog, "_queue_action_block_message"):
            block_reason = music_cog._queue_action_block_message(player)
        if block_reason:
            await interaction.response.send_message(
                f"⏳ {block_reason}. Please wait a moment.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        if player.queue.count > 1:
            player.queue.shuffle()
        await self.update_player_message(force=True)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🔁", label="", row=0)
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return
        await interaction.response.defer()
        if player.queue.mode == wavelink.QueueMode.normal:
            player.queue.mode = wavelink.QueueMode.loop
        elif player.queue.mode == wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.loop_all
        else:
            player.queue.mode = wavelink.QueueMode.normal

        await self.update_player_message(force=True)

    @discord.ui.button(style=discord.ButtonStyle.success, emoji="➕", label="", row=1)
    async def add_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddTrackModal())

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🎤", label="", row=1)
    async def open_lyrics(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        music_cog = _get_music_cog(interaction)
        if not music_cog or not hasattr(music_cog, "_send_current_lyrics"):
            await interaction.response.send_message("Lyrics are temporarily unavailable.", ephemeral=True)
            return
        await music_cog._send_current_lyrics(interaction, ephemeral=False)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="Q", row=1)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._live_player(interaction)
        lines = build_queue_lines(player) if player else ["No active player. Use **Add** or **Lists** to start playback."]
        view = QueuePaginatorView(lines=lines, title="Queue", player=player)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
        try:
            view.message = await interaction.original_response()
        except discord.NotFound:
            pass

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="💾", label="", row=2)
    async def manage_lists(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = SavedPlaylistManagerView(
            self,
            owner_user_id=int(interaction.user.id),
        )
        await interaction.response.send_message(
            content=view._build_content(),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.NotFound:
            pass
        await view.refresh_preview(force_update=view.play_order == "recent")
        await view.refresh_message()

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🔊", label="", row=0)
    async def volume_set(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._live_player(interaction)
        current_volume = int(getattr(player, "volume", 15)) if player else 15
        music_cog = _get_music_cog(interaction)
        if player and music_cog and hasattr(music_cog, "_effective_player_volume"):
            try:
                current_volume = music_cog._effective_player_volume(player)
            except Exception:
                pass
        await interaction.response.send_modal(VolumeModal(self, current_volume=current_volume))

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="Fx", row=1)
    async def filter_popup(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = AudioFilterPickerView(self)
        await interaction.response.send_message(
            content=f"Select audio filter (current: **{self._active_filter_name()}**).",
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.NotFound:
            pass

    async def toggle_ai_dj(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._require_live_player(interaction)
        if not player:
            await interaction.response.send_message("No active player.", ephemeral=True)
            return

        from utils.dj import dj as ai_dj, SeedTrack

        guild = getattr(player, "guild", None)
        guild_id = getattr(guild, "id", 0) if guild else 0
        if not guild_id:
            await interaction.response.send_message("Could not determine guild.", ephemeral=True)
            return

        if ai_dj.is_active(guild_id):
            # Turn OFF
            ai_dj.stop_session(guild_id)
            music_cog = _get_music_cog(interaction)
            if music_cog and hasattr(music_cog, "_cancel_pending_dj_tasks"):
                try:
                    music_cog._cancel_pending_dj_tasks(guild_id)
                except Exception:
                    pass
            await interaction.response.defer()
            await self.update_player_message(force=True)
        else:
            # Turn ON — seed with current track
            seed = None
            if player.current:
                seed = SeedTrack(
                    title=getattr(player.current, "_source_title", None) or player.current.title,
                    artist=getattr(player.current, "_source_artist", None) or getattr(player.current, "author", "Unknown"),
                    duration_ms=int(getattr(player.current, "length", 0) or 0),
                )
            if not seed:
                await interaction.response.send_message(
                    "Play a track first, then enable AI DJ.", ephemeral=True
                )
                return

            ai_dj.start_session(guild_id, seed=seed)
            await interaction.response.defer()
            # Pre-fetch recommendations immediately
            ai_dj.trigger_prefetch(guild_id)
            await self.update_player_message(force=True)
