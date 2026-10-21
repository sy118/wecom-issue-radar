from __future__ import annotations

import asyncio
import json
import os
import sys

from .errors import ProxyTalkError


class McpAdapter:
    """Stateless MCP adapter; runtime policy stays outside this true-external seam."""

    def discover(self, server: dict, secrets: dict) -> list[dict]:
        return self._run(self._discover(server, secrets), timeout=45)

    def call(self, server: dict, secrets: dict, tool_name: str, arguments: dict, *, timeout: int = 60):
        return self._run(self._call(server, secrets, tool_name, arguments), timeout=timeout)

    @staticmethod
    def _run(awaitable, *, timeout: int):
        try:
            return asyncio.run(asyncio.wait_for(awaitable, timeout=max(1, timeout)))
        except ProxyTalkError:
            raise
        except TimeoutError as exc:
            raise ProxyTalkError("MCP_TIMEOUT", "MCP 操作超时。", retryable=True) from exc
        except (ModuleNotFoundError, ImportError) as exc:
            raise ProxyTalkError("MCP_SDK_UNAVAILABLE", "安装包中缺少 MCP SDK。") from exc
        except Exception as exc:
            raise ProxyTalkError("MCP_CONNECTION_FAILED", "MCP 连接或调用失败。", retryable=True) from exc

    async def _discover(self, server: dict, secrets: dict) -> list[dict]:
        async with self._session(server, secrets) as session:
            response = await session.list_tools()
            result = []
            for tool in response.tools:
                annotations = getattr(tool, "annotations", None)
                read_only = bool(getattr(annotations, "readOnlyHint", False) or getattr(annotations, "read_only_hint", False))
                result.append(
                    {
                        "name": tool.name,
                        "title": getattr(tool, "title", None) or tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.inputSchema or {"type": "object"},
                        "readOnly": read_only,
                    }
                )
            return result

    async def _call(self, server: dict, secrets: dict, tool_name: str, arguments: dict):
        async with self._session(server, secrets) as session:
            result = await session.call_tool(tool_name, arguments)
            if getattr(result, "isError", False) or getattr(result, "is_error", False):
                raise ProxyTalkError("MCP_TOOL_ERROR", f"MCP 工具 {tool_name} 返回错误。")
            if hasattr(result, "model_dump"):
                return result.model_dump(mode="json", by_alias=True)
            return json.loads(json.dumps(result, default=str))

    def _session(self, server: dict, secrets: dict):
        return _McpSessionContext(server, secrets)


class _McpSessionContext:
    def __init__(self, server: dict, secrets: dict) -> None:
        self.server = server
        self.secrets = secrets
        self._transport_context = None
        self._session_context = None
        self._transport_entered = False
        self._session_entered = False

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters

        try:
            if self.server["transport"] == "stdio":
                from mcp.client.stdio import stdio_client

                environment = os.environ.copy()
                environment.update({str(key): str(value) for key, value in (self.secrets.get("env") or {}).items()})
                params = StdioServerParameters(
                    command=self.server["command"],
                    args=[str(item) for item in self.server.get("args") or []],
                    env=environment,
                )
                self._transport_context = stdio_client(params)
                read, write = await self._transport_context.__aenter__()
                self._transport_entered = True
            else:
                from mcp.client.streamable_http import streamablehttp_client

                headers = {str(key): str(value) for key, value in (self.secrets.get("headers") or {}).items()}
                self._transport_context = streamablehttp_client(self.server["url"], headers=headers)
                transport = await self._transport_context.__aenter__()
                self._transport_entered = True
                read, write = transport[0], transport[1]
            self._session_context = ClientSession(read, write)
            session = await self._session_context.__aenter__()
            self._session_entered = True
            await session.initialize()
            return session
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if self._session_context and self._session_entered:
                await self._session_context.__aexit__(exc_type, exc, traceback)
        finally:
            if self._transport_context and self._transport_entered:
                await self._transport_context.__aexit__(exc_type, exc, traceback)
            self._session_context = None
            self._transport_context = None
            self._session_entered = False
            self._transport_entered = False


class MemoryMcpAdapter:
    def __init__(self, catalog: list[dict] | None = None, results: dict | None = None) -> None:
        self.catalog = catalog or []
        self.results = results or {}

    def discover(self, server: dict, secrets: dict) -> list[dict]:
        return list(self.catalog)

    def call(self, server: dict, secrets: dict, tool_name: str, arguments: dict, *, timeout: int = 60):
        return self.results.get(tool_name, {"content": [{"type": "text", "text": ""}]})
