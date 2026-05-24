#!/usr/bin/env python3
"""Build deterministic HERDR tab labels from caller workspace/tab names."""
import argparse
import json
import os
import re
import socket
import sys
import uuid

RESERVED = {"pi", "claude", "codex", "opencode", "hermes", "herdr",
            "default", "none", "null"}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){0,2}$")
SAFE_CHARS_RE = re.compile(r"[^a-z0-9]+")


class NamingError(Exception):
    pass


def validate_slug(slug):
    if not slug or not SLUG_RE.fullmatch(slug):
        raise NamingError("slug must be lowercase, 1-3 words, and match [a-z0-9-]")
    parts = slug.split("-")
    bad = [p for p in parts if p in RESERVED]
    if bad:
        raise NamingError(f"slug contains reserved word: {bad[0]}")
    return slug


def safe_name(value, fallback):
    raw = (value or fallback or "").strip().lower()
    safe = SAFE_CHARS_RE.sub("-", raw).strip("-")
    safe = re.sub(r"-{2,}", "-", safe)
    return safe or SAFE_CHARS_RE.sub("-", fallback.lower()).strip("-") or "unknown"


def rpc(socket_path, method, params=None, timeout=10):
    request = {"id": f"name_{uuid.uuid4().hex}", "method": method, "params": params or {}}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    try:
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    if not data:
        raise NamingError("HERDR socket closed without a response")
    response = json.loads(data.decode("utf-8"))
    if "error" in response:
        err = response["error"]
        raise NamingError(f"{method} failed: {err.get('message', err)}")
    return response["result"]


def caller_context(request, env=None):
    env = env or os.environ
    pane_id = env.get("HERDR_PANE_ID")
    if not pane_id:
        raise NamingError("HERDR_PANE_ID is not set")
    pane = request("pane.get", {"pane_id": pane_id})["pane"]
    workspace_id = pane["workspace_id"]
    tab_id = pane["tab_id"]
    workspace = request("workspace.get", {"workspace_id": workspace_id})["workspace"]
    tab = request("tab.get", {"tab_id": tab_id})["tab"]
    return {
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "space_name": safe_name(workspace.get("label"), workspace_id),
        "tab_name": safe_name(tab.get("label"), tab_id),
        "space_label": workspace.get("label") or workspace_id,
        "tab_label": tab.get("label") or tab_id,
    }


def collision_free_label(request, workspace_id, base_label):
    existing = {
        t.get("label")
        for t in request("tab.list", {"workspace_id": workspace_id}).get("tabs", [])
    }
    if base_label not in existing:
        return base_label
    suffix = 2
    while f"{base_label}-{suffix}" in existing:
        suffix += 1
    return f"{base_label}-{suffix}"


def build_label(request, slug, target_workspace_id=None, env=None):
    slug = validate_slug(slug)
    ctx = caller_context(request, env)
    base = f"{ctx['space_name']}-{ctx['tab_name']}-{slug}"
    label = collision_free_label(request, target_workspace_id or ctx["workspace_id"], base)
    ctx.update({"slug": slug, "base_label": base, "label": label})
    return ctx


def main():
    parser = argparse.ArgumentParser(description="Build a HERDR tab label from caller context.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--workspace", default=None, help="Target workspace for collision checks.")
    parser.add_argument("--socket", default=os.path.expanduser("~/.config/herdr/herdr.sock"))
    args = parser.parse_args()
    try:
        result = build_label(lambda m, p: rpc(args.socket, m, p), args.slug, args.workspace)
    except NamingError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
