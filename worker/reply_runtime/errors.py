from __future__ import annotations


class RuntimeProtocolError(Exception):
    """A stable, serializable error returned across the sidecar boundary."""

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
        self.details = details

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.retryable:
            result["retryable"] = True
        if self.details:
            result["details"] = self.details
        return result
