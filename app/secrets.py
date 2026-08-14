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


def _crypt32():
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


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)
