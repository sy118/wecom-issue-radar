from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inputTokens": max(0, int(self.input_tokens)),
            "outputTokens": max(0, int(self.output_tokens)),
            "totalTokens": max(0, int(self.total_tokens)),
        }


@dataclass(frozen=True)
class AgentCheckpoint:
    stage: str
    rounds: int = 0
    tool_calls: int = 0
    stop_reason: str = ""
    evidence: list[dict] = field(default_factory=list)
    usage: AgentUsage = field(default_factory=AgentUsage)
