"""本地密钥加密（零新依赖）。

- Windows：DPAPI CryptProtectData/CryptUnprotectData，本机 + 当前用户绑定。
- 非 Windows：无系统级密钥库时退回明文（不伪装成加密）。
- 存储格式：enc:v1:<base64>。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import platform

_PREFIX = "enc:v1:"
_IS_WINDOWS = platform.system() == "Windows"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _blob_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


_CONFIGURED = False


def _configure_crypt32() -> None:
    """一次性声明 DPAPI 函数签名（描述串按 LPCWSTR 传递，返回值显式 bool）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True
    try:
        crypt32 = ctypes.windll.crypt32
        pblob = ctypes.POINTER(_DATA_BLOB)
        sig = [pblob, ctypes.c_wchar_p, pblob, ctypes.c_void_p, ctypes.c_void_p,
               ctypes.c_uint32, pblob]
        crypt32.CryptProtectData.argtypes = sig
        crypt32.CryptProtectData.restype = ctypes.c_bool
        crypt32.CryptUnprotectData.argtypes = [
            pblob, ctypes.POINTER(ctypes.c_wchar_p), pblob,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, pblob,
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    except Exception:
        _CONFIGURED = False  # 失败则退回 ctypes 默认转换，不阻塞加解密


def _crypt32():
    _configure_crypt32()
    return ctypes.windll.crypt32


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    if not _IS_WINDOWS:
        return plaintext
    try:
        data_in = _blob(plaintext.encode("utf-16-le"))
        data_out = _DATA_BLOB()
        ok = _crypt32().CryptProtectData(
            ctypes.byref(data_in),
            "GiliAgent",
            None,
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(data_out),
        )
        if not ok:
            return plaintext
        raw = _blob_bytes(data_out)
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
        return _PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception:
        return plaintext


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value
    if not _IS_WINDOWS:
        return ""
    try:
        raw = base64.b64decode(value[len(_PREFIX):])
        data_in = _blob(raw)
        data_out = _DATA_BLOB()
        # 注意：encrypt 里的 "GiliAgent" 是描述串（第 2 参，仅展示用途），不是熵；
        # 熵（第 3 参）两侧都传 None —— 解密不需要描述串匹配，与历史密文完全兼容。
        ok = _crypt32().CryptUnprotectData(
            ctypes.byref(data_in),
            None,
            None,
            None,
            None,
            0x01,
            ctypes.byref(data_out),
        )
        if not ok:
            return ""
        out = _blob_bytes(data_out)
        ctypes.windll.kernel32.LocalFree(data_out.pbData)
        return out.decode("utf-16-le")
    except Exception:
        return ""

