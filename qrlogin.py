# -*- coding: utf-8 -*-
"""到梦空间 Web 扫码登录 -> 拿 token / uid -> 写回 config.yaml

流程：
    1. GET  /scan_login/getSecurityId          拿 securityId
    2. 本地渲染二维码（cv2 自带编码器），图片存 data/qr_login.png
    3. 用手机「到梦空间」App 扫码并确认
    4. POST /scan_login/rollPoling?securityId= 轮询，确认后返回 token
    5. GET  /token/checkToken?token=           换出用户信息（含 uid）

用法：
    python qrlogin.py            # 生成二维码并等待扫码（最长 180 秒）
    python qrlogin.py --again    # 重新生成
"""
import sys, os, json, time, base64, io
sys.stdout.reconfigure(encoding="utf-8")

import requests
import urllib3
import cv2
import numpy as np
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.5idream.net"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
QR_PNG = "data/qr_login.png"


def _hdr():
    return {"User-Agent": UA, "Referer": BASE + "/", "Accept": "application/json, text/plain, */*"}


def _json(resp):
    t = resp.text.lstrip("\ufeff").strip()
    # 兼容 JSONP: cb123({...})
    if t.startswith("cb") and "(" in t:
        t = t[t.index("(") + 1: t.rindex(")")]
    return json.loads(t)


def get_security_id():
    r = requests.get(BASE + "/scan_login/getSecurityId", headers=_hdr(), timeout=20, verify=False)
    r.raise_for_status()
    d = _json(r)
    if not d.get("success"):
        raise RuntimeError(f"获取 securityId 失败: {d}")
    res = d["result"]
    sid = str(res["securityId"])
    qr_url = f"{res['url']}?securityId={sid}"
    payload = json.dumps({"qrCode": qr_url, "qrType": str(res["qrType"])},
                         ensure_ascii=False, separators=(",", ":"))
    return sid, payload


def render_qr(payload: str, path: str, scale: int = 12, border: int = 4) -> str:
    """cv2 编码 -> 放大 -> 加白边 -> 存 PNG。"""
    enc = cv2.QRCodeEncoder_create()
    mat = enc.encode(payload)                 # 0/255 的方阵
    mat = cv2.resize(mat, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    mat = cv2.copyMakeBorder(mat, border * scale, border * scale, border * scale, border * scale,
                             cv2.BORDER_CONSTANT, value=255)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cv2.imwrite(path, mat)
    return path


def poll_once(sid: str):
    cb = f"cb{int(time.time() * 1000)}"
    r = requests.post(BASE + "/scan_login/rollPoling",
                      params={"securityId": sid, "callback": cb},
                      headers=_hdr(), timeout=20, verify=False)
    r.raise_for_status()
    try:
        d = _json(r)
    except json.JSONDecodeError:
        return None, None
    code = str(d.get("code", ""))
    result = d.get("result")
    # 未确认时后端返回 dummy / 500 / 90000017
    if result and result != "dummy" and code not in {"500", "90000017"}:
        return str(result), d
    return None, d


def check_token(token: str) -> dict:
    r = requests.get(BASE + "/token/checkToken", params={"token": token},
                     headers=_hdr(), cookies={"dmkj_web_token": token},
                     timeout=20, verify=False)
    r.raise_for_status()
    d = _json(r)
    if str(d.get("code")) != "100":
        raise RuntimeError(f"token 校验失败: {d}")
    return d.get("result") or {}


def save_cfg(uid: str, token: str, user: dict):
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    cfg["uid"] = str(uid)
    cfg["token"] = str(token)
    if user.get("name") or user.get("realName"):
        cfg["user_name"] = user.get("name") or user.get("realName")
    yaml.safe_dump(cfg, open("config.yaml", "w", encoding="utf-8"),
                   allow_unicode=True, sort_keys=False)
    return cfg


def main():
    wait = 180
    print("== 1. 申请登录二维码 ==")
    sid, payload = get_security_id()
    print(f"   securityId = {sid}")
    print(f"   payload    = {payload}")
    path = render_qr(payload, QR_PNG)
    print(f"   二维码已保存: {os.path.abspath(path)}")
    print()
    print("== 2. 用手机「到梦空间」App 扫码并确认登录 ==")
    print("   （在 App 里：我的 -> 扫一扫，或者首页右上角扫码）")
    print(f"   二维码 {wait} 秒内有效，我在后台轮询…")
    print()

    t0 = time.time()
    last = None
    while time.time() - t0 < wait:
        token, raw = poll_once(sid)
        if token:
            print(f"\n[+] 扫码成功！token = {token[:24]}…（共 {len(token)} 字符）")
            print("== 3. 换取用户信息 ==")
            try:
                user = check_token(token)
                print("   " + json.dumps(user, ensure_ascii=False)[:400])
                uid = user.get("id") or user.get("userId") or user.get("uid") or ""
                if not uid:
                    print("   [!] 返回里没找到 uid 字段，完整内容：")
                    print("   " + json.dumps(user, ensure_ascii=False, indent=2))
                    uid = input("   请手动输入 uid: ").strip()
                save_cfg(uid, token, user)
                print(f"\n[+] 已写入 config.yaml: uid={uid}")
                print("[+] 下一步: python cli.py probe")
            except Exception as e:
                print(f"   [!] checkToken 失败: {e}")
                print(f"   已拿到 token，请手动填入 config.yaml: {token}")
            return

        code = (raw or {}).get("code")
        if code and code != last:
            print(f"   …状态 code={code} msg={(raw or {}).get('msg','')}")
            last = code
        time.sleep(2)

    print("\n[-] 超时未扫码。重新运行 python qrlogin.py 可再生成一张。")


if __name__ == "__main__":
    main()
