"""活动详情的跨用户共享缓存。

一个学校的所有人看到的活动详情是同一份数据，没必要每个用户都重新拉一遍。
放在 data/shared_details.db，按 activityId 存详情 JSON，带 TTL。

只用标准库，线程安全（每个线程一个连接）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS details (
    aid  TEXT PRIMARY KEY,
    ts   REAL NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_details_ts ON details(ts);
"""


class DetailCache:
    def __init__(self, path: str = "data/shared_details.db", ttl: int = 900) -> None:
        self.path = path
        self.ttl = int(ttl)
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------- 内部
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------- 读写
    def get(self, aid: str) -> dict | None:
        try:
            row = self._conn().execute(
                "SELECT ts, body FROM details WHERE aid = ?", (str(aid),)
            ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        ts, body = row
        if time.time() - float(ts) > self.ttl:
            return None
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return None

    def get_many(self, aids) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = [str(a) for a in aids]
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self._conn().execute(
                    f"SELECT aid, ts, body FROM details WHERE aid IN ({marks})", chunk
                ).fetchall()
            except Exception:  # noqa: BLE001
                continue
            now = time.time()
            for aid, ts, body in rows:
                if now - float(ts) <= self.ttl:
                    try:
                        out[aid] = json.loads(body)
                    except Exception:  # noqa: BLE001
                        pass
        return out

    def drop(self, aids) -> None:
        """删掉若干条缓存（报名/取消后强制下次重新拉）。"""
        aids = [str(a) for a in aids]
        if not aids:
            return
        c = self._conn()
        c.executemany("DELETE FROM details WHERE aid=?", [(a,) for a in aids])
        c.commit()

    def put_many(self, items: dict[str, dict]) -> None:
        if not items:
            return
        now = time.time()
        rows = [(str(k), now, json.dumps(v, ensure_ascii=False)) for k, v in items.items()]
        try:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO details(aid, ts, body) VALUES(?,?,?) "
                "ON CONFLICT(aid) DO UPDATE SET ts=excluded.ts, body=excluded.body",
                rows,
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> tuple[int, int]:
        """返回 (条数, 未过期条数)。"""
        try:
            total = self._conn().execute("SELECT COUNT(*) FROM details").fetchone()[0]
            alive = self._conn().execute(
                "SELECT COUNT(*) FROM details WHERE ts > ?", (time.time() - self.ttl,)
            ).fetchone()[0]
            return int(total), int(alive)
        except Exception:  # noqa: BLE001
            return 0, 0


if __name__ == "__main__":
    import tempfile

    p = os.path.join(tempfile.gettempdir(), "dm_detail_cache_test.db")
    if os.path.exists(p):
        os.remove(p)
    dc = DetailCache(p, ttl=60)
    dc.put_many({"1": {"a": 1}, "2": {"b": 2}})
    assert dc.get("1") == {"a": 1}, dc.get("1")
    assert set(dc.get_many(["1", "2", "3"])) == {"1", "2"}
    assert dc.get("3") is None
    assert dc.stats() == (2, 2)
    dc.put_many({"1": {"a": 9}})
    assert dc.get("1") == {"a": 9}
    print("detail_cache selftest: OK")
