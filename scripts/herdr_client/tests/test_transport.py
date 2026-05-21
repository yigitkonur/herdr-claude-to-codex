from __future__ import annotations

import os
from pathlib import Path

import pytest

from herdr_client.transport import DEFAULT_SOCKET_CANDIDATES, resolve_socket_path


@pytest.fixture(autouse=True)
def clear_socket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("HERDR_SOCKET_PATH", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)


def test_default_socket_candidates_include_documented_fallbacks() -> None:
    candidates = DEFAULT_SOCKET_CANDIDATES()

    assert Path("/tmp/herdr.sock") in candidates
    assert Path.home() / ".config" / "herdr" / "herdr.sock" in candidates


def test_resolve_socket_path_prefers_explicit_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "explicit.sock"
    explicit.touch()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(explicit))

    resolved = resolve_socket_path()

    assert resolved == explicit


def test_resolve_socket_path_falls_back_to_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime_socket = runtime_dir / "herdr.sock"
    runtime_socket.touch()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    resolved = resolve_socket_path()

    assert resolved == runtime_socket


def test_resolve_socket_path_raises_when_no_candidate_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_socket_path(
            candidates=[tmp_path / "missing-a.sock", tmp_path / "missing-b.sock"]
        )
