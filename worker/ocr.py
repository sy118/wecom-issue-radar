from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import urllib.request
from pathlib import Path
from typing import Callable


def run_ocr(
    config: dict,
    image_manifest: Path,
    grouped_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> bool:
    notify = progress or (lambda _message: None)
    if not image_manifest.exists():
        notify("没有发现需要 OCR 的截图")
        return False

    with image_manifest.open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    records = [record for record in manifest.get("records", []) if record.get("accepted")]
    if not records:
        notify("没有发现需要 OCR 的截图")
        return False

    ocr_config = config.get("ocr") or {}
    api_base = (
        ocr_config.get("base_url")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or "https://api.anthropic.com"
    ).rstrip("/")
    api_key = (
        ocr_config.get("api_key")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )
    if not api_key:
        raise ValueError("已启用截图 OCR，但尚未在设置中配置 OCR API Key")
    model = (
        ocr_config.get("model")
        or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        or "claude-sonnet-4-6"
    )
    concurrency = max(1, min(int(ocr_config.get("concurrency") or 4), 12))

    grouped_dir.mkdir(parents=True, exist_ok=True)
    output_path = grouped_dir / "image_ocr.jsonl"
    existing = _read_existing(output_path)
    rows = list(existing.values())
    pending = [record for record in records if record["local_path"] not in existing]
    notify(f"正在识别 {len(pending)} 张新截图（已缓存 {len(existing)} 张）…")

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_ocr_one_image, record, api_base, api_key, model): record
            for record in pending
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            rows.append(future.result())
            if index % 10 == 0 or index == len(pending):
                _write_rows(output_path, rows)
            notify(f"截图 OCR：{index}/{len(pending)}")

    if not pending and not output_path.exists():
        _write_rows(output_path, rows)
    return True


def _ocr_one_image(record: dict, api_base: str, api_key: str, model: str) -> dict:
    local_path = record["local_path"]
    filename = record["filename"]
    try:
        image_bytes = Path(local_path).read_bytes()
        media_type = "image/jpeg" if Path(filename).suffix.lower() in (".jpg", ".jpeg") else "image/png"
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(image_bytes).decode("ascii"),
                                },
                            },
                            {
                                "type": "text",
                                "text": "请提取截图中所有可见文字，原样输出，不要解释；没有文字则输出空字符串。",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        if api_base.endswith("/v1/messages"):
            endpoint = api_base
        elif api_base.endswith("/v1"):
            endpoint = f"{api_base}/messages"
        else:
            endpoint = f"{api_base}/v1/messages"
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        blocks = [block for block in result.get("content", []) if block.get("type") == "text"]
        return _result_row(record, blocks[0].get("text", "") if blocks else "", model)
    except Exception as error:
        row = _result_row(record, "", model)
        row["ocr_error"] = str(error)
        return row


def _result_row(record: dict, text: str, model: str) -> dict:
    return {
        "local_path": record["local_path"],
        "filename": record["filename"],
        "size_bytes": record.get("size_bytes", 0),
        "source_message_id": record.get("source_message_id"),
        "ocr_text": text,
        "ocr_provider": "issue_radar_vision",
        "ocr_model": model,
    }


def _read_existing(path: Path) -> dict[str, dict]:
    existing: dict[str, dict] = {}
    if not path.exists():
        return existing
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                row = json.loads(line)
                existing[row["local_path"]] = row
            except (json.JSONDecodeError, KeyError):
                continue
    return existing


def _write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
