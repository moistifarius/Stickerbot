"""SQLite-backed state: packs, dedup set, added stickers and a persistent job queue."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
    name   TEXT PRIMARY KEY,
    idx    INTEGER NOT NULL,
    title  TEXT NOT NULL DEFAULT '',
    count  INTEGER NOT NULL DEFAULT 0,
    full   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS stickers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_uid   TEXT NOT NULL,
    pack_name    TEXT NOT NULL,
    pack_file_id TEXT NOT NULL,
    pack_uid     TEXT NOT NULL,
    added_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    payload   TEXT NOT NULL,
    attempts  INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL DEFAULT 0,
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    # -- kv -----------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    # -- seen ---------------------------------------------------------------
    def is_seen(self, uid: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone() is not None

    def mark_seen(self, *uids: str) -> None:
        self.db.executemany("INSERT OR IGNORE INTO seen(uid) VALUES(?)", [(u,) for u in uids if u])

    def unsee(self, *uids: str) -> None:
        self.db.executemany("DELETE FROM seen WHERE uid=?", [(u,) for u in uids if u])

    def seen_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    # -- packs --------------------------------------------------------------
    def upsert_pack(self, name: str, idx: int, title: str, count: int, full: bool) -> None:
        self.db.execute(
            "INSERT INTO packs(name,idx,title,count,full) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET idx=excluded.idx, title=excluded.title, "
            "count=excluded.count, full=excluded.full",
            (name, idx, title, count, int(full)),
        )

    def set_pack_count(self, name: str, count: int, limit: int) -> None:
        self.db.execute(
            "UPDATE packs SET count=?, full=? WHERE name=?", (count, int(count >= limit), name)
        )

    def mark_full(self, name: str) -> None:
        self.db.execute("UPDATE packs SET full=1 WHERE name=?", (name,))

    def drop_pack(self, name: str) -> None:
        self.db.execute("DELETE FROM packs WHERE name=?", (name,))

    def current_pack(self, limit: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM packs WHERE full=0 AND count<? ORDER BY idx DESC LIMIT 1", (limit,)
        ).fetchone()

    def packs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM packs ORDER BY idx").fetchall()

    def max_index(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(idx),0) FROM packs").fetchone()[0]

    def pack_names(self) -> set[str]:
        return {r["name"] for r in self.db.execute("SELECT name FROM packs")}

    # -- added stickers -----------------------------------------------------
    def record_sticker(self, source_uid: str, pack: str, file_id: str, pack_uid: str) -> int:
        cur = self.db.execute(
            "INSERT INTO stickers(source_uid,pack_name,pack_file_id,pack_uid,added_at) VALUES(?,?,?,?,?)",
            (source_uid, pack, file_id, pack_uid, time.time()),
        )
        return cur.lastrowid

    def last_sticker(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM stickers ORDER BY id DESC LIMIT 1").fetchone()

    def sticker_by_pack_uid(self, pack_uid: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM stickers WHERE pack_uid=? ORDER BY id DESC LIMIT 1", (pack_uid,)
        ).fetchone()

    def forget_sticker(self, row_id: int) -> None:
        self.db.execute("DELETE FROM stickers WHERE id=?", (row_id,))

    def total_added(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]

    # -- jobs ---------------------------------------------------------------
    def enqueue(self, payload: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO jobs(payload,created) VALUES(?,?)", (json.dumps(payload), time.time())
        )
        return cur.lastrowid

    def next_job(self, now: float | None = None) -> tuple[int, dict, int] | None:
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT * FROM jobs WHERE not_before<=? ORDER BY id LIMIT 1", (now,)
        ).fetchone()
        if not row:
            return None
        return row["id"], json.loads(row["payload"]), row["attempts"]

    def defer_job(self, job_id: int, delay: float) -> None:
        self.db.execute(
            "UPDATE jobs SET attempts=attempts+1, not_before=? WHERE id=?",
            (time.time() + delay, job_id),
        )

    def finish_job(self, job_id: int) -> None:
        self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def queue_size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
