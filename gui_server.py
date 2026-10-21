"""多用户看板服务：每个访客用自己的到梦空间账号登录，数据各存各的。

启动：
    python gui_server.py --no-open            # 本机测试
    DM_PORT=8770 python gui_server.py         # 服务器

路由：
    GET  /                     有会话 -> 看板；无会话 -> 登录页
    POST /api/login            {account, password} 登录（没有就注册）
    POST /api/logout           退出
    GET  /api/me               当前用户
    GET  /api/state            本用户的活动与状态
    POST /api/refresh          抓一轮（手动刷新）
    POST /api/token            重新登录换 token（会踢掉手机端）
    GET  /api/activity?aid=    活动详情

原则：服务端单点登录，重新登录会把该账号手机端踢下线，
所以**拿到 token 后绝不主动重登**，只在用户点按钮时做。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from dmclient import DreamClient  # noqa: E402
from monitor import Monitor  # noqa: E402
from users import Users  # noqa: E402

UI_DIR = ROOT / "ui"
DATA_DIR = ROOT / "data"
PORT = 8770
COOKIE = "dms"

SLIM_KEYS = ("aid", "name", "state", "status_raw", "quota", "joined", "capacity",
             "category", "organizer", "sign_end", "sign_start", "act_start", "credit")
DETAIL_KEYS = ("activityId", "activityName", "statusText", "joinNum", "joinmaxnum",
               "joindate", "startdate", "startdateDate", "enddate", "schoolname",
               "address", "catalog2name", "levelText", "joinrangeText", "joinWayDesc",
               "countdownText", "canJoin", "ableJoinFlag", "score")


def _slim(a: dict) -> dict:
    out = {k: a.get(k) for k in SLIM_KEYS}
    out["aid"] = str(out["aid"] or "")
    out.pop("raw", None)
    return out


class Hub:
    """一个用户一个 Hub：自己的 Monitor、自己的库、自己的日志。"""

    def __init__(self, user: dict, base_cfg: dict) -> None:
        self.uid = str(user["uid"])
        self.account = str(user.get("account") or "")
        self.name = str(user.get("name") or "")
        self.school = str(user.get("school") or "")
        self.college = str(user.get("college") or "")
        self.student_id = str(user.get("student_id") or "")
        self.phone = str(user.get("phone") or "")
        self.avatar = str(user.get("avatar") or "")
        self.score: dict | None = None
        self._score_job = False
        self._load_score()
        self.lock = threading.Lock()
        self.busy = False
        self.last_sync: float | None = None
        self.sent = 0
        self.logs: list[dict] = []
        self.items: list[dict] = []
        self.places: dict[str, str] = {}
        self._place_job = False
        self.token_job: dict | None = None
        self.enriching = False

        cfg = copy.deepcopy(base_cfg)
        api = dict(cfg.get("api") or {})
        api.update(uid=self.uid, account=self.account,
                   password=USERS.password_of(self.uid), token=str(user.get("token") or ""))
        api["auto_relogin"] = False          # 铁律：不自动重登，避免踢手机
        cfg["api"] = api
        cfg["db"] = USERS.db_for(self.uid)   # 每人一个库
        cfg["notify"] = {}                   # v2 不做通知
        cfg["on_token"] = self._save_token
        self.cfg = cfg
        self.mon = Monitor(cfg)
        self.mon.on_list = self._on_list
        self.log("工作台已就绪")
        self._first = threading.Thread(target=self._boot, daemon=True)
        self._first.start()

    def _boot(self) -> None:
        """启动就抓第一轮。

        不再单独预热：预热本身也要打一轮请求，反而把首屏时间往后推；
        抓取过程中线程池里的连接自然就热了，第二轮开始每页只要 0.2 秒。
        """
        self.tick()

    # ---------------------------------------------------------------- 内部
    def _on_list(self, acts: list[dict]) -> None:
        """列表阶段先回一版数据：名额/地点可能还空着，界面不用干等。

        逐页推送时会在旧数据的基础上增量合并，已经有名额/地点的不会被抹掉。
        """
        with self.lock:
            old = {it["aid"]: it for it in self.items}
            merged = []
            for a in acts:
                slim = _slim(a)
                prev = old.get(slim["aid"])
                if prev:
                    for k in ("quota", "limit", "address", "sign_end", "untilText"):
                        if slim.get(k) in (None, "", 0) and prev.get(k) not in (None, "", 0):
                            slim[k] = prev[k]
                merged.append(slim)
            self.items = merged
            self.enriching = True

    def _save_token(self, uid: str, token: str) -> None:
        USERS.set_token(uid, token)
        self.cfg.setdefault("api", {}).update(token=token, uid=uid)

    def log(self, msg: str) -> None:
        with self.lock:
            self.logs.append({"t": time.time(), "m": msg})
            del self.logs[:-120]

    # ---------------------------------------------------------------- 一轮
    def tick(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        try:
            sent, text = self.mon.tick()
            if sent < 0:
                self.log(f"本轮未成功：{text}")
                return False
            acts = list(self.mon.last_acts)
            with self.lock:
                if acts:
                    self.items = [_slim(a) for a in acts]
                self.sent = sent if sent > 0 else 0
                self.last_sync = time.time()
                self.enriching = False
            self.log(f"抓取 {len(acts)} 条")
            self._fill_places()   # 纯缓存查询，很快，直接同步做
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"本轮失败：{type(e).__name__}: {e}")
            return False
        finally:
            self.busy = False
            self.enriching = False

    def _fill_places(self) -> None:
        """地点补齐：只查共享缓存，不再打网络。

        fetch_all 已经并发拉过详情并写进 DetailCache，这里直接取，
        取不到就留空——绝不为了一条地址再串行打几十个请求。
        """
        if self._place_job:
            return
        self._place_job = True
        try:
            with self.lock:
                need = [i["aid"] for i in self.items if i["aid"] not in self.places]
            if not need:
                return
            cache = getattr(self.mon, "cache", None)
            hits = cache.get_many(need) if cache else {}
            for aid in need:
                det = hits.get(aid) or {}
                self.places[aid] = det.get("address") or ""
            filled = sum(1 for v in self.places.values() if v)
            self.log(f"地点补齐 {filled}/{len(self.places)} 条（本次查 {len(need)}，缓存命中 {len(hits)}）")
        finally:
            self._place_job = False

    # ---------------------------------------------------------------- token
    def refresh_token(self) -> None:
        if self.token_job and self.token_job.get("status") == "running":
            return

        def run() -> None:
            self.token_job = {"status": "running", "msg": "正在用账号密码登录…"}
            self.log("开始重新登录")
            ok, msg = self.mon.relogin()
            if ok:
                self.token_job = {"status": "ok", "msg": msg}
                self.log(msg)
                self.tick()
            else:
                self.token_job = {"status": "fail", "msg": msg}
                self.log(f"重新登录失败：{msg}")

        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- 积分
    def _score_path(self) -> Path:
        d = ROOT / "data" / "users"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.uid}_score.json"

    def _load_score(self) -> None:
        try:
            self.score = json.loads(self._score_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.score = None

    def scan_score(self) -> None:
        """后台统计积分（按活动记录的分数累加），结果落盘缓存。"""
        if self._score_job:
            return
        self._score_job = True

        def run() -> None:
            try:
                res = self.mon.client.scan_score()
                res["at"] = time.time()
                self.score = res
                self._score_path().write_text(
                    json.dumps(res, ensure_ascii=False), encoding="utf-8")
                self.log(f"积分读取完成：{res.get('text') or res['total']} 分（{res.get('source','')}）")
            except Exception as e:  # noqa: BLE001
                self.log(f"积分统计失败：{e}")
            finally:
                self._score_job = False

        threading.Thread(target=run, daemon=True).start()

    def profile(self) -> dict:
        phone = self.phone
        if len(phone) >= 7:
            phone = f"{phone[:3]}****{phone[-4:]}"
        return {
            "uid": self.uid,
            "account": self.account,
            "name": self.name,
            "school": self.school,
            "college": self.college,
            "student_id": self.student_id,
            "phone": phone,
            "avatar": self.avatar,
            "score": self.score,
            "score_scanning": self._score_job,
            "score_source": "App「我的」页 /v3/user/mine_homepage",
        }

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict:
        items = list(self.items)
        cnt: dict[str, int] = {}
        for it in items:
            cnt[it["state"]] = cnt.get(it["state"], 0) + 1
        live = [it for it in items if it["state"] != "ended"]
        for it in live:
            it["address"] = self.places.get(it["aid"], "") or it.get("place", "")
        live.sort(key=lambda x: (0 if x["state"] in ("open", "full") else 1,
                                 x["sign_end"] or 0))
        return {
            "busy": self.busy,
            "enriching": self.enriching,
            "progress": dict(self.mon.progress or {}),
            "last_sync": self.last_sync,
            "stale_secs": (time.time() - self.last_sync) if self.last_sync else None,
            "token": str((self.cfg.get("api") or {}).get("token", "")),
            "me": {"uid": self.uid, "account": self.account,
                   "name": self.name, "school": self.school,
                   "avatar": self.avatar},
            "stats": {
                "total": len(items),
                "live": len(live),
                "open": cnt.get("open", 0) + cnt.get("full", 0),
                "soon": cnt.get("soon", 0),
                "running": cnt.get("running", 0),
                "ended": cnt.get("ended", 0),
                "sent": self.sent,
            },
            "items": live,
            "logs": list(reversed(self.logs[-24:])),
            "token_job": self.token_job,
            "auth": {
                "stale": bool(getattr(self.mon, "auth_stale", False)),
                "msg": getattr(self.mon, "auth_msg", ""),
                "account": self.account,
                "uid": self.uid,
                "auto": False,
            },
        }


# ------------------------------------------------------------------ 全局
BASE_CFG: dict = {}
USERS: Users | None = None
HUBS: dict[str, Hub] = {}
HUBS_LOCK = threading.Lock()


def get_hub(uid: str) -> Hub | None:
    """按 uid 取（或建）用户的 Hub。"""
    with HUBS_LOCK:
        hub = HUBS.get(uid)
        if hub:
            return hub
        user = USERS.get(uid) if USERS else None
        if not user:
            return None
        hub = Hub(user, BASE_CFG)
        HUBS[uid] = hub
        return hub


def _api_login(account: str, password: str) -> dict:
    """用账号密码打到梦空间换 uid/token（会踢掉手机端）。"""
    base = (BASE_CFG.get("api") or {})
    cli = DreamClient({
        "version": str(base.get("version") or "4.9.3"),
        "uid": "",
        "token": "",
    })
    return cli.login_account(account, password)


def _profile_of(info: dict) -> dict:
    return {
        "college": str(info.get("college") or ""),
        "student_id": str(info.get("student_id") or ""),
        "phone": str(info.get("phone") or ""),
        "avatar": str(info.get("avatar") or ""),
    }


def _token_alive(uid: str, token: str) -> bool:
    base = (BASE_CFG.get("api") or {})
    cli = DreamClient({"version": str(base.get("version") or "4.9.3"),
                       "uid": uid, "token": token})
    try:
        ok, _ = cli.ping()
        return bool(ok)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ HTTP
class Handler(SimpleHTTPRequestHandler):
    server_version = "DMDesk/2.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI_DIR), **kw)

    def log_message(self, fmt, *args):
        return

    def end_headers(self):  # noqa: N802
        """HTML 一律不缓存，静态资源按版本号长缓存。

        SimpleHTTPRequestHandler 不带 cache-control，浏览器会按
        Last-Modified 做启发式缓存；登录后跳转 "/" 可能命中缓存里的
        login.html，看起来就像“登录了但没跳转”。
        """
        if not getattr(self, "_cache_done", False):
            path = urlparse(self.path).path.lower()
            if path.endswith((".html", "/")) or path == "":
                self.send_header("cache-control", "no-store")
            else:
                self.send_header("cache-control", "public, max-age=604800")
        super().end_headers()

    # ---------------------------------------------------------------- 工具
    def _json(self, obj, code: int = 200, cookie: str | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self._cache_done = True
        if cookie:
            self.send_header("set-cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    def _sid(self) -> str:
        raw = self.headers.get("cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _uid(self) -> str | None:
        if USERS is None:
            return None
        return USERS.session_uid(self._sid())

    def _need_login(self) -> bool:
        if self._uid():
            return False
        self._json({"error": "未登录", "need_login": True}, 401)
        return True

    # ---------------------------------------------------------------- GET
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        uid = self._uid()

        if u.path in ("/", "/index.html"):
            self.path = "/index.html" if uid else "/login.html"
            return super().do_GET()

        if u.path == "/api/me":
            if self._need_login():
                return
            hub = get_hub(uid)
            self._json({"uid": uid, "hub": bool(hub),
                        "name": hub.name if hub else "",
                        "school": hub.school if hub else ""})
            return

        if u.path == "/api/state":
            if self._need_login():
                return
            hub = get_hub(uid)
            if not hub:
                self._json({"error": "用户不存在"}, 404)
                return
            self._json(hub.state())
            return

        if u.path == "/api/profile":
            if self._need_login():
                return
            hub = get_hub(uid)
            if not hub:
                self._json({"error": "用户不存在"}, 404)
                return
            data = hub.profile()
            if not data["score"] and not data["score_scanning"]:
                hub.scan_score()
                data["score_scanning"] = True
            self._json(data)
            return

        if u.path == "/api/activity":
            if self._need_login():
                return
            hub = get_hub(uid)
            aid = (parse_qs(u.query).get("aid") or [""])[0]
            if not (hub and aid):
                self._json({"error": "missing"}, 400)
                return
            try:
                det = hub.mon.client.detail(aid) or {}
                self._json({k: det.get(k) for k in DETAIL_KEYS})
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return

        super().do_GET()

    # ---------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        body = self._body()

        if u.path == "/api/login":
            account = str(body.get("account") or "").strip()
            password = str(body.get("password") or "")
            if not (account and password):
                self._json({"ok": False, "error": "请填写账号和密码"}, 400)
                return
            force = bool(body.get("force"))
            user = USERS.by_account(account) if USERS else None
            if user:
                if not USERS.check_password(user, password):
                    self._json({"ok": False, "error": "密码不对"}, 400)
                    return
                uid = str(user["uid"])
                token = str(user.get("token") or "")
                kicked = False
                if force or not _token_alive(uid, token):
                    try:
                        info = _api_login(account, password)
                    except Exception as e:  # noqa: BLE001
                        self._json({"ok": False, "error": f"登录失败：{e}"}, 401)
                        return
                    uid, token, kicked = str(info["uid"]), str(info["token"]), True
                    USERS.upsert(uid=uid, account=account, password=password,
                                 token=token, name=info.get("name", ""),
                                 school=info.get("school", ""),
                                 profile=_profile_of(info))
                else:
                    USERS.upsert(uid=uid, account=account, password=password,
                                 token=token, name=user.get("name", ""),
                                 school=user.get("school", ""))
            else:
                try:
                    info = _api_login(account, password)
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": f"登录失败：{e}"}, 401)
                    return
                uid, token, kicked = str(info["uid"]), str(info["token"]), True
                USERS.upsert(uid=uid, account=account, password=password,
                             token=token, name=info.get("name", ""),
                             school=info.get("school", ""),
                             profile=_profile_of(info))

            sid = USERS.new_session(uid)
            # 登录后必须丢掉旧 Hub：它内存里还揣着失效的 token 和 stale 标记
            if USERS is not None and "HUBS" in globals():
                HUBS.pop(uid, None)
            get_hub(uid)
            self._json(
                {"ok": True, "uid": uid, "kicked_phone": kicked,
                 "note": "已登录（手机端被挤下线）" if kicked else "已登录"},
                200, cookie=f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            return

        if u.path == "/api/logout":
            USERS.drop_session(self._sid())
            self._json({"ok": True}, 200, cookie=f"{COOKIE}=; Path=/; Max-Age=0")
            return

        uid = self._uid()
        if not uid:
            self._json({"error": "未登录", "need_login": True}, 401)
            return
        hub = get_hub(uid)
        if not hub:
            self._json({"error": "用户不存在"}, 404)
            return

        if u.path == "/api/refresh":
            threading.Thread(target=hub.tick, daemon=True).start()
            self._json({"ok": True, "started": True})
        elif u.path == "/api/score":
            hub.scan_score()
            self._json({"ok": True, "scanning": True})
        elif u.path == "/api/token":
            hub.refresh_token()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会让两个进程同时绑同一端口，必须关掉；
    # Linux 上关掉它则会导致重启时撞上 TIME_WAIT（Address already in use）。
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def main() -> int:
    global BASE_CFG, USERS
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("DM_PORT") or os.environ.get("PORT") or PORT))
    ap.add_argument("--host", default=os.environ.get("DM_HOST", "0.0.0.0"))
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    BASE_CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    USERS = Users(DATA_DIR)

    # 老配置里的账号自动迁移成第一个用户，省得重新登录
    api = BASE_CFG.get("api") or {}
    if USERS.count() == 0 and api.get("account") and api.get("password") and api.get("uid"):
        USERS.upsert(uid=str(api["uid"]), account=str(api["account"]),
                     password=str(api["password"]), token=str(api.get("token") or ""))
        print(f"已迁移老配置账号：{api['account']}")

    httpd = Server((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"工作台已启动：http://{shown}:{args.port}/")
    print(f"监听 {args.host}:{args.port} · 多用户（各自登录）· 手动刷新 · 用户数 {USERS.count()}")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://{shown}:{args.port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
