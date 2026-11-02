"""Resolve WeCom image cache paths for the reply assistant's vision call.

The page no longer OCRs images while loading context; when the user generates a
reply it sends the image message ids and this module turns them into trusted
``Cache/Image`` paths (the same trust rules as the export pipeline). The model
layer then attaches the images to the chat completion request.
"""

from __future__ import annotations

import time
from pathlib import Path

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def resolve_message_images(
    config: dict,
    conversation_id: str,
    message_ids: list[str],
) -> list[Path]:
    """Return up to six readable image files for the requested message ids."""

    conversation_id = str(conversation_id or "").strip()
    wanted = {str(value).strip() for value in (message_ids or []) if str(value).strip()}
    if not conversation_id or not wanted:
        return []

    from worker.wecom.local_db import read_messages
    from worker.wecom.media import extract_local_image_paths, inspect_image_file

    now = int(time.time())
    try:
        rows = read_messages(
            config,
            conversation_id,
            now - 30 * 86400,
            now + 2,
            limit=200,
        )
    except Exception:  # Missing keys / locked DB: reply without images instead.
        return []

    rows = sorted(
        rows,
        key=lambda item: (
            int(item.get("send_time") or 0),
            int(item.get("sequence") or 0),
            int(item.get("message_id") or 0),
        ),
    )
    images: list[Path] = []
    seen: set[str] = set()
    for raw in rows:
        message_id = str(raw.get("message_id") or "")
        if message_id not in wanted:
            continue
        for field in ("local_extra_content_raw", "extra_content_raw", "content_raw"):
            for path in extract_local_image_paths(config, raw.get(field)):
                identity = str(path)
                if identity in seen:
                    continue
                descriptor = inspect_image_file(path)
                if not descriptor.available or not descriptor.mime.startswith("image/"):
                    continue
                if descriptor.size and descriptor.size > MAX_IMAGE_BYTES:
                    continue
                seen.add(identity)
                images.append(path)
                if len(images) >= MAX_IMAGES:
                    return images
    return images
