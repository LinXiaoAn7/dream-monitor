"""
dmcrypto —— 到梦空间 App 请求体的加密与签名实现（纯标准库，零第三方依赖）

链路（逆向自 App v4.9.5 的 com.jingcai.apps.qualitydev.network.netRequesthttp.BaseRequest
与 common.encrypt.e.a）：

    明文 JSON(含 timestamp + signToken，键排序)
      └─ AES-CBC/PKCS5(key=随机16位 session_key, iv=ReIV) → 密文段0
         └─ base64，每 60 字符插 1 空格                         → 段0文本
    session_key
      └─ RSA-1024 / PKCS#1 v1.5 公钥加密                        → 密文段1
         └─ base64                                              → 段1文本
    段0文本 + "==" + 段1文本
      └─ base64，每 60 字符插 1 空格                            → d 参数

    signToken = MD5( (SHA512(紧凑JSON)[1::2])[0::2] ).upper()

服务端语义：code=100 成功；11001 签名错误；11002 参数错误；
-2000 设备指纹/业务异常；2000011 已报名；2000166 非报名期。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import string
import struct
from typing import Any

# ---------------------------------------------------------------- 业务常量

# RSA 公钥（App 运行时 getNetSignModuleKey 返回，1024 bit）
RSA_N_HEX = (
    "9eed813259ad13f963176bcab53d04a8d8dd79e85cfa73588dd162cc9b9747d4"
    "a05d1a8fd11ce7cb38186729bfdd82f6b24141df2b3b021f0d0f9bca4d1120e1"
    "1554e5fd79dd8373b8025f8bf17a03e463dea876bc4862e8ad19cb38f864ea74"
    "e4aa53ef15735801dc8fcd3e98e75ef5133d50d8515c5b88d40d3a39b055f6ab"
)
RSA_N = int(RSA_N_HEX, 16)
RSA_E = 65537
RSA_BYTES = (RSA_N.bit_length() + 7) // 8  # 128

# AES IV（ReIV 用于加密，PlIV 用于响应解密）
AES_IV_RE = b"1628092121312213"
AES_IV_PL = b"9618953120112110"


# ---------------------------------------------------------------- AES

def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，模多项式 x^8+x^4+x^3+x+1 (0x11b)。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_tables():
    # 乘法逆元表：x * inv = 1 (mod 0x11b)
    inv = [0] * 256
    for x in range(1, 256):
        for y in range(1, 256):
            if _gmul(x, y) == 1:
                inv[x] = y
                break

    def rotl(v: int, n: int) -> int:
        return ((v << n) | (v >> (8 - n))) & 0xFF

    sbox = [0] * 256
    for i in range(256):
        b = inv[i]
        sbox[i] = b ^ rotl(b, 1) ^ rotl(b, 2) ^ rotl(b, 3) ^ rotl(b, 4) ^ 0x63

    inv_sbox = [0] * 256
    for i, v in enumerate(sbox):
        inv_sbox[v] = i

    return sbox, inv_sbox


_SBOX, _INV_SBOX = _build_tables()


def _expand_key(key: bytes):
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise ValueError("AES key must be 16/24/32 bytes")
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(words[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _gmul(rcon, 2)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        words.append([words[i - nk][j] ^ t[j] for j in range(4)])
    return words, nr


def _add_round_key(s, w, rnd):
    for c in range(4):
        for r in range(4):
            s[r][c] ^= w[rnd * 4 + c][r]


def _encrypt_block(block: bytes, words, nr: int) -> bytes:
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(s, words, 0)
    for rnd in range(1, nr + 1):
        # SubBytes + ShiftRows
        for r in range(4):
            for c in range(4):
                s[r][c] = _SBOX[s[r][c]]
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
        if rnd != nr:
            # MixColumns
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
                s[1][c] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
                s[2][c] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
                s[3][c] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)
        _add_round_key(s, words, rnd)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _decrypt_block(block: bytes, words, nr: int) -> bytes:
    s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
    _add_round_key(s, words, nr)
    for rnd in range(nr - 1, -1, -1):
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
        for r in range(4):
            for c in range(4):
                s[r][c] = _INV_SBOX[s[r][c]]
        _add_round_key(s, words, rnd)
        if rnd != 0:
            for c in range(4):
                a = [s[r][c] for r in range(4)]
                s[0][c] = _gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9)
                s[1][c] = _gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13)
                s[2][c] = _gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11)
                s[3][c] = _gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data.endswith(bytes([n]) * n):
        return data[:-n]
    return data


def aes_cbc_encrypt(key: bytes, iv: bytes, plain: bytes) -> bytes:
    words, nr = _expand_key(key)
    plain = pkcs7_pad(plain)
    out = bytearray()
    prev = iv
    for i in range(0, len(plain), 16):
        blk = bytes(x ^ y for x, y in zip(plain[i:i + 16], prev))
        prev = _encrypt_block(blk, words, nr)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, cipher: bytes) -> bytes:
    words, nr = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(cipher), 16):
        blk = cipher[i:i + 16]
        dec = _decrypt_block(blk, words, nr)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = blk
    return pkcs7_unpad(bytes(out))


# ---------------------------------------------------------------- RSA

def rsa_pkcs1_encrypt(msg: bytes) -> bytes:
    """RSA PKCS#1 v1.5 公钥加密，输出 RSA_BYTES 字节。"""
    if len(msg) > RSA_BYTES - 11:
        raise ValueError("message too long for RSA-1024 PKCS#1")
    ps_len = RSA_BYTES - len(msg) - 3
    ps = bytes(random.randint(1, 255) for _ in range(ps_len))
    em = b"\x00\x02" + ps + b"\x00" + msg
    m = int.from_bytes(em, "big")
    if m >= RSA_N:
        raise ValueError("encoded message out of range")
    c = pow(m, RSA_E, RSA_N)
    return c.to_bytes(RSA_BYTES, "big")


# ---------------------------------------------------------------- 编码/签名

def b64_spaces(raw: bytes) -> str:
    """标准 base64，每 60 个字符插入一个空格（Java b.d）。"""
    s = base64.b64encode(raw).decode()
    return " ".join(s[i:i + 60] for i in range(0, len(s), 60))


def strip_spaces(s: str) -> str:
    return s.replace(" ", "").replace("\n", "").replace("\r", "")


def fastjson(d: dict) -> str:
    """键排序、无空格紧凑 JSON、非 ASCII 原样输出（对齐 fastjson 序列化）。"""
    norm = {k: ("" if v is None else str(v)) for k, v in d.items()}
    return json.dumps(norm, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sign_token(params: dict) -> str:
    """signToken = MD5( SHA512(紧凑JSON)[1::2][0::2] ).upper()"""
    h = hashlib.sha512(fastjson(params).encode()).hexdigest()
    odd = h[1::2]
    even = odd[0::2]
    return hashlib.md5(even.encode()).hexdigest().upper()


def rand_session_key(n: int = 16) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def make_d(params: dict, session_key: str | None = None) -> str:
    """把最终参数表（已含 timestamp/signToken）封装成 d 参数。"""
    skey = (session_key or rand_session_key()).encode()
    assert len(skey) == 16, "session_key 必须 16 字节"

    seg0 = b64_spaces(aes_cbc_encrypt(skey, AES_IV_RE, fastjson(params).encode()))
    seg1 = base64.b64encode(rsa_pkcs1_encrypt(skey)).decode()
    inner = (seg0 + "==" + seg1).encode()
    return b64_spaces(inner)


def decrypt_d(d: str) -> str:
    """仅用于调试：用已知 session_key 无法还原，这里演示结构解析。"""
    raw = base64.b64decode(strip_spaces(base64.b64decode(strip_spaces(d))))
    txt = raw.decode("latin-1")
    if "==" in txt:
        a, _, b = txt.partition("==")
        return f"seg0={len(a)}B seg1={len(b)}B"
    return f"len={len(raw)}"


# ---------------------------------------------------------------- 自检

def _selftest() -> None:
    # NIST FIPS-197 AES-128 ECB 向量
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    words, nr = _expand_key(key)
    ct = _encrypt_block(pt, words, nr)
    assert ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()

    # NIST SP 800-38A F.2.1 CBC-AES128 向量（第一块）
    k128 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt1 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    cbc_ct = aes_cbc_encrypt(k128, iv, pt1)
    assert cbc_ct.hex().startswith("7649abac8119b246cee98e9b12e9197d"), cbc_ct.hex()[:32]
    assert aes_cbc_decrypt(k128, iv, cbc_ct) == pt1

    # 往返
    for klen in (16, 24, 32):
        k = os.urandom(klen)
        msg = "活动报名测试 / activity id 12345678".encode()
        c = aes_cbc_encrypt(k, AES_IV_RE, msg)
        assert aes_cbc_decrypt(k, AES_IV_RE, c) == msg

    # RSA 往返（用已知 n/e 加密后验证 < n 且长度正确）
    ctxt = rsa_pkcs1_encrypt(b"0123456789abcdef")
    assert len(ctxt) == 128
    assert int.from_bytes(ctxt, "big") < RSA_N

    # 签名格式
    st = sign_token({"uid": "1", "token": "t", "version": "4.9.5"})
    assert len(st) == 32 and st.isupper() and all(ch in string.hexdigits.upper() for ch in st)

    d = make_d({"uid": "1", "token": "t", "version": "4.9.5", "timestamp": "1", "signToken": st})
    assert " " in d and len(d) > 200
    print("dmcrypto selftest: ALL PASS")
    print("  sample d head:", d[:80].replace(" ", "_"))
    print("  signToken demo:", st)


if __name__ == "__main__":
    _selftest()
