# -*- coding: utf-8 -*-
"""到梦空间活动分享接口封装（无需登录）。

数据源：
    POST https://apph5.5idream.net/apih5/share/content
    body: share=<32位分享码>

返回 data.activity 里带全部关键字段：status / statusText / joinNum / joinmaxnum /
joindate（报名时间窗）/ startdate / schoolname / collegename / tribeVo /
canJoin / ableJoinFlag / unableJoinReason / countdownText 等。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SHARE_API = "https://apph5.5idream.net/apih5/share/content"
SHARE_PAGE = "https://apph5.5idream.net/share/activity?share={code}"

HEADERS = {
    "origin": "https://apph5.5idream.net",
    "referer": "https://apph5.5idream.net/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}

_CODE_RE = re.compile(r"[?&]share=([0-9A-Fa-f]{16,64})")
_BARE_RE = re.compile(r"^[0-9A-Fa-f]{16,64}$")


def extract_code(text: str) -> str:
    """从分享链接或裸码里取出分享码。"""
    text = (text or "").strip()
    m = _CODE_RE.search(text)
    if m:
        return m.group(1).upper()
    if _BARE_RE.match(text):
        return text.upper()
    # 兜底：链接里任意一段 32 位 hex
    m = re.search(r"[0-9A-Fa-f]{32}", text)
    if m:
        return m.group(0).upper()
    raise ValueError(f"无法识别分享码: {text[:80]}")


def fetch(code: str, timeout: float = 20.0, retries: int = 2) -> dict[str, Any]:
    """拉一个活动的数据，返回 data.activity。"""
    code = extract_code(code)
    last = None
    for i in range(retries + 1):
        try:
            r = requests.post(SHARE_API, headers=HEADERS, data={"share": code},
                              timeout=timeout, verify=False)
            r.raise_for_status()
            j = r.json()
            a = (j.get("data") or {}).get("activity")
            if not a:
                raise RuntimeError(f"分享码 {code} 无数据: {str(j)[:160]}")
            return a
        except Exception as e:            # 网络抖动重试
            last = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"拉取 {code} 失败: {type(last).__name__}: {last}")


def _parse_window(s: str) -> tuple[datetime | None, datetime | None]:
    """解析 "2026.10.15 10:00-2026.10.20 18:00" 这类时间窗。"""
    if not s:
        return None, None
    s = s.strip().replace("～", "-").replace("~", "-")
    parts = re.split(r"\s*-\s*", s)
    if len(parts) < 2:
        return None, None

    def _p(x: str) -> datetime | None:
        for fmt in ("%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M", "%Y.%m.%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(x.strip(), fmt)
            except ValueError:
                continue
        return None

    return _p(parts[0]), _p(parts[1])


def _int(v, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def normalize(a: dict[str, Any], code: str = "") -> dict[str, Any]:
    """把原始 activity 压成监控用的字段。"""
    joined = _int(a.get("joinNum"))
    raw_max = a.get("joinmaxnum", a.get("maxnum"))
    unlimited = str(raw_max).strip() in ("不限", "0", "", "None")
    maxnum = None if unlimited else _int(raw_max)
    remain = None if unlimited else max(0, (maxnum or 0) - joined)

    j_start, j_end = _parse_window(a.get("joindate", ""))
    s_start, s_end = _parse_window(a.get("startdate", ""))

    tribe = a.get("tribeVo") or {}
    return {
        "code": code or "",
        "aid": str(a.get("activityId", "")),
        "name": a.get("activityName", ""),
        "status": str(a.get("status", "")),
        "status_text": a.get("statusText", ""),
        "joined": joined,
        "maxnum": maxnum,
        "remain": remain,
        "unlimited": unlimited,
        "can_join": bool(a.get("canJoin")),
        "able_flag": bool(a.get("ableJoinFlag")),
        "unable_reason": a.get("unableJoinReason") or "",
        "countdown_text": a.get("countdownText") or "",
        "join_window": a.get("joindate") or "",
        "signup_start": j_start,
        "signup_end": j_end,
        "run_window": a.get("startdate") or "",
        "run_start": s_start,
        "run_end": s_end,
        "school": a.get("schoolname") or "",
        "college": a.get("collegename") or "",
        "tribe": tribe.get("name") or "",
        "tribe_type": tribe.get("typeDesc") or "",
        "level_text": a.get("levelText") or "",
        "join_range": a.get("joinrangeText") or "",
        "label": a.get("labelname") or "",
        "join_way": a.get("joinWayDesc") or "",
        "address": a.get("address") or "",
        "content": (a.get("content") or "")[:400],
        "url": SHARE_PAGE.format(code=code) if code else "",
        "raw": a,
    }


def hours_left(dt: datetime | None) -> float | None:
    if not dt:
        return None
    return (dt - datetime.now()).total_seconds() / 3600.0


def describe(n: dict[str, Any]) -> str:
    """单行摘要。"""
    if n["unlimited"]:
        quota = f"{n['joined']}/不限"
    else:
        quota = f"{n['joined']}/{n['maxnum']}（余 {n['remain']}）"
    bits = [
        f"[{n['status_text'] or n['status']}]",
        n["name"],
        quota,
        f"报名 {n['join_window']}" if n["join_window"] else "",
        f"{n['school']}" if n["school"] else "",
        f"{n['tribe']}" if n["tribe"] else "",
    ]
    if n["unable_reason"]:
        bits.append(f"不可报名: {n['unable_reason']}")
    return "  ".join(b for b in bits if b)
