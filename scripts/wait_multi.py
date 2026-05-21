#!/usr/bin/env python3
"""wait_multi.py — wait for ANY or ALL of several panes to reach a status.

Background `herdr agent wait` is perfect for one sub-agent, but when you run a
fleet you want a single signal: "tell me when the FIRST one finishes" (so you
can react immediately) or "tell me when they're ALL done" (a join/barrier).
This script does that over ONE event socket — which matters, because herdr
silently breaks if you open two subscriptions on the same socket. It packs
every pane into one subscription request and tracks them together.

It is hybrid: panes already at the target status when the script starts count
immediately (so you never miss a fast finisher).

Usage:
  python3 scripts/wait_multi.py --mode any  --status idle PANE_A PANE_B PANE_C
  python3 scripts/wait_multi.py --mode all  --status idle --timeout 900 P1 P2
  python3 scripts/wait_multi.py --help

Note on `idle`: the status value reported in events is the API form, so `done`
(= idle + unseen) is what a finished agent emits. This script treats `--status
idle` as "settled" and matches BOTH `idle` and `done`, mirroring `herdr agent
wait --status idle`. For `--status blocked` it matches `blocked` exactly.

Run via your Bash tool with run_in_background: true; you'll be notified when the
ANY/ALL condition is met (or on timeout).

Stdout (JSON):
  --mode any : {"mode":"any","done":["PANE_A"],"first":"PANE_A","status":"done","pending":["PANE_B"]}
  --mode all : {"mode":"all","done":["P1","P2"],"pending":[],"complete":true}
  on timeout : {... ,"timeout":true,"pending":[...]}

Exit codes:
  0  condition met (any finisher / all finished)
  1  timed out before the condition was met
  2  invalid arguments / socket not found
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
DEFAULT_TIMEOUT = 900   # 15 min: a fleet of agents on real tasks can run a while


def _matches(target, status):
    """`idle` target matches idle OR done (done = idle+unseen). Others match exactly."""
    if status is None:
        return False
    if target == "idle":
        return status in ("idle", "done")
    return status == target


def _current_status(sock_path, pane_id):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((json.dumps({"id": "g", "method": "pane.get",
                           "params": {"pane_id": pane_id}}) + "\n").encode())
    buf = b""
    s.settimeout(10)
    while not buf.endswith(b"\n"):
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
    s.close()
    d = json.loads(buf.decode())
    if "error" in d:
        return None
    return d["result"]["pane"].get("agent_status")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Wait for ANY or ALL of several panes to reach a status.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("pane_ids", nargs="+", help="Pane ids to watch.")
    p.add_argument("--mode", choices=["any", "all"], default="any",
                   help="any = return on the first finisher; all = wait for everyone.")
    p.add_argument("--status", default="idle",
                   help="Target status (default idle; idle also matches done).")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"Seconds before giving up (default {DEFAULT_TIMEOUT}).")
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    args = p.parse_args()

    if not os.path.exists(args.socket):
        print(f"Error: herdr socket not found at {args.socket}. Is the server running?",
              file=sys.stderr)
        return 2

    pending = set(args.pane_ids)
    done = []

    # Hybrid: count panes already at the target before we even subscribe.
    for pid in list(pending):
        st = _current_status(args.socket, pid)
        if st is None:
            print(f"Error: pane {pid} not found.", file=sys.stderr)
            return 2
        if _matches(args.status, st):
            done.append(pid)
            pending.discard(pid)

    def _result(timeout=False):
        obj = {"mode": args.mode, "done": done, "pending": sorted(pending)}
        if args.mode == "any":
            obj["first"] = done[0] if done else None
        else:
            obj["complete"] = (len(pending) == 0)
        if timeout:
            obj["timeout"] = True
        return obj

    condition_met = (done and args.mode == "any") or (not pending and args.mode == "all")
    if condition_met:
        print(json.dumps(_result(), indent=2))
        return 0

    # One subscription, all panes — never open a second subscribe on this socket.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(args.socket)
    subs = [{"type": "pane.agent_status_changed", "pane_id": pid} for pid in args.pane_ids]
    s.sendall((json.dumps({"id": "w", "method": "events.subscribe",
                           "params": {"subscriptions": subs}}) + "\n").encode())
    s.settimeout(args.timeout)
    deadline = time.time() + args.timeout
    buf = b""
    try:
        while time.time() < deadline:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                d = json.loads(line.decode())
                da = d.get("data", {})
                pid = da.get("pane_id")
                st = da.get("agent_status")
                if pid in pending and _matches(args.status, st):
                    pending.discard(pid)
                    if pid not in done:
                        done.append(pid)
                    if args.mode == "any":
                        obj = _result()
                        obj["status"] = st
                        print(json.dumps(obj, indent=2))
                        return 0
                    if not pending:  # mode all
                        print(json.dumps(_result(), indent=2))
                        return 0
    finally:
        s.close()

    print(json.dumps(_result(timeout=True), indent=2))
    return 1


if __name__ == "__main__":
    sys.exit(main())
