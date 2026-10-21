# -*- coding: utf-8 -*-
"""接口路径探测：用假账号打登录类接口，靠错误码区分「接口存在」与「404」。

判定依据：
    1200 / 900xxxxx / 11002  -> 接口存在（进到业务层了）
    404 / BADJSON / 网关兜底  -> 不存在

不碰真实账号，不消耗登录次数。
"""
import sys, time, json
sys.stdout.reconfigure(encoding="utf-8")

import yaml
from dmclient import DreamClient, ApiError, AuthExpired

FAKE_ACC = "13800000000"
FAKE_PWD = "x" * 8

PATHS = [
    # ---- 登录类 ----
    "/v2/login/phone", "/v2/login/sms", "/v2/login/code", "/v2/login/smscode",
    "/v2/login/phonecode", "/v2/login/onekey", "/v2/login/wechat", "/v2/login/qq",
    "/v2/login/token", "/v2/login/refresh", "/v2/login/logout", "/v2/login/check",
    "/v2/login/verify", "/v2/login/msg", "/v2/login/message", "/v2/login/captcha",
    "/v3/login/phone", "/v3/login/sms", "/v3/login/code", "/v4/login/phone",
    "/v2/passport/login", "/v2/passport/sms", "/v2/passport/code",
    "/v2/account/login", "/v2/account/sms", "/v2/account/code",
    "/v2/user/login", "/v2/user/smslogin", "/v2/user/codelogin",
    # ---- 验证码 ----
    "/v2/sms/send", "/v2/sms/code", "/v2/sms/sendcode", "/v2/sms/verify",
    "/v2/code/send", "/v2/code/sms", "/v2/verify/send", "/v2/verify/code",
    "/v2/message/send", "/v2/message/code", "/v2/captcha/send",
    "/v2/user/sendcode", "/v2/user/sms", "/v2/user/sendmsg",
    # ---- 账号信息 ----
    "/v3/user/account/summary", "/v2/user/account/summary", "/v2/user/info",
    "/v2/user/detail", "/v2/user/profile", "/v2/user/me",
    # ---- 活动类（已知存在，作对照）----
    "/v2/activity/activities",
    # ---- 其它可能 ----
    "/v2/config/version", "/v2/app/version", "/v2/common/config",
    "/v2/index/promotionlist", "/v2/index/config",
]


def main():
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    cli = DreamClient(cfg)
    cli.delay = 0.45

    ok, bad = [], []
    for p in PATHS:
        try:
            cli._post(p, {"account": FAKE_ACC, "pwd": FAKE_PWD, "phone": FAKE_ACC,
                          "mobile": FAKE_ACC, "code": "123456", "smsCode": "123456",
                          "type": "1"}, auth=False)
            ok.append((p, "OK-200", ""))
            print(f"  ✅ {p:<36} 200 正常返回")
        except AuthExpired as e:
            ok.append((p, e.code, str(e)[:50]))
            print(f"  ✅ {p:<36} {e.code} {str(e)[:40]}")
        except ApiError as e:
            if e.code in ("BADJSON",) or "404" in str(e) or "<!DOCTYPE" in str(e.payload or ""):
                bad.append(p)
                print(f"  ·  {p:<36} 不存在")
            else:
                ok.append((p, e.code, e.msg))
                print(f"  ✅ {p:<36} {e.code} {e.msg[:40]}")
        except Exception as e:
            bad.append(p)
            print(f"  !  {p:<36} {type(e).__name__}")
        time.sleep(0.05)

    print("\n" + "=" * 70)
    print(f"存在 {len(ok)} 个 / 不存在 {len(bad)} 个")
    print("\n【存在】")
    for p, c, m in ok:
        print(f"  {p:<36} {c:<10} {m}")
    print("\n【不存在】")
    for p in bad:
        print(f"  {p}")

    json.dump({"exists": ok, "missing": bad}, open("data/scan_api.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
