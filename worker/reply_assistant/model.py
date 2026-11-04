"""LLM access for the reply assistant.

Reuses ``config.llm`` (OpenAI-compatible chat completions and Anthropic messages)
with a bounded timeout. Every failure is raised as :class:`ModelUnavailable`
carrying a ``kind`` so the action can fall back to rule-based Chinese results and
report a useful reason instead of surfacing an error to the UI.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 45
MAX_ASSISTANT_TIMEOUT_SECONDS = 300

FAILURE_KINDS = frozenset(
    {
        "timeout",
        "http_4xx",
        "http_5xx",
        "network_error",
        "invalid_response",
        "empty",
        "vision_unsupported",
        "unknown",
    }
)

_VISION_HINT_KEYWORDS = ("image", "vision", "unsupported", "不支持")
_TIMEOUT_HINT_KEYWORDS = ("timeout", "timed out", "超时")
_VISION_REJECTION_KEYWORDS = (
    "unsupported",
    "not support",
    "reject",
    "invalid content",
    "不支持",
)


def _infer_failure_kind(message: str) -> str:
    """Best-effort classification for callers that raise with just a message."""

    text = str(message or "").casefold()
    if any(keyword in text for keyword in _VISION_HINT_KEYWORDS) and any(
        keyword in text for keyword in _VISION_REJECTION_KEYWORDS
    ):
        return "vision_unsupported"
    if any(keyword in text for keyword in _TIMEOUT_HINT_KEYWORDS):
        return "timeout"
    if "json" in text:
        return "invalid_response"
    if "空内容" in text or "empty" in text:
        return "empty"
    return "unknown"


class ModelUnavailable(RuntimeError):
    """The configured model is missing, unreachable or returned junk."""

    def __init__(self, message: str = "", kind: str = "") -> None:
        super().__init__(message)
        resolved = str(kind or "").strip()
        self.kind = resolved if resolved in FAILURE_KINDS else _infer_failure_kind(message)


def model_configured(config: dict | None) -> bool:
    llm = (config or {}).get("llm") or {}
    return bool(str(llm.get("base_url") or "").strip() and str(llm.get("model") or "").strip())


def call_llm(
    config: dict | None,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 1200,
    json_mode: bool = False,
    images: list[Path] | None = None,
    timeout_seconds: int | None = None,
) -> str:
    """Call the configured chat model and return its text answer.

    ``timeout_seconds`` overrides ``config.llm.timeout_seconds`` for this call; the
    reply assistant always passes an explicit per-attempt budget.
    """
    llm = (config or {}).get("llm") or {}
    provider = str(llm.get("provider") or "openai_compatible").lower()
    base_url = str(llm.get("base_url") or "").rstrip("/")
    api_key = str(llm.get("api_key") or "")
    model = str(llm.get("model") or "")
    if not base_url or not model:
        raise ModelUnavailable("尚未配置大模型", "unknown")
    if timeout_seconds is not None:
        try:
            timeout = max(1, min(int(timeout_seconds), MAX_ASSISTANT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            configured = int(llm.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            configured = DEFAULT_TIMEOUT_SECONDS
        # The reply assistant never waits on the legacy 180s configuration.
        timeout = max(5, min(configured, DEFAULT_TIMEOUT_SECONDS))

    if provider == "anthropic":
        if base_url.endswith("/v1/messages"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/messages"
        else:
            endpoint = f"{base_url}/v1/messages"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": float(llm.get("temperature") or 0.3),
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": _anthropic_content(user_prompt, images)}
            ],
        }
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        if base_url.endswith("/chat/completions"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/chat/completions"
        else:
            endpoint = f"{base_url}/v1/chat/completions"
        body = {
            "model": model,
            "temperature": float(llm.get("temperature") or 0.3),
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _openai_content(user_prompt, images)},
            ],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    try:
        data = _post_json(endpoint, body, headers, timeout)
    except ModelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - network errors are expected here
        raise ModelUnavailable(f"模型请求失败：{exc}", "unknown") from exc

    try:
        if provider == "anthropic":
            blocks = data.get("content") or []
            text = "\n".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            choices = data.get("choices") or []
            if not choices:
                raise ModelUnavailable("模型响应中没有 choices", "invalid_response")
            content = (choices[0].get("message") or {}).get("content") or ""
            if isinstance(content, list):
                text = "\n".join(
                    str(item.get("text") or item.get("content") or "")
                    for item in content
                    if isinstance(item, dict)
                )
            else:
                text = str(content)
    except ModelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - defensive against odd payloads
        raise ModelUnavailable(
            f"模型响应格式无法解析：{exc}", "invalid_response"
        ) from exc
    if not text.strip():
        raise ModelUnavailable("模型返回了空内容", "empty")
    return text.strip()


MAX_VISION_IMAGES = 3
MAX_VISION_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VISION_TOTAL_BYTES = 12 * 1024 * 1024


def _image_mime(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".bmp":
        return "image/bmp"
    return "image/png"


def _read_images(images: list[Path] | None) -> list[tuple[str, str]]:
    loaded: list[tuple[str, str]] = []
    total_bytes = 0
    for item in images or []:
        try:
            data = Path(item).read_bytes()
        except OSError:
            continue
        if not data or len(data) > MAX_VISION_IMAGE_BYTES:
            continue
        if total_bytes + len(data) > MAX_VISION_TOTAL_BYTES:
            continue
        total_bytes += len(data)
        loaded.append(
            (_image_mime(Path(item).name), base64.b64encode(data).decode("ascii"))
        )
        if len(loaded) >= MAX_VISION_IMAGES:
            break
    return loaded


def _anthropic_content(user_prompt: str, images: list[Path] | None) -> list[dict]:
    blocks: list[dict] = []
    for mime, data in _read_images(images):
        blocks.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            }
        )
    blocks.append({"type": "text", "text": user_prompt})
    return blocks


def _openai_content(user_prompt: str, images: list[Path] | None) -> list[dict] | str:
    contents: list[dict] = []
    loaded = _read_images(images)
    if not loaded:
        return user_prompt
    for mime, data in loaded:
        contents.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            }
        )
    contents.append({"type": "text", "text": user_prompt})
    return contents


def _looks_like_timeout(value) -> bool:
    if isinstance(value, TimeoutError):
        return True
    text = str(value or "").casefold()
    return any(keyword in text for keyword in _TIMEOUT_HINT_KEYWORDS)


def _looks_like_vision_rejection(message: str) -> bool:
    text = str(message or "").casefold()
    return any(
        keyword in text for keyword in ("image", "vision", "unsupported", "content")
    )


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        code = int(exc.code)
        if 400 <= code < 500:
            kind = "vision_unsupported" if _looks_like_vision_rejection(detail) else "http_4xx"
        elif code >= 500:
            kind = "http_5xx"
        else:
            kind = "unknown"
        raise ModelUnavailable(f"模型请求失败 HTTP {code}：{detail}", kind) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        kind = "timeout" if _looks_like_timeout(reason) else "network_error"
        raise ModelUnavailable(f"无法连接模型服务：{reason}", kind) from exc
    except OSError as exc:
        kind = "timeout" if _looks_like_timeout(exc) else "network_error"
        raise ModelUnavailable(f"模型请求失败：{exc}", kind) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelUnavailable("模型返回的不是 JSON", "invalid_response") from exc
    if not isinstance(data, dict):
        raise ModelUnavailable("模型响应顶层不是 JSON 对象", "invalid_response")
    return data


def parse_json_object(text: str) -> dict:
    """Parse a model JSON answer, tolerating markdown fences and chatter."""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`")
        if value.lower().startswith("json"):
            value = value[4:]
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ModelUnavailable("模型返回内容不是 JSON 对象", "invalid_response")
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelUnavailable(
                "模型返回内容不是 JSON 对象", "invalid_response"
            ) from exc
    if not isinstance(parsed, dict):
        raise ModelUnavailable("模型返回内容不是 JSON 对象", "invalid_response")
    return parsed
