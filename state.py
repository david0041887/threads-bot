"""
state.py
SQLite-backed persistent state for the Threads bot.

取代 main.py 裡原本的記憶體 dict / set，重啟後狀態仍保留。
檔案位置：Railway Volume 掛載的 /app/data/state.db
"""

import os
import json
import sqlite3
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.environ.get("STATE_DB_PATH") or "/app/data/state.db"


def _conn() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS pending_jobs (
                job_id      TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_replies (
                reply_job_id TEXT PRIMARY KEY,
                payload      TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_ids (
                kind        TEXT NOT NULL,
                id          TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                PRIMARY KEY (kind, id)
            );
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
    _migrate_legacy_json()
    logger.info(f"state.py: DB ready at {_DB_PATH}")


def _migrate_legacy_json() -> None:
    f = Path("processed_reply_ids.json")
    if not f.exists():
        return
    try:
        ids = json.loads(f.read_text())
    except Exception:
        return
    if not ids:
        return
    now = int(time.time())
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO processed_ids (kind, id, created_at) VALUES ('reply', ?, ?)",
            [(i, now) for i in ids],
        )
    logger.info(f"state.py: migrated {len(ids)} reply IDs from JSON")


# ─── pending jobs (每日草稿) ───────────────────────────────────

def save_pending_job(job_id: str, payload: dict, status: str = "pending") -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pending_jobs VALUES (?, ?, ?, ?)",
            (job_id, json.dumps(payload, ensure_ascii=False), status, int(time.time())),
        )


def get_pending_job(job_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT payload, status FROM pending_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if not row:
        return None
    j = json.loads(row["payload"])
    j["status"] = row["status"]
    return j


def update_job(job_id: str, status: str, extra: Optional[dict] = None) -> None:
    with _conn() as c:
        row = c.execute(
            "SELECT payload FROM pending_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return
        payload = json.loads(row["payload"])
        if extra:
            payload.update(extra)
        c.execute(
            "UPDATE pending_jobs SET payload = ?, status = ? WHERE job_id = ?",
            (json.dumps(payload, ensure_ascii=False), status, job_id),
        )


def list_pending_jobs() -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT job_id, payload, status FROM pending_jobs ORDER BY created_at DESC"
        ).fetchall()
    return {
        r["job_id"]: {**json.loads(r["payload"]), "status": r["status"]}
        for r in rows
    }


def latest_pending_job_id() -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT job_id FROM pending_jobs WHERE status = 'pending' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row["job_id"] if row else None


def latest_pending_reply_id() -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT reply_job_id FROM pending_replies WHERE status = 'pending' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row["reply_job_id"] if row else None


# ─── pending replies（留言審核） ───────────────────────────────

def save_pending_reply(reply_job_id: str, payload: dict, status: str = "pending") -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO pending_replies VALUES (?, ?, ?, ?)",
            (reply_job_id, json.dumps(payload, ensure_ascii=False), status, int(time.time())),
        )


def get_pending_reply(reply_job_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT payload, status FROM pending_replies WHERE reply_job_id = ?",
            (reply_job_id,),
        ).fetchone()
    if not row:
        return None
    j = json.loads(row["payload"])
    j["status"] = row["status"]
    return j


def update_reply(reply_job_id: str, status: str, extra: Optional[dict] = None) -> None:
    with _conn() as c:
        row = c.execute(
            "SELECT payload FROM pending_replies WHERE reply_job_id = ?", (reply_job_id,)
        ).fetchone()
        if not row:
            return
        payload = json.loads(row["payload"])
        if extra:
            payload.update(extra)
        c.execute(
            "UPDATE pending_replies SET payload = ?, status = ? WHERE reply_job_id = ?",
            (json.dumps(payload, ensure_ascii=False), status, reply_job_id),
        )


# ─── processed IDs（去重：海巡 shortcode、留言 id） ───────────

def mark_processed(kind: str, id_: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO processed_ids (kind, id, created_at) VALUES (?, ?, ?)",
            (kind, id_, int(time.time())),
        )


def is_processed(kind: str, id_: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM processed_ids WHERE kind = ? AND id = ?", (kind, id_)
        ).fetchone()
    return row is not None


# ─── KV（daily_proactive_count 等） ───────────────────────────

def get_kv(key: str, default=None):
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_kv(key: str, value) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ─── TTL 清理 ─────────────────────────────────────────────────

def cleanup_old_jobs(hours: int = 24) -> int:
    cutoff = int(time.time()) - hours * 3600
    total = 0
    with _conn() as c:
        for table in ("pending_jobs", "pending_replies"):
            cur = c.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
            total += cur.rowcount
    if total:
        logger.info(f"state.py: cleaned up {total} old rows (> {hours}h)")
    return total


def cleanup_old_processed_ids(days: int = 90) -> int:
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        cur = c.execute("DELETE FROM processed_ids WHERE created_at < ?", (cutoff,))
    if cur.rowcount:
        logger.info(f"state.py: cleaned up {cur.rowcount} processed_ids (> {days}d)")
    return cur.rowcount
