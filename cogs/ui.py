import os
import logging
import asyncio
import time
import re
import sqlite3
import discord
import wavelink
from typing import Optional, Any

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_PLAYLIST_DB_PATH = os.path.join(_DATA_DIR, "music_bot.db")
_SAVED_PLAYLIST_DB_READY = False
_MAX_SAVED_PLAYLISTS = 3
_PLAYBAR_LEFT_PAD = "\u2002\u2002"

_HAS_LAYOUT_V2 = all(
    hasattr(discord.ui, name)
    for name in ("LayoutView", "Container", "Section", "TextDisplay", "Thumbnail", "ActionRow")
)
_DashboardBaseView = discord.ui.LayoutView if _HAS_LAYOUT_V2 else discord.ui.View

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
    conn = sqlite3.connect(_PLAYLIST_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_scope_id(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_playlists (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, url)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_saved_playlists_scope_saved_at
            ON saved_playlists (guild_id, user_id, saved_at DESC)
            """
        )
        conn.commit()

    _SAVED_PLAYLIST_DB_READY = True


def _load_saved_playlists(guild_id: int, user_id: int) -> list[dict[str, Any]]:
    guild_scope = _normalize_scope_id(guild_id)
    user_scope = _normalize_scope_id(user_id)
    _ensure_saved_playlists_storage()
    with _db_connect() as conn:
        rows = conn.execute(
            """
            SELECT url, saved_at
            FROM saved_playlists
            WHERE guild_id = ? AND user_id = ?
            ORDER BY saved_at DESC
            LIMIT ?
            """,
            (guild_scope, user_scope, _MAX_SAVED_PLAYLISTS),
        ).fetchall()

    parsed = []
    for row in rows:
        url = _normalize_spotify_playlist_url(str(row["url"]))
        if not url:
            continue
        parsed.append({"url": url, "saved_at": int(row["saved_at"])})
    return _sanitize_saved_playlist_items(parsed)


def _save_saved_playlists(guild_id: int, user_id: int, playlists: list[dict[str, Any]]) -> None:
    guild_scope = _normalize_scope_id(guild_id)
    user_scope = _normalize_scope_id(user_id)
    _ensure_saved_playlists_storage()
    cleaned = _sanitize_saved_playlist_items(playlists)
    with _db_connect() as conn:
        conn.execute(
            "DELETE FROM saved_playlists WHERE guild_id = ? AND user_id = ?",
            (guild_scope, user_scope),
        )
        conn.executemany(
            "INSERT INTO saved_playlists (guild_id, user_id, url, saved_at) VALUES (?, ?, ?, ?)",
            [(guild_scope, user_scope, item["url"], int(item["saved_at"])) for item in cleaned],
        )
        conn.commit()


def _upsert_saved_playlist(url: str, guild_id: int, user_id: int) -> tuple[list[dict[str, Any]], bool]:
    playlists = _load_saved_playlists(guild_id, user_id)
    playlists = [item for item in playlists if item.get("url") != url]
    playlists.insert(0, {"url": url, "saved_at": int(time.time())})
    trimmed = len(playlists) > _MAX_SAVED_PLAYLISTS
    playlists = playlists[:_MAX_SAVED_PLAYLISTS]
    _save_saved_playlists(guild_id, user_id, playlists)
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
        owner_guild_id = (
            self.parent_view.owner_guild_id
            if self.parent_view
            else _normalize_scope_id(getattr(getattr(interaction, "guild", None), "id", 0))
        )
        playlists, trimmed = _upsert_saved_playlist(url, owner_guild_id, owner_user_id)
        token = _playlist_token_from_url(url)
        if trimmed:
            message = (
                f"Saved playlist `{token}`. Kept most recent {_MAX_SAVED_PLAYLISTS} saved lists "
                "(oldest one was removed)."
            )
        else:
            message = f"Saved playlist `{token}`."

        await interaction.response.send_message(message, ephemeral=True)

        if self.parent_view:
            self.parent_view.playlists = playlists
            self.parent_view.selected_index = 0
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
        _save_saved_playlists(self.parent_view.owner_guild_id, self.parent_view.owner_user_id, deduped)

        self.parent_view.playlists = deduped
        self.parent_view.selected_index = min(self.index, len(deduped) - 1) if deduped else -1

        await interaction.response.send_message("Saved playlist URL updated.", ephemeral=True)
        await self.parent_view.refresh_message()


class SavedPlaylistManagerView(discord.ui.View):
    def __init__(self, dashboard: "SpotifyUI", owner_user_id: int, owner_guild_id: int):
        super().__init__(timeout=180)
        self.dashboard = dashboard
        self.owner_user_id = int(owner_user_id)
        self.owner_guild_id = _normalize_scope_id(owner_guild_id)
        self.playlists: list[dict[str, Any]] = _load_saved_playlists(self.owner_guild_id, self.owner_user_id)
        self.selected_index: int = 0 if self.playlists else -1
        default_order = str(getattr(self.dashboard, "_saved_playlist_order", "recent") or "recent")
        self.play_order = default_order if default_order in {"recent", "spotify"} else "recent"
        self.message: Optional[discord.Message] = None
        self._sync_components()

    def _order_label(self) -> str:
        return "Recently Added" if self.play_order == "recent" else "Spotify Order"

    def _selected_url(self) -> str:
        if 0 <= self.selected_index < len(self.playlists):
            return str(self.playlists[self.selected_index].get("url", ""))
        return ""

    def _build_content(self) -> str:
        lines = [
            f"Saved playlists (`{len(self.playlists)}/{_MAX_SAVED_PLAYLISTS}`)",
            "",
        ]
        if not self.playlists:
            lines.append("No saved playlists yet. Use **Add New** below.")
        else:
            for idx, item in enumerate(self.playlists, start=1):
                url = str(item.get("url", ""))
                token = _playlist_token_from_url(url)
                marker = "▶️" if idx - 1 == self.selected_index else "•"
                lines.append(f"{marker} **{idx}.** [`{token}`]({url})")
        lines.append("")
        lines.append(f"Current order: **{self._order_label()}**")
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
        await interaction.response.edit_message(content=self._build_content(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="Order: Recently Added", row=1)
    async def toggle_order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.play_order = "spotify" if self.play_order == "recent" else "recent"
        setattr(self.dashboard, "_saved_playlist_order", self.play_order)
        self._sync_components()
        await interaction.response.edit_message(content=self._build_content(), view=self)

    @discord.ui.button(style=discord.ButtonStyle.success, emoji=None, label="Play", row=1)
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

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji=None, label="Edit", row=2)
    async def edit_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_index < 0 or self.selected_index >= len(self.playlists):
            await interaction.response.send_message("No saved playlist selected.", ephemeral=True)
            return
        await interaction.response.send_modal(EditSavedPlaylistModal(self, self.selected_index))

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="💾", label="", row=2)
    async def save_playlist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SavePlaylistModal(parent_view=self))

    @discord.ui.button(style=discord.ButtonStyle.danger, emoji="🗑", label="", row=2)
    async def delete_selected(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected_index < 0 or self.selected_index >= len(self.playlists):
            await interaction.response.send_message("No saved playlist selected.", ephemeral=True)
            return

        removed = self.playlists.pop(self.selected_index)
        _save_saved_playlists(self.owner_guild_id, self.owner_user_id, self.playlists)

        if self.playlists:
            self.selected_index = min(self.selected_index, len(self.playlists) - 1)
        else:
            self.selected_index = -1

        self._sync_components()
        await interaction.response.edit_message(
            content=f"Deleted `{_playlist_token_from_url(str(removed.get('url', '')))}`.\n\n{self._build_content()}",
            view=self,
        )


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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
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

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page_index > 0:
            self.page_index -= 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.page_index < self.total_pages - 1:
            self.page_index += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Remove Track", style=discord.ButtonStyle.secondary, row=1)
    async def remove_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.player or self.player.queue.is_empty:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveTrackModal(self))

    @discord.ui.button(label="Clear Queue", style=discord.ButtonStyle.danger, row=1)
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
        self._use_v2_layout = _HAS_LAYOUT_V2
        self._v2_header_container = None
        self._v2_section = None
        self._v2_title_text = None
        self._v2_meta_text = None
        self._v2_progress_text = None
        self._v2_fallback_accessory = None
        self._v2_thumb_url: str = ""
        self._position_anchor_ms: int = int(getattr(player, "position", 0) or 0)
        self._position_anchor_at: float = time.monotonic()
        self._position_anchor_track_key: str = self._track_key(player.current)
        refresh_raw = (os.getenv("UI_REFRESH_INTERVAL_SECONDS") or "0.49").strip()
        try:
            self._refresh_interval = max(0.49, float(refresh_raw))
        except ValueError:
            self._refresh_interval = 0.49
        self._setup_controls_for_layout_if_needed()
        self._setup_v2_layout_shell()
        self._reorder_fallback_controls_if_needed()
        self._refresh_task: Optional[asyncio.Task] = asyncio.create_task(self._auto_refresh_loop())
        self._last_render_key: Optional[tuple] = None
        self._sync_components()

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
        self.show_queue = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="Q", row=1
        )
        self.manage_lists = discord.ui.Button(
            style=discord.ButtonStyle.primary, emoji="💾", label="", row=1
        )
        self.volume_set = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="🔊", label="", row=0
        )
        self.filter_popup = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji=None, label="Fx", row=1
        )

        self.previous_track.callback = self._manual_previous_track
        self.pause_resume.callback = self._manual_pause_resume
        self.skip_track.callback = self._manual_skip_track
        self.shuffle_queue.callback = self._manual_shuffle_queue
        self.toggle_loop.callback = self._manual_toggle_loop
        self.add_track.callback = self._manual_add_track
        self.show_queue.callback = self._manual_show_queue
        self.manage_lists.callback = self._manual_manage_lists
        self.volume_set.callback = self._manual_volume_set
        self.filter_popup.callback = self._manual_filter_popup

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
            self.show_queue,
            self.manage_lists,
            self.add_track,
        )

        self.add_item(self._v2_controls_row_primary)
        self.add_item(self._v2_controls_row_queue)

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
            self.show_queue,
            self.manage_lists,
            self.add_track,
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

    async def _manual_show_queue(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.show_queue(self, interaction, self.show_queue)

    async def _manual_manage_lists(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.manage_lists(self, interaction, self.manage_lists)

    async def _manual_volume_set(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.volume_set(self, interaction, self.volume_set)

    async def _manual_filter_popup(self, interaction: discord.Interaction) -> None:
        await SpotifyUI.filter_popup(self, interaction, self.filter_popup)

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
        self._v2_meta_text.content = "\u200b"
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

    def _display_position_ms(self) -> int:
        track = self.player.current
        if not track:
            self._position_anchor_track_key = "none"
            self._position_anchor_ms = 0
            self._position_anchor_at = time.monotonic()
            return 0

        track_key = self._track_key(track)
        base = int(getattr(self.player, "position", 0) or 0)
        now = time.monotonic()

        if track_key != self._position_anchor_track_key:
            self._position_anchor_track_key = track_key
            self._position_anchor_ms = base
            self._position_anchor_at = now
            return base

        # Keep anchor in sync whenever Lavalink gives a newer authoritative position.
        if base > self._position_anchor_ms:
            self._position_anchor_ms = base
            self._position_anchor_at = now

        if bool(getattr(self.player, "paused", False)) or not bool(getattr(self.player, "playing", False)):
            return base

        estimated = self._position_anchor_ms + int((now - self._position_anchor_at) * 1000)
        if estimated < base:
            estimated = base

        # Prevent runaway local drift if websocket updates lag.
        if estimated - base > 1800:
            estimated = base + 1800

        length = int(getattr(track, "length", 0) or 0)
        if length > 0:
            return max(0, min(estimated, length))
        return max(0, estimated)

    def _render_state_key(self) -> tuple:
        current = self.player.current
        track_key = self._track_key(current)
        position_bucket = self._display_position_ms() // 250
        return (
            track_key,
            position_bucket,
            bool(getattr(self.player, "paused", False)),
            int(getattr(self.player, "volume", 15)),
            int(self.player.queue.count),
            str(self.player.queue.mode),
            self._active_filter_name(),
        )

    async def _auto_refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._refresh_interval)
                if not self.message:
                    continue
                try:
                    await self.update_player_message()
                except Exception as exc:
                    logging.debug("Dashboard auto-refresh error (continuing): %s", exc)
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
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
            return True

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

        new_key = self._render_state_key()
        if not force and self._last_render_key == new_key:
            return

        self._sync_components()
        try:
            if self._use_v2_layout:
                await self.message.edit(view=self)
            else:
                embed = self.build_embed()
                await self.message.edit(embed=embed, view=self)
            self._last_render_key = new_key
        except discord.NotFound:
            self.message = None
        except discord.HTTPException as exc:
            logging.debug("Dashboard refresh skipped due to HTTP error: %s", exc)

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

        embed = discord.Embed(
            title="Music Dashboard",
            description=(
                f"### Now Playing\n"
                f"{title_line}\n\n"
                f"{_PLAYBAR_LEFT_PAD}`{block_bar}  {timer_text}`"
            ),
            color=0x2B2D31,
        )

        if getattr(track, "artwork", None):
            embed.set_image(url=track.artwork)
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
        await self.update_player_message()

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
            await player.play(player.queue.get(), volume=int(getattr(player, "volume", 15)))
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

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="💾", label="", row=1)
    async def manage_lists(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = SavedPlaylistManagerView(
            self,
            owner_user_id=int(interaction.user.id),
            owner_guild_id=_normalize_scope_id(getattr(getattr(interaction, "guild", None), "id", 0)),
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

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🔊", label="", row=0)
    async def volume_set(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self._live_player(interaction)
        current_volume = int(getattr(player, "volume", 15)) if player else 15
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
