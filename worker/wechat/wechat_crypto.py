from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
from typing import Callable, Iterable

from Crypto.Cipher import AES

from .errors import ProxyTalkError


PAGE_SIZE = 4096
SALT_SIZE = 16
RESERVE_SIZE = 80
IV_SIZE = 16
HMAC_SIZE = 64
SQLITE_HEADER = b"SQLite format 3\x00"
SQLCIPHER_KDF_ITERATIONS = 256_000

_V4_POINTER_SUFFIX = (
    (b"\x00" * 10)
    + b"\x20"
    + (b"\x00" * 7)
    + b"\x2f"
    + (b"\x00" * 7)
)
_V4_DLL_KEY_PATTERN = re.compile(
    b"\x48\xBA(.{8}).{3,8}?"
    b"\x48\xBA(.{8}).{3,8}?"
    b"\x48\xBA(.{8}).{3,8}?"
    b"\x48\xBA(.{8}).{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL,
)


def derive_hmac_key(encryption_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(value ^ 0x3A for value in salt)
    return hashlib.pbkdf2_hmac("sha512", encryption_key, mac_salt, 2, dklen=32)


def derive_encryption_key(key_material: bytes, salt: bytes) -> bytes:
    """Derive a per-database SQLCipher key from account-level V4 key material."""
    return hashlib.pbkdf2_hmac(
        "sha512",
        key_material,
        salt,
        SQLCIPHER_KDF_ITERATIONS,
        dklen=32,
    )


def resolve_encryption_key(
    key_material: bytes,
    salt: bytes,
    encrypted_page: bytes,
) -> bytes | None:
    """Accept either a raw page key or account-level SQLCipher key material."""
    if len(key_material) != 32:
        return None
    if page_hmac_is_valid(key_material, salt, encrypted_page, 1):
        return key_material
    derived = derive_encryption_key(key_material, salt)
    if page_hmac_is_valid(derived, salt, encrypted_page, 1):
        return derived
    return None


def page_hmac_is_valid(encryption_key: bytes, salt: bytes, encrypted_page: bytes, page_no: int) -> bool:
    if len(encrypted_page) != PAGE_SIZE or len(encryption_key) != 32:
        return False
    body_start = SALT_SIZE if page_no == 1 else 0
    authenticated = encrypted_page[body_start : PAGE_SIZE - HMAC_SIZE]
    expected = encrypted_page[PAGE_SIZE - HMAC_SIZE :]
    digest = hmac.new(derive_hmac_key(encryption_key, salt), authenticated, hashlib.sha512)
    digest.update(struct.pack("<I", page_no))
    return hmac.compare_digest(digest.digest(), expected)


def decrypt_page(
    encryption_key: bytes,
    salt: bytes,
    encrypted_page: bytes,
    page_no: int,
    *,
    strict_hmac: bool = True,
) -> bytes:
    if strict_hmac and not page_hmac_is_valid(
        encryption_key, salt, encrypted_page, page_no
    ):
        raise ProxyTalkError(
            "WECHAT_KEY_INVALID",
            "微信数据库密钥校验失败，可能需要重新扫描密钥。",
            details={"page": page_no},
        )
    body_start = SALT_SIZE if page_no == 1 else 0
    body_end = PAGE_SIZE - RESERVE_SIZE
    iv = encrypted_page[body_end : body_end + IV_SIZE]
    plaintext = AES.new(encryption_key, AES.MODE_CBC, iv).decrypt(
        encrypted_page[body_start:body_end]
    )
    if page_no == 1:
        return SQLITE_HEADER + plaintext + (b"\x00" * RESERVE_SIZE)
    return plaintext + (b"\x00" * RESERVE_SIZE)


def copy_stable_database(source: Path, destination: Path, *, attempts: int = 4) -> Path:
    """Copy a DB and its WAL while rejecting snapshots that visibly changed mid-copy."""
    if not source.is_file():
        raise ProxyTalkError("WECHAT_DB_MISSING", f"找不到微信数据库：{source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidates = [source, Path(f"{source}-wal"), Path(f"{source}-shm")]
    for attempt in range(attempts):
        for stale in (destination, Path(f"{destination}-wal"), Path(f"{destination}-shm")):
            stale.unlink(missing_ok=True)
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in candidates
            if path.is_file()
        }
        for path in before:
            shutil.copy2(path, destination if path == source else Path(f"{destination}{path.name[len(source.name):]}"))
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in candidates
            if path.is_file()
        }
        if before == after:
            return destination
        time.sleep(0.08 * (attempt + 1))
    raise ProxyTalkError(
        "WECHAT_SNAPSHOT_BUSY",
        "微信正在快速写入数据库，未能取得一致快照；稍后会自动重试。",
        retryable=True,
    )


def decrypt_database(snapshot: Path, output: Path, encryption_key: bytes) -> None:
    """Decrypt a SQLCipher 4 database and directly fold committed WAL frames into it."""
    size = snapshot.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE:
        raise ProxyTalkError("WECHAT_DB_INVALID", "微信数据库大小不是有效的 SQLCipher 页大小。")
    with snapshot.open("rb") as handle:
        first = handle.read(PAGE_SIZE)
    salt = first[:SALT_SIZE]
    resolved_key = resolve_encryption_key(encryption_key, salt, first)
    if resolved_key is None:
        raise ProxyTalkError("WECHAT_KEY_INVALID", "微信数据库密钥无效，请重新扫描。")

    output.parent.mkdir(parents=True, exist_ok=True)
    with snapshot.open("rb") as source, output.open("w+b") as target:
        for page_no in range(1, size // PAGE_SIZE + 1):
            encrypted = source.read(PAGE_SIZE)
            target.write(
                decrypt_page(
                    resolved_key,
                    salt,
                    encrypted,
                    page_no,
                    strict_hmac=page_no == 1,
                )
            )

        wal_path = Path(f"{snapshot}-wal")
        if wal_path.is_file() and wal_path.stat().st_size >= 32:
            _fold_wal(target, wal_path, resolved_key, salt)
        target.flush()

    with output.open("rb") as handle:
        if handle.read(16) != SQLITE_HEADER:
            output.unlink(missing_ok=True)
            raise ProxyTalkError("WECHAT_DECRYPT_FAILED", "微信数据库解密结果无效。")


def _fold_wal(target, wal_path: Path, encryption_key: bytes, salt: bytes) -> None:
    raw_size = wal_path.stat().st_size
    with wal_path.open("rb") as wal:
        header = wal.read(32)
        magic = struct.unpack(">I", header[:4])[0]
        if magic not in {0x377F0682, 0x377F0683}:
            raise ProxyTalkError("WECHAT_WAL_INVALID", "微信数据库 WAL 头无效。", retryable=True)
        page_size = struct.unpack(">I", header[8:12])[0]
        if page_size == 1:
            page_size = 65536
        if page_size != PAGE_SIZE:
            raise ProxyTalkError("WECHAT_SCHEMA_UNSUPPORTED", f"暂不支持 {page_size} 字节的微信数据库页。")
        frame_size = 24 + page_size
        frame_count = (raw_size - 32) // frame_size
        frames: list[tuple[int, int, bytes]] = []
        last_commit = -1
        header_salts = header[16:24]
        for index in range(frame_count):
            frame_header = wal.read(24)
            encrypted_page = wal.read(page_size)
            if len(frame_header) != 24 or len(encrypted_page) != page_size:
                break
            if frame_header[8:16] != header_salts:
                break
            page_no, database_pages = struct.unpack(">II", frame_header[:8])
            if not page_no:
                break
            frames.append((page_no, database_pages, encrypted_page))
            if database_pages:
                last_commit = index
        if last_commit < 0:
            return
        final_size = 0
        for page_no, database_pages, encrypted_page in frames[: last_commit + 1]:
            decrypted = decrypt_page(
                encryption_key,
                salt,
                encrypted_page,
                page_no,
                strict_hmac=page_no == 1,
            )
            target.seek((page_no - 1) * PAGE_SIZE)
            target.write(decrypted)
            if database_pages:
                final_size = database_pages * PAGE_SIZE
        if final_size:
            target.truncate(final_size)


def collect_encrypted_databases(root: Path) -> list[tuple[Path, str, bytes]]:
    result = []
    for path in root.rglob("*.db"):
        if path.name.endswith(("-wal", "-shm")) or not path.is_file():
            continue
        try:
            if path.stat().st_size < PAGE_SIZE:
                continue
            with path.open("rb") as handle:
                first = handle.read(PAGE_SIZE)
        except OSError:
            continue
        if len(first) == PAGE_SIZE and first[:16] != SQLITE_HEADER:
            result.append((path, first[:16].hex(), first))
    return result


def pointer_record_addresses(data: bytes) -> list[int]:
    """Extract V4 key-buffer pointers from Weixin's in-memory metadata records."""
    addresses: list[int] = []
    seen: set[int] = set()
    offset = 0
    while True:
        suffix_at = data.find(_V4_POINTER_SUFFIX, offset)
        if suffix_at < 0:
            break
        record_at = suffix_at - 6
        if record_at >= 0:
            address = struct.unpack_from("<Q", data, record_at)[0]
            if 0x10000 <= address < 0x8000_0000_0000 and address not in seen:
                seen.add(address)
                addresses.append(address)
        offset = suffix_at + 1
    return addresses


def dll_internal_key_candidates(data: bytes) -> list[bytes]:
    """Extract functional 32-byte V4 XOR-mask candidates from a DLL byte range."""
    values: list[bytes] = []
    seen: set[bytes] = set()
    offset = 0
    while True:
        instruction_at = data.find(b"\x48\xBA", offset)
        if instruction_at < 0:
            break
        match = _V4_DLL_KEY_PATTERN.match(data[instruction_at : instruction_at + 85])
        if match:
            value = b"".join(match.groups())
            if value not in seen:
                seen.add(value)
                values.append(value)
            offset = instruction_at + len(match.group(0))
        else:
            offset = instruction_at + 1
    return values


def _potential_key(value: bytes) -> bool:
    if len(value) != 32 or len(set(value)) < 15:
        return False
    return sum(32 <= byte <= 126 for byte in value) <= 24


def resolve_v4_key_material(
    memory_candidates: Iterable[bytes],
    internal_key_candidates: Iterable[bytes],
    encrypted_page: bytes,
) -> tuple[bytes, bool] | None:
    """Resolve account-level V4 material without exposing it outside this seam.

    The bool records whether the valid memory value required the installation
    DLL mask. Both raw page keys and account-level derived keys are accepted.
    """
    if len(encrypted_page) != PAGE_SIZE:
        return None
    memory_values = list(dict.fromkeys(
        bytes(value) for value in memory_candidates if _potential_key(bytes(value))
    ))
    masks = list(dict.fromkeys(
        bytes(value) for value in internal_key_candidates if len(bytes(value)) == 32
    ))
    plans: list[tuple[bytes, bool]] = []
    for memory_value in memory_values:
        plans.extend(
            (
                bytes(left ^ right for left, right in zip(memory_value, mask)),
                True,
            )
            for mask in masks
        )
        plans.append((memory_value, False))
    if not plans:
        return None

    salt = encrypted_page[:SALT_SIZE]

    def verify(plan: tuple[bytes, bool]) -> tuple[bytes, bool] | None:
        material, used_mask = plan
        return plan if resolve_encryption_key(material, salt, encrypted_page) else None

    worker_count = max(4, min(32, os.cpu_count() or 4, len(plans)))
    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures = [executor.submit(verify, plan) for plan in plans]
    try:
        for future in as_completed(futures):
            value = future.result()
            if value is not None:
                return value
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
    return None


def _process_executable_path(pid: int) -> Path | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(size)
        ):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(process)


def _wechat_dll_paths(pids: Iterable[int]) -> list[Path]:
    values: list[Path] = []
    seen: set[str] = set()
    for pid in pids:
        executable = _process_executable_path(pid)
        if executable is None:
            continue
        install_dir = executable.parent
        candidates = [
            *(install_dir / name for name in ("Weixin.dll", "WeChat.dll")),
            *(install_dir.glob("*/Weixin.dll")),
            *(install_dir.glob("*/WeChat.dll")),
            *(install_dir.glob("install/*/Weixin.dll")),
            *(install_dir.glob("install/*/WeChat.dll")),
        ]
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                key = str(candidate.resolve()).casefold()
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                values.append(candidate)
    values.sort(
        key=lambda path: path.stat().st_mtime_ns if path.is_file() else 0,
        reverse=True,
    )
    return values


def _scan_dll_internal_keys(path: Path) -> list[bytes]:
    found: list[bytes] = []
    overlap = b""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(2 * 1024 * 1024):
                data = overlap + chunk
                for value in dll_internal_key_candidates(data):
                    if value not in found:
                        found.append(value)
                overlap = data[-128:]
    except OSError:
        return []
    return found


def _read_pointer_keys(pid: int, addresses: Iterable[int]) -> list[bytes]:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not process:
        return []
    values: list[bytes] = []
    try:
        for address in addresses:
            buffer = ctypes.create_string_buffer(32)
            read = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(
                process,
                ctypes.c_void_p(address),
                buffer,
                32,
                ctypes.byref(read),
            ) or read.value != 32:
                continue
            value = bytes(buffer.raw)
            if _potential_key(value) and value not in values:
                values.append(value)
    finally:
        kernel32.CloseHandle(process)
    return values


def scan_wechat_process_keys(
    root: Path,
    *,
    progress: Callable[[dict], None] | None = None,
) -> dict[str, str]:
    """Recover V4 account key material and return verified per-database keys."""
    if not sys_is_windows():
        raise ProxyTalkError("WECHAT_PLATFORM_UNSUPPORTED", "微信密钥扫描仅支持 Windows。")
    databases = collect_encrypted_databases(root)
    if not databases:
        raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "所选目录中没有可识别的微信数据库。")
    by_salt: dict[str, list[tuple[Path, bytes]]] = {}
    for path, salt, page in databases:
        by_salt.setdefault(salt, []).append((path, page))
    remaining = set(by_salt)
    found: dict[str, str] = {}
    pids = _weixin_pids()
    if not pids:
        raise ProxyTalkError("WECHAT_NOT_RUNNING", "Weixin.exe 未运行，请先登录微信。")

    internal_keys: list[bytes] = []
    dll_paths = _wechat_dll_paths(pids)
    for dll_path in dll_paths:
        for value in _scan_dll_internal_keys(dll_path):
            if value not in internal_keys:
                internal_keys.append(value)
    if progress:
        progress(
            {
                "phase": "installation_scan",
                "dllCount": len(dll_paths),
                "dllMaskCount": len(internal_keys),
                "found": len(found),
                "total": len(by_salt),
            }
        )

    total_scanned = 0
    total_pointer_candidates = 0
    for pid in pids:
        scanned = 0
        pointer_addresses: set[int] = set()
        text_candidates: list[bytes] = []
        for block in _readable_process_blocks(pid):
            scanned += len(block)
            total_scanned += len(block)
            pointer_addresses.update(pointer_record_addresses(block))
            for candidate in _key_candidates(block):
                key = bytes.fromhex(candidate)
                if key not in text_candidates:
                    text_candidates.append(key)
                for salt in tuple(remaining):
                    page_key = resolve_encryption_key(
                        key, bytes.fromhex(salt), by_salt[salt][0][1]
                    )
                    if page_key is not None:
                        found[salt] = page_key.hex()
                        remaining.remove(salt)
            if progress and scanned % (128 * 1024 * 1024) < len(block):
                progress(
                    {
                        "phase": "process_scan",
                        "pid": pid,
                        "scannedBytes": scanned,
                        "pointerCount": len(pointer_addresses),
                        "found": len(found),
                        "total": len(by_salt),
                    }
                )
            if not remaining:
                break

        if remaining:
            memory_candidates = _read_pointer_keys(pid, pointer_addresses)
            total_pointer_candidates += len(memory_candidates)
            for value in text_candidates:
                if value not in memory_candidates:
                    memory_candidates.append(value)
            probe_salt = min(
                remaining,
                key=lambda salt: _database_probe_priority(root, by_salt[salt][0][0]),
            )
            resolved = resolve_v4_key_material(
                memory_candidates,
                internal_keys,
                by_salt[probe_salt][0][1],
            )
            if resolved is not None:
                account_key, used_mask = resolved
                for salt in tuple(remaining):
                    page_key = resolve_encryption_key(
                        account_key,
                        bytes.fromhex(salt),
                        by_salt[salt][0][1],
                    )
                    if page_key is not None:
                        found[salt] = page_key.hex()
                        remaining.remove(salt)
                if progress:
                    progress(
                        {
                            "phase": "key_verification",
                            "pid": pid,
                            "candidateCount": len(memory_candidates),
                            "maskApplied": used_mask,
                            "found": len(found),
                            "total": len(by_salt),
                        }
                    )
        if not remaining:
            break
    if not found:
        message = (
            "无法读取微信进程内存，请以管理员身份运行 ProxyTalk 后重试。"
            if not total_scanned
            else "已扫描微信进程，但没有找到与当前账号数据库匹配的 V4 密钥。"
        )
        raise ProxyTalkError(
            "WECHAT_KEY_SCAN_FAILED",
            message,
            retryable=True,
            details={
                "databaseCount": len(databases),
                "processCount": len(pids),
                "scannedBytes": total_scanned,
                "dllMaskCount": len(internal_keys),
                "pointerCandidateCount": total_pointer_candidates,
            },
        )
    return found


def _database_probe_priority(root: Path, path: Path) -> tuple[int, str]:
    try:
        relative = path.relative_to(root).as_posix().casefold()
    except ValueError:
        relative = path.as_posix().casefold()
    if relative == "session/session.db":
        return 0, relative
    if relative == "message/message_0.db":
        return 1, relative
    if relative.startswith("message/"):
        return 2, relative
    return 3, relative


def _key_candidates(data: bytes):
    seen: set[str] = set()
    for match in re.finditer(rb"x'([0-9a-fA-F]{64,192})'", data):
        text = match.group(1).decode("ascii")
        values = []
        if len(text) == 64:
            values.append(text)
        elif len(text) >= 96 and len(text) % 2 == 0:
            values.append(text[:64])
        for value in values:
            lowered = value.lower()
            if lowered not in seen:
                seen.add(lowered)
                yield lowered


def _weixin_pids() -> list[int]:
    completed = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=0x08000000,
        check=False,
    )
    rows = csv.reader(io.StringIO(completed.stdout))
    values: list[tuple[int, int]] = []
    for row in rows:
        if len(row) < 5 or row[0].lower() != "weixin.exe":
            continue
        try:
            memory = int(re.sub(r"\D", "", row[4]) or "0")
            values.append((int(row[1]), memory))
        except ValueError:
            continue
    return [pid for pid, _ in sorted(values, key=lambda item: item[1], reverse=True)]


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD),
        ("_alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("_alignment2", wintypes.DWORD),
    ]


def _readable_process_blocks(pid: int):
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not process:
        return
    readable = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
    try:
        address = 0
        info = _MemoryBasicInformation()
        while address < 0x7FFF_FFFF_FFFF:
            queried = kernel32.VirtualQueryEx(
                process,
                ctypes.c_void_p(address),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not queried:
                break
            base = int(info.BaseAddress)
            region_size = int(info.RegionSize)
            if info.State == 0x1000 and (info.Protect & 0xFF) in readable and region_size:
                offset = 0
                overlap = b""
                while offset < region_size:
                    request_size = min(8 * 1024 * 1024, region_size - offset)
                    buffer = ctypes.create_string_buffer(request_size)
                    read = ctypes.c_size_t()
                    ok = kernel32.ReadProcessMemory(
                        process,
                        ctypes.c_void_p(base + offset),
                        buffer,
                        request_size,
                        ctypes.byref(read),
                    )
                    if ok and read.value:
                        block = overlap + buffer.raw[: read.value]
                        yield block
                        overlap = block[-256:]
                    else:
                        overlap = b""
                    offset += request_size
            next_address = base + region_size
            if next_address <= address:
                break
            address = next_address
    finally:
        kernel32.CloseHandle(process)


def sys_is_windows() -> bool:
    import sys

    return sys.platform == "win32"
