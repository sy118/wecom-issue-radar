"""Read-only discovery of local WeChat (personal) database directories.

The scanner only looks at a bounded set of well-known parent directories and
never walks an entire drive.  A directory is considered usable only when it
contains ``session/session.db``; everything else is reported as a candidate
with a human-readable reason so the UI can explain why detection failed.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

MAX_CANDIDATES = 12
MAX_CHILDREN = 80
MAX_CHECKED = 24
_DRIVE_LETTERS = ("C", "D", "E", "F", "G", "H")
_DAY = 86_400.0


def _parents() -> list[tuple[Path, str]]:
    """Well-known parent directories that may contain one account per child."""

    home = Path.home()
    documents = home / "Documents"
    entries: list[tuple[Path, str]] = [
        (documents / "xwechat_files", "微信 4.x 默认目录"),
        (documents / "WeChat Files", "微信 3.x 默认目录"),
        (home / "OneDrive" / "Documents" / "xwechat_files", "OneDrive 文档目录"),
        (home / "OneDrive" / "Documents" / "WeChat Files", "OneDrive 微信 3.x 目录"),
    ]
    try:
        for redirected in home.glob("OneDrive*/Documents"):
            entries.append((redirected / "xwechat_files", "OneDrive 文档目录"))
            entries.append((redirected / "WeChat Files", "OneDrive 微信 3.x 目录"))
    except OSError:
        pass
    if sys.platform == "win32":
        for letter in _DRIVE_LETTERS:
            root = Path(f"{letter}:\\")
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
            entries.append((root / "xwechat_files", "磁盘根目录"))
            entries.append((root / "WeChat Files", "磁盘根目录"))
            entries.append((root / "Documents" / "xwechat_files", "磁盘文档目录"))
    return _dedupe_parents(entries)


def _dedupe_parents(entries: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[str] = set()
    result: list[tuple[Path, str]] = []
    for path, label in entries:
        try:
            key = str(path.resolve(strict=False)).casefold()
        except (OSError, RuntimeError):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append((path, label))
    return result


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _shards(message_dir: Path) -> list[Path]:
    if not message_dir.is_dir():
        return []
    try:
        return sorted(
            path
            for path in message_dir.glob("message_*.db")
            if not path.name.endswith(("_fts.db", "_resource.db"))
        )
    except OSError:
        return []


def evaluate(db_dir: Path, source_label: str = "", *, configured: bool = False) -> dict:
    """Score one ``db_storage`` directory and explain the evidence."""

    db_dir = Path(db_dir)
    score = 0
    evidence: list[str] = []
    problems: list[str] = []
    has_session = (db_dir / "session" / "session.db").is_file()
    if has_session:
        score += 60
        evidence.append("session/session.db")
    else:
        problems.append("缺少 session/session.db")
    message_dir = db_dir / "message"
    shards = _shards(message_dir)
    if shards:
        score += 20
        evidence.append(f"{len(shards)} 个消息分片")
    elif message_dir.is_dir():
        problems.append("消息目录为空（可能还没同步过聊天）")
    if (db_dir / "contact" / "contact.db").is_file():
        score += 10
        evidence.append("contact.db")
    try:
        if message_dir.is_dir() and next(message_dir.glob("media_*.db"), None) is not None:
            score += 5
            evidence.append("媒体索引")
    except OSError:
        pass
    lowered = str(db_dir).casefold()
    if "xwechat_files" in lowered:
        score += 4
        evidence.append("微信 4.x 路径")
    elif "wechat files" in lowered:
        problems.append("这是微信 3.x 目录，当前版本只支持微信 4.x")
    stamp = _mtime(db_dir)
    if stamp:
        age = max(0.0, time.time() - stamp)
        if age < 3 * _DAY:
            score += 5
        elif age < 30 * _DAY:
            score += 3
        elif age < 180 * _DAY:
            score += 1
    if configured:
        score += 8
        evidence.append("当前已选择")
    if has_session:
        reason = "；".join(evidence) if evidence else "已通过 session.db 校验"
    else:
        reason = "；".join(problems + evidence)
    return {
        "path": str(db_dir),
        "accountId": db_dir.parent.name,
        "root": str(db_dir.parent),
        "reason": reason,
        "score": int(score),
        "valid": bool(has_session),
        "hasSession": bool(has_session),
        "shardCount": len(shards),
        "modifiedAt": int(stamp),
        "source": source_label,
        "selected": False,
    }


def _candidate_db_dir(child: Path) -> Path | None:
    if child.name == "db_storage":
        return child
    nested = child / "db_storage"
    if nested.is_dir():
        return nested
    if (child / "session" / "session.db").is_file():
        return child
    return None


def scan(
    *,
    configured: str | None = None,
    roots: list[tuple[Path, str]] | None = None,
) -> dict:
    """Return scored candidates, checked parents and a failure explanation."""

    checked: list[str] = []
    candidates: list[dict] = []
    seen: set[str] = set()
    configured_path = ""
    if configured:
        try:
            configured_path = str(Path(configured).expanduser().resolve(strict=False)).casefold()
        except (OSError, RuntimeError):
            configured_path = ""
    for parent, label in (roots if roots is not None else _parents()):
        if len(candidates) >= MAX_CANDIDATES * 2:
            break
        try:
            if not parent.is_dir():
                continue
        except OSError:
            continue
        if len(checked) < MAX_CHECKED:
            checked.append(str(parent))
        direct = _candidate_db_dir(parent)
        if direct is not None:
            _add_candidate(candidates, seen, evaluate(direct, label, configured=_same_path(direct, configured_path)), configured_path)
        try:
            children = sorted(parent.iterdir())[:MAX_CHILDREN]
        except OSError:
            continue
        for child in children:
            if len(candidates) >= MAX_CANDIDATES * 2:
                break
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            db_dir = _candidate_db_dir(child)
            if db_dir is None:
                continue
            if db_dir.name != "db_storage" and not (db_dir / "session" / "session.db").is_file():
                continue
            _add_candidate(candidates, seen, evaluate(db_dir, label, configured=_same_path(db_dir, configured_path)), configured_path)
    if configured and configured_path and not any(_same_path(Path(item["path"]), configured_path) for item in candidates):
        try:
            configured_dir = Path(configured).expanduser()
            if configured_dir.is_dir():
                _add_candidate(
                    candidates,
                    seen,
                    evaluate(configured_dir, "当前设置", configured=True),
                    configured_path,
                )
        except OSError:
            pass
    candidates.sort(key=lambda item: (not item["valid"], -item["score"], -item["modifiedAt"]))
    valid = [item for item in candidates if item["valid"]]
    default = configured if (configured and configured_path and any(_same_path(Path(item["path"]), configured_path) and item["valid"] for item in candidates)) else (valid[0]["path"] if valid else "")
    reason = ""
    if not valid:
        if candidates:
            reason = "找到疑似微信目录但都不包含有效的 session.db：" + "、".join(
                f"{item['path']}（{item['reason']}）" for item in candidates[:3]
            )
        elif checked:
            reason = (
                "未找到 db_storage 目录。请确认已登录微信 4.x 并至少打开过一次聊天；"
                "已检查：" + "、".join(checked[:6])
            )
        else:
            reason = "未找到常见的微信文档目录；请确认微信已安装并登录，或手动选择 db_storage 目录。"
    return {
        "candidates": candidates[:MAX_CANDIDATES],
        "checked": checked,
        "defaultDbDir": default,
        "reason": reason,
        "scannedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _add_candidate(candidates: list[dict], seen: set[str], candidate: dict, configured_path: str) -> None:
    try:
        key = str(Path(candidate["path"]).resolve(strict=False)).casefold()
    except (OSError, RuntimeError):
        key = candidate["path"].casefold()
    if key in seen:
        return
    seen.add(key)
    candidate["selected"] = bool(configured_path) and key == configured_path
    candidates.append(candidate)


def _same_path(path: Path, expected: str) -> bool:
    if not expected:
        return False
    try:
        return str(path.expanduser().resolve(strict=False)).casefold() == expected
    except (OSError, RuntimeError):
        return False


def top_valid(candidates: list[dict] | None) -> dict | None:
    for item in candidates or []:
        if item.get("valid"):
            return item
    return None
