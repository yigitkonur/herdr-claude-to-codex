#!/usr/bin/env python3
"""await_done.py — wait for an agent's turn to end, then CLASSIFY why.

The core lesson of herdr orchestration: `idle`/`done` does NOT mean "task
complete." It means "the agent's turn ended and it is waiting for your input."
Empirically that covers four very different situations:
  - the task is actually finished        (a completion marker is on screen)
  - the agent asked you a question        (screen ends in a question)
  - the agent presented a choice menu     (e.g. plan-mode "Implement this plan?")
  - the agent hit a tool-permission gate  (status is `blocked`, not idle)

This script waits for the pane to settle (idle / done / blocked), reads the
screen, and returns a single JSON verdict so the calling agent knows whether to
move on, answer a question, pick a menu option, or approve a permission prompt —
WITHOUT having to eyeball raw scrollback.

It is hybrid like `herdr agent wait`: if the pane is already settled, it returns
at once; otherwise it subscribes and waits for the next settling transition.

Usage:
  python3 scripts/await_done.py <pane_id> --marker BUILD_COMPLETE --timeout 600
  python3 scripts/await_done.py <pane_id>            # no marker; classify heuristically
  python3 scripts/await_done.py --help

Run via your Bash tool with run_in_background: true. When the agent settles, the
script exits and you are notified; read its stdout for the verdict.

Stdout (JSON):
  {
    "outcome": "complete" | "waiting_question" | "waiting_choice"
             | "blocked" | "idle_unclassified" | "timeout",
    "status": "done" | "idle" | "blocked" | ...,   # raw API status
    "pane_id": "...",
    "marker": "BUILD_COMPLETE" | null,
    "marker_found": true | false,
    "tail": "last ~25 visible lines, ANSI-stripped"
  }

Exit codes:
  0  settled and classified (any outcome except timeout)
  1  timed out before the pane settled
  2  invalid arguments / socket not found / pane not found
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
DEFAULT_TIMEOUT = 600          # 10 min: comfortably covers a multi-file edit turn
SETTLED = {"idle", "done", "blocked"}   # statuses that mean "turn ended, your move"
TAIL_LINES = 25                # enough to see a question or a menu without flooding context
# The status event can arrive a few hundred ms BEFORE the TUI finishes painting
# a choice menu. Pause briefly after settling so the screen we read is complete.
SETTLE_DELAY = 0.8
# A long plan/menu can paint >1s after the idle event. If the first read is
# inconclusive (idle_unclassified), re-read a few times to let it finish.
RECHECK_TRIES = 4
RECHECK_DELAY = 1.0
# A numbered menu line looks like "1. ...", "› 1. ...", "2) ..." — codex/claude prompts use these.
MENU_RE = re.compile(r"^\s*[›>]?\s*\d+[.)]\s+\S", re.MULTILINE)


def _rpc(sock_path, method, params):
    """One-shot JSON request/response over the herdr socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"id": "x", "method": method, "params": params}) + "\n").encode())
    buf = b""
    s.settimeout(10)
    while not buf.endswith(b"\n"):
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def _current_status(sock_path, pane_id):
    """Return the pane's current API status, or None if pane is gone."""
    resp = _rpc(sock_path, "pane.get", {"pane_id": pane_id})
    if "error" in resp:
        return None
    return resp["result"]["pane"].get("agent_status")


def _read_tail(sock_path, pane_id, lines):
    resp = _rpc(sock_path, "pane.read",
                {"pane_id": pane_id, "source": "visible", "lines": lines, "format": "text"})
    if "error" in resp:
        return ""
    # pane.read nests its payload under result.read (NOT result directly).
    return resp.get("result", {}).get("read", {}).get("text", "")


def _marker_on_own_line(marker, tail):
    """True only if the marker appears as a STANDALONE line (the agent's output) —
    NOT as a substring of the echoed prompt. The prompt always contains the marker
    (you told the agent 'print exactly: MARKER'), so a bare `marker in tail` check
    false-matches the prompt echo. The agent prints the marker on its own line,
    optionally behind a TUI bullet (•/›) — match that, and reject mid-sentence hits."""
    if not marker:
        return False
    for ln in tail.splitlines():
        cleaned = ln.strip().lstrip("•›>*-").strip()
        if cleaned == marker:
            return True
    return False


def _classify(status, tail, marker):
    """Map (status, screen tail) -> an actionable outcome.

    ORDER MATTERS. We decide 'is the agent waiting on me RIGHT NOW' before we
    trust a completion marker, because the marker is echoed in the prompt above
    and would otherwise win even while the agent is asking a question."""
    if status == "blocked":
        return "blocked"
    # Look only at the BOTTOM of the screen — the agent's current prompt to you —
    # so an old numbered list or question higher in scrollback can't false-match.
    nonempty = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    bottom = "\n".join(nonempty[-8:])
    # A numbered menu at the bottom (plan approval, multiple-choice questions, etc.).
    if MENU_RE.search(bottom):
        return "waiting_choice"
    # A trailing question in the last few bottom lines => the agent asked something.
    if any(ln.endswith("?") for ln in nonempty[-6:]):
        return "waiting_question"
    # Only now, with no pending question/menu, trust a STANDALONE marker line.
    if _marker_on_own_line(marker, tail):
        return "complete"
    return "idle_unclassified"


def _wait_for_settle(sock_path, pane_id, timeout):
    """Subscribe and block until the pane reaches a SETTLED status, or timeout.
    Returns the settled status string, or None on timeout."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    req = {"id": "w", "method": "events.subscribe",
           "params": {"subscriptions": [
               {"type": "pane.agent_status_changed", "pane_id": pane_id}]}}
    s.sendall((json.dumps(req) + "\n").encode())
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
        description="Wait for an agent's turn to end, then classify the outcome.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("pane_id", help="Pane id of the agent (long w...-N or short p_X).")
    p.add_argument("--marker", default=None,
                   help="Completion string the agent was told to print (e.g. BUILD_COMPLETE). "
                        "If present on screen, outcome is 'complete'.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"Seconds to wait for the turn to end (default {DEFAULT_TIMEOUT}).")
    p.add_argument("--tail-lines", type=int, default=TAIL_LINES,
                   help=f"How many visible lines to include in the verdict (default {TAIL_LINES}).")
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    args = p.parse_args()

    if not os.path.exists(args.socket):
        print(f"Error: herdr socket not found at {args.socket}. Is the server running?",
              file=sys.stderr)
        return 2

    # Hybrid step 1: is the pane already settled right now?
    status = _current_status(args.socket, args.pane_id)
    if status is None:
        print(f"Error: pane {args.pane_id} not found.", file=sys.stderr)
        return 2

    if status not in SETTLED:
        # Hybrid step 2: block until it settles.
        status = _wait_for_settle(args.socket, args.pane_id, args.timeout)
        if status is None:
            tail = _read_tail(args.socket, args.pane_id, args.tail_lines)
            print(json.dumps({
                "outcome": "timeout", "status": None, "pane_id": args.pane_id,
                "marker": args.marker, "marker_found": False, "tail": tail.strip(),
            }, indent=2))
            return 1

    # Let the TUI finish painting (menus/questions can lag the status event).
    # A long plan can take >1s to render AFTER the status went idle, so if the
    # first read is inconclusive, re-read a few times before giving up — the
    # menu/marker/question may still be painting. Definite outcomes return at once.
    time.sleep(SETTLE_DELAY)
    tail = _read_tail(args.socket, args.pane_id, args.tail_lines)
    outcome = _classify(status, tail, args.marker)
    for _ in range(RECHECK_TRIES):
        if outcome != "idle_unclassified":
            break
        time.sleep(RECHECK_DELAY)
        tail = _read_tail(args.socket, args.pane_id, args.tail_lines)
        outcome = _classify(status, tail, args.marker)
    print(json.dumps({
        "outcome": outcome,
        "status": status,
        "pane_id": args.pane_id,
        "marker": args.marker,
        # Standalone-line match (real agent output), NOT a bare substring — the
        # prompt echo always contains the marker and would mislead otherwise.
        "marker_found": _marker_on_own_line(args.marker, tail),
        "tail": tail.strip(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
