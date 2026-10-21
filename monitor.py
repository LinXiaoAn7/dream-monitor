"""monitor —— 轮询、状态比对、通知、可选自动报名。"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

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

    # ------------------------------------------------------------ 抓取
    def fetch_all(self) -> list[dict]:
        """翻页拉全部活动，返回归一化后的列表。

        列表接口只给 name/status/statusText/activitytime 这几项，
        名额和报名起止时间必须再打一次 detail —— 所以只对**没见过的**活动补详情。
        """
        out: list[dict] = []
        enrich = bool(self.cfg.get("enrich_detail", True))
        # 存量活动如果当初没抓到详情，每轮补一点，避免一次把请求打满
        budget = int(self.cfg.get("detail_refresh_per_tick", 25))
        for page in range(1, self.max_pages + 1):
            data = self.client.activities(page=page)
            if not self._dumped and self.cfg.get("dump_raw"):
                self._dump(data, page)
            items = find_list(data) or []
            if not items:
                break
            for it in items:
                aid = str(it.get("aid") or it.get("activityId") or "")
                if aid and enrich:
                    row = self.store.get(aid)
                    if row is None:
                        # 新活动：补一次详情，拿名额/报名起止
                        try:
                            it = {**it, **(self.client.detail(aid) or {})}
                        except Exception as e:  # noqa: BLE001
                            log.debug("拉详情失败 %s: %s", aid, e)
                    else:
                        # 老活动：列表项只有 name/status，用上次存的详情补齐名额与时间，
                        # 否则每轮都会显示成「余量=不限 截止=-」
                        try:
                            stored = json.loads(row["raw"] or "{}")
                        except Exception:  # noqa: BLE001
                            stored = {}
                        if "joinmaxnum" not in stored and budget > 0:
                            budget -= 1
                            try:
                                stored = {**stored, **(self.client.detail(aid) or {})}
                            except Exception as e:  # noqa: BLE001
                                log.debug("补详情失败 %s: %s", aid, e)
                        merged = dict(stored)
                        merged.update({k: v for k, v in it.items()
                                       if v not in (None, "", [])})
                        it = merged
                try:
                    act = R.normalize(it, self.status_map)
                    if act["aid"]:
                        out.append(act)
                except Exception as e:  # noqa: BLE001
                    log.debug("归一化失败，跳过: %s", e)
            more = data.get("haveMore") if isinstance(data, dict) else None
            if more is False or len(items) < 40:
                break
        self._dumped = True
        self.last_acts = out
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
        first_round = self._round == 0
        quiet_first = first_round and not self.cfg.get("notify_on_first_run")
        added = 0

        for act in acts:
            aid = act["aid"]
            prev = self.store.get(aid)
            state, quota = act["state"], act["quota"]
            cand: tuple[int, str, str] | None = None

            if prev is None:
                added += 1
                if not quiet_first:
                    cand = (4, "new", "首次发现")
            else:
                p_state, p_quota = prev["state"], prev["quota"]
                if p_state != state:
                    detail = (f"{R.STATE_LABEL.get(p_state, p_state)} → "
                              f"{R.STATE_LABEL.get(state, state)}")
                    if state == "open" and p_state in ("full", "soon", "ended", "unknown"):
                        cand = (3, "reopen", detail)
                    else:
                        cand = (1, "state", detail)
                if p_quota == 0 and quota > 0 and (cand is None or cand[0] < 2):
                    cand = (2, "quota", f"余量 0 → {quota}")

            if cand:
                events.append((cand[1], act, cand[2]))
            self.store.upsert(aid, act["name"], state, quota, act["raw"])

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
            try:
                info = self.client.login_account(account, password)
                self.cfg.setdefault("api", {}).update(
                    uid=info["uid"], token=info["token"])
                self._save_token(info["uid"], info["token"])
                log.info("已用账号密码换到新 token：%s", info["token"])
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("账号密码登录失败（转内存方案）：%s", e)

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

    def _save_token(self, uid: str, token: str) -> None:
        """把新 token 写回 config.yaml，重启后也不用再登。"""
        try:
            import yaml
            path = ROOT / "config.yaml"
            cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
            cfg.setdefault("api", {}).update(uid=str(uid), token=str(token))
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("写回 config.yaml 失败：%s", e)

    def tick(self) -> tuple[int, str]:
        try:
            acts = self.fetch_all()
        except AuthExpired as e:
            log.warning("凭据失效 [%s]，自动换新 token ...", e.code)
            if not self._refresh_token():
                return -1, f"凭据失效：{e.msg}（手动跑 python tools/sync_token.py）"
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
