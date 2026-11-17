"""Error envelope that matches the Error schema in contracts/v1/openapi.json."""
from datetime import UTC, datetime

from fastapi.responses import JSONResponse

CATEGORIES = {400: "INPUT", 401: "AUTHENTICATION", 403: "PERMISSION", 404: "MISSING", 409: "CONFLICT",
              410: "MISSING", 429: "CONFLICT", 500: "INTERNAL", 502: "PROVIDER", 503: "PROVIDER"}


class ApiError(Exception):
    def __init__(self, status, code, message, *, category=None, transaction=None, provider=None,
                 retryable=False, headers=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.category = category or CATEGORIES.get(status, "INTERNAL")
        self.transaction, self.provider = transaction, provider
        self.retryable, self.headers = retryable, headers or {}

    def response(self):
        body = {"code": self.code, "category": self.category, "message": self.message,
                "transaction_reference": str(self.transaction) if self.transaction else None,
                "provider_reference": self.provider, "retryable": self.retryable,
                "timestamp": datetime.now(UTC).isoformat()}
        return JSONResponse(status_code=self.status, content=body, headers=self.headers)


def unauthorized(code="INVALID_CREDENTIALS", message="Valid credentials are required."):
    return ApiError(401, code, message, headers={"WWW-Authenticate": "Bearer"})


def not_found(code, message, **extra):
    return ApiError(404, code, message, **extra)


def conflict(code, message, **extra):
    return ApiError(409, code, message, **extra)
