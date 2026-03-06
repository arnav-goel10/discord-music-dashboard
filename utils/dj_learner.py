"""Online learning engine for AI DJ v3.

Self-improving recommendation system with three components:
1. BPR (Bayesian Personalized Ranking) — learns user/item embeddings from implicit feedback
2. Thompson Sampling — principled explore/exploit via Beta posterior on track success
3. Sequential scorer — lightweight attention over recent session for next-track prediction

All state is persisted to SQLite so the system resumes learning across bot restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import struct
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_DB_PATH = os.getenv("DJ_DB_PATH", os.path.join(_DATA_DIR, "music_bot.db"))

# Embedding dimension — small enough for CPU, large enough for signal
_EMB_DIM = 32


# ---------------------------------------------------------------------------
# Helpers: pack/unpack float vectors to/from SQLite BLOB
# ---------------------------------------------------------------------------

def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vec(blob: bytes, dim: int) -> list[float]:
    if not blob or len(blob) != dim * 4:
        return [0.0] * dim
    return list(struct.unpack(f"{dim}f", blob))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _add_scaled(a: list[float], b: list[float], scale: float) -> list[float]:
    return [x + scale * y for x, y in zip(a, b)]


def _l2_norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _normalize(v: list[float]) -> list[float]:
    n = _l2_norm(v)
    if n < 1e-12:
        return v
    return [x / n for x in v]


def _random_init(dim: int) -> list[float]:
    """Small random initialization."""
    return [random.gauss(0, 0.02) for _ in range(dim)]


def _sigmoid(x: float) -> float:
    if x > 20:
        return 1.0
    if x < -20:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# OnlineLearner: the self-improving engine
# ---------------------------------------------------------------------------

class OnlineLearner:
    """Incremental learning engine that improves from every play/skip."""

    def __init__(
        self,
        *,
        db_path: str = _DB_PATH,
        embedding_dim: int = _EMB_DIM,
        bpr_lr: float = 0.01,
        bpr_reg: float = 0.001,
        bpr_steps_per_event: int = 3,
        ts_prior_alpha: float = 1.0,
        ts_prior_beta: float = 1.0,
        seq_window: int = 10,
    ):
        self._db_path = db_path
        self._dim = embedding_dim
        self._bpr_lr = bpr_lr
        self._bpr_reg = bpr_reg
        self._bpr_steps = bpr_steps_per_event
        self._ts_alpha0 = ts_prior_alpha
        self._ts_beta0 = ts_prior_beta
        self._seq_window = seq_window
        self._interaction_count = 0
        self._ensure_tables()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

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
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS dj_embeddings (
                        entity_type TEXT NOT NULL,  -- 'guild' or 'track'
                        entity_id TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (entity_type, entity_id)
                    );

                    CREATE TABLE IF NOT EXISTS dj_bandit_arms (
                        guild_id INTEGER NOT NULL,
                        track_id TEXT NOT NULL,
                        alpha REAL NOT NULL DEFAULT 1.0,
                        beta REAL NOT NULL DEFAULT 1.0,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (guild_id, track_id)
                    );

                    CREATE TABLE IF NOT EXISTS dj_reward_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        track_id TEXT NOT NULL,
                        reward REAL NOT NULL,
                        features_json TEXT DEFAULT '{}',
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dj_reward_log_guild
                        ON dj_reward_log(guild_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS dj_reward_model (
                        model_id TEXT PRIMARY KEY DEFAULT 'default',
                        weights_json TEXT NOT NULL,
                        bias REAL NOT NULL DEFAULT 0.0,
                        n_samples INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                """)
        except Exception as exc:
            logger.error("Failed creating online learner tables: %s", exc)

    # ------------------------------------------------------------------
    # Embedding CRUD
    # ------------------------------------------------------------------

    def _get_embedding(self, entity_type: str, entity_id: str) -> Optional[list[float]]:
        try:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT embedding FROM dj_embeddings WHERE entity_type = ? AND entity_id = ?",
                    (entity_type, entity_id),
                ).fetchone()
            if row and row["embedding"]:
                return _unpack_vec(row["embedding"], self._dim)
        except Exception:
            pass
        return None

    def _set_embedding(self, conn: sqlite3.Connection, entity_type: str, entity_id: str, vec: list[float]) -> None:
        conn.execute(
            """INSERT INTO dj_embeddings (entity_type, entity_id, embedding, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                   embedding = excluded.embedding, updated_at = excluded.updated_at""",
            (entity_type, entity_id, _pack_vec(vec), time.time()),
        )

    def _get_or_init_embedding(self, entity_type: str, entity_id: str) -> list[float]:
        vec = self._get_embedding(entity_type, entity_id)
        if vec is None:
            vec = _random_init(self._dim)
            try:
                with self._db_connect() as conn:
                    self._set_embedding(conn, entity_type, entity_id, vec)
                    conn.commit()
            except Exception:
                pass
        return vec

    # ------------------------------------------------------------------
    # 1. BPR: Bayesian Personalized Ranking
    # ------------------------------------------------------------------

    async def bpr_update(
        self,
        guild_id: int,
        positive_track_id: str,
        negative_track_ids: list[str],
    ) -> None:
        """Run BPR SGD steps: guild embedding should prefer positive over negatives."""
        if not negative_track_ids:
            return

        def _step() -> None:
            guild_key = str(guild_id)
            guild_emb = self._get_or_init_embedding("guild", guild_key)
            pos_emb = self._get_or_init_embedding("track", positive_track_id)

            with self._db_connect() as conn:
                for _ in range(self._bpr_steps):
                    neg_id = random.choice(negative_track_ids)
                    neg_emb = self._get_or_init_embedding("track", neg_id)

                    # Score difference
                    score_pos = _dot(guild_emb, pos_emb)
                    score_neg = _dot(guild_emb, neg_emb)
                    diff = score_pos - score_neg

                    # Gradient of -log(sigmoid(diff))
                    grad = _sigmoid(diff) - 1.0  # negative when diff is small (good)

                    # SGD update with L2 regularization
                    lr = self._bpr_lr
                    reg = self._bpr_reg
                    guild_emb = _add_scaled(
                        _add_scaled(guild_emb, pos_emb, -lr * grad),
                        guild_emb, -lr * reg,
                    )
                    # Clip for stability
                    guild_emb = [max(-3.0, min(3.0, x)) for x in guild_emb]

                    pos_emb = _add_scaled(
                        _add_scaled(pos_emb, guild_emb, -lr * grad),
                        pos_emb, -lr * reg,
                    )
                    neg_emb = _add_scaled(
                        _add_scaled(neg_emb, guild_emb, lr * grad),
                        neg_emb, -lr * reg,
                    )

                self._set_embedding(conn, "guild", guild_key, guild_emb)
                self._set_embedding(conn, "track", positive_track_id, pos_emb)
                for neg_id in set(negative_track_ids):
                    neg_emb_final = self._get_or_init_embedding("track", neg_id)
                    self._set_embedding(conn, "track", neg_id, neg_emb_final)
                conn.commit()

        try:
            await asyncio.to_thread(_step)
        except Exception as exc:
            logger.debug("BPR update error: %s", exc)

    def bpr_score(self, guild_id: int, track_id: str) -> float:
        """Dot product of guild and track embeddings = affinity score."""
        guild_emb = self._get_embedding("guild", str(guild_id))
        track_emb = self._get_embedding("track", track_id)
        if guild_emb is None or track_emb is None:
            return 0.0
        return _dot(guild_emb, track_emb)

    # ------------------------------------------------------------------
    # 2. Thompson Sampling
    # ------------------------------------------------------------------

    def ts_sample(self, guild_id: int, track_id: str) -> float:
        """Sample from Beta posterior for this (guild, track) arm."""
        try:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT alpha, beta FROM dj_bandit_arms WHERE guild_id = ? AND track_id = ?",
                    (guild_id, track_id),
                ).fetchone()
            if row:
                a, b = float(row["alpha"]), float(row["beta"])
            else:
                a, b = self._ts_alpha0, self._ts_beta0
            return random.betavariate(max(0.01, a), max(0.01, b))
        except Exception:
            return 0.5

    async def ts_update(
        self,
        guild_id: int,
        track_id: str,
        reward: float,
    ) -> None:
        """Update Beta posterior: reward > 0 → increment alpha, else increment beta."""
        def _update() -> None:
            try:
                with self._db_connect() as conn:
                    # Clamp reward contribution to avoid runaway posteriors
                    alpha_inc = min(1.0, max(0.0, reward))
                    beta_inc = min(1.0, max(0.0, 1.0 - reward))
                    conn.execute(
                        """INSERT INTO dj_bandit_arms (guild_id, track_id, alpha, beta, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(guild_id, track_id) DO UPDATE SET
                               alpha = alpha + excluded.alpha - 1.0,
                               beta = beta + excluded.beta - 1.0,
                               updated_at = excluded.updated_at""",
                        (guild_id, track_id, self._ts_alpha0 + alpha_inc,
                         self._ts_beta0 + beta_inc, time.time()),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("TS update error: %s", exc)

        await asyncio.to_thread(_update)

    # ------------------------------------------------------------------
    # 3. Sequential attention scorer
    # ------------------------------------------------------------------

    def sequential_score(
        self,
        recent_track_ids: list[str],
        candidate_track_id: str,
    ) -> float:
        """Score a candidate based on similarity to recent session context.

        Uses exponentially-decayed attention over recent track embeddings,
        then cosine similarity with candidate.
        """
        if not recent_track_ids:
            return 0.0

        window = recent_track_ids[-self._seq_window:]
        candidate_emb = self._get_embedding("track", candidate_track_id)
        if candidate_emb is None:
            return 0.0

        # Build attention-weighted session embedding
        session_emb = [0.0] * self._dim
        total_weight = 0.0
        for i, tid in enumerate(window):
            track_emb = self._get_embedding("track", tid)
            if track_emb is None:
                continue
            # Exponential recency: most recent track gets highest weight
            w = math.exp(0.3 * (i - len(window) + 1))  # e^0 for last, decays backward
            session_emb = _add_scaled(session_emb, track_emb, w)
            total_weight += w

        if total_weight < 1e-12:
            return 0.0

        session_emb = [x / total_weight for x in session_emb]

        # Cosine similarity
        norm_s = _l2_norm(session_emb)
        norm_c = _l2_norm(candidate_emb)
        if norm_s < 1e-12 or norm_c < 1e-12:
            return 0.0
        return _dot(session_emb, candidate_emb) / (norm_s * norm_c)

    # ------------------------------------------------------------------
    # 4. Online reward model (logistic regression)
    # ------------------------------------------------------------------

    def compute_reward(
        self,
        *,
        listened_ms: int,
        duration_ms: int,
        was_skipped: bool,
    ) -> float:
        """Compute a continuous reward in [-1.0, +1.0] from implicit signals."""
        if duration_ms <= 0:
            return -0.5 if was_skipped else 0.3

        ratio = min(1.0, max(0.0, listened_ms / duration_ms))

        if was_skipped:
            # Early skip = strong negative, late skip ≈ neutral
            if ratio < 0.15:
                return -1.0
            elif ratio < 0.30:
                return -0.7
            elif ratio < 0.50:
                return -0.3
            elif ratio < 0.80:
                return 0.0  # neutral — listened to most of it
            else:
                return 0.3  # late skip ≈ completion
        else:
            # Completion: reward scales with how much was played
            if ratio >= 0.90:
                return 1.0
            elif ratio >= 0.70:
                return 0.7
            elif ratio >= 0.50:
                return 0.3
            else:
                return 0.0

    async def record_reward(
        self,
        guild_id: int,
        track_id: str,
        reward: float,
        features: Optional[dict[str, float]] = None,
    ) -> None:
        """Log reward and trigger incremental learning updates."""
        self._interaction_count += 1

        def _log() -> None:
            try:
                with self._db_connect() as conn:
                    conn.execute(
                        """INSERT INTO dj_reward_log (guild_id, track_id, reward, features_json, created_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (guild_id, track_id, reward, json.dumps(features or {}), time.time()),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("Reward log error: %s", exc)

        await asyncio.to_thread(_log)

        # Thompson Sampling update (every event)
        await self.ts_update(guild_id, track_id, max(0.0, (reward + 1.0) / 2.0))

        # BPR update every 5 interactions — sample negatives from guild history
        if self._interaction_count % 5 == 0 and reward > 0:
            negatives = await self._sample_negative_tracks(guild_id, track_id, count=5)
            if negatives:
                await self.bpr_update(guild_id, track_id, negatives)

        # Update reward model every 20 interactions
        if self._interaction_count % 20 == 0:
            await self._update_reward_model(guild_id)

    async def _sample_negative_tracks(
        self, guild_id: int, positive_id: str, count: int = 5
    ) -> list[str]:
        """Sample tracks the guild has skipped or not interacted with."""
        def _query() -> list[str]:
            try:
                with self._db_connect() as conn:
                    # Get recently skipped tracks as hard negatives
                    rows = conn.execute(
                        """SELECT DISTINCT track_id FROM (
                            SELECT track_id FROM dj_events_v2
                            WHERE guild_id = ? AND event_type = 'skip'
                            ORDER BY ts DESC LIMIT 50
                        ) WHERE track_id != ?""",
                        (guild_id, positive_id),
                    ).fetchall()
                    skipped = [str(r["track_id"]) for r in rows]

                    # Also sample random tracks from catalog as soft negatives
                    catalog_rows = conn.execute(
                        """SELECT track_id FROM dj_track_catalog_v2
                           WHERE track_id != ?
                           ORDER BY RANDOM() LIMIT ?""",
                        (positive_id, max(1, count * 2)),
                    ).fetchall()
                    catalog = [str(r["track_id"]) for r in catalog_rows]

                    # Mix: 60% hard negatives, 40% random
                    hard_count = max(1, int(count * 0.6))
                    soft_count = count - hard_count
                    result = []
                    if skipped:
                        result.extend(random.sample(skipped, min(hard_count, len(skipped))))
                    if catalog:
                        result.extend(random.sample(catalog, min(soft_count, len(catalog))))
                    return result[:count]
            except Exception:
                return []

        return await asyncio.to_thread(_query)

    async def _update_reward_model(self, guild_id: int) -> None:
        """Incremental logistic regression on recent rewards to learn what predicts success."""
        def _fit() -> None:
            try:
                with self._db_connect() as conn:
                    rows = conn.execute(
                        """SELECT reward, features_json FROM dj_reward_log
                           WHERE guild_id = ? ORDER BY created_at DESC LIMIT 200""",
                        (guild_id,),
                    ).fetchall()
                if len(rows) < 20:
                    return

                # Load current model
                model_row = None
                with self._db_connect() as conn:
                    model_row = conn.execute(
                        "SELECT weights_json, bias, n_samples FROM dj_reward_model WHERE model_id = ?",
                        (f"guild_{guild_id}",),
                    ).fetchone()

                feature_names = [
                    "skip_risk", "completion_prob", "novelty", "session_fit",
                    "artist_affinity", "semantic_similarity", "sequential_affinity",
                    "recency_penalty", "artist_repetition_penalty",
                ]

                if model_row:
                    weights = json.loads(model_row["weights_json"])
                    bias = float(model_row["bias"])
                    n_prev = int(model_row["n_samples"])
                else:
                    weights = {f: 0.0 for f in feature_names}
                    bias = 0.0
                    n_prev = 0

                # Online SGD on recent samples
                lr = max(0.001, 0.01 / (1.0 + n_prev / 100.0))  # decaying learning rate
                for row in rows:
                    features = json.loads(row["features_json"] or "{}")
                    target = 1.0 if float(row["reward"]) > 0 else 0.0

                    # Linear prediction
                    z = bias
                    for fname in feature_names:
                        z += weights.get(fname, 0.0) * float(features.get(fname, 0.0))

                    pred = _sigmoid(z)
                    error = pred - target  # gradient of log loss

                    # Update weights
                    for fname in feature_names:
                        fval = float(features.get(fname, 0.0))
                        weights[fname] = weights.get(fname, 0.0) - lr * error * fval
                    bias -= lr * error

                # Persist
                with self._db_connect() as conn:
                    conn.execute(
                        """INSERT INTO dj_reward_model (model_id, weights_json, bias, n_samples, updated_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(model_id) DO UPDATE SET
                               weights_json = excluded.weights_json,
                               bias = excluded.bias,
                               n_samples = excluded.n_samples,
                               updated_at = excluded.updated_at""",
                        (f"guild_{guild_id}", json.dumps(weights), bias,
                         n_prev + len(rows), time.time()),
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("Reward model update error: %s", exc)

        await asyncio.to_thread(_fit)

    def predict_reward(self, guild_id: int, features: dict[str, float]) -> float:
        """Predict reward probability using the learned model for this guild."""
        try:
            with self._db_connect() as conn:
                row = conn.execute(
                    "SELECT weights_json, bias FROM dj_reward_model WHERE model_id = ?",
                    (f"guild_{guild_id}",),
                ).fetchone()
            if not row:
                return 0.5

            weights = json.loads(row["weights_json"])
            bias = float(row["bias"])
            z = bias
            for fname, w in weights.items():
                z += w * float(features.get(fname, 0.0))
            return _sigmoid(z)
        except Exception:
            return 0.5

    # ------------------------------------------------------------------
    # Combined scoring for ranking
    # ------------------------------------------------------------------

    def score_candidate(
        self,
        guild_id: int,
        track_id: str,
        recent_track_ids: list[str],
        rank_features: Optional[dict[str, float]] = None,
    ) -> dict[str, float]:
        """Score a candidate using all learned signals. Returns individual scores."""
        bpr = self.bpr_score(guild_id, track_id)
        ts = self.ts_sample(guild_id, track_id)
        seq = self.sequential_score(recent_track_ids, track_id)
        predicted = self.predict_reward(guild_id, rank_features or {})

        return {
            "bpr_affinity": bpr,
            "ts_exploration": ts,
            "sequential_fit": seq,
            "predicted_reward": predicted,
        }


# Singleton
learner = OnlineLearner()
