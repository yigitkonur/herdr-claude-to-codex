from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .exceptions import HerdrApiError, HerdrClientError
from .transport import resolve_socket_path

JsonDict = dict[str, Any]


class Subscription:
    def __init__(self, sock: socket.socket, file: Any, ack: JsonDict) -> None:
        self._socket = sock
        self.ack = ack
        self._file = file

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._socket.close()

    def events(self) -> Iterator[JsonDict]:
        while True:
            line = self._file.readline()
            if line == "":
                return
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


class HerdrClient:
    def __init__(self, socket_path: str | Path | None = None, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path) if socket_path is not None else resolve_socket_path()
        self.timeout = timeout

    def _new_id(self) -> str:
        return f"req_{uuid.uuid4().hex}"

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        return sock

    def _send_envelope(self, sock: socket.socket, method: str, params: JsonDict | None = None) -> JsonDict:
        request_id = self._new_id()
        envelope = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        payload = json.dumps(envelope).encode("utf-8") + b"\n"
        sock.sendall(payload)

        file = sock.makefile("r", encoding="utf-8")
        try:
            line = file.readline()
        finally:
            file.close()
        if line == "":
            raise HerdrClientError("herdr socket closed before a response was received")
        response = json.loads(line)
        if "error" in response:
            error = response["error"]
            raise HerdrApiError(error["code"], error["message"])
        return response["result"]

    def request(self, method: str, params: JsonDict | None = None) -> JsonDict:
        with self._connect() as sock:
            return self._send_envelope(sock, method, params)

    def ping(self) -> JsonDict:
        return self.request("ping")

    def workspace_list(self) -> JsonDict:
        return self.request("workspace.list")

    def tab_list(self, workspace_id: str | None = None) -> JsonDict:
        params = {"workspace_id": workspace_id} if workspace_id is not None else {}
        return self.request("tab.list", params)

    def pane_list(self, workspace_id: str | None = None) -> JsonDict:
        params = {"workspace_id": workspace_id} if workspace_id is not None else {}
        return self.request("pane.list", params)

    def pane_send_text(self, pane_id: str, text: str) -> JsonDict:
        return self.request("pane.send_text", {"pane_id": pane_id, "text": text})

    def pane_send_keys(self, pane_id: str, keys: Sequence[str]) -> JsonDict:
        return self.request("pane.send_keys", {"pane_id": pane_id, "keys": list(keys)})

    def pane_send_input(
        self, pane_id: str, text: str = "", keys: Sequence[str] | None = None
    ) -> JsonDict:
        return self.request(
            "pane.send_input",
            {
                "pane_id": pane_id,
                "text": text,
                "keys": list(keys or []),
            },
        )

    def pane_read(
        self,
        pane_id: str,
        source: str = "recent",
        lines: int | None = 80,
        strip_ansi: bool = True,
    ) -> JsonDict:
        params: JsonDict = {
            "pane_id": pane_id,
            "source": source,
            "strip_ansi": strip_ansi,
        }
        if lines is not None:
            params["lines"] = lines
        return self.request("pane.read", params)

    def pane_targeted_read(
        self,
        pane_id: str,
        target: JsonDict,
        source: str = "visible",
        lines: int | None = None,
        strip_ansi: bool = True,
        trim: bool = False,
    ) -> JsonDict:
        params: JsonDict = {
            "pane_id": pane_id,
            "source": source,
            "target": target,
            "strip_ansi": strip_ansi,
            "trim": trim,
        }
        if lines is not None:
            params["lines"] = lines
        return self.request("pane.targeted_read", params)

    def pane_wait_for_output(
        self,
        pane_id: str,
        match: JsonDict,
        source: str = "recent",
        lines: int | None = None,
        timeout_ms: int | None = None,
        strip_ansi: bool = True,
    ) -> JsonDict:
        params: JsonDict = {
            "pane_id": pane_id,
            "source": source,
            "match": match,
            "strip_ansi": strip_ansi,
        }
        if lines is not None:
            params["lines"] = lines
        if timeout_ms is not None:
            params["timeout_ms"] = timeout_ms
        return self.request("pane.wait_for_output", params)

    def subscribe(self, subscriptions: Iterable[JsonDict]) -> Subscription:
        sock = self._connect()
        request_id = self._new_id()
        envelope = {
            "id": request_id,
            "method": "events.subscribe",
            "params": {"subscriptions": list(subscriptions)},
        }
        sock.sendall(json.dumps(envelope).encode("utf-8") + b"\n")
        file = sock.makefile("r", encoding="utf-8")
        line = file.readline()
        if line == "":
            file.close()
            sock.close()
            raise HerdrClientError("herdr socket closed before subscription ack")
        ack = json.loads(line)
        if "error" in ack:
            error = ack["error"]
            file.close()
            sock.close()
            raise HerdrApiError(error["code"], error["message"])
        return Subscription(sock, file, ack)
