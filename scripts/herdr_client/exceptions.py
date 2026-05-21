from __future__ import annotations


class HerdrClientError(Exception):
    """Base exception for client-side herdr errors."""


class HerdrApiError(HerdrClientError):
    """Raised when the herdr API returns an error envelope."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
