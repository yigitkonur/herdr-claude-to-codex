"""Python client for the herdr Unix socket API."""

from .client import HerdrClient
from .exceptions import HerdrApiError, HerdrClientError
from .transport import DEFAULT_SOCKET_CANDIDATES, resolve_socket_path

__all__ = [
    "DEFAULT_SOCKET_CANDIDATES",
    "HerdrApiError",
    "HerdrClient",
    "HerdrClientError",
    "resolve_socket_path",
]
