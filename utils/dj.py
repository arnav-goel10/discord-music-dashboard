"""AI DJ engine.

v2 uses a multi-stage recommender:
- Hybrid retrieval (semantic ANN + sequential + graph + rule pools)
- Contextual utility ranking with bandit exploration bonus
- MMR slate reranking with diversity constraints
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import aiohttp

from utils.dj_index import HybridTrackIndex
from utils.dj_models import (
    ContextualRanker,
    FeatureBuilder,
    LinUCBBanditPolicy,
    RankedCandidate,
    SlateReranker,
)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_DB_PATH = os.getenv("DJ_DB_PATH", os.path.join(_DATA_DIR, "music_bot.db"))

logger = logging.getLogger(__name__)


def _skip_severity(listened_ms: int, duration_ms: int) -> float:
    """Return 0.0–1.0 indicating how much a skip should count as a negative signal.

    Early skips (< 20% listened) → severity ~1.0  (strong negative)
    Mid skips  (20–70%)          → severity ~0.5–0.3
    Late skips (> 80%)           → severity ~0.1  (barely negative, nearly a completion)
    """
    if duration_ms <= 0 or listened_ms <= 0:
        return 1.0  # unknown duration → treat as full skip
    ratio = min(1.0, max(0.0, listened_ms / duration_ms))
    # Exponential decay: severity = exp(-3 * ratio)
    # ratio=0   → 1.0,  ratio=0.2 → 0.55,  ratio=0.5 → 0.22,  ratio=0.8 → 0.09, ratio=1.0 → 0.05
    return math.exp(-3.0 * ratio)


def _centered_ts_bonus(ts_sample: float) -> float:
    """Return a zero-centered Thompson bonus so unseen tracks are neutral by default."""
    ts = min(1.0, max(0.0, float(ts_sample)))
    return (ts - 0.5) * 3.0


def _is_cold_start(seed_count: int, guild_history_count: int) -> bool:
    """Limit exploration-heavy cold start to genuinely sparse sessions/history."""
    return seed_count <= 2 and guild_history_count < 6


@dataclass
class SeedTrack:
    title: str
    artist: str
    duration_ms: int = 0


@dataclass
class RecommendedTrack:
    title: str
    artist: str
    track_id: str = ""
    score: float = 0.0
    source: str = ""
    reason: str = ""
    rank_features: dict[str, float] = field(default_factory=dict)
    exploration_flag: bool = False
    model_version: str = "v2"
    candidate_sources: list[str] = field(default_factory=list)
    audit_event_id: str = ""


@dataclass
class DJSession:
    guild_id: int
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    active: bool = True
    seed_tracks: list[SeedTrack] = field(default_factory=list)
    played_titles: set[str] = field(default_factory=set)
    skipped_titles: set[str] = field(default_factory=set)
    recommendation_buffer: list[RecommendedTrack] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    recent_track_ids: list[str] = field(default_factory=list)
    recent_artist_history: list[str] = field(default_factory=list)
    skip_streak: int = 0
    completion_streak: int = 0
    last_audit_event_id: str = ""
    last_activity_at: float = field(default_factory=time.time)
    _prefetch_task: Optional[asyncio.Task] = field(default=None, repr=False)


class LastFMClient:
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(4)

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, method: str, **params: Any) -> dict[str, Any]:
        params.update({"method": method, "api_key": self._api_key, "format": "json"})
        async with self._semaphore:
            try:
                async with self.session.get(self.BASE, params=params) as resp:
                    if resp.status != 200:
                        logger.debug("Last.fm %s returned %s", method, resp.status)
                        return {}
                    return await resp.json(content_type=None)
            except Exception as exc:
                logger.debug("Last.fm %s error: %s", method, exc)
                return {}

    async def track_get_similar(self, artist: str, track: str, *, limit: int = 30) -> list[dict[str, Any]]:
        data = await self._get("track.getSimilar", artist=artist, track=track, limit=str(limit), autocorrect="1")
        tracks = data.get("similartracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        out: list[dict[str, Any]] = []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("name", "")).strip()
            ainfo = t.get("artist", {})
            cand_artist = str(ainfo.get("name", "") if isinstance(ainfo, dict) else ainfo).strip()
            try:
                match = float(t.get("match", 0.0))
            except (TypeError, ValueError):
                match = 0.0
            if title and cand_artist:
                out.append({"title": title, "artist": cand_artist, "match": match})
        return out

    async def artist_get_similar(self, artist: str, *, limit: int = 15) -> list[dict[str, Any]]:
        data = await self._get("artist.getSimilar", artist=artist, limit=str(limit), autocorrect="1")
        artists = data.get("similarartists", {}).get("artist", [])
        if isinstance(artists, dict):
            artists = [artists]
        out: list[dict[str, Any]] = []
        for a in artists:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name", "")).strip()
            try:
                match = float(a.get("match", 0.0))
            except (TypeError, ValueError):
                match = 0.0
            if name:
                out.append({"name": name, "match": match})
        return out

    async def artist_get_top_tracks(self, artist: str, *, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._get("artist.getTopTracks", artist=artist, limit=str(limit), autocorrect="1")
        tracks = data.get("toptracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        out: list[dict[str, Any]] = []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("name", "")).strip()
            ainfo = t.get("artist", {})
            cand_artist = str(ainfo.get("name", "") if isinstance(ainfo, dict) else ainfo).strip()
            if title and cand_artist:
                out.append({"title": title, "artist": cand_artist})
        return out

    async def track_get_top_tags(self, artist: str, track: str) -> list[dict[str, Any]]:
        data = await self._get("track.getTopTags", artist=artist, track=track, autocorrect="1")
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        out: list[dict[str, Any]] = []
        for t in tags:
            if not isinstance(t, dict):
                continue
            tag_name = str(t.get("name", "")).strip().lower()
            try:
                count = int(t.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if tag_name and count > 0:
                out.append({"name": tag_name, "count": count})
        return out

    async def tag_get_top_tracks(self, tag: str, *, limit: int = 10) -> list[dict[str, Any]]:
        data = await self._get("tag.getTopTracks", tag=tag, limit=str(limit))
        tracks = data.get("tracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        out: list[dict[str, Any]] = []
        for t in tracks:
            if not isinstance(t, dict):
                continue
            title = str(t.get("name", "")).strip()
            ainfo = t.get("artist", {})
            cand_artist = str(ainfo.get("name", "") if isinstance(ainfo, dict) else ainfo).strip()
            if title and cand_artist:
                out.append({"title": title, "artist": cand_artist})
        return out


class CandidateRetriever:
    """Hybrid retrieval stage for v2 recommender."""

    CHANNEL_WEIGHTS = {
        "semantic_ann": 1.00,
        "sequential": 0.96,
        "graph": 0.90,
        "lastfm": 0.82,
        "popularity_tail": 0.58,
    }

    def __init__(self, dj: "AIDJ"):
        self._dj = dj

    @staticmethod
    def _rrf_accumulate(
        fused: dict[str, dict[str, Any]],
        items: list[dict[str, Any]],
        *,
        channel: str,
        weight: float,
        rrf_k: int,
    ) -> None:
        for rank, item in enumerate(items, start=1):
            track_id = str(item.get("track_id") or "").strip()
            title = str(item.get("title") or "").strip()
            artist = str(item.get("artist") or "").strip()
            if not (track_id and title and artist):
                continue
            bucket = fused.setdefault(
                track_id,
                {
                    "track_id": track_id,
                    "title": title,
                    "artist": artist,
                    "source": channel,
                    "reason": str(item.get("reason") or ""),
                    "candidate_sources": [],
                    "retrieval_score": 0.0,
                    "semantic_score": 0.0,
                    "sequential_score": 0.0,
                    "graph_score": 0.0,
                    "rule_score": 0.0,
                },
            )
            if channel not in bucket["candidate_sources"]:
                bucket["candidate_sources"].append(channel)
            score = weight / (rrf_k + rank)
            bucket["retrieval_score"] += score

            if channel == "semantic_ann":
                bucket["semantic_score"] = max(bucket["semantic_score"], float(item.get("score", 0.0) or 0.0))
            elif channel == "sequential":
                bucket["sequential_score"] = max(bucket["sequential_score"], float(item.get("score", 0.0) or 0.0))
            elif channel == "graph":
                bucket["graph_score"] = max(bucket["graph_score"], float(item.get("score", 0.0) or 0.0))
            else:
                bucket["rule_score"] = max(bucket["rule_score"], float(item.get("score", 0.0) or 0.0))

            if not bucket.get("reason") and item.get("reason"):
                bucket["reason"] = str(item.get("reason"))

    async def retrieve(self, session: DJSession, k: int) -> list[dict[str, Any]]:
        seeds = session.seed_tracks[-5:]
        if not seeds:
            return []

        seed_text = " | ".join(f"{s.title} - {s.artist}" for s in seeds)

        semantic_items: list[dict[str, Any]] = []
        sequential_items: list[dict[str, Any]] = []
        graph_items: list[dict[str, Any]] = []
        rule_items: list[dict[str, Any]] = []
        popularity_items: list[dict[str, Any]] = []

        async def _semantic() -> None:
            hits = await self._dj._index.search_by_text(text=seed_text, limit=min(max(50, k), 300))
            for hit in hits:
                payload = hit.get("payload") if isinstance(hit.get("payload"), dict) else {}
                title = str(payload.get("title") or "").strip()
                artist = str(payload.get("artist") or "").strip()
                track_id = str(hit.get("track_id") or payload.get("track_id") or "").strip()
                if not (track_id and title and artist):
                    continue
                semantic_items.append(
                    {
                        "track_id": track_id,
                        "title": title,
                        "artist": artist,
                        "score": float(hit.get("score") or 0.0),
                        "reason": "Semantic match to your recent listening context",
                    }
                )

        async def _sequential_and_graph() -> None:
            seq = await self._dj._get_pair_candidates(session.guild_id, session.recent_track_ids[-6:], limit=120)
            for item in seq:
                sequential_items.append(item)

            graph = await self._dj._get_pair_candidates(session.guild_id, session.recent_track_ids[-6:], limit=120)
            for item in graph:
                graph_items.append(item)

        async def _rule_pool() -> None:
            rules = await self._dj._get_rule_pool_candidates(session.guild_id, seeds, limit=160)
            rule_items.extend(rules)
            popularity_items.extend(await self._dj._get_popularity_tail_candidates(limit=120))

        await asyncio.gather(_semantic(), _sequential_and_graph(), _rule_pool(), return_exceptions=True)

        # Remove explicit session repeats before fusion.
        seen_session = set(session.played_titles) | set(session.skipped_titles)
        for arr in (semantic_items, sequential_items, graph_items, rule_items, popularity_items):
            arr[:] = [
                it
                for it in arr
                if f"{str(it.get('artist', '')).lower()} - {str(it.get('title', '')).lower()}" not in seen_session
            ]

        fused: dict[str, dict[str, Any]] = {}
        self._rrf_accumulate(
            fused,
            semantic_items,
            channel="semantic_ann",
            weight=self.CHANNEL_WEIGHTS["semantic_ann"],
            rrf_k=self._dj._resolver_rrf_k,
        )
        self._rrf_accumulate(
            fused,
            sequential_items,
            channel="sequential",
            weight=self.CHANNEL_WEIGHTS["sequential"],
            rrf_k=self._dj._resolver_rrf_k,
        )
        self._rrf_accumulate(
            fused,
            graph_items,
            channel="graph",
            weight=self.CHANNEL_WEIGHTS["graph"],
            rrf_k=self._dj._resolver_rrf_k,
        )
        self._rrf_accumulate(
            fused,
            rule_items,
            channel="lastfm",
            weight=self.CHANNEL_WEIGHTS["lastfm"],
            rrf_k=self._dj._resolver_rrf_k,
        )
        self._rrf_accumulate(
            fused,
            popularity_items,
            channel="popularity_tail",
            weight=self.CHANNEL_WEIGHTS["popularity_tail"],
            rrf_k=self._dj._resolver_rrf_k,
        )

        merged = list(fused.values())
        merged.sort(key=lambda x: float(x.get("retrieval_score", 0.0) or 0.0), reverse=True)
        merged = merged[: max(1, k)]

        for row in merged:
            sources = list(row.get("candidate_sources") or [])
            resolver_confidence = 0.55
            if "semantic_ann" in sources:
                resolver_confidence = 0.78
            elif "sequential" in sources or "graph" in sources:
                resolver_confidence = 0.68
            elif "lastfm" in sources:
                resolver_confidence = 0.60
            row["resolver_confidence"] = resolver_confidence
            row["source"] = sources[0] if sources else "unknown"
            if not row.get("reason"):
                row["reason"] = "Recommended from your recent session behavior"

        return merged


class AIDJ:
    def __init__(self):
        self._db_path = _DB_PATH
        self._lastfm: Optional[LastFMClient] = None
        lastfm_key = (os.getenv("LASTFM_API_KEY") or "").strip()
        if lastfm_key:
            self._lastfm = LastFMClient(lastfm_key)
            logger.info("AI DJ initialized with Last.fm support.")
        else:
            logger.warning("AI DJ: LASTFM_API_KEY not set; rule retrieval is limited.")

        self._prefetch_count = self._env_int("DJ_PREFETCH_COUNT", 5, minimum=2, maximum=20)
        self._history_days = self._env_int("DJ_HISTORY_WEIGHT_DAYS", 30, minimum=1)
        self._similar_cache_ttl = self._env_int("DJ_SIMILAR_CACHE_TTL_SECONDS", 604_800, minimum=3600)

        self._vector_backend = (os.getenv("DJ_VECTOR_BACKEND", "qdrant") or "qdrant").strip().lower()
        self._qdrant_url = (os.getenv("DJ_QDRANT_URL") or "").strip()
        self._candidate_pool_size = self._env_int("DJ_CANDIDATE_POOL_SIZE", 300, minimum=30, maximum=700)
        self._rank_topk = self._env_int("DJ_RANK_TOPK", 50, minimum=10, maximum=200)
        self._buffer_size = self._env_int("DJ_BUFFER_SIZE", 10, minimum=2, maximum=40)
        self._bandit_alpha = self._env_float("DJ_BANDIT_ALPHA", 0.14, minimum=0.0)
        self._mmr_lambda = self._env_float("DJ_MMR_LAMBDA", 0.65, minimum=0.05, maximum=0.95)
        self._global_signal_enabled = self._env_bool("DJ_GLOBAL_SIGNAL_ENABLED", True)
        self._resolver_rrf_k = self._env_int("TRACK_RESOLVER_RRF_K", 20, minimum=1)

        self._sessions: dict[int, DJSession] = {}
        self._guild_stats: dict[int, dict[str, dict[str, float]]] = {}
        self._guild_stats_ts: dict[int, float] = {}  # timestamp of last guild stats load
        self._guild_stats_ttl: float = 120.0  # seconds before guild stats are reloaded
        self._session_idle_timeout: float = 6 * 3600.0  # 6 hours

        self._feature_builder = FeatureBuilder()
        self._ranker = ContextualRanker(weights=self._load_ranker_weights())
        self._bandit = LinUCBBanditPolicy()
        self._slate = SlateReranker(mmr_lambda=self._mmr_lambda)

        from utils.dj_learner import learner as _learner
        self._learner = _learner

        self._index = HybridTrackIndex(
            db_path=self._db_path,
            backend=self._vector_backend,
            qdrant_url=self._qdrant_url,
            snapshot_path=os.path.join(_DATA_DIR, "faiss_tracks_v2.index"),
            embedding_dim=self._env_int("DJ_EMBEDDING_DIM", 256, minimum=64, maximum=1536),
            embed_model_name=(
                os.getenv("DJ_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
                or "sentence-transformers/all-MiniLM-L6-v2"
            ).strip(),
        )
        self._retriever = CandidateRetriever(self)

        self._ensure_tables()

    def _load_ranker_weights(self) -> Optional[dict[str, float]]:
        weights_path = (
            os.getenv("DJ_RANKER_WEIGHTS_PATH")
            or os.path.join(_DATA_DIR, "dj_ranker_weights.json")
        ).strip()
        if not weights_path or not os.path.exists(weights_path):
            return None
        try:
            with open(weights_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            weights_raw = payload.get("weights") if isinstance(payload, dict) else None
            if not isinstance(weights_raw, dict):
                raise ValueError("missing weights object")
            weights: dict[str, float] = {}
            for key, value in weights_raw.items():
                if not isinstance(key, str):
                    continue
                weights[key] = float(value)
            if not weights:
                raise ValueError("no valid weights")
            logger.info("AI DJ loaded trained ranker weights from %s", weights_path)
            return weights
        except Exception as exc:
            logger.warning("AI DJ failed loading ranker weights from %s: %s", weights_path, exc)
            return None

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 999_999_999) -> int:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _env_float(
        name: str,
        default: float,
        *,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
    ) -> float:
        raw = (os.getenv(name) or "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _db_connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_tables(self) -> None:
        try:
            with self._db_connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dj_listen_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        track_title TEXT NOT NULL,
                        track_artist TEXT NOT NULL,
                        track_duration_ms INTEGER DEFAULT 0,
                        listened_ms INTEGER DEFAULT 0,
                        was_skipped INTEGER DEFAULT 0,
                        skip_position_ms INTEGER DEFAULT 0,
                        source TEXT DEFAULT 'user_queued',
                        session_id TEXT DEFAULT '',
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_history_guild ON dj_listen_history(guild_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_dj_history_track ON dj_listen_history(track_artist, track_title);

                    CREATE TABLE IF NOT EXISTS dj_track_tags (
                        track_artist TEXT NOT NULL,
                        track_title TEXT NOT NULL,
                        tags_json TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (track_artist, track_title)
                    );

                    CREATE TABLE IF NOT EXISTS dj_similar_cache (
                        cache_key TEXT PRIMARY KEY,
                        result_json TEXT NOT NULL,
                        source TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS dj_track_catalog_v2 (
                        track_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        artist TEXT NOT NULL,
                        isrc TEXT DEFAULT '',
                        tags_json TEXT DEFAULT '[]',
                        popularity REAL DEFAULT 0,
                        source_catalog TEXT DEFAULT '',
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_track_catalog_v2_title_artist
                        ON dj_track_catalog_v2(artist, title);

                    CREATE TABLE IF NOT EXISTS dj_events_v2 (
                        event_id TEXT PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        anon_user_id TEXT NOT NULL,
                        track_id TEXT NOT NULL,
                        ts REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        listened_ms INTEGER DEFAULT 0,
                        context_json TEXT DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_events_v2_guild_ts
                        ON dj_events_v2(guild_id, ts DESC);

                    CREATE TABLE IF NOT EXISTS dj_item_stats_v2 (
                        track_id TEXT PRIMARY KEY,
                        plays INTEGER DEFAULT 0,
                        skips INTEGER DEFAULT 0,
                        completions INTEGER DEFAULT 0,
                        last_played_at REAL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS dj_pair_cooccurrence_v2 (
                        track_id_a TEXT NOT NULL,
                        track_id_b TEXT NOT NULL,
                        guild_id INTEGER NOT NULL,
                        weight REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (track_id_a, track_id_b, guild_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_pair_cooccurrence_v2_a
                        ON dj_pair_cooccurrence_v2(track_id_a, guild_id, weight DESC);
                    CREATE INDEX IF NOT EXISTS idx_dj_pair_cooccurrence_v2_b
                        ON dj_pair_cooccurrence_v2(track_id_b, guild_id, weight DESC);

                    CREATE TABLE IF NOT EXISTS dj_recs_audit_v2 (
                        event_id TEXT PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        served_model TEXT NOT NULL,
                        ranked_json TEXT NOT NULL,
                        chosen_track_id TEXT DEFAULT '',
                        outcome_json TEXT DEFAULT '{}',
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_recs_audit_v2_guild
                        ON dj_recs_audit_v2(guild_id, created_at DESC);
                    """
                )
        except Exception as exc:
            logger.error("Failed creating AI DJ tables: %s", exc)

    @staticmethod
    def _anon_user_id(user_id: int) -> str:
        return hashlib.sha1(str(user_id).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _title_artist_key(title: str, artist: str) -> str:
        return f"{str(artist).lower().strip()} - {str(title).lower().strip()}"

    async def _get_cooccurrence_candidates(
        self,
        guild_id: int,
        seeds: list[SeedTrack],
        *,
        limit: int = 20,
    ) -> list[tuple[dict[str, Any], float]]:
        if not seeds:
            return []

        seed_keys = [(s.artist.lower(), s.title.lower()) for s in seeds]

        def _query() -> list[tuple[dict[str, Any], float]]:
            results: list[tuple[dict[str, Any], float]] = []
            cutoff = time.time() - (self._history_days * 86400)
            try:
                with self._db_connect() as conn:
                    seed_conditions = " OR ".join(
                        "(LOWER(track_artist) = ? AND LOWER(track_title) = ?)"
                        for _ in seed_keys
                    )
                    params: list[Any] = []
                    for ak, tk in seed_keys:
                        params.extend([ak, tk])

                    sessions = conn.execute(
                        f"""
                        SELECT DISTINCT session_id
                        FROM dj_listen_history
                        WHERE guild_id = ? AND created_at > ? AND session_id != ''
                        AND ({seed_conditions})
                        """,
                        [guild_id, cutoff] + params,
                    ).fetchall()
                    session_ids = [str(r["session_id"]) for r in sessions if str(r["session_id"]).strip()]
                    if not session_ids:
                        return results

                    placeholders = ",".join("?" for _ in session_ids)
                    rows = conn.execute(
                        f"""
                        SELECT track_artist, track_title, COUNT(*) AS freq
                        FROM dj_listen_history
                        WHERE guild_id = ? AND session_id IN ({placeholders}) AND was_skipped = 0
                        GROUP BY LOWER(track_artist), LOWER(track_title)
                        ORDER BY freq DESC
                        LIMIT ?
                        """,
                        [guild_id] + session_ids + [limit * 2],
                    ).fetchall()

                    seed_set = {f"{a} - {t}" for a, t in seed_keys}
                    for row in rows:
                        key = f"{str(row['track_artist']).lower()} - {str(row['track_title']).lower()}"
                        if key in seed_set:
                            continue
                        score = min(1.0, float(row["freq"] or 0.0) / 5.0)
                        results.append(({"title": str(row["track_title"]), "artist": str(row["track_artist"])}, score))
                        if len(results) >= limit:
                            break
            except Exception as exc:
                logger.debug("Co-occurrence query error: %s", exc)
            return results

        return await asyncio.to_thread(_query)

    @staticmethod
    def _source_entropy(rows: list[RankedCandidate]) -> float:
        if not rows:
            return 0.0
        counts: dict[str, int] = {}
        total = 0
        for row in rows:
            src = row.source or "unknown"
            counts[src] = counts.get(src, 0) + 1
            total += 1
        if total <= 0:
            return 0.0
        entropy = 0.0
        for count in counts.values():
            p = count / total
            entropy -= p * math.log(p + 1e-12, 2)
        return entropy / max(1.0, math.log(max(2, len(counts)), 2))

    @staticmethod
    def _track_id(title: str, artist: str, *, isrc: Optional[str] = None) -> str:
        normalized_isrc = str(isrc or "").strip().upper()
        if normalized_isrc:
            return f"isrc:{normalized_isrc}"
        raw = f"{str(artist).strip().lower()}::{str(title).strip().lower()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    async def _upsert_catalog_and_index(
        self,
        *,
        track_id: str,
        title: str,
        artist: str,
        isrc: Optional[str] = None,
        tags: Optional[list[str]] = None,
        popularity: float = 0.0,
        source_catalog: str = "session",
    ) -> None:
        payload_tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]

        def _write() -> None:
            with self._db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dj_track_catalog_v2
                    (track_id, title, artist, isrc, tags_json, popularity, source_catalog, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(track_id) DO UPDATE SET
                        title=excluded.title,
                        artist=excluded.artist,
                        isrc=excluded.isrc,
                        tags_json=excluded.tags_json,
                        popularity=excluded.popularity,
                        source_catalog=excluded.source_catalog,
                        updated_at=excluded.updated_at
                    """,
                    (
                        track_id,
                        title,
                        artist,
                        str(isrc or "").strip().upper(),
                        json.dumps(payload_tags),
                        float(popularity),
                        source_catalog,
                        time.time(),
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_write)
        await self._index.upsert_track(
            track_id=track_id,
            title=title,
            artist=artist,
            payload={
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "isrc": str(isrc or "").strip().upper(),
                "tags": payload_tags,
                "popularity": float(popularity),
                "source_catalog": source_catalog,
            },
        )

    def get_session(self, guild_id: int) -> Optional[DJSession]:
        return self._sessions.get(guild_id)

    def is_active(self, guild_id: int) -> bool:
        session = self._sessions.get(guild_id)
        return bool(session and session.active)

    def start_session(self, guild_id: int, seed: Optional[SeedTrack] = None) -> DJSession:
        old = self._sessions.get(guild_id)
        if old and old._prefetch_task and not old._prefetch_task.done():
            old._prefetch_task.cancel()

        session = DJSession(guild_id=guild_id)
        if seed:
            session.seed_tracks.append(seed)
            session.recent_artist_history.append(seed.artist)
            session.recent_artist_history = session.recent_artist_history[-20:]
        self._sessions[guild_id] = session
        logger.info("AI DJ session started for guild %s", guild_id)
        return session

    def stop_session(self, guild_id: int) -> None:
        session = self._sessions.pop(guild_id, None)
        if session:
            if session._prefetch_task and not session._prefetch_task.done():
                session._prefetch_task.cancel()
            session.active = False
            logger.info("AI DJ session stopped for guild %s", guild_id)
        self._guild_stats.pop(guild_id, None)
        self._guild_stats_ts.pop(guild_id, None)

    def cleanup_idle_sessions(self) -> int:
        """Remove sessions idle longer than ``_session_idle_timeout``. Returns count removed."""
        now = time.time()
        expired = [
            gid
            for gid, s in self._sessions.items()
            if now - s.last_activity_at > self._session_idle_timeout
        ]
        for gid in expired:
            self.stop_session(gid)
        if expired:
            logger.info("AI DJ: cleaned up %d idle session(s)", len(expired))
        # Also trim guild stats for guilds with no active session (max 200 entries)
        if len(self._guild_stats) > 200:
            active_guilds = set(self._sessions.keys())
            stale = [gid for gid in self._guild_stats if gid not in active_guilds]
            for gid in stale:
                self._guild_stats.pop(gid, None)
                self._guild_stats_ts.pop(gid, None)
        return len(expired)

    def add_seed(self, guild_id: int, seed: SeedTrack) -> None:
        session = self._sessions.get(guild_id)
        if not session or not session.active:
            return
        session.last_activity_at = time.time()
        session.seed_tracks.append(seed)
        if len(session.seed_tracks) > 12:
            session.seed_tracks = session.seed_tracks[-12:]
        session.recent_artist_history.append(seed.artist)
        if len(session.recent_artist_history) > 30:
            session.recent_artist_history = session.recent_artist_history[-30:]

    def on_skip(
        self,
        guild_id: int,
        title: str,
        artist: str,
        *,
        listened_ms: int = 0,
        duration_ms: int = 0,
    ) -> None:
        session = self._sessions.get(guild_id)
        if not session or not session.active:
            return
        session.last_activity_at = time.time()
        session.recent_artist_history.append(artist)
        if len(session.recent_artist_history) > 30:
            session.recent_artist_history = session.recent_artist_history[-30:]

        severity = _skip_severity(listened_ms, duration_ms)
        track_key = self._title_artist_key(title, artist)
        session.skipped_titles.add(track_key)

        # Late skips (severity < 0.15) are practically completions — don't
        # penalise the session or nuke the buffer.
        if severity >= 0.15:
            session.skip_streak += 1
            session.completion_streak = 0

            skipped_artist = artist.lower()
            # Only purge same-artist from buffer on strong skips (early in track)
            if severity >= 0.5:
                session.recommendation_buffer = [
                    r
                    for r in session.recommendation_buffer
                    if r.artist.lower() != skipped_artist
                    and self._title_artist_key(r.title, r.artist) not in session.skipped_titles
                ]
            else:
                # Mild skip — only remove the exact track, keep the artist
                session.recommendation_buffer = [
                    r
                    for r in session.recommendation_buffer
                    if self._title_artist_key(r.title, r.artist) not in session.skipped_titles
                ]

            if session._prefetch_task and not session._prefetch_task.done():
                session._prefetch_task.cancel()
                session._prefetch_task = None
        else:
            # Late skip ≈ completion — credit the session
            session.skip_streak = 0
            session.completion_streak += 1

        self._guild_stats.pop(guild_id, None)
        self._guild_stats_ts.pop(guild_id, None)

    async def record_listen(
        self,
        guild_id: int,
        user_id: int,
        title: str,
        artist: str,
        duration_ms: int,
        listened_ms: int,
        was_skipped: bool,
        skip_position_ms: int = 0,
        source: str = "user_queued",
        context: Optional[dict[str, Any]] = None,
        update_session: bool = True,
    ) -> None:
        context = dict(context or {})
        session = self._sessions.get(guild_id)
        session_id = session.session_id if session else ""

        track_key = self._title_artist_key(title, artist)
        track_id = self._track_id(title, artist, isrc=context.get("isrc"))

        severity = _skip_severity(listened_ms, duration_ms) if was_skipped else 0.0
        is_hard_skip = was_skipped and severity >= 0.15

        if session:
            session.last_activity_at = time.time()
            if update_session:
                if is_hard_skip:
                    session.skipped_titles.add(track_key)
                    session.skip_streak += 1
                    session.completion_streak = 0
                else:
                    session.played_titles.add(track_key)
                    session.skip_streak = 0
                    session.completion_streak += 1
                session.recent_artist_history.append(artist)
                session.recent_artist_history = session.recent_artist_history[-30:]

        now = time.time()
        event_type = "skip" if was_skipped else "complete"
        anon_user_id = self._anon_user_id(user_id)

        def _persist() -> None:
            try:
                with self._db_connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO dj_listen_history
                        (guild_id, user_id, track_title, track_artist, track_duration_ms,
                         listened_ms, was_skipped, skip_position_ms, source, session_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            user_id,
                            title,
                            artist,
                            int(duration_ms or 0),
                            int(listened_ms or 0),
                            int(bool(was_skipped)),
                            int(skip_position_ms or 0),
                            source,
                            session_id,
                            now,
                        ),
                    )

                    event_id = uuid.uuid4().hex
                    context_blob = json.dumps(context, ensure_ascii=False)
                    conn.execute(
                        """
                        INSERT INTO dj_events_v2
                        (event_id, guild_id, anon_user_id, track_id, ts, event_type, listened_ms, context_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            guild_id,
                            anon_user_id,
                            track_id,
                            now,
                            event_type,
                            int(listened_ms or 0),
                            context_blob,
                        ),
                    )

                    # Severity-weighted skip: early skip counts as ~1.0 skip,
                    # late skip (>80% listened) counts as ~0.1 skip
                    weighted_skip = severity if was_skipped else 0.0
                    weighted_completion = (1.0 - severity) if was_skipped else 1.0
                    conn.execute(
                        """
                        INSERT INTO dj_item_stats_v2(track_id, plays, skips, completions, last_played_at, updated_at)
                        VALUES (?, 1, ?, ?, ?, ?)
                        ON CONFLICT(track_id) DO UPDATE SET
                            plays = plays + 1,
                            skips = skips + excluded.skips,
                            completions = completions + excluded.completions,
                            last_played_at = excluded.last_played_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            track_id,
                            weighted_skip,
                            weighted_completion,
                            now,
                            now,
                        ),
                    )

                    if session and session.recent_track_ids:
                        prev_id = session.recent_track_ids[-1]
                        if prev_id and prev_id != track_id:
                            # Co-occurrence weight: full for completions,
                            # severity-scaled for skips (late skip ≈ 0.9, early skip ≈ 0.1)
                            pair_weight = (1.0 - severity) if was_skipped else 1.0
                            conn.execute(
                                """
                                INSERT INTO dj_pair_cooccurrence_v2(track_id_a, track_id_b, guild_id, weight, updated_at)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(track_id_a, track_id_b, guild_id) DO UPDATE SET
                                    weight = weight + excluded.weight,
                                    updated_at = excluded.updated_at
                                """,
                                (prev_id, track_id, guild_id, pair_weight, now),
                            )

                    conn.commit()
            except Exception as exc:
                logger.warning("Failed recording DJ listen event: %s", exc)

        await asyncio.to_thread(_persist)

        # Online learning: record reward and trigger incremental updates
        try:
            reward = self._learner.compute_reward(
                listened_ms=listened_ms,
                duration_ms=duration_ms,
                was_skipped=was_skipped,
            )
            rank_features = dict(context.get("rank_features", {}) or {})
            await self._learner.record_reward(
                guild_id=guild_id,
                track_id=track_id,
                reward=reward,
                features=rank_features,
            )
        except Exception as exc:
            logger.debug("Online learner reward recording error: %s", exc)

        if session:
            session.recent_track_ids.append(track_id)
            session.recent_track_ids = session.recent_track_ids[-24:]
            audit_event_id = str(context.get("audit_event_id") or "").strip()
            if not audit_event_id:
                audit_event_id = str(session.last_audit_event_id or "").strip()
            if source == "ai_dj" and audit_event_id:
                await self._append_audit_outcome(
                    audit_event_id,
                    {
                        "event_type": event_type,
                        "track_id": track_id,
                        "listened_ms": int(listened_ms or 0),
                        "skip_severity": round(severity, 3) if was_skipped else 0.0,
                        "ts": now,
                    },
                )

        self._guild_stats.pop(guild_id, None)
        self._guild_stats_ts.pop(guild_id, None)
        try:
            await self._upsert_catalog_and_index(
                track_id=track_id,
                title=title,
                artist=artist,
                isrc=context.get("isrc"),
                tags=context.get("tags") if isinstance(context.get("tags"), list) else None,
                popularity=float(context.get("popularity") or 0.0),
                source_catalog=str(context.get("source_catalog") or "session"),
            )
        except Exception as exc:
            logger.debug("DJ catalog/index upsert skipped: %s", exc)

    async def record_resolver_outcome(
        self,
        *,
        guild_id: int,
        user_id: int,
        title: str,
        artist: str,
        resolved: bool,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        context = dict(context or {})
        track_id = self._track_id(title, artist, isrc=context.get("isrc"))
        event_type = "resolver_success" if resolved else "resolver_failure"
        anon_user_id = self._anon_user_id(user_id)

        def _write() -> None:
            with self._db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dj_events_v2
                    (event_id, guild_id, anon_user_id, track_id, ts, event_type, listened_ms, context_json)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        guild_id,
                        anon_user_id,
                        track_id,
                        time.time(),
                        event_type,
                        json.dumps(context, ensure_ascii=False),
                    ),
                )
                conn.commit()

        await asyncio.to_thread(_write)

    async def _append_audit_outcome(self, event_id: str, outcome_fragment: dict[str, Any]) -> None:
        def _write() -> None:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT outcome_json FROM dj_recs_audit_v2 WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                payload: dict[str, Any] = {}
                if row:
                    try:
                        payload = json.loads(str(row["outcome_json"] or "{}"))
                    except Exception:
                        payload = {}
                history = payload.get("events")
                if not isinstance(history, list):
                    history = []
                history.append(outcome_fragment)
                payload["events"] = history[-40:]
                conn.execute(
                    "UPDATE dj_recs_audit_v2 SET outcome_json = ? WHERE event_id = ?",
                    (json.dumps(payload, ensure_ascii=False), event_id),
                )
                conn.commit()

        await asyncio.to_thread(_write)

    def _load_guild_stats(self, guild_id: int) -> dict[str, dict[str, float]]:
        now = time.time()
        if guild_id in self._guild_stats:
            if now - self._guild_stats_ts.get(guild_id, 0.0) < self._guild_stats_ttl:
                return self._guild_stats[guild_id]

        cutoff = time.time() - (self._history_days * 86400)
        out: dict[str, dict[str, float]] = {}
        try:
            with self._db_connect() as conn:
                rows = conn.execute(
                    """
                    SELECT track_artist, track_title,
                           COUNT(*) as plays,
                           SUM(was_skipped) as skips,
                           MAX(created_at) as last_played,
                           AVG(CASE WHEN was_skipped = 1 AND track_duration_ms > 0
                               THEN MIN(1.0, CAST(listened_ms AS REAL) / track_duration_ms)
                               ELSE NULL END) as avg_skip_ratio
                    FROM dj_listen_history
                    WHERE guild_id = ? AND created_at > ?
                    GROUP BY track_artist, track_title
                    """,
                    (guild_id, cutoff),
                ).fetchall()
            for row in rows:
                key = self._title_artist_key(row["track_title"], row["track_artist"])
                plays = max(1, int(row["plays"] or 0))
                raw_skips = int(row["skips"] or 0)
                # Weight skips by severity: late skips (avg ratio > 0.8) count less
                avg_skip_ratio = float(row["avg_skip_ratio"] or 0.0)
                avg_severity = math.exp(-3.0 * avg_skip_ratio) if raw_skips > 0 else 0.0
                weighted_skips = raw_skips * avg_severity
                out[key] = {
                    "skip_rate": weighted_skips / plays,
                    "completion_rate": 1.0 - (weighted_skips / plays),
                    "last_played": float(row["last_played"] or 0.0),
                    "plays": float(plays),
                }
        except Exception as exc:
            logger.debug("Failed loading guild stats: %s", exc)

        self._guild_stats[guild_id] = out
        self._guild_stats_ts[guild_id] = time.time()
        return out

    def _load_item_stats(self, track_ids: list[str]) -> dict[str, dict[str, float]]:
        if not track_ids:
            return {}
        out: dict[str, dict[str, float]] = {}
        try:
            with self._db_connect() as conn:
                placeholders = ",".join("?" for _ in track_ids)
                rows = conn.execute(
                    f"""
                    SELECT track_id, plays, skips, completions, last_played_at
                    FROM dj_item_stats_v2
                    WHERE track_id IN ({placeholders})
                    """,
                    track_ids,
                ).fetchall()
            for row in rows:
                out[str(row["track_id"])] = {
                    "plays": float(row["plays"] or 0.0),
                    "skips": float(row["skips"] or 0.0),
                    "completions": float(row["completions"] or 0.0),
                    "last_played": float(row["last_played_at"] or 0.0),
                }
        except Exception as exc:
            logger.debug("Failed loading item stats: %s", exc)
        return out

    async def _get_pair_candidates(
        self,
        guild_id: int,
        seed_track_ids: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        seeds = [s for s in seed_track_ids if s]
        if not seeds:
            return []

        def _query() -> list[dict[str, Any]]:
            rows_out: list[dict[str, Any]] = []
            try:
                with self._db_connect() as conn:
                    clauses = []
                    params: list[Any] = []
                    for sid in seeds:
                        clauses.append("track_id_a = ?")
                        params.append(sid)
                    seed_clause = " OR ".join(clauses)

                    base_query = f"""
                        SELECT track_id_b AS track_id, SUM(weight) AS total_weight
                        FROM dj_pair_cooccurrence_v2
                        WHERE ({seed_clause})
                    """
                    if not self._global_signal_enabled:
                        base_query += " AND guild_id = ? "
                        params.append(guild_id)
                    base_query += " GROUP BY track_id_b ORDER BY total_weight DESC LIMIT ?"
                    params.append(max(1, limit * 2))

                    rows = conn.execute(base_query, params).fetchall()
                    if not rows:
                        return []

                    track_ids = [str(row["track_id"]) for row in rows if str(row["track_id"]).strip()]
                    if not track_ids:
                        return []

                    placeholders = ",".join("?" for _ in track_ids)
                    cat = conn.execute(
                        f"""
                        SELECT track_id, title, artist
                        FROM dj_track_catalog_v2
                        WHERE track_id IN ({placeholders})
                        """,
                        track_ids,
                    ).fetchall()
                    catalog = {str(row["track_id"]): row for row in cat}

                    max_w = max(float(row["total_weight"] or 0.0) for row in rows) or 1.0
                    log_max = math.log1p(max_w)
                    for row in rows:
                        tid = str(row["track_id"])
                        cat_row = catalog.get(tid)
                        if not cat_row:
                            continue
                        raw_w = float(row["total_weight"] or 0.0)
                        score = math.log1p(raw_w) / log_max if log_max > 0 else 0.0
                        rows_out.append(
                            {
                                "track_id": tid,
                                "title": str(cat_row["title"]),
                                "artist": str(cat_row["artist"]),
                                "score": min(1.0, max(0.0, score)),
                                "reason": "Frequently follows tracks like your current session",
                            }
                        )
                        if len(rows_out) >= limit:
                            break
            except Exception as exc:
                logger.debug("Pair candidate query error: %s", exc)
            return rows_out

        return await asyncio.to_thread(_query)

    async def _get_popularity_tail_candidates(self, *, limit: int) -> list[dict[str, Any]]:
        def _query() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            try:
                with self._db_connect() as conn:
                    rows = conn.execute(
                        """
                        SELECT c.track_id, c.title, c.artist,
                               (s.completions * 1.0) / CASE WHEN s.plays <= 0 THEN 1 ELSE s.plays END AS completion_rate,
                               s.plays
                        FROM dj_item_stats_v2 s
                        JOIN dj_track_catalog_v2 c ON c.track_id = s.track_id
                        WHERE s.plays >= 2
                        ORDER BY completion_rate DESC, s.plays DESC
                        LIMIT ?
                        """,
                        (max(1, limit),),
                    ).fetchall()
                max_plays = max((int(row["plays"] or 0) for row in rows), default=1)
                for row in rows:
                    score = float(row["completion_rate"] or 0.0)
                    play_norm = min(1.0, (int(row["plays"] or 0) / max_plays))
                    out.append(
                        {
                            "track_id": str(row["track_id"]),
                            "title": str(row["title"]),
                            "artist": str(row["artist"]),
                            "score": 0.75 * score + 0.25 * play_norm,
                            "reason": "Popular completion-friendly pick from your broader listening graph",
                        }
                    )
            except Exception as exc:
                logger.debug("Popularity candidate query error: %s", exc)
            return out

        return await asyncio.to_thread(_query)

    async def _cached_lastfm_call(self, cache_key: str, source: str, fetcher: Callable[[], Any]) -> list[dict[str, Any]]:
        try:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT result_json, updated_at FROM dj_similar_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if row and (time.time() - float(row["updated_at"] or 0.0)) < self._similar_cache_ttl:
                    return json.loads(str(row["result_json"] or "[]"))
        except Exception:
            pass

        results = await fetcher()
        if results:
            try:
                with self._db_connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO dj_similar_cache (cache_key, result_json, source, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (cache_key, json.dumps(results), source, time.time()),
                    )
                    conn.commit()
            except Exception:
                pass
        return results

    async def _get_track_tags(self, artist: str, title: str) -> list[dict[str, Any]]:
        lower_artist = str(artist).lower().strip()
        lower_title = str(title).lower().strip()
        try:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT tags_json, updated_at FROM dj_track_tags WHERE track_artist = ? AND track_title = ?",
                    (lower_artist, lower_title),
                ).fetchone()
                if row and (time.time() - float(row["updated_at"] or 0.0)) < self._similar_cache_ttl:
                    return json.loads(str(row["tags_json"] or "[]"))
        except Exception:
            pass

        if not self._lastfm:
            return []

        tags = await self._lastfm.track_get_top_tags(artist, title)
        if tags:
            try:
                with self._db_connect() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO dj_track_tags (track_artist, track_title, tags_json, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (lower_artist, lower_title, json.dumps(tags), time.time()),
                    )
                    conn.commit()
            except Exception:
                pass
        return tags

    async def _get_rule_pool_candidates(
        self,
        guild_id: int,
        seeds: list[SeedTrack],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not seeds:
            return []

        merged: dict[str, dict[str, Any]] = {}

        def _add(title: str, artist: str, score: float, reason: str) -> None:
            track_id = self._track_id(title, artist)
            item = merged.setdefault(
                track_id,
                {
                    "track_id": track_id,
                    "title": title,
                    "artist": artist,
                    "score": 0.0,
                    "reason": reason,
                },
            )
            item["score"] = max(float(item.get("score", 0.0)), float(score))
            if not item.get("reason"):
                item["reason"] = reason

        # Last.fm sources.
        if self._lastfm:
            recent = seeds[-3:]
            for idx, seed in enumerate(recent):
                seed_weight = 1.0 * (0.72 ** (len(recent) - 1 - idx))
                cache_key = f"similar:{seed.artist.lower()}|{seed.title.lower()}"
                similars = await self._cached_lastfm_call(
                    cache_key,
                    "track.getSimilar",
                    lambda s=seed: self._lastfm.track_get_similar(s.artist, s.title, limit=28),
                )
                for row in similars:
                    _add(
                        str(row.get("title") or "").strip(),
                        str(row.get("artist") or "").strip(),
                        float(row.get("match") or 0.0) * seed_weight,
                        f"Similar to {seed.title}",
                    )

            for seed in seeds[-2:]:
                cache_key = f"artist_similar:{seed.artist.lower()}"
                arts = await self._cached_lastfm_call(
                    cache_key,
                    "artist.getSimilar",
                    lambda s=seed: self._lastfm.artist_get_similar(s.artist, limit=10),
                )
                for art in arts[:6]:
                    art_name = str(art.get("name") or "").strip()
                    if not art_name:
                        continue
                    top_key = f"artist_top:{art_name.lower()}"
                    top_tracks = await self._cached_lastfm_call(
                        top_key,
                        "artist.getTopTracks",
                        lambda a=art_name: self._lastfm.artist_get_top_tracks(a, limit=4),
                    )
                    for t in top_tracks:
                        _add(
                            str(t.get("title") or "").strip(),
                            str(t.get("artist") or "").strip(),
                            float(art.get("match") or 0.0) * 0.58,
                            f"Top track by artist related to {seed.artist}",
                        )

            latest_seed = seeds[-1]
            tags = await self._get_track_tags(latest_seed.artist, latest_seed.title)
            # Deduplicate similar tags and use up to 4, weighted by count
            deduped_tags: list[dict] = []
            seen_tag_stems: set[str] = set()
            max_count = max((int(t.get("count") or 1) for t in tags[:10]), default=1) if tags else 1
            for tag in tags[:10]:
                tag_name = str(tag.get("name") or "").strip()
                if not tag_name:
                    continue
                stem = tag_name.lower().replace("-", " ").replace("_", " ")
                stem = " ".join(sorted(stem.split()))
                if any(s in stem or stem in s for s in seen_tag_stems):
                    continue
                seen_tag_stems.add(stem)
                deduped_tags.append(tag)
                if len(deduped_tags) >= 4:
                    break
            for tag in deduped_tags:
                tag_name = str(tag.get("name") or "").strip()
                if not tag_name:
                    continue
                tag_count = max(1, int(tag.get("count") or 1))
                tag_weight = math.log1p(tag_count) / math.log1p(max_count) if max_count > 1 else 1.0
                tag_key = f"tag_top:{tag_name}"
                tag_tracks = await self._cached_lastfm_call(
                    tag_key,
                    "tag.getTopTracks",
                    lambda t=tag_name: self._lastfm.tag_get_top_tracks(t, limit=8),
                )
                for t in tag_tracks:
                    _add(
                        str(t.get("title") or "").strip(),
                        str(t.get("artist") or "").strip(),
                        0.46 * tag_weight,
                        f"Shares style tag '{tag_name}'",
                    )

        # Guild-local co-occurrence fallback pool.
        cooc = await self._get_cooccurrence_candidates(guild_id, seeds, limit=min(60, limit // 2))
        for track, score in cooc:
            _add(
                str(track.get("title") or "").strip(),
                str(track.get("artist") or "").strip(),
                float(score or 0.0),
                "Frequently co-played in your guild",
            )

        rows = [row for row in merged.values() if row.get("track_id") and row.get("title") and row.get("artist")]
        rows.sort(key=lambda item: float(item.get("score", 0.0) or 0.0), reverse=True)

        # Ensure catalog/index knows these candidates.
        for row in rows[: min(len(rows), max(40, limit // 2))]:
            try:
                await self._upsert_catalog_and_index(
                    track_id=str(row["track_id"]),
                    title=str(row["title"]),
                    artist=str(row["artist"]),
                    source_catalog="lastfm",
                )
            except Exception:
                pass

        return rows[: max(1, limit)]

    async def _get_recommendations_v2(self, guild_id: int, *, count: int) -> list[RecommendedTrack]:
        session = self._sessions.get(guild_id)
        if not session or not session.active or not session.seed_tracks:
            return []

        candidates = await self._retriever.retrieve(session, self._candidate_pool_size)
        if not candidates:
            return []

        track_ids = [str(c.get("track_id")) for c in candidates if c.get("track_id")]
        guild_stats = self._load_guild_stats(guild_id)
        item_stats = self._load_item_stats(track_ids)

        rows = self._feature_builder.build(
            session=session,
            candidates=candidates,
            guild_stats=guild_stats,
            item_stats=item_stats,
            now_ts=time.time(),
        )
        if not rows:
            return []

        rows = self._ranker.score(rows)

        # Blend in online-learned signals (BPR affinity, Thompson Sampling, sequential fit)
        recent_ids = list(session.recent_track_ids[-10:])
        for row in rows:
            try:
                learned = self._learner.score_candidate(
                    guild_id=guild_id,
                    track_id=row.track_id,
                    recent_track_ids=recent_ids,
                    rank_features=row.rank_features,
                )
                # Scale learned signals and blend into rank_score
                # BPR affinity: personal preference signal (±10 points)
                bpr_bonus = learned.get("bpr_affinity", 0.0) * 10.0
                # Thompson Sampling: zero-centered uncertainty adjustment.
                ts_bonus = _centered_ts_bonus(learned.get("ts_exploration", 0.5))
                # Sequential fit: session flow signal (0–8 points)
                seq_bonus = learned.get("sequential_fit", 0.0) * 8.0
                # Predicted reward: learned skip/completion model (0–6 points)
                pred_bonus = (learned.get("predicted_reward", 0.5) - 0.5) * 12.0

                row.rank_score += bpr_bonus + ts_bonus + seq_bonus + pred_bonus
                row.rank_features["bpr_affinity"] = learned.get("bpr_affinity", 0.0)
                row.rank_features["ts_exploration"] = learned.get("ts_exploration", 0.0)
                row.rank_features["sequential_fit"] = learned.get("sequential_fit", 0.0)
                row.rank_features["predicted_reward"] = learned.get("predicted_reward", 0.0)
            except Exception:
                pass

        # Re-sort after blending learned signals
        rows.sort(key=lambda item: item.rank_score, reverse=True)
        source_entropy = self._source_entropy(rows[:20])

        # Cold-start: boost exploration when seeds and history are thin
        is_cold_start = _is_cold_start(len(session.seed_tracks), len(guild_stats))
        bandit_alpha = self._bandit_alpha
        mmr_lambda = self._mmr_lambda
        if is_cold_start:
            bandit_alpha = max(bandit_alpha, 0.20)
            mmr_lambda = max(mmr_lambda, 0.72)

        rows = self._bandit.apply(
            rows,
            base_alpha=bandit_alpha,
            session_skip_streak=session.skip_streak,
            session_completion_streak=session.completion_streak,
            source_entropy=source_entropy,
        )

        rows = rows[: max(1, self._rank_topk)]
        reranker = self._slate if not is_cold_start else SlateReranker(mmr_lambda=mmr_lambda)
        slate = reranker.rerank(rows, max_results=max(count, self._buffer_size))

        recs: list[RecommendedTrack] = []
        for row in slate[:count]:
            recs.append(
                RecommendedTrack(
                    track_id=row.track_id,
                    title=row.title,
                    artist=row.artist,
                    score=row.rank_score,
                    source=row.source,
                    reason=row.reason,
                    rank_features=dict(row.rank_features),
                    exploration_flag=row.exploration_flag,
                    model_version="v2",
                    candidate_sources=list(row.candidate_sources),
                )
            )
        return recs

    async def _write_recommendation_audit(
        self,
        guild_id: int,
        session: DJSession,
        *,
        served_model: str,
        ranked: list[RecommendedTrack],
    ) -> str:
        event_id = uuid.uuid4().hex
        try:
            ranked_json = json.dumps(
                [
                    {
                        "track_id": r.track_id or self._track_id(r.title, r.artist),
                        "title": r.title,
                        "artist": r.artist,
                        "score": r.score,
                        "source": r.source,
                        "reason": r.reason,
                        "model_version": r.model_version,
                        "exploration_flag": r.exploration_flag,
                        "candidate_sources": r.candidate_sources,
                        "rank_features": r.rank_features,
                    }
                    for r in ranked
                ],
                ensure_ascii=False,
            )
            with self._db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dj_recs_audit_v2
                    (event_id, guild_id, session_id, served_model, ranked_json, chosen_track_id, outcome_json, created_at)
                    VALUES (?, ?, ?, ?, ?, '', '{}', ?)
                    """,
                    (event_id, guild_id, session.session_id, served_model, ranked_json, time.time()),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Failed writing DJ audit row: %s", exc)
            return ""
        return event_id

    async def _mark_audit_chosen(self, event_id: str, rec: RecommendedTrack) -> None:
        if not event_id:
            return
        chosen_id = str(rec.track_id or self._track_id(rec.title, rec.artist)).strip()

        def _write() -> None:
            with self._db_connect() as conn:
                conn.execute(
                    "UPDATE dj_recs_audit_v2 SET chosen_track_id = ? WHERE event_id = ?",
                    (chosen_id, event_id),
                )
                conn.commit()

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.debug("Failed marking DJ audit chosen track: %s", exc)

    async def get_recommendations(self, guild_id: int, *, count: int = 0) -> list[RecommendedTrack]:
        session = self._sessions.get(guild_id)
        if not session or not session.active or not session.seed_tracks:
            return []

        if count <= 0:
            count = self._prefetch_count

        try:
            recs_v2 = await self._get_recommendations_v2(guild_id, count=count)
        except Exception as exc:
            logger.warning("AI DJ v2 recommendation failed: %s", exc)
            return []

        if recs_v2:
            audit_event_id = await self._write_recommendation_audit(
                guild_id,
                session,
                served_model="v2",
                ranked=recs_v2,
            )
            session.last_audit_event_id = audit_event_id
            for rec in recs_v2:
                if not rec.audit_event_id:
                    rec.audit_event_id = audit_event_id
        return recs_v2

    async def pop_next_recommendation(self, guild_id: int) -> Optional[RecommendedTrack]:
        session = self._sessions.get(guild_id)
        if not session or not session.active:
            return None

        if not session.recommendation_buffer:
            session.recommendation_buffer = await self.get_recommendations(guild_id)

        if not session.recommendation_buffer:
            return None

        rec = session.recommendation_buffer.pop(0)
        session.played_titles.add(self._title_artist_key(rec.title, rec.artist))
        audit_event_id = str(rec.audit_event_id or session.last_audit_event_id or "").strip()
        if audit_event_id:
            try:
                await self._mark_audit_chosen(audit_event_id, rec)
            except Exception as exc:
                logger.debug("Failed marking chosen recommendation: %s", exc)
        return rec

    def trigger_prefetch(self, guild_id: int) -> None:
        session = self._sessions.get(guild_id)
        if not session or not session.active:
            return
        if session._prefetch_task and not session._prefetch_task.done():
            return

        async def _prefetch() -> None:
            try:
                if len(session.recommendation_buffer) >= max(2, self._buffer_size // 2):
                    return
                recs = await self.get_recommendations(guild_id, count=self._buffer_size)
                existing = {
                    self._title_artist_key(r.title, r.artist)
                    for r in session.recommendation_buffer
                }
                # Also exclude already-played, skipped tracks, and recent artists
                excluded = existing | session.played_titles | session.skipped_titles
                recent_artists = {
                    str(a).lower() for a in session.recent_artist_history[-5:]
                }
                for rec in recs:
                    key = self._title_artist_key(rec.title, rec.artist)
                    if key in excluded:
                        continue
                    if rec.artist.lower() in recent_artists:
                        continue
                    session.recommendation_buffer.append(rec)
                    excluded.add(key)
            except Exception as exc:
                logger.debug("AI DJ prefetch error: %s", exc)

        session._prefetch_task = asyncio.create_task(_prefetch())

    async def close(self) -> None:
        for guild_id in list(self._sessions):
            self.stop_session(guild_id)
        if self._lastfm:
            await self._lastfm.close()
        await self._index.close()


# Module-level singleton
dj = AIDJ()
