"""本地工作台服务：把 Monitor 包成可点击的界面。

启动：
    python gui_server.py            # 起服务并打开浏览器
    python gui_server.py --no-open  # 只起服务

每个控件都对应一个真实动作，没有占位按钮：
    开始 / 暂停      -> 轮询线程启停
    立即刷新         -> 同步跑一轮 tick()
    刷新 token       -> 后台跑 tools/sync_token.py（读手机内存）
    测试通知         -> 真发一条 Server酱
    状态筛选         -> 前端过滤真实数据
    点击活动         -> 调 /v2/activity/detail 拉详情
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from monitor import Monitor  # noqa: E402
from notify import Notifier  # noqa: E402

UI_DIR = ROOT / "ui"
PORT = 8770

SLIM_KEYS = ("aid", "name", "state", "status_raw", "quota", "joined", "capacity",
             "category", "organizer", "sign_end", "sign_start", "act_start", "credit")


def _slim(a: dict) -> dict:
    out = {k: a.get(k) for k in SLIM_KEYS}
    out["aid"] = str(out["aid"] or "")
    out.pop("raw", None)
    return out


class Hub:
    def __init__(self) -> None:
        self.cfg_path = ROOT / "config.yaml"
        self.cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.mon = Monitor(self.cfg)
        self.lock = threading.Lock()
        self.running = False
        self.busy = False
        self.last_sync: float | None = None
        self.sent = 0
        self.logs: list[dict] = []
        self.items: list[dict] = []
        self.places: dict[str, str] = {}
        self._place_job = False
        self.token_job: dict | None = None
        self._worker: threading.Thread | None = None
        self.log("工作台已启动")

    # ---------------------------------------------------------------- 日志
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
            self.log(f"抓取 {len(acts)} 条 · 推送 {max(sent, 0)} 条")
            threading.Thread(target=self._fill_places, daemon=True).start()
            return True
        except Exception as e:  # noqa: BLE001
            self.log(f"本轮失败：{type(e).__name__}: {e}")
            return False
        finally:
            self.busy = False

    # ------------------------------------------------------------ 活动地点
    def _fill_places(self) -> None:
        """列表接口没有地点，逐条拉详情补上地址；拉到就缓存，不重复打。"""
        if self._place_job:
            return
        self._place_job = True
        try:
            with self.lock:
                need = [i["aid"] for i in self.items if i["aid"] not in self.places]
            for aid in need[:80]:
                try:
                    det = self.mon.client.detail(aid) or {}
                    self.places[aid] = det.get("address") or ""
                except Exception:  # noqa: BLE001
                    self.places[aid] = ""
                time.sleep(0.15)
            if need:
                self.log(f"已补 {min(len(need), 80)} 条活动地点")
        finally:
            self._place_job = False

    # ---------------------------------------------------------------- 首轮
    def start(self) -> None:
        """只在启动时抓一次；不再自动轮询（页面按需手动刷新）。"""
        if self.running:
            return
        self.running = True
        threading.Thread(target=self.tick, daemon=True).start()
        self.log("启动抓取一次")

    def stop(self) -> None:
        self.running = False

    # ---------------------------------------------------------------- token
    def refresh_token(self) -> None:
        if self.token_job and self.token_job.get("status") == "running":
            return

        def run() -> None:
            self.token_job = {"status": "running", "msg": "正在读手机内存…"}
            self.log("开始刷新 token")
            try:
                r = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / "sync_token.py")],
                    cwd=str(ROOT), capture_output=True, timeout=900)
                out = (r.stdout or b"").decode("utf-8", "replace")
                if r.returncode != 0:
                    self.token_job = {"status": "fail", "msg": out.strip()[-200:] or "刷新失败"}
                    self.log("token 刷新失败")
                    return
                cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))["api"]
                self.mon.client.token = str(cfg["token"])
                self.mon.client.uid = str(cfg["uid"])
                self.token_job = {"status": "ok", "msg": f"已更新为 {str(cfg['token'])[:8]}…"}
                self.log(f"token 已刷新：{cfg['token']}")
            except Exception as e:  # noqa: BLE001
                self.token_job = {"status": "fail", "msg": f"{type(e).__name__}: {e}"}
                self.log(f"token 刷新异常：{e}")

        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict:
        items = list(self.items)
        cnt: dict[str, int] = {}
        for it in items:
            cnt[it["state"]] = cnt.get(it["state"], 0) + 1
        # 已结束的不再展示（用户只看还能报/还没开始的）
        live = [it for it in items if it["state"] != "ended"]
        for it in live:
            it["address"] = self.places.get(it["aid"], "")
        live.sort(key=lambda x: (0 if x["state"] in ("open", "full") else 1,
                                 x["sign_end"] or 0))
        return {
            "busy": self.busy,
            "last_sync": self.last_sync,
            "token": str((self.cfg.get("api") or {}).get("token", "")),
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
        }


HUB: Hub | None = None
ACCESS_CODE = ""   # 非空：访问需要 ?k=<口令>，会写 cookie 记住


class Handler(SimpleHTTPRequestHandler):
    server_version = "DMDesk/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(UI_DIR), **kw)

    def log_message(self, fmt, *args):  # 静音访问日志
        return

    # ---------------------------------------------------------------- 口令
    def _authed(self) -> bool:
        if not ACCESS_CODE:
            return True
        q = parse_qs(urlparse(self.path).query)
        if (q.get("k") or [""])[0] == ACCESS_CODE:
            return True
        return f"dmk={ACCESS_CODE}" in (self.headers.get("cookie") or "")

    def _maybe_login(self) -> bool:
        """口令放行时写 cookie 再跳回首页，返回 True 表示已处理。"""
        q = parse_qs(urlparse(self.path).query)
        if (q.get("k") or [""])[0] != ACCESS_CODE:
            return False
        body = ("<!doctype html><meta charset=utf-8>"
                "<script>document.cookie='dmk=" + ACCESS_CODE +
                ";path=/;max-age=2592000';location.replace('/' + location.hash);</script>")
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return True

    def _deny(self) -> None:
        raw = "<!doctype html><meta charset=utf-8><p>需要访问口令：在网址后面加 ?k=你的口令".encode("utf-8")
        self.send_response(403)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---------------------------------------------------------------- 工具
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if HUB is None:
            self._json({"error": "hub not ready"}, 503)
            return False
        return True

    # ---------------------------------------------------------------- GET
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if ACCESS_CODE:
            # 带 ?k= 时先写 cookie 再跳回干净地址，否则后续 /api 请求没有凭证
            if self._maybe_login():
                return
            if not self._authed():
                self._deny()
                return
        if u.path == "/api/state":
            if not self._guard():
                return
            self._json(HUB.state())
            return
        if u.path == "/api/activity":
            if not self._guard():
                return
            aid = (parse_qs(u.query).get("aid") or [""])[0]
            if not aid:
                self._json({"error": "missing aid"}, 400)
                return
            try:
                det = HUB.mon.client.detail(aid) or {}
                keep = ("activityId", "activityName", "statusText", "joinNum", "joinmaxnum",
                        "joindate", "startdate", "startdateDate", "enddate", "schoolname",
                        "address", "catalog2name", "levelText", "joinrangeText", "joinWayDesc",
                        "countdownText", "canJoin", "ableJoinFlag", "score")
                self._json({k: det.get(k) for k in keep})
            except Exception as e:  # noqa: BLE001
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        if u.path == "/":
            self.path = "/index.html"
        super().do_GET()

    # ---------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        if ACCESS_CODE and not self._authed():
            self._deny()
            return
        if not self._guard():
            return
        if u.path == "/api/refresh":
            if HUB.busy:
                self._json({"ok": False, "reason": "busy"})
            else:
                threading.Thread(target=HUB.tick, daemon=True).start()
                self._json({"ok": True, "started": True})
        elif u.path == "/api/token":
            HUB.refresh_token()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会让多个进程绑同一端口，导致请求打到旧实例
    allow_reuse_address = False
    daemon_threads = True


def main() -> int:
    global HUB, ACCESS_CODE
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("DM_PORT") or os.environ.get("PORT") or PORT),
                    help="监听端口；也可用环境变量 DM_PORT / PORT（云平台惯例）")
    ap.add_argument("--host", default=os.environ.get("DM_HOST", "0.0.0.0"),
                    help="监听地址；内网穿透/云主机用 0.0.0.0，只本机用 127.0.0.1")
    ap.add_argument("--code", default=os.environ.get("DM_CODE", ""),
                    help="访问口令，非空则需 ?k=口令；也可用环境变量 DM_CODE")
    ap.add_argument("--auto", action="store_true",
                    help="启动后自动开始轮询（也可用环境变量 DM_AUTO=1）")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if os.environ.get("DM_AUTO", "").strip() in ("1", "true", "yes", "on"):
        args.auto = True

    ACCESS_CODE = args.code.strip()
    HUB = Hub()
    if args.auto:
        HUB.start()
    httpd = Server((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{args.port}/" + (f"?k={ACCESS_CODE}" if ACCESS_CODE else "")
    print(f"工作台已启动：{url}")
    print(f"监听 {args.host}:{args.port} · 手动刷新（无自动轮询）· "
          f"口令 {'已开启' if ACCESS_CODE else '未设置'} · 通知 {'开' if (HUB.cfg.get('notify') or {}).get('serverchan', {}).get('enabled') else '关'}")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
