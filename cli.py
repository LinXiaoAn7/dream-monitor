#!/usr/bin/env python
"""到梦空间活动监控 —— 命令行入口。

用法：
  python cli.py init                 生成 config.yaml
  python cli.py login                交互式写入 uid/token 并验证
  python cli.py probe                dump 原始响应，校准字段映射
  python cli.py once                 跑一轮
  python cli.py run                  持续监控
  python cli.py test-notify          测试推送渠道
  python cli.py events               查看最近状态事件
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import yaml

CONFIG = os.environ.get("DM_CONFIG", "config.yaml")
EXAMPLE = "config.example.yaml"

# Windows 控制台默认 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def require_credentials(api: dict) -> None:
    missing = [k for k in ("uid", "token") if not str(api.get(k, "")).strip()]
    if missing:
        sys.exit(f"配置缺少 {'/'.join(missing)}。先运行 `python cli.py login` 填入抓包结果。")


def setup_log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_cfg() -> dict:
    if not os.path.exists(CONFIG):
        sys.exit(f"找不到 {CONFIG}，先运行：python cli.py init")
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    api = cfg.setdefault("api", {})
    # 环境变量优先级最高，便于 CI / 临时覆盖
    api["uid"] = os.environ.get("DM_UID") or api.get("uid", "")
    api["token"] = os.environ.get("DM_TOKEN") or api.get("token", "")
    api["jPushId"] = os.environ.get("DM_JPUSH") or api.get("jPushId", "")
    cfg.setdefault("db", "data/dm.db")
    return cfg


def save_cfg(cfg: dict) -> None:
    with open(CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


# ------------------------------------------------------------------ 命令
def cmd_init(a) -> None:
    if os.path.exists(CONFIG):
        print(f"{CONFIG} 已存在，跳过（删除后重试可重新生成）")
        return
    if not os.path.exists(EXAMPLE):
        sys.exit(f"缺少模板 {EXAMPLE}")
    with open(EXAMPLE, encoding="utf-8") as f:
        text = f.read()
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已生成 {CONFIG}")
    print("下一步：python cli.py login   （填入抓包得到的 uid / token）")


def cmd_login(a) -> None:
    cfg = load_cfg()
    api = cfg["api"]

    from dmclient import ApiError, DreamClient

    account = (os.environ.get("DM_ACCOUNT") or api.get("account") or "").strip()
    password = (os.environ.get("DM_PASSWORD") or api.get("password") or "").strip()

    if not account:
        account = input("到梦空间手机号: ").strip()
    if not password:
        try:
            import getpass
            password = getpass.getpass("密码（不回显）: ").strip()
        except Exception:  # noqa: BLE001
            password = input("密码: ").strip()

    if not account or not password:
        sys.exit("手机号和密码都得填。")

    print(f"→ 正在登录 {account} ...")
    try:
        info = DreamClient(api).login_phone(account, password)
    except ApiError as e:
        sys.exit(f"❌ 登录失败 [{e.code}] {e.msg}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"❌ 登录异常 {type(e).__name__}: {e}")

    print(f"✅ 登录成功：{info.get('name') or '(未返回昵称)'}")
    print(f"   uid   = {info['uid']}")
    print(f"   token = {info['token']}")
    note = f"密码 {info['pwd_bytes']} 字节"
    if info["truncated"]:
        note += " → 超过 8 字节，已截断到前 8 字节（服务端只认单块 DES）"
    print(f"   {note}")

    api.update(uid=info["uid"], token=info["token"], account=account, password=password)
    save_cfg(cfg)
    print(f"   已写入 {CONFIG}")

    ok, msg = DreamClient(api).ping()
    print(("✅ " if ok else "❌ ") + "凭据校验：" + msg)
    if ok:
        print("\n下一步：python cli.py probe  （看字段对不对）")


def cmd_probe(a) -> None:
    cfg = load_cfg()
    require_credentials(cfg["api"])
    cfg["dump_raw"] = True
    from dmclient import DreamClient, find_list
    import rules as R
    c = DreamClient(cfg["api"])
    print("→ 请求活动列表 ...")
    data = c.activities(page=1, page_size=int(a.size))
    with open("data/raw_page1.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("原始响应 → data/raw_page1.json")

    items = find_list(data) or []
    print(f"\n识别到 {len(items)} 条活动，归一化结果：")
    for it in items[:int(a.show)]:
        act = R.normalize(it, cfg.get("status_map"))
        print(" ", R.describe(act))
        print("   aid =", act["aid"])
    if items:
        print("\n首条原始字段名：")
        print(" ", sorted(items[0].keys()))
        print("\n若上面显示 (无标题)/余量错乱，按这些字段名改 rules.py 里的候选列表。")


def cmd_once(a) -> None:
    cfg = load_cfg()
    require_credentials(cfg["api"])
    from monitor import Monitor
    m = Monitor(cfg)
    sent, text = m.tick()
    print("\n" + text)


def cmd_run(a) -> None:
    cfg = load_cfg()
    require_credentials(cfg["api"])
    from monitor import Monitor
    Monitor(cfg).run_forever()


def cmd_test_notify(a) -> None:
    cfg = load_cfg()
    from notify import Notifier
    res = Notifier(cfg.get("notify") or {}).send(
        "到梦空间监控测试",
        "这是一条测试消息。收到即表示渠道配置正确。\n时间：" + __import__("datetime").datetime.now().strftime("%F %T"),
        "https://apph5.5idream.net/")
    print("\n渠道结果：", json.dumps(res, ensure_ascii=False, indent=2))


def cmd_events(a) -> None:
    from store import Store
    s = Store(load_cfg().get("db", "data/dm.db"))
    rows = s.recent_events(int(a.limit))
    if not rows:
        print("暂无事件记录")
        return
    import time as _t
    for r in rows:
        ts = _t.strftime("%m-%d %H:%M:%S", _t.localtime(r["ts"]))
        print(f"{ts}  [{r['kind']:6s}] {r['name']}  —  {r['detail']}")


# ------------------------------------------------------------------ main
def main() -> None:
    setup_log()
    p = argparse.ArgumentParser(description="到梦空间活动监控")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="生成 config.yaml").set_defaults(fn=cmd_init)
    sub.add_parser("login", help="写入并验证凭据").set_defaults(fn=cmd_login)

    sp = sub.add_parser("probe", help="dump 原始响应，校准字段")
    sp.add_argument("--size", default=5, help="每页条数")
    sp.add_argument("--show", default=5, help="打印条数")
    sp.set_defaults(fn=cmd_probe)

    sub.add_parser("once", help="跑一轮").set_defaults(fn=cmd_once)
    sub.add_parser("run", help="持续监控").set_defaults(fn=cmd_run)
    sub.add_parser("test-notify", help="测试推送").set_defaults(fn=cmd_test_notify)

    sp = sub.add_parser("events", help="最近事件")
    sp.add_argument("--limit", default=30)
    sp.set_defaults(fn=cmd_events)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
