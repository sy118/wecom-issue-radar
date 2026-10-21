from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
from typing import Iterable
import wave

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .errors import ProxyTalkError
from .models import Chat, SourceCursor, SourceMessage
from .vault import SecretVault
from .wechat_content import decode_content, parse_message, split_group_sender
from .wechat_crypto import (
    collect_encrypted_databases,
    copy_stable_database,
    decrypt_database,
    scan_wechat_process_keys,
)
from .wechat_image import (
    ImageKeyPair,
    resolve_aes_image_key,
    scan_local_image_key,
    scan_v2_image_templates,
    wechat_process_ids,
)


KEY_VAULT_NAME = "wechat:sqlcipher-keys"
IMAGE_KEY_VAULT_NAME = "wechat:image-aes-key"

_WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  } |
  Select-Object -First 1
function Await-Result($operation, $resultType) {
  $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
  $task.Wait()
  return $task.Result
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:PROXYTALK_OCR_INPUT)) ([Windows.Storage.StorageFile])
$stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
try {
  $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) { throw 'No Windows OCR language is installed.' }
  $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  [Console]::Out.Write($result.Text)
} finally {
  if ($null -ne $stream) { $stream.Dispose() }
}
"""


def discover_wechat_roots() -> list[Path]:
    candidates: list[Path] = []
    home = Path.home()
    parents = [home / "Documents" / "xwechat_files"]
    one_drive = home / "OneDrive" / "Documents" / "xwechat_files"
    if one_drive.is_dir():
        parents.append(one_drive)
    for parent in parents:
        if not parent.is_dir():
            continue
        for path in parent.glob("*/db_storage"):
            if (path / "session" / "session.db").is_file():
                candidates.append(path.resolve())
    unique = {str(path).lower(): path for path in candidates}
    return sorted(unique.values(), key=lambda path: path.stat().st_mtime, reverse=True)


def wechat_is_running() -> bool:
    return bool(wechat_process_ids())


class LocalWeChatSource:
    """Deep module for all local WeChat discovery, decryption, schema and cursor logic."""

    def __init__(self, store, vault: SecretVault, temporary_root: Path, *, event_sink=None) -> None:
        self.store = store
        self.vault = vault
        self.temporary_root = temporary_root.resolve()
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.event_sink = event_sink or (lambda _event: None)
        self._lock = threading.RLock()
        self._transcription_cache: dict[str, str] = {}
        self._reported_diagnostics: set[str] = set()
        self._shard_fingerprints: dict[str, tuple] = {}
        self._shard_tables: dict[str, set[str]] = {}
        self._contacts_fingerprint: tuple | None = None
        self._contacts_cache: dict[str, str] = {}
        self._cleanup_stale_snapshots()

    def inspect(self) -> dict:
        configured = str(self.store.get_setting("wechatDbDir", "") or "")
        roots = discover_wechat_roots()
        if configured:
            path = Path(configured).expanduser()
            if path.is_dir() and all(path.resolve() != item for item in roots):
                roots.insert(0, path.resolve())
        keys = self._keys()
        key_count = len(keys)
        selected = Path(configured) if configured else (roots[0] if roots else None)
        databases = collect_encrypted_databases(selected) if selected and selected.is_dir() else []
        covered = sum(1 for _, salt, _ in databases if salt in keys)
        image_pair = self._image_key_pair()
        return {
            "platformSupported": _is_windows(),
            "running": wechat_is_running(),
            "configuredDbDir": str(selected or ""),
            "accounts": [
                {"accountId": path.parent.name, "dbDir": str(path), "selected": bool(selected and path == selected.resolve())}
                for path in roots
            ],
            "keyCount": key_count,
            "databaseCount": len(databases),
            "coveredDatabaseCount": covered,
            "ready": bool(selected and databases and covered == len(databases)),
            "imageKeyConfigured": image_pair is not None,
            "imageKeyVerified": bool(image_pair and image_pair.verified),
        }

    def save_image_key(self, value: str) -> dict:
        key = value.strip()
        if not key:
            self.vault.delete(IMAGE_KEY_VAULT_NAME)
            return self.inspect()
        try:
            encoded = key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProxyTalkError("WECHAT_IMAGE_KEY_INVALID", "图片 AES Key 必须是 16 个 ASCII 字符。") from exc
        if len(encoded) != 16:
            raise ProxyTalkError("WECHAT_IMAGE_KEY_INVALID", "图片 AES Key 必须是 16 个 ASCII 字符。")
        root = self._configured_root()
        templates = scan_v2_image_templates(root.parent)
        if not templates:
            raise ProxyTalkError(
                "WECHAT_IMAGE_TEMPLATE_MISSING",
                "没有找到可用于验证的 V2 图片。请先在微信中打开几张普通图片，再重试。",
                retryable=True,
            )
        pair = resolve_aes_image_key(key, templates)
        if pair is None:
            raise ProxyTalkError(
                "WECHAT_IMAGE_KEY_INVALID",
                "这个 AES Key 无法通过本机 V2 图片验证，未保存。",
                retryable=True,
            )
        self._save_image_key_pair(pair)
        return self.inspect()

    def scan_image_key(self) -> dict:
        root = self._configured_root()
        self.event_sink({"kind": "runtime.status", "scope": "wechat_image", "status": "scanning"})
        result = scan_local_image_key(
            root.parent,
            wxids=(root.parent.name,),
            progress=lambda message: self.event_sink(
                {
                    "kind": "runtime.status",
                    "scope": "wechat_image",
                    "status": "scanning",
                    "message": message,
                }
            ),
        )
        if result.pair is None:
            messages = {
                "no_templates": "没有找到 V2 图片。请保持微信运行，先打开几张普通图片，再重新扫描。",
                "wechat_not_running": "未检测到正在运行的微信，无法扫描图片密钥。",
                "platform_unsupported": "图片密钥自动扫描目前只支持 Windows。",
                "not_found": "找到 V2 图片，但没有发现能通过真实图片验证的密钥。请在微信中打开图片后重试；必要时以管理员身份运行。",
            }
            raise ProxyTalkError(
                "WECHAT_IMAGE_KEY_NOT_FOUND",
                messages.get(result.reason, "没有找到可验证的 V2 图片密钥。"),
                retryable=True,
                details={"reason": result.reason, "templateCount": result.template_count},
            )
        self._save_image_key_pair(result.pair)
        status = self.inspect()
        self.event_sink(
            {
                "kind": "runtime.status",
                "scope": "wechat_image",
                "status": "ready",
                "method": result.method,
                "templateCount": result.template_count,
            }
        )
        return status

    def unlock(self, db_dir: str) -> dict:
        root = self._validate_root(Path(db_dir))
        self.event_sink({"kind": "runtime.status", "scope": "wechat", "status": "scanning_keys"})
        keys = scan_wechat_process_keys(
            root,
            progress=lambda value: self.event_sink({"kind": "runtime.status", "scope": "wechat", "status": "scanning_keys", **value}),
        )
        merged_keys = self._keys()
        merged_keys.update(keys)
        self.vault.put_text(KEY_VAULT_NAME, json.dumps(merged_keys, separators=(",", ":")))
        self.store.save_settings({"wechatDbDir": str(root)})
        result = self.inspect()
        self.event_sink({"kind": "runtime.status", "scope": "wechat", "status": "ready", "details": result})
        return result

    def select_root(self, db_dir: str) -> dict:
        root = self._validate_root(Path(db_dir))
        self.store.save_settings({"wechatDbDir": str(root)})
        return self.inspect()

    def list_chats(self) -> list[dict]:
        root = self._configured_root()
        contacts = self._contacts(root)
        account_id = root.parent.name
        with self._open_database(root, Path("session/session.db")) as conn:
            tables = self._tables(conn)
            if "SessionTable" not in tables:
                raise ProxyTalkError("WECHAT_SCHEMA_UNSUPPORTED", "当前微信 session.db 缺少 SessionTable。")
            columns = self._columns(conn, "SessionTable")
            required = {"username", "last_timestamp"}
            if not required.issubset(columns):
                raise ProxyTalkError("WECHAT_SCHEMA_UNSUPPORTED", "当前微信会话表字段不受支持。")
            select = [
                "username",
                "last_timestamp",
                "unread_count" if "unread_count" in columns else "0 AS unread_count",
                "summary" if "summary" in columns else "'' AS summary",
                "WCDB_CT_summary" if "WCDB_CT_summary" in columns else "0 AS WCDB_CT_summary",
            ]
            rows = conn.execute(
                f"SELECT {','.join(select)} FROM SessionTable WHERE last_timestamp>0 ORDER BY last_timestamp DESC LIMIT 1000"
            ).fetchall()
        chats = []
        for row in rows:
            chat_id = str(row["username"] or "")
            if not chat_id:
                continue
            summary = decode_content(row["summary"], int(row["WCDB_CT_summary"] or 0))
            _, summary = split_group_sender(summary, chat_id.endswith(("@chatroom", "@openim")))
            chats.append(
                Chat(
                    account_id=account_id,
                    chat_id=chat_id,
                    display_name=contacts.get(chat_id, chat_id),
                    kind="group" if chat_id.endswith(("@chatroom", "@openim")) else "direct",
                    last_message=summary[:300],
                    last_timestamp=int(row["last_timestamp"] or 0),
                    unread=int(row["unread_count"] or 0),
                ).public()
            )
        return chats

    def poll(self, projects: Iterable[dict], *, backlog: bool) -> tuple[list[SourceMessage], list[SourceCursor]]:
        projects = list(projects)
        if not projects:
            return [], []
        root = self._configured_root()
        configured_account = root.parent.name
        matching_projects = [
            project for project in projects
            if str(project.get("accountId") or "") == configured_account
        ]
        if len(matching_projects) != len(projects):
            self._report_once(
                "WECHAT_ACCOUNT_MISMATCH",
                "当前微信账号与部分项目不一致；这些项目已暂停读取。",
                scope="wechat",
            )
        projects = matching_projects
        if not projects:
            return [], []
        contacts = self._contacts(root)
        all_messages: list[SourceMessage] = []
        all_cursors: list[SourceCursor] = []
        shard_paths = sorted(
            path
            for path in (root / "message").glob("message_*.db")
            if not path.name.endswith(("_fts.db", "_resource.db"))
        )
        project_states = [
            (
                project,
                f"Msg_{hashlib.md5(project['chatId'].encode('utf-8')).hexdigest()}",
                self.store.cursors(project["id"]),
            )
            for project in projects
        ]
        for shard in shard_paths:
            relative = shard.relative_to(root)
            relative_text = relative.as_posix()
            fingerprint_key = str(shard.resolve()).casefold()
            fingerprint = self._database_fingerprint(shard)
            known_tables = self._shard_tables.get(fingerprint_key)
            needs_initial_read = known_tables is None or any(
                table in known_tables and relative_text not in cursor_map
                for _project, table, cursor_map in project_states
            )
            if (
                not needs_initial_read
                and self._shard_fingerprints.get(fingerprint_key) == fingerprint
            ):
                continue
            try:
                with self._open_database(root, relative) as conn:
                    tables = self._tables(conn)
                    self._shard_tables[fingerprint_key] = tables
                    for project, table, cursor_map in project_states:
                        if table not in tables:
                            continue
                        initialized = relative_text in cursor_map
                        cursor = cursor_map.get(relative_text, SourceCursor(project["id"], relative_text))
                        messages, next_cursor = self._read_chat_table(
                            conn,
                            project,
                            table,
                            relative_text,
                            cursor,
                            contacts,
                            backlog or not initialized,
                        )
                        all_messages.extend(messages)
                        if next_cursor:
                            all_cursors.append(next_cursor)
                        elif not initialized:
                            # Persist an explicit zero cursor for an empty table so its
                            # first future row is live rather than repeatedly "first scan".
                            all_cursors.append(cursor)
                self._shard_fingerprints[fingerprint_key] = fingerprint
            except ProxyTalkError as exc:
                if exc.code in {"WECHAT_KEY_MISSING", "WECHAT_SNAPSHOT_BUSY"}:
                    self.event_sink({"kind": "runtime.error", "scope": "wechat", "code": exc.code, "message": exc.message})
                    continue
                raise
        all_messages.sort(key=lambda item: (item.create_time, item.sort_seq, item.local_id, item.source_key))
        return all_messages, all_cursors

    def materialize_media(self, project: dict, message: dict, destination: Path) -> list[dict]:
        """Best-effort media export. Missing or unsupported media remains explicit metadata."""
        output = []
        for index, media in enumerate(message.get("media") or []):
            entry = dict(media)
            message_token = re.sub(r"[^A-Za-z0-9_-]", "_", str(message.get("id") or ""))[:12] or "message"
            if media.get("kind") in {"image", "sticker"}:
                resolved = self._resolve_image(project, int(message.get("localId") or 0))
                if resolved:
                    raw, extension, source = resolved
                    filename = f"{int(message.get('createTime') or 0)}-{message_token}-{index}.{extension}"
                    path = destination / filename
                    path.write_bytes(raw)
                    entry.update({"available": True, "path": f"media/{filename}", "sourceName": source.name})
                else:
                    entry.update({"available": False, "reason": "未找到可验证或可解密的本地微信图片"})
            elif media.get("kind") == "voice":
                resolved_voice = self._resolve_voice(project, message)
                if resolved_voice:
                    filename = f"{int(message.get('createTime') or 0)}-{message_token}-{index}.wav"
                    path = destination / filename
                    try:
                        self._decode_silk_to_wav(resolved_voice[0], path)
                        entry.update({"available": True, "path": f"media/{filename}", "sourceName": filename})
                    except (OSError, RuntimeError, ValueError):
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        entry.update({"available": False, "reason": "本地微信语音解码失败"})
                else:
                    entry.update({"available": False, "reason": "未找到对应的本地微信语音"})
            else:
                entry.setdefault("available", False)
                entry.setdefault("reason", "当前微信媒体索引未提供可验证的本地文件")
            output.append(entry)
        return output

    def image_preview(
        self,
        project: dict,
        message: dict,
        *,
        max_bytes: int = 12 * 1024 * 1024,
    ) -> dict:
        """Return one bounded image payload without exposing a local filesystem path."""
        if message.get("projectId") != project.get("id"):
            raise ProxyTalkError("MEDIA_MESSAGE_MISMATCH", "图片消息不属于这个聊天项目。")
        if message.get("kind") != "image":
            raise ProxyTalkError("MEDIA_KIND_UNSUPPORTED", "这条消息不是可预览的图片消息。")
        resolved = self._resolve_image(project, int(message.get("localId") or 0))
        if not resolved:
            return {
                "available": False,
                "mimeType": "",
                "dataBase64": "",
                "reason": "未找到可验证或可解密的本地微信图片",
            }
        raw, extension, _ = resolved
        if len(raw) > max_bytes:
            return {
                "available": False,
                "mimeType": "",
                "dataBase64": "",
                "reason": "图片超过 12 MiB 的内联预览上限，可通过导出查看",
            }
        mime_type = {
            "jpg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(extension, "")
        if not mime_type:
            return {
                "available": False,
                "mimeType": "",
                "dataBase64": "",
                "reason": "图片格式不支持内联预览",
            }
        return {
            "available": True,
            "mimeType": mime_type,
            "dataBase64": base64.b64encode(raw).decode("ascii"),
            "reason": "",
        }

    def enrich_for_model(
        self,
        project: dict,
        messages: list[dict],
        *,
        supports_vision: bool = True,
        max_images: int = 4,
        max_voices: int = 3,
        max_image_bytes: int = 20 * 1024 * 1024,
    ) -> list[dict]:
        enriched = [dict(message) for message in messages]
        remaining = max_images
        remaining_image_bytes = max_image_bytes
        for message in reversed(enriched):
            if remaining <= 0 or message.get("kind") != "image":
                continue
            resolved = self._resolve_image(project, int(message.get("localId") or 0))
            if not resolved:
                continue
            raw, extension, _ = resolved
            if len(raw) > remaining_image_bytes:
                continue
            mime = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(extension)
            if not mime:
                continue
            if supports_vision:
                message["images"] = [{"mimeType": mime, "base64": base64.b64encode(raw).decode("ascii")}]
            else:
                text = self._windows_ocr(raw, extension)
                if text:
                    message["text"] = f"{message.get('text') or '[图片]'}\n[本地 OCR]\n{text}"
            remaining -= 1
            remaining_image_bytes -= len(raw)
        settings = self.store.settings()
        if settings.get("voiceTranscription") and settings.get("voiceCommand"):
            remaining_voices = max_voices
            for message in reversed(enriched):
                if remaining_voices <= 0 or message.get("kind") != "voice":
                    continue
                transcript = self._transcribe_voice(project, message, settings)
                if transcript:
                    message["text"] = f"{message.get('text') or '[语音]'}\n[本地语音转录]\n{transcript}"
                remaining_voices -= 1
        return enriched

    def _transcribe_voice(self, project: dict, message: dict, settings: dict) -> str:
        command = str(settings.get("voiceCommand") or "").strip()
        resolved_command = shutil.which(command) or (command if Path(command).is_file() else "")
        if not resolved_command:
            self._report_once("VOICE_COMMAND_NOT_FOUND", "找不到配置的本地语音转录命令。")
            return ""
        local_id = int(message.get("localId") or 0)
        voice = self._resolve_voice(project, message)
        if not voice:
            return ""
        data, _ = voice
        cache_material = "|".join(
            [
                project["chatId"],
                str(local_id),
                str(message.get("sourceDb") or ""),
                str(message.get("createTime") or 0),
                command,
                str(settings.get("voiceModelPath") or ""),
                repr(settings.get("voiceArguments") or []),
            ]
        )
        cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
        if cache_key in self._transcription_cache:
            return self._transcription_cache[cache_key]
        work = Path(tempfile.mkdtemp(prefix="snapshot-voice-", dir=self.temporary_root))
        try:
            wav_path = work / "voice.wav"
            self._decode_silk_to_wav(data, wav_path)
            model_path = str(settings.get("voiceModelPath") or "")
            argument_templates = settings.get("voiceArguments") or ["{input}"]
            arguments = [
                str(item).replace("{input}", str(wav_path)).replace("{model}", model_path)
                for item in argument_templates
            ]
            completed = subprocess.run(
                [resolved_command, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
                creationflags=0x08000000 if _is_windows() else 0,
            )
            if completed.returncode:
                self._report_once("VOICE_TRANSCRIPTION_FAILED", "本地语音转录命令执行失败。")
                return ""
            transcript = completed.stdout.strip()[:12_000]
            if transcript:
                self._transcription_cache[cache_key] = transcript
            return transcript
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            self._report_once("VOICE_TRANSCRIPTION_FAILED", "本地语音转录失败。")
            return ""
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _resolve_voice(self, project: dict, message: dict) -> tuple[bytes, int] | None:
        root = self._root_for_project(project)
        if root is None:
            return None
        local_id = int(message.get("localId") or 0)
        create_time = int(message.get("createTime") or 0)
        paths = sorted((root / "message").glob("media_*.db"))
        shard_match = re.search(r"message_(\d+)\.db$", str(message.get("sourceDb") or ""))
        if shard_match:
            preferred = root / "message" / f"media_{shard_match.group(1)}.db"
            paths.sort(key=lambda item: 0 if item == preferred else 1)
        for path in paths:
            relative = path.relative_to(root)
            try:
                with self._open_database(root, relative) as conn:
                    if not {"Name2Id", "VoiceInfo"}.issubset(self._tables(conn)):
                        continue
                    chat = conn.execute(
                        "SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1", (project["chatId"],)
                    ).fetchone()
                    if not chat:
                        continue
                    if create_time:
                        row = conn.execute(
                            "SELECT voice_data,create_time FROM VoiceInfo WHERE chat_name_id=? AND local_id=? ORDER BY ABS(create_time-?) LIMIT 1",
                            (int(chat[0]), local_id, create_time),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT voice_data,create_time FROM VoiceInfo WHERE chat_name_id=? AND local_id=? LIMIT 1",
                            (int(chat[0]), local_id),
                        ).fetchone()
                    if row and isinstance(row[0], bytes):
                        return bytes(row[0]), int(row[1] or 0)
            except (ProxyTalkError, sqlite3.DatabaseError):
                continue
        return None

    @staticmethod
    def _decode_silk_to_wav(data: bytes, destination: Path) -> None:
        import pysilk

        silk = data[1:] if data[:1] == b"\x02" else data
        decoded = io.BytesIO()
        pysilk.decode(io.BytesIO(silk), decoded, 24000)
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(decoded.getvalue())

    def _windows_ocr(self, data: bytes, extension: str) -> str:
        if not _is_windows():
            return ""
        work = Path(tempfile.mkdtemp(prefix="snapshot-ocr-", dir=self.temporary_root))
        try:
            image_path = work / f"image.{extension}"
            image_path.write_bytes(data)
            script = _WINDOWS_OCR_SCRIPT
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            environment = {**os.environ, "PROXYTALK_OCR_INPUT": str(image_path)}
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
                creationflags=0x08000000,
                env=environment,
            )
            if completed.returncode:
                self._report_once("OCR_UNAVAILABLE", "Windows 本地 OCR 不可用；图片仍会以占位信息加入上下文。")
                return ""
            return completed.stdout.strip()[:12_000]
        except (OSError, subprocess.SubprocessError):
            self._report_once("OCR_UNAVAILABLE", "Windows 本地 OCR 不可用；图片仍会以占位信息加入上下文。")
            return ""
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _report_once(self, code: str, message: str, *, scope: str = "media") -> None:
        with self._lock:
            if code in self._reported_diagnostics:
                return
            self._reported_diagnostics.add(code)
        self.event_sink({"kind": "runtime.error", "scope": scope, "code": code, "message": message, "retryable": True})

    def _resolve_image(self, project: dict, local_id: int) -> tuple[bytes, str, Path] | None:
        root = self._root_for_project(project)
        if root is None:
            return None
        resource = Path("message/message_resource.db")
        if not (root / resource).is_file():
            return None
        file_md5 = ""
        try:
            with self._open_database(root, resource) as conn:
                if not {"ChatName2Id", "MessageResourceInfo"}.issubset(self._tables(conn)):
                    return None
                chat_row = conn.execute(
                    "SELECT rowid FROM ChatName2Id WHERE user_name=? LIMIT 1", (project["chatId"],)
                ).fetchone()
                if not chat_row:
                    return None
                resource_columns = self._columns(conn, "MessageResourceInfo")
                type_filter = " AND message_local_type IN (3,47)" if "message_local_type" in resource_columns else ""
                row = conn.execute(
                    f"SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? AND message_local_id=?{type_filter} LIMIT 1",
                    (int(chat_row[0]), local_id),
                ).fetchone()
                file_md5 = self._extract_md5(row[0] if row else None)
        except (ProxyTalkError, sqlite3.DatabaseError):
            return None
        if not file_md5:
            return None
        chat_hash = hashlib.md5(project["chatId"].encode("utf-8")).hexdigest()
        search_roots = [root.parent / "msg" / "attach" / chat_hash]
        for configured in self.store.get_setting("imageSearchRoots", []) or []:
            try:
                candidate = Path(str(configured)).expanduser().resolve()
                if candidate.is_dir():
                    search_roots.append(candidate / chat_hash if (candidate / chat_hash).is_dir() else candidate)
            except OSError:
                continue
        candidates = []
        for search_root in search_roots:
            if not search_root.is_dir():
                continue
            for path in search_root.rglob(f"{file_md5}*.dat"):
                name = path.stem.lower()
                priority = 0 if name.endswith("_h") else 1 if name == file_md5 else 3 if name.endswith("_t") else 2
                candidates.append((priority, path))
        for _, path in sorted(candidates, key=lambda item: item[0]):
            try:
                if path.stat().st_size > 64 * 1024 * 1024:
                    continue
                decoded = self._decode_image_dat(path.read_bytes())
            except OSError:
                continue
            if decoded:
                return decoded[0], decoded[1], path
        return None

    @staticmethod
    def _extract_md5(blob) -> str:
        if not isinstance(blob, bytes):
            return ""
        marker = b"\x12\x22\x0a\x20"
        position = blob.find(marker)
        candidates = []
        if position >= 0:
            candidates.append(blob[position + len(marker) : position + len(marker) + 32])
        candidates.extend(re.findall(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", blob.lower()))
        for candidate in candidates:
            try:
                value = candidate.decode("ascii")
                int(value, 16)
                return value.lower()
            except (UnicodeDecodeError, ValueError):
                continue
        return ""

    def _decode_image_dat(self, data: bytes) -> tuple[bytes, str] | None:
        if len(data) < 16:
            return None
        if data[:6] in {b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"}:
            if data[:6].startswith(b"\x07\x08V2"):
                pair = self._image_key_pair()
                key = pair.aes_key.encode("ascii", errors="ignore") if pair else b""
                xor_key = pair.xor_key if pair else 0
            else:
                key = b"cfcd208495d565ef"
                xor_key = 0x88
            if len(key) != 16:
                return None
            try:
                aes_size, xor_size = struct.unpack_from("<LL", data, 6)
                if aes_size > len(data) or xor_size > len(data):
                    return None
                aligned = aes_size + (16 - (aes_size % 16))
                offset = 15
                tail_start = len(data) - xor_size
                if aligned <= 0 or offset + aligned > tail_start:
                    return None
                decrypted_head = unpad(AES.new(key, AES.MODE_ECB).decrypt(data[offset : offset + aligned]), 16)
                offset += aligned
                middle = data[offset:tail_start]
                tail = bytes(value ^ xor_key for value in data[tail_start:])
                raw = decrypted_head + middle + tail
            except (ValueError, struct.error):
                return None
        else:
            magics = [b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF"]
            raw = b""
            for magic in magics:
                key = data[0] ^ magic[0]
                if all((data[index] ^ key) == value for index, value in enumerate(magic)):
                    raw = bytes(value ^ key for value in data)
                    break
            if not raw:
                return None
        extension = self._image_extension(raw)
        return (raw, extension) if extension else None

    def _save_image_key_pair(self, pair: ImageKeyPair) -> None:
        self.vault.put_text(
            IMAGE_KEY_VAULT_NAME,
            json.dumps(
                {
                    "aesKey": pair.aes_key,
                    "xorKey": pair.xor_key,
                    "verified": bool(pair.verified),
                },
                separators=(",", ":"),
            ),
        )

    def _image_key_pair(self) -> ImageKeyPair | None:
        raw = self.vault.get_text(IMAGE_KEY_VAULT_NAME)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            aes_key = str(value.get("aesKey") or "")
            xor_key = value.get("xorKey")
            try:
                encoded = aes_key.encode("ascii")
                numeric_xor = int(xor_key)
            except (UnicodeEncodeError, TypeError, ValueError):
                return None
            if len(encoded) == 16 and not isinstance(xor_key, bool) and 0 <= numeric_xor <= 0xFF:
                return ImageKeyPair(
                    aes_key=aes_key,
                    xor_key=numeric_xor,
                    verified=bool(value.get("verified")),
                )
            return None
        try:
            encoded = raw.encode("ascii")
        except UnicodeEncodeError:
            return None
        if len(encoded) == 16:
            # V0.0.2 stored only the AES value and always used WeChat's legacy
            # 0x88 XOR byte. Keep it readable until the automatic scan upgrades it.
            return ImageKeyPair(aes_key=raw, xor_key=0x88, verified=False)
        return None

    @staticmethod
    def _image_extension(raw: bytes) -> str:
        if raw.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "webp"
        return ""

    def _cleanup_stale_snapshots(self) -> None:
        root = self.temporary_root
        if not root.is_dir():
            return
        for child in root.iterdir():
            try:
                resolved = child.resolve()
                if resolved.parent == root and child.name.startswith("snapshot-"):
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            except OSError:
                continue

    def _read_chat_table(
        self,
        conn: sqlite3.Connection,
        project: dict,
        table: str,
        source_db: str,
        cursor: SourceCursor,
        contacts: dict[str, str],
        backlog: bool,
    ) -> tuple[list[SourceMessage], SourceCursor | None]:
        columns = self._columns(conn, table)
        required = {"local_id", "local_type", "create_time", "message_content"}
        if not required.issubset(columns):
            raise ProxyTalkError("WECHAT_SCHEMA_UNSUPPORTED", "当前微信消息表字段不受支持。")
        names = {}
        if "Name2Id" in self._tables(conn):
            for row in conn.execute("SELECT rowid,user_name FROM Name2Id"):
                names[int(row[0])] = str(row[1] or "")
        select = [
            "local_id",
            "server_id" if "server_id" in columns else "0 AS server_id",
            "local_type",
            "sort_seq" if "sort_seq" in columns else "local_id AS sort_seq",
            "real_sender_id" if "real_sender_id" in columns else "0 AS real_sender_id",
            "create_time",
            "message_content",
            "WCDB_CT_message_content" if "WCDB_CT_message_content" in columns else "0 AS WCDB_CT_message_content",
        ]
        overlap_from = max(0, cursor.create_time - 3)
        rows = conn.execute(
            f"SELECT {','.join(select)} FROM {table} WHERE create_time>=? ORDER BY create_time,sort_seq,local_id",
            (overlap_from,),
        ).fetchall()
        messages = []
        newest = (cursor.create_time, cursor.sort_seq, cursor.local_id)
        account_id = self._self_username(project, contacts)
        is_group = project["kind"] == "group"
        if is_group and not account_id:
            self._report_once(
                "WECHAT_SELF_ID_UNRESOLVED",
                "无法可靠识别当前微信账号；群聊消息不会自动触发草稿。",
                scope="wechat",
            )
        for row in rows:
            local_id = int(row["local_id"] or 0)
            server_id = int(row["server_id"] or 0)
            sort_seq = int(row["sort_seq"] or 0)
            created = int(row["create_time"] or 0)
            current = (created, sort_seq, local_id)
            if current > newest:
                newest = current
            raw_sender = names.get(int(row["real_sender_id"] or 0), "")
            content = decode_content(row["message_content"], int(row["WCDB_CT_message_content"] or 0))
            embedded_sender, content = split_group_sender(content, is_group)
            sender_id = embedded_sender or raw_sender
            base_type = int(row["local_type"] or 0) & 0xFFFFFFFF
            if base_type in {10000, 10002}:
                direction = "system"
            elif not is_group and sender_id == project["chatId"]:
                direction = "incoming"
            elif not is_group:
                direction = "outgoing"
            elif account_id and sender_id == account_id:
                direction = "outgoing"
            elif account_id and sender_id:
                direction = "incoming"
            elif sender_id:
                direction = "system"
            else:
                direction = "outgoing"
            kind, text, quote, media, mentioned_me = parse_message(
                int(row["local_type"] or 0), content, account_id
            )
            text = text[:200_000]
            for item in media:
                item.update({"localId": local_id, "sourceDb": source_db})
            source_key = self._source_key(
                project,
                server_id=server_id,
                local_id=local_id,
                sort_seq=sort_seq,
                create_time=created,
                local_type=int(row["local_type"] or 0),
                content=content,
            )
            messages.append(
                SourceMessage(
                    project_id=project["id"], source_key=source_key, source_db=source_db,
                    local_id=local_id, server_id=server_id, sort_seq=sort_seq, create_time=created,
                    sender_id=sender_id, sender_name=contacts.get(sender_id, sender_id or "我"),
                    direction=direction, kind=kind, text=text, quote=quote, media=media,
                    mentioned_me=mentioned_me, backlog=backlog,
                )
            )
        next_cursor = None
        if newest > (cursor.create_time, cursor.sort_seq, cursor.local_id):
            next_cursor = SourceCursor(project["id"], source_db, *newest)
        return messages, next_cursor

    @staticmethod
    def _self_username(project: dict, contacts: dict[str, str]) -> str:
        account = str(project.get("accountId") or "")
        candidates = [account]
        match = re.fullmatch(r"(.+)_([0-9a-fA-F]{4,})", account)
        if match:
            candidates.insert(0, match.group(1))
        resolved = next((candidate for candidate in candidates if candidate in contacts), "")
        if resolved:
            return resolved
        return next((candidate for candidate in candidates if candidate.startswith("wxid_")), "")

    @staticmethod
    def _source_key(
        project: dict,
        *,
        server_id: int,
        local_id: int,
        sort_seq: int,
        create_time: int,
        local_type: int,
        content: str,
    ) -> str:
        if server_id:
            parts = [project["accountId"], project["chatId"], "server", str(server_id)]
        else:
            content_digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
            parts = [
                project["accountId"], project["chatId"], "local", str(local_id),
                str(sort_seq), str(create_time), str(local_type), content_digest,
            ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _contacts(self, root: Path) -> dict[str, str]:
        source = root / "contact/contact.db"
        fingerprint = (str(source.resolve()).casefold(), *self._database_fingerprint(source))
        with self._lock:
            if fingerprint == self._contacts_fingerprint:
                return dict(self._contacts_cache)
        try:
            with self._open_database(root, Path("contact/contact.db")) as conn:
                if "contact" not in self._tables(conn):
                    return {}
                columns = self._columns(conn, "contact")
                if "username" not in columns:
                    return {}
                select = [
                    "username",
                    "remark" if "remark" in columns else "'' AS remark",
                    "nick_name" if "nick_name" in columns else "'' AS nick_name",
                ]
                contacts = {
                    str(row["username"]): str(row["remark"] or row["nick_name"] or row["username"])
                    for row in conn.execute(f"SELECT {','.join(select)} FROM contact")
                    if row["username"]
                }
                with self._lock:
                    self._contacts_fingerprint = fingerprint
                    self._contacts_cache = contacts
                return dict(contacts)
        except ProxyTalkError:
            return {}

    @staticmethod
    def _database_fingerprint(path: Path) -> tuple:
        values = []
        for candidate in (path, Path(f"{path}-wal")):
            try:
                stat = candidate.stat()
                values.append((stat.st_size, stat.st_mtime_ns))
            except OSError:
                values.append(None)
        return tuple(values)

    @contextmanager
    def _open_database(self, root: Path, relative: Path):
        source = (root / relative).resolve()
        try:
            source.relative_to(root.resolve())
        except ValueError as exc:
            raise ProxyTalkError("WECHAT_PATH_INVALID", "微信数据库路径越过了账号目录。") from exc
        key = self._key_for(source)
        work = Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.temporary_root))
        try:
            encrypted = work / "encrypted.db"
            decrypted = work / "decrypted.db"
            copy_stable_database(source, encrypted)
            decrypt_database(encrypted, decrypted, key)
            connection = sqlite3.connect(f"file:{decrypted.as_posix()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                yield connection
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            raise ProxyTalkError("WECHAT_DB_INVALID", "解密后的微信数据库无法读取。") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _key_for(self, source: Path) -> bytes:
        try:
            with source.open("rb") as handle:
                salt = handle.read(16).hex()
        except OSError as exc:
            raise ProxyTalkError("WECHAT_DB_MISSING", f"无法读取微信数据库：{source.name}") from exc
        key = self._keys().get(salt)
        if not key:
            raise ProxyTalkError(
                "WECHAT_KEY_MISSING",
                f"数据库 {source.name} 尚无密钥，请重新执行微信检测。",
                details={"database": source.name},
            )
        try:
            raw = bytes.fromhex(key)
        except ValueError as exc:
            raise ProxyTalkError("WECHAT_KEY_INVALID", "保存的微信数据库密钥格式无效。") from exc
        if len(raw) != 32:
            raise ProxyTalkError("WECHAT_KEY_INVALID", "保存的微信数据库密钥长度无效。")
        return raw

    def _keys(self) -> dict[str, str]:
        raw = self.vault.get_text(KEY_VAULT_NAME)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}

    def _configured_root(self) -> Path:
        value = str(self.store.get_setting("wechatDbDir", "") or "")
        if not value:
            roots = discover_wechat_roots()
            if not roots:
                raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "未检测到个人微信数据目录。")
            value = str(roots[0])
        return self._validate_root(Path(value))

    def _root_for_project(self, project: dict) -> Path | None:
        root = self._configured_root()
        if root.parent.name != str(project.get("accountId") or ""):
            self._report_once(
                "WECHAT_ACCOUNT_MISMATCH_MEDIA",
                "当前微信账号与项目不一致；已跳过本地媒体读取。",
                scope="wechat",
            )
            return None
        return root

    @staticmethod
    def _validate_root(path: Path) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "选择的微信数据库目录不存在。") from exc
        if not (resolved / "session" / "session.db").is_file():
            raise ProxyTalkError("WECHAT_DB_NOT_FOUND", "请选择包含 session/session.db 的 db_storage 目录。")
        return resolved

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        if not table.replace("_", "").isalnum():
            raise ProxyTalkError("WECHAT_SCHEMA_INVALID", "微信表名无效。")
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"
