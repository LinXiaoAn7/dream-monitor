#!/usr/bin/env python
"""登录尝试器 —— 账号被锁定后用来在解锁第一时间试对密码方案。

用法：
  python pwdtry.py plain              # 明文密码（最可能）
  python pwdtry.py plain --wait 700   # 等 700 秒再试
  python pwdtry.py des                # DES 零填充截断（已失败过）
  python pwdtry.py md5                # MD5 全 32 位（会被 11002 拒绝，仅诊断）

成功会打印 uid/token 并写回 config.yaml。
每次真正进入密码校验的请求消耗 1 次机会；11002 参数错误不消耗。
"""

from __future__ import annotations

import os
import sys
import time

import yaml

CONFIG = os.environ.get("DM_CONFIG", "config.yaml")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = args[0] if args else "plain"

    wait = 0
    for i, a in enumerate(sys.argv):
        if a == "--wait" and i + 1 < len(sys.argv):
            wait = int(sys.argv[i + 1])

    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    api = cfg.setdefault("api", {})

    account = (os.environ.get("DM_ACCOUNT") or api.get("account") or "").strip()
    password = (os.environ.get("DM_PASSWORD") or api.get("password") or "").strip()
    if not account or not password:
        print("config.yaml 里的 api.account / api.password 是空的")
        return 2

    if wait > 0:
        target = time.strftime("%H:%M:%S", time.localtime(time.time() + wait))
        print(f"等待 {wait} 秒（到 {target}）后尝试 mode={mode} ...", flush=True)
        time.sleep(wait)

    import dmclient as D

    print(f"[{time.strftime('%H:%M:%S')}] 尝试 mode={mode} account={account}", flush=True)
    c = D.DreamClient(api)
    try:
        info = c.login_phone(account, password, mode=mode)
    except D.ApiError as e:
        print(f"❌ 失败 [{e.code}] {e.msg}")
        if e.code == "90000025" or "限制" in (e.msg or ""):
            print("   仍在锁定期，等满 15 分钟再来。")
        elif e.code == "90000022":
            print("   密码不对，换一个 mode 再试。")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"❌ 异常 {type(e).__name__}: {e}")
        return 1

    print(f"✅ 成功！{info.get('name')}")
    print(f"   uid   = {info['uid']}")
    print(f"   token = {info['token']}")

    api.update(uid=info["uid"], token=info["token"], account=account,
               password=password, pwd_mode=mode)
    with open(CONFIG, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    print(f"   已写入 {CONFIG}，下一步：python cli.py probe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
