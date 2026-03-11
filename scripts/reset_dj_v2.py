#!/usr/bin/env python3
"""Reset AI DJ v2 learned state, caches, and audit history."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Any


DJ_TABLES = [
    "dj_embeddings",
    "dj_bandit_arms",
    "dj_reward_log",
    "dj_reward_model",
    "dj_events_v2",
    "dj_item_stats_v2",
    "dj_pair_cooccurrence_v2",
    "dj_recs_audit_v2",
    "dj_similar_cache",
    "dj_track_tags",
    "dj_listen_history",
    "dj_track_catalog_v2",
    "dj_track_vectors_v2",
]


def _db_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _stage_snapshot(snapshot_path: str) -> str:
    if not snapshot_path or not os.path.exists(snapshot_path):
        return ""
    staged_path = f"{snapshot_path}.resetting"
    suffix = 0
    while os.path.exists(staged_path):
        suffix += 1
        staged_path = f"{snapshot_path}.resetting.{suffix}"
    os.replace(snapshot_path, staged_path)
    return staged_path


def reset_dj_state(db_path: str, *, snapshot_path: str) -> dict[str, Any]:
    cleared: dict[str, int] = {}
    staged_snapshot_path = ""
    try:
        staged_snapshot_path = _stage_snapshot(snapshot_path)
        with _db_connect(db_path) as conn:
            existing = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            for table in DJ_TABLES:
                if table not in existing:
                    continue
                count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                conn.execute(f"DELETE FROM {table}")
                cleared[table] = count
            conn.commit()
    except Exception:
        if staged_snapshot_path and not os.path.exists(snapshot_path) and os.path.exists(staged_snapshot_path):
            try:
                os.replace(staged_snapshot_path, snapshot_path)
            except Exception:
                pass
        raise

    snapshot_removed = False
    if staged_snapshot_path and os.path.exists(staged_snapshot_path):
        try:
            os.remove(staged_snapshot_path)
            snapshot_removed = True
        except OSError:
            snapshot_removed = False

    return {
        "db_path": db_path,
        "snapshot_path": snapshot_path,
        "staged_snapshot_path": staged_snapshot_path,
        "snapshot_removed": snapshot_removed,
        "tables_cleared": cleared,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset AI DJ v2 state and caches.")
    parser.add_argument("--db", default=os.path.join("data", "music_bot.db"), help="Path to sqlite db")
    parser.add_argument(
        "--snapshot",
        default=os.path.join("data", "faiss_tracks_v2.index"),
        help="Path to the local vector snapshot placeholder",
    )
    args = parser.parse_args()

    summary = reset_dj_state(args.db, snapshot_path=args.snapshot)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
