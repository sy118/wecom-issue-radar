from __future__ import annotations


class ProxyTalkError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or None

    def as_dict(self) -> dict:
        value = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            value["details"] = self.details
        return value


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ProxyTalkError(code, message)
