from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def DEFAULT_SOCKET_CANDIDATES() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("HERDR_SOCKET_PATH")
    if explicit:
        candidates.append(Path(explicit))

    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        candidates.append(Path(xdg_runtime_dir) / "herdr.sock")

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        candidates.append(Path(xdg_config_home) / "herdr" / "herdr.sock")

    candidates.append(Path.home() / ".config" / "herdr" / "herdr.sock")
    candidates.append(Path("/tmp/herdr.sock"))

    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def resolve_socket_path(candidates: Iterable[Path] | None = None) -> Path:
    resolved_candidates = list(candidates) if candidates is not None else DEFAULT_SOCKET_CANDIDATES()
    for candidate in resolved_candidates:
        if candidate.exists():
            return candidate
    searched = ", ".join(str(candidate) for candidate in resolved_candidates)
    raise FileNotFoundError(f"No herdr socket found; checked: {searched}")
