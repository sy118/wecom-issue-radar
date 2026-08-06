from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from .agent import AgentRetrievalResult
from .errors import RuntimeProtocolError


@dataclass(frozen=True)
class AnswerEngineRequest:
    """Provider-neutral input for one retrieval-and-answer run."""

    provider_config: dict
    listener_id: str
    group_id: str
    sender_id: str
    question: str
    messages: list[dict]
    context: list[dict]
    attachments: list[dict]
    images: list[dict]
    timeout_seconds: int
    max_rounds: int = 6


@dataclass(frozen=True)
class AnswerEngineResult:
    provider: str
    answer: str
    evidence: list[dict]
    provider_audit: dict = field(default_factory=dict)
    stop_reason: str = "completed"


class AnswerEngine(Protocol):
    def run(self, request: AnswerEngineRequest) -> AnswerEngineResult:
        """Return a provider answer and evidence or raise a public runtime error."""


class McpAnswerEngine:
    """Deep adapter over native-tool Agent retrieval and MCP-backed answering."""

    def __init__(
        self,
        *,
        model,
        mcp,
        tool_specs: Callable[[list[dict]], list[dict]],
        server_loader: Callable[[str], dict],
        redact: Callable[[object], object],
        has_evidence: Callable[[object], bool],
    ) -> None:
        self._model = model
        self._mcp = mcp
        self._tool_specs = tool_specs
        self._server_loader = server_loader
        self._redact = redact
        self._has_evidence = has_evidence

    def run(self, request: AnswerEngineRequest) -> AnswerEngineResult:
        if self._model is None:
            raise RuntimeProtocolError(
                "MODEL_ADAPTER_UNAVAILABLE", "model adapter is unavailable"
            )
        if self._mcp is None:
            raise RuntimeProtocolError(
                "MCP_ADAPTER_UNAVAILABLE", "MCP adapter is unavailable"
            )

        listener = request.provider_config
        grants = listener.get("toolGrants") or []
        tools = self._tool_specs(grants)
        allowed = {
            (str(grant.get("serverId") or ""), str(grant.get("toolName") or "")): grant
            for grant in grants
            if isinstance(grant, dict)
        }

        def invoke_tool(server_id, tool_name, arguments, remaining_seconds):
            key = (str(server_id or ""), str(tool_name or ""))
            if key not in allowed:
                raise RuntimeProtocolError(
                    "MODEL_INVALID_TOOL_CALL",
                    "the model requested a tool outside the listener grant set",
                )
            server = self._server_loader(key[0])
            try:
                result = self._mcp.call(
                    server=server,
                    toolName=key[1],
                    arguments=arguments if isinstance(arguments, dict) else {},
                    timeoutSeconds=max(1, int(remaining_seconds)),
                )
            except RuntimeProtocolError as exc:
                raise RuntimeProtocolError(
                    exc.code,
                    str(self._redact(exc.message)),
                    retryable=exc.retryable,
                    details=(
                        self._redact(exc.details)
                        if isinstance(exc.details, dict)
                        else None
                    ),
                ) from exc
            except Exception as exc:
                timeout_error = isinstance(exc, TimeoutError) or (
                    "timeout" in type(exc).__name__.lower()
                )
                raise RuntimeProtocolError(
                    "MCP_TIMEOUT" if timeout_error else "MCP_OPERATION_FAILED",
                    str(self._redact(str(exc))) or "MCP operation failed",
                    retryable=True,
                ) from exc
            return self._redact(result)

        retriever = getattr(self._model, "retrieve", None)
        if not callable(retriever):
            raise RuntimeProtocolError(
                "MODEL_TOOL_CALLING_UNSUPPORTED",
                "the configured model adapter does not implement native tool retrieval",
            )
        retrieval = retriever(
            question=request.question,
            context=request.context,
            tools=tools,
            systemPrompt=str(listener.get("systemPrompt") or ""),
            images=request.images,
            invokeTool=invoke_tool,
            hasEvidence=self._has_evidence,
            maxRounds=request.max_rounds,
            timeoutSeconds=request.timeout_seconds,
        )
        if not isinstance(retrieval, AgentRetrievalResult):
            raise RuntimeProtocolError(
                "MODEL_INVALID_TOOL_CALL",
                "the native tool Agent returned an invalid retrieval result",
            )
        evidence = list(retrieval.evidence)
        if retrieval.stop_reason == "timeout" and not evidence:
            raise RuntimeProtocolError(
                "MCP_TIMEOUT", "MCP retrieval budget was exhausted", retryable=True
            )
        audit = {
            "rounds": int(retrieval.rounds),
            "toolCalls": int(retrieval.tool_calls),
            "stopReason": str(retrieval.stop_reason),
        }
        if not evidence:
            return AnswerEngineResult(
                provider="mcp",
                answer="",
                evidence=[],
                provider_audit=audit,
                stop_reason=str(retrieval.stop_reason),
            )

        answer = str(
            self._model.answer(
                question=request.question,
                context=request.context,
                evidence=evidence,
                systemPrompt=str(listener.get("systemPrompt") or ""),
                images=request.images,
            )
            or ""
        ).strip()
        return AnswerEngineResult(
            provider="mcp",
            answer=str(self._redact(answer)),
            evidence=list(self._redact(evidence)),
            provider_audit=audit,
            stop_reason=str(retrieval.stop_reason),
        )
