"""多用户存储：账号、加密密码、会话、每人独立的数据目录。

设计取舍（2026-10-21 与用户确认）：
    ① 密码加密存盘 —— 允许 token 失效后自动/手动续期，代价是服务器上有密文
    ② 每人一个独立库 —— data/users/<到梦空间uid>.db，互不干扰
    ③ 开放注册 —— 谁打开页面都能用自己的到梦空间账号登录

密码用 AES-256-CBC 加密，密钥放在 data/secret.key（首次启动自动生成，
权限 0600）。密钥与库分开存放：只拿到 users.db 无法解出密码。
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

import dmcrypto

SESSION_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid        TEXT PRIMARY KEY,
    account    TEXT UNIQUE NOT NULL,
    name       TEXT DEFAULT '',
    school     TEXT DEFAULT '',
    college    TEXT DEFAULT '',
    student_id TEXT DEFAULT '',
    phone      TEXT DEFAULT '',
    avatar     TEXT DEFAULT '',
    pwd_enc    TEXT NOT NULL,
    token      TEXT DEFAULT '',
    created    REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    sid     TEXT PRIMARY KEY,
    uid     TEXT NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);
"""


class Users:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "users.db"
        self.key = self._load_key()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """老库补列（v1 -> v2 加了学院/学号/手机号/头像）。"""
        have = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for col in ("college", "student_id", "phone", "avatar"):
            if col not in have:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT ''")

    # ------------------------------------------------------------------ 基础
    def _load_key(self) -> bytes:
        p = self.dir / "secret.key"
        if p.exists():
            raw = p.read_bytes()
            if len(raw) == 32:
                return raw
        raw = os.urandom(32)
        p.write_bytes(raw)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return raw

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    # -------------------------------------------------------------- 密码加解密
    def encrypt(self, plain: str) -> str:
        iv = os.urandom(16)
        ct = dmcrypto.aes_cbc_encrypt(self.key, iv, plain.encode("utf-8"))
        return f"{base64.b64encode(iv).decode()}:{base64.b64encode(ct).decode()}"

    def decrypt(self, blob: str) -> str:
        iv_b64, ct_b64 = blob.split(":", 1)
        iv = base64.b64decode(iv_b64)
        ct = base64.b64decode(ct_b64)
        return dmcrypto.aes_cbc_decrypt(self.key, iv, ct).decode("utf-8")

    # ------------------------------------------------------------------ 用户
    def upsert(self, *, uid: str, account: str, password: str,
               token: str, name: str = "", school: str = "",
               profile: dict | None = None) -> dict:
        now = time.time()
        p = dict(profile or {})
        college = str(p.get("college", ""))
        student_id = str(p.get("student_id", ""))
        phone = str(p.get("phone", ""))
        avatar = str(p.get("avatar", ""))
        with self._conn() as c:
            c.execute(
                "INSERT INTO users(uid,account,name,school,college,student_id,phone,avatar,"
                " pwd_enc,token,created,last_seen)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(uid) DO UPDATE SET account=excluded.account,"
                " name=excluded.name, school=excluded.school,"
                " college=CASE WHEN excluded.college<>'' THEN excluded.college ELSE users.college END,"
                " student_id=CASE WHEN excluded.student_id<>'' THEN excluded.student_id ELSE users.student_id END,"
                " phone=CASE WHEN excluded.phone<>'' THEN excluded.phone ELSE users.phone END,"
                " avatar=CASE WHEN excluded.avatar<>'' THEN excluded.avatar ELSE users.avatar END,"
                " pwd_enc=excluded.pwd_enc, token=excluded.token, last_seen=excluded.last_seen",
                (uid, account, name, school, college, student_id, phone, avatar,
                 self.encrypt(password), token, now, now),
            )
        return self.get(uid) or {}

    def get(self, uid: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        return dict(row) if row else None

    def by_account(self, account: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM users WHERE account=?", (account,)).fetchone()
        return dict(row) if row else None

    def check_password(self, user: dict, password: str) -> bool:
        try:
            return hmac.compare_digest(self.decrypt(user["pwd_enc"]), password)
        except Exception:  # noqa: BLE001
            return False

    def password_of(self, uid: str) -> str:
        u = self.get(uid) or {}
        return self.decrypt(u["pwd_enc"]) if u else ""

    def set_token(self, uid: str, token: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET token=?, last_seen=? WHERE uid=?",
                      (token, time.time(), uid))

    def touch(self, uid: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def db_for(self, uid: str) -> str:
        """每人一个活动库：data/users/<uid>.db，删用户就删这个文件。"""
        d = self.dir / "users"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{uid}.db")

    # ------------------------------------------------------------------ 会话
    def new_session(self, uid: str) -> str:
        sid = secrets.token_hex(24)
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO sessions(sid,uid,created,expires) VALUES(?,?,?,?)",
                      (sid, uid, now, now + SESSION_DAYS * 86400))
            c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        return sid

    def session_uid(self, sid: str) -> str | None:
        if not sid:
            return None
        with self._conn() as c:
            row = c.execute("SELECT uid,expires FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not row or row["expires"] < time.time():
            return None
        return str(row["uid"])

    def drop_session(self, sid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE sid=?", (sid,))


def _selftest() -> None:
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="dmusers_"))
    u = Users(d)
    rec = u.upsert(uid="62153844", account="18281107989", password="96287248Ly.",
                   token="abc", name="刘烨", school="四川职业技术学院")
    assert rec["uid"] == "62153844", rec
    assert u.get("62153844")["token"] == "abc"
    assert u.by_account("18281107989")["name"] == "刘烨"
    assert u.check_password(rec, "96287248Ly.") is True
    assert u.check_password(rec, "96287248ly.") is False
    assert u.password_of("62153844") == "96287248Ly."
    # 密文里不该出现明文
    raw = (d / "users.db").read_bytes()
    assert b"96287248" not in raw, "密码明文落盘了"
    sid = u.new_session("62153844")
    assert u.session_uid(sid) == "62153844"
    assert u.session_uid("bogus") is None
    u.drop_session(sid)
    assert u.session_uid(sid) is None
    print("users selftest: OK")


if __name__ == "__main__":
    _selftest()
