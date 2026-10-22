"""定时报名调度器（服务端常驻线程）。

设计要点
- 只做「到点就报」：每条规则存 join_start（活动报名开始时间，epoch 秒）。
- 提前 `LEAD` 秒进入开火窗口，每 `POLL` 秒重试一次，直到成功 / 超时 / 被取消。
- 服务端时间与到梦空间时间可能有几秒偏差：太早会拿到「不在报名时间」，
  这种情况不算失败，继续重试，靠这个窗口自动吸收偏差。
- 一切都是服务端行为，浏览器关掉也照常执行。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

LEAD = 15.0          # 提前多少秒开始尝试
POLL = 2.0           # 重试间隔（秒）
GIVE_UP_AFTER = 300.  # 报名开始后多久仍失败就放弃（秒）

# 这些错误码/文案表示「还没到点」，要继续重试
RETRY_HINTS = ("不在报名时间", "未开始", "2000166", "报名尚未开始", "还没开始")
# 这些表示「已经不需要再报了」
DONE_HINTS = ("已报名", "已经报过", "2000011", "名额已满", "名额已经满了", "报名已结束", "已结束")


class Scheduler:
    def __init__(self, users, hub_of: Callable[[str], object | None], log: Callable[[str], None] | None = None):
        self.users = users
        self.hub_of = hub_of
        self._log = log or (lambda m: None)
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self.last_tick = 0.0
        self.running = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="autosign", daemon=True)
        self._th.start()
        self.running = True
        self._log("定时报名调度器已启动")

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"调度器异常：{type(e).__name__}: {e}")
            self._stop.wait(POLL)

    # ------------------------------------------------------------------ 主逻辑
    def tick(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        self.last_tick = now
        due = self.users.auto_due(now, LEAD)
        fired = []
        for row in due:
            uid, aid = row["uid"], row["aid"]
            start = float(row["join_start"])
            if now > start + GIVE_UP_AFTER:
                self.users.auto_mark(uid, aid, "failed", "超过 5 分钟仍未成功，已放弃")
                self._log(f"定时报名放弃：{row['name'][:20]}")
                continue
            hub = self.hub_of(uid)
            if hub is None:
                # 用户还没在本站登录过（或服务刚重启），等下一轮
                continue
            try:
                res = hub.signup(aid)
                msg = (res or {}).get("msg") or "已报名"
                self.users.auto_mark(uid, aid, "success", msg)
                self._log(f"定时报名成功：{row['name'][:20]}｜{msg}")
                fired.append({"uid": uid, "aid": aid, "ok": True, "msg": msg})
                continue
            except Exception as e:  # noqa: BLE001
                text = str(e)
                low = text.lower()
                if any(h in text for h in DONE_HINTS) or "already" in low:
                    self.users.auto_mark(uid, aid, "failed", text[:120])
                    self._log(f"定时报名跳过：{row['name'][:20]}｜{text[:60]}")
                elif any(h in text for h in RETRY_HINTS):
                    self.users.auto_mark(uid, aid, "retry", text[:120])
                else:
                    self.users.auto_mark(uid, aid, "retry", text[:120])
                    self._log(f"定时报名重试：{row['name'][:20]}｜{text[:60]}")
        return fired
