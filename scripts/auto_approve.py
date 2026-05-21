#!/usr/bin/env python3
"""auto_approve.py — watch a pane for approval prompts and answer them by rule.

When a sub-agent stops at a decision point — a tool-permission gate (status
`blocked`) or a choice menu like plan-mode's "Implement this plan?" (status
`done` with a numbered menu) — this script reads the prompt, matches it against
your allow/deny rules, and sends the right key. It is the unattended-approver
you reach for when an agent will hit several prompts during a long run and you
don't want to babysit each one.

SAFETY: the default action for an UNMATCHED prompt is `escalate` — the script
sends NOTHING and exits with the prompt text so YOU decide. It only presses keys
when one of your explicit --allow / --deny rules matches. Nothing is auto-
approved unless you said so.

Usage:
  # Approve any prompt that mentions writing into /tmp; escalate everything else.
  python3 scripts/auto_approve.py PANE --allow 'tmp/|\.html' --default escalate

  # Approve plan menus, deny anything mentioning rm/sudo/delete, loop until idle.
  python3 scripts/auto_approve.py PANE \
      --allow 'Implement this plan' --deny 'rm |sudo|delete|drop table' \
      --default escalate --loop --timeout 600

  python3 scripts/auto_approve.py --help

Decision order for each prompt: first matching --deny wins, then first matching
--allow, then --default.
  allow  -> send the approve key  (default: Enter; option 1 / "Yes")
  deny   -> send the decline key  (default: Esc; cancels in most TUIs)
  escalate -> send nothing; print the prompt and stop so a human/you can act

Stdout (JSON), one decision object per handled prompt (and a final summary):
  {"event":"decision","action":"allow","matched":"Implement this plan","sent":["Enter"],"tail":"..."}
  {"event":"summary","handled":2,"stopped_reason":"idle"|"timeout"|"escalated"}

Exit codes:
  0  finished cleanly (agent reached a non-prompt idle, or one prompt handled in one-shot mode)
  1  timed out
  2  invalid arguments / socket not found / pane gone
  3  escalated: an unmatched prompt needs a human (prompt text on stdout)
"""
import argparse
import json
import os
import re
import socket
import sys
import time

DEFAULT_SOCKET = os.environ.get(
    "HERDR_SOCKET_PATH",
    os.path.expanduser("~/.config/herdr/herdr.sock"),
)
DEFAULT_TIMEOUT = 600
SETTLED = {"idle", "done", "blocked"}
MENU_RE = re.compile(r"^\s*[›>]?\s*\d+[.)]\s+\S", re.MULTILINE)
TAIL_LINES = 30
# Status events can precede the menu render by a few hundred ms; pause before reading.
SETTLE_DELAY = 0.8


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


def _status(sock_path, pid):
    g = _rpc(sock_path, "pane.get", {"pane_id": pid})
    return None if "error" in g else g["result"]["pane"].get("agent_status")


def _tail(sock_path, pid, lines):
    r = _rpc(sock_path, "pane.read",
             {"pane_id": pid, "source": "visible", "lines": lines, "format": "text"})
    if "error" in r:
        return ""
    return r.get("result", {}).get("read", {}).get("text", "")


def _send_keys(sock_path, pid, keys):
    _rpc(sock_path, "pane.send_keys", {"pane_id": pid, "keys": keys})


def _is_prompt(status, tail):
    """A prompt = blocked, or a settled screen showing a numbered choice menu."""
    if status == "blocked":
        return True
    if status in ("idle", "done") and MENU_RE.search(tail):
        return True
    return False


def _wait_settle(sock_path, pid, timeout):
    st = _status(sock_path, pid)
    if st in SETTLED:
        return st
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"id": "w", "method": "events.subscribe",
                           "params": {"subscriptions": [
                               {"type": "pane.agent_status_changed", "pane_id": pid}]}}) + "\n").encode())
    s.settimeout(timeout)
    deadline = time.time() + timeout
    buf = b""
    try:
        while time.time() < deadline:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                d = json.loads(line.decode())
                st = d.get("data", {}).get("agent_status")
                if st in SETTLED:
                    return st
    finally:
        s.close()
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Watch a pane for approval prompts and answer by rule.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("pane_id")
    p.add_argument("--allow", action="append", default=[], metavar="REGEX",
                   help="Approve prompts matching this regex (repeatable).")
    p.add_argument("--deny", action="append", default=[], metavar="REGEX",
                   help="Decline prompts matching this regex (repeatable). Checked before --allow.")
    p.add_argument("--default", choices=["allow", "deny", "escalate"], default="escalate",
                   help="Action for prompts no rule matches (default escalate = stop and ask a human).")
    p.add_argument("--approve-keys", default="Enter",
                   help="Space-separated keys to approve (default 'Enter' = option 1 / Yes).")
    p.add_argument("--decline-keys", default="Esc",
                   help="Space-separated keys to decline (default 'Esc' = cancel).")
    p.add_argument("--loop", action="store_true",
                   help="Keep handling prompts until the agent reaches a non-prompt idle.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    args = p.parse_args()

    if not os.path.exists(args.socket):
        print(f"Error: herdr socket not found at {args.socket}. Is the server running?",
              file=sys.stderr)
        return 2

    allow_res = [re.compile(r) for r in args.allow]
    deny_res = [re.compile(r) for r in args.deny]
    approve_keys = args.approve_keys.split()
    decline_keys = args.decline_keys.split()
    deadline = time.time() + args.timeout
    handled = 0

    def _decide(tail):
        for r in deny_res:
            if r.search(tail):
                return "deny", r.pattern
        for r in allow_res:
            if r.search(tail):
                return "allow", r.pattern
        return args.default, None

    while time.time() < deadline:
        st = _wait_settle(args.socket, args.pane_id, int(deadline - time.time()))
        if st is None:
            print(json.dumps({"event": "summary", "handled": handled, "stopped_reason": "timeout"}))
            return 1
        time.sleep(SETTLE_DELAY)   # let a menu finish painting before we read it
        tail = _tail(args.socket, args.pane_id, TAIL_LINES)
        if not _is_prompt(st, tail):
            # Settled but not a prompt => the agent is just done/waiting on content.
            print(json.dumps({"event": "summary", "handled": handled,
                              "stopped_reason": "idle", "status": st, "tail": tail.strip()[-600:]}))
            return 0
        action, matched = _decide(tail)
        if action == "escalate":
            print(json.dumps({"event": "escalate", "matched": None,
                              "status": st, "tail": tail.strip()[-1200:]}, indent=2))
            return 3
        sent = approve_keys if action == "allow" else decline_keys
        _send_keys(args.socket, args.pane_id, sent)
        handled += 1
        print(json.dumps({"event": "decision", "action": action, "matched": matched,
                          "sent": sent, "tail": tail.strip()[-400:]}), flush=True)
        if not args.loop:
            print(json.dumps({"event": "summary", "handled": handled, "stopped_reason": "one-shot"}))
            return 0
        # Loop: give the agent a moment to leave the prompt before re-watching.
        time.sleep(1.0)

    print(json.dumps({"event": "summary", "handled": handled, "stopped_reason": "timeout"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
