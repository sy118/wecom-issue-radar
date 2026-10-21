from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import shutil

from .errors import ProxyTalkError


class ChatExporter:
    def __init__(self, store, source) -> None:
        self.store = store
        self.source = source

    def export(self, project_id: str, destination: str, *, start_time: int = 0, end_time: int = 0) -> dict:
        project = self.store.project(project_id)
        if start_time and end_time and start_time > end_time:
            raise ProxyTalkError("EXPORT_RANGE_INVALID", "导出开始时间不能晚于结束时间。")
        base = Path(destination).expanduser()
        try:
            base = base.resolve(strict=True)
        except OSError as exc:
            raise ProxyTalkError("EXPORT_DESTINATION_INVALID", "导出目标目录不存在。") from exc
        if not base.is_dir():
            raise ProxyTalkError("EXPORT_DESTINATION_INVALID", "导出目标必须是目录。")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._unique_directory(base / f"ProxyTalk-{self._safe_name(project['name'])}-{stamp}")
        messages = self.store.export_messages(project_id, start_time=start_time, end_time=end_time)
        media_dir = target / "media"
        created = False
        try:
            target.mkdir(parents=False)
            created = True
            media_dir.mkdir()
            exported_media = []
            exported_messages = []
            for message in messages:
                media = self.source.materialize_media(project, message, media_dir)
                exported_messages.append({**message, "media": media})
                exported_media.extend({"messageId": message["id"], **item} for item in media)
            envelope = {
                "schemaVersion": 1,
                "exportedAt": datetime.now().astimezone().isoformat(),
                "project": {
                    "id": project["id"],
                    "name": project["name"],
                    "accountId": project["accountId"],
                    "chatId": project["chatId"],
                    "chatTitle": project["chatTitle"],
                    "kind": project["kind"],
                    "summary": project["summary"],
                },
                "range": {"startTime": start_time or None, "endTime": end_time or None},
                "messages": exported_messages,
                "media": exported_media,
            }
            (target / "chat.json").write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (target / "chat.md").write_text(self._markdown(project, exported_messages), encoding="utf-8")
        except Exception:
            if created:
                shutil.rmtree(target, ignore_errors=True)
            raise
        return {
            "path": str(target),
            "messageCount": len(messages),
            "mediaCount": sum(1 for item in exported_media if item.get("available")),
        }

    @staticmethod
    def _markdown(project: dict, messages: list[dict]) -> str:
        lines = [
            f"# {project['name']}",
            "",
            f"- 微信聊天：`{project['chatId']}`",
            f"- 类型：{'群聊' if project['kind'] == 'group' else '单聊'}",
            f"- 导出时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
            "",
        ]
        current_day = ""
        for message in messages:
            moment = datetime.fromtimestamp(int(message["createTime"])).astimezone()
            day = moment.strftime("%Y-%m-%d")
            if day != current_day:
                lines.extend([f"## {day}", ""])
                current_day = day
            sender = "我" if message["direction"] == "outgoing" else (message["senderName"] or "未知发送者")
            text = str(message.get("text") or "").replace("\r\n", "\n")
            lines.extend([f"**{moment.strftime('%H:%M:%S')} · {sender}**", "", text or f"[{message['kind']}]", ""])
            quote = message.get("quote")
            if quote:
                quoted = str(quote.get("text") or "").replace("\n", " ")
                lines.extend([f"> 引用 {quote.get('sender') or ''}：{quoted}", ""])
            for media in message.get("media") or []:
                if media.get("available") and media.get("path"):
                    lines.extend([f"- 媒体：[{media.get('sourceName') or media.get('kind') or '文件'}]({media['path']})", ""])
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _safe_name(value: str) -> str:
        name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .")
        return name[:80] or "chat"

    @staticmethod
    def _unique_directory(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.name}-{index}")
            if not candidate.exists():
                return candidate
        raise ProxyTalkError("EXPORT_DESTINATION_BUSY", "无法为导出创建唯一目录。")
