"""离线端到端仿真：伪造 API 响应，验证 归一化 → diff → 筛选 → 通知 全链路。"""
import json
import os
import time
import tempfile

import rules as R
import monitor as M
from store import Store


def act(aid, name, status, joined, cap, **kw):
    d = {
        "activityId": aid, "activityName": name, "status": status,
        "joinNum": joined, "joinmaxnum": cap,
        "signupEndTime": str(int((time.time() + 86400) * 1000)),
        "categoryName": kw.get("cat", "文体活动"),
        "createUnitName": kw.get("org", "校学生会"),
        "credit": kw.get("credit", "0.5"),
        "shareCode": kw.get("share", ""),
    }
    return d


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def activities(self, page=1, page_size=20, **kw):
        self.calls = page
        items = self.pages.get(page, [])
        more = bool(self.pages) and page < max(self.pages)
        return {"code": "100", "data": {"list": items, "haveMore": more}}


def build(pages):
    cfg = {
        "api": {"uid": "u", "token": "t"},
        "db": os.path.join("data", f"test_{int(time.time()*1000)%100000}.db"),
        "notify": {"console": {"enabled": False}},
        "rules": {"states": ["open", "full"], "require_quota": True,
                  "include": [], "exclude": ["测试"]},
        "poll_interval": 1, "page_size": 2, "max_pages": 3,
        "notify_on_first_run": False,
    }
    cfg["page_workers"] = 1          # 离线仿真不并发
    cfg["detail_workers"] = 1
    cfg["enrich_detail"] = False     # 假数据没有详情接口
    from detail_cache import DetailCache
    cfg["detail_cache"] = DetailCache(
        os.path.join("data", f"tc_{int(time.time()*1000)%100000}.db"), 60)
    m = M.Monitor(cfg)
    m.client = FakeClient(pages)
    return m


print("### 第1轮：首轮静默登记 ###")
pages1 = {1: [act(1001, "校园歌手大赛", 3, 45, 100), act(1002, "志愿者招募", 3, 20, 20)],
          2: [act(1003, "测试活动XYZ", 3, 1, 50)]}
m1 = build(pages1)
e1 = m1.diff(m1.fetch_all())
print("事件数:", len(e1), "（首轮应静默，预期 0）")
print("DB 活动数:", len(m1.store.all_ids()))

print("\n### 第2轮：1001 满员 / 1002 释放名额 / 1004 新增 / 1003 被排除词过滤 ###")
m1.client.pages = {
    1: [act(1001, "校园歌手大赛", 3, 100, 100),
        act(1002, "志愿者招募", 3, 18, 20)],
    2: [act(1003, "测试活动XYZ", 3, 1, 50),
        act(1004, "创新创业讲座", 3, 5, 80, cat="创新创业", org="计算机学院")],
}
acts2 = m1.fetch_all()
e2 = m1.diff(acts2)
for kind, a, detail in e2:
    ok, why = R.match(a, m1.rules)
    print(f"  [{kind:6s}] {a['name']:16s} state={a['state']:8s} quota={a['quota']:3d} "
          f"match={ok} ({why})  {detail}")

print("\n### 通知去重 ###")
sent = m1.notify_events(e2)
print("首次推送:", sent, "条")
sent2 = m1.notify_events(e2)
print("重复推送:", sent2, "条（预期 0）")

print("\n### 统计汇总 ###")
print(m1.summary(acts2))

print("\n### 字段容错测试 ###")
weird = [
    {"id": 9, "name": "无 activityId 的活动", "state": 3},
    {"activityid": "10", "title": "小写键", "activityStatus": 3, "joinNum": "7", "joinmaxnum": "不限"},
    {"activityId": 11, "activityName": "日期字符串", "status": 3,
     "signupEndTime": "2026-12-31 23:59", "activityStartTime": "2027-01-01 09:00"},
    {},
]
for w in weird:
    n = R.normalize(w)
    print(f"  aid={n['aid']!r:8s} name={n['name']!r:24s} state={n['state']:8s} "
          f"quota={n['quota']:3d} end={R.fmt_ts(n['sign_end'])}")

print("\n### 事件表 ###")
for r in m1.store.recent_events(10):
    print(f"  {r['kind']:6s} {r['name']} — {r['detail']}")

assert len(e1) == 0, "首轮不应产生事件"
kinds = [k for k, _, _ in e2]
assert "new" in kinds, f"应检测到新活动，实际 {kinds}"
assert "reopen" in kinds, f"应检测到重新开放，实际 {kinds}"
# 同一活动不得重复产出事件（reopen 应吸收 quota/state）
per_aid = {}
for k, a, _ in e2:
    per_aid.setdefault(a["aid"], []).append(k)
dup = {aid: ks for aid, ks in per_aid.items() if len(ks) > 1}
assert not dup, f"同一活动产生了多条事件: {dup}"
assert sent > 0 and sent2 == 0, "去重失败"
print("\n✅ 全部断言通过")
