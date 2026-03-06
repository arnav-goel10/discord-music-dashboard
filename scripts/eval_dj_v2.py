#!/usr/bin/env python3
"""Offline evaluation for AI DJ v2 recommendation audits."""

from __future__ import annotations

import argparse
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


def evaluate(db_path: str, *, k: int) -> dict[str, float]:
    with _db_connect(db_path) as conn:
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
    for row in rows:
        ranked = _safe_json(row["ranked_json"], [])
        chosen = str(row["chosen_track_id"] or "").strip()
        outcome = _safe_json(row["outcome_json"], {})
        events = outcome.get("events") if isinstance(outcome, dict) else []
        events = events if isinstance(events, list) else []

        rec_ids: list[str] = []
        for item in ranked if isinstance(ranked, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip().lower()
            artist = str(item.get("artist") or "").strip().lower()
            if title and artist:
                rec_ids.append(f"{artist}::{title}")

        target = ""
        if chosen:
            target = chosen
        elif rec_ids:
            # Fallback target heuristic when chosen_track_id was not persisted.
            target = rec_ids[0]

        if not rec_ids or not target:
            continue

        was_skip = any(isinstance(ev, dict) and str(ev.get("event_type")) == "skip" for ev in events)

        n += 1
        hr += hit_rate_at_k(rec_ids, target, k=k)
        ndcg += ndcg_at_k(rec_ids, target, k=k)
        mrr += mrr_at_k(rec_ids, target, k=k)
        skip1 += skip_at_1(was_skip)

    if n == 0:
        return {
            "samples": 0,
            "k": k,
            "hit_rate_at_k": 0.0,
            "ndcg_at_k": 0.0,
            "mrr_at_k": 0.0,
            "skip_at_1": 0.0,
        }

    return {
        "samples": n,
        "k": k,
        "hit_rate_at_k": hr / n,
        "ndcg_at_k": ndcg / n,
        "mrr_at_k": mrr / n,
        "skip_at_1": skip1 / n,
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
