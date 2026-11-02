"""Read-only WeCom chat context + on-demand OCR for the high-EQ reply page.

The reply runtime already owns a decrypted message snapshot and sender
identity caches, so the page reads recent context through the runtime instead
of spawning a worker process every poll. OCR is bounded and cached: one round
handles at most ``MAX_IMAGES_PER_ROUND`` images within ``TOTAL_BUDGET_SECONDS``
so a 30-second poll never repeatedly bills the same screenshots.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from worker.ocr import (
    engine_fingerprint,
    guess_media_type,
    ocr_image_bytes,
    resolve_ocr_engine,
)

MAX_IMAGES_PER_ROUND = 6
TOTAL_BUDGET_SECONDS = 45.0
PER_IMAGE_TIMEOUT_SECONDS = 60.0
MAX_WORKERS = 2
CACHE_VERSION = 1
CACHE_DIR_NAME = "chat-ocr"

_ENGINE_UNAVAILABLE = "未配置 OCR 模型"
_IMAGE_UNAVAILABLE = "图片不可用"
_OVER_BUDGET = "图片 OCR 超时"
_OVER_LIMIT = "超过单轮识别上限"


class ChatOcrService:
    """OCR the images of one conversation round, with a persistent disk cache."""

    def __init__(self, cache_root: Path | None = None) -> None:
        self.cache_root = Path(cache_root) if cache_root else None
        self._lock = threading.Lock()

    # -- public ---------------------------------------------------------

    def ocr_messages(
        self,
        config: dict,
        images_by_message: dict[str, list[dict]],
        *,
        max_images: int = MAX_IMAGES_PER_ROUND,
        budget_seconds: float = TOTAL_BUDGET_SECONDS,
    ) -> dict:
        engine = resolve_ocr_engine(config)
        engine_meta = {
            "available": engine is not None,
            "source": str(engine.get("source") or "") if engine else "",
            "model": str(engine.get("model") or "") if engine else "",
        }
        results = []
        requests = []
        for message_id, images in images_by_message.items():
            for image in images:
                base = {
                    "messageId": str(message_id),
                    "index": int(image.get("index") or 0),
                    "model": engine_meta["model"],
                    "source": engine_meta["source"],
                    "text": "",
                    "error": "",
                }
                local_path = str(image.get("localPath") or "")
                error_code = str(image.get("errorCode") or "")
                if error_code or not local_path:
                    base["error"] = _IMAGE_UNAVAILABLE
                    results.append(base)
                elif engine is None:
                    base["error"] = _ENGINE_UNAVAILABLE
                    results.append(base)
                else:
                    requests.append((base, local_path, str(image.get("mimeType") or "")))

        if requests and engine is not None:
            capped = requests[: max(1, int(max_images))]
            for skipped, _path, _mime in requests[len(capped):]:
                skipped["error"] = _OVER_LIMIT
                results.append(skipped)
            results.extend(
                self._run_batch(
                    capped,
                    engine,
                    engine_meta,
                    budget_seconds=max(5.0, float(budget_seconds)),
                )
            )

        results.sort(key=lambda item: (item["messageId"], item["index"]))
        return {"results": results, "engine": engine_meta}

    # -- internals ------------------------------------------------------

    def _run_batch(
        self,
        requests: list[tuple[dict, str, str]],
        engine: dict,
        engine_meta: dict,
        *,
        budget_seconds: float,
    ) -> list[dict]:
        deadline = time.monotonic() + budget_seconds
        fingerprint = engine_fingerprint(engine)
        completed: dict[int, dict] = {}
        pending: dict[concurrent.futures.Future, tuple[int, dict, str, str]] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for position, (base, local_path, mime_type) in enumerate(requests):
                image_bytes = self._read_image(local_path)
                if image_bytes is None:
                    base["error"] = _IMAGE_UNAVAILABLE
                    completed[position] = base
                    continue
                cache_key = self._cache_key(image_bytes, fingerprint)
                cached_text = self._read_cache(cache_key)
                if cached_text is not None:
                    completed[position] = {
                        **base,
                        "text": cached_text,
                        "model": engine_meta["model"],
                        "source": engine_meta["source"],
                    }
                    continue
                media_type = guess_media_type(local_path, mime_type)
                remaining = max(1.0, min(PER_IMAGE_TIMEOUT_SECONDS, deadline - time.monotonic()))
                future = executor.submit(
                    ocr_image_bytes,
                    image_bytes,
                    engine=engine,
                    timeout=remaining,
                    media_type=media_type,
                )
                pending[future] = (position, base, cache_key, media_type)

            for future in concurrent.futures.as_completed(pending):
                position, base, cache_key, _media_type = pending[future]
                try:
                    outcome = future.result()
                except Exception:  # defensive: ocr_image_bytes should not raise
                    outcome = {"text": "", "error": _OVER_BUDGET}
                if outcome.get("error"):
                    completed[position] = {**base, "error": str(outcome["error"])}
                else:
                    text = str(outcome.get("text") or "")
                    completed[position] = {
                        **base,
                        "text": text,
                        "model": engine_meta["model"],
                        "source": engine_meta["source"],
                    }
                    self._write_cache(cache_key, text)

            # Anything the executor has not delivered by the deadline is a
            # timeout; cancel what we can and mark the rest explicitly.
            for future, (position, base, _key, _media) in pending.items():
                if position in completed:
                    continue
                future.cancel()
                completed[position] = {**base, "error": _OVER_BUDGET}

        return [completed[position] for position in sorted(completed)]

    def _read_image(self, local_path: str) -> bytes | None:
        try:
            return Path(local_path).read_bytes()
        except Exception:
            return None

    def _cache_key(self, image_bytes: bytes, fingerprint: str) -> str | None:
        if self.cache_root is None:
            return None
        digest = hashlib.sha256(image_bytes).hexdigest()
        return hashlib.sha256(
            f"{CACHE_VERSION}|{digest}|{fingerprint}".encode("utf-8")
        ).hexdigest()[:40]

    def _cache_path(self, cache_key: str | None) -> Path | None:
        if self.cache_root is None or not cache_key:
            return None
        return self.cache_root / f"{cache_key}.json"

    def _read_cache(self, cache_key: str | None) -> str | None:
        path = self._cache_path(cache_key)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        text = payload.get("text")
        return str(text) if isinstance(text, str) else None

    def _write_cache(self, cache_key: str | None, text: str) -> None:
        path = self._cache_path(cache_key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"text": text}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except Exception:
            # A read-only cache directory must not break OCR results.
            return
