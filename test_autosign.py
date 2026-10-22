"""定时报名调度器离线自测：不联网，用假 Users / 假 Hub 验证状态机。

python test_autosign.py  →  ✅ 全部断言通过
"""

from __future__ import annotations

import time

from autosign import Scheduler


class FakeUsers:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def auto_add(self, uid, aid, name, join_start):
        self.rows[(uid, aid)] = {"uid": uid, "aid": aid, "name": name,
                                 "join_start": join_start, "status": "pending", "note": ""}

    def auto_due(self, now, window=15.0):
        return [dict(r) for r in self.rows.values()
                if r["status"] in ("pending", "retry") and r["join_start"] - window <= now]

    def auto_mark(self, uid, aid, status, note=""):
        self.rows[(uid, aid)]["status"] = status
        self.rows[(uid, aid)]["note"] = note


class FakeHub:
    """按脚本逐次返回：字符串=抛该异常，None=成功。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def signup(self, aid):
        self.calls += 1
        nxt = self.script.pop(0) if self.script else None
        if nxt:
            raise ValueError(nxt)
        return {"ok": True, "msg": "已报名「测试活动」"}


def run():
    u = FakeUsers()
    hub = FakeHub(["不在报名时间", "不在报名时间", None])
    sch = Scheduler(u, lambda uid: hub)
    t0 = time.time()

    u.auto_add("62153844", "16697326", "测试活动", t0 + 10)

    # 提前窗口外：不该动
    fired = sch.tick(t0 - 60)
    assert fired == [] and hub.calls == 0, "窗口外不应触发"

    # 进入窗口：第一次「不在报名时间」→ retry
    sch.tick(t0 + 10 - 5)
    assert u.rows[("62153844", "16697326")]["status"] == "retry", u.rows
    assert hub.calls == 1

    # 第二次仍太早 → retry
    sch.tick(t0 + 10)
    assert u.rows[("62153844", "16697326")]["status"] == "retry"
    assert hub.calls == 2

    # 第三次成功
    fired = sch.tick(t0 + 11)
    assert len(fired) == 1 and fired[0]["ok"], fired
    assert u.rows[("62153844", "16697326")]["status"] == "success", u.rows
    print("✅ 重试→成功 状态机正确")

    # 已报过（2000011）→ failed，不再重试
    u.auto_add("62153844", "2", "已报活动", t0 + 5)
    hub2 = FakeHub(["你已经报过这个活动了 2000011"])
    sch2 = Scheduler(u, lambda uid: hub2)
    sch2.tick(t0 + 6)
    assert u.rows[("62153844", "2")]["status"] == "failed", u.rows
    print("✅ 已报名不重复提交")

    # 用户不在线（hub 为 None）→ 保持 pending
    u.auto_add("999", "3", "无 Hub", t0 + 1)
    sch3 = Scheduler(u, lambda uid: None)
    sch3.tick(t0 + 2)
    assert u.rows[("999", "3")]["status"] == "pending", u.rows
    print("✅ 用户未登录时保持等待")

    # 超过 5 分钟仍未成功 → failed
    u.auto_add("62153844", "4", "超时活动", t0 - 1000)
    hub4 = FakeHub(["不在报名时间"])
    sch4 = Scheduler(u, lambda uid: hub4)
    sch4.tick(t0)
    assert u.rows[("62153844", "4")]["status"] == "failed", u.rows
    print("✅ 超时放弃")


if __name__ == "__main__":
    run()
    print("✅ 全部断言通过")
