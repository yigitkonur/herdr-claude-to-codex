#!/usr/bin/env python3
"""spawn.py — spawn a sub-agent, wait for it to register, return its ids as JSON.

Two papercuts make raw `herdr agent start` annoying to script:
  1. The new pane id is at result.agent.pane_id (NOT result.pane.pane_id — that
     shape is `pane split`'s). Easy to fish out of the wrong key.
  2. The integration hook needs a few seconds to register the agent. Query too
     soon and `agent get` returns agent_not_found.
This wrapper does both correctly and hands you a clean, stable record.

Usage:
  python3 scripts/spawn.py --label codex-worker --split right -- codex
  python3 scripts/spawn.py --label helper --split down --cwd /path -- claude --dangerously-skip-permissions
  python3 scripts/spawn.py --help

Everything after `--` is the command run inside the new pane (the agent CLI and
its flags). The script splits a pane, launches that command, then blocks (up to
--register-timeout) until the agent reports itself idle.

Stdout (JSON):
  {"pane_id":"w...-3","terminal_id":"term_...","agent":"codex","label":"codex-worker","registered":true}
If registration times out, "registered" is false but the pane_id is still valid
(the agent may simply be slow; you can wait on it yourself).

Exit codes:
  0  spawned and registered
  1  spawned but registration timed out (pane_id still returned)
  2  invalid arguments / socket not found / spawn failed
"""
import argparse
import json
import os
import socket
import sys
import time

DEFAULT_SOCKET = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)
REGISTER_TIMEOUT = 20   # integration hooks register within ~3-5s; 20s is a safe ceiling


def _rpc(sock_path, method, params, timeout=10):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"id": "x", "method": method, "params": params}) + "\n").encode())
    buf = b""
    s.settimeout(timeout)
    while not buf.endswith(b"\n"):
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def main() -> int:
    # Manual split on `--` so the agent command can carry its own flags.
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--" not in argv:
        print("Error: missing `--`. Usage: spawn.py --label X --split right -- <agent cli...>",
              file=sys.stderr)
        return 2
    sep = argv.index("--")
    own_args, agent_cmd = argv[:sep], argv[sep + 1:]
    if not agent_cmd:
        print("Error: no agent command after `--`.", file=sys.stderr)
        return 2

    p = argparse.ArgumentParser(
        description="Spawn a sub-agent and return its ids.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--label", required=True, help="A unique label (avoid reserved type names).")
    p.add_argument("--split", choices=["right", "down"], default="right")
    p.add_argument("--cwd", default=None, help="Working directory for the new pane.")
    p.add_argument("--register-timeout", type=int, default=REGISTER_TIMEOUT)
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    args = p.parse_args(own_args)

    if not os.path.exists(args.socket):
        print(f"Error: herdr socket not found at {args.socket}. Is the server running?",
              file=sys.stderr)
        return 2

    params = {"name": args.label, "split": args.split, "focus": False, "argv": agent_cmd}
    if args.cwd:
        params["cwd"] = args.cwd
    resp = _rpc(args.socket, "agent.start", params)
    if "error" in resp:
        print(f"Error: agent.start failed: {resp['error']}", file=sys.stderr)
        return 2

    # agent.start returns the pane under result.agent (NOT result.pane).
    ag = resp["result"]["agent"]
    pane_id = ag["pane_id"]
    rec = {
        "pane_id": pane_id,
        "terminal_id": ag.get("terminal_id"),
        "agent": ag.get("agent"),
        "label": args.label,
        "registered": False,
    }

    # Wait for the integration hook to register (poll current status; cheap and robust).
    deadline = time.time() + args.register_timeout
    while time.time() < deadline:
        g = _rpc(args.socket, "pane.get", {"pane_id": pane_id})
        if "result" in g:
            pane = g["result"]["pane"]
            if pane.get("agent"):  # agent field populated => hook fired
                rec["agent"] = pane.get("agent")
                rec["registered"] = True
                break
        time.sleep(0.5)

    print(json.dumps(rec, indent=2))
    return 0 if rec["registered"] else 1


if __name__ == "__main__":
    sys.exit(main())
