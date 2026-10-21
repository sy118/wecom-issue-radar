from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import csv
import ctypes
from ctypes import wintypes
import hashlib
import heapq
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from typing import Callable, Iterable, Iterator, Sequence

from Crypto.Cipher import AES


V2_MAGIC = b"\x07\x08V2\x08\x07"
V2_CIPHERTEXT_OFFSET = 0x0F
AES_BLOCK_SIZE = 16

_KVCOMM_NAME = re.compile(r"^key_(\d+)_.+\.statistic$", re.IGNORECASE)
_MONTH_NAME = re.compile(r"^\d{4}-\d{2}$")
_ASCII_CANDIDATE = re.compile(rb"(?<![A-Za-z0-9])([A-Za-z0-9]{32})(?![A-Za-z0-9])")
_UTF16_CANDIDATE = re.compile(
    rb"(?<![A-Za-z0-9]\x00)((?:[A-Za-z0-9]\x00){32})(?![A-Za-z0-9]\x00)"
)

_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_MEM_COMMIT = 0x1000
_PAGE_NOACCESS = 0x01
_PAGE_READWRITE = 0x04
_PAGE_WRITECOPY = 0x08
_PAGE_EXECUTE_READWRITE = 0x40
_PAGE_EXECUTE_WRITECOPY = 0x80
_PAGE_GUARD = 0x100
_MAX_REGION_BYTES = 50 * 1024 * 1024
_CHUNK_BYTES = 4 * 1024 * 1024
_CHUNK_OVERLAP = 68
_MAX_USER_ADDRESS = 0x7FFF_FFFF_FFFF

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ImageKeyPair:
    aes_key: str
    xor_key: int
    verified: bool = True


@dataclass(frozen=True, slots=True)
class ImageKeyScanResult:
    pair: ImageKeyPair | None
    method: str
    template_count: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _V2Sample:
    ciphertext: bytes
    encoded_tail: bytes
    path: Path | None = None
    modified_ns: int = 0


def _clean_wxid(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    match = re.fullmatch(r"(wxid_[^_]+)(?:_.+)", candidate, flags=re.IGNORECASE)
    return match.group(1) if match else candidate


def derive_image_key_pair(code: int, wxid: str) -> ImageKeyPair:
    if isinstance(code, bool) or not isinstance(code, int) or not 0 < code <= 0xFFFFFFFF:
        raise ValueError("code must be in the range 1..0xffffffff")
    account = str(wxid or "").strip()
    if not account:
        raise ValueError("wxid must not be empty")
    digest = hashlib.md5(f"{code}{account}".encode("utf-8")).hexdigest()
    return ImageKeyPair(aes_key=digest[:16], xor_key=code & 0xFF)


def _sample_from_bytes(data: bytes, path: Path | None = None, modified_ns: int = 0) -> _V2Sample | None:
    if len(data) < V2_CIPHERTEXT_OFFSET + AES_BLOCK_SIZE or not data.startswith(V2_MAGIC):
        return None
    try:
        aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    except struct.error:
        return None
    if aes_size <= 0 or xor_size < 2:
        return None
    ciphertext = data[V2_CIPHERTEXT_OFFSET : V2_CIPHERTEXT_OFFSET + AES_BLOCK_SIZE]
    return _V2Sample(ciphertext, data[-2:], path, modified_ns)


def _normalise_samples(values: Iterable[bytes | _V2Sample]) -> tuple[_V2Sample, ...]:
    samples: list[_V2Sample] = []
    for value in values:
        sample = value if isinstance(value, _V2Sample) else _sample_from_bytes(bytes(value))
        if sample is not None:
            samples.append(sample)
    return tuple(samples)


def _detect_image_header(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "webp"
    if raw.startswith((b"wxgf", b"WXGF")):
        return "wxgf"
    return ""


def _decrypt_header(aes_key: str, ciphertext: bytes) -> bytes:
    try:
        key = aes_key.encode("ascii")
    except UnicodeEncodeError:
        return b""
    if len(key) != AES_BLOCK_SIZE or len(ciphertext) != AES_BLOCK_SIZE:
        return b""
    try:
        return AES.new(key, AES.MODE_ECB).decrypt(ciphertext)
    except ValueError:
        return b""


def _jpeg_tail_xor(encoded_tail: bytes) -> int | None:
    if len(encoded_tail) != 2:
        return None
    first = encoded_tail[0] ^ 0xFF
    second = encoded_tail[1] ^ 0xD9
    return first if first == second else None


def _verified_xor(aes_key: str, samples: Sequence[_V2Sample]) -> int | None:
    recognised = False
    jpeg_xors: list[int] = []
    for sample in samples:
        image_format = _detect_image_header(_decrypt_header(aes_key, sample.ciphertext))
        if not image_format:
            continue
        recognised = True
        if image_format == "jpeg":
            inferred = _jpeg_tail_xor(sample.encoded_tail)
            if inferred is not None:
                jpeg_xors.append(inferred)
    if not recognised or not jpeg_xors:
        return None
    return Counter(jpeg_xors).most_common(1)[0][0]


def resolve_derived_image_key(
    codes: Iterable[int],
    wxids: Iterable[str],
    templates: Iterable[bytes | _V2Sample],
) -> ImageKeyPair | None:
    samples = _normalise_samples(templates)
    if not samples:
        return None
    valid_codes: set[int] = set()
    for value in codes:
        if isinstance(value, bool):
            continue
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < numeric <= 0xFFFFFFFF:
            valid_codes.add(numeric)
    unique_codes = sorted(valid_codes)
    account_candidates: list[str] = []
    for value in wxids:
        raw_value = str(value or "").strip()
        cleaned_value = _clean_wxid(raw_value)
        for candidate in (raw_value, cleaned_value):
            if candidate and candidate not in account_candidates:
                account_candidates.append(candidate)
    unique_wxids = tuple(account_candidates)
    for wxid in unique_wxids:
        for code in unique_codes:
            pair = derive_image_key_pair(code, wxid)
            inferred_xor = _verified_xor(pair.aes_key, samples)
            if inferred_xor == pair.xor_key:
                return pair
    return None


def _memory_candidates(data: bytes) -> Iterator[str]:
    seen: set[str] = set()
    for match in _ASCII_CANDIDATE.finditer(data):
        candidate = match.group(1)[:AES_BLOCK_SIZE].decode("ascii")
        if candidate not in seen:
            seen.add(candidate)
            yield candidate
    for match in _UTF16_CANDIDATE.finditer(data):
        candidate = match.group(1)[::2][:AES_BLOCK_SIZE].decode("ascii")
        if candidate not in seen:
            seen.add(candidate)
            yield candidate


def resolve_memory_image_key(
    memory_chunks: Iterable[bytes],
    templates: Iterable[bytes | _V2Sample],
) -> ImageKeyPair | None:
    samples = _normalise_samples(templates)
    if not samples:
        return None
    for chunk in memory_chunks:
        for aes_key in _memory_candidates(bytes(chunk)):
            xor_key = _verified_xor(aes_key, samples)
            if xor_key is not None:
                return ImageKeyPair(aes_key=aes_key, xor_key=xor_key)
    return None


def resolve_aes_image_key(
    aes_key: str,
    templates: Iterable[bytes | _V2Sample],
) -> ImageKeyPair | None:
    samples = _normalise_samples(templates)
    xor_key = _verified_xor(aes_key, samples)
    return ImageKeyPair(aes_key=aes_key, xor_key=xor_key) if xor_key is not None else None


def _child_directories(path: Path) -> list[Path]:
    result: list[Path] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        result.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return result


def _offer_templates(directory: Path, heap: list[tuple[int, str]], capacity: int) -> None:
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.casefold().endswith("_t.dat"):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    modified_ns = entry.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
                item = (modified_ns, entry.path)
                if len(heap) < capacity:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)
    except OSError:
        return


def _candidate_template_paths(account_dir: Path, capacity: int) -> tuple[Path, ...]:
    heap: list[tuple[int, str]] = []
    attach_root = account_dir / "msg" / "attach"
    chat_directories = _child_directories(attach_root)

    def modified_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    chat_directories.sort(key=modified_ns, reverse=True)
    visited = 0
    for chat_directory in chat_directories:
        if visited >= 2_000:
            break
        visited += 1
        months = [item for item in _child_directories(chat_directory) if _MONTH_NAME.fullmatch(item.name)]
        months.sort(key=lambda item: item.name, reverse=True)
        for month in months:
            if visited >= 2_000:
                break
            visited += 1
            for child in _child_directories(month):
                if child.name.casefold() == "img":
                    _offer_templates(child, heap, capacity)

    if not heap:
        queue = [item for item in (account_dir / "msg", account_dir / "cache", account_dir / "resource") if item.is_dir()]
        cursor = 0
        while cursor < len(queue) and cursor < 500:
            directory = queue[cursor]
            cursor += 1
            _offer_templates(directory, heap, capacity)
            for child in _child_directories(directory):
                lowered = child.name.casefold()
                if "emoticon" in lowered or "thumb" in lowered:
                    continue
                if len(queue) < 500:
                    queue.append(child)
    return tuple(Path(value[1]) for value in sorted(heap, reverse=True))


def scan_v2_image_templates(account_dir: Path, *, limit: int = 64) -> tuple[_V2Sample, ...]:
    if limit <= 0:
        return ()
    paths = _candidate_template_paths(account_dir, max(64, limit * 4))
    samples: list[_V2Sample] = []
    for path in paths:
        if len(samples) >= limit:
            break
        try:
            with path.open("rb") as stream:
                header = stream.read(V2_CIPHERTEXT_OFFSET + AES_BLOCK_SIZE)
                if len(header) < V2_CIPHERTEXT_OFFSET + AES_BLOCK_SIZE or not header.startswith(V2_MAGIC):
                    continue
                stream.seek(-2, os.SEEK_END)
                encoded_tail = stream.read(2)
            sample = _sample_from_bytes(header + encoded_tail, path, path.stat().st_mtime_ns)
        except (OSError, ValueError):
            continue
        if sample is not None:
            samples.append(sample)
    return tuple(samples)


def _kvcomm_codes(account_dir: Path) -> tuple[int, ...]:
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    candidates = [
        appdata / "Tencent" / "xwechat" / "net" / "kvcomm",
        account_dir / "app_data" / "net" / "kvcomm",
        account_dir.parent / "app_data" / "net" / "kvcomm",
        account_dir.parent.parent / "app_data" / "net" / "kvcomm",
    ]
    codes: set[int] = set()
    seen: set[str] = set()
    for directory in candidates:
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    match = _KVCOMM_NAME.fullmatch(entry.name)
                    if not match:
                        continue
                    value = int(match.group(1))
                    if 0 < value <= 0xFFFFFFFF:
                        codes.add(value)
        except (OSError, ValueError):
            continue
    return tuple(sorted(codes))


def wechat_process_ids() -> tuple[int, ...]:
    if sys.platform != "win32":
        return ()
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=0x08000000,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    pids: set[int] = set()
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) < 2 or row[0].casefold() not in {"weixin.exe", "wechat.exe"}:
            continue
        try:
            pids.add(int(row[1].replace(",", "")))
        except ValueError:
            continue
    return tuple(sorted(value for value in pids if value > 0))


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _scan_process_memory(
    pid: int,
    samples: Sequence[_V2Sample],
    deadline: float,
    progress: ProgressCallback | None,
) -> ImageKeyPair | None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryBasicInformation),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
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

    handle = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    if progress:
        progress(f"正在验证微信进程 {pid} 的图片密钥候选")
    try:
        address = 0
        while address < _MAX_USER_ADDRESS and time.monotonic() < deadline:
            info = _MemoryBasicInformation()
            queried = kernel32.VirtualQueryEx(
                handle,
                ctypes.c_void_p(address),
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not queried:
                break
            base_address = int(info.BaseAddress or 0)
            region_size = int(info.RegionSize)
            next_address = base_address + region_size
            if next_address <= address:
                break
            protection = int(info.Protect)
            writable = (protection & 0xFF) in {
                _PAGE_READWRITE,
                _PAGE_WRITECOPY,
                _PAGE_EXECUTE_READWRITE,
                _PAGE_EXECUTE_WRITECOPY,
            }
            scannable = (
                int(info.State) == _MEM_COMMIT
                and writable
                and not protection & (_PAGE_GUARD | _PAGE_NOACCESS)
                and 0 < region_size <= _MAX_REGION_BYTES
            )
            if scannable:
                offset = 0
                overlap = b""
                while offset < region_size and time.monotonic() < deadline:
                    request_size = min(_CHUNK_BYTES, region_size - offset)
                    buffer = ctypes.create_string_buffer(request_size)
                    bytes_read = ctypes.c_size_t(0)
                    ok = kernel32.ReadProcessMemory(
                        handle,
                        ctypes.c_void_p(base_address + offset),
                        buffer,
                        request_size,
                        ctypes.byref(bytes_read),
                    )
                    if ok and bytes_read.value:
                        chunk = overlap + buffer.raw[: bytes_read.value]
                        pair = resolve_memory_image_key((chunk,), samples)
                        if pair is not None:
                            return pair
                        overlap = chunk[-_CHUNK_OVERLAP:] if bytes_read.value == request_size else b""
                    else:
                        overlap = b""
                    offset += request_size
            address = next_address
    finally:
        kernel32.CloseHandle(handle)
    return None


def scan_local_image_key(
    account_dir: Path,
    *,
    wxids: Iterable[str] = (),
    timeout_seconds: float = 45,
    progress: ProgressCallback | None = None,
) -> ImageKeyScanResult:
    account_dir = account_dir.expanduser().resolve()
    samples = scan_v2_image_templates(account_dir)
    if not samples:
        return ImageKeyScanResult(None, "", 0, "no_templates")
    codes = _kvcomm_codes(account_dir)
    account_candidates = [*wxids, account_dir.name, _clean_wxid(account_dir.name), "unknown"]
    pair = resolve_derived_image_key(codes, account_candidates, samples)
    if pair is not None:
        return ImageKeyScanResult(pair, "kvcomm", len(samples))
    if sys.platform != "win32":
        return ImageKeyScanResult(None, "", len(samples), "platform_unsupported")
    pids = wechat_process_ids()
    if not pids:
        return ImageKeyScanResult(None, "", len(samples), "wechat_not_running")
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    for pid in pids:
        if time.monotonic() >= deadline:
            break
        pair = _scan_process_memory(pid, samples, deadline, progress)
        if pair is not None:
            return ImageKeyScanResult(pair, "memory", len(samples))
    return ImageKeyScanResult(None, "", len(samples), "not_found")


__all__ = [
    "ImageKeyPair",
    "ImageKeyScanResult",
    "derive_image_key_pair",
    "resolve_aes_image_key",
    "resolve_derived_image_key",
    "resolve_memory_image_key",
    "scan_local_image_key",
    "scan_v2_image_templates",
    "wechat_process_ids",
]
