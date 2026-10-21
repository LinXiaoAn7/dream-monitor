"""验证「DES-ECB / key=51434574 / 零填充」是不是 pwd 的加密方式。

依据：手机内存里找到的 uid/token 密文，用这个 key 做 DES-ECB 解密后能得到
`<uid>`、`,<token 前 9 位>`… —— 说明 App 确实用这套 DES 处理敏感字段。
所以 pwd 极可能同款，只差填充方式。

用法：python try_pwd_des.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from dmclient import ApiError, DreamClient

# ---------------------------------------------------------------- DES（纯标准库）
IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
      64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
      61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
      37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
      34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18,
     19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9,
     19, 13, 30, 6, 22, 11, 4, 25]
PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3,
       60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
       29, 21, 13, 5, 28, 20, 12, 4]
PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
       41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
SH = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
SBOX = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8, 4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5, 0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1, 13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9, 10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6, 4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8, 9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6, 1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2, 7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _perm(bits, table):
    return [bits[i - 1] for i in table]


def _b2i(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _i2b(v, n):
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def _subkeys(key8: bytes):
    k = _perm(_i2b(int.from_bytes(key8, "big"), 64), PC1)
    c, d = k[:28], k[28:]
    out = []
    for s in SH:
        c = c[s:] + c[:s]
        d = d[s:] + d[:s]
        out.append(_perm(c + d, PC2))
    return out


def des_block(block8: bytes, key8: bytes, decrypt: bool = False) -> bytes:
    ks = _subkeys(key8)
    if decrypt:
        ks = ks[::-1]
    b = _perm(_i2b(int.from_bytes(block8, "big"), 64), IP)
    left, right = b[:32], b[32:]
    for k in ks:
        x = [a ^ c for a, c in zip(_perm(right, E), k)]
        s = []
        for i in range(8):
            six = x[i * 6:(i + 1) * 6]
            row = (six[0] << 1) | six[5]
            col = (six[1] << 3) | (six[2] << 2) | (six[3] << 1) | six[4]
            s += _i2b(SBOX[i][row * 16 + col], 4)
        left, right = right, [a ^ c for a, c in zip(left, _perm(s, P))]
    return _b2i(_perm(right + left, FP)).to_bytes(8, "big")


def des_ecb(data: bytes, key: bytes = b"51434574", pad: str = "zero") -> str:
    if len(data) % 8:
        if pad == "zero":
            data += b"\x00" * (8 - len(data) % 8)
        else:  # pkcs5
            n = 8 - len(data) % 8
            data += bytes([n]) * n
    out = b"".join(des_block(data[i:i + 8], key) for i in range(0, len(data), 8))
    return out.hex().upper()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    api = cfg["api"]
    account = str(api.get("account") or "")
    pwd = str(api.get("password") or "")
    if not account or not pwd:
        print("config.yaml 里缺 api.account / api.password")
        return 2

    cands = [
        ("zero-32", des_ecb(pwd.encode(), pad="zero")),
        ("pkcs5-32", des_ecb(pwd.encode(), pad="pkcs5")),
    ]
    first8 = pwd.encode()[:8]
    cands.append(("first8-zero-16", des_ecb(first8, pad="zero")))
    cands.append(("first8-pkcs5-16", des_ecb(first8, pad="pkcs5")))

    print(f"账号 {account}｜密码长度 {len(pwd)}")
    for name, val in cands:
        print(f"  {name:16s} {val}")

    client = DreamClient(api)
    for name, val in cands:
        print(f"\n>>> 试 {name} ({len(val)} 字符)")
        try:
            data = client.login_phone(account, val, raw_pwd=True)
        except ApiError as e:
            print(f"    失败 [{e.code}] {e.msg}")
            if str(e.code) == "90000022" and "超过限制" in str(e.msg):
                print("    账号被临时锁定，等 15 分钟")
                return 3
            time.sleep(2)
            continue
        print("    成功！返回：", json.dumps(data, ensure_ascii=False)[:300])
        tok = (data.get("token") or "")
        uid = str(data.get("uid") or data.get("userId") or "")
        if tok:
            cfg["api"]["token"] = tok
            cfg["api"]["uid"] = uid or cfg["api"].get("uid")
            cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
            print(f"    已写回 config.yaml：uid={cfg['api']['uid']} token={tok}")
            print(f"    命中方式 = {name}")
        return 0
    print("\n全部失败，说明 DES/零填充这套不是 pwd 的算法")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
