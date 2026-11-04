"""Resolve WeCom image cache paths for the reply assistant's vision call.

The page no longer OCRs images while loading context; when the user generates a
reply it sends the image message ids and this module turns them into trusted
``Cache/Image`` paths (the same trust rules as the export pipeline). The model
layer then attaches the images to the chat completion request.
"""

from __future__ import annotations

import time
from pathlib import Path

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024


def resolve_message_images(
    config: dict,
    conversation_id: str,
    message_ids: list[str],
) -> list[Path]:
    """Return up to three readable image files for the requested message ids."""

    return resolve_message_images_with_meta(
        config, conversation_id, message_ids
    )["paths"]


def resolve_message_images_with_meta(
    config: dict,
    conversation_id: str,
    message_ids: list[str],
) -> dict:
    """Resolve images plus how many usable candidates were skipped by the limits.

    ``paths`` is capped at ``MAX_IMAGES`` with a cumulative ``MAX_TOTAL_IMAGE_BYTES``
    budget; ``skipped`` counts readable images dropped by the per-image size,
    count or total-byte limits (missing/unreadable files are not counted).
    """

    conversation_id = str(conversation_id or "").strip()
    wanted = {str(value).strip() for value in (message_ids or []) if str(value).strip()}
    if not conversation_id or not wanted:
        return {"paths": [], "skipped": 0}

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
        return {"paths": [], "skipped": 0}

    rows = sorted(
        rows,
        key=lambda item: (
            int(item.get("send_time") or 0),
            int(item.get("sequence") or 0),
            int(item.get("message_id") or 0),
        ),
    )
    images: list[Path] = []
    skipped = 0
    total_bytes = 0
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
                seen.add(identity)
                size = int(descriptor.size or 0)
                if size and size > MAX_IMAGE_BYTES:
                    skipped += 1
                    continue
                if len(images) >= MAX_IMAGES:
                    skipped += 1
                    continue
                if total_bytes + size > MAX_TOTAL_IMAGE_BYTES:
                    skipped += 1
                    continue
                total_bytes += size
                images.append(path)
    return {"paths": images, "skipped": skipped}


# Identity marker: actions can tell the real resolver apart from a test double
# patched over ``resolve_message_images`` and keep the list contract compatible.
_ORIGINAL_RESOLVE_MESSAGE_IMAGES = resolve_message_images
