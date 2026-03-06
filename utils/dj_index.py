"""Vector index utilities for AI DJ v2.

Primary backend: Qdrant (when configured)
Fallback backend: local sqlite-backed vector search (Faiss-like fallback path).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class IndexedTrack:
    track_id: str
    title: str
    artist: str
    vector: list[float]
    payload: dict[str, Any]


class TextEmbedder:
    """Embeds text using sentence-transformers when available; fallback is hashed vectors."""

    def __init__(self, *, dim: int = 256, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self.dim = max(64, int(dim))
        self.model_name = model_name
        normalized_model = (model_name or "").strip().lower()
        self._force_hash = normalized_model in {"hash", "hashed", "fallback", "off", "none", "local-hash"}
        self._hash_mode_logged = False
        self._model = None
        self._model_failed = False
        self._load_lock = asyncio.Lock()

    async def _get_model(self):
        if self._force_hash:
            if not self._hash_mode_logged:
                self._hash_mode_logged = True
                logger.info("AI DJ embedder forced to hashed mode via DJ_EMBED_MODEL=%s", self.model_name)
            return None
        if self._model is not None:
            return self._model
        if self._model_failed:
            return None

        async with self._load_lock:
            if self._model is not None:
                return self._model
            if self._model_failed:
                return None

            try:
                from sentence_transformers import SentenceTransformer  # type: ignore
            except Exception as exc:
                self._model_failed = True
                logger.warning(
                    "AI DJ embedder using hashed fallback (no semantic similarity!); "
                    "install `sentence-transformers` for quality recommendations."
                )
                logger.debug("sentence-transformers import error: %s", exc)
                return None

            try:
                self._model = await asyncio.to_thread(SentenceTransformer, self.model_name)
                logger.info("AI DJ embedder loaded model: %s", self.model_name)
                return self._model
            except Exception as exc:
                self._model_failed = True
                logger.warning("Failed loading embedder model `%s`; using hashed fallback: %s", self.model_name, exc)
                return None

    @property
    def is_hash_mode(self) -> bool:
        return self._force_hash or self._model_failed

    @staticmethod
    def _normalize(v: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in v))
        if norm <= 1e-12:
            return [0.0 for _ in v]
        return [x / norm for x in v]

    def _hash_embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[\w']+", str(text or "").lower())
        if not tokens:
            return vec

        for token in tokens:
            digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
            idx = int(digest[:8], 16) % self.dim
            sign = -1.0 if int(digest[8:10], 16) % 2 else 1.0
            vec[idx] += sign

        return self._normalize(vec)

    async def embed(self, text: str) -> list[float]:
        model = await self._get_model()
        if model is None:
            return self._hash_embed_sync(text)

        def _encode() -> list[float]:
            raw = model.encode([text], normalize_embeddings=True)
            first = raw[0]
            return [float(x) for x in first]

        try:
            return await asyncio.to_thread(_encode)
        except Exception as exc:
            logger.warning("Embedder encode failed; falling back to hash embedding: %s", exc)
            return self._hash_embed_sync(text)


class LocalVectorStore:
    """SQLite-backed vector store used as Faiss-like fallback."""

    def __init__(self, *, db_path: str, snapshot_path: str):
        self.db_path = db_path
        self.snapshot_path = snapshot_path
        self._ensure_tables()

    def _db_connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_tables(self) -> None:
        with self._db_connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dj_track_vectors_v2 (
                    track_id TEXT PRIMARY KEY,
                    vector_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dj_track_vectors_v2_updated
                    ON dj_track_vectors_v2(updated_at);
                """
            )

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na <= 1e-12 or nb <= 1e-12:
            return 0.0
        return dot / (na * nb)

    async def upsert(self, item: IndexedTrack) -> None:
        def _write() -> None:
            with self._db_connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dj_track_vectors_v2 (track_id, vector_json, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        item.track_id,
                        json.dumps(item.vector),
                        json.dumps(item.payload),
                        time.time(),
                    ),
                )
                conn.commit()

            # Lightweight snapshot for operational visibility / fallback compatibility.
            try:
                os.makedirs(os.path.dirname(self.snapshot_path), exist_ok=True)
                with open(self.snapshot_path, "w", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "format": "faiss_fallback_v1",
                                "updated_at": time.time(),
                                "note": "sqlite-backed vector snapshot placeholder",
                            },
                            ensure_ascii=False,
                        )
                    )
            except Exception as exc:
                logger.warning("Failed writing vector snapshot: %s", exc)

        await asyncio.to_thread(_write)

    async def search(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            with self._db_connect() as conn:
                recs = conn.execute(
                    "SELECT track_id, vector_json, payload_json FROM dj_track_vectors_v2 ORDER BY updated_at DESC LIMIT 5000"
                ).fetchall()

            scored: list[tuple[float, dict[str, Any]]] = []
            for row in recs:
                try:
                    row_vec = [float(x) for x in json.loads(str(row["vector_json"]))]
                    payload = json.loads(str(row["payload_json"]))
                except Exception as exc:
                    logger.debug("Skipping malformed vector row: %s", exc)
                    continue
                score = self._cosine(vector, row_vec)
                if score <= 0:
                    continue
                scored.append((score, {"track_id": row["track_id"], "payload": payload, "score": score}))

            scored.sort(key=lambda it: it[0], reverse=True)
            for _, record in scored[: max(1, limit)]:
                rows.append(record)
            return rows

        return await asyncio.to_thread(_read)


class QdrantVectorStore:
    def __init__(self, *, url: str, collection: str, dim: int):
        self.url = url.rstrip("/")
        self.collection = collection
        self.dim = dim
        self._session: Optional[aiohttp.ClientSession] = None
        self._ready = False

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_collection(self) -> bool:
        if self._ready:
            return True

        try:
            async with self.session.put(
                f"{self.url}/collections/{self.collection}",
                json={
                    "vectors": {
                        "size": self.dim,
                        "distance": "Cosine",
                    }
                },
            ) as resp:
                # 200 and 409 both imply usable collection.
                if resp.status not in {200, 201, 409}:
                    txt = await resp.text()
                    logger.debug("Qdrant collection ensure failed (%s): %s", resp.status, txt[:200])
                    return False
            self._ready = True
            return True
        except Exception as exc:
            logger.debug("Qdrant unavailable: %s", exc)
            return False

    @staticmethod
    def _point_id(track_id: str) -> str:
        return f"dj-{track_id}"

    async def upsert(self, item: IndexedTrack) -> bool:
        if not await self._ensure_collection():
            return False

        point = {
            "id": self._point_id(item.track_id),
            "vector": item.vector,
            "payload": {"track_id": item.track_id, **item.payload},
        }
        try:
            async with self.session.put(
                f"{self.url}/collections/{self.collection}/points",
                json={"points": [point]},
            ) as resp:
                if resp.status not in {200, 201}:
                    txt = await resp.text()
                    logger.debug("Qdrant upsert failed (%s): %s", resp.status, txt[:220])
                    return False
            return True
        except Exception as exc:
            logger.debug("Qdrant upsert error: %s", exc)
            return False

    async def search(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        if not await self._ensure_collection():
            return []

        body = {
            "vector": vector,
            "limit": max(1, int(limit)),
            "with_payload": True,
            "score_threshold": 0.0,
        }
        try:
            async with self.session.post(
                f"{self.url}/collections/{self.collection}/points/search",
                json=body,
            ) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.debug("Qdrant search failed (%s): %s", resp.status, txt[:200])
                    return []
                payload = await resp.json(content_type=None)
        except Exception as exc:
            logger.debug("Qdrant search error: %s", exc)
            return []

        results = payload.get("result") if isinstance(payload, dict) else []
        out: list[dict[str, Any]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            pld = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            track_id = str(pld.get("track_id") or "").strip()
            if not track_id:
                continue
            out.append(
                {
                    "track_id": track_id,
                    "payload": pld,
                    "score": float(item.get("score") or 0.0),
                }
            )
        return out


class HybridTrackIndex:
    """Qdrant primary, sqlite vector fallback."""

    def __init__(
        self,
        *,
        db_path: str,
        backend: str,
        qdrant_url: str,
        collection: str = "tracks_v1",
        embedding_dim: int = 256,
        snapshot_path: str,
        embed_model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ):
        self.embedder = TextEmbedder(dim=embedding_dim, model_name=embed_model_name)
        self.local_store = LocalVectorStore(db_path=db_path, snapshot_path=snapshot_path)
        self.backend = str(backend or "faiss").strip().lower()
        self.qdrant: Optional[QdrantVectorStore] = None
        if self.backend == "qdrant" and qdrant_url.strip():
            self.qdrant = QdrantVectorStore(url=qdrant_url.strip(), collection=collection, dim=embedding_dim)

    async def close(self) -> None:
        if self.qdrant:
            await self.qdrant.close()

    @staticmethod
    def _build_text(title: str, artist: str, payload: dict[str, Any]) -> str:
        bits = [title, artist]
        tags = payload.get("tags") or []
        if isinstance(tags, list):
            bits.extend(str(x) for x in tags[:8])
        catalog = payload.get("source_catalog")
        if isinstance(catalog, str) and catalog.strip():
            bits.append(catalog)
        return " | ".join([str(x).strip() for x in bits if str(x).strip()])

    async def upsert_track(
        self,
        *,
        track_id: str,
        title: str,
        artist: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        payload = dict(payload or {})
        payload.setdefault("title", title)
        payload.setdefault("artist", artist)

        text = self._build_text(title, artist, payload)
        vector = await self.embedder.embed(text)
        item = IndexedTrack(
            track_id=track_id,
            title=title,
            artist=artist,
            vector=vector,
            payload=payload,
        )

        await self.local_store.upsert(item)
        if self.qdrant:
            ok = await self.qdrant.upsert(item)
            if not ok:
                logger.debug("Qdrant upsert failed for %s; kept local vector store", track_id)

    async def search_by_text(self, *, text: str, limit: int = 50) -> list[dict[str, Any]]:
        if not text.strip():
            return []

        vector = await self.embedder.embed(text)

        if self.qdrant:
            qdrant_hits = await self.qdrant.search(vector, limit)
            if qdrant_hits:
                return qdrant_hits

        return await self.local_store.search(vector, limit)
