"""Modeling and ranking utilities for AI DJ v2.

This module keeps algorithmic building blocks decoupled from Discord/Lavalink code.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RankedCandidate:
    track_id: str
    title: str
    artist: str
    reason: str = ""
    source: str = ""
    candidate_sources: list[str] = field(default_factory=list)
    retrieval_score: float = 0.0
    rank_score: float = 0.0
    exploration_bonus: float = 0.0
    exploration_flag: bool = False
    rank_features: dict[str, float] = field(default_factory=dict)


class FeatureBuilder:
    """Builds model-ready features for ranking candidates."""

    def build(
        self,
        *,
        session: Any,
        candidates: list[dict[str, Any]],
        guild_stats: dict[str, dict[str, float]],
        item_stats: dict[str, dict[str, float]],
        now_ts: float,
    ) -> list[RankedCandidate]:
        seed_artists = {str(seed.artist).lower() for seed in getattr(session, "seed_tracks", [])[-5:]}
        recent_artists = {
            str(item).lower() for item in getattr(session, "recent_artist_history", [])[-10:]
        }

        ranked: list[RankedCandidate] = []
        for cand in candidates:
            track_id = str(cand.get("track_id") or "").strip()
            title = str(cand.get("title") or "").strip()
            artist = str(cand.get("artist") or "").strip()
            if not (track_id and title and artist):
                continue

            track_key = f"{artist.lower()} - {title.lower()}"
            gstats = guild_stats.get(track_key, {})
            istats = item_stats.get(track_id, {})

            plays_global = float(istats.get("plays", 0.0) or 0.0)
            skips_global = float(istats.get("skips", 0.0) or 0.0)
            completions_global = float(istats.get("completions", 0.0) or 0.0)

            # Bayesian-smoothed behavior priors.
            skip_risk = (skips_global + 1.0) / (plays_global + 3.0)
            completion_prob = (completions_global + 1.0) / (plays_global + 3.0)

            # Blend guild-local stats when available.
            g_skip = float(gstats.get("skip_rate", 0.0) or 0.0)
            g_comp = float(gstats.get("completion_rate", 0.0) or 0.0)
            if gstats:
                skip_risk = 0.62 * skip_risk + 0.38 * g_skip
                completion_prob = 0.62 * completion_prob + 0.38 * g_comp

            last_played = float(gstats.get("last_played", 0.0) or 0.0)
            recency_penalty = 0.0
            if last_played > 0:
                hours_since = max(0.0, (now_ts - last_played) / 3600.0)
                recency_penalty = math.exp(-hours_since / 12.0)  # smooth 12h half-life

            novelty = max(0.35, 1.0 / (1.0 + math.log1p(max(0.0, plays_global))))
            artist_affinity = 1.0 if artist.lower() in seed_artists else 0.2
            # Time-decayed artist repetition: most recent = strongest penalty
            artist_lower = artist.lower()
            artist_repetition_penalty = 0.0
            if artist_lower in recent_artists:
                # Find position from the end (0 = most recent)
                for _ri, _ra in enumerate(reversed(
                    [str(a).lower() for a in getattr(session, "recent_artist_history", [])[-10:]]
                )):
                    if _ra == artist_lower:
                        artist_repetition_penalty = math.exp(-_ri / 5.0)
                        break

            semantic_score = float(cand.get("semantic_score", 0.0) or 0.0)
            sequential_score = float(cand.get("sequential_score", 0.0) or 0.0)
            graph_score = float(cand.get("graph_score", 0.0) or 0.0)
            rule_score = float(cand.get("rule_score", 0.0) or 0.0)
            retrieval_score = float(cand.get("retrieval_score", 0.0) or 0.0)

            session_fit = (
                0.42 * semantic_score
                + 0.26 * sequential_score
                + 0.22 * graph_score
                + 0.10 * rule_score
            )

            resolver_conf = float(cand.get("resolver_confidence", 0.55) or 0.55)
            uncertainty = 1.0 / math.sqrt(plays_global + 1.0)

            ranked.append(
                RankedCandidate(
                    track_id=track_id,
                    title=title,
                    artist=artist,
                    reason=str(cand.get("reason") or ""),
                    source=str(cand.get("source") or ""),
                    candidate_sources=list(cand.get("candidate_sources") or []),
                    retrieval_score=retrieval_score,
                    rank_features={
                        "semantic_similarity": semantic_score,
                        "sequential_affinity": sequential_score,
                        "co_listen_strength": graph_score,
                        "rule_affinity": rule_score,
                        "session_fit": session_fit,
                        "skip_risk": skip_risk,
                        "completion_prob": completion_prob,
                        "novelty": novelty,
                        "artist_affinity": artist_affinity,
                        "recency_penalty": recency_penalty,
                        "artist_repetition_penalty": artist_repetition_penalty,
                        "resolver_confidence": resolver_conf,
                        "uncertainty": uncertainty,
                    },
                )
            )

        return ranked


class ContextualRanker:
    """Weighted utility ranker for retention + discovery."""

    def __init__(self, *, weights: dict[str, float] | None = None):
        default = {
            "completion_prob": 0.33,
            "skip_risk": -0.28,
            "novelty": 0.19,
            "session_fit": 0.29,
            "artist_affinity": 0.11,
            "semantic_similarity": 0.16,
            "sequential_affinity": 0.14,
            "co_listen_strength": 0.12,
            "rule_affinity": 0.09,
            "recency_penalty": -0.17,
            "artist_repetition_penalty": -0.12,
            "resolver_confidence": 0.08,
            "retrieval_score": 0.21,
        }
        if weights:
            default.update({k: float(v) for k, v in weights.items()})
        self.weights = default

    def score(self, ranked: list[RankedCandidate]) -> list[RankedCandidate]:
        for row in ranked:
            f = row.rank_features
            utility = (
                self.weights["completion_prob"] * f.get("completion_prob", 0.0)
                + self.weights["skip_risk"] * f.get("skip_risk", 0.0)
                + self.weights["novelty"] * f.get("novelty", 0.0)
                + self.weights["session_fit"] * f.get("session_fit", 0.0)
                + self.weights["artist_affinity"] * f.get("artist_affinity", 0.0)
                + self.weights["semantic_similarity"] * f.get("semantic_similarity", 0.0)
                + self.weights["sequential_affinity"] * f.get("sequential_affinity", 0.0)
                + self.weights["co_listen_strength"] * f.get("co_listen_strength", 0.0)
                + self.weights["rule_affinity"] * f.get("rule_affinity", 0.0)
                + self.weights["recency_penalty"] * f.get("recency_penalty", 0.0)
                + self.weights["artist_repetition_penalty"] * f.get("artist_repetition_penalty", 0.0)
                + self.weights["resolver_confidence"] * f.get("resolver_confidence", 0.0)
                + self.weights["retrieval_score"] * row.retrieval_score
            )
            row.rank_score = utility * 100.0

        ranked.sort(key=lambda item: item.rank_score, reverse=True)
        return ranked


class LinUCBBanditPolicy:
    """Lightweight contextual exploration policy (LinUCB-inspired)."""

    def apply(
        self,
        ranked: list[RankedCandidate],
        *,
        base_alpha: float,
        session_skip_streak: int,
        session_completion_streak: int,
        source_entropy: float,
    ) -> list[RankedCandidate]:
        if not ranked:
            return ranked

        skip_boost = 1.0 + min(1.5, max(0, session_skip_streak) * 0.25)
        completion_dampen = 0.72 if session_completion_streak >= 3 else 1.0
        entropy_boost = 1.0 + min(0.8, max(0.0, source_entropy) * 0.6)
        alpha = max(0.0, base_alpha) * skip_boost * completion_dampen * entropy_boost

        for row in ranked:
            uncertainty = float(row.rank_features.get("uncertainty", 0.0) or 0.0)
            novelty = float(row.rank_features.get("novelty", 0.0) or 0.0)
            skip_risk = float(row.rank_features.get("skip_risk", 0.0) or 0.0)
            bonus = alpha * uncertainty
            row.exploration_bonus = bonus
            row.rank_score += bonus
            row.exploration_flag = bool(bonus >= 0.12 and novelty >= 0.45 and skip_risk < 0.55)

        ranked.sort(key=lambda item: item.rank_score, reverse=True)
        return ranked


class SlateReranker:
    """MMR reranker with artist and duplicate controls."""

    def __init__(self, *, mmr_lambda: float = 0.65):
        self.mmr_lambda = max(0.05, min(0.95, float(mmr_lambda)))

    @staticmethod
    def _normalize_title(raw: str) -> str:
        text = str(raw or "").lower()
        text = re.sub(r"[^\w\s]", " ", text)
        text = " ".join(text.split())
        return text

    @classmethod
    def _title_similarity(cls, a: str, b: str) -> float:
        left = set(cls._normalize_title(a).split())
        right = set(cls._normalize_title(b).split())
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    def rerank(self, ranked: list[RankedCandidate], *, max_results: int) -> list[RankedCandidate]:
        if not ranked:
            return []

        pool = list(ranked)
        selected: list[RankedCandidate] = []
        artist_counts: dict[str, int] = {}
        top_score = pool[0].rank_score
        floor = top_score - 35.0

        while pool and len(selected) < max_results:
            best_idx = -1
            best_score = float("-inf")

            for idx, candidate in enumerate(pool):
                if candidate.rank_score < floor:
                    continue

                artist_key = candidate.artist.lower()
                if len(selected) < 5 and artist_counts.get(artist_key, 0) >= 1:
                    continue

                duplicate = False
                for prev in selected[-20:]:
                    if self._title_similarity(prev.title, candidate.title) >= 0.88:
                        duplicate = True
                        break
                if duplicate:
                    continue

                redundancy = 0.0
                for prev in selected:
                    redundancy = max(
                        redundancy,
                        self._title_similarity(prev.title, candidate.title) * 0.7
                        + (0.3 if prev.artist.lower() == candidate.artist.lower() else 0.0),
                    )

                mmr = self.mmr_lambda * candidate.rank_score - (1.0 - self.mmr_lambda) * (redundancy * 100.0)
                if mmr > best_score:
                    best_score = mmr
                    best_idx = idx

            if best_idx < 0:
                break

            picked = pool.pop(best_idx)
            selected.append(picked)
            akey = picked.artist.lower()
            artist_counts[akey] = artist_counts.get(akey, 0) + 1

        # Fill from remaining by pure rank if constraints over-pruned.
        if len(selected) < max_results:
            selected_ids = {x.track_id for x in selected}
            for row in ranked:
                if row.track_id in selected_ids:
                    continue
                selected.append(row)
                if len(selected) >= max_results:
                    break

        return selected[:max_results]


def hit_rate_at_k(recommended_ids: list[str], target_id: str, k: int = 10) -> float:
    if not target_id:
        return 0.0
    shortlist = recommended_ids[: max(1, k)]
    return 1.0 if target_id in shortlist else 0.0


def ndcg_at_k(recommended_ids: list[str], target_id: str, k: int = 10) -> float:
    if not target_id:
        return 0.0
    shortlist = recommended_ids[: max(1, k)]
    try:
        rank = shortlist.index(target_id) + 1
    except ValueError:
        return 0.0
    return 1.0 / math.log2(rank + 1)


def mrr_at_k(recommended_ids: list[str], target_id: str, k: int = 10) -> float:
    if not target_id:
        return 0.0
    shortlist = recommended_ids[: max(1, k)]
    try:
        rank = shortlist.index(target_id) + 1
    except ValueError:
        return 0.0
    return 1.0 / rank


def skip_at_1(skipped_first: bool) -> float:
    return 1.0 if skipped_first else 0.0
