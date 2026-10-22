"""LLM access for the daily assistant.

Reuses ``config.llm`` (OpenAI-compatible chat completions and Anthropic messages)
with a short timeout. Every failure is raised as :class:`ModelUnavailable` so the
actions can fall back to rule-based Chinese results instead of surfacing an error
to the UI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT_SECONDS = 45


class ModelUnavailable(RuntimeError):
    """The configured model is missing, unreachable or returned junk."""


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
) -> str:
    """Call the configured chat model and return its text answer."""
    llm = (config or {}).get("llm") or {}
    provider = str(llm.get("provider") or "openai_compatible").lower()
    base_url = str(llm.get("base_url") or "").rstrip("/")
    api_key = str(llm.get("api_key") or "")
    model = str(llm.get("model") or "")
    if not base_url or not model:
        raise ModelUnavailable("尚未配置大模型")
    try:
        timeout = max(5, min(int(llm.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS), 120))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS

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
            "messages": [{"role": "user", "content": user_prompt}],
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
                {"role": "user", "content": user_prompt},
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
        raise ModelUnavailable(f"模型请求失败：{exc}") from exc

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
                raise ModelUnavailable("模型响应中没有 choices")
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
        raise ModelUnavailable(f"模型响应格式无法解析：{exc}") from exc
    if not text.strip():
        raise ModelUnavailable("模型返回了空内容")
    return text.strip()


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
        raise ModelUnavailable(f"模型请求失败 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise ModelUnavailable(f"无法连接模型服务：{exc.reason}") from exc
    except OSError as exc:
        raise ModelUnavailable(f"模型请求失败：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelUnavailable("模型返回的不是 JSON") from exc
    if not isinstance(data, dict):
        raise ModelUnavailable("模型响应顶层不是 JSON 对象")
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
            raise ModelUnavailable("模型返回内容不是 JSON 对象")
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelUnavailable("模型返回内容不是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ModelUnavailable("模型返回内容不是 JSON 对象")
    return parsed
