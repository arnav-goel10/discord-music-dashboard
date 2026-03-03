import aiohttp
import asyncio
from bs4 import BeautifulSoup
from dataclasses import dataclass
import difflib
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Optional


_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


@dataclass
class LyricsResult:
    title: str
    artist: str
    lyrics: str
    source: str
    synced: bool = False
    source_url: Optional[str] = None
    synced_lyrics: Optional[str] = None


class LyricsResolver:
    _SPOTIFY_TRACK_ID_RE = re.compile(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})")

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache_file = os.path.join(_DATA_DIR, "lyrics_cache.json")
        self._cache_enabled = self._env_bool("LYRICS_CACHE_ENABLED", True)
        self._cache_ttl_seconds = self._env_int("LYRICS_CACHE_TTL_SECONDS", 1_209_600, minimum=0)
        self._prefer_synced = self._env_bool("LYRICS_PREFER_SYNCED", False)

        self._lrclib_exact_min_score = self._env_float("LYRICS_LRCLIB_EXACT_MIN_SCORE", 118.0)
        self._lrclib_search_min_score = self._env_float("LYRICS_LRCLIB_SEARCH_MIN_SCORE", 86.0)

        self._genius_token = (os.getenv("GENIUS_ACCESS_TOKEN") or "").strip()

        self._spotify_unofficial_enabled = self._env_bool("LYRICS_SPOTIFY_UNOFFICIAL_ENABLED", False)
        self._spotify_market = (os.getenv("LYRICS_SPOTIFY_MARKET") or "US").strip().upper() or "US"
        self._spotify_web_token_override = (os.getenv("LYRICS_SPOTIFY_WEB_TOKEN") or "").strip()
        self._spotify_web_token_auto = self._env_bool("LYRICS_SPOTIFY_WEB_TOKEN_AUTO", True)

        self._spotify_client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip()
        self._spotify_client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip()
        self._spotify_user_token = (os.getenv("SPOTIFY_USER_ACCESS_TOKEN") or "").strip()

        self._spotify_api_token: str = ""
        self._spotify_api_token_expiry = 0.0
        self._spotify_web_token: str = ""
        self._spotify_web_token_expiry = 0.0
        self._spotify_token_lock = asyncio.Lock()

        self._cache = self._load_cache()

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
        raw = (os.getenv(name) or "").strip()
        try:
            value = int(raw) if raw else default
        except ValueError:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = (os.getenv(name) or "").strip()
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    ),
                },
            )
        return self._session

    @staticmethod
    def _normalize_text(raw: Optional[str]) -> str:
        text = str(raw or "").strip().lower()
        text = text.replace("’", "'")
        text = re.sub(r"\s*-\s*topic\s*$", "", text)
        text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
        text = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", text)
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        return " ".join(text.split())

    @staticmethod
    def _strip_timestamp_tags(raw: str) -> str:
        if not raw:
            return ""
        lines = [line.strip() for line in raw.splitlines()]
        cleaned: list[str] = []
        for line in lines:
            line = re.sub(r"^\[(\d{1,2}):(\d{2})(?:\.\d{1,3})?]\s*", "", line)
            if line:
                cleaned.append(line)
        return "\n".join(cleaned).strip()

    @staticmethod
    def _clean_lyrics(raw: Optional[str]) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

    @staticmethod
    def _clean_genius_lyrics(raw: Optional[str], *, title_hint: Optional[str] = None) -> str:
        text = LyricsResolver._clean_lyrics(raw)
        if not text:
            return ""

        lines = [line.strip() for line in text.splitlines()]

        # Genius often splits section headers like:
        # [Pre-Chorus: A,
        # B
        # ]
        merged_headers: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("[") and "]" not in line:
                parts = [line]
                j = i + 1
                while j < len(lines):
                    parts.append(lines[j])
                    if "]" in lines[j]:
                        break
                    j += 1
                line = " ".join(part.strip() for part in parts if part.strip())
                i = j + 1
            else:
                i += 1
            merged_headers.append(line)

        title_norm = " ".join(str(title_hint or "").split()).strip().lower()
        cleaned: list[str] = []
        for line in merged_headers:
            if not line:
                cleaned.append("")
                continue

            compact = " ".join(line.split())
            compact_lower = compact.lower()

            if re.fullmatch(r"\d+\s+contributors?", compact, flags=re.IGNORECASE):
                continue
            if re.fullmatch(r"\d+\s*embed", compact, flags=re.IGNORECASE):
                continue
            if compact_lower == "read more":
                continue
            if title_norm and compact_lower == f"{title_norm} lyrics":
                continue
            if (
                compact_lower.endswith(" lyrics")
                and len(compact.split()) <= 6
                and not compact.startswith("[")
            ):
                continue
            # Drop short editorial description lines before actual lyric sections.
            if (
                " is about " in compact_lower
                or compact_lower.startswith(("sonically,", "lyrically,", "the song "))
                or compact_lower.endswith("read more")
            ):
                continue

            # Normalize punctuation spacing left by HTML flattening.
            compact = re.sub(r"\s+([,.:;!?])", r"\1", compact)
            compact = re.sub(r"\(\s+", "(", compact)
            compact = re.sub(r"\s+\)", ")", compact)
            compact = re.sub(r"\[\s+", "[", compact)
            compact = re.sub(r"\s+]", "]", compact)
            compact = re.sub(r"\s{2,}", " ", compact).strip()

            # Skip obvious non-lyric garbage lines after normalization.
            if compact in {"[?]", "[]"}:
                continue

            cleaned.append(compact)

        # Remove leading empty lines and collapse excess spacing.
        while cleaned and not cleaned[0]:
            cleaned.pop(0)

        # Heal obvious wrapped-word breaks from Genius HTML flattening:
        # e.g. "greatest mis" + "take" -> "greatest mistake"
        stitched: list[str] = []
        idx = 0
        while idx < len(cleaned):
            line = cleaned[idx]
            if idx + 1 < len(cleaned):
                nxt = cleaned[idx + 1]
                last_match = re.search(r"([a-z]{2,4})$", line)
                next_match = re.fullmatch(r"([a-z]{4,10})", nxt or "")
                if (
                    line
                    and nxt
                    and not line.startswith("[")
                    and last_match
                    and next_match
                    and len(line.split()) >= 4
                ):
                    line = line[: -len(last_match.group(1))] + last_match.group(1) + next_match.group(1)
                    idx += 1
            stitched.append(line)
            idx += 1

        result = "\n".join(stitched)
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        return result

    @staticmethod
    def _looks_like_noisy_genius_lyrics(text: Optional[str]) -> bool:
        sample = str(text or "")
        if not sample:
            return False
        lowered = sample.lower()
        noise_markers = (
            " contributors\n",
            "\nread more",
            " lyrics\n",
            "lyrics\n[",
            "sonically,",
            " is about ",
        )
        return any(marker in lowered for marker in noise_markers)

    @staticmethod
    def _extract_genius_hits(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        response = payload.get("response")
        if not isinstance(response, dict):
            return []

        direct_hits = response.get("hits")
        if isinstance(direct_hits, list):
            return [h for h in direct_hits if isinstance(h, dict)]

        sections = response.get("sections")
        if not isinstance(sections, list):
            return []

        collected: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            hits = section.get("hits")
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if isinstance(hit, dict):
                    collected.append(hit)
        return collected

    @staticmethod
    def _extract_genius_container_text(container: Any) -> str:
        # Using get_text("\\n") on Genius containers creates fake line breaks between
        # inline spans/anchors, which causes split words and fragmented lines.
        fragment = BeautifulSoup(str(container), "html.parser")
        root = fragment.find(attrs={"data-lyrics-container": "true"}) or fragment

        for br in root.find_all("br"):
            br.replace_with("\n")

        # Remove non-lyric visual/control elements while keeping lyric text.
        for node in root.select("script, style, button, svg"):
            node.decompose()

        # Unwrap inline tags so words stay contiguous.
        for node in root.find_all(["a", "span", "em", "strong", "i", "b", "u", "small"]):
            node.unwrap()

        text = root.get_text("", strip=False)
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        # Preserve stanza spacing but drop repeated empties.
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1]:
                out.append("")
        return "\n".join(out).strip()

    @staticmethod
    def _parse_duration_ms(raw: Any) -> int:
        if raw is None:
            return 0
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0
        if value <= 0:
            return 0
        # LRCLIB exposes duration in seconds; some providers expose ms.
        if value > 10_000:
            return int(value)
        return int(value * 1000)

    @staticmethod
    def _parse_spotify_track_id(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        match = LyricsResolver._SPOTIFY_TRACK_ID_RE.search(str(raw))
        if match:
            return match.group(1)
        compact = str(raw).strip()
        if re.fullmatch(r"[A-Za-z0-9]{22}", compact):
            return compact
        return None

    @classmethod
    def _title_variants(cls, title: str) -> list[str]:
        variants: list[str] = []

        def add(item: Optional[str]) -> None:
            if not item:
                return
            value = " ".join(str(item).split()).strip()
            if value and value not in variants:
                variants.append(value)

        add(title)
        stripped = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title).strip()
        add(stripped)
        feat_stripped = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", stripped, flags=re.IGNORECASE).strip()
        add(feat_stripped)
        add(title.replace("’", "'"))
        return variants[:4]

    @classmethod
    def _artist_variants(cls, artist: Optional[str]) -> list[Optional[str]]:
        variants: list[Optional[str]] = []

        def add(item: Optional[str]) -> None:
            if item is None:
                if None not in variants:
                    variants.append(None)
                return
            value = " ".join(str(item).split()).strip()
            if value and value not in variants:
                variants.append(value)

        add(artist)
        if artist:
            cleaned = re.sub(r"\s*-\s*topic\s*$", "", artist, flags=re.IGNORECASE).strip()
            add(cleaned)
            primary = re.split(r"\b(?:feat\.?|ft\.?|featuring|and|,|&| x )\b", cleaned, maxsplit=1, flags=re.IGNORECASE)[
                0
            ].strip()
            add(primary)
        add(None)
        return variants[:4]

    @classmethod
    def _album_variants(cls, album: Optional[str]) -> list[Optional[str]]:
        variants: list[Optional[str]] = [None]

        def add(item: Optional[str]) -> None:
            if not item:
                return
            value = " ".join(str(item).split()).strip()
            if value and value not in variants:
                variants.append(value)

        add(album)
        if album:
            stripped = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", album).strip()
            add(stripped)
            deluxe_stripped = re.sub(r"\b(deluxe|expanded|edition|remastered?)\b", " ", stripped, flags=re.IGNORECASE)
            add(deluxe_stripped)
        return variants[:4]

    @staticmethod
    def _duration_variants(duration_ms: Optional[int]) -> list[Optional[int]]:
        if not duration_ms:
            return [None]
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            return [None]
        if duration_ms <= 0:
            return [None]

        base = max(1, int(round(duration_ms / 1000)))
        variants: list[Optional[int]] = [base]
        for offset in (-1, 1, -2, 2):
            candidate = base + offset
            if candidate > 0 and candidate not in variants:
                variants.append(candidate)
        variants.append(None)
        return variants[:6]

    def _build_cache_keys(
        self,
        *,
        title: str,
        artist: Optional[str],
        album: Optional[str] = None,
        duration_ms: Optional[int] = None,
        spotify_track_id: Optional[str] = None,
    ) -> list[str]:
        keys: list[str] = []

        def add(raw_title: Optional[str], raw_artist: Optional[str], raw_album: Optional[str], raw_duration: Optional[int]) -> None:
            title_norm = self._normalize_text(raw_title)
            artist_norm = self._normalize_text(raw_artist)
            album_norm = self._normalize_text(raw_album)
            duration_key = ""
            if raw_duration:
                try:
                    duration_key = str(max(0, int(raw_duration) // 1000))
                except (TypeError, ValueError):
                    duration_key = ""
            if not title_norm:
                return
            key = f"{title_norm}::{artist_norm}::{album_norm}::{duration_key}"
            if key not in keys:
                keys.append(key)

        add(title, artist, album, duration_ms)
        for t in self._title_variants(title):
            for a in self._artist_variants(artist):
                for alb in self._album_variants(album):
                    add(t, a, alb, duration_ms)

        parsed_spotify_id = self._parse_spotify_track_id(spotify_track_id)
        if parsed_spotify_id:
            keys.insert(0, f"spotify::{parsed_spotify_id}")

        return keys[:24]

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self._cache_enabled:
            return {}
        if not os.path.exists(self._cache_file):
            return {}

        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            logging.warning("Failed reading lyrics cache: %s", exc)
            return {}

        if not isinstance(raw, dict):
            return {}

        now = int(time.time())
        parsed: dict[str, dict[str, Any]] = {}
        changed = False
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                changed = True
                continue
            try:
                updated_at = int(value.get("updated_at", 0))
            except (TypeError, ValueError):
                updated_at = 0
            if self._cache_ttl_seconds > 0 and updated_at and (now - updated_at) > self._cache_ttl_seconds:
                changed = True
                continue

            lyrics = self._clean_lyrics(value.get("lyrics"))
            title = str(value.get("title", "")).strip()
            artist = str(value.get("artist", "")).strip()
            source = str(value.get("source", "")).strip()
            if "genius" in source.lower():
                cleaned_genius = self._clean_genius_lyrics(lyrics, title_hint=title)
                if cleaned_genius and cleaned_genius != lyrics:
                    lyrics = cleaned_genius
                    changed = True
            if not lyrics or not title:
                changed = True
                continue

            parsed[key] = {
                "title": title,
                "artist": artist,
                "lyrics": lyrics,
                "source": source,
                "synced": bool(value.get("synced", False)),
                "source_url": value.get("source_url"),
                "synced_lyrics": self._clean_lyrics(value.get("synced_lyrics")) or None,
                "updated_at": updated_at or now,
            }

        if changed:
            self._cache = parsed
            self._save_cache()
        return parsed

    def _save_cache(self) -> None:
        if not self._cache_enabled:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logging.warning("Failed writing lyrics cache: %s", exc)

    def _get_cached(self, keys: list[str]) -> Optional[LyricsResult]:
        if not self._cache_enabled:
            return None

        now = int(time.time())
        changed = False
        for key in keys:
            item = self._cache.get(key)
            if not isinstance(item, dict):
                continue

            try:
                updated_at = int(item.get("updated_at", 0))
            except (TypeError, ValueError):
                updated_at = 0
            if self._cache_ttl_seconds > 0 and updated_at and (now - updated_at) > self._cache_ttl_seconds:
                self._cache.pop(key, None)
                changed = True
                continue

            lyrics = self._clean_lyrics(item.get("lyrics"))
            title = str(item.get("title", "")).strip()
            source = str(item.get("source", "")).strip() or "cache"
            if "genius" in source.lower():
                cleaned_genius = self._clean_genius_lyrics(lyrics, title_hint=title)
                if cleaned_genius and cleaned_genius != lyrics:
                    lyrics = cleaned_genius
                    item["lyrics"] = lyrics
                    item["updated_at"] = now
                    changed = True
            if not lyrics or not title:
                self._cache.pop(key, None)
                changed = True
                continue

            if changed:
                self._save_cache()
            return LyricsResult(
                title=title,
                artist=str(item.get("artist", "")).strip(),
                lyrics=lyrics,
                source=source,
                synced=bool(item.get("synced", False)),
                source_url=str(item.get("source_url", "")).strip() or None,
                synced_lyrics=self._clean_lyrics(item.get("synced_lyrics")) or None,
            )

        if changed:
            self._save_cache()
        return None

    def _set_cached(self, keys: list[str], result: LyricsResult) -> None:
        if not self._cache_enabled:
            return
        lyrics = self._clean_lyrics(result.lyrics)
        if not keys or not lyrics:
            return

        entry = {
            "title": result.title,
            "artist": result.artist,
            "lyrics": lyrics,
            "source": result.source,
            "synced": result.synced,
            "source_url": result.source_url,
            "synced_lyrics": self._clean_lyrics(result.synced_lyrics) if result.synced_lyrics else None,
            "updated_at": int(time.time()),
        }
        changed = False
        for key in keys:
            existing = self._cache.get(key)
            if existing != entry:
                self._cache[key] = entry
                changed = True
        if changed:
            self._save_cache()

    def _score_record(
        self,
        record: dict[str, Any],
        *,
        title: str,
        artist: Optional[str],
        album: Optional[str],
        duration_ms: Optional[int],
    ) -> float:
        score = 0.0

        rec_title = str(record.get("trackName", "")).strip()
        rec_artist = str(record.get("artistName", "")).strip()
        rec_album = str(record.get("albumName", "")).strip()

        title_sim = difflib.SequenceMatcher(None, self._normalize_text(title), self._normalize_text(rec_title)).ratio()
        score += title_sim * 100

        if artist:
            artist_sim = difflib.SequenceMatcher(
                None,
                self._normalize_text(artist),
                self._normalize_text(rec_artist),
            ).ratio()
            score += artist_sim * 78
            norm_artist = self._normalize_text(artist)
            norm_rec_artist = self._normalize_text(rec_artist)
            if norm_artist and norm_rec_artist and (norm_artist in norm_rec_artist or norm_rec_artist in norm_artist):
                score += 12

        if album:
            album_sim = difflib.SequenceMatcher(
                None,
                self._normalize_text(album),
                self._normalize_text(rec_album),
            ).ratio()
            score += album_sim * 34

        rec_duration_ms = self._parse_duration_ms(record.get("duration"))
        if duration_ms and duration_ms > 0 and rec_duration_ms > 0:
            delta = abs(int(duration_ms) - rec_duration_ms)
            if delta <= 2000:
                score += 30
            elif delta <= 5000:
                score += 20
            elif delta <= 10000:
                score += 12
            elif delta <= 20000:
                score += 5
            elif delta >= 50000:
                score -= 15

        plain = self._clean_lyrics(record.get("plainLyrics"))
        synced = self._clean_lyrics(record.get("syncedLyrics"))
        if plain:
            score += 7
        if synced:
            score += 6
        return score

    def _result_from_lrclib_record(
        self,
        record: dict[str, Any],
        *,
        fallback_title: str,
        fallback_artist: Optional[str],
        source_label: str,
    ) -> Optional[LyricsResult]:
        plain = self._clean_lyrics(record.get("plainLyrics"))
        synced = self._clean_lyrics(record.get("syncedLyrics"))

        if self._prefer_synced:
            chosen = synced or plain
            is_synced = bool(synced)
        else:
            chosen = plain or self._strip_timestamp_tags(synced)
            is_synced = bool(synced and not plain)

        chosen = self._clean_lyrics(chosen)
        if not chosen:
            return None

        rec_title = str(record.get("trackName", "")).strip() or fallback_title
        rec_artist = str(record.get("artistName", "")).strip() or (fallback_artist or "")
        rec_id = record.get("id")
        source_url = f"https://lrclib.net/api/get/{rec_id}" if rec_id is not None else None

        return LyricsResult(
            title=rec_title,
            artist=rec_artist,
            lyrics=chosen,
            source=source_label,
            synced=is_synced,
            source_url=source_url,
            synced_lyrics=self._clean_lyrics(synced) or None,
        )

    async def _fetch_lrclib_exact(
        self,
        *,
        title: str,
        artist: Optional[str],
        album: Optional[str],
        duration_ms: Optional[int],
    ) -> tuple[Optional[LyricsResult], float]:
        if not artist:
            return None, -1.0

        attempts: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()

        for duration_seconds in self._duration_variants(duration_ms):
            for album_name in self._album_variants(album):
                params: dict[str, str] = {
                    "track_name": title,
                    "artist_name": artist,
                }
                if duration_seconds is not None:
                    params["duration"] = str(duration_seconds)
                if album_name:
                    params["album_name"] = album_name

                key = tuple(sorted(params.items()))
                if key in seen:
                    continue
                seen.add(key)
                attempts.append(params)

        best_result: Optional[LyricsResult] = None
        best_score = -1.0

        for params in attempts[:10]:
            try:
                async with self.session.get("https://lrclib.net/api/get", params=params) as response:
                    if response.status == 404:
                        continue
                    if response.status != 200:
                        continue
                    payload = await response.json(content_type=None)
            except Exception as exc:
                logging.debug("LRCLIB exact lookup failed for %s: %s", params, exc)
                continue

            if not isinstance(payload, dict):
                continue

            score = self._score_record(
                payload,
                title=title,
                artist=artist,
                album=album,
                duration_ms=duration_ms,
            ) + 9.0
            result = self._result_from_lrclib_record(
                payload,
                fallback_title=title,
                fallback_artist=artist,
                source_label="LRCLIB (Exact)",
            )
            if result and score > best_score:
                best_score = score
                best_result = result

        return best_result, best_score

    async def _fetch_lrclib_search(
        self,
        *,
        title: str,
        artist: Optional[str],
        album: Optional[str],
        duration_ms: Optional[int],
    ) -> tuple[Optional[LyricsResult], float]:
        params: dict[str, str] = {"track_name": title}
        if artist:
            params["artist_name"] = artist

        try:
            async with self.session.get("https://lrclib.net/api/search", params=params) as response:
                if response.status != 200:
                    return None, -1.0
                payload = await response.json(content_type=None)
        except Exception as exc:
            logging.debug("LRCLIB search lookup failed for %s / %s: %s", title, artist, exc)
            return None, -1.0

        if not isinstance(payload, list) or not payload:
            return None, -1.0

        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            ranked.append(
                (
                    self._score_record(item, title=title, artist=artist, album=album, duration_ms=duration_ms),
                    item,
                )
            )

        if not ranked:
            return None, -1.0

        ranked.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best_item = ranked[0]
        result = self._result_from_lrclib_record(
            best_item,
            fallback_title=title,
            fallback_artist=artist,
            source_label="LRCLIB",
        )
        return result, best_score

    async def _get_spotify_api_token(self) -> Optional[str]:
        # For track search, app credentials are more stable than static user tokens.
        # Use app token first when available; only fall back to user token.
        if not (self._spotify_client_id and self._spotify_client_secret):
            return self._spotify_user_token or None

        now = time.time()
        if self._spotify_api_token and now < self._spotify_api_token_expiry:
            return self._spotify_api_token

        async with self._spotify_token_lock:
            now = time.time()
            if self._spotify_api_token and now < self._spotify_api_token_expiry:
                return self._spotify_api_token

            auth_header = aiohttp.BasicAuth(self._spotify_client_id, self._spotify_client_secret)

            try:
                async with self.session.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    auth=auth_header,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as response:
                    if response.status != 200:
                        return self._spotify_user_token or None
                    payload = await response.json(content_type=None)
            except Exception as exc:
                logging.debug("Spotify app token fetch failed: %s", exc)
                return self._spotify_user_token or None

            token = str((payload or {}).get("access_token") or "").strip()
            if not token:
                return self._spotify_user_token or None
            try:
                expires_in = int((payload or {}).get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires_in = 3600

            self._spotify_api_token = token
            self._spotify_api_token_expiry = time.time() + max(120, expires_in - 60)
            return self._spotify_api_token

        return self._spotify_user_token or None

    async def _get_spotify_web_token(self) -> Optional[str]:
        if self._spotify_web_token_override:
            return self._spotify_web_token_override
        if not self._spotify_web_token_auto:
            return None

        now = time.time()
        if self._spotify_web_token and now < self._spotify_web_token_expiry:
            return self._spotify_web_token

        async with self._spotify_token_lock:
            now = time.time()
            if self._spotify_web_token and now < self._spotify_web_token_expiry:
                return self._spotify_web_token

            try:
                async with self.session.get(
                    "https://open.spotify.com/get_access_token",
                    params={"reason": "transport", "productType": "web_player"},
                ) as response:
                    if response.status != 200:
                        return None
                    payload = await response.json(content_type=None)
            except Exception as exc:
                logging.debug("Spotify web token fetch failed: %s", exc)
                return None

            token = str((payload or {}).get("accessToken") or "").strip()
            if not token:
                return None

            exp_ms = (payload or {}).get("accessTokenExpirationTimestampMs")
            try:
                expiry = (int(exp_ms) / 1000.0) - 60.0 if exp_ms is not None else (time.time() + 3000.0)
            except (TypeError, ValueError):
                expiry = time.time() + 3000.0

            self._spotify_web_token = token
            self._spotify_web_token_expiry = max(time.time() + 180.0, expiry)
            return self._spotify_web_token

    def _score_spotify_track(
        self,
        track: dict[str, Any],
        *,
        title: str,
        artist: Optional[str],
        duration_ms: Optional[int],
    ) -> float:
        item_title = str(track.get("name") or "").strip()
        artists = track.get("artists") if isinstance(track.get("artists"), list) else []
        item_artist = ""
        for raw_artist in artists:
            if isinstance(raw_artist, dict):
                name = str(raw_artist.get("name") or "").strip()
                if name:
                    item_artist = name
                    break

        score = difflib.SequenceMatcher(None, self._normalize_text(title), self._normalize_text(item_title)).ratio() * 100

        if artist:
            score += (
                difflib.SequenceMatcher(None, self._normalize_text(artist), self._normalize_text(item_artist)).ratio() * 85
            )

        item_duration_ms = self._parse_duration_ms(track.get("duration_ms"))
        if duration_ms and duration_ms > 0 and item_duration_ms > 0:
            delta = abs(int(duration_ms) - item_duration_ms)
            if delta <= 2000:
                score += 25
            elif delta <= 5000:
                score += 16
            elif delta <= 9000:
                score += 9
            elif delta >= 45000:
                score -= 10

        try:
            popularity = int(track.get("popularity") or 0)
        except (TypeError, ValueError):
            popularity = 0
        score += max(0, min(popularity, 100)) * 0.04
        return score

    async def _resolve_spotify_track_id(
        self,
        *,
        title: str,
        artist: Optional[str],
        duration_ms: Optional[int],
    ) -> Optional[str]:
        token = await self._get_spotify_api_token()
        if not token:
            return None

        query_parts = []
        if title:
            query_parts.append(f'track:"{title}"')
        if artist:
            query_parts.append(f'artist:"{artist}"')
        query = " ".join(query_parts) or title
        if not query:
            return None

        try:
            async with self.session.get(
                "https://api.spotify.com/v1/search",
                params={
                    "q": query,
                    "type": "track",
                    "limit": 8,
                    "market": self._spotify_market,
                },
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except Exception as exc:
            logging.debug("Spotify track search failed for query '%s': %s", query, exc)
            return None

        items = (((payload or {}).get("tracks") or {}).get("items") or [])
        if not isinstance(items, list) or not items:
            return None

        best_id = None
        best_score = -1.0
        for item in items:
            if not isinstance(item, dict):
                continue
            track_id = str(item.get("id") or "").strip()
            if not track_id:
                continue
            score = self._score_spotify_track(item, title=title, artist=artist, duration_ms=duration_ms)
            if score > best_score:
                best_score = score
                best_id = track_id

        if best_score < 112:
            return None
        return best_id

    async def _fetch_spotify_unofficial(
        self,
        *,
        title: str,
        artist: Optional[str],
        duration_ms: Optional[int],
        spotify_track_id: Optional[str],
    ) -> Optional[LyricsResult]:
        if not self._spotify_unofficial_enabled:
            return None

        track_id = self._parse_spotify_track_id(spotify_track_id)
        if not track_id:
            track_id = await self._resolve_spotify_track_id(title=title, artist=artist, duration_ms=duration_ms)
        if not track_id:
            return None

        web_token = await self._get_spotify_web_token()
        if not web_token:
            return None

        url = f"https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}"
        try:
            async with self.session.get(
                url,
                params={"format": "json", "market": self._spotify_market},
                headers={
                    "Authorization": f"Bearer {web_token}",
                    "app-platform": "WebPlayer",
                },
            ) as response:
                if response.status != 200:
                    logging.debug(
                        "Spotify unofficial lyrics returned HTTP %s for track %s",
                        response.status,
                        track_id,
                    )
                    return None
                payload = await response.json(content_type=None)
        except Exception as exc:
            logging.debug("Spotify unofficial lyrics lookup failed for track %s: %s", track_id, exc)
            return None

        lyrics_blob = (payload or {}).get("lyrics") if isinstance(payload, dict) else None
        if not isinstance(lyrics_blob, dict):
            return None

        lines = lyrics_blob.get("lines") if isinstance(lyrics_blob.get("lines"), list) else []
        if not lines:
            return None

        sync_type = str(lyrics_blob.get("syncType") or "").strip().upper()
        is_synced = sync_type in {"LINE_SYNCED", "SYLLABLE_SYNCED", "WORD_SYNCED"}

        rendered_lines: list[str] = []
        timed_lines: list[str] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            words = self._clean_lyrics(line.get("words"))
            if not words:
                continue
            start_raw = line.get("startTimeMs")
            try:
                start_ms = int(start_raw) if start_raw is not None else 0
            except (TypeError, ValueError):
                start_ms = 0
            mm, ss = divmod(max(0, start_ms) // 1000, 60)
            timed_lines.append(f"[{mm:02d}:{ss:02d}] {words}")

            if self._prefer_synced and is_synced:
                rendered_lines.append(timed_lines[-1])
            else:
                rendered_lines.append(words)

        lyrics = self._clean_lyrics("\n".join(rendered_lines))
        if not lyrics:
            return None

        if not self._prefer_synced and is_synced:
            lyrics = self._strip_timestamp_tags(lyrics)

        source_track_url = f"https://open.spotify.com/track/{track_id}"
        return LyricsResult(
            title=title,
            artist=artist or "",
            lyrics=lyrics,
            source="Spotify Lyrics (Unofficial)",
            synced=self._prefer_synced and is_synced,
            source_url=source_track_url,
            synced_lyrics=self._clean_lyrics("\n".join(timed_lines)) if timed_lines else None,
        )

    async def _fetch_lyrics_ovh(self, *, title: str, artist: Optional[str]) -> Optional[LyricsResult]:
        if not artist:
            return None
        safe_artist = urllib.parse.quote(artist, safe="")
        safe_title = urllib.parse.quote(title, safe="")
        url = f"https://api.lyrics.ovh/v1/{safe_artist}/{safe_title}"
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except Exception as exc:
            logging.debug("lyrics.ovh lookup failed for %s / %s: %s", title, artist, exc)
            return None

        if not isinstance(payload, dict):
            return None
        lyrics = self._clean_lyrics(payload.get("lyrics"))
        if not lyrics:
            return None
        return LyricsResult(
            title=title,
            artist=artist,
            lyrics=lyrics,
            source="lyrics.ovh",
            synced=False,
            source_url=url,
            synced_lyrics=None,
        )

    async def _fetch_genius(self, *, title: str, artist: Optional[str]) -> Optional[LyricsResult]:
        query = " ".join(part for part in [title, artist] if part).strip()
        if not query:
            return None

        payload: Optional[dict[str, Any]] = None
        endpoints: list[tuple[str, dict[str, str]]] = []
        if self._genius_token:
            endpoints.append(("https://api.genius.com/search", {"Authorization": f"Bearer {self._genius_token}"}))
        endpoints.append(("https://genius.com/api/search/song", {}))

        for url, headers in endpoints:
            try:
                async with self.session.get(url, params={"q": query}, headers=headers or None) as response:
                    if response.status != 200:
                        continue
                    candidate = await response.json(content_type=None)
            except Exception as exc:
                logging.debug("Genius search lookup failed for %s via %s: %s", query, url, exc)
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break

        if not payload:
            return None

        hits = self._extract_genius_hits(payload)
        if not isinstance(hits, list) or not hits:
            return None

        best_url = ""
        best_title = title
        best_artist = artist or ""
        best_score = -1.0
        for hit in hits[:8]:
            if not isinstance(hit, dict):
                continue
            result = hit.get("result") if isinstance(hit.get("result"), dict) else {}
            hit_title = str(result.get("title", "")).strip()
            hit_artist = str((result.get("primary_artist") or {}).get("name", "")).strip()
            hit_url = str(result.get("url", "")).strip()
            if not hit_title or not hit_url:
                continue
            score = difflib.SequenceMatcher(None, self._normalize_text(title), self._normalize_text(hit_title)).ratio()
            if artist:
                score += difflib.SequenceMatcher(
                    None,
                    self._normalize_text(artist),
                    self._normalize_text(hit_artist),
                ).ratio()
            if score > best_score:
                best_score = score
                best_url = hit_url
                best_title = hit_title
                best_artist = hit_artist or (artist or "")

        if not best_url:
            return None

        try:
            async with self.session.get(best_url) as response:
                if response.status != 200:
                    return None
                html = await response.text()
        except Exception as exc:
            logging.debug("Genius page fetch failed (%s): %s", best_url, exc)
            return None

        soup = BeautifulSoup(html, "html.parser")
        parts: list[str] = []
        for node in soup.find_all("div", attrs={"data-lyrics-container": "true"}):
            text = self._extract_genius_container_text(node)
            text = self._clean_lyrics(text)
            if text:
                parts.append(text)
        lyrics = self._clean_genius_lyrics("\n\n".join(parts), title_hint=best_title or title)
        if not lyrics:
            return None

        return LyricsResult(
            title=best_title,
            artist=best_artist,
            lyrics=lyrics,
            source="Genius",
            synced=False,
            source_url=best_url,
            synced_lyrics=None,
        )

    async def get_lyrics(
        self,
        *,
        title: str,
        artist: Optional[str] = None,
        duration_ms: Optional[int] = None,
        album: Optional[str] = None,
        spotify_track_id: Optional[str] = None,
    ) -> Optional[LyricsResult]:
        title = " ".join(str(title or "").split()).strip()
        artist = " ".join(str(artist or "").split()).strip() or None
        album = " ".join(str(album or "").split()).strip() or None
        spotify_track_id = self._parse_spotify_track_id(spotify_track_id)

        if not title:
            return None

        cache_keys = self._build_cache_keys(
            title=title,
            artist=artist,
            album=album,
            duration_ms=duration_ms,
            spotify_track_id=spotify_track_id,
        )
        cached = self._get_cached(cache_keys)
        if cached and ("genius" not in cached.source.lower() or not self._looks_like_noisy_genius_lyrics(cached.lyrics)):
            return cached
        if cached and "genius" in cached.source.lower():
            # Force a refresh if an older noisy Genius entry is stuck in cache.
            changed = False
            for key in cache_keys:
                item = self._cache.get(key)
                if isinstance(item, dict) and "genius" in str(item.get("source", "")).lower():
                    self._cache.pop(key, None)
                    changed = True
            if changed:
                self._save_cache()

        title_variants = self._title_variants(title)
        artist_variants = self._artist_variants(artist)
        best_exact_result: Optional[LyricsResult] = None
        best_exact_score = -1.0

        for candidate_title in title_variants[:3]:
            for candidate_artist in artist_variants[:3]:
                if not candidate_artist:
                    continue
                result, score = await self._fetch_lrclib_exact(
                    title=candidate_title,
                    artist=candidate_artist,
                    album=album,
                    duration_ms=duration_ms,
                )
                if result and score > best_exact_score:
                    best_exact_result = result
                    best_exact_score = score

        if best_exact_result and best_exact_score >= self._lrclib_exact_min_score:
            self._set_cached(cache_keys, best_exact_result)
            return best_exact_result

        if self._spotify_unofficial_enabled:
            for candidate_title in title_variants[:2]:
                for candidate_artist in artist_variants[:2]:
                    result = await self._fetch_spotify_unofficial(
                        title=candidate_title,
                        artist=candidate_artist,
                        duration_ms=duration_ms,
                        spotify_track_id=spotify_track_id,
                    )
                    if result:
                        self._set_cached(cache_keys, result)
                        return result

        best_search_result: Optional[LyricsResult] = None
        best_search_score = -1.0
        for candidate_title in title_variants:
            for candidate_artist in artist_variants:
                result, score = await self._fetch_lrclib_search(
                    title=candidate_title,
                    artist=candidate_artist,
                    album=album,
                    duration_ms=duration_ms,
                )
                if result and score > best_search_score:
                    best_search_result = result
                    best_search_score = score

        if best_search_result and best_search_score >= self._lrclib_search_min_score:
            self._set_cached(cache_keys, best_search_result)
            return best_search_result

        if best_exact_result:
            # Keep weaker exact results as a final fallback before moving to lower-quality providers.
            self._set_cached(cache_keys, best_exact_result)
            return best_exact_result

        for candidate_title in title_variants:
            for candidate_artist in artist_variants:
                if not candidate_artist:
                    continue
                result = await self._fetch_lyrics_ovh(title=candidate_title, artist=candidate_artist)
                if result:
                    self._set_cached(cache_keys, result)
                    return result

        for candidate_title in title_variants:
            for candidate_artist in artist_variants:
                result = await self._fetch_genius(title=candidate_title, artist=candidate_artist)
                if result:
                    self._set_cached(cache_keys, result)
                    return result

        return None

    async def close(self):
        if self._session:
            await self._session.close()


lyrics_resolver = LyricsResolver()
