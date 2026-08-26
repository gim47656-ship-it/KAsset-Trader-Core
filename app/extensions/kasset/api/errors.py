"""Sanitized errors for the Android compatibility API."""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MobileApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any] | None = None

    def response_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def unauthorized(message: str = "세션이 만료되었거나 폐기되었습니다.") -> MobileApiError:
    return MobileApiError(401, "UNAUTHORIZED", message)
