from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from worker.reply_runtime import ReplyRuntime
from worker.reply_runtime.answer_engine import AnswerEngineRequest
from worker.reply_runtime.dify import DifyChatflowAnswerEngine
from worker.reply_runtime.errors import RuntimeProtocolError


class _DifyFixture:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.upload_count = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "contentType": self.headers.get("Content-Type"),
                        "body": b"",
                    }
                )
                if self.path != "/v1/parameters":
                    self.send_error(404)
                    return
                payload = json.dumps(
                    {
                        "user_input_form": [
                            {
                                "text-input": {
                                    "variable": "region",
                                    "label": "区域",
                                    "required": True,
                                }
                            }
                        ],
                        "file_upload": {
                            "image": {
                                "enabled": True,
                                "number_limits": 3,
                                "transfer_methods": ["local_file"],
                            }
                        },
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                fixture.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "contentType": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )
                if self.path == "/v1/files/upload":
                    fixture.upload_count += 1
                    payload = json.dumps(
                        {
                            "id": f"upload-{fixture.upload_count}",
                            "name": f"file-{fixture.upload_count}",
                            "size": len(body),
                            "mime_type": "application/octet-stream",
                        }
                    ).encode("utf-8")
                    self.send_response(201)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path != "/v1/chat-messages":
                    self.send_error(404)
                    return
                events = [
                    "event: ping\n\n",
                    'data: {"event":"workflow_started","task_id":"task-1","workflow_run_id":"run-1","conversation_id":"conversation-1"}\n\n',
                    'data: {"event":"message","task_id":"task-1","message_id":"message-1","conversation_id":"conversation-1","answer":"库存"}\n\n',
                    'data: {"event":"message","task_id":"task-1","message_id":"message-1","conversation_id":"conversation-1","answer":"正常"}\n\n',
                    'data: {"event":"message_end","task_id":"task-1","message_id":"message-1","conversation_id":"conversation-1","metadata":{"retriever_resources":[{"document_name":"库存规则","content":"可售库存正常"}]}}\n\n',
                    'data: {"event":"workflow_finished","task_id":"task-1","workflow_run_id":"run-1","conversation_id":"conversation-1","data":{"status":"succeeded"}}\n\n',
                ]
                payload = "".join(events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class DifyChatflowAnswerEngineTests(unittest.TestCase):
    def test_common_answer_engine_request_runs_chatflow(self):
        fixture = _DifyFixture()
        try:
            result = DifyChatflowAnswerEngine().run(
                AnswerEngineRequest(
                    provider_config={
                        "baseUrl": fixture.base_url,
                        "inputs": {},
                        "apiKey": "app-secret",
                    },
                    listener_id="listener",
                    group_id="room",
                    sender_id="alice",
                    question="库存正常吗？",
                    messages=[{"text": "库存正常吗？"}],
                    context=[],
                    attachments=[],
                    images=[],
                    timeout_seconds=30,
                )
            )
        finally:
            fixture.close()

        self.assertEqual(result.provider, "dify")
        self.assertEqual(result.answer, "库存正常")

    def test_stream_returns_after_terminal_events_without_waiting_for_connection_close(self):
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                events = [
                    'data: {"event":"message","answer":"DIFY_OK","message_id":"m1"}\n\n',
                    'data: {"event":"message_end","message_id":"m1","metadata":{}}\n\n',
                    'data: {"event":"workflow_finished","workflow_run_id":"w1","data":{"status":"succeeded"}}\n\n',
                ]
                for event in events:
                    chunk = event.encode("utf-8")
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                release.wait(3)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        started = time.monotonic()
        try:
            result = DifyChatflowAnswerEngine().run(
                app={
                    "id": "probe",
                    "baseUrl": f"http://{host}:{port}/v1",
                    "inputs": {},
                    "apiKey": "app-secret",
                },
                listener_id="listener",
                group_id="room",
                sender_id="alice",
                question="probe",
                messages=[{"text": "probe"}],
                context=[],
                attachments=[],
                timeout_seconds=10,
            )
            elapsed = time.monotonic() - started
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result.answer, "DIFY_OK")
        self.assertLess(elapsed, 1.0)

    def test_connection_reads_app_parameters_without_starting_a_chat(self):
        fixture = _DifyFixture()
        try:
            capabilities = DifyChatflowAnswerEngine().test_connection(
                {
                    "baseUrl": fixture.base_url,
                    "apiKey": "app-secret",
                    "inputs": {"region": "cn"},
                },
                timeout_seconds=10,
            )
        finally:
            fixture.close()

        self.assertEqual(
            capabilities,
            {
                "inputVariables": [
                    {"name": "region", "label": "区域", "required": True}
                ],
                "fileUpload": {
                    "image": {
                        "enabled": True,
                        "numberLimit": 3,
                        "transferMethods": ["local_file"],
                    }
                },
            },
        )
        self.assertEqual(len(fixture.requests), 1)
        self.assertEqual(fixture.requests[0]["path"], "/v1/parameters")
        self.assertEqual(fixture.requests[0]["authorization"], "Bearer app-secret")

    def test_connection_rejects_missing_required_fixed_inputs(self):
        fixture = _DifyFixture()
        try:
            with self.assertRaises(RuntimeProtocolError) as raised:
                DifyChatflowAnswerEngine().test_connection(
                    {
                        "baseUrl": fixture.base_url,
                        "apiKey": "app-secret",
                        "inputs": {},
                    },
                    timeout_seconds=10,
                )
        finally:
            fixture.close()

        self.assertEqual(raised.exception.code, "DIFY_REQUIRED_INPUT_MISSING")
        self.assertEqual(raised.exception.details["inputNames"], ["region"])
        self.assertEqual([item["path"] for item in fixture.requests], ["/v1/parameters"])

    def test_streaming_chat_returns_answer_evidence_and_audit(self):
        fixture = _DifyFixture()
        try:
            engine = DifyChatflowAnswerEngine()
            result = engine.run(
                app={
                    "id": "stock-flow",
                    "baseUrl": fixture.base_url,
                    "inputs": {"tenant": "support"},
                    "apiKey": "app-secret",
                },
                listener_id="listener-1",
                group_id="group-1",
                sender_id="sender-1",
                question="库存为什么不对？",
                messages=[
                    {
                        "senderName": "Alice",
                        "text": "库存为什么不对？",
                        "localPath": "C:/must-not-leak.png",
                    }
                ],
                context=[{"question": "昨天正常吗？", "answer": "昨天正常"}],
                attachments=[],
                timeout_seconds=30,
            )
        finally:
            fixture.close()

        self.assertEqual(result.provider, "dify")
        self.assertEqual(result.answer, "库存正常")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.provider_audit["conversationId"], "conversation-1")
        self.assertEqual(result.provider_audit["messageId"], "message-1")
        self.assertEqual(result.provider_audit["taskId"], "task-1")
        self.assertEqual(result.provider_audit["workflowRunId"], "run-1")
        self.assertEqual(result.evidence[0]["provider"], "dify")
        self.assertEqual(result.evidence[0]["answer"], "库存正常")
        self.assertEqual(
            result.evidence[0]["retrieverResources"],
            [{"documentName": "库存规则", "content": "可售库存正常"}],
        )

        request = fixture.requests[0]
        self.assertEqual(request["path"], "/v1/chat-messages")
        self.assertEqual(request["authorization"], "Bearer app-secret")
        payload = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(payload["inputs"], {"tenant": "support"})
        self.assertEqual(payload["response_mode"], "streaming")
        self.assertEqual(payload["conversation_id"], "")
        self.assertEqual(payload["files"], [])
        self.assertTrue(payload["user"].startswith("wir-"))
        self.assertIn("库存为什么不对？", payload["query"])
        self.assertIn("昨天正常", payload["query"])
        self.assertNotIn("localPath", payload["query"])
        self.assertNotIn("must-not-leak", payload["query"])

    def test_local_files_are_uploaded_in_order_and_referenced_by_chat(self):
        fixture = _DifyFixture()
        try:
            with tempfile.TemporaryDirectory() as directory:
                image_path = Path(directory) / "库存截图.png"
                document_path = Path(directory) / "stock.pdf"
                image_path.write_bytes(b"png-file-bytes")
                document_path.write_bytes(b"pdf-file-bytes")

                result = DifyChatflowAnswerEngine().run(
                    app={
                        "id": "stock-flow",
                        "baseUrl": fixture.base_url,
                        "inputs": {},
                        "apiKey": "app-secret",
                    },
                    listener_id="listener-1",
                    group_id="group-1",
                    sender_id="sender-1",
                    question="请看附件",
                    messages=[{"text": "请看附件"}],
                    context=[],
                    attachments=[
                        {
                            "localPath": str(image_path),
                            "filename": image_path.name,
                            "mimeType": "image/png",
                        },
                        {
                            "localPath": str(document_path),
                            "filename": document_path.name,
                            "mimeType": "application/pdf",
                        },
                    ],
                    timeout_seconds=30,
                )
        finally:
            fixture.close()

        self.assertEqual(result.answer, "库存正常")
        self.assertEqual(
            [request["path"] for request in fixture.requests],
            ["/v1/files/upload", "/v1/files/upload", "/v1/chat-messages"],
        )
        image_upload, document_upload, chat_request = fixture.requests
        self.assertEqual(image_upload["authorization"], "Bearer app-secret")
        self.assertIn("multipart/form-data; boundary=", image_upload["contentType"])
        self.assertIn('name="file"; filename="库存截图.png"'.encode(), image_upload["body"])
        self.assertIn(b"Content-Type: image/png", image_upload["body"])
        self.assertIn(b"png-file-bytes", image_upload["body"])
        self.assertIn('name="file"; filename="stock.pdf"'.encode(), document_upload["body"])
        self.assertIn(b"Content-Type: application/pdf", document_upload["body"])
        self.assertIn(b"pdf-file-bytes", document_upload["body"])

        chat_payload = json.loads(chat_request["body"].decode("utf-8"))
        self.assertEqual(
            chat_payload["files"],
            [
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": "upload-1",
                },
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_file_id": "upload-2",
                },
            ],
        )
        user_marker = f'\r\n\r\n{chat_payload["user"]}\r\n'.encode()
        self.assertIn(user_marker, image_upload["body"])
        self.assertIn(user_marker, document_upload["body"])

    def test_more_than_eight_attachments_are_rejected_before_any_upload(self):
        fixture = _DifyFixture()
        try:
            with self.assertRaises(RuntimeProtocolError) as raised:
                DifyChatflowAnswerEngine().run(
                    app={
                        "baseUrl": fixture.base_url,
                        "inputs": {},
                        "apiKey": "app-secret",
                    },
                    listener_id="listener",
                    group_id="room",
                    sender_id="alice",
                    question="附件",
                    messages=[{"text": "附件"}],
                    context=[],
                    attachments=[
                        {
                            "localPath": f"missing-{index}.png",
                            "filename": f"{index}.png",
                            "mimeType": "image/png",
                        }
                        for index in range(9)
                    ],
                    timeout_seconds=10,
                )
        finally:
            fixture.close()

        self.assertEqual(raised.exception.code, "DIFY_TOO_MANY_FILES")
        self.assertEqual(fixture.requests, [])

    def test_parameter_capability_hint_does_not_block_a_real_upload(self):
        fixture = _DifyFixture()
        try:
            with tempfile.TemporaryDirectory() as directory:
                image_path = Path(directory) / "capture.png"
                image_path.write_bytes(b"png-file-bytes")
                result = DifyChatflowAnswerEngine().run(
                    app={
                        "id": "stock-flow",
                        "baseUrl": fixture.base_url,
                        "inputs": {},
                        "apiKey": "app-secret",
                        "capabilities": {
                            "inputVariables": [],
                            "fileUpload": {
                                "image": {
                                    "enabled": False,
                                    "numberLimit": 3,
                                    "transferMethods": ["local_file"],
                                }
                            },
                        },
                    },
                    listener_id="listener-1",
                    group_id="group-1",
                    sender_id="sender-1",
                    question="请看附件",
                    messages=[{"text": "请看附件"}],
                    context=[],
                    attachments=[
                        {
                            "localPath": str(image_path),
                            "filename": image_path.name,
                            "mimeType": "image/png",
                        }
                    ],
                    timeout_seconds=30,
                )
        finally:
            fixture.close()

        self.assertEqual(result.answer, "库存正常")
        self.assertEqual(
            [request["path"] for request in fixture.requests],
            ["/v1/files/upload", "/v1/chat-messages"],
        )

    def test_paused_chatflow_is_reported_without_waiting_for_disconnect(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                payload = (
                    'data: {"event":"human_input_required","task_id":"task-1",'
                    '"workflow_run_id":"run-1","data":{"form_id":"form-1"}}\n\n'
                    'data: {"event":"workflow_paused","task_id":"task-1",'
                    '"workflow_run_id":"run-1","data":{"status":"paused"}}\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            with self.assertRaises(RuntimeProtocolError) as raised:
                DifyChatflowAnswerEngine().run(
                    app={
                        "baseUrl": f"http://{host}:{port}/v1",
                        "inputs": {},
                        "apiKey": "app-secret",
                    },
                    listener_id="listener",
                    group_id="room",
                    sender_id="alice",
                    question="需要人工输入",
                    messages=[{"text": "需要人工输入"}],
                    context=[],
                    attachments=[],
                    timeout_seconds=10,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(raised.exception.code, "DIFY_WORKFLOW_PAUSED")
        self.assertFalse(raised.exception.retryable)

    def test_stream_decodes_split_utf8_and_applies_message_replacement(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                payload = (
                    'event: ping\n\n'
                    'data: {"event":"message","answer":"旧答"}\n\n'
                    'data: {"event":"message_replace","answer":"替换回答"}\n\n'
                    'data: {"event":"workflow_finished","workflow_run_id":"run-1",'
                    '"data":{"status":"succeeded"}}\n\n'
                    'data: {"event":"message_end","message_id":"message-1",'
                    '"metadata":{"retriever_resources":[]}}\n\n'
                ).encode("utf-8")
                split = payload.index("替".encode("utf-8")) + 1
                for chunk in (payload[:split], payload[split:]):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                    self.wfile.flush()
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            result = DifyChatflowAnswerEngine().run(
                app={
                    "baseUrl": f"http://{host}:{port}/v1",
                    "inputs": {},
                    "apiKey": "app-secret",
                },
                listener_id="listener",
                group_id="room",
                sender_id="alice",
                question="替换回答",
                messages=[{"text": "替换回答"}],
                context=[],
                attachments=[],
                timeout_seconds=10,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result.answer, "替换回答")


class DifyRuntimeConfigTests(unittest.TestCase):
    @staticmethod
    def _command(command_id: str, revision: int, body: dict) -> dict:
        return {
            "protocolVersion": 1,
            "commandId": command_id,
            "expectedRevision": revision,
            "body": body,
        }

    def test_dify_app_save_is_durable_revisioned_and_secret_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            runtime = ReplyRuntime(database, autostart=False)
            try:
                saved = runtime.execute(
                    {
                        "protocolVersion": 1,
                        "commandId": "save-dify-stock",
                        "expectedRevision": 0,
                        "body": {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1/",
                                "inputs": {"region": "cn"},
                            },
                            "secretPatch": {
                                "apiKey": {
                                    "mode": "replace",
                                    "value": "app-secret",
                                }
                            },
                        },
                    }
                )
                listed = runtime.query({"kind": "dify.list"})
            finally:
                runtime.close()

            reopened = ReplyRuntime(database, autostart=False)
            durable = reopened.query({"kind": "dify.list"})
            reopened.close()

        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["app"], listed["apps"][0])
        self.assertEqual(listed, durable)
        self.assertEqual(listed["apps"][0]["id"], "stock-flow")
        self.assertEqual(listed["apps"][0]["baseUrl"], "https://api.dify.ai/v1")
        self.assertEqual(listed["apps"][0]["inputs"], {"region": "cn"})
        self.assertTrue(listed["apps"][0]["secrets"]["apiKeyConfigured"])
        self.assertTrue(listed["apps"][0]["secrets"]["fingerprint"])
        self.assertNotIn("apiKey", listed["apps"][0])
        self.assertNotIn("app-secret", repr(listed))

    def test_dify_connection_test_uses_private_key_and_persists_capabilities(self):
        class DifyBoundary:
            def __init__(self) -> None:
                self.apps = []

            def test_connection(self, app, *, timeout_seconds):
                self.apps.append((app, timeout_seconds))
                return {
                    "inputVariables": [],
                    "fileUpload": {
                        "image": {
                            "enabled": True,
                            "numberLimit": 4,
                            "transferMethods": ["local_file"],
                        }
                    },
                }

        with tempfile.TemporaryDirectory() as directory:
            boundary = DifyBoundary()
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                dify=boundary,
                autostart=False,
            )
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {},
                            },
                            "secretPatch": {
                                "apiKey": {
                                    "mode": "replace",
                                    "value": "app-secret",
                                }
                            },
                        },
                    )
                )
                tested = runtime.execute(
                    self._command(
                        "test-dify",
                        1,
                        {"kind": "dify.test", "appId": "stock-flow"},
                    )
                )
                listed = runtime.query({"kind": "dify.list"})
            finally:
                runtime.close()

        self.assertEqual(tested["revision"], 2)
        self.assertTrue(tested["connected"])
        self.assertEqual(tested["capabilities"]["fileUpload"]["image"]["numberLimit"], 4)
        self.assertEqual(len(boundary.apps), 1)
        private_app, timeout_seconds = boundary.apps[0]
        self.assertEqual(private_app["apiKey"], "app-secret")
        self.assertEqual(timeout_seconds, 30)
        public_app = listed["apps"][0]
        self.assertTrue(public_app["connectionTestCurrent"])
        self.assertEqual(public_app["lastTest"]["status"], "success")
        self.assertEqual(public_app["capabilities"], tested["capabilities"])
        self.assertNotIn("app-secret", repr(tested))
        self.assertNotIn("app-secret", repr(listed))

    def test_dify_api_key_keep_replace_and_clear_are_secret_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            try:
                original = {
                    "id": "stock-flow",
                    "name": "库存 Chatflow",
                    "enabled": True,
                    "baseUrl": "https://api.dify.ai/v1",
                    "inputs": {},
                }
                runtime.execute(
                    self._command(
                        "save-original",
                        0,
                        {
                            "kind": "dify.save",
                            "app": original,
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "first-secret"}
                            },
                        },
                    )
                )
                kept = runtime.execute(
                    self._command(
                        "keep-secret",
                        1,
                        {
                            "kind": "dify.save",
                            "app": {**original, "name": "库存助手"},
                            "secretPatch": {"apiKey": {"mode": "keep"}},
                        },
                    )
                )
                replaced = runtime.execute(
                    self._command(
                        "replace-secret",
                        2,
                        {
                            "kind": "dify.save",
                            "app": original,
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "second-secret"}
                            },
                        },
                    )
                )
                cleared = runtime.execute(
                    self._command(
                        "clear-secret",
                        3,
                        {
                            "kind": "dify.save",
                            "app": original,
                            "secretPatch": {"apiKey": {"mode": "clear"}},
                        },
                    )
                )
                listed = runtime.query({"kind": "dify.list"})
            finally:
                runtime.close()

        self.assertTrue(kept["app"]["secrets"]["apiKeyConfigured"])
        self.assertTrue(replaced["app"]["secrets"]["apiKeyConfigured"])
        self.assertNotEqual(
            kept["app"]["secrets"]["fingerprint"],
            replaced["app"]["secrets"]["fingerprint"],
        )
        self.assertFalse(cleared["app"]["secrets"]["apiKeyConfigured"])
        self.assertEqual(cleared["app"]["secrets"]["fingerprint"], "")
        self.assertNotIn("first-secret", repr(listed))
        self.assertNotIn("second-secret", repr(listed))

    def test_dify_listener_does_not_require_mcp_grants_and_has_timeout_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {},
                            },
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                saved = runtime.execute(
                    self._command(
                        "save-listener",
                        1,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "stock-listener",
                                "name": "库存群",
                                "groupId": "room-1",
                                "answerEngine": "dify",
                                "difyAppId": "stock-flow",
                                "toolGrants": [],
                            },
                        },
                    )
                )
            finally:
                runtime.close()

        listener = saved["listener"]
        self.assertEqual(listener["answerEngine"], "dify")
        self.assertEqual(listener["difyAppId"], "stock-flow")
        self.assertEqual(listener["difyTimeoutSeconds"], 300)
        self.assertEqual(listener["toolGrants"], [])

    def test_dify_auto_send_requires_enabled_current_connection_test(self):
        class DifyBoundary:
            def test_connection(self, _app, *, timeout_seconds):
                self.timeout_seconds = timeout_seconds
                return {"inputVariables": [], "fileUpload": {}}

        class WebhookBoundary:
            def send(self, **_payload):
                return {"status": "sent"}

        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                dify=DifyBoundary(),
                webhook=WebhookBoundary(),
                autostart=False,
            )
            listener = {
                "id": "stock-listener",
                "name": "库存群",
                "groupId": "room-1",
                "answerEngine": "dify",
                "difyAppId": "stock-flow",
                "toolGrants": [],
                "webhookUrl": (
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                    "dify-auto-send-key"
                ),
            }
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {},
                            },
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "save-listener",
                        1,
                        {"kind": "listener.save", "listener": listener},
                    )
                )
                tested_webhook = runtime.execute(
                    self._command(
                        "test-webhook",
                        2,
                        {"kind": "listener.test_webhook", "listenerId": "stock-listener"},
                    )
                )
                runtime.execute(
                    self._command(
                        "confirm-webhook",
                        3,
                        {
                            "kind": "listener.confirm_webhook",
                            "listenerId": "stock-listener",
                            "testCode": tested_webhook["testCode"],
                            "appearedInSelectedGroup": True,
                        },
                    )
                )
                listener.pop("webhookUrl")
                listener["autoSend"] = True
                with self.assertRaisesRegex(Exception, "connection test"):
                    runtime.execute(
                        self._command(
                            "enable-before-dify-test",
                            4,
                            {
                                "kind": "listener.save",
                                "listener": listener,
                                "secretPatch": {"webhookUrl": {"mode": "keep"}},
                            },
                        )
                    )
                runtime.execute(
                    self._command(
                        "test-dify",
                        4,
                        {"kind": "dify.test", "appId": "stock-flow"},
                    )
                )
                enabled = runtime.execute(
                    self._command(
                        "enable-after-dify-test",
                        5,
                        {
                            "kind": "listener.save",
                            "listener": listener,
                            "secretPatch": {"webhookUrl": {"mode": "keep"}},
                        },
                    )
                )
            finally:
                runtime.close()

        self.assertTrue(enabled["listener"]["autoSend"])

    def test_dify_connection_change_invalidates_test_and_dependent_listener(self):
        class DifyBoundary:
            def test_connection(self, _app, *, timeout_seconds):
                return {"inputVariables": [], "fileUpload": {}}

        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                dify=DifyBoundary(),
                autostart=False,
            )
            app = {
                "id": "stock-flow",
                "name": "库存 Chatflow",
                "enabled": True,
                "baseUrl": "https://api.dify.ai/v1",
                "inputs": {"region": "cn"},
            }
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": app,
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "test-dify",
                        1,
                        {"kind": "dify.test", "appId": "stock-flow"},
                    )
                )
                runtime.execute(
                    self._command(
                        "save-listener",
                        2,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "stock-listener",
                                "name": "库存群",
                                "groupId": "room-1",
                                "answerEngine": "dify",
                                "difyAppId": "stock-flow",
                                "toolGrants": [],
                            },
                        },
                    )
                )
                changed = runtime.execute(
                    self._command(
                        "change-inputs",
                        3,
                        {
                            "kind": "dify.save",
                            "app": {**app, "inputs": {"region": "us"}},
                            "secretPatch": {"apiKey": {"mode": "keep"}},
                        },
                    )
                )
                apps = runtime.query({"kind": "dify.list"})
                listeners = runtime.query({"kind": "listener.list"})
            finally:
                runtime.close()

        self.assertFalse(changed["app"]["connectionTestCurrent"])
        self.assertFalse(apps["apps"][0]["connectionTestCurrent"])
        self.assertEqual(listeners["listeners"][0]["generation"], 2)
        self.assertEqual(
            listeners["listeners"][0]["health"]["status"],
            "dify_connection_test_required",
        )

    def test_enabling_or_disabling_dify_isolates_dependent_listener_work(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            app = {
                "id": "stock-flow",
                "name": "库存 Chatflow",
                "enabled": True,
                "baseUrl": "https://api.dify.ai/v1",
                "inputs": {},
            }
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": app,
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "save-listener",
                        1,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "stock-listener",
                                "name": "库存群",
                                "groupId": "room-1",
                                "answerEngine": "dify",
                                "difyAppId": "stock-flow",
                                "toolGrants": [],
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "disable-dify",
                        2,
                        {
                            "kind": "dify.save",
                            "app": {**app, "enabled": False},
                            "secretPatch": {"apiKey": {"mode": "keep"}},
                        },
                    )
                )
                listener = runtime.query({"kind": "listener.list"})["listeners"][0]
            finally:
                runtime.close()

        self.assertEqual(listener["generation"], 2)
        self.assertEqual(listener["health"]["status"], "disabled_dify_app")

    def test_referenced_dify_app_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(Path(directory) / "runtime.sqlite3", autostart=False)
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {},
                            },
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "save-listener",
                        1,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "stock-listener",
                                "name": "库存群",
                                "groupId": "room-1",
                                "answerEngine": "dify",
                                "difyAppId": "stock-flow",
                                "toolGrants": [],
                            },
                        },
                    )
                )
                with self.assertRaises(RuntimeProtocolError) as raised:
                    runtime.execute(
                        self._command(
                            "delete-dify",
                            2,
                            {"kind": "dify.delete", "appId": "stock-flow"},
                        )
                    )
            finally:
                runtime.close()

        self.assertEqual(raised.exception.code, "DIFY_APP_IN_USE")
        self.assertEqual(raised.exception.details["listenerIds"], ["stock-listener"])

    def test_failed_connection_test_redacts_the_configured_api_key(self):
        class DifyBoundary:
            def test_connection(self, app, *, timeout_seconds):
                raise RuntimeProtocolError(
                    "DIFY_CONNECTION_FAILED",
                    f"upstream rejected {app['apiKey']}",
                    retryable=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                dify=DifyBoundary(),
                autostart=False,
            )
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {},
                            },
                            "secretPatch": {
                                "apiKey": {
                                    "mode": "replace",
                                    "value": "super-secret-dify-key",
                                }
                            },
                        },
                    )
                )
                with self.assertRaises(RuntimeProtocolError) as raised:
                    runtime.execute(
                        self._command(
                            "test-dify",
                            1,
                            {"kind": "dify.test", "appId": "stock-flow"},
                        )
                    )
                listed = runtime.query({"kind": "dify.list"})
            finally:
                runtime.close()

        self.assertNotIn("super-secret-dify-key", repr(raised.exception.as_dict()))
        self.assertNotIn("super-secret-dify-key", repr(listed))
        self.assertIn("[REDACTED]", repr(listed))


class DifyRuntimeAnswerFlowTests(unittest.TestCase):
    @staticmethod
    def _command(command_id: str, revision: int, body: dict) -> dict:
        return {
            "protocolVersion": 1,
            "commandId": command_id,
            "expectedRevision": revision,
            "body": body,
        }

    def test_dify_listener_routes_answer_and_evidence_through_existing_review_gate(self):
        class Clock:
            value = 1_000.0

            def now(self):
                return self.value

        class Messages:
            def __init__(self):
                self.rows = []

            def watermark(self, _listener):
                return self.rows[-1]["cursor"] if self.rows else [999, 0, 0, 1]

            def read(self, _listener, cursor):
                return [
                    row for row in self.rows if tuple(row["cursor"]) > tuple(cursor)
                ]

        class Model:
            def __init__(self):
                self.reviews = []

            def classify(self, **_kwargs):
                return {"labels": ["question"]}

            def retrieve(self, **_kwargs):
                raise AssertionError("Dify mode must not use MCP retrieval")

            def answer(self, **_kwargs):
                raise AssertionError("Dify provides the raw answer")

            def review(self, **kwargs):
                self.reviews.append(kwargs)
                return {"supported": True, "reason": "Dify evidence supports the answer"}

        class DifyBoundary:
            def __init__(self):
                self.runs = []

            def test_connection(self, _app, *, timeout_seconds):
                return {"inputVariables": [], "fileUpload": {}}

            def run(self, request):
                self.runs.append(request)
                answer = "库存正常，请刷新页面。"
                return __import__(
                    "worker.reply_runtime.dify", fromlist=["AnswerEngineResult"]
                ).AnswerEngineResult(
                    provider="dify",
                    answer=answer,
                    evidence=[
                        {
                            "provider": "dify",
                            "answer": answer,
                            "retrieverResources": [
                                {"documentName": "库存规则", "content": "库存状态正常"}
                            ],
                            "result": {
                                "answer": answer,
                                "retrieverResources": [
                                    {"documentName": "库存规则", "content": "库存状态正常"}
                                ],
                            },
                        }
                    ],
                    provider_audit={"messageId": "dify-message-1"},
                )

        class McpBoundary:
            def call(self, **_kwargs):
                raise AssertionError("Dify mode must not call MCP")

        with tempfile.TemporaryDirectory() as directory:
            clock, source, model, dify = Clock(), Messages(), Model(), DifyBoundary()
            document_path = Path(directory) / "stock.pdf"
            document_path.write_bytes(b"pdf-bytes")
            runtime = ReplyRuntime(
                Path(directory) / "runtime.sqlite3",
                clock=clock,
                message_source=source,
                model=model,
                mcp=McpBoundary(),
                dify=dify,
                autostart=False,
            )
            try:
                runtime.execute(
                    self._command(
                        "save-dify",
                        0,
                        {
                            "kind": "dify.save",
                            "app": {
                                "id": "stock-flow",
                                "name": "库存 Chatflow",
                                "enabled": True,
                                "baseUrl": "https://api.dify.ai/v1",
                                "inputs": {"region": "cn"},
                            },
                            "secretPatch": {
                                "apiKey": {"mode": "replace", "value": "app-secret"}
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command(
                        "test-dify",
                        1,
                        {"kind": "dify.test", "appId": "stock-flow"},
                    )
                )
                runtime.execute(
                    self._command(
                        "save-listener",
                        2,
                        {
                            "kind": "listener.save",
                            "listener": {
                                "id": "stock-listener",
                                "name": "库存群",
                                "groupId": "room",
                                "enabled": True,
                                "answerEngine": "dify",
                                "difyAppId": "stock-flow",
                                "difyTimeoutSeconds": 120,
                                "pollIntervalSeconds": 2,
                                "sameSenderMergeSeconds": 2,
                                "humanReplyWaitSeconds": 10,
                                "toolGrants": [],
                            },
                        },
                    )
                )
                runtime.execute(
                    self._command("baseline", 3, {"kind": "runtime.tick", "wait": True})
                )
                source.rows.append(
                    {
                        "cursor": [1_001, 1, 1, 1],
                        "messageId": "1",
                        "serverId": "1",
                        "sequence": 1,
                        "sendTime": 1_001,
                        "groupId": "room",
                        "senderId": "alice",
                        "senderName": "Alice",
                        "contentType": "text",
                        "text": "库存为什么不对？",
                        "files": [
                            {
                                "localPath": str(document_path),
                                "filename": "stock.pdf",
                                "mimeType": "application/pdf",
                                "size": len(b"pdf-bytes"),
                            }
                        ],
                    }
                )
                clock.value = 1_005
                runtime.execute(
                    self._command("collect", 3, {"kind": "runtime.tick", "wait": True})
                )
                clock.value = 1_007
                runtime.execute(
                    self._command("classify", 3, {"kind": "runtime.tick", "wait": True})
                )
                clock.value = 1_017
                runtime.execute(
                    self._command("retrieve", 3, {"kind": "runtime.tick", "wait": True})
                )
                item = runtime.query({"kind": "work.list"})["items"][0]
                detail = runtime.query(
                    {"kind": "work.detail", "workId": item["id"]}
                )["item"]
            finally:
                runtime.close()

        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["answerEngine"], "dify")
        self.assertEqual(item["answer"], "库存正常，请刷新页面。")
        self.assertEqual(detail["evidence"][0]["provider"], "dify")
        self.assertEqual(detail["evidence"][0]["serverName"], "Dify Chatflow")
        self.assertIn("库存正常", detail["evidence"][0]["summary"])
        self.assertEqual(len(dify.runs), 1)
        self.assertEqual(dify.runs[0].timeout_seconds, 120)
        self.assertEqual(dify.runs[0].provider_config["apiKey"], "app-secret")
        self.assertEqual(
            dify.runs[0].attachments,
            [
                {
                    "localPath": str(document_path),
                    "filename": "stock.pdf",
                    "mimeType": "application/pdf",
                    "size": len(b"pdf-bytes"),
                }
            ],
        )
        self.assertEqual(model.reviews[0]["evidence"][0]["provider"], "dify")


if __name__ == "__main__":
    unittest.main()
