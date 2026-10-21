"""Process-wide personal WeChat backend runtime.

This module owns the single ``WeChatRuntime`` instance used by
``worker.wechat.actions``.  It reuses the durable ProxyTalk draft runtime
(inbox/cursor/draft/tool-approval state) but drives polling from the desktop
timer instead of an always-on listener thread, so the sidecar never talks to
the WeChat databases unless the UI asks for it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .errors import ProxyTalkError
from .runtime import REWRITE_INSTRUCTIONS, DraftRuntime
from .store import RuntimeStore
from .vault import MemoryVault, SecretVault
from .wechat_source import LocalWeChatSource

DEFAULT_PROVIDER_ID = "llm-default"
DEFAULT_PROVIDER_NAME = "默认模型（全局设置）"
WECHAT_DB_FILENAME = "wechat.sqlite3"
VAULT_MEMORY_NOTICE = "本机 DPAPI 凭据存储不可用：API Key 仅保存在内存中，重启应用后需要重新填写。"

_RUNTIME_LOCK = threading.RLock()
_RUNTIME: "WeChatRuntime | None" = None
_TEST_OVERRIDES: dict = {}


# ---------------------------------------------------------------------------
# paths / config


def data_root() -> Path:
    override = (
        os.environ.get("WECOM_ISSUE_RADAR_DATA_DIR")
        or os.environ.get("WECOM_ISSUE_RADAR_HOME")
        or ""
    ).strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".wecom-issue-radar").resolve()


def temporary_root() -> Path:
    override = (os.environ.get("WECOM_ISSUE_RADAR_WECHAT_TEMP") or "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        local = (os.environ.get("LOCALAPPDATA") or "").strip()
        path = (Path(local) / "WeComIssueRadar" / "wechat-temp") if local else (data_root() / "temp")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def config_path() -> Path:
    from worker.pipeline.config_store import default_config_path

    return default_config_path()


def load_llm_config() -> dict:
    """Return the global ``llm`` partition, or ``{}`` when it cannot be read."""

    try:
        from worker.pipeline.config_store import load_config

        config, _ = load_config(config_path())
        llm = config.get("llm")
        return dict(llm) if isinstance(llm, dict) else {}
    except Exception:
        return {}


def llm_provider_kind(llm: dict) -> str:
    return "anthropic" if "anthropic" in str(llm.get("provider") or "").lower() or "claude" in str(llm.get("provider") or "").lower() else "openai_compatible"


def describe_llm_config(llm: dict | None = None) -> dict:
    llm = load_llm_config() if llm is None else dict(llm)
    return {
        "provider": llm_provider_kind(llm),
        "baseUrl": str(llm.get("base_url") or "").strip(),
        "model": str(llm.get("model") or "").strip(),
        "configured": bool(str(llm.get("base_url") or "").strip() and str(llm.get("model") or "").strip()),
        "hasApiKey": bool(str(llm.get("api_key") or "").strip()),
    }


def build_vault(root: Path) -> tuple[object, str]:
    if (os.environ.get("WECOM_ISSUE_RADAR_WECHAT_VAULT") or "").strip().lower() == "memory":
        return MemoryVault(), "memory"
    try:
        vault = SecretVault(root / "secrets")
        vault.put_text("vault:probe", "ok")
        usable = vault.get_text("vault:probe") == "ok"
        vault.delete("vault:probe")
        if usable:
            return vault, "dpapi"
    except Exception:
        pass
    return MemoryVault(), "memory"


# ---------------------------------------------------------------------------
# runtime


class WeChatRuntime(DraftRuntime):
    """ProxyTalk draft runtime with config.llm provider fallback and timer polling."""

    def __init__(
        self,
        *,
        state_root: Path,
        temp_root: Path,
        store: RuntimeStore | None = None,
        vault=None,
        vault_mode: str = "dpapi",
        source=None,
        model=None,
        config_loader=None,
        autostart: bool = False,
    ) -> None:
        self.state_root = Path(state_root)
        self.temporary_root = Path(temp_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.vault_mode = vault_mode
        self._config_loader = config_loader or load_llm_config
        store = store or RuntimeStore(self.state_root / WECHAT_DB_FILENAME)
        vault = vault if vault is not None else MemoryVault()
        super().__init__(
            self.state_root,
            self.temporary_root,
            store=store,
            vault=vault,
            source=source,
            model=model,
            autostart=autostart,
        )
        self._poll_initialized = False
        self.default_provider = self.ensure_default_provider()

    # -- providers ---------------------------------------------------------

    def ensure_default_provider(self) -> dict:
        """Create one ``providers`` row from ``config.llm`` when the table is empty."""

        result = self._ensure_default_provider()
        self.default_provider = result
        return result

    def _ensure_default_provider(self) -> dict:
        providers = self.store.providers()
        if providers:
            return {"id": providers[0]["id"], "source": "store", "notice": ""}
        llm = self._config_loader()
        base_url = str(llm.get("base_url") or "").strip()
        model = str(llm.get("model") or "").strip()
        if not (base_url and model):
            return {"id": "", "source": "unconfigured", "notice": "全局设置里还没有模型连接，请先在设置中填写 Base URL 和模型名称。"}
        api_key = str(llm.get("api_key") or "")
        secret_ref = ""
        if api_key:
            try:
                self.vault.put_text(f"provider:{DEFAULT_PROVIDER_ID}", api_key)
                if self.vault.has(f"provider:{DEFAULT_PROVIDER_ID}"):
                    secret_ref = f"provider:{DEFAULT_PROVIDER_ID}"
            except Exception:
                secret_ref = ""
        try:
            provider = self.store.save_provider(
                {
                    "id": DEFAULT_PROVIDER_ID,
                    "name": DEFAULT_PROVIDER_NAME,
                    "kind": llm_provider_kind(llm),
                    "baseUrl": base_url,
                    "model": model,
                    "temperature": float(llm.get("temperature") or 0.3),
                    "maxOutputTokens": int(llm.get("max_output_tokens") or 1200),
                    "supportsVision": True,
                },
                secret_ref=secret_ref,
            )
        except ProxyTalkError as exc:
            return {"id": "", "source": "invalid", "notice": f"全局模型连接不可用：{exc.message}"}
        return {"id": provider["id"], "source": "config.llm", "notice": ""}

    def provider_key(self, provider: dict) -> str:
        """Public helper used by tests and diagnostics."""

        try:
            return self._provider_key(provider)
        except ProxyTalkError:
            return ""

    def _provider_key(self, provider: dict) -> str:  # runtime hook
        """Resolve an API key from the vault, falling back to ``config.llm``."""

        secret_ref = str(provider.get("secretRef") or "")
        key = ""
        if secret_ref:
            try:
                key = self.vault.get_text(secret_ref)
            except ProxyTalkError:
                key = ""
        if key:
            return key
        if str(provider.get("id") or "") == DEFAULT_PROVIDER_ID:
            return str(self._config_loader().get("api_key") or "")
        return ""

    # -- generation --------------------------------------------------------

    def generate_sync(self, project_id: str, *, instruction: str = "", timeout: float = 180.0) -> dict:
        """Queue one draft generation and wait (bounded) for it to settle."""

        draft = self._queue_generation(project_id, instruction=instruction, immediate=True)
        if draft.get("status") == "error":
            return draft
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            with self._guard:
                active = project_id in self._active
                pending = project_id in self._pending
            if not active and not pending:
                break
            time.sleep(0.05)
        return self.store.draft(project_id)

    # -- polling -----------------------------------------------------------

    def poll_once(self) -> dict:
        projects = self.store.projects(enabled_only=True)
        if not projects:
            return {"projects": [], "updated": False, "inserted": 0}
        backlog = not self._poll_initialized
        try:
            with self._source_guard:
                messages, cursors = self.source.poll(projects, backlog=backlog)
                inserted = self.store.ingest(messages, cursors)
        except ProxyTalkError as exc:
            self._poll_initialized = True
            return {"projects": [], "updated": False, "inserted": 0, "error": exc.as_dict()}
        self._poll_initialized = True
        by_id = {project["id"]: project for project in projects}
        per_project: dict[str, list[dict]] = {}
        for message in inserted:
            project_id = str(message.get("projectId") or "")
            if not project_id:
                continue
            per_project.setdefault(project_id, []).append(message)
        changed: list[dict] = []
        for project_id, messages in per_project.items():
            project = by_id.get(project_id)
            if project is None:
                continue
            entry = {
                "projectId": project_id,
                "newMessages": messages,
                "newMessageCount": len(messages),
                "unreadDelta": sum(1 for item in messages if item.get("direction") == "incoming"),
            }
            if any(item.get("direction") != "system" for item in messages):
                self._context_changed(project, schedule=False)
                entry["draft"] = self.store.draft(project_id)
            changed.append(entry)
        return {"projects": changed, "updated": bool(inserted), "inserted": len(inserted)}

    # -- diagnostics -------------------------------------------------------

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.vault_mode == "memory":
            warnings.append(VAULT_MEMORY_NOTICE)
        notice = str((self.default_provider or {}).get("notice") or "")
        if notice:
            warnings.append(notice)
        return warnings


# ---------------------------------------------------------------------------
# singleton lifecycle


def _build_runtime() -> WeChatRuntime:
    root = data_root()
    temp = temporary_root()
    root.mkdir(parents=True, exist_ok=True)
    store = RuntimeStore(root / WECHAT_DB_FILENAME)
    vault = _TEST_OVERRIDES.get("vault")
    if vault is not None:
        vault_mode = "memory"
    else:
        vault, vault_mode = build_vault(root)
    source = _TEST_OVERRIDES.get("source")
    if source is None:
        source = LocalWeChatSource(store, vault, temp)
    return WeChatRuntime(
        state_root=root,
        temp_root=temp,
        store=store,
        vault=vault,
        vault_mode=vault_mode,
        source=source,
        model=_TEST_OVERRIDES.get("model"),
        autostart=False,
    )


def get_runtime() -> WeChatRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = _build_runtime()
        return _RUNTIME


def reset_runtime_for_tests() -> None:
    """Close and forget the singleton; only used by tests and shutdown."""

    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
    if runtime is not None:
        runtime.close()


def configure_for_tests(
    *,
    data_root_path: Path | None = None,
    config_path_value: Path | None = None,
    source=None,
    model=None,
    vault=None,
) -> WeChatRuntime:
    """Install a deterministic runtime for tests (fake source/model, memory vault)."""

    reset_runtime_for_tests()
    if data_root_path is not None:
        os.environ["WECOM_ISSUE_RADAR_DATA_DIR"] = str(data_root_path)
    if config_path_value is not None:
        os.environ["WECOM_ISSUE_RADAR_CONFIG"] = str(config_path_value)
    _TEST_OVERRIDES.clear()
    _TEST_OVERRIDES.update({"source": source, "model": model, "vault": vault or MemoryVault()})
    return get_runtime()


def shutdown() -> None:
    reset_runtime_for_tests()


__all__ = [
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_PROVIDER_NAME",
    "REWRITE_INSTRUCTIONS",
    "WeChatRuntime",
    "config_path",
    "configure_for_tests",
    "data_root",
    "describe_llm_config",
    "get_runtime",
    "load_llm_config",
    "reset_runtime_for_tests",
    "shutdown",
    "temporary_root",
]
