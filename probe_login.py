"""探针4：找一个能直接账号密码登录的接口，绕开模拟器。
判断依据：不存在的路径返回 404 HTML；存在的返回 JSON（任何 code）。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import dmcrypto as C

UA_APP = json.dumps({
    "channelName": "appstore", "createTime": "1750000000000",
    "device": "Redmi Note 5", "hardware": "xiaomi/sdm660", "imei": "",
    "jPushId": "1a0018970bff627d740", "mac": "", "operator": "wifi",
    "packageName": "com.jingcai.apps.qualitydev", "screen": "1080*2160",
    "startTime": "1750000000000", "sysVersion": "11",
    "uuid": "a1b2c3d4e5f6478899aabbccddeeff00", "version": "4.9.5",
}, separators=(",", ":"))

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def app_post(path, extra=None, host="https://appdmkj.5idream.net"):
    p = dict(extra or {})
    p["timestamp"] = str(int(time.time() * 1000))
    p["signToken"] = C.sign_token(p)
    body = "d=" + urllib.parse.quote(C.make_d(p), safe="")
    req = urllib.request.Request(
        host + path, data=body.encode(),
        headers={"standardUA": UA_APP, "user-agent": "okhttp/3.11.0",
                 "content-type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read()[:180].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120].decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def h5_post(path, data, host="https://apph5.5idream.net"):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        host + path, data=body,
        headers={"user-agent": UA_BROWSER, "origin": host,
                 "referer": host + "/", "content-type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, r.read()[:180].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120].decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


print("=== A. App API 登录路径扫描（只看是否为 JSON：404 HTML = 不存在）===")
app_paths = [
    "/v2/user/login", "/v2/user/loginByPwd", "/v2/user/pwd/login", "/v2/login",
    "/v2/login/account", "/v2/user/account/login", "/v1/user/login",
    "/user/login", "/login", "/v2/user/signin", "/v2/signin",
    "/v2/user/passwordLogin", "/v2/passport/login", "/v2/user/login_password",
    "/v3/user/loginByPwd", "/v2/user/loginV2", "/v2/account/login",
    "/v2/user/mobileLogin", "/v2/user/phoneLogin", "/v2/user/codeLogin",
]
for p in app_paths:
    st, body = app_post(p, {"account": "13800000000", "password": "Test1234",
                            "loginName": "13800000000", "phone": "13800000000"})
    kind = "JSON" if body.strip().startswith("{") else ("404" if "404" in body else "?")
    print(f"  {kind:4s} {st} {p:34s} {body[:80]}")
    time.sleep(0.7)

print("\n=== B. H5 站接口扫描 ===")
h5_paths = [
    "/apih5/user/login", "/apih5/login", "/apih5/user/pwdlogin",
    "/apih5/share/content", "/apih5/user/info", "/apih5/activity/list",
    "/apih5/index", "/apih5/user/loginByPwd", "/apih5/captcha",
]
for p in h5_paths:
    st, body = h5_post(p, {"account": "13800000000", "password": "Test1234",
                           "share": "E3FC243F08B44E61AA7E6758487AF594"})
    kind = "JSON" if body.strip().startswith("{") else ("404" if "404" in body else "?")
    print(f"  {kind:4s} {st} {p:28s} {body[:80]}")
    time.sleep(0.7)
