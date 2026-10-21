from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time

from .errors import ProxyTalkError
from .exporter import ChatExporter
from .mcp_adapter import McpAdapter
from .providers import ModelAdapter
from .skills import SkillReader
from .store import RuntimeStore
from .vault import SecretVault
from .wechat_source import LocalWeChatSource


REWRITE_INSTRUCTIONS = {
    "shorter": "保持原意，改得更短、更直接，适合微信发送。",
    "polite": "保持事实不变，语气更礼貌、体贴，但不要过度客套。",
    "casual": "保持事实不变，改成更自然随意的微信口吻。",
    "regenerate": "重新拟一份自然、有帮助且不过度承诺的回复。",
}


class DraftRuntime:
    """Owns the entire listen → context → tool → draft lifecycle."""

    def __init__(
        self,
        state_root: Path,
        temporary_root: Path,
        *,
        event_sink=None,
        store: RuntimeStore | None = None,
        vault=None,
        source=None,
        model=None,
        mcp=None,
        autostart: bool = True,
    ) -> None:
        state_root.mkdir(parents=True, exist_ok=True)
        self.store = store or RuntimeStore(state_root / "proxytalk.sqlite3")
        self.vault = vault or SecretVault(state_root / "secrets")
        self._event_sink = event_sink or (lambda _seq, _event: None)
        self.source = source or LocalWeChatSource(
            self.store,
            self.vault,
            temporary_root,
            event_sink=self._emit,
        )
        self.model = model or ModelAdapter()
        self.mcp = mcp or McpAdapter()
        self.skills = SkillReader()
        self.exporter = ChatExporter(self.store, self.source)
        self._closed = threading.Event()
        self._enabled = threading.Event()
        if autostart:
            self._enabled.set()
        self._live = False
        self._pending: dict[str, tuple[float, str, bool]] = {}
        self._active: set[str] = set()
        self._guard = threading.RLock()
        self._source_guard = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="proxytalk-draft")
        self._listener = threading.Thread(target=self._listen_loop, name="proxytalk-listener", daemon=True)
        self._listener.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._enabled.set()
        self._listener.join(timeout=4)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.store.close()

    def handle(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ProxyTalkError("INVALID_REQUEST", "请求内容必须是对象。")
        kind = str(body.get("kind") or "")
        if kind == "app.snapshot":
            value = self.store.snapshot()
            value.update({"wechat": self.source.inspect(), "runtime": self._runtime_status()})
            return value
        if kind == "wechat.detect":
            return self.source.inspect()
        if kind == "wechat.select":
            with self._source_guard:
                self._live = False
                result = self.source.select_root(str(body.get("dbDir") or ""))
            self._emit_status()
            return result
        if kind == "wechat.unlock":
            with self._source_guard:
                self._live = False
                result = self.source.unlock(str(body.get("dbDir") or ""))
            self._emit_status()
            return result
        if kind == "wechat.image_key.save":
            return {"wechat": self.source.save_image_key(str(body.get("imageKey") or ""))}
        if kind == "wechat.image_key.scan":
            with self._source_guard:
                return {"wechat": self.source.scan_image_key()}
        if kind == "chat.list":
            with self._source_guard:
                return {"chats": self.source.list_chats()}
        if kind == "settings.save":
            settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
            selected_root = str(self.store.get_setting("wechatDbDir", "") or "")
            if "wechatDbDir" in settings and str(settings["wechatDbDir"] or "") != selected_root:
                raise ProxyTalkError("INVALID_SETTINGS", "请通过微信数据页切换账号目录。")
            result = self.store.save_settings(settings)
            self._emit({"kind": "runtime.status", "scope": "settings", "status": "saved"})
            return {"settings": result}
        if kind == "provider.save":
            return {"provider": self._save_provider(body)}
        if kind == "provider.test":
            provider = self.store.provider(str(body.get("providerId") or ""))
            return self.model.test(provider, api_key=self._provider_key(provider))
        if kind == "mcp.save":
            return {"server": self._save_mcp(body)}
        if kind == "mcp.test":
            return {"server": self._test_mcp(str(body.get("serverId") or ""))}
        if kind == "skill.inspect":
            return {"skill": self.skills.inspect(str(body.get("path") or ""))}
        if kind == "project.save":
            value = body.get("project") if isinstance(body.get("project"), dict) else {}
            project_id = str(value.get("id") or "")
            with self._source_guard:
                with self._guard:
                    reschedule = project_id in self._active or project_id in self._pending
                project = self.store.save_project(value)
                self._context_changed(project, schedule=reschedule)
            self._emit({"kind": "project.updated", "project": project})
            return {"project": project}
        if kind == "project.delete":
            project_id = str(body.get("projectId") or "")
            with self._source_guard:
                with self._guard:
                    if project_id in self._active:
                        raise ProxyTalkError(
                            "PROJECT_BUSY",
                            "草稿仍在生成，请完成后再删除项目。",
                            retryable=True,
                        )
                    self._pending.pop(project_id, None)
                self.store.delete_project(project_id)
            self._emit({"kind": "project.deleted", "projectId": project_id})
            return {"deleted": True}
        if kind == "messages.list":
            return {
                "messages": self.store.messages(
                    str(body.get("projectId") or ""),
                    limit=int(body.get("limit") or 120),
                    before=int(body["before"]) if body.get("before") else None,
                )
            }
        if kind == "media.image":
            project_id = str(body.get("projectId") or "")
            message_id = str(body.get("messageId") or "")
            project = self.store.project(project_id)
            message = self.store.message(project_id, message_id)
            with self._source_guard:
                return self.source.image_preview(project, message)
        if kind == "runtime.start":
            with self._source_guard:
                self._live = False
                self._enabled.set()
            self._emit_status()
            return self._runtime_status()
        if kind == "runtime.stop":
            with self._source_guard:
                self._enabled.clear()
                self._live = False
            self._emit_status()
            return self._runtime_status()
        if kind == "draft.generate":
            return {"draft": self._queue_generation(str(body.get("projectId") or ""), instruction="", immediate=True)}
        if kind == "draft.rewrite":
            style = str(body.get("style") or "regenerate")
            instruction = REWRITE_INSTRUCTIONS.get(style)
            if not instruction:
                raise ProxyTalkError("INVALID_REWRITE", "不支持的草稿改写方式。")
            return {"draft": self._queue_generation(str(body.get("projectId") or ""), instruction=instruction, immediate=True)}
        if kind == "draft.update":
            project_id = str(body.get("projectId") or "")
            draft = self.store.save_draft(project_id, content=str(body.get("content") or ""), status="ready", stale=False, errorCode="")
            self._emit({"kind": "draft.updated", "draft": draft})
            return {"draft": draft}
        if kind == "tool.approve":
            project_id = str(body.get("projectId") or "")
            server_id = str(body.get("serverId") or "")
            tool_name = str(body.get("toolName") or "")
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
            digest = self._arguments_hash(arguments)
            current = self.store.draft(project_id)
            traces = current.get("toolTrace") if current.get("status") == "awaiting_tool_approval" else []
            matching_index = next(
                (
                    index
                    for index, item in enumerate(traces or [])
                    if item.get("serverId") == server_id
                    and item.get("toolName") == tool_name
                    and self._arguments_hash(item.get("arguments") or {}) == digest
                ),
                None,
            )
            if matching_index is None:
                raise ProxyTalkError("TOOL_APPROVAL_STALE", "这次工具确认已经失效，请根据最新上下文重新生成。")
            self.store.approve_tool(project_id, server_id, tool_name, digest)
            remaining = [item for index, item in enumerate(traces) if index != matching_index]
            if remaining:
                draft = self.store.save_draft(project_id, toolTrace=remaining, stale=False)
                self._emit({"kind": "draft.updated", "draft": draft})
                return {"draft": draft}
            return {"draft": self._queue_generation(project_id, instruction="", immediate=True, approval_cycle=True)}
        if kind == "export.run":
            with self._source_guard:
                result = self.exporter.export(
                    str(body.get("projectId") or ""),
                    str(body.get("destination") or ""),
                    start_time=int(body.get("startTime") or 0),
                    end_time=int(body.get("endTime") or 0),
                )
            self._emit({"kind": "export.completed", **result})
            return result
        raise ProxyTalkError("UNKNOWN_COMMAND", f"不支持的运行时命令：{kind}")

    def _save_provider(self, body: dict) -> dict:
        value = body.get("provider") if isinstance(body.get("provider"), dict) else {}
        provider_id = str(value.get("id") or "").strip()
        secret_ref = f"provider:{provider_id}"
        if body.get("clearSecret"):
            self.vault.delete(secret_ref)
        elif isinstance(body.get("apiKey"), str) and body["apiKey"]:
            self.vault.put_text(secret_ref, body["apiKey"])
        persisted_ref = secret_ref if self.vault.has(secret_ref) else ""
        return self.store.save_provider(value, secret_ref=persisted_ref)

    def _save_mcp(self, body: dict) -> dict:
        value = body.get("server") if isinstance(body.get("server"), dict) else {}
        server_id = str(value.get("id") or "").strip()
        secret_ref = f"mcp:{server_id}"
        if body.get("clearSecrets"):
            self.vault.delete(secret_ref)
        elif isinstance(body.get("secrets"), dict):
            secrets = body["secrets"]
            for field in ("env", "headers"):
                if field in secrets and not isinstance(secrets[field], dict):
                    raise ProxyTalkError("INVALID_MCP_SECRET", "MCP 凭据必须是 JSON 对象。")
            self.vault.put_text(secret_ref, json.dumps(secrets, ensure_ascii=False, separators=(",", ":")))
        persisted_ref = secret_ref if self.vault.has(secret_ref) else ""
        return self.store.save_mcp_server(value, secret_ref=persisted_ref)

    def _test_mcp(self, server_id: str) -> dict:
        server = self.store.mcp_server(server_id)
        try:
            catalog = self.mcp.discover(server, self._mcp_secrets(server))
        except ProxyTalkError as exc:
            self.store.update_mcp_catalog(server_id, server.get("catalog") or [], exc.message)
            raise
        self.store.update_mcp_catalog(server_id, catalog, "")
        return self.store.mcp_server(server_id)

    def _listen_loop(self) -> None:
        next_poll = 0.0
        while not self._closed.is_set():
            if not self._enabled.is_set():
                self._closed.wait(0.25)
                continue
            now = time.monotonic()
            if now >= next_poll:
                settings = self.store.settings()
                next_poll = now + max(1.0, min(30.0, float(settings["pollIntervalSeconds"])))
                try:
                    with self._source_guard:
                        projects = self.store.projects(enabled_only=True)
                        messages, cursors = self.source.poll(projects, backlog=not self._live)
                        inserted = self.store.ingest(messages, cursors)
                        by_id = {project["id"]: project for project in projects}
                        context_changes = {}
                        scheduled_projects = set()
                        for message in inserted:
                            self._emit({"kind": "message.appended", "message": message})
                            project = by_id.get(message["projectId"])
                            if project and message.get("direction") != "system":
                                context_changes[project["id"]] = project
                                if self._live and self._should_generate(project, message):
                                    scheduled_projects.add(project["id"])
                        for project_id, project in context_changes.items():
                            self._context_changed(project, schedule=project_id in scheduled_projects)
                        if not self._live and self._enabled.is_set():
                            self._live = True
                            self._emit_status(backlogCount=len(inserted))
                except ProxyTalkError as exc:
                    self._emit({"kind": "runtime.error", "scope": "listener", **exc.as_dict()})
                except Exception:
                    self._emit({"kind": "runtime.error", "scope": "listener", "code": "LISTENER_FAILED", "message": "微信监听发生内部错误。", "retryable": True})
            self._start_due_generations()
            self._closed.wait(0.2)

    def _context_changed(self, project: dict, *, schedule: bool) -> None:
        quiet_ms = int((project.get("trigger") or {}).get("quietWindowMs") or 1800)
        self.store.clear_tool_approvals(project["id"])
        updated_draft = None
        with self._guard:
            if schedule:
                self._pending[project["id"]] = (
                    time.monotonic() + max(0.3, min(10.0, quiet_ms / 1000)),
                    "",
                    False,
                )
            if project["id"] in self._active:
                updated_draft = self.store.save_draft(project["id"], stale=True)
            else:
                current = self.store.draft(project["id"])
                if current.get("status") == "awaiting_tool_approval":
                    updated_draft = self.store.save_draft(
                        project["id"],
                        status="ready" if current.get("content") else "idle",
                        toolTrace=[],
                        stale=True,
                    )
                elif current.get("content") and not current.get("stale"):
                    updated_draft = self.store.save_draft(project["id"], stale=True)
        if updated_draft:
            self._emit({"kind": "draft.updated", "draft": updated_draft})
        self._emit_status()

    def _start_due_generations(self) -> None:
        now = time.monotonic()
        with self._guard:
            ready = [
                (project_id, instruction, approval_cycle)
                for project_id, (due, instruction, approval_cycle) in self._pending.items()
                if due <= now and project_id not in self._active
            ]
            for project_id, instruction, approval_cycle in ready:
                self._pending.pop(project_id, None)
                self._submit_generation(project_id, instruction, approval_cycle)

    def _queue_generation(
        self,
        project_id: str,
        *,
        instruction: str,
        immediate: bool,
        approval_cycle: bool = False,
    ) -> dict:
        self.store.project(project_id)
        if not approval_cycle:
            self.store.clear_tool_approvals(project_id)
        with self._guard:
            if project_id in self._active:
                self._pending[project_id] = (time.monotonic() + (0.0 if immediate else 0.3), instruction, approval_cycle)
                draft = self.store.save_draft(project_id, stale=True)
                self._emit_status()
                return draft
            self._submit_generation(project_id, instruction, approval_cycle)
        return self.store.draft(project_id)

    def _submit_generation(self, project_id: str, instruction: str, approval_cycle: bool) -> None:
        self._active.add(project_id)
        draft = self.store.save_draft(project_id, status="generating", stale=False, errorCode="")
        self._emit({"kind": "draft.updated", "draft": draft})
        self._emit_status()
        future = self._executor.submit(self._generate, project_id, instruction, approval_cycle)

        def completed(_future) -> None:
            with self._guard:
                self._active.discard(project_id)
            self._emit_status()

        future.add_done_callback(completed)

    def _generate(self, project_id: str, instruction: str, approval_cycle: bool) -> None:
        try:
            project = self.store.project(project_id)
            if not project.get("providerId"):
                raise ProxyTalkError("MODEL_NOT_CONFIGURED", "请先为项目选择模型配置。")
            provider = self.store.provider(project["providerId"])
            api_key = self._provider_key(provider)
            messages = self._context_messages(project_id)
            if not messages:
                raise ProxyTalkError("CONTEXT_EMPTY", "这个项目还没有可用于生成回复的聊天记录。")
            basis = [message["id"] for message in messages]
            skill_text = self.skills.load(project.get("skillPaths") or [])
            evidence, pending = self._collect_mcp_evidence(project, provider, api_key, messages)
            if pending:
                draft = self.store.save_draft(
                    project_id,
                    status="awaiting_tool_approval",
                    basedOn=basis,
                    toolTrace=pending,
                    errorCode="",
                )
                self._emit({"kind": "draft.updated", "draft": draft})
                for item in pending:
                    self._emit({"kind": "tool.approval_required", "projectId": project_id, **item})
                return
            with self._source_guard:
                model_messages = self.source.enrich_for_model(
                    project,
                    messages,
                    supports_vision=bool(provider.get("supportsVision")),
                )
            content = self.model.generate(
                provider,
                api_key=api_key,
                project=project,
                messages=model_messages,
                skills=skill_text,
                evidence=evidence,
                instruction=instruction,
            )
            with self._source_guard:
                current_basis = [message["id"] for message in self._context_messages(project_id)]
                current_revision = int(self.store.project(project_id).get("revision") or 0)
                stale = current_basis != basis or current_revision != int(project.get("revision") or 0)
                draft = self.store.save_draft(
                    project_id,
                    content=content,
                    status="ready",
                    basedOn=basis,
                    model=provider["model"],
                    toolTrace=evidence,
                    stale=stale,
                    errorCode="",
                )
            self._emit({"kind": "draft.updated", "draft": draft})
            if stale:
                with self._guard:
                    self._pending[project_id] = (time.monotonic() + 0.4, "", False)
                self._emit_status()
        except ProxyTalkError as exc:
            draft = self.store.save_draft(project_id, status="error", errorCode=exc.code)
            self._emit({"kind": "draft.updated", "draft": draft})
            self._emit({"kind": "runtime.error", "scope": "draft", "projectId": project_id, **exc.as_dict()})
        except Exception:
            draft = self.store.save_draft(project_id, status="error", errorCode="DRAFT_FAILED")
            self._emit({"kind": "draft.updated", "draft": draft})
            self._emit({"kind": "runtime.error", "scope": "draft", "projectId": project_id, "code": "DRAFT_FAILED", "message": "草稿生成发生内部错误。", "retryable": True})
        finally:
            if approval_cycle:
                self.store.clear_tool_approvals(project_id)

    def _collect_mcp_evidence(self, project: dict, provider: dict, api_key: str, messages: list[dict]) -> tuple[list[dict], list[dict]]:
        tools = []
        servers = {}
        for server_id in project.get("mcpServerIds") or []:
            try:
                server = self.store.mcp_server(server_id)
            except ProxyTalkError:
                continue
            if not server.get("enabled"):
                continue
            servers[server_id] = server
            for tool in server.get("catalog") or []:
                tools.append({"serverId": server_id, **tool})
        calls = self.model.plan_tools(provider, api_key=api_key, project=project, messages=messages, tools=tools)
        allowed = {(item["serverId"], item["name"]): item for item in tools}
        evidence, pending = [], []
        for call in calls:
            server_id = str(call.get("serverId") or "")
            tool_name = str(call.get("toolName") or "")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            tool = allowed.get((server_id, tool_name))
            if not tool:
                continue
            digest = self._arguments_hash(arguments)
            if not tool.get("readOnly"):
                if not self.store.tool_is_approved(project["id"], server_id, tool_name, digest):
                    pending.append({"serverId": server_id, "toolName": tool_name, "arguments": arguments, "description": tool.get("description", "")})
                    continue
                if not self.store.consume_tool_approval(project["id"], server_id, tool_name, digest):
                    pending.append({"serverId": server_id, "toolName": tool_name, "arguments": arguments, "description": tool.get("description", "")})
                    continue
            result = self.mcp.call(servers[server_id], self._mcp_secrets(servers[server_id]), tool_name, arguments, timeout=60)
            encoded = json.dumps(result, ensure_ascii=False, default=str)
            evidence.append({"serverId": server_id, "toolName": tool_name, "result": encoded[:30000]})
        return evidence, pending

    def _context_messages(self, project_id: str) -> list[dict]:
        settings = self.store.settings()
        limit = min(500, max(10, int(settings["contextMessageLimit"])))
        character_limit = min(100_000, max(4000, int(settings["contextCharacterLimit"])))
        messages = self.store.messages(project_id, limit=limit)
        selected, used = [], 0
        for message in reversed(messages):
            size = len(str(message.get("text") or ""))
            if selected and used + size > character_limit:
                break
            selected.append(message)
            used += size
        return list(reversed(selected))

    @staticmethod
    def _should_generate(project: dict, message: dict) -> bool:
        if message.get("direction") != "incoming" or message.get("backlog"):
            return False
        trigger = project.get("trigger") or {}
        mode = trigger.get("mode") or ("auto" if project["kind"] == "direct" else "manual")
        if project["kind"] == "direct":
            return mode == "auto"
        if mode == "manual":
            return False
        if mode == "auto":
            return True
        text = str(message.get("text") or "").casefold()
        keywords = [str(item).casefold() for item in trigger.get("keywords") or [] if str(item).strip()]
        senders = {str(item) for item in trigger.get("senderIds") or []}
        return (
            any(keyword in text for keyword in keywords)
            or message.get("senderId") in senders
            or bool(trigger.get("mentionMe") and message.get("mentionedMe"))
        )

    def _provider_key(self, provider: dict) -> str:
        secret_ref = str(provider.get("secretRef") or "")
        return self.vault.get_text(secret_ref) if secret_ref else ""

    def _mcp_secrets(self, server: dict) -> dict:
        secret_ref = str(server.get("secretRef") or "")
        raw = self.vault.get_text(secret_ref) if secret_ref else ""
        try:
            value = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _arguments_hash(arguments: dict) -> str:
        return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _runtime_status(self) -> dict:
        with self._guard:
            return {
                "running": self._enabled.is_set(),
                "live": self._enabled.is_set() and self._live,
                "activeGenerations": len(self._active),
                "queuedGenerations": len(self._pending),
            }

    def _emit_status(self, **details) -> None:
        state = self._runtime_status()
        status = "stopped" if not state["running"] else "live" if state["live"] else "running"
        self._emit({"kind": "runtime.status", "scope": "listener", "status": status, **state, **details})

    def _emit(self, event: dict) -> None:
        seq = self.store.next_event_seq()
        self._event_sink(seq, event)
