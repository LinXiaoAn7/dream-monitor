# -*- coding: utf-8 -*-
"""watch —— 到梦空间活动实时监控 / 名额捡漏 / 状态变更通知。

完全不依赖登录态，数据源是活动分享接口（见 share.py）。

用法：
    python watch.py add "https://apph5.5idream.net/share/activity?share=XXXX"
    python watch.py ls
    python watch.py show "https://apph5.5idream.net/share/activity?share=XXXX"
    python watch.py rm XXXX
    python watch.py run                 # 常驻监控
    python watch.py run --interval 10   # 临时改轮询间隔

配置文件 config.yaml 里的 watch 段：
    watch:
      interval: 15              # 轮询秒数
      close_soon_hours: 6       # 报名截止前多少小时提醒
      codes: [XXXX, YYYY]       # 监控的分享码
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

import yaml

from share import (extract_code, fetch, normalize, describe, hours_left,
                   SHARE_PAGE)
from notify import Notifier

CONFIG = os.environ.get("DM_CONFIG", "config.yaml")
STATE_PATH = os.path.join("data", "watch_state.json")

DEFAULT_WATCH = {"interval": 15, "close_soon_hours": 6, "codes": []}


# ------------------------------------------------------------------ 配置 / 状态
def load_cfg() -> dict:
    if not os.path.exists(CONFIG):
        sys.exit(f"找不到 {CONFIG}")
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_cfg(cfg: dict) -> None:
    with open(CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def watch_cfg(cfg: dict) -> dict:
    w = cfg.setdefault("watch", {})
    for k, v in DEFAULT_WATCH.items():
        w.setdefault(k, v)
    w["codes"] = [extract_code(c) for c in (w.get("codes") or [])]
    return w


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _snapshot(n: dict) -> dict:
    return {
        "name": n["name"],
        "status": n["status"],
        "status_text": n["status_text"],
        "joined": n["joined"],
        "maxnum": n["maxnum"],
        "remain": n["remain"],
        "unlimited": n["unlimited"],
        "can_join": n["can_join"],
        "unable_reason": n["unable_reason"],
        "signup_end": n["signup_end"].isoformat() if n["signup_end"] else "",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ 事件判定
def diff(prev: dict | None, cur: dict, close_soon_hours: float) -> list[tuple[str, str]]:
    """返回 [(事件类型, 说明)]。prev 为 None 表示首次纳入。"""
    ev: list[tuple[str, str]] = []
    if prev is None:
        ev.append(("watch", "已纳入监控"))
        return ev

    # 1. 状态文字变化
    if prev.get("status_text") != cur["status_text"]:
        ev.append(("status", f"状态变化：{prev.get('status_text') or prev.get('status')} → "
                             f"{cur['status_text'] or cur['status']}"))

    # 2. 名额
    pr, cr = prev.get("remain"), cur["remain"]
    if pr is not None and cr is not None:
        if pr <= 0 < cr:
            ev.append(("quota", f"有位置了！剩余名额 {pr} → {cr}"))
        elif pr > 0 >= cr:
            ev.append(("full", f"名额已满（{cur['joined']}/{cur['maxnum']}）"))
        elif cr > pr:
            ev.append(("quota", f"名额增加：剩余 {pr} → {cr}"))

    # 3. 可报名开关
    if not prev.get("can_join") and cur["can_join"]:
        ev.append(("open", "现在可以报名了"))

    # 4. 报名即将截止
    left = hours_left(cur["signup_end"])
    if left is not None and 0 <= left <= close_soon_hours:
        prev_left = None
        if prev.get("signup_end"):
            try:
                prev_left = hours_left(datetime.fromisoformat(prev["signup_end"]))
            except ValueError:
                prev_left = None
        # 只在首次进入窗口时提醒一次
        if prev_left is None or prev_left > close_soon_hours:
            ev.append(("closing", f"报名将在 {left:.1f} 小时后截止"))

    return ev


# ------------------------------------------------------------------ 单轮
def step(codes: list[str], close_soon_hours: float, state: dict,
         notifier: Notifier, verbose: bool = True) -> int:
    """跑一轮，返回本轮触发的事件数。"""
    fired = 0
    for code in codes:
        try:
            n = normalize(fetch(code), code)
        except Exception as e:                       # noqa: BLE001
            print(f"  [!] {code} 拉取失败: {type(e).__name__}: {e}")
            continue

        prev = state.get(code)
        events = diff(prev, n, close_soon_hours)
        state[code] = _snapshot(n)

        if verbose and not events:
            print(f"  · {describe(n)}")
            continue

        for kind, detail in events:
            fired += 1
            print(f"  [{kind}] {n['name']} — {detail}")
            if kind == "watch":
                continue                             # 首次纳入只记录，不推送
            title = {
                "quota": "名额捡漏",
                "full": "名额已满",
                "open": "开始报名",
                "status": "活动状态变化",
                "closing": "报名即将截止",
            }.get(kind, "活动提醒")
            body = (f"{n['name']}\n{detail}\n"
                    f"报名时间：{n['join_window']}\n"
                    f"活动时间：{n['run_window']}\n"
                    f"地点：{n['address']}\n"
                    f"名额：{n['joined']}/{n['maxnum'] if not n['unlimited'] else '不限'}"
                    + (f"\n不可报名原因：{n['unable_reason']}" if n["unable_reason"] else ""))
            notifier.send(f"【{title}】{n['name']}", body, n["url"])
            if kind in ("quota", "open"):
                print(f"      >>> 快去报名：{n['url']}")
    return fired


# ------------------------------------------------------------------ 子命令
def cmd_add(a) -> None:
    cfg = load_cfg()
    w = watch_cfg(cfg)
    added = []
    for raw in a.codes:
        try:
            code = extract_code(raw)
        except ValueError as e:
            print(f"  [!] {e}")
            continue
        if code in w["codes"]:
            print(f"  = 已存在 {code}")
            continue
        # 立刻验证一次
        try:
            n = normalize(fetch(code), code)
            print(f"  + {code}  {n['name']}")
            print(f"      {describe(n)}")
        except Exception as e:
            print(f"  [!] {code} 验证失败（仍然写入）: {e}")
        w["codes"].append(code)
        added.append(code)
    save_cfg(cfg)
    if added:
        print(f"\n已添加 {len(added)} 个活动，当前共 {len(w['codes'])} 个。")


def cmd_rm(a) -> None:
    cfg = load_cfg()
    w = watch_cfg(cfg)
    code = extract_code(a.code)
    if code in w["codes"]:
        w["codes"].remove(code)
        save_cfg(cfg)
        print(f"已移除 {code}，剩余 {len(w['codes'])} 个。")
    else:
        print(f"没找到 {code}")


def cmd_ls(a) -> None:
    cfg = load_cfg()
    w = watch_cfg(cfg)
    if not w["codes"]:
        print("监控列表为空。用 add 添加分享链接。")
        return
    print(f"共 {len(w['codes'])} 个，轮询间隔 {w['interval']}s\n")
    for code in w["codes"]:
        try:
            n = normalize(fetch(code), code)
            print(f"  {describe(n)}")
            print(f"    {n['url']}")
        except Exception as e:
            print(f"  [!] {code} 拉取失败: {e}")


def cmd_show(a) -> None:
    code = extract_code(a.code)
    n = normalize(fetch(code), code)
    print("=" * 70)
    print(f"活动：{n['name']}")
    print(f"ID  ：{n['aid']}    分享码：{code}")
    print(f"状态：{n['status_text']} (status={n['status']})  {n['countdown_text']}")
    print(f"名额：{n['joined']}/{n['maxnum'] if not n['unlimited'] else '不限'}"
          + (f"  剩余 {n['remain']}" if n['remain'] is not None else ""))
    print(f"报名：{n['join_window']}")
    print(f"活动：{n['run_window']}")
    print(f"学校：{n['school']} / {n['college']}")
    print(f"部落：{n['tribe']}  {n['tribe_type']}")
    print(f"级别：{n['level_text']}   范围：{n['join_range']}   标签：{n['label']}")
    print(f"地点：{n['address']}")
    print(f"可报名：{n['can_join']}   ableFlag：{n['able_flag']}")
    if n["unable_reason"]:
        print(f"不可报名：{n['unable_reason']}")
    left = hours_left(n["signup_end"])
    if left is not None:
        print(f"距报名截止：{left:.2f} 小时")
    print(f"报名方式：{n['join_way'].replace(chr(10), ' ')}")
    print(f"链接：{n['url']}")
    print("-" * 70)
    print(n["content"])
    print("=" * 70)


def cmd_run(a) -> None:
    cfg = load_cfg()
    w = watch_cfg(cfg)
    if not w["codes"]:
        sys.exit("监控列表为空，先 python watch.py add <分享链接>")
    interval = a.interval or w["interval"]
    close_h = w["close_soon_hours"]

    notifier = Notifier(cfg.get("notify") or {})
    state = load_state()

    print(f"== 到梦空间活动监控 ==")
    print(f"活动数 {len(w['codes'])}，间隔 {interval}s，报名截止提醒阈值 {close_h}h")
    print(f"按 Ctrl+C 停止\n")

    round_no = 0
    try:
        while True:
            round_no += 1
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] 第 {round_no} 轮")
            fired = step(w["codes"], close_h, state, notifier, verbose=(round_no <= 1))
            save_state(state)
            if fired:
                print(f"  本轮触发 {fired} 个事件")
            time.sleep(interval)
    except KeyboardInterrupt:
        save_state(state)
        print("\n已停止，状态已保存。")


# ------------------------------------------------------------------ 入口
def main() -> None:
    p = argparse.ArgumentParser(description="到梦空间活动监控 / 名额捡漏")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="添加活动分享链接")
    s.add_argument("codes", nargs="+")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("rm", help="移除活动")
    s.add_argument("code")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("ls", help="列出监控中的活动")
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("show", help="看单个活动详情")
    s.add_argument("code")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("run", help="常驻监控")
    s.add_argument("--interval", type=int, default=0)
    s.set_defaults(fn=cmd_run)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
