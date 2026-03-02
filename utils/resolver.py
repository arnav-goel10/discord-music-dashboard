import aiohttp
import asyncio
from bs4 import BeautifulSoup
import base64
import datetime
from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any, List, Optional
import urllib.parse

import spotipy
from spotipy.exceptions import SpotifyException

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
from spotipy.oauth2 import SpotifyClientCredentials

@dataclass
class ResolvedTrack:
    title: str
    artist: str
    added_at: Optional[str] = None
    
    @property
    def query(self) -> str:
        return f"{self.title} {self.artist}".strip()

class SpotifyResolver:
    def __init__(self):
        self._session = None
        self._spotify = None
        self._spotify_client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip()
        self._spotify_client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip()
        self._spotify_user_token = (os.getenv("SPOTIFY_USER_ACCESS_TOKEN") or "").strip()
        self._spotify_refresh_token = (os.getenv("SPOTIFY_USER_REFRESH_TOKEN") or "").strip()
        self._spotify_user_token_expires_at = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        if self._spotify_user_token:
            expires_raw = (os.getenv("SPOTIFY_USER_TOKEN_EXPIRES_AT") or "").strip()
            if expires_raw:
                try:
                    self._spotify_user_token_expires_at = datetime.datetime.fromtimestamp(
                        int(expires_raw), tz=datetime.timezone.utc
                    )
                except ValueError:
                    logging.warning("Invalid SPOTIFY_USER_TOKEN_EXPIRES_AT value; using short-lived token window.")
                    # If no expiry metadata is provided, keep the token for a short window
                    # and then force a refresh (if refresh token is configured).
                    self._spotify_user_token_expires_at = datetime.datetime.now(
                        datetime.timezone.utc
                    ) + datetime.timedelta(minutes=45)
            else:
                self._spotify_user_token_expires_at = datetime.datetime.now(
                    datetime.timezone.utc
                ) + datetime.timedelta(minutes=45)

        self._last_failure_code: Optional[str] = None
        self._last_failure_message: str = ""
        self.cache_file = os.path.join(_DATA_DIR, "playlists.json")
        self.cache = self._load_cache()
        
        # Initialize Spotipy if credentials exist
        client_id = self._spotify_client_id
        client_secret = self._spotify_client_secret
        if client_id and client_secret:
            try:
                auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
                self._spotify = spotipy.Spotify(auth_manager=auth_manager)
            except Exception as e:
                logging.error(f"Failed to initialize Spotipy: {e}")
        elif self._spotify_user_token:
            logging.warning(
                "SPOTIFY_USER_ACCESS_TOKEN is set but SPOTIFY_CLIENT_ID/SECRET is missing. "
                "Token refresh will not work after expiry."
            )

        if self._spotify_user_token or self._spotify_refresh_token:
            logging.info(
                "Spotify resolver initialized with user auth support (refresh_token=%s).",
                bool(self._spotify_refresh_token),
            )
        elif self._spotify:
            logging.info("Spotify resolver initialized with app-only credentials.")
        else:
            logging.warning("Spotify resolver initialized without Spotify credentials.")

    @property
    def session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        return self._session

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        return loaded
                    logging.warning("Playlist cache is not a dict; ignoring cache file.")
        except Exception as e:
            logging.error(f"Error loading playlist cache: {e}")
        return {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=4)
        except Exception as e:
            logging.error(f"Error saving playlist cache: {e}")

    def _deserialize_tracks(self, raw_tracks: Any) -> List[ResolvedTrack]:
        if not isinstance(raw_tracks, list):
            return []

        parsed: List[ResolvedTrack] = []
        for item in raw_tracks:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            artist = str(item.get("artist", "")).strip()
            if title and artist:
                added_at_raw = item.get("added_at")
                added_at = str(added_at_raw).strip() if isinstance(added_at_raw, str) else None
                parsed.append(ResolvedTrack(title=title, artist=artist, added_at=added_at or None))
        return parsed

    def _get_cached_playlist_tracks(self, playlist_key: str, force_cache_bypass: bool) -> List[ResolvedTrack]:
        data = self.cache.get(playlist_key)
        if not isinstance(data, dict):
            return []

        tracks = self._deserialize_tracks(data.get("tracks", []))
        if not tracks:
            return []

        if force_cache_bypass:
            logging.info(f"💾 Loaded PLAYLIST from local hard drive cache (forced): {playlist_key}")
            return tracks

        timestamp = data.get("timestamp")
        if not isinstance(timestamp, str):
            return []

        try:
            saved_time = datetime.datetime.fromisoformat(timestamp)
        except ValueError:
            logging.warning(f"Invalid cache timestamp for {playlist_key}; refreshing snapshot.")
            return []

        if datetime.datetime.now() - saved_time < datetime.timedelta(days=7):
            logging.info(f"💾 Loaded PLAYLIST from local hard drive cache: {playlist_key}")
            return tracks

        return []

    def _save_playlist_snapshot(self, playlist_key: str, tracks: List[ResolvedTrack]) -> None:
        if not tracks:
            return

        self.cache[playlist_key] = {
            "timestamp": datetime.datetime.now().isoformat(),
            "tracks": [
                {
                    "title": t.title,
                    "artist": t.artist,
                    "added_at": t.added_at,
                }
                for t in tracks
            ],
        }
        self._save_cache()

    def has_cached_playlist(self, url: str) -> bool:
        playlist_key = url.split("?")[0]
        data = self.cache.get(playlist_key)
        if not isinstance(data, dict):
            return False
        return bool(self._deserialize_tracks(data.get("tracks", [])))

    @property
    def last_failure_code(self) -> Optional[str]:
        return self._last_failure_code

    @property
    def last_failure_message(self) -> str:
        return self._last_failure_message

    def _set_failure(self, code: str, message: str) -> None:
        self._last_failure_code = code
        self._last_failure_message = message

    def _clear_failure(self) -> None:
        self._last_failure_code = None
        self._last_failure_message = ""

    @staticmethod
    def _extract_first_artist_name(artists: Any) -> str:
        if isinstance(artists, list):
            for artist in artists:
                if isinstance(artist, dict):
                    name = artist.get("name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

        if isinstance(artists, dict):
            items = artists.get("items", [])
            if isinstance(items, list):
                for artist in items:
                    if not isinstance(artist, dict):
                        continue
                    name = artist.get("name")
                    if not name and isinstance(artist.get("profile"), dict):
                        name = artist["profile"].get("name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

        return ""

    @staticmethod
    def _build_track(title: Any, artist: Any, *, added_at: Optional[Any] = None) -> Optional[ResolvedTrack]:
        if not isinstance(title, str) or not isinstance(artist, str):
            return None
        title = title.strip()
        artist = artist.strip()
        if not title or not artist:
            return None
        parsed_added_at = str(added_at).strip() if isinstance(added_at, str) and added_at.strip() else None
        return ResolvedTrack(title=title, artist=artist, added_at=parsed_added_at)

    def _get_app_access_token(self) -> Optional[str]:
        if not self._spotify:
            return None
        try:
            token = self._spotify.auth_manager.get_access_token(as_dict=False)
            if isinstance(token, dict):
                token = token.get("access_token")
            if isinstance(token, str) and token.strip():
                return token.strip()
        except Exception as exc:
            logging.error("Failed to acquire Spotify app access token: %s", exc)
        return None

    async def _refresh_user_access_token(self) -> Optional[str]:
        if not (
            self._spotify_refresh_token
            and self._spotify_client_id
            and self._spotify_client_secret
        ):
            return None

        credentials = f"{self._spotify_client_id}:{self._spotify_client_secret}".encode("utf-8")
        auth_header = base64.b64encode(credentials).decode("ascii")

        try:
            async with self.session.post(
                "https://accounts.spotify.com/api/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._spotify_refresh_token,
                },
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ) as response:
                raw = await response.text()
                try:
                    payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    payload = {}

                if response.status != 200:
                    self._set_failure(
                        "spotify_user_refresh_failed",
                        f"Spotify token refresh failed with HTTP {response.status}.",
                    )
                    logging.warning(
                        "Spotify user token refresh failed (status=%s): %s",
                        response.status,
                        payload or raw[:200],
                    )
                    return None

                access_token = payload.get("access_token")
                if not isinstance(access_token, str) or not access_token.strip():
                    self._set_failure(
                        "spotify_user_refresh_failed",
                        "Spotify token refresh returned no access_token.",
                    )
                    return None

                refresh_token = payload.get("refresh_token")
                if isinstance(refresh_token, str) and refresh_token.strip():
                    self._spotify_refresh_token = refresh_token.strip()

                expires_in = payload.get("expires_in")
                try:
                    ttl_seconds = int(expires_in)
                except (TypeError, ValueError):
                    ttl_seconds = 3600
                ttl_seconds = max(120, ttl_seconds)

                self._spotify_user_token = access_token.strip()
                self._spotify_user_token_expires_at = datetime.datetime.now(
                    datetime.timezone.utc
                ) + datetime.timedelta(seconds=ttl_seconds - 60)
                return self._spotify_user_token
        except Exception as exc:
            self._set_failure(
                "spotify_user_refresh_failed",
                f"Spotify token refresh request failed: {exc}",
            )
            logging.error("Spotify user token refresh error: %s", exc)
            return None

    async def _get_user_access_token(self) -> Optional[str]:
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._spotify_user_token and now < self._spotify_user_token_expires_at:
            return self._spotify_user_token

        if self._spotify_refresh_token:
            refreshed = await self._refresh_user_access_token()
            if refreshed:
                return refreshed

        if self._spotify_user_token:
            # Last resort: return the existing token even if we cannot validate expiry.
            return self._spotify_user_token
        return None

    async def _spotify_api_get(
        self,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        use_user_token: bool = False,
    ) -> tuple[Optional[dict[str, Any]], int]:
        token_label = "user" if use_user_token else "app"
        token_fetcher = self._get_user_access_token if use_user_token else None
        access_token = await token_fetcher() if token_fetcher else self._get_app_access_token()

        if not access_token:
            code = "spotify_user_auth_missing" if use_user_token else "spotify_app_auth_missing"
            self._set_failure(code, f"Spotify {token_label} access token is not available.")
            return None, 0

        url = f"https://api.spotify.com/v1/{endpoint.lstrip('/')}"
        headers = {"Authorization": f"Bearer {access_token}"}

        for attempt in range(2):
            try:
                async with self.session.get(url, params=params, headers=headers) as response:
                    raw = await response.text()
                    try:
                        payload = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        payload = {}

                    if response.status == 200:
                        if isinstance(payload, dict):
                            return payload, 200
                        return {}, 200

                    detail = ""
                    if isinstance(payload, dict):
                        error_blob = payload.get("error")
                        if isinstance(error_blob, dict):
                            detail = str(error_blob.get("message") or "")
                        elif isinstance(error_blob, str):
                            detail = error_blob

                    if response.status == 429 and attempt == 0:
                        # Respect the Retry-After header; cap at 30s to avoid blocking too long.
                        try:
                            retry_after = int(response.headers.get("Retry-After", "5"))
                        except (ValueError, TypeError):
                            retry_after = 5
                        retry_after = min(retry_after, 30)
                        logging.warning(
                            "Spotify API rate-limited on %s; retrying in %ss",
                            endpoint, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status == 429:
                        self._set_failure(
                            "spotify_rate_limited",
                            f"Spotify API rate limit exceeded for `{endpoint}`.",
                        )
                        return None, 429

                    if response.status == 401 and use_user_token and attempt == 0 and self._spotify_refresh_token:
                        # Token may be stale; force one refresh and retry.
                        self._spotify_user_token = ""
                        self._spotify_user_token_expires_at = datetime.datetime.min.replace(
                            tzinfo=datetime.timezone.utc
                        )
                        refreshed = await self._refresh_user_access_token()
                        if refreshed:
                            headers["Authorization"] = f"Bearer {refreshed}"
                            continue

                    if response.status == 403:
                        self._set_failure(
                            "spotify_forbidden",
                            f"Spotify API returned HTTP 403 for `{endpoint}` using {token_label} auth. {detail}".strip(),
                        )
                    elif response.status == 401:
                        self._set_failure(
                            "spotify_unauthorized",
                            f"Spotify API returned HTTP 401 for `{endpoint}` using {token_label} auth. {detail}".strip(),
                        )
                    else:
                        self._set_failure(
                            "spotify_http_error",
                            f"Spotify API returned HTTP {response.status} for `{endpoint}` using {token_label} auth. {detail}".strip(),
                        )

                    logging.warning(
                        "Spotify API %s request failed for %s (status=%s): %s",
                        token_label,
                        endpoint,
                        response.status,
                        detail or raw[:220],
                    )
                    return None, response.status
            except Exception as exc:
                self._set_failure(
                    "spotify_request_failed",
                    f"Spotify API request failed for `{endpoint}` using {token_label} auth: {exc}",
                )
                logging.error("Spotify API request error for %s (%s): %s", endpoint, token_label, exc)
                return None, 0

        return None, 0

    def _parse_playlist_item_track(self, item: Any) -> Optional[ResolvedTrack]:
        if not isinstance(item, dict):
            return None

        candidate = item.get("item")
        if not isinstance(candidate, dict):
            candidate = item.get("track")
        if not isinstance(candidate, dict):
            candidate = item

        if candidate.get("type") not in (None, "track"):
            return None

        return self._build_track(
            candidate.get("name"),
            self._extract_first_artist_name(candidate.get("artists")),
            added_at=item.get("added_at"),
        )

    async def _fetch_playlist_items_api(self, playlist_id: str, *, use_user_token: bool) -> List[ResolvedTrack]:
        tracks: List[ResolvedTrack] = []
        market = os.getenv("SPOTIFY_MARKET", "US")
        offset = 0

        while True:
            page, status = await self._spotify_api_get(
                f"playlists/{playlist_id}/items",
                params={
                    "limit": 100,
                    "offset": offset,
                    "market": market,
                    "additional_types": "track",
                },
                use_user_token=use_user_token,
            )
            if page is None:
                if status == 403:
                    auth_name = "user" if use_user_token else "app"
                    logging.warning(
                        "Spotify playlist items were forbidden with %s auth for playlist %s.",
                        auth_name,
                        playlist_id,
                    )
                return []

            page_items = page.get("items", [])
            if not isinstance(page_items, list):
                page_items = []

            for item in page_items:
                track = self._parse_playlist_item_track(item)
                if track:
                    tracks.append(track)

            has_next = bool(page.get("next"))
            if not has_next:
                break

            step = len(page_items) or 100
            offset += step

        return tracks

    async def _fetch_album_tracks_api(self, album_id: str) -> List[ResolvedTrack]:
        tracks: List[ResolvedTrack] = []
        market = os.getenv("SPOTIFY_MARKET", "US")
        offset = 0

        while True:
            page, _ = await self._spotify_api_get(
                f"albums/{album_id}/tracks",
                params={"limit": 50, "offset": offset, "market": market},
                use_user_token=False,
            )
            if page is None:
                break

            page_items = page.get("items", [])
            if not isinstance(page_items, list):
                page_items = []

            for item in page_items:
                if not isinstance(item, dict):
                    continue
                track = self._build_track(
                    item.get("name"),
                    self._extract_first_artist_name(item.get("artists")),
                )
                if track:
                    tracks.append(track)

            if not page.get("next"):
                break
            offset += len(page_items) or 50

        return tracks

    async def _fetch_via_spotipy(self, url: str) -> List[ResolvedTrack]:
        if not (self._spotify or self._spotify_user_token or self._spotify_refresh_token):
            return []

        tracks: List[ResolvedTrack] = []

        if "/playlist/" in url:
            playlist_id = url.split("/playlist/")[1].split("?")[0]
            # Prefer user auth when available because Spotify dev-mode restrictions can
            # block app-only credentials on playlist item extraction.
            if self._spotify_user_token or self._spotify_refresh_token:
                tracks = await self._fetch_playlist_items_api(playlist_id, use_user_token=True)

            if not tracks and self._spotify:
                # App auth still works for extended-quota apps and some public playlists.
                tracks = await self._fetch_playlist_items_api(playlist_id, use_user_token=False)

            if not tracks and self.last_failure_code == "spotify_forbidden":
                self._set_failure(
                    "spotify_playlist_forbidden",
                    (
                        "Spotify blocked playlist item access (HTTP 403). "
                        "Use a user OAuth token with playlist scopes for owner/collab playlists "
                        "or request extended quota mode in Spotify Dashboard."
                    ),
                )

        elif "/album/" in url:
            album_id = url.split("/album/")[1].split("?")[0]
            tracks = await self._fetch_album_tracks_api(album_id)

        return tracks

    def _extract_tracks_from_session_blob(self, html: str) -> List[ResolvedTrack]:
        soup = BeautifulSoup(html, "html.parser")
        scripts: list[str] = []

        primary = soup.find("script", id="session")
        if primary:
            encoded = primary.get_text(strip=True)
            if encoded:
                scripts.append(encoded)

        # Spotify can move the payload between text/plain blobs. Try all of them.
        for script in soup.find_all("script", attrs={"type": "text/plain"}):
            encoded = script.get_text(strip=True)
            if encoded and encoded not in scripts and len(encoded) > 48:
                scripts.append(encoded)

        if not scripts:
            logging.debug("Spotify page fallback: no candidate text/plain payload scripts found.")
            return []

        for encoded in scripts:
            payload = None
            for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                try:
                    decoded = decoder(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                    payload = json.loads(decoded)
                    break
                except Exception:
                    continue

            if payload is None:
                continue

            seen: set[str] = set()
            collected: List[ResolvedTrack] = []

            def visit(node: Any, _depth: int = 0) -> None:
                if _depth > 60:
                    return
                if isinstance(node, dict):
                    if node.get("__typename") == "Track":
                        title = node.get("name")
                        artist = self._extract_first_artist_name(node.get("artists"))
                        track = self._build_track(title, artist)
                        if track:
                            identity = str(node.get("uri") or f"{track.title.lower()}::{track.artist.lower()}")
                            if identity not in seen:
                                seen.add(identity)
                                collected.append(track)
                    for value in node.values():
                        visit(value, _depth + 1)
                elif isinstance(node, list):
                    for item in node:
                        visit(item, _depth + 1)

            visit(payload)
            if collected:
                return collected

        logging.debug("Spotify page fallback: no tracks decoded from text/plain payload scripts.")
        return []

    @staticmethod
    def _extract_music_song_urls(html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        urls: List[str] = []
        for meta in soup.find_all("meta"):
            meta_key = (meta.get("name") or meta.get("property") or "").strip().lower()
            if meta_key != "music:song":
                continue
            content = (meta.get("content") or "").strip()
            if content.startswith("https://open.spotify.com/track/"):
                urls.append(content.split("?")[0])

        if urls:
            return list(dict.fromkeys(urls))

        # Regex fallback for malformed markup.
        track_urls = re.findall(
            r'https://open\.spotify\.com/track/[A-Za-z0-9]+',
            html,
        )
        uris = re.findall(r'spotify:track:[A-Za-z0-9]+', html)
        for uri in uris:
            track_id = uri.split(":")[-1]
            track_urls.append(f"https://open.spotify.com/track/{track_id}")

        return list(dict.fromkeys(track_urls))

    async def _resolve_track_urls(self, urls: List[str], limit: int = 80) -> List[ResolvedTrack]:
        if not urls:
            return []

        sem = asyncio.Semaphore(6)

        async def resolve_one(track_url: str) -> Optional[ResolvedTrack]:
            async with sem:
                await asyncio.sleep(0.2)
                try:
                    return await self.get_single_track(track_url)
                except Exception as exc:
                    logging.debug("Error in parallel scrape for track %s: %s", track_url, exc)
                    return None

        resolved = await asyncio.gather(*(resolve_one(url) for url in urls[:limit]), return_exceptions=True)

        tracks: List[ResolvedTrack] = []
        seen: set[str] = set()
        for item in resolved:
            if isinstance(item, Exception) or item is None:
                continue
            identity = f"{item.title.lower()}::{item.artist.lower()}"
            if identity in seen:
                continue
            seen.add(identity)
            tracks.append(item)

        return tracks

    async def _extract_tracks_from_page(self, url: str) -> List[ResolvedTrack]:
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    logging.warning(f"Spotify page fallback returned HTTP {response.status} for {url}")
                    return []
                html = await response.text()
        except Exception as exc:
            logging.error(f"Spotify page fallback request failed: {exc}")
            return []

        tracks = self._extract_tracks_from_session_blob(html)
        if tracks:
            logging.info(f"🌐 Extracted {len(tracks)} tracks from Spotify page payload.")
            return tracks

        deduped_urls = self._extract_music_song_urls(html)
        logging.info("Spotify page fallback: found %s track URLs in page metadata.", len(deduped_urls))
        if not deduped_urls:
            return []

        resolved = await self._resolve_track_urls(deduped_urls)

        if resolved:
            logging.info(f"🌐 Extracted {len(resolved)} tracks from Spotify `music:song` metadata.")
        return resolved

    async def get_playlist_tracks(self, url: str, force_update: bool = False, force_cache_bypass: bool = False) -> List[ResolvedTrack]:
        """Fetches tracks from a Spotify playlist or album securely. Uses local JSON caching (7 day TTL) to minimize Spotipy usage."""
        playlist_key = url.split("?")[0]
        self._clear_failure()

        if not force_update:
            cached = self._get_cached_playlist_tracks(playlist_key, force_cache_bypass=force_cache_bypass)
            if cached:
                return cached

        tracks: List[ResolvedTrack] = []
        has_api_auth = bool(self._spotify or self._spotify_user_token or self._spotify_refresh_token)
        if has_api_auth:
            try:
                logging.info(f"🌐 Fetching fresh data from Spotify API for: {playlist_key}")
                tracks = await self._fetch_via_spotipy(playlist_key)
            except SpotifyException as exc:
                logging.warning(
                    "Spotify API rejected playlist extraction (status=%s). Falling back to page metadata.",
                    getattr(exc, "http_status", "unknown"),
                )
                logging.debug(f"Spotipy exception detail: {exc}")
            except Exception as exc:
                logging.error(f"Spotipy exception: {exc}")
        else:
            logging.warning("Spotipy credentials missing; using Spotify page fallback for playlists/albums.")
            self._set_failure(
                "spotify_credentials_missing",
                "Spotify credentials are missing, API playlist extraction is unavailable.",
            )

        if not tracks:
            if self.last_failure_code == "spotify_playlist_forbidden":
                # If Spotify explicitly blocked API access, page scraping is unlikely
                # to be stable enough for a large playlist; avoid misleading fallback hops.
                logging.warning(self.last_failure_message)
            else:
                tracks = await self._extract_tracks_from_page(playlist_key)

        if tracks:
            self._save_playlist_snapshot(playlist_key, tracks)
        else:
            logging.warning(f"No tracks extracted for {playlist_key}.")

        return tracks

    async def resolve_via_odesli(self, url: str) -> Optional[ResolvedTrack]:
        """Uses Odesli (Songlink) API as an extremely reliable fallback for track resolution."""
        odesli_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(url)}"
        try:
            async with self.session.get(odesli_url) as response:
                if response.status == 200:
                    data = await response.json()
                    # The entityUniqueId points to the primary metadata
                    unique_id = data.get("entityUniqueId")
                    if unique_id and unique_id in data.get("entitiesByUniqueId", {}):
                        entity = data["entitiesByUniqueId"][unique_id]
                        return ResolvedTrack(title=entity.get("title", ""), artist=entity.get("artistName", ""))
        except Exception as e:
            logging.error(f"Odesli resolution failed: {e}")
        return None

    async def get_single_track(self, url: str) -> Optional[ResolvedTrack]:
        """Scrapes a single Spotify track URL or uses the Spotipy API if available."""
        if self._spotify and "/track/" in url:
            try:
                track_id = url.split("/track/")[1].split("?")[0]
                track = await asyncio.to_thread(self._spotify.track, track_id)
                if track and 'name' in track and track['artists']:
                    return ResolvedTrack(track['name'], track['artists'][0]['name'])
            except Exception as exc:
                logging.debug(f"Spotipy single-track lookup failed; falling back to scrape: {exc}")
                
        # Scrape fallback for single tracks
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    return None
                    
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Format: "Song Title - song and lyrics by Artist | Spotify"
                title_tag = soup.find("title")
                if not title_tag:
                    return None
                    
                full_title = title_tag.string or ""
                if " - song and lyrics by " in full_title:
                    parts = full_title.split(" - song and lyrics by ")
                    song_name = parts[0].strip()
                    artist_name = parts[1].split(" | Spotify")[0].strip()
                    return ResolvedTrack(song_name, artist_name)
                    
                return None
        except Exception as e:
            logging.error(f"Error scraping single track: {e}")
            return None

    async def close(self):
        if self._session:
            await self._session.close()

# Global instance
resolver = SpotifyResolver()
