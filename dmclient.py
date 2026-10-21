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
PATH_MINE_HOMEPAGE = "/v3/user/mine_homepage"

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


DEVICE_PRESETS = [
    {"channelName": "yingyongbao", "device": "V2301A", "hardware": "qcom/sm8475",
     "screen": "1260*2800", "sysVersion": "15"},
    {"channelName": "appstore", "device": "PGT-AN10", "hardware": "qcom/sm8550",
     "screen": "1176*2500", "sysVersion": "14"},
    {"channelName": "huawei", "device": "ALN-AL00", "hardware": "qcom/sm8475",
     "screen": "1216*2688", "sysVersion": "15"},
    {"channelName": "oppo", "device": "PJD110", "hardware": "qcom/sm8650",
     "screen": "1264*2780", "sysVersion": "15"},
]


def new_identity(cfg: dict, preset: int | None = None) -> dict:
    """生成一套全新的设备指纹并写进 cfg（uuid / jPushId / device）。

    为什么需要：到梦空间的风控是按**设备指纹**记的。被标记后，同一个
    standardUA 会一直拿到 10005（哪怕刚登录成功），换一套指纹立刻恢复。
    """
    import random
    dev = dict(DEVICE_PRESETS[(preset if preset is not None
                               else random.randrange(len(DEVICE_PRESETS)))])
    now = str(int(__import__("time").time() * 1000))
    dev.setdefault("createTime", now)
    dev.setdefault("startTime", now)
    dev["operator"] = "wifi"
    cfg["device"] = dev
    cfg["uuid"] = uuid.uuid4().hex
    cfg["jPushId"] = "%012x%s" % (uuid.uuid4().int & 0xFFFFFFFFFFFF, uuid.uuid4().hex[:8])
    return cfg


IDENTITY_PATH = "data/identity.json"


def _ensure_identity(cfg: dict) -> dict:
    """保证 uuid / jPushId / version 齐全且跨重启稳定。

    实测：submit 报 -2000 的真因就是 UA 与参数里的 jPushId 不一致、
    或者 UA 缺 jPushId/uuid。身份一旦生成就落盘复用，别每次换。
    """
    try:
        import pathlib as _p
        f = _p.Path(IDENTITY_PATH)
        if f.exists():
            saved = json.loads(f.read_text(encoding="utf-8"))
        else:
            saved = {}
    except Exception:
        saved = {}
    if not cfg.get("uuid"):
        cfg["uuid"] = saved.get("uuid") or uuid.uuid4().hex
    if not cfg.get("jPushId"):
        cfg["jPushId"] = saved.get("jPushId") or ("%013x" % (int(time.time() * 1000) % (16 ** 13)))
    cfg.setdefault("version", "5.0.0")
    try:
        import pathlib as _p2
        f2 = _p2.Path(IDENTITY_PATH)
        f2.parent.mkdir(parents=True, exist_ok=True)
        f2.write_text(json.dumps({"uuid": cfg["uuid"], "jPushId": cfg["jPushId"]}),
                      encoding="utf-8")
    except Exception:
        pass
    return cfg


def _ua_json(cfg: dict) -> str:
    """构造 standardUA 设备指纹（字段对齐 App 5.0.0 实际发的那个）。"""
    dev = cfg.get("device", {})
    now = int(time.time() * 1000)
    profile = {
        "channelName": dev.get("channelName", "dmkj_Android"),
        "countryCode": dev.get("countryCode", "CN"),
        "createTime": int(dev.get("createTime", now - 86400000)),
        "device": dev.get("device", "OnePlus PJE110"),
        "hardware": dev.get("hardware", "qcom"),
        "jPushId": cfg.get("jPushId", ""),
        "modifyTime": int(dev.get("modifyTime", now - 3600000)),
        "operator": dev.get("operator", "wifi"),
        "screenResolution": dev.get("screenResolution", "1264-2584"),
        "startTime": int(dev.get("startTime", now - 600000)),
        "sysVersion": dev.get("sysVersion", "Android 15 35"),
        "system": "android",
        "uuid": cfg.get("uuid") or uuid.uuid4().hex,
        "version": cfg.get("version", "5.0.0"),
    }
    return json.dumps(profile, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class DreamClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg.get("host", HOST)
        self.uid = str(cfg.get("uid", ""))
        self.token = str(cfg.get("token", ""))
        _ensure_identity(cfg)
        self.version = cfg.get("version", "5.0.0")
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
            # jPushId 只在 submit 里显式传：多数接口的白名单里没有它，
            # 多塞一个就 11002。
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
        self.profile = out
        return {
            "uid": self.uid,
            "token": self.token,
            "name": out.get("displayName") or out.get("name", ""),
            "school": out.get("schoolName", ""),
            "college": out.get("collegeName", ""),
            "student_id": str(out.get("studentId", "")),
            "phone": str(out.get("phone", "")),
            "avatar": str(out.get("avatar", "")),
            "profile": out,
        }

    # ---------------------------------------------------------- 积分
    def mine_homepage(self) -> dict:
        """「我的」页数据（App 同款接口）。

        - 路径 /v3/user/mine_homepage 是从 App 进程内存里挖出来的（加固壳把 dex 加密，
          静态分析看不到）；必须带 page 参数，少一个就是 11002。
        - accountList 里 accountTypeSchoolName == "积分" 的那项，
          accountMoney 就是 App 上显示的积分（如 "43.00"）。
        """
        d = self._post(PATH_MINE_HOMEPAGE, {"page": "1"}) or {}
        return d if isinstance(d, dict) else {}

    def points(self) -> dict:
        """只取积分（个人主页展示用）。"""
        data = self.mine_homepage()
        accounts = data.get("accountList") or []
        picked = None
        for it in accounts:
            if str(it.get("accountTypeSchoolName") or "") == "积分":
                picked = it
                break
        if picked is None:
            for it in accounts:
                if str(it.get("accountTypeName") or "") in ("积分", "学分"):
                    picked = it
                    break
        if picked is None and accounts:
            picked = accounts[0]
        raw = str((picked or {}).get("accountMoney") or "")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = None
        return {
            "value": value,
            "text": raw,
            "label": str((picked or {}).get("accountTypeSchoolName") or "积分"),
            "cardName": str(data.get("cardName") or ""),
            "schoolRank": str(data.get("schoolRank") or ""),
            "practiceNumber": str(data.get("practiceNumber") or ""),
            "raw": data,
        }

    def mine(self, page: int = 1) -> dict:
        """我参与的活动（服务端要 keyword/page/type 三件套，且不能带 timestamp）。"""
        d = self._post("/v2/activity/mine/list",
                       extra={"page": str(page), "keyword": "", "type": "1"}, ts=False) or {}
        return d if isinstance(d, dict) else {}

    def scan_score(self, max_pages: int = 6) -> dict:
        """积分：直接问 App 同款接口，不再逐条累加。

        早期以为没有「总积分」接口，只能把「我的活动」里每条详情的 score 加起来，
        得出的 24.9 跟 App 上显示的 43.00 对不上 —— 口径是错的。真实值在
        /v3/user/mine_homepage 的 accountList[积分].accountMoney。
        """
        p = self.points()
        return {
            "total": p["value"] if p["value"] is not None else 0.0,
            "text": p["text"],
            "label": p["label"],
            "cardName": p["cardName"],
            "schoolRank": p["schoolRank"],
            "practiceNumber": p["practiceNumber"],
            "source": "到梦空间「我的」页",
        }

    # ---------------------------------------------------------- 业务
    def summary(self) -> dict:
        """账号概览（个人信息页用）：姓名、学校、学院、积分等。"""
        data = self._post("/v3/user/account/summary") or {}
        return data if isinstance(data, dict) else {}

    ACTIVITY_QUERY = {
        "catalogId": "", "catalogId2": "", "endTime": "", "joinEndTime": "",
        "joinFlag": "", "joinStartTime": "", "keyword": "", "level": "",
        "sort": "", "specialFlag": "", "startTime": "", "status": "",
    }

    def activities(self, page: int = 1, page_size: int | None = None, **kw) -> Any:
        """活动列表。

        服务端现在只认 `page`（外加调用方显式传的关键字）；把老的 12 键
        ACTIVITY_QUERY 整套塞进去会返回 11002 —— 那套字段是 2023 年的老版本
        留下的，接口后来又收紧过。
        """
        extra = {"page": str(page)}
        for k, v in kw.items():
            if v is not None:
                extra[k] = v
        return self._post(PATH_ACTIVITIES, extra)

    def detail(self, activity_id: str) -> Any:
        """活动详情。注意：这个接口**不能**带 timestamp，带了就 11002。"""
        return self._post(PATH_DETAIL, {"activityId": str(activity_id)}, ts=False)

    def signup_info(self, activity_id: str) -> Any:
        """报名前置信息（joinTip / joinMaxNum / 是否需要验证码）。

        坑：这个接口**不能**带 timestamp，带了就是 11002；和 detail 一样。
        """
        return self._post(PATH_SIGNUP_INFO, {"activityId": str(activity_id)}, ts=False)

    def signup_form(self, activity_id: str) -> Any:
        """报名前拉表单（字段模板）。和 signup_info 是同一个接口。"""
        return self.signup_info(activity_id)

    def submit(self, activity_id: str, data: str = "[]", remark: str = "") -> Any:
        """提交报名。data 是报名表单的 JSON 字符串（不是对象）。

        实测（App 5.0.0 内存抓的真参数）：
        activityId / data / jPushId / remark / useNumber / uid / token / version / timestamp
        —— 少 jPushId 或 useNumber 就是 -2000，jPushId 还必须与本机 standardUA 里的一致。
        """
        return self._post(PATH_SIGNUP_SUBMIT, {
            "activityId": str(activity_id),
            "data": data,
            "remark": remark,
            "useNumber": "0",
            "jPushId": self.jpush,
        })

    def cancel(self, signup_id: str) -> Any:
        """取消报名。要的是 signUpId（详情里的 signUpId 字段），不是 activityId。"""
        # 实测：取消报名同样不能带 timestamp，带了就是 11002
        return self._post(PATH_SIGNUP_CANCEL, {"signUpId": str(signup_id)}, ts=False)

    def rotate_identity(self) -> dict:
        """换一套设备指纹（风控被标记时用），并重建 standardUA。"""
        new_identity(self.cfg)
        self.ua = _ua_json(self.cfg)
        self.sess.headers.update({"standardUA": self.ua})
        log.warning("已切换设备指纹：uuid=%s… device=%s",
                    str(self.cfg.get("uuid"))[:8], self.cfg["device"]["device"])
        return {"uuid": self.cfg["uuid"], "jPushId": self.cfg["jPushId"],
                "device": self.cfg["device"]}

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
