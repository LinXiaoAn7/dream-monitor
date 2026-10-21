"""store —— SQLite 持久化：活动快照、状态事件、通知去重。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    aid        TEXT PRIMARY KEY,
    name       TEXT,
    state      TEXT,            -- 归一化状态: open/full/soon/running/ended/unknown
    quota      INTEGER,         -- 剩余名额, -1 = 不限/未知
    raw        TEXT,            -- 原始 JSON
    first_seen REAL,
    last_seen  REAL,
    updated    REAL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    aid      TEXT,
    name     TEXT,
    kind     TEXT,              -- new / reopen / state / quota
    detail   TEXT,
    ts       REAL
);
CREATE TABLE IF NOT EXISTS notified (
    aid   TEXT,
    kind  TEXT,
    ts    REAL,
    PRIMARY KEY (aid, kind)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
"""


class Store:
    def __init__(self, path: str = "data/dm.db"):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        # GUI 会在工作线程里用同一个连接，所以关掉线程归属检查
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------- 活动
    def get(self, aid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM activities WHERE aid=?", (aid,)).fetchone()

    def upsert(self, aid: str, name: str, state: str, quota: int, raw: Any) -> None:
        now = time.time()
        payload = json.dumps(raw, ensure_ascii=False)
        row = self.get(aid)
        if row is None:
            self.db.execute(
                "INSERT INTO activities(aid,name,state,quota,raw,first_seen,last_seen,updated)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (aid, name, state, quota, payload, now, now, now))
        else:
            self.db.execute(
                "UPDATE activities SET name=?,state=?,quota=?,raw=?,last_seen=?,updated=?"
                " WHERE aid=?",
                (name, state, quota, payload, now, now, aid))
        self.db.commit()

    def snapshot(self) -> dict[str, tuple[str, int]]:
        """一次查出全部活动的 (state, quota)，避免逐条 SELECT。"""
        return {r["aid"]: (r["state"], r["quota"])
                for r in self.db.execute("SELECT aid,state,quota FROM activities")}

    def upsert_many(self, rows) -> None:
        """批量写（rows = [(aid,name,state,quota,raw)]），只 commit 一次。"""
        if not rows:
            return
        now = time.time()
        payloads = [(r[0], r[1], r[2], r[3], json.dumps(r[4], ensure_ascii=False), now)
                    for r in rows]
        self.db.executemany(
            "INSERT INTO activities(aid,name,state,quota,raw,first_seen,last_seen,updated)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(aid) DO UPDATE SET name=excluded.name, state=excluded.state,"
            " quota=excluded.quota, raw=excluded.raw, last_seen=excluded.last_seen,"
            " updated=excluded.updated",
            [p0 + (p0[5], p0[5]) for p0 in payloads])
        self.db.commit()

    def all_ids(self) -> set[str]:
        return {r["aid"] for r in self.db.execute("SELECT aid FROM activities")}

    # ------------------------------------------------------------- 事件
    def log_event(self, aid: str, name: str, kind: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO events(aid,name,kind,detail,ts) VALUES(?,?,?,?,?)",
            (aid, name, kind, detail, time.time()))
        self.db.commit()

    def recent_events(self, limit: int = 40) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)))

    # ------------------------------------------------------------- 去重
    def was_notified(self, aid: str, kind: str) -> bool:
        r = self.db.execute(
            "SELECT 1 FROM notified WHERE aid=? AND kind=?", (aid, kind)).fetchone()
        return r is not None

    def mark_notified(self, aid: str, kind: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO notified(aid,kind,ts) VALUES(?,?,?)",
            (aid, kind, time.time()))
        self.db.commit()

    def reset_notified(self, aid: str | None = None) -> None:
        if aid:
            self.db.execute("DELETE FROM notified WHERE aid=?", (aid,))
        else:
            self.db.execute("DELETE FROM notified")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
