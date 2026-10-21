from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path
import sys
import threading

from .errors import ProxyTalkError


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


class SecretVault:
    """Machine-local secret storage backed by Windows DPAPI.

    The interface intentionally exposes only named byte values. Callers never need
    to know file names, DPAPI flags, or serialization details.
    """

    _ENTROPY = b"ProxyTalk/v1/local-secret"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def put_text(self, name: str, value: str) -> None:
        protected = self._protect(value.encode("utf-8"))
        path = self._path(name)
        temporary = path.with_suffix(".tmp")
        with self.lock:
            try:
                temporary.write_bytes(protected)
                temporary.replace(path)
            except OSError as exc:
                raise ProxyTalkError("SECRET_STORE_FAILED", "无法保存本机加密凭据。") from exc
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def get_text(self, name: str) -> str:
        path = self._path(name)
        try:
            with self.lock:
                if not path.is_file():
                    return ""
                envelope = path.read_bytes()
            return self._unprotect(envelope).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProxyTalkError(
                "SECRET_UNAVAILABLE",
                "本机加密凭据无法读取，请重新配置。",
                details={"secret": name},
            ) from exc

    def delete(self, name: str) -> None:
        path = self._path(name)
        try:
            with self.lock:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise ProxyTalkError("SECRET_DELETE_FAILED", "无法删除本机凭据。") from exc

    def has(self, name: str) -> bool:
        with self.lock:
            return self._path(name).is_file()

    def _path(self, name: str) -> Path:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.dpapi"

    def _protect(self, data: bytes) -> bytes:
        if sys.platform != "win32":
            raise ProxyTalkError("DPAPI_UNAVAILABLE", "ProxyTalk 密钥库仅支持 Windows。")
        source, source_buffer = _blob(data)
        entropy, entropy_buffer = _blob(self._ENTROPY)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "ProxyTalk",
            ctypes.byref(entropy),
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
        _ = source_buffer, entropy_buffer
        if not ok:
            raise ProxyTalkError("DPAPI_PROTECT_FAILED", "Windows 无法加密本机凭据。")
        try:
            raw = ctypes.string_at(output.pbData, output.cbData)
            return b"PTDP1:" + base64.b64encode(raw)
        finally:
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))

    def _unprotect(self, envelope: bytes) -> bytes:
        if sys.platform != "win32" or not envelope.startswith(b"PTDP1:"):
            raise ProxyTalkError("SECRET_INVALID", "本机凭据格式无效。")
        try:
            encrypted = base64.b64decode(envelope[6:], validate=True)
        except ValueError as exc:
            raise ProxyTalkError("SECRET_INVALID", "本机凭据格式无效。") from exc
        source, source_buffer = _blob(encrypted)
        entropy, entropy_buffer = _blob(self._ENTROPY)
        output = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy),
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
        _ = source_buffer, entropy_buffer
        if not ok:
            raise ProxyTalkError("DPAPI_UNPROTECT_FAILED", "Windows 无法解密本机凭据。")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


class MemoryVault:
    """In-process adapter used at the SecretVault seam by tests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lock = threading.RLock()

    def put_text(self, name: str, value: str) -> None:
        with self.lock:
            self.values[name] = value

    def get_text(self, name: str) -> str:
        with self.lock:
            return self.values.get(name, "")

    def delete(self, name: str) -> None:
        with self.lock:
            self.values.pop(name, None)

    def has(self, name: str) -> bool:
        with self.lock:
            return name in self.values
