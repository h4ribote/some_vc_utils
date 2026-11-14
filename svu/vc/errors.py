"""Errors from VirtualCrypto, split by whether the request certainly did not take effect."""

from __future__ import annotations

from typing import Any


class VCError(Exception):
    """A failed VirtualCrypto request: status is None when no response arrived, code is the error_info or error_description of the body."""

    def __init__(self, message: str, *, status: int | None = None, error: str | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.code = code

    @property
    def definitive(self) -> bool:
        """True when VirtualCrypto refused the request, so it had no effect and repeating it unchanged will be refused again."""
        return False


class VCRequestError(VCError):
    """VirtualCrypto refused the request; nothing changed on its side."""

    @property
    def definitive(self) -> bool:
        return True


class VCTransientError(VCError):
    """No answer, a server error, a rate limit, an authentication problem or a key still in flight: the outcome is unknown and the same request may be retried."""


def error_from_response(status: int, body: Any) -> VCError:
    """Classify an error response: 400, 404, 409 (except an idempotency key still processing) and 422 are refusals, anything else leaves the outcome unknown."""
    error = body.get("error") if isinstance(body, dict) else None
    code = None
    if isinstance(body, dict):
        for key in ("error_info", "error_description"):
            value = body.get(key)
            if isinstance(value, str):
                code = value
                break
    message = f"VirtualCrypto answered HTTP {status}" + (f" ({error}: {code})" if error or code else "")
    error_name = error if isinstance(error, str) else None
    if status in (400, 404, 422) or (status == 409 and error_name != "processing"):
        return VCRequestError(message, status=status, error=error_name, code=code)
    return VCTransientError(message, status=status, error=error_name, code=code)
