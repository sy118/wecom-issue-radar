from __future__ import annotations

import json
import sys
import threading
from typing import TextIO

from .errors import RuntimeProtocolError


class NdjsonWriter:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, value: dict) -> None:
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.stream.write(line + "\n")
            self.stream.flush()


def serve_reply_runtime(
    runtime,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    writer: NdjsonWriter | None = None,
) -> int:
    """Serve correlated command/query requests until stdin reaches EOF."""

    writer = writer or NdjsonWriter(output_stream)
    runtime.event_sink = lambda seq, event: writer.write(
        {"type": "event", "seq": int(seq), "event": event}
    )
    try:
        for raw_line in input_stream:
            line = raw_line.strip()
            if not line:
                continue
            request_id = None
            try:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeProtocolError(
                        "INVALID_NDJSON", "request line is not valid JSON",
                        details={"line": exc.lineno, "column": exc.colno},
                    ) from exc
                if not isinstance(request, dict):
                    raise RuntimeProtocolError("INVALID_REQUEST", "request must be a JSON object")
                request_id = request.get("id")
                operation = str(request.get("op") or "")
                payload = request.get("payload")
                if operation == "execute":
                    data = runtime.execute(payload)
                elif operation == "query":
                    data = runtime.query(payload)
                else:
                    raise RuntimeProtocolError(
                        "UNKNOWN_OPERATION", "op must be execute or query"
                    )
                if hasattr(runtime, "redact_public"):
                    data = runtime.redact_public(data)
                writer.write({"id": request_id, "ok": True, "data": data})
            except RuntimeProtocolError as exc:
                error = exc.as_dict()
                if hasattr(runtime, "redact_public"):
                    error = runtime.redact_public(error)
                writer.write({"id": request_id, "ok": False, "error": error})
            except Exception as exc:
                # Detailed tracebacks belong on stderr only; never contaminate stdout NDJSON.
                print(
                    f"reply runtime request failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                writer.write(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "reply runtime request failed",
                            "retryable": True,
                        },
                    }
                )
    finally:
        runtime.close()
    return 0


def run_default_reply_runtime(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    from .factory import build_default_runtime

    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    writer = NdjsonWriter(output_stream)
    runtime = build_default_runtime(
        event_sink=lambda seq, event: writer.write(
            {"type": "event", "seq": int(seq), "event": event}
        ),
        autostart=True,
    )
    return serve_reply_runtime(runtime, input_stream, output_stream, writer=writer)
