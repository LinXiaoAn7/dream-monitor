"""monitor —— 轮询、状态比对、通知、可选自动报名。"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from detail_cache import DetailCache
from dmclient import ApiError, AuthExpired, DreamClient, find_list
from notify import Notifier
import rules as R
from store import Store

log = logging.getLogger("monitor")
ROOT = Path(__file__).resolve().parent


class Monitor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client = DreamClient(cfg["api"])
        self.store = Store(cfg.get("db", "data/dm.db"))
        self.notifier = Notifier(cfg.get("notify") or {})
        self.rules = cfg.get("rules") or {}
        self.poll = cfg.get("poll_interval", 30)
        self.max_pages = cfg.get("max_pages", 10)
        self.page_size = cfg.get("page_size", 20)
        self.status_map = cfg.get("status_map")
        self.auto_signup = cfg.get("auto_signup") or {}
        self._last_signup = 0.0
        self._round = 0
        self._dumped = False
        self.last_acts: list[dict] = []
        self.on_list = None          # 列表阶段的回调：先把活动推给界面
        self._pool = None            # 常驻线程池：复用线程，省掉每轮的 TCP/TLS 握手
        self._page_items: dict[str, dict] = {}
        self.progress: dict = {}      # 抓取进度，给界面画进度条用
        # 会话状态：单点登录下，任何一次登录都会把手机端挤下线，
        # 所以默认**不主动登录**，失效了只在页面上提示，由用户点按钮决定。
        self.auth_stale = False
        self.auth_msg = ""
        self.last_login: float | None = None

        # 并发 + 跨用户共享缓存：新用户首屏不再一条条串行拉详情
        self.api_cfg = copy.deepcopy(cfg["api"])
        self.page_workers = max(1, int(cfg.get("page_workers", 8)))
        self.detail_workers = max(1, int(cfg.get("detail_workers", 10)))
        self._tls = threading.local()
        self.cache = cfg.get("detail_cache") or DetailCache(
            cfg.get("detail_cache_path", "data/shared_details.db"),
            int(cfg.get("detail_ttl", 900)),
        )

    # ------------------------------------------------------------ 抓取
    # ------------------------------------------------------ 并发小工具
    def _thread_client(self) -> DreamClient:
        """每个线程一个客户端（各自独立 session 与限速），token 跟随主客户端。"""
        c = getattr(self._tls, "client", None)
        if c is None:
            cfg = dict(self.api_cfg)
            cfg["token"] = self.client.token
            c = DreamClient(cfg)
            self._tls.client = c
        else:
            c.token = self.client.token
        return c

    def _pages_parallel(self) -> list[dict]:
        """一次把所有页并发打出去；返回按页码排好序的响应列表。"""
        if self.page_workers <= 1:
            out = []
            for n in range(1, self.max_pages + 1):
                try:
                    out.append(self.client.activities(page=n))
                except AuthExpired:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("第 %s 页失败: %s", n, e)
                    break
            return out

        def work(n: int):
            try:
                return n, self._thread_client().activities(page=n)
            except AuthExpired:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("第 %s 页失败: %s", n, e)
                return n, None

        got: dict[int, dict] = {}
        ex = self._get_pool()
        futs = [ex.submit(work, n) for n in range(1, self.max_pages + 1)]
        for f in as_completed(futs):
            n, data = f.result()
            if data:
                got[n] = data
                self._flush_page(data)      # 每页到货就推一版给界面
            self.progress = {"phase": "pages",
                             "done": len(got), "total": self.max_pages}
        return [got[n] for n in sorted(got)]

    def _flush_page(self, data: dict) -> None:
        """把刚到的这一页并进「先出图」的缓存，并推给界面。"""
        if not self.on_list:
            return
        try:
            for it in (find_list(data) or []):
                aid = str(it.get("aid") or it.get("activityId") or "")
                if aid:
                    self._page_items[aid] = it
            if self._page_items:
                self.on_list(self._merge_and_normalize(list(self._page_items.values()), {}, {}))
        except Exception as e:  # noqa: BLE001
            log.debug("逐页出图失败: %s", e)

    def _retry_auth(self, fn, tries: int = 3):
        """并发抓取时偶尔会撞到一次 10007（请求太密被临时拒），
        稍等重试一次通常就好了；真的失效才会一路抛上去。"""
        for i in range(tries):
            try:
                return fn()
            except AuthExpired:
                if i == tries - 1:
                    raise
                wait = 0.35 + i * 0.35
                log.debug("撞到一次 10007，%.1fs 后重试（第 %s 次）", wait, i + 2)
                time.sleep(wait)
        return None

    @staticmethod
    def _have_more(data) -> bool | None:
        """haveMore 在响应里可能位于顶层，也可能嵌在 data/content 里。"""
        if not isinstance(data, dict):
            return None
        if "haveMore" in data:
            return bool(data["haveMore"])
        for key in ("data", "content", "result"):
            inner = data.get(key)
            if isinstance(inner, dict) and "haveMore" in inner:
                return bool(inner["haveMore"])
        return None

    def _details_parallel(self, aids: list[str]) -> dict[str, dict]:
        """并发拉详情；先查共享缓存，缺的再并发打，遇到 10007 自动重试。

        并发时偶尔会被服务端临时拒掉一两条（10007），
        所以失败的那几条要单独重试，不能整批丢掉、也不能直接判失效。
        """
        if not aids:
            return {}
        out = self.cache.get_many(aids)
        miss = [a for a in aids if a not in out]

        def work(aid: str):
            try:
                return aid, (self._thread_client().detail(aid) or {}), False
            except AuthExpired:
                return aid, None, True
            except Exception as e:  # noqa: BLE001
                log.debug("拉详情失败 %s: %s", aid, e)
                return aid, None, False

        for attempt in range(3):
            if not miss:
                break
            fresh: dict[str, dict] = {}
            retry: list[str] = []
            results: list = []
            if self.detail_workers <= 1:
                for a in miss:
                    results.append(work(a))
                    self.progress = {"phase": "details", "done": len(results),
                                     "total": len(miss)}
            else:
                ex = self._get_pool()
                futs = [ex.submit(work, a) for a in miss]
                for f in as_completed(futs):
                    try:
                        results.append(f.result())
                    except Exception as e:  # noqa: BLE001
                        log.debug("详情失败: %s", e)
                    self.progress = {"phase": "details", "done": len(results),
                                     "total": len(miss)}
            for aid, det, auth_fail in results:
                if det:
                    fresh[aid] = det
                elif auth_fail:
                    retry.append(aid)
            if fresh:
                self.cache.put_many(fresh)
                out.update(fresh)
            miss = retry
            if miss and attempt < 2:
                wait = 0.35 + attempt * 0.35
                log.debug("%d 条详情撞到 10007，%.1fs 后重试", len(miss), wait)
                time.sleep(wait)
        if miss:
            log.info("有 %d 条详情这次没取到（下一轮自动补）", len(miss))
        return out

    def fetch_all(self) -> list[dict]:
        """并发翻页 + 并发补详情，并复用跨用户共享缓存。

        列表接口只给 name/status，名额/地点/报名时间要再打 detail。
        所以这里做三件事：
          1. 所有页一次性并发（page_workers）；
          2. 需要详情的活动先去共享缓存找（同一学校所有人共用）；
          3. 缓存没有的再并发打（detail_workers），结果写回缓存。
        """
        t0 = time.time()
        out: list[dict] = []
        self._page_items = {}
        self.progress = {"phase": "pages", "done": 0, "total": self.max_pages}
        enrich = bool(self.cfg.get("enrich_detail", True))
        # 哪些原始状态算「已结束」——其余的都要补详情（名额/地点/时间都只在详情里）
        smap = {int(k): v for k, v in (self.status_map or {}).items()}
        ended_states = {k for k, v in smap.items() if v == "ended"} or {5, 6, 7}

        def _alive(item: dict) -> bool:
            st = item.get("status")
            if st is None:
                return True
            s_st = str(st).strip()
            return not (s_st.isdigit() and int(s_st) in ended_states)
        budget = int(self.cfg.get("detail_refresh_per_tick", 25))

        # ---- 1. 拉列表
        pages = self._retry_auth(self._pages_parallel)
        raw_items: list[dict] = []
        for idx, data in enumerate(pages, 1):
            if not self._dumped and self.cfg.get("dump_raw"):
                self._dump(data, idx)
            items = find_list(data) or []
            if not items:
                break
            raw_items.extend(items)
            more = self._have_more(data)
            if more is False:
                break
            if more is None and len(items) < self.page_size:
                break
        # 一条都没抓到：多半是 token 被别处顶掉了。
        # 这时列表接口可能回空表而不是报错，所以用「上一轮有多少条」来判断：
        # 一个正常账号不可能从有活动突然变成 0 条。
        if not raw_items:
            prev = len(self.last_acts)
            if not prev:
                try:
                    prev = len(self.store.all_ids())
                except Exception:  # noqa: BLE001
                    prev = 0
            if prev:
                raise AuthExpired("10007", f"本轮抓取 0 条（上轮 {prev} 条），疑似登录被顶掉")
            log.info("列表为空（还没有历史数据，视为空账号）")

        t_pages = time.time() - t0

        # ---- 2. 判定谁需要详情
        need: list[str] = []
        stale: list[tuple[float, str]] = []
        stored_map: dict[str, dict] = {}
        seen: set[str] = set()
        stale_after = int(self.cfg.get("detail_ttl", 900))
        for it in raw_items:
            if not enrich:
                break
            aid = str(it.get("aid") or it.get("activityId") or "")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            row = self.store.get(aid)
            if row is None:
                if _alive(it):
                    need.append(aid)
                continue
            if not _alive(it):
                continue
            try:
                stored = json.loads(row["raw"] or "{}")
            except Exception:  # noqa: BLE001
                stored = {}
            # 存量数据一律带上，否则列表项缺少 joinmaxnum 时名额会变成“不限”
            if stored:
                stored_map[aid] = stored
            # 已经存过名额字段的不用再打详情；只看名额字段，别用 joinNum 冒充
            enriched = any(k in stored for k in
                           ("joinmaxnum", "joinMaxNum", "activityMaxNum"))
            try:
                age = time.time() - float(row["updated"] or 0)
            except Exception:  # noqa: BLE001
                age = 1e9
            if not enriched:
                if budget > 0:
                    budget -= 1
                    need.append(aid)
            elif age > stale_after:
                # 名额会变，旧详情要按 ttl 重新拉，否则数字一直停在第一次抓到的值
                stale.append((age, aid))

        if stale:
            stale.sort(reverse=True)          # 最旧的先刷
            picked = [aid for _, aid in stale[:max(0, budget)]]
            if picked:
                log.info("按 TTL 刷新 %d 条旧详情（共 %d 条过期）", len(picked), len(stale))
                need.extend(picked)

        # ---- 2.5 先出图：列表已经拿到了，名额/地点随后再补 ----
        if self.on_list and raw_items:
            try:
                early = self._merge_and_normalize(raw_items, {}, stored_map)
                if early:
                    log.info("先出列表：%d 条（详情随后补）", len(early))
                    self.on_list(early)
            except Exception as e:  # noqa: BLE001
                log.debug("先出列表失败: %s", e)

        # ---- 3. 并发取详情（先查缓存）
        self.progress = {"phase": "details", "done": 0, "total": len(need)}
        t1 = time.time()
        details = self._retry_auth(lambda: self._details_parallel(need))
        t_detail = time.time() - t1
        log.info("并发抓取：%s 页 %.1fs，%s 条详情 %.1fs（共 %.1fs）",
                 len(pages), t_pages, len(need), t_detail, time.time() - t0)

        # ---- 4. 合并归一化（正式的）
        out = self._merge_and_normalize(raw_items, details, stored_map)
        self._dumped = True
        self.last_acts = out
        self.progress = {}
        return out

    def _get_pool(self) -> ThreadPoolExecutor:
        """常驻线程池。线程复用后每个线程的 requests.Session 也复用，
        每页从「握手 + 请求」的 1.3 秒降到 0.2 秒左右。"""
        if self._pool is None:
            n = max(self.page_workers, self.detail_workers, 2)
            self._pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="dm")
        return self._pool

    def warm(self) -> None:
        """预热：让每个工作线程各自建好连接，用户点刷新就不用再等握手。"""
        try:
            n = max(self.page_workers, self.detail_workers)
            futs = [self._get_pool().submit(self._thread_client().activities, 1)
                    for _ in range(n)]
            for f in futs:
                try:
                    f.result(timeout=15)
                except Exception:  # noqa: BLE001
                    pass
            log.info("连接预热完成（%d 条线程）", n)
        except Exception as e:  # noqa: BLE001
            log.debug("预热失败: %s", e)

    def _merge_and_normalize(self, raw_items: list[dict], details: dict[str, dict],
                             stored_map: dict[str, dict]) -> list[dict]:
        out: list[dict] = []
        for it in raw_items:
            aid = str(it.get("aid") or it.get("activityId") or "")
            det = details.get(aid)
            if det:
                if aid in stored_map:
                    merged = dict(stored_map[aid])
                    merged.update(det)
                    merged.update({k: v for k, v in it.items() if v not in (None, "", [])})
                    it = merged
                else:
                    it = {**it, **det}
            elif aid in stored_map:
                merged = dict(stored_map[aid])
                merged.update({k: v for k, v in it.items() if v not in (None, "", [])})
                it = merged
            try:
                act = R.normalize(it, self.status_map)
                if act["aid"]:
                    out.append(act)
            except Exception as e:  # noqa: BLE001
                log.debug("归一化失败，跳过: %s", e)
        return out

    def _dump(self, data, page: int) -> None:
        path = f"data/raw_page{page}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info("原始响应已写入 %s", path)
        except Exception as e:  # noqa: BLE001
            log.warning("dump 失败: %s", e)

    # ------------------------------------------------------------ 比对
    def diff(self, acts: list[dict]) -> list[tuple[str, dict, str]]:
        """返回 [(kind, activity, detail)]。

        规则：
        - 仅第 1 轮静默（避免运行时被历史活动刷屏），之后的新活动都算 new；
        - 同一活动同一轮只取优先级最高的一条事件：new(4) > reopen(3) > quota(2) > state(1)，
          否则"满员→开放"会同时触发 reopen + quota + state 三条重复通知。
        """
        events: list[tuple[str, dict, str]] = []
        prev_map = self.store.snapshot()      # 一次查询，别逐条 SELECT
        pending: list[tuple] = []
        first_round = self._round == 0
        quiet_first = first_round and not self.cfg.get("notify_on_first_run")
        added = 0

        for act in acts:
            aid = act["aid"]
            prev = prev_map.get(aid)
            state, quota = act["state"], act["quota"]
            cand: tuple[int, str, str] | None = None

            if prev is None:
                added += 1
                if not quiet_first:
                    cand = (4, "new", "首次发现")
            else:
                p_state, p_quota = prev[0], prev[1]
                if p_state != state:
                    detail = (f"{R.STATE_LABEL.get(p_state, p_state)} → "
                              f"{R.STATE_LABEL.get(state, state)}")
                    if state == "open" and p_state in ("full", "soon", "ended", "unknown"):
                        cand = (3, "reopen", detail)
                    else:
                        cand = (1, "state", detail)
                if p_quota == 0 and quota > 0 and (cand is None or cand[0] < 2):
                    cand = (2, "quota", f"余量 0 → {quota}")

            # 名额未知（-2）时不要拿来和旧值比对，否则 0 → 未知 会被误判成“满员→开放”
            if quota == -2:
                cand = None
            if cand:
                events.append((cand[1], act, cand[2]))
            pending.append((aid, act["name"], state, quota, act["raw"]))

        self.store.upsert_many(pending)      # 一次 commit
        self._round += 1
        if first_round:
            log.info("首轮登记 %d 个活动（静默）", added)
        return events

    # ------------------------------------------------------------ 通知
    def notify_events(self, events: list[tuple[str, dict, str]]) -> int:
        sent = 0
        for kind, act, detail in events:
            ok, why = R.match(act, self.rules)
            if not ok:
                log.debug("跳过 %s: %s", act["name"], why)
                continue
            if self.store.was_notified(act["aid"], kind):
                log.debug("已通知过 %s/%s，跳过", act["aid"], kind)
                continue

            title = {
                "new": "新活动",
                "reopen": "开放报名",
                "quota": "名额释放",
                "state": "状态变化",
            }.get(kind, "活动更新") + f"｜{R.STATE_LABEL.get(act['state'], act['state'])}"

            body = f"**{act['name']}**\n\n{R.describe(act)}\n\n变动：{detail}"
            if self.rules.get("include"):
                body += f"\n\n筛选命中：{','.join(self.rules['include'])}"

            self.notifier.send(title, body, act.get("url", ""))
            self.store.mark_notified(act["aid"], kind)
            self.store.log_event(act["aid"], act["name"], kind, detail)
            sent += 1

            if self.auto_signup.get("enabled"):
                self.try_signup(act)
        return sent

    # ------------------------------------------------------------ 自动报名
    def try_signup(self, act: dict) -> None:
        cfg = self.auto_signup
        allow = cfg.get("only_ids") or []
        if allow and act["aid"] not in [str(x) for x in allow]:
            return
        if not allow and not self.rules.get("include"):
            log.warning("自动报名已开启但未限定范围（rules.include 或 auto_signup.only_ids 为空），跳过")
            return
        gap = cfg.get("min_interval", 60)
        if time.time() - self._last_signup < gap:
            log.info("自动报名冷却中，跳过 %s", act["name"])
            return
        self._last_signup = time.time()
        try:
            res = self.client.submit(act["aid"], data="", remark=cfg.get("remark", ""))
            log.warning("自动报名成功: %s %s", act["name"], json.dumps(res, ensure_ascii=False)[:200])
            self.notifier.send("自动报名成功", f"{act['name']}\n{json.dumps(res, ensure_ascii=False)[:400]}",
                               act.get("url", ""))
        except ApiError as e:
            log.warning("自动报名失败 [%s] %s ← %s", e.code, e.msg, act["name"])
            self.notifier.send("自动报名失败", f"{act['name']}\n[{e.code}] {e.msg}", act.get("url", ""))

    # ------------------------------------------------------------ 统计
    def summary(self, acts: list[dict]) -> str:
        cnt = Counter(a["state"] for a in acts)
        hits = [a for a in acts if R.match(a, self.rules)[0]]
        open_hits = [a for a in hits if a["state"] in ("open", "quota") or a["quota"] > 0]
        lines = [
            f"抓取 {len(acts)} 条｜"
            + " ".join(f"{R.STATE_LABEL.get(k, k)}:{v}" for k, v in cnt.most_common())
            + f"｜命中 {len(hits)}｜可报名 {len(open_hits)}"
        ]
        for a in open_hits[:15]:
            q = "不限" if a["quota"] < 0 else a["quota"]
            lines.append(f"  · [{R.STATE_LABEL.get(a['state'], a['state'])}] {a['name']}"
                         f"（余 {q}，截止 {R.fmt_ts(a['sign_end'])}）")
        return "\n".join(lines)

    # ------------------------------------------------------------ 单轮
    def _refresh_token(self) -> bool:
        """先试账号密码直登（秒级、不碰手机）；不行再从手机内存捞。"""
        api = self.cfg.get("api") or {}
        account = str(api.get("account") or "")
        password = str(api.get("password") or "")
        if account and password:
            for attempt in (1, 2):
                try:
                    info = self.client.login_account(account, password)
                    self.cfg.setdefault("api", {}).update(
                        uid=info["uid"], token=info["token"])
                    self.client.token = str(info["token"])
                    self.client.uid = str(info["uid"])
                    self._save_token(info["uid"], info["token"])
                    log.info("已用账号密码换到新 token：%s", info["token"])
                    # 登录成功≠能用：风控会按设备指纹拦业务接口（返回 10005），
                    # 这时换一套指纹再登一次就能过。
                    ok, why = self.client.ping()
                    if ok:
                        return True
                    log.warning("登录成功但业务接口被拦：%s（第 %d 次）", why, attempt)
                    if attempt == 1:
                        self.client.rotate_identity()
                        self._save_identity()
                    else:
                        return False
                except Exception as e:  # noqa: BLE001
                    log.warning("账号密码登录失败（转内存方案）：%s", e)
                    break

        if self.cfg.get("on_token"):
            # 多用户模式：每个账号有自己的凭据，绝不能去读别人的 config.yaml
            return False

        try:
            r = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_token.py")],
                               cwd=str(ROOT), capture_output=True, timeout=900)
            if r.returncode != 0:
                err = (r.stdout or b"").decode("utf-8", "replace")[-400:]
                log.error("token 刷新失败：%s", err.replace("\n", " "))
                return False
            import yaml
            cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["api"]
            self.client.token = str(cfg["token"])
            self.client.uid = str(cfg["uid"])
            self.cfg.setdefault("api", {}).update(token=str(cfg["token"]), uid=str(cfg["uid"]))
            log.info("token 已自动刷新：%s", self.client.token)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("token 刷新异常: %s", e)
            return False

    # ------------------------------------------------------------ 手动重登
    def relogin(self) -> tuple[bool, str]:
        """用户主动要求重新登录。

        服务端是单点登录：这一步一旦成功，**手机端上同一账号的登录会立刻失效**。
        所以只在用户点按钮时才走这里，绝不在后台偷偷做。
        """
        api = self.cfg.get("api") or {}
        account = str(api.get("account") or "")
        password = str(api.get("password") or "")
        if not (account and password):
            return False, "config.yaml 里没有账号密码"
        try:
            info = self.client.login_account(account, password)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        self.cfg.setdefault("api", {}).update(
            uid=info["uid"], token=info["token"])
        self._save_token(info["uid"], info["token"])
        self.auth_stale = False
        self.auth_msg = ""
        self.last_login = time.time()
        log.info("手动重登成功，新 token：%s", info["token"])
        return True, f"已登录，新 token {info['token'][:8]}…（手机端的登录已被替换）"

    def _save_token(self, uid: str, token: str) -> None:
        """把新 token 写回（单用户写 config.yaml；多用户走 on_token 回调写库）。"""
        cb = self.cfg.get("on_token")
        if callable(cb):
            try:
                cb(str(uid), str(token))
            except Exception as e:  # noqa: BLE001
                log.warning("on_token 回调失败：%s", e)
            return
        try:
            import yaml
            path = ROOT / "config.yaml"
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            cfg.setdefault("api", {}).update(uid=str(uid), token=str(token))
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("写回 config.yaml 失败：%s", e)

    def _save_identity(self) -> None:
        """把新的设备指纹写回 config.yaml（多用户模式下也写，指纹是全局的）。"""
        if self.cfg.get("on_identity"):
            try:
                self.cfg["on_identity"]({k: self.cfg.get(k) for k in ("uuid", "jPushId", "device")})
            except Exception as e:  # noqa: BLE001
                log.warning("on_identity 回调失败：%s", e)
        try:
            import yaml
            path = ROOT / "config.yaml"
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for k in ("uuid", "jPushId", "device"):
                cfg[k] = self.cfg.get(k)
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("写回设备指纹失败：%s", e)

    def tick(self) -> tuple[int, str]:
        try:
            acts = self.fetch_all()
        except AuthExpired as e:
            api = self.cfg.get("api") or {}
            auto = bool(api.get("auto_relogin", False))
            log.warning("凭据失效 [%s]（auto_relogin=%s）", e.code, auto)
            if not auto:
                self.auth_stale = True
                self.auth_msg = f"登录已失效 [{e.code}]，需要重新登录"
                return -1, ("凭据失效：服务端是单点登录，重新登录会把手机端挤下线，"
                            "已暂停自动登录，请在页面上手动点「重新登录」")
            if not self._refresh_token():
                self.auth_stale = True
                self.auth_msg = f"自动换 token 失败 [{e.code}]"
                return -1, f"凭据失效：{e.msg}"
            try:
                acts = self.fetch_all()
            except Exception as e2:  # noqa: BLE001
                log.error("刷新后仍失败: %s", e2)
                return -1, f"凭据失效且刷新无效：{e.msg}"
        except ApiError as e:
            log.error("接口错误 [%s] %s", e.code, e.msg)
            return -2, f"接口错误 [{e.code}] {e.msg}"
        except Exception as e:  # noqa: BLE001
            log.error("抓取异常: %s", e)
            return -3, f"网络异常：{type(e).__name__}: {e}"

        self.auth_stale = False
        self.auth_msg = ""
        events = self.diff(acts)
        sent = self.notify_events(events)
        text = self.summary(acts)
        log.info(text.replace("\n", " ")[:300])
        if sent:
            log.info("本轮推送 %d 条", sent)
        return sent, text

    # ------------------------------------------------------------ 主循环
    def run_forever(self) -> None:
        log.info("开始监控，间隔 %ss（Ctrl+C 退出）", self.poll)
        n = 0
        while True:
            n += 1
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("轮次 %d 异常: %s", n, e)
            jitter = self.poll * (0.85 + 0.3 * (n % 3) / 3)
            time.sleep(jitter)
