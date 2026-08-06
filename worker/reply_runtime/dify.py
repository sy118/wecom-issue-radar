from __future__ import annotations

import codecs
import hashlib
import http.client
import json
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .answer_engine import AnswerEngineRequest, AnswerEngineResult
from .errors import RuntimeProtocolError


MAX_DIFY_ATTACHMENTS = 8
MAX_DIFY_ATTACHMENT_TOTAL_BYTES = 40 * 1024 * 1024

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeProtocolError(
            "DIFY_REDIRECT_BLOCKED",
            "Dify returned a redirect; refusing to forward the API key",
        )


class DifyChatflowAnswerEngine:
    """Deep adapter over Dify file upload and Chatflow streaming."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def test_connection(self, app: dict, *, timeout_seconds: int = 30) -> dict:
        base_url = _base_url(app)
        api_key = str(app.get("apiKey") or "")
        if not api_key:
            raise RuntimeProtocolError("DIFY_AUTH_REQUIRED", "Dify API key is required")
        request = urllib.request.Request(
            f"{base_url}/parameters",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=max(1, int(timeout_seconds))) as response:
                payload = response.read(1_048_577)
                if len(payload) > 1_048_576:
                    raise RuntimeProtocolError(
                        "DIFY_INVALID_RESPONSE",
                        "Dify parameters response is too large",
                        retryable=True,
                    )
                value = json.loads(payload.decode("utf-8"))
        except RuntimeProtocolError:
            raise
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeProtocolError(
                "DIFY_INVALID_RESPONSE",
                "Dify parameters response is malformed",
                retryable=True,
            ) from exc
        except http.client.HTTPException as exc:
            raise RuntimeProtocolError(
                "DIFY_CONNECTION_FAILED",
                "Dify parameters response was interrupted",
                retryable=True,
            ) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise RuntimeProtocolError(
                "DIFY_TIMEOUT" if _looks_like_timeout(exc) else "DIFY_CONNECTION_FAILED",
                "Dify parameters request timed out"
                if _looks_like_timeout(exc)
                else "could not connect to Dify",
                retryable=True,
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeProtocolError(
                "DIFY_INVALID_RESPONSE",
                "Dify parameters response must be an object",
                retryable=True,
            )
        capabilities = _normalize_capabilities(value)
        validate_dify_app_capabilities(app, capabilities)
        return capabilities

    def run(
        self,
        request: AnswerEngineRequest | None = None,
        *,
        app: dict | None = None,
        listener_id: str = "",
        group_id: str = "",
        sender_id: str = "",
        question: str = "",
        messages: list[dict] | None = None,
        context: list[dict] | None = None,
        attachments: list[dict] | None = None,
        timeout_seconds: int | None = None,
    ) -> AnswerEngineResult:
        if request is not None:
            if not isinstance(request, AnswerEngineRequest):
                raise RuntimeProtocolError(
                    "INVALID_ANSWER_ENGINE_REQUEST",
                    "Dify answer engine request has an invalid shape",
                )
            app = request.provider_config
            listener_id = request.listener_id
            group_id = request.group_id
            sender_id = request.sender_id
            question = request.question
            messages = request.messages
            context = request.context
            attachments = request.attachments
            timeout_seconds = request.timeout_seconds
        if not isinstance(app, dict):
            raise RuntimeProtocolError("INVALID_DIFY_APP", "Dify app is required")
        messages = messages or []
        context = context or []
        attachments = attachments or []
        timeout_seconds = 300 if timeout_seconds is None else int(timeout_seconds)
        base_url = _base_url(app)
        api_key = str(app.get("apiKey") or "")
        if not api_key:
            raise RuntimeProtocolError("DIFY_AUTH_REQUIRED", "Dify API key is required")
        inputs = app.get("inputs") or {}
        if not isinstance(inputs, dict):
            raise RuntimeProtocolError("INVALID_DIFY_APP", "Dify inputs must be an object")
        user = _stable_user(listener_id, group_id, sender_id)
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        _validate_attachment_capabilities(app.get("capabilities"), attachments)
        uploaded_files = self._upload_attachments(
            base_url=base_url,
            api_key=api_key,
            user=user,
            attachments=attachments,
            deadline=deadline,
        )
        payload = {
            "inputs": inputs,
            "query": _query_text(question, messages, context),
            "response_mode": "streaming",
            "conversation_id": "",
            "user": user,
            "files": uploaded_files,
        }
        request = urllib.request.Request(
            f"{base_url}/chat-messages",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=_remaining_seconds(deadline)) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    raise RuntimeProtocolError(
                        "DIFY_INVALID_RESPONSE",
                        "Dify Chatflow did not return an event stream",
                        retryable=True,
                    )
                parsed = _read_chatflow_events(response, deadline)
        except RuntimeProtocolError:
            raise
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except UnicodeDecodeError as exc:
            raise RuntimeProtocolError(
                "DIFY_INVALID_RESPONSE",
                "Dify Chatflow returned invalid UTF-8",
                retryable=True,
            ) from exc
        except http.client.HTTPException as exc:
            raise RuntimeProtocolError(
                "DIFY_STREAM_INCOMPLETE",
                "Dify Chatflow stream was interrupted",
                retryable=True,
            ) from exc
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            raise RuntimeProtocolError(
                "DIFY_TIMEOUT" if _looks_like_timeout(exc) else "DIFY_CONNECTION_FAILED",
                "Dify Chatflow request timed out"
                if _looks_like_timeout(exc)
                else "could not connect to Dify Chatflow",
                retryable=True,
            ) from exc

        answer = "".join(parsed["answerParts"]).strip()
        if not parsed["messageEnd"] or parsed["workflowStatus"] != "succeeded":
            raise RuntimeProtocolError(
                "DIFY_STREAM_INCOMPLETE",
                "Dify Chatflow stream ended before successful completion",
                retryable=True,
            )
        if not answer:
            return AnswerEngineResult(
                provider="dify",
                answer="",
                evidence=[],
                provider_audit=parsed["audit"],
                stop_reason="empty_answer",
            )
        evidence = [
            {
                "provider": "dify",
                "answer": answer,
                "retrieverResources": parsed["retrieverResources"],
                "result": {
                    "answer": answer,
                    "retrieverResources": parsed["retrieverResources"],
                },
                **parsed["audit"],
            }
        ]
        return AnswerEngineResult(
            provider="dify",
            answer=answer,
            evidence=evidence,
            provider_audit=parsed["audit"],
        )

    def _upload_attachments(
        self,
        *,
        base_url: str,
        api_key: str,
        user: str,
        attachments: list[dict],
        deadline: float,
    ) -> list[dict]:
        if len(attachments) > MAX_DIFY_ATTACHMENTS:
            raise RuntimeProtocolError(
                "DIFY_TOO_MANY_FILES",
                f"Dify accepts at most {MAX_DIFY_ATTACHMENTS} attachments per question",
            )
        prepared: list[tuple[str, str, bytes]] = []
        total_bytes = 0
        for attachment in attachments:
            if not isinstance(attachment, dict):
                raise RuntimeProtocolError(
                    "DIFY_INVALID_FILE", "Dify attachment must be an object"
                )
            path = Path(str(attachment.get("localPath") or ""))
            if not path.is_file():
                raise RuntimeProtocolError(
                    "DIFY_FILE_MISSING", "a Dify attachment is no longer available"
                )
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise RuntimeProtocolError(
                    "DIFY_FILE_UNREADABLE", "could not read a Dify attachment"
                ) from exc
            total_bytes += len(data)
            if total_bytes > MAX_DIFY_ATTACHMENT_TOTAL_BYTES:
                raise RuntimeProtocolError(
                    "DIFY_FILES_TOO_LARGE",
                    "Dify attachments exceed the 40 MB total limit",
                )
            filename = Path(str(attachment.get("filename") or path.name)).name
            filename = (
                filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
                or path.name
            )
            mime_type = str(attachment.get("mimeType") or "application/octet-stream")
            prepared.append((filename, mime_type, data))

        result = []
        for filename, mime_type, data in prepared:
            boundary = f"wecom-issue-radar-{secrets.token_hex(12)}"
            body = _multipart_file(boundary, filename, mime_type, data, user)
            request = urllib.request.Request(
                f"{base_url}/files/upload",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with self._opener.open(request, timeout=_remaining_seconds(deadline)) as response:
                    raw_response = response.read(1_048_577)
                    if len(raw_response) > 1_048_576:
                        raise RuntimeProtocolError(
                            "DIFY_INVALID_RESPONSE",
                            "Dify file upload response is too large",
                            retryable=True,
                        )
                    value = json.loads(raw_response.decode("utf-8"))
            except RuntimeProtocolError:
                raise
            except urllib.error.HTTPError as exc:
                raise _http_error(exc) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeProtocolError(
                    "DIFY_INVALID_RESPONSE",
                    "Dify file upload returned malformed JSON",
                    retryable=True,
                ) from exc
            except http.client.HTTPException as exc:
                raise RuntimeProtocolError(
                    "DIFY_CONNECTION_FAILED",
                    "Dify file upload response was interrupted",
                    retryable=True,
                ) from exc
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                raise RuntimeProtocolError(
                    "DIFY_TIMEOUT" if _looks_like_timeout(exc) else "DIFY_CONNECTION_FAILED",
                    "Dify file upload timed out"
                    if _looks_like_timeout(exc)
                    else "could not upload a file to Dify",
                    retryable=True,
                ) from exc
            upload_id = str(value.get("id") or "") if isinstance(value, dict) else ""
            if not upload_id:
                raise RuntimeProtocolError(
                    "DIFY_INVALID_RESPONSE",
                    "Dify file upload response did not contain an id",
                    retryable=True,
                )
            result.append(
                {
                    "type": _dify_file_type(mime_type),
                    "transfer_method": "local_file",
                    "upload_file_id": upload_id,
                }
            )
        return result


def _base_url(app: dict) -> str:
    value = str(app.get("baseUrl") or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeProtocolError(
            "INVALID_DIFY_APP", "Dify Base URL must be an HTTP or HTTPS URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeProtocolError(
            "INVALID_DIFY_APP", "Dify Base URL contains unsupported URL parts"
        )
    return value


def _stable_user(listener_id: str, group_id: str, sender_id: str) -> str:
    digest = hashlib.sha256(
        "\0".join((str(listener_id), str(group_id), str(sender_id))).encode("utf-8")
    ).hexdigest()
    return f"wir-{digest[:32]}"


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeProtocolError(
            "DIFY_TIMEOUT", "Dify Chatflow request timed out", retryable=True
        )
    return max(1.0, remaining)


def _multipart_file(
    boundary: str, filename: str, mime_type: str, data: bytes, user: str
) -> bytes:
    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8")
    suffix = (
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="user"\r\n\r\n'
        f"{user}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    return prefix + data + suffix


def _dify_file_type(mime_type: str) -> str:
    value = str(mime_type or "").lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    if value.startswith("text/") or value in {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }:
        return "document"
    return "custom"


def _normalize_capabilities(value: dict) -> dict:
    input_variables = []
    forms = value.get("user_input_form")
    if isinstance(forms, list):
        for form in forms:
            if not isinstance(form, dict):
                continue
            definition = next(
                (item for item in form.values() if isinstance(item, dict)), None
            )
            if not definition:
                continue
            name = str(definition.get("variable") or "")
            if not name:
                continue
            input_variables.append(
                {
                    "name": name,
                    "label": str(definition.get("label") or name),
                    "required": bool(definition.get("required", False)),
                }
            )
    file_upload = {}
    configured_upload = value.get("file_upload")
    if isinstance(configured_upload, dict):
        for category, definition in configured_upload.items():
            if not isinstance(definition, dict):
                continue
            try:
                number_limit = max(0, int(definition.get("number_limits") or 0))
            except (TypeError, ValueError):
                number_limit = 0
            methods = definition.get("transfer_methods")
            file_upload[str(category)] = {
                "enabled": bool(definition.get("enabled", False)),
                "numberLimit": number_limit,
                "transferMethods": [
                    str(item) for item in methods if isinstance(item, str)
                ]
                if isinstance(methods, list)
                else [],
            }
    return {"inputVariables": input_variables, "fileUpload": file_upload}


def validate_dify_app_capabilities(app: dict, capabilities: dict) -> None:
    inputs = app.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise RuntimeProtocolError("INVALID_DIFY_APP", "Dify inputs must be an object")
    variables = capabilities.get("inputVariables") if isinstance(capabilities, dict) else []
    missing = [
        str(item.get("name") or "")
        for item in variables or []
        if isinstance(item, dict)
        and item.get("required") is True
        and str(item.get("name") or "") not in inputs
    ]
    missing = [name for name in missing if name]
    if missing:
        raise RuntimeProtocolError(
            "DIFY_REQUIRED_INPUT_MISSING",
            "Dify fixed inputs are missing required Chatflow variables",
            details={"inputNames": missing},
        )


def _validate_attachment_capabilities(capabilities, attachments: list[dict]) -> None:
    if not attachments or not isinstance(capabilities, dict):
        return
    file_upload = capabilities.get("fileUpload")
    if not isinstance(file_upload, dict):
        return
    counts: dict[str, int] = {}
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        file_type = _dify_file_type(str(attachment.get("mimeType") or ""))
        counts[file_type] = counts.get(file_type, 0) + 1
    for file_type, count in counts.items():
        config = file_upload.get(file_type)
        if not isinstance(config, dict) and file_type != "image":
            config = file_upload.get("fileUploadConfig")
        # Some self-hosted Dify versions report file_upload.enabled=false from
        # /parameters while /files/upload still accepts and Chatflow consumes the
        # same file. Treat the flag and transfer methods as UI hints; the upload
        # endpoint remains the authoritative format/size capability check.
        if not isinstance(config, dict):
            continue
        try:
            number_limit = max(0, int(config.get("numberLimit") or 0))
        except (TypeError, ValueError):
            number_limit = 0
        if number_limit and count > number_limit:
            raise RuntimeProtocolError(
                "DIFY_TOO_MANY_FILES",
                f"Dify app accepts at most {number_limit} {file_type} attachments",
                details={"fileType": file_type, "limit": number_limit, "actual": count},
            )


def _query_text(question: str, messages: list[dict], context: list[dict]) -> str:
    payload = {
        "question": str(question or ""),
        "currentMessages": _public_context(messages),
        "sentConversation": _public_context(context[-6:]),
    }
    return (
        "请根据以下企业微信问题、当前消息和已经实际发送的历史问答作答。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _public_context(value):
    if isinstance(value, list):
        return [_public_context(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _public_context(nested)
            for key, nested in value.items()
            if str(key) not in {"localPath", "data", "base64", "dataUrl"}
        }
    return value


def _read_chatflow_events(response, deadline: float) -> dict:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    answer_parts: list[str] = []
    message_end = False
    workflow_status = ""
    retriever_resources: list[dict] = []
    audit = {
        "conversationId": "",
        "messageId": "",
        "taskId": "",
        "workflowRunId": "",
    }

    def result() -> dict:
        return {
            "answerParts": answer_parts,
            "messageEnd": message_end,
            "workflowStatus": workflow_status,
            "retrieverResources": retriever_resources,
            "audit": audit,
        }

    def consume(block: str) -> None:
        nonlocal message_end, workflow_status, retriever_resources
        data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            return
        try:
            event = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise RuntimeProtocolError(
                "DIFY_INVALID_RESPONSE",
                "Dify returned malformed streaming JSON",
                retryable=True,
            ) from exc
        if not isinstance(event, dict):
            return
        for source, target in (
            ("conversation_id", "conversationId"),
            ("message_id", "messageId"),
            ("task_id", "taskId"),
            ("workflow_run_id", "workflowRunId"),
        ):
            if event.get(source):
                audit[target] = str(event[source])
        kind = str(event.get("event") or "")
        if kind == "message":
            answer_parts.append(str(event.get("answer") or ""))
        elif kind == "message_replace":
            answer_parts[:] = [str(event.get("answer") or "")]
        elif kind == "message_end":
            message_end = True
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            retriever_resources = _retriever_resources(metadata.get("retriever_resources"))
        elif kind == "workflow_finished":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            workflow_status = str(data.get("status") or "")
            if workflow_status == "failed":
                raise RuntimeProtocolError(
                    "DIFY_WORKFLOW_FAILED",
                    str(data.get("error") or "Dify Chatflow workflow failed"),
                    retryable=True,
                )
            if workflow_status and workflow_status != "succeeded":
                raise RuntimeProtocolError(
                    "DIFY_WORKFLOW_FAILED",
                    str(data.get("error") or f"Dify Chatflow ended with {workflow_status}"),
                    retryable=True,
                )
        elif kind in {"human_input_required", "workflow_paused"}:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            raise RuntimeProtocolError(
                "DIFY_WORKFLOW_PAUSED",
                "Dify Chatflow requires interactive human input and cannot be auto-sent",
                retryable=False,
                details={"formId": str(data.get("form_id") or "")},
            )
        elif kind == "error":
            raise RuntimeProtocolError(
                "DIFY_WORKFLOW_FAILED",
                str(event.get("message") or "Dify Chatflow returned an error"),
                retryable=True,
                details={"difyCode": str(event.get("code") or "")},
            )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeProtocolError(
                "DIFY_TIMEOUT", "Dify Chatflow request timed out", retryable=True
            )
        _set_response_timeout(response, max(0.1, remaining))
        read_available = getattr(response, "read1", None)
        chunk = (
            read_available(4096)
            if callable(read_available)
            else response.read(4096)
        )
        if not chunk:
            buffer += decoder.decode(b"", final=True)
            break
        buffer += decoder.decode(chunk)
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            consume(block)
            if message_end and workflow_status:
                return result()
    if buffer.strip():
        consume(buffer)
    return result()


def _set_response_timeout(response, timeout_seconds: float) -> None:
    """Keep each blocking SSE read inside the run-wide deadline."""

    candidates = [
        getattr(response, "fp", None),
        getattr(getattr(response, "fp", None), "raw", None),
    ]
    for candidate in candidates:
        sock = getattr(candidate, "_sock", None)
        if sock is None:
            sock = getattr(candidate, "sock", None)
        setter = getattr(sock, "settimeout", None)
        if callable(setter):
            try:
                setter(float(timeout_seconds))
            except OSError:
                pass
            return


def _retriever_resources(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:50]:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "documentName": str(item.get("document_name") or "")[:500],
                "content": str(item.get("content") or "")[:16_000],
            }
        )
    return result


def _http_error(exc: urllib.error.HTTPError) -> RuntimeProtocolError:
    code = (
        "DIFY_AUTH_FAILED"
        if exc.code == 401
        else "DIFY_FILE_TOO_LARGE"
        if exc.code == 413
        else "DIFY_FILE_UNSUPPORTED"
        if exc.code == 415
        else "DIFY_RATE_LIMITED"
        if exc.code == 429
        else "DIFY_REQUEST_FAILED"
    )
    return RuntimeProtocolError(
        code,
        f"Dify request failed with HTTP {int(exc.code)}",
        retryable=exc.code == 429 or exc.code >= 500,
    )


def _looks_like_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in str(exc).lower()
