"""One-shot worker actions for local chat search + context."""

from __future__ import annotations

from . import index as search_index
from .sources import resolve_paths


def _load_config() -> dict:
    from worker.pipeline.config_store import load_config

    try:
        config, _path = load_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # A broken config must not break local search.
        return {}


def _paths():
    return resolve_paths(_load_config())


def handle_search_action(action: str, body: dict | None = None) -> dict:
    """Dispatch a ``search_action`` worker request.

    ``action`` is one of ``status`` / ``rebuild`` / ``query`` / ``context`` and
    ``body`` is the payload object sent by the Electron shell.
    """
    body = body if isinstance(body, dict) else {}
    name = str(action or "").strip().lower()
    paths = _paths()
    try:
        if name == "status":
            return search_index.status(paths)
        if name == "rebuild":
            # The worker is a one-shot process, so the rebuild runs synchronously
            # and the result is visible to the next status call.
            search_index.rebuild(paths)
            return {"started": True}
        if name == "query":
            return search_index.query(paths, body)
        if name == "context":
            return search_index.context(paths, body)
    except ValueError:
        raise
    except Exception as exc:  # SQLite/IO errors become actionable Chinese copy.
        raise ValueError(f"检索命令执行失败：{exc}") from exc
    raise ValueError(f"未知的搜索动作：{name}")
