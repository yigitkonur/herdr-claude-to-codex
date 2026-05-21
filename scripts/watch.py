#!/usr/bin/env python3
"""watch.py — stream herdr agent-status + lifecycle events for one or more panes.

Subscribes to the herdr event socket and prints each event with a relative
timestamp. Use it to SEE what an agent is doing in real time, or to capture a
timeline while debugging an orchestration flow. Stdlib only; no dependencies.

Usage:
  python3 scripts/watch.py <pane_id> [pane_id ...]
  python3 scripts/watch.py --timeout 300 --json w6522ea4d2775bf-2
  python3 scripts/watch.py --help

Output (default, human): one line per event, e.g.
  3.142s  status  pane=w6522ea4d2775bf-2  agent=codex  working
  9.871s  status  pane=w6522ea4d2775bf-2  agent=codex  done
Output (--json): one JSON object per line (newline-delimited), e.g.
  {"t": 3.142, "kind": "status", "pane_id": "...", "agent": "codex", "status": "working"}

Run with your Bash tool and run_in_background: true to get a notification when
it exits (on --timeout). Read its output file for the captured timeline.

Exit codes:
  0  clean exit (timeout reached or socket closed)
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
# 300 s default keeps a watch alive across a typical multi-minute agent task
# without lingering forever if the caller forgets to stop it.
DEFAULT_TIMEOUT = 300


def main() -> int:
    p = argparse.ArgumentParser(
        description="Stream herdr agent-status and pane-lifecycle events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  python3 scripts/watch.py w6522ea4d2775bf-2\n"
        "  python3 scripts/watch.py --json --timeout 600 PANE_A PANE_B\n",
    )
    p.add_argument("pane_ids", nargs="+", help="One or more pane ids to watch.")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"Seconds to watch before exiting (default {DEFAULT_TIMEOUT}).")
    p.add_argument("--json", action="store_true",
                   help="Emit one JSON object per line instead of human text.")
    p.add_argument("--socket", default=DEFAULT_SOCKET,
                   help="Path to the herdr API socket.")
    args = p.parse_args()

    if not os.path.exists(args.socket):
        print(f"Error: herdr socket not found at {args.socket}. "
              f"Is the server running? Try `herdr status`.", file=sys.stderr)
        return 2

    subs = [{"type": "pane.agent_status_changed", "pane_id": pid} for pid in args.pane_ids]
    # Lifecycle events are global (no filter) — they tell you when a pane dies.
    subs.append({"type": "pane.exited"})
    subs.append({"type": "pane.closed"})

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(args.socket)
    except OSError as e:
        print(f"Error: cannot connect to {args.socket}: {e}", file=sys.stderr)
        return 2

    req = {"id": "watch", "method": "events.subscribe", "params": {"subscriptions": subs}}
    sock.sendall((json.dumps(req) + "\n").encode())

    t0 = time.time()
    sock.settimeout(args.timeout)
    buf = b""
    watched = set(args.pane_ids)
    try:
        while time.time() - t0 < args.timeout:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    d = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                t = round(time.time() - t0, 3)
                if "result" in d:
                    _emit(args.json, {"t": t, "kind": "ack",
                                      "ack": d["result"].get("type")})
                elif "error" in d:
                    _emit(args.json, {"t": t, "kind": "error", "error": d["error"]})
                elif "data" in d:
                    da = d["data"]
                    pid = da.get("pane_id", "")
                    ev = d.get("event", "")
                    # Lifecycle events fire for ALL panes; only surface ours.
                    if ev in ("pane.exited", "pane.closed") and pid not in watched:
                        continue
                    _emit(args.json, {
                        "t": t,
                        "kind": "status" if "status" in ev else ev.split(".")[-1],
                        "event": ev,
                        "pane_id": pid,
                        "agent": da.get("agent"),
                        "status": da.get("agent_status"),
                    })
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


def _emit(as_json: bool, obj: dict) -> None:
    if as_json:
        print(json.dumps(obj), flush=True)
        return
    t = f"{obj.get('t', 0):7.3f}s"
    kind = obj.get("kind", "")
    if kind == "ack":
        print(f"{t}  ack     {obj.get('ack')}", flush=True)
    elif kind == "error":
        print(f"{t}  error   {obj.get('error')}", flush=True)
    else:
        pid = obj.get("pane_id", "")
        ag = obj.get("agent") or "-"
        st = obj.get("status") or ""
        ev = obj.get("event", "")
        print(f"{t}  {kind:7s} pane={pid} agent={ag} {st}{'  '+ev if not st else ''}",
              flush=True)


if __name__ == "__main__":
    sys.exit(main())
