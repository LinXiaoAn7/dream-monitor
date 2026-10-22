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

from dmclient import DreamClient, ApiError  # noqa: E402
from monitor import Monitor  # noqa: E402
import autosign
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
               "countdownText", "canJoin", "ableJoinFlag", "score",
               # 报名状态：抽屉里的「已报名 / 取消报名」全靠这几个字段
               "signUpId", "injoin", "cancel", "joinCanCancel", "joinWay", "joinStatus")


def _slim(a: dict) -> dict:
    out = {k: a.get(k) for k in SLIM_KEYS}
    out["aid"] = str(out["aid"] or "")
    out.pop("raw", None)
    return out


SIGNUP_MSG = {
    "2000011": "你已经报过这个活动了",
    "2000166": "名额已经满了（报名人数达到上限）",
    "2000000": "活动不存在或已下架",
    "11111": "报名表单还有必填项没填",
    "11002": "接口参数没被服务端接受",
    "-2000": "服务端处理报名时出错（多半是报名表单不合规或触发了风控）",
    "10005": "登录已失效，请重新登录",
    "10007": "登录已失效，请重新登录",
}


def _friendly_signup_error(code: str, msg: str, item: dict | None) -> str:
    """把接口错误码翻成用户能看懂的原因，尽量带上活动当前状态。"""
    base = SIGNUP_MSG.get(str(code)) or msg or f"未知错误（{code}）"
    if item:
        st = item.get("state")
        quota = item.get("quota")
        if st == "full" or quota == 0:
            return "这个活动名额已经满了"
        if st == "soon":
            return "这个活动还没开始报名"
        if st in ("running", "ended"):
            return "这个活动的报名已经结束了"
    if str(code) == "-2000":
        return (base + "。如果是审核制活动，"
                "待审核的人也算在名额里，可能是没名额了，去 App 里看一眼最准。")
    return base


def _build_signup_data(info, name: str) -> str:
    """把 /v2/signup/info 返回的表单模板转成提交用的 data 字符串。

    data 是 JSON 字符串（不是对象），字段结构沿用 App：
    {key, fullid, title, content, conent, notNull, notList, system}
    姓名字段用当前账号姓名预填，其余留空由用户自行到 App 补。
    """
    fields = None
    node = info.get("data") if isinstance(info, dict) and isinstance(info.get("data"), dict) else info
    if isinstance(node, dict):
        for k in ("list", "fields", "formList", "items", "data"):
            v = node.get(k)
            if isinstance(v, list):
                fields = v
                break
    if not fields:
        return "[]"
    out = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        title = str(f.get("title") or "")
        content = name if (name and "姓名" in title) else ""
        out.append({
            "key": f.get("key", ""),
            "fullid": str(f.get("fullid", "")),
            "title": title,
            "content": content,
            "conent": content,
            "notNull": str(f.get("notNull", "false")),
            "notList": str(f.get("notList", "false")),
            "system": f.get("system", 0),
        })
    return json.dumps(out, ensure_ascii=False)


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
        self.mine_at = 0.0
        self._mine_cache: list[dict] = []

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
    # ------------------------------------------------ 报名 / 取消报名
    def _patch_item(self, aid: str, detail: dict) -> None:
        """用最新详情就地更新内存里的那一条，界面无需整轮重抓。"""
        if not isinstance(detail, dict):
            return
        from rules import normalize
        n = normalize(detail, self.cfg.get("status_map"))
        sid = str(detail.get("signUpId", "0"))
        mine = sid not in ("", "0", "None", "null")   # injoin 不可信：未报名时它也是 True
        for it in self.items:
            if it["aid"] == str(aid):
                it["quota"] = n["quota"]
                it["joined"] = n["joined"]
                it["capacity"] = n["capacity"]
                it["can_join"] = bool(detail.get("canJoin"))
                it["can_cancel"] = bool(detail.get("cancel") or detail.get("joinCanCancel"))
                it["joined_by_me"] = mine
                it["signup_id"] = "" if not mine else sid

    def _after_signup(self, aid: str, msg: str) -> dict:
        """报名/取消之后：清缓存、重拉详情、就地更新、写日志。"""
        self.mine_at = 0.0
        self._mine_cache = []
        try:
            self.mon.cache.drop([aid])
            d = self.mon.client.detail(aid)
            self.mon.cache.put_many({str(aid): d})
            self._patch_item(aid, d)
        except Exception as e:  # noqa: BLE001
            self.log(f"报名后刷新详情失败：{e}")
        self.log(msg)
        return {"ok": True, "msg": msg}

    def signup(self, aid: str) -> dict:
        aid = str(aid or "").strip()
        if not aid:
            raise ValueError("缺少活动 ID")
        c = self.mon.client
        info = c.signup_form(aid)
        data = _build_signup_data(info, self.name or "")
        self.log(f"报名「{self._name_of(aid)}」表单 {len(data)} 字节：{data[:160]}")
        try:
            res = c.submit(aid, data=data)
            self.log(f"报名返回：{str(res)[:220]}")
        except ApiError as e:
            self.log(f"报名失败：[{e.code}] {e.msg}")
            if str(e.code) == "2000011":
                return self._after_signup(aid, f"你已经报过「{self._name_of(aid)}」了")
            item = next((it for it in self.items if it["aid"] == aid), None)
            raise ValueError(_friendly_signup_error(e.code, e.msg, item))
        sid = ""
        if isinstance(res, dict):
            d = res.get("data")
            if isinstance(d, dict):
                sid = str(d.get("signUpId") or "")
            elif d is not None:
                sid = str(d)
        return self._after_signup(aid, f"已报名「{self._name_of(aid)}」" + (f"（编号 {sid}）" if sid else ""))

    def cancel_signup(self, aid: str) -> dict:
        aid = str(aid or "").strip()
        item = next((it for it in self.items if it["aid"] == aid), None)
        if not item:
            raise ValueError("找不到这条活动")
        sid = str(item.get("signup_id") or "")
        if not sid:
            # 内存里没有 signUpId 就现拉一次详情
            d = self.mon.client.detail(aid)
            sid = str((d or {}).get("signUpId") or "")
            if sid in ("", "0", "None"):
                raise ValueError("这条活动没有报名记录，无法取消")
            self._patch_item(aid, d)
        self.log(f"取消报名「{self._name_of(aid)}」signUpId={sid}")
        try:
            res = self.mon.client.cancel(sid)
            self.log(f"取消返回：{str(res)[:220]}")
        except ApiError as e:
            self.log(f"取消失败：[{e.code}] {e.msg}")
            raise ValueError(_friendly_signup_error(e.code, e.msg, item))
        return self._after_signup(aid, f"已取消报名「{self._name_of(aid)}」")

    def _warm_mine(self) -> None:
        try:
            self.mine_items()
        except Exception:  # noqa: BLE001
            pass

    def mine_items(self, ttl: float = 60.0) -> list[dict]:
        """「我的活动」：我这个账号已经报名的活动。"""
        now = time.time()
        if self._mine_cache and now - self.mine_at < ttl:
            return self._mine_cache
        smap = {int(k): v for k, v in ((self.mon.status_map or {}) or {}).items()}
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 4):
            try:
                d = self.mon.client.mine(page)
            except ApiError as e:
                if str(e.code) in ("10005", "10007"):
                    self.log("我的活动：登录已失效")
                else:
                    self.log(f"我的活动抓取失败：[{e.code}] {e.msg}")
                break
            rows = (d or {}).get("list") or (d or {}).get("rows") or []
            for r in rows:
                aid = str(r.get("aid") or r.get("activityId") or "")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                try:
                    raw = int(str(r.get("status") or "0"))
                except Exception:
                    raw = 0
                st = smap.get(raw) or "open"
                sid = r.get("signUpId") or r.get("signupId") or ""
                out.append({
                    "aid": aid,
                    "name": str(r.get("name") or r.get("activityName") or ""),
                    "state": st,
                    "status_text": str(r.get("statusText") or ""),
                    "time": str(r.get("activitytime") or r.get("activityStartTime") or ""),
                    "category": str(r.get("catalog2name") or ""),
                    "signup_id": "" if str(sid) in ("", "0", "None") else str(sid),
                })
            if not (d or {}).get("haveMore"):
                break
        self._mine_cache, self.mine_at = out, now
        return out

    def _name_of(self, aid: str) -> str:
        for it in self.items:
            if it["aid"] == str(aid):
                return it.get("name", "")
        return str(aid)

    def state(self) -> dict:
        items = list(self.items)
        cnt: dict[str, int] = {}
        for it in items:
            cnt[it["state"]] = cnt.get(it["state"], 0) + 1
        live = [it for it in items if it["state"] != "ended"]
        # 只吃缓存，不在这里同步打接口（否则首页要等 3 页「我的活动」）
        if self._mine_cache and time.time() - self.mine_at < 120:
            mine_rows = {m["aid"]: m for m in self._mine_cache}
        else:
            mine_rows = {}
            threading.Thread(target=self._warm_mine, daemon=True).start()
        for it in live:
            it["address"] = self.places.get(it["aid"], "") or it.get("place", "")
            m = mine_rows.get(it["aid"])
            if m:
                it["joined_by_me"] = True
                it["signup_id"] = m.get("signup_id", "")
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
            "mine_warming": not (self._mine_cache and time.time() - self.mine_at < 120),
            "auto": (USERS.auto_map(self.uid) if USERS else {}),
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
SCHED: "autosign.Scheduler | None" = None


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

        if u.path == "/api/auto":
            if self._need_login():
                return
            self._json({"items": USERS.auto_list(uid)})
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

        if u.path == "/api/mine":
            if self._need_login():
                return
            hub = get_hub(uid)
            if not hub:
                self._json({"error": "用户不存在"}, 404)
                return
            self._json({"items": hub.mine_items(), "at": int(time.time())})
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
                out = {k: det.get(k) for k in DETAIL_KEYS}
                # 补上归一化结果：前端要靠 state / sign_start 判断能否「定时报名」
                try:
                    from rules import normalize
                    n = normalize(det, hub.cfg.get("status_map"))
                    out["state"] = n.get("state")
                    out["sign_start"] = n.get("sign_start")
                    out["sign_end"] = n.get("sign_end")
                except Exception:  # noqa: BLE001
                    pass
                self._json(out)
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
                _old = HUBS.pop(uid, None)
                if _old is not None:
                    try:
                        _old.mon.close()
                    except Exception:  # noqa: BLE001
                        pass
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
        elif u.path in ("/api/auto", "/api/auto/cancel"):
            aid = str(body.get("aid") or "").strip()
            if not aid:
                self._json({"ok": False, "error": "缺少活动 ID"}, 400)
                return
            if u.path == "/api/auto/cancel":
                USERS.auto_del(uid, aid)
                self._json({"ok": True, "msg": "已取消定时报名"})
                return
            # 取报名开始时间：优先显式传入，其次详情
            at = body.get("at")
            try:
                at_f = float(at) if at not in (None, "") else 0.0
            except (TypeError, ValueError):
                at_f = 0.0
            name = hub._name_of(aid)
            if not name or name == aid:
                try:
                    _d = hub.mon.client.detail(aid) or {}
                    name = str(_d.get("activityName") or _d.get("name") or name)
                except Exception:  # noqa: BLE001
                    pass
            if not at_f:
                try:
                    d = hub.mon.client.detail(aid)
                    from rules import normalize
                    n = normalize(d, hub.cfg.get("status_map"))
                    at_f = float(n.get("sign_start") or 0)   # normalize 已归一到「秒」
                    if at_f > 1e11:
                        at_f /= 1000.0
                    if not name:
                        name = str(n.get("name") or "")
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": f"拿不到报名开始时间：{e}"}, 400)
                    return
            if not at_f:
                self._json({"ok": False, "error": "这条活动没有报名开始时间"}, 400)
                return
            if at_f * 1000 < time.time() * 1000:
                pass  # 已经到点也允许加，调度器会立刻尝试
            USERS.auto_add(uid, aid, name, at_f)
            hub.log(f"已设定时报名：{name[:20]}｜{time.strftime('%m-%d %H:%M:%S', time.localtime(at_f))}")
            self._json({"ok": True, "msg": "已设定时报名", "at": at_f})
        elif u.path in ("/api/signup", "/api/cancel"):
            aid = str(body.get("aid") or "").strip()
            try:
                if u.path == "/api/signup":
                    self._json(hub.signup(aid))
                else:
                    self._json(hub.cancel_signup(aid))
            except ApiError as e:
                self._json({"ok": False, "error": e.msg, "code": e.code}, 400)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 400)
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

    global SCHED
    SCHED = autosign.Scheduler(USERS, get_hub, log=lambda m: print(m, flush=True))
    SCHED.start()

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
