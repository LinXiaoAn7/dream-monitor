"""rules —— 活动归一化 + 条件匹配。

到梦空间各接口返回的字段名并不统一，这里用多候选名容错提取；
未知字段一律保留在 raw 里，`probe` 命令可完整 dump。
"""

from __future__ import annotations

import re
import time
from typing import Any

# 状态码 -> 归一化状态（默认映射，可在 config.status_map 覆盖）
DEFAULT_STATUS_MAP = {
    1: "soon",      # 未开始报名
    2: "soon",
    3: "open",      # 报名中
    4: "running",   # 进行中
    5: "ended",     # 已结束
    6: "ended",
    7: "ended",
}

STATE_LABEL = {
    "open": "报名中",
    "full": "已满员",
    "soon": "未开始",
    "running": "进行中",
    "ended": "已结束",
    "unknown": "未知",
}


def _pick(d: Any, *names, default=None):
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", [], {}):
            return v
    return default


def _int(v, default: int = 0) -> int:
    try:
        if v is None or v == "" or str(v).strip() in ("不限", "null", "None"):
            return default
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def _ts(v) -> float | None:
    """时间戳可能是毫秒/秒/日期字符串。"""
    if v in (None, "", 0, "0"):
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{13}", s):
        return int(s) / 1000.0
    if re.fullmatch(r"\d{10}", s):
        return float(s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", s):
        try:
            return time.mktime(time.strptime(s.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"
                                             if len(s) >= 19 else "%Y-%m-%d %H:%M"))
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return time.mktime(time.strptime(s, "%Y-%m-%d"))
        except ValueError:
            return None
    return None


def normalize(a: dict, status_map: dict | None = None) -> dict:
    smap = {int(k): v for k, v in (status_map or DEFAULT_STATUS_MAP).items()}

    aid = str(_pick(a, "activityId", "id", "activityid", "actId", default=""))
    name = str(_pick(a, "activityName", "name", "title", "actName", default="(无标题)"))

    joined_raw = _pick(a, "joinNum", "joinUserNum", "enrollNum", "signupNum",
                       "joinCount", "alreadyJoinNum")
    joined_known = joined_raw is not None and str(joined_raw).strip() not in ("", "None")
    joined = _int(joined_raw, 0)
    cap_raw = _pick(a, "joinmaxnum", "activityMaxNum", "maxNum", "quota",
                    "joinMaxNum", "limitNum", "personNum")
    cap = _int(cap_raw, -1)
    if cap_raw is None or str(cap_raw).strip() == "":
        quota = -2          # -2 = 详情还没拿到，名额未知（不是“不限”）
    elif str(cap_raw).strip() == "不限":
        quota = -1
    elif not joined_known:
        quota = -2          # 有上限但没拿到已报人数，算不出剩余
    else:
        quota = max(cap - joined, 0) if cap > 0 else -1

    status_raw = _int(_pick(a, "status", "activityStatus", "state", "statusCode"), -1)

    sign_start = _ts(_pick(a, "joinstartdate", "signupStartTime", "signStartTime",
                           "applyStartTime", "enrollStartTime", "registrationStartTime"))
    sign_end = _ts(_pick(a, "joinenddate", "signupEndTime", "signEndTime",
                         "applyEndTime", "enrollEndTime", "registrationEndTime"))
    act_start = _ts(_pick(a, "startdateDate", "activityStartTime", "startTime", "beginTime"))
    act_end = _ts(_pick(a, "enddate", "activityEndTime", "endTime", "finishTime"))

    now = time.time()
    if status_raw in smap:
        state = smap[status_raw]
    elif sign_start and now < sign_start:
        state = "soon"
    elif sign_end and now > sign_end:
        state = "running" if (act_end and now < act_end) else "ended"
    elif sign_start and sign_end:
        state = "open"
    else:
        state = "unknown"

    # 名额修正：报名中但没位了 -> full
    if state == "open" and quota == 0:
        state = "full"

    return {
        "aid": aid,
        "name": name,
        "state": state,
        "status_raw": status_raw,
        "quota": quota,
        "joined": joined,
        "capacity": cap,
        "category": str(_pick(a, "catalog2name", "categoryName", "sourceTypeName", "typeName",
                              "activityTypeName", "classifyName", default="") or ""),
        "organizer": str(_pick(a, "schoolname", "createUnitName", "organizer", "unitName",
                               "publishUnitName", "departmentName", default="") or ""),
        "place": str(_pick(a, "address", "place", "location", "venue", default="") or ""),
        # 报名状态（来自详情接口；列表项没有这些字段时为 None）
        "can_join": bool(_pick(a, "canJoin", default=False)),
        "can_cancel": bool(_pick(a, "cancel", "joinCanCancel", default=False)),
        "signup_id": str(_pick(a, "signUpId", default="") or ""),
        # 只看 signUpId：详情接口在「没报名」时也会返回 injoin=true，不能信
        "joined_by_me": str(_pick(a, "signUpId", "signupId", default="0") or "0")
        not in ("", "0", "None", "null"),
        "credit": str(_pick(a, "credit", "score", "creditScore", "integral", default="") or ""),
        "sign_start": sign_start,
        "sign_end": sign_end,
        "act_start": act_start,
        "act_end": act_end,
        "url": f"https://apph5.5idream.net/share/activity?share="
               f"{_pick(a, 'shareCode', 'share', default='')}" if _pick(a, "shareCode", "share") else "",
        "raw": a,
    }


def fmt_ts(t: float | None) -> str:
    if not t:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def describe(act: dict) -> str:
    q = "—" if act["quota"] == -2 else ("不限" if act["quota"] < 0 else str(act["quota"]))
    parts = [
        f"{act['name']}",
        f"状态={STATE_LABEL.get(act['state'], act['state'])}({act['status_raw']})",
        f"余量={q}",
        f"已报={act['joined']}",
        f"报名截止={fmt_ts(act['sign_end'])}",
    ]
    if act["category"]:
        parts.append(f"类别={act['category']}")
    if act["organizer"]:
        parts.append(f"主办={act['organizer']}")
    if act["credit"]:
        parts.append(f"学分={act['credit']}")
    return " | ".join(parts)


def match(act: dict, rules: dict) -> tuple[bool, str]:
    """返回 (是否命中, 命中原因)。rules 为空则全部命中。"""
    if not rules:
        return True, "no-rules"

    # 状态闸门
    allow_states = rules.get("states") or ["open", "full", "soon"]
    if act["state"] not in allow_states:
        return False, f"状态 {act['state']} 不在允许集"

    # 有名额才要
    if rules.get("require_quota") and act["quota"] == 0:
        return False, "无名额"

    # 关键字
    inc = [k.lower() for k in (rules.get("include") or []) if k]
    exc = [k.lower() for k in (rules.get("exclude") or []) if k]
    blob = f"{act['name']} {act['category']} {act['organizer']}".lower()
    if inc and not any(k in blob for k in inc):
        return False, "无命中关键字"
    if exc and any(k in blob for k in exc):
        return False, "命中排除词"

    # 类别 / 主办方
    cats = [c.lower() for c in (rules.get("categories") or []) if c]
    if cats and not any(c in act["category"].lower() for c in cats):
        return False, "类别不符"
    orgs = [o.lower() for o in (rules.get("organizers") or []) if o]
    if orgs and not any(o in act["organizer"].lower() for o in orgs):
        return False, "主办方不符"

    # 报名截止至少还剩 N 小时
    min_left = rules.get("min_hours_left")
    if min_left and act["sign_end"]:
        left_h = (act["sign_end"] - time.time()) / 3600
        if left_h < float(min_left):
            return False, "距截止过近"

    return True, "matched"
