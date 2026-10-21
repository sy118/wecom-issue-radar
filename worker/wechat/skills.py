from __future__ import annotations

import re
from pathlib import Path

from .errors import ProxyTalkError


_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)#?]+)(?:#[^)]*)?\)")
_ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}


class SkillReader:
    """Read-only SKILL.md module. It deliberately has no script execution interface."""

    def __init__(self, *, max_files: int = 24, max_characters: int = 180_000) -> None:
        self.max_files = max_files
        self.max_characters = max_characters

    def inspect(self, path_value: str) -> dict:
        root, skill_file = self._resolve(path_value)
        text = self._read_text(skill_file)
        name = root.name
        description = ""
        if text.startswith("---"):
            frontmatter = text.split("---", 2)[1]
            for line in frontmatter.splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip().strip("'\"")
                if key.strip() == "description" and value.strip():
                    description = value.strip().strip("'\"")
        return {"path": str(root), "name": name, "description": description, "valid": True}

    def load(self, path_values: list[str]) -> str:
        sections: list[str] = []
        used = 0
        for path_value in path_values:
            root, skill_file = self._resolve(path_value)
            queue = [skill_file]
            visited: set[Path] = set()
            files = 0
            skill_parts = []
            while queue and files < self.max_files:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                text = self._read_text(current)
                used += len(text)
                if used > self.max_characters:
                    raise ProxyTalkError("SKILL_TOO_LARGE", "所选 Skill 引用内容超过安全上限。")
                files += 1
                skill_parts.append(f"### {current.relative_to(root).as_posix()}\n{text}")
                for raw_link in _MARKDOWN_LINK.findall(text):
                    candidate = (current.parent / raw_link).resolve()
                    if candidate.suffix.lower() not in _ALLOWED_SUFFIXES or not candidate.is_file():
                        continue
                    try:
                        candidate.relative_to(root)
                    except ValueError:
                        continue
                    queue.append(candidate)
            if queue:
                raise ProxyTalkError(
                    "SKILL_TOO_MANY_FILES",
                    f"Skill 引用文件超过安全上限：{root.name}",
                )
            sections.append(f"## Skill: {root.name}\n" + "\n\n".join(skill_parts))
        return "\n\n".join(sections)

    @staticmethod
    def _resolve(path_value: str) -> tuple[Path, Path]:
        path = Path(path_value).expanduser()
        skill_file = path / "SKILL.md" if path.is_dir() else path
        try:
            skill_file = skill_file.resolve(strict=True)
        except OSError as exc:
            raise ProxyTalkError("SKILL_NOT_FOUND", "找不到所选 Skill 的 SKILL.md。") from exc
        if skill_file.name.lower() != "skill.md" or not skill_file.is_file():
            raise ProxyTalkError("SKILL_INVALID", "Skill 必须指向 SKILL.md 或其所在目录。")
        return skill_file.parent, skill_file

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            if path.stat().st_size > 512_000:
                raise ProxyTalkError("SKILL_FILE_TOO_LARGE", f"Skill 文件过大：{path.name}")
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProxyTalkError("SKILL_ENCODING_INVALID", f"Skill 文件不是 UTF-8：{path.name}") from exc
        except OSError as exc:
            raise ProxyTalkError("SKILL_UNREADABLE", f"无法读取 Skill 文件：{path.name}") from exc
