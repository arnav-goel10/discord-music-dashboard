#!/usr/bin/env python3
"""Offline evaluation for AI DJ v2 recommendation audits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from typing import Any

from utils.dj_models import hit_rate_at_k, mrr_at_k, ndcg_at_k, skip_at_1


def _db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _safe_json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(raw))
    except Exception:
        return fallback


def _track_id(title: str, artist: str, *, isrc: str = "") -> str:
    normalized_isrc = str(isrc or "").strip().upper()
    if normalized_isrc:
        return f"isrc:{normalized_isrc}"
    raw = f"{str(artist).strip().lower()}::{str(title).strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _load_catalog_lookup(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    try:
        rows = conn.execute(
            """
            SELECT track_id, title, artist
            FROM dj_track_catalog_v2
            """
        ).fetchall()
    except Exception:
        return {}
    for row in rows:
        title = str(row["title"] or "").strip().lower()
        artist = str(row["artist"] or "").strip().lower()
        track_id = str(row["track_id"] or "").strip()
        if not title or not artist or not track_id:
            continue
        key = (artist, title)
        existing = lookup.get(key)
        if existing and existing != track_id:
            ambiguous.add(key)
            lookup.pop(key, None)
            continue
        if key not in ambiguous:
            lookup[key] = track_id
    return lookup


def _recommended_id(item: Any, *, catalog_lookup: dict[tuple[str, str], str] | None = None) -> str:
    if not isinstance(item, dict):
        return ""
    track_id = str(item.get("track_id") or "").strip()
    if track_id:
        return track_id
    title = str(item.get("title") or "").strip()
    artist = str(item.get("artist") or "").strip()
    if not title or not artist:
        return ""
    catalog_track_id = (catalog_lookup or {}).get((artist.lower(), title.lower()))
    if catalog_track_id:
        return catalog_track_id
    return _track_id(title, artist, isrc=str(item.get("isrc") or ""))


def _normalized_artist(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("artist") or "").strip().lower()


def _rank_feature(item: Any, key: str) -> float:
    if not isinstance(item, dict):
        return 0.0
    features = item.get("rank_features")
    if not isinstance(features, dict):
        return 0.0
    try:
        return float(features.get(key, 0.0) or 0.0)
    except Exception:
        return 0.0


def _artist_diversity_at_k(ranked: list[Any], *, k: int) -> float:
    shortlist = ranked[: max(1, k)]
    artists = [_normalized_artist(item) for item in shortlist]
    artists = [artist for artist in artists if artist]
    if not artists:
        return 0.0
    return len(set(artists)) / len(artists)


def _artist_repetition_rate_at_k(ranked: list[Any], *, k: int) -> float:
    diversity = _artist_diversity_at_k(ranked, k=k)
    if diversity <= 0.0:
        return 0.0
    return 1.0 - diversity


def _mean_feature_at_k(ranked: list[Any], key: str, *, k: int) -> float:
    shortlist = ranked[: max(1, k)]
    if not shortlist:
        return 0.0
    values = [_rank_feature(item, key) for item in shortlist]
    return sum(values) / len(values)


def _long_tail_share_at_k(ranked: list[Any], *, k: int, novelty_floor: float = 0.55) -> float:
    shortlist = ranked[: max(1, k)]
    if not shortlist:
        return 0.0
    hits = 0
    for item in shortlist:
        if _rank_feature(item, "novelty") >= novelty_floor:
            hits += 1
    return hits / len(shortlist)


def _early_skip_rate_at_1(events: list[Any], *, severity_floor: float = 0.55) -> float:
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "") != "skip":
            continue
        try:
            severity = float(event.get("skip_severity", 0.0) or 0.0)
        except Exception:
            severity = 0.0
        return 1.0 if severity >= severity_floor else 0.0
    return 0.0


def evaluate(db_path: str, *, k: int) -> dict[str, float]:
    with _db_connect(db_path) as conn:
        catalog_lookup = _load_catalog_lookup(conn)
        try:
            live_catalog_size = int(conn.execute("SELECT COUNT(*) FROM dj_track_catalog_v2").fetchone()[0] or 0)
        except Exception:
            live_catalog_size = 0
        rows = conn.execute(
            """
            SELECT ranked_json, chosen_track_id, outcome_json
            FROM dj_recs_audit_v2
            ORDER BY created_at DESC
            LIMIT 50000
            """
        ).fetchall()

    n = 0
    hr = 0.0
    ndcg = 0.0
    mrr = 0.0
    skip1 = 0.0
    early_skip1 = 0.0
    artist_diversity = 0.0
    artist_repetition = 0.0
    novelty = 0.0
    long_tail_share = 0.0
    unique_recommended_ids: set[str] = set()
    audited_catalog_ids: set[str] = set()
    for row in rows:
        ranked = _safe_json(row["ranked_json"], [])
        chosen = str(row["chosen_track_id"] or "").strip()
        outcome = _safe_json(row["outcome_json"], {})
        events = outcome.get("events") if isinstance(outcome, dict) else []
        events = events if isinstance(events, list) else []

        rec_ids: list[str] = []
        for item in ranked if isinstance(ranked, list) else []:
            rec_id = _recommended_id(item, catalog_lookup=catalog_lookup)
            if rec_id:
                rec_ids.append(rec_id)
                audited_catalog_ids.add(rec_id)

        target = ""
        if chosen:
            target = chosen
        elif rec_ids:
            target = rec_ids[0]

        if not rec_ids or not target:
            continue

        was_skip = any(isinstance(ev, dict) and str(ev.get("event_type")) == "skip" for ev in events)

        n += 1
        hr += hit_rate_at_k(rec_ids, target, k=k)
        ndcg += ndcg_at_k(rec_ids, target, k=k)
        mrr += mrr_at_k(rec_ids, target, k=k)
        skip1 += skip_at_1(was_skip)
        early_skip1 += _early_skip_rate_at_1(events)
        artist_diversity += _artist_diversity_at_k(ranked, k=k)
        artist_repetition += _artist_repetition_rate_at_k(ranked, k=k)
        novelty += _mean_feature_at_k(ranked, "novelty", k=k)
        long_tail_share += _long_tail_share_at_k(ranked, k=k)
        unique_recommended_ids.update(rec_ids[: max(1, k)])

    if n == 0:
        return {
            "samples": 0,
            "k": k,
            "catalog_size": 0,
            "live_catalog_size": live_catalog_size,
            "hit_rate_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "mrr_at_k": 0.0,
            "skip_at_1": 0.0,
            "early_skip_rate_at_1": 0.0,
            "artist_diversity_at_k": 0.0,
            "artist_repetition_rate_at_k": 0.0,
            "mean_novelty_at_k": 0.0,
            "long_tail_share_at_k": 0.0,
            "catalog_coverage_at_k": 0.0,
        }

    return {
        "samples": n,
        "k": k,
        "catalog_size": len(audited_catalog_ids),
        "live_catalog_size": live_catalog_size,
        "hit_rate_at_k": hr / n,
        "ndcg_at_k": ndcg / n,
        "mrr_at_k": mrr / n,
        "skip_at_1": skip1 / n,
        "early_skip_rate_at_1": early_skip1 / n,
        "artist_diversity_at_k": artist_diversity / n,
        "artist_repetition_rate_at_k": artist_repetition / n,
        "mean_novelty_at_k": novelty / n,
        "long_tail_share_at_k": long_tail_share / n,
        "catalog_coverage_at_k": (
            len(unique_recommended_ids) / len(audited_catalog_ids) if audited_catalog_ids else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate AI DJ v2 offline metrics from audit logs.")
    parser.add_argument("--db", default=os.path.join("data", "music_bot.db"), help="Path to sqlite db")
    parser.add_argument("--k", type=int, default=10, help="Top-k cutoff for ranking metrics")
    args = parser.parse_args()

    metrics = evaluate(args.db, k=max(1, args.k))
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
