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
    raw_value = (value or fallback or "").strip()
    if raw_value == "~":
        raw_value = os.path.basename(os.path.expanduser("~"))
    raw = raw_value.lower()
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
    tab_name = safe_name(tab.get("label"), tab_id)
    space_label = workspace.get("label") or workspace_id
    space_name = tab_name if space_label == "~" else safe_name(space_label, workspace_id)
    return {
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "space_name": space_name,
        "tab_name": tab_name,
        "space_label": space_label,
        "tab_label": tab.get("label") or tab_id,
    }


MODES = ("pane", "tab", "space")


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


def pane_collision_free(request, tab_id, base_label):
    """Walk -2, -3, ... suffixes against existing pane labels in `tab_id`. Used for
    pane mode where multiple Codex helpers can share a tab; the existing tab-level
    helper checks tab labels, not pane labels, so a pane-scoped check is needed."""
    panes = request("pane.list", {}).get("panes", [])
    existing = {p.get("label") for p in panes if p.get("tab_id") == tab_id}
    if base_label not in existing:
        return base_label
    suffix = 2
    while f"{base_label}-{suffix}" in existing:
        suffix += 1
    return f"{base_label}-{suffix}"


def collision_free_workspace_label(request, base_label):
    """Walk -2, -3, ... suffixes against existing workspace labels. Used for space
    mode so each `codex.py start` gets a uniquely-named workspace — without this,
    repeated runs in space mode accumulate workspaces with identical labels."""
    existing = {
        w.get("label")
        for w in request("workspace.list", {}).get("workspaces", [])
    }
    if base_label not in existing:
        return base_label
    suffix = 2
    while f"{base_label}-{suffix}" in existing:
        suffix += 1
    return f"{base_label}-{suffix}"


def build_label(request, slug, mode="tab", target_workspace_id=None,
                target_tab_id=None, env=None):
    """Compose a deterministic label per spawn mode and apply collision suffixing.

    pane  -> label = <slug>; collision-scoped to panes within target_tab_id.
             The caller's tab/space already provide context to the human, so the
             pane label stays short. Applied via pane.rename after agent.start.
    tab   -> label = <caller-space>-<caller-tab>-<slug>; collision-scoped to tabs
             within target_workspace_id. Applied at tab.create time.
    space -> workspace label = <caller-tab-name>; inner tab label = <slug>.
             The workspace label already carries caller context, so prepending it
             again to the inner tab would be redundant noise.
    """
    if mode not in MODES:
        raise NamingError(f"unknown mode: {mode}; expected one of {MODES}")
    slug = validate_slug(slug)
    ctx = caller_context(request, env)
    ctx.update({"slug": slug, "mode": mode})
    if mode == "pane":
        tab_id = target_tab_id or ctx["tab_id"]
        base = slug
        label = pane_collision_free(request, tab_id, base)
        ctx.update({
            "target_tab_id": tab_id,
            "target_workspace_id": ctx["workspace_id"],
            "base_label": base,
            "label": label,
        })
    elif mode == "tab":
        workspace_id = target_workspace_id or ctx["workspace_id"]
        base = f"{ctx['space_name']}-{ctx['tab_name']}-{slug}"
        label = collision_free_label(request, workspace_id, base)
        ctx.update({
            "target_workspace_id": workspace_id,
            "base_label": base,
            "label": label,
        })
    else:  # space
        base_workspace_label = ctx["tab_name"]
        workspace_label = collision_free_workspace_label(request, base_workspace_label)
        inner_label = slug
        ctx.update({
            "workspace_label": workspace_label,
            "base_label": inner_label,
            "label": inner_label,
        })
    return ctx


def main():
    parser = argparse.ArgumentParser(description="Build a HERDR tab/pane/workspace label from caller context.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--mode", choices=MODES, default="tab",
                        help="Spawn target shape; controls label composition and collision scope.")
    parser.add_argument("--workspace", default=None, help="Target workspace for tab-mode collision checks.")
    parser.add_argument("--tab", default=None, help="Target tab for pane-mode collision checks.")
    parser.add_argument("--socket", default=os.path.expanduser("~/.config/herdr/herdr.sock"))
    args = parser.parse_args()
    try:
        result = build_label(lambda m, p: rpc(args.socket, m, p), args.slug,
                             mode=args.mode, target_workspace_id=args.workspace,
                             target_tab_id=args.tab)
    except NamingError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
