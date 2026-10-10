"""Error type shared by the browser client, daemon, and command handlers."""

from typing import Optional


class BrowserError(Exception):
    """An expected, user-facing failure with a stable error code."""

    def __init__(self, message: str, code: str, details: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details

    def to_response(self) -> dict:
        resp = {"ok": False, "error": self.message, "code": self.code}
        if self.details:
            resp["details"] = self.details
        return resp
