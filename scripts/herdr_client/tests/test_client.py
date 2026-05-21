from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from herdr_client.client import HerdrClient
from herdr_client.exceptions import HerdrApiError


class FakeHerdrServer:
    def __init__(self, socket_path: Path, handlers: list[dict[str, Any]]) -> None:
        self.socket_path = socket_path
        self.handlers = handlers
        self.requests: list[dict[str, Any]] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=2), "server did not start"

    def close(self) -> None:
        self._stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(self.socket_path))
        except OSError:
            pass
        self._thread.join(timeout=2)
        if self.socket_path.exists():
            self.socket_path.unlink()
        if self._error is not None:
            raise self._error

    def _serve(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen()
        server.settimeout(0.1)
        self._ready.set()
        try:
            while not self._stop.is_set() and self.handlers:
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                with conn:
                    raw = self._recv_line(conn)
                    if not raw:
                        continue
                    request = json.loads(raw)
                    self.requests.append(request)
                    handler = self.handlers.pop(0)
                    for response in handler["responses"]:
                        payload = response(request) if callable(response) else response
                        conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except BaseException as exc:  # pragma: no cover - surfaced in close()
            self._error = exc
        finally:
            server.close()

    @staticmethod
    def _recv_line(conn: socket.socket) -> str:
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        data = b"".join(chunks)
        return data.splitlines()[0].decode("utf-8") if data else ""


@pytest.fixture
def socket_path() -> Path:
    # Vendored-test portability tweak (see ../NOTICE): macOS caps AF_UNIX socket
    # paths (~104 chars) and pytest's tmp_path is too long, so bind under a short
    # /tmp dir instead. This is the only change from the upstream test suite.
    d = Path(tempfile.mkdtemp(prefix="hc", dir="/tmp"))
    try:
        yield d / "h.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ping_round_trips_over_unix_socket(socket_path: Path) -> None:
    server = FakeHerdrServer(
        socket_path,
        handlers=[
            {
                "responses": [
                    lambda request: {
                        "id": request["id"],
                        "result": {"type": "pong", "version": "0.2.0"},
                    }
                ]
            }
        ],
    )
    server.start()
    try:
        client = HerdrClient(socket_path=socket_path)

        result = client.ping()

        assert result["type"] == "pong"
        assert result["version"] == "0.2.0"
        assert server.requests[0]["method"] == "ping"
        assert server.requests[0]["params"] == {}
    finally:
        server.close()


def test_api_errors_raise_exception(socket_path: Path) -> None:
    server = FakeHerdrServer(
        socket_path,
        handlers=[
            {
                "responses": [
                    lambda request: {
                        "id": request["id"],
                        "error": {
                            "code": "pane_not_found",
                            "message": "pane w123-99 not found",
                        },
                    }
                ]
            }
        ],
    )
    server.start()
    try:
        client = HerdrClient(socket_path=socket_path)

        with pytest.raises(HerdrApiError) as exc:
            client.pane_send_text("w123-99", "hello")

        assert exc.value.code == "pane_not_found"
        assert "w123-99" in str(exc.value)
    finally:
        server.close()


def test_pane_send_input_sends_text_and_keys(socket_path: Path) -> None:
    server = FakeHerdrServer(
        socket_path,
        handlers=[
            {
                "responses": [
                    lambda request: {"id": request["id"], "result": {"type": "ok"}}
                ]
            }
        ],
    )
    server.start()
    try:
        client = HerdrClient(socket_path=socket_path)

        result = client.pane_send_input("w123-1", text="status", keys=["Enter"])

        assert result == {"type": "ok"}
        assert server.requests[0]["method"] == "pane.send_input"
        assert server.requests[0]["params"] == {
            "pane_id": "w123-1",
            "text": "status",
            "keys": ["Enter"],
        }
    finally:
        server.close()


def test_pane_targeted_read_sends_region_target(socket_path: Path) -> None:
    server = FakeHerdrServer(
        socket_path,
        handlers=[
            {
                "responses": [
                    lambda request: {
                        "id": request["id"],
                        "result": {
                            "type": "pane_targeted_read",
                            "read": {
                                "pane_id": "w123-1",
                                "workspace_id": "w123",
                                "tab_id": "w123:1",
                                "source": "visible",
                                "target_type": "region",
                                "region": {
                                    "left": 0,
                                    "top": 0,
                                    "width": 80,
                                    "height": 24,
                                },
                                "text": "targeted output",
                                "revision": 3,
                                "truncated": False,
                            },
                        },
                    }
                ]
            }
        ],
    )
    server.start()
    try:
        client = HerdrClient(socket_path=socket_path)
        target = {
            "type": "region",
            "left": 0,
            "width": 80,
            "top": 0,
            "bottom": 0,
        }

        result = client.pane_targeted_read("w123-1", target)

        assert result["read"]["text"] == "targeted output"
        assert server.requests[0]["method"] == "pane.targeted_read"
        assert server.requests[0]["params"] == {
            "pane_id": "w123-1",
            "source": "visible",
            "target": target,
            "strip_ansi": True,
            "trim": False,
        }
    finally:
        server.close()


def test_subscription_reads_ack_then_events(socket_path: Path) -> None:
    server = FakeHerdrServer(
        socket_path,
        handlers=[
            {
                "responses": [
                    lambda request: {
                        "id": request["id"],
                        "result": {"type": "subscription_started"},
                    },
                    {
                        "event": "workspace_created",
                        "data": {
                            "workspace": {
                                "workspace_id": "w1",
                                "number": 1,
                                "label": "demo",
                                "focused": True,
                                "pane_count": 1,
                                "tab_count": 1,
                                "active_tab_id": "w1:1",
                                "agent_status": "unknown",
                            }
                        },
                    },
                ]
            }
        ],
    )
    server.start()
    try:
        client = HerdrClient(socket_path=socket_path)

        with client.subscribe([{"type": "workspace.created"}]) as subscription:
            assert subscription.ack["result"]["type"] == "subscription_started"
            event = next(subscription.events())

        assert event["event"] == "workspace_created"
        assert event["data"]["workspace"]["workspace_id"] == "w1"
        assert server.requests[0]["method"] == "events.subscribe"
    finally:
        server.close()
