"""
dmclient —— 到梦空间 App API 客户端

协议：POST https://appdmkj.5idream.net/{path}
      Header: standardUA(设备指纹JSON) / user-agent: okhttp/3.11.0
      Body:   d=<加密参数>  (form-urlencoded)

所有业务参数都塞进加密体，没有 Authorization 头。
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import time
import uuid
from typing import Any

import requests

import dmcrypto as C

log = logging.getLogger("dmclient")

HOST = "https://appdmkj.5idream.net"
PATH_LOGIN_PHONE = "/v2/login/phone"
PATH_ACTIVITIES = "/v2/activity/activities"
PATH_DETAIL = "/v2/activity/detail"
PATH_SIGNUP_SUBMIT = "/v2/signup/submit"
PATH_SIGNUP_INFO = "/v2/signup/info"
PATH_SIGNUP_DETAIL = "/v2/signup/detail"
PATH_MINE_LIST = "/v2/activity/mime/list"
PATH_SIGNUP_CANCEL = "/v2/signup/cancel"
PATH_SCORE = "/v3/user/account/summary"

# 密码加密用的 DES 密钥（客户端硬编码）
DES_KEY = "51434574"


def des_encrypt(text: str, key: str = DES_KEY, trunc: bool = False) -> str:
    """调 node helper 做 DES-ECB 零填充（3DES 三密钥相同，等价 DES）。

    服务端只收单块 8 字节密文（16 hex），所以不用 PKCS5；
    trunc=True 时把密码截断到前 8 字节。
    """
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "des_helper.js")
    node = shutil.which("node") or "node"
    argv = [node, helper, key, text] + (["1"] if trunc else [])
    res = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    if res.returncode != 0:
        raise RuntimeError(f"DES helper 失败：{(res.stderr or '').strip()[:300]}")
    return res.stdout.strip()

# 业务码
CODE_OK = {"100"}
CODE_AUTH_EXPIRED = {"10005", "10004", "10007"}
CODE_BAD_SIGN = {"11001"}
CODE_BAD_PARAM = {"11002"}


class ApiError(Exception):
    def __init__(self, code: str, msg: str, payload: Any = None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.payload = payload


class AuthExpired(ApiError):
    pass


def _ua_json(cfg: dict) -> str:
    """构造 standardUA 设备指纹。缺字段用稳定默认值，保证同一设备复用。"""
    dev = cfg.get("device", {})
    profile = {
        "channelName": dev.get("channelName", "appstore"),
        "createTime": str(dev.get("createTime", "1750000000000")),
        "device": dev.get("device", "Redmi Note 5"),
        "hardware": dev.get("hardware", "xiaomi/sdm660"),
        "imei": "",
        "jPushId": cfg.get("jPushId", ""),
        "mac": "",
        "operator": dev.get("operator", "wifi"),
        "packageName": "com.jingcai.apps.qualitydev",
        "screen": dev.get("screen", "1080*2160"),
        "startTime": str(dev.get("startTime", "1750000000000")),
        "sysVersion": dev.get("sysVersion", "11"),
        "uuid": cfg.get("uuid") or uuid.uuid4().hex,
        "version": cfg.get("version", "4.9.5"),
    }
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"))


class DreamClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg.get("host", HOST)
        self.uid = str(cfg.get("uid", ""))
        self.token = str(cfg.get("token", ""))
        self.version = cfg.get("version", "4.9.5")
        self.jpush = cfg.get("jPushId", "")
        self.timeout = cfg.get("timeout", 20)
        self.delay = cfg.get("request_delay", 1.0)
        self.ua = _ua_json(cfg)
        self.sess = requests.Session()
        self.sess.headers.update({
            "standardUA": self.ua,
            "user-agent": "okhttp/3.11.0",
            "content-type": "application/x-www-form-urlencoded",
            "accept-encoding": "gzip",
        })
        self._last_call = 0.0
        self.stats = {"ok": 0, "err": 0}

    # ---------------------------------------------------------- 底层
    def _throttle(self):
        gap = self.delay * random.uniform(0.85, 1.2)
        wait = self._last_call + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _post(self, path: str, extra: dict | None = None, auth: bool = True,
              ts: bool = True) -> Any:
        params: dict[str, str] = {}
        if auth:
            params.update({
                "uid": self.uid,
                "token": self.token,
                "version": self.version,
            })
            if self.jpush:
                params["jPushId"] = self.jpush
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        if ts:
            params["timestamp"] = str(int(time.time() * 1000))
        params["signToken"] = C.sign_token(params)

        d = C.make_d(params)
        self._throttle()
        resp = self.sess.post(self.host + path, data={"d": d}, timeout=self.timeout)
        resp.raise_for_status()
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise ApiError("BADJSON", resp.text[:200])

        code = str(data.get("code", ""))
        if code in CODE_OK:
            self.stats["ok"] += 1
            return data.get("data")
        self.stats["err"] += 1
        msg = (data.get("actionSheet") or {}).get("content") or data.get("msg") or ""
        if code in CODE_AUTH_EXPIRED:
            raise AuthExpired(code, msg, data)
        raise ApiError(code, msg, data)

    # ---------------------------------------------------------- 登录
    def login_phone(self, account: str, password: str, raw_pwd: bool = False,
                    trunc: bool | None = None, mode: str | None = None) -> dict:
        """手机号 + 密码登录，返回 {uid, token, name, pwd_bytes, truncated}。

        明文参数表（不含 signToken）算签名，再把 signToken 塞进去整体加密。
        登录接口不接受 timestamp，带了会触发 11002。

        密码坑：服务端只认单块 8 字节的 DES 密文（16 hex）。密码 UTF-8 超过
        8 字节时必须截断，否则拼出 32 hex 会被判「参数错误 11002」。
        """
        pwd_bytes = len(password.encode("utf-8"))
        if mode is None:
            mode = "plain" if raw_pwd else "des"

        if mode == "plain":
            pwd = password
        elif mode == "md5":
            import hashlib
            pwd = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
        elif mode == "sha512_16":
            import hashlib
            pwd = hashlib.sha512(password.encode("utf-8")).hexdigest()[:16].upper()
        else:  # des
            if trunc is None:
                trunc = pwd_bytes > 8
            pwd = des_encrypt(password, trunc=trunc)
        base = {"account": str(account), "pwd": pwd, "version": self.version}
        base["signToken"] = C.sign_token(base)
        d = C.make_d(base)

        self._throttle()
        resp = self.sess.post(self.host + PATH_LOGIN_PHONE, data={"d": d}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        code = str(data.get("code", ""))
        if code not in CODE_OK:
            msg = (data.get("actionSheet") or {}).get("content") or data.get("msg") or ""
            raise ApiError(code, msg, data)
        out = data.get("data") or {}
        self.uid = str(out.get("uid", ""))
        self.token = str(out.get("token", ""))
        return {
            "uid": self.uid,
            "token": self.token,
            "name": out.get("name", ""),
            "pwd_bytes": pwd_bytes,
            "truncated": bool(trunc),
        }

    # ---------------------------------------------------------- 账号密码登录（长期方案）
    def login_account(self, account: str, password: str) -> dict:
        """用账号 + 明文密码直接换 token，不需要手机。

        要点（用服务端实际行为验证过）：
        - pwd 必须是 DES-ECB(key=51434574, PKCS5) 的 32 位大写十六进制
        - 参数表里必须带 uid / token 两个空串，否则 11002
        - 不能带 timestamp
        """
        from dm_des import encrypt_password

        base = {
            "uid": "",
            "token": "",
            "account": str(account),
            "pwd": encrypt_password(password),
            "version": self.version,
        }
        base["signToken"] = C.sign_token(base)
        d = C.make_d(base)

        self._throttle()
        resp = self.sess.post(self.host + PATH_LOGIN_PHONE, data={"d": d}, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        code = str(data.get("code", ""))
        if code not in CODE_OK:
            msg = (data.get("actionSheet") or {}).get("content") or data.get("msg") or ""
            raise ApiError(code, msg, data)
        out = data.get("data") or {}
        self.uid = str(out.get("uid", ""))
        self.token = str(out.get("token", ""))
        return {
            "uid": self.uid,
            "token": self.token,
            "name": out.get("displayName") or out.get("name", ""),
            "school": out.get("schoolName", ""),
            "college": out.get("collegeName", ""),
        }

    # ---------------------------------------------------------- 业务
    ACTIVITY_QUERY = {
        "catalogId": "", "catalogId2": "", "endTime": "", "joinEndTime": "",
        "joinFlag": "", "joinStartTime": "", "keyword": "", "level": "",
        "sort": "", "specialFlag": "", "startTime": "", "status": "",
    }

    def activities(self, page: int = 1, page_size: int | None = None, **kw) -> Any:
        """活动列表。服务端只认固定这套字段，多传一个未知键就是 11002。"""
        extra = dict(self.ACTIVITY_QUERY)
        extra["page"] = str(page)
        extra.update(kw)
        return self._post(PATH_ACTIVITIES, extra)

    def detail(self, activity_id: str) -> Any:
        """活动详情。注意：这个接口**不能**带 timestamp，带了就 11002。"""
        return self._post(PATH_DETAIL, {"activityId": str(activity_id)}, ts=False)

    def signup_info(self, activity_id: str) -> Any:
        return self._post(PATH_SIGNUP_INFO, {"activityId": str(activity_id)})

    def submit(self, activity_id: str, data: str = "", remark: str = "") -> Any:
        return self._post(PATH_SIGNUP_SUBMIT, {
            "activityId": str(activity_id), "data": data, "remark": remark,
        })

    def ping(self) -> tuple[bool, str]:
        """验证凭据是否有效。"""
        try:
            self.activities(page=1)
            return True, "token 有效"
        except AuthExpired as e:
            return False, f"token 失效：{e.msg}"
        except ApiError as e:
            return False, f"接口异常 [{e.code}] {e.msg}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 容错解析

def pick(d: Any, *names, default=None):
    """在 dict 里按候选名取第一个非空值（大小写不敏感）。"""
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, "", [], {}):
            return v
    return default


def as_int(v, default=0) -> int:
    try:
        if v is None or v == "" or v == "不限":
            return default
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def find_list(obj: Any, depth: int = 0) -> list | None:
    """在任意嵌套响应里找到"看起来像活动数组"的那个 list。"""
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            keys = {str(k).lower() for k in obj[0]}
            if keys & {"activityname", "activityid", "name", "title", "id"}:
                return obj
        return None
    if isinstance(obj, dict):
        # 优先常见容器名
        for k in ("list", "records", "rows", "content", "data", "activities", "items"):
            v = next((vv for kk, vv in obj.items() if str(kk).lower() == k), None)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():
            got = find_list(v, depth + 1)
            if got:
                return got
    return None
