#!/usr/bin/env python3
"""codex.py — drive a Codex sub-agent from Claude Code through ONE interface.

This is the only orchestration tool the skill exposes. It spawns Codex in a
herdr side-pane, sends work, waits for the turn to end, and returns a STRUCTURED
VERDICT — not a bare status — so you (the orchestrator) always know what
happened and what to do next. The operational complexity (pane lifecycle,
status interpretation, the idle≠done trap, plan menus, the blocked
multiple-choice widget, the pane-slot renumbering trap, screen-render lag) lives
in Python, not in your head.

USE FROM CLAUDE CODE: run every blocking verb (start/send/reply/await) via your
Bash tool with run_in_background: true. The command blocks until Codex's turn
ends, prints one JSON envelope, and exits — and Claude Code auto-notifies you
with that JSON. You do not poll.

VERBS
  start   --task "<p>" [--plan] [--expect PATH]... [--cwd DIR] [--label NAME]
          [--marker STR] [--timeout SEC] [--no-wait]
              Spawn Codex, send the task (with an auto-injected completion
              marker + "ask me if unsure" discipline), wait, and return a
              verdict + a new session id.
  send    --session <id> --message "<p>" [--expect PATH]... [--timeout SEC]
              Continue a live session: send a follow-up, wait, return a verdict.
  reply   --session <id> (--text "<answer>" | --choice N | --approve | --reject)
          [--expect PATH]... [--timeout SEC]
              Respond to an awaiting state (answer a question, pick an option,
              approve/reject a plan), then wait and return a verdict.
  await   --session <id> [--expect PATH]... [--timeout SEC]
              Re-enter the wait without sending anything (e.g. after you acted).
  status  --session <id> [--expect PATH]...
              One-shot snapshot, no waiting. Returns a verdict for the current
              screen (state=working if Codex is mid-turn).
  end     --session <id>
              Graceful cleanup: close the pane, release the agent registry,
              delete session state.
  sessions
              List live sessions; prune dead ones.

OUTPUT: one JSON envelope on stdout (see README of the skill / scripting-
patterns.md). Diagnostics go to stderr. Read result.state and do what
result.next_action.command says.

EXIT CODES
  0  a valid verdict was produced (any state, including awaiting/working/timeout)
  2  usage error (bad/missing arguments)
  3  herdr environment error (server down / socket missing)
  4  session or pane not found / session dead
  5  internal error
"""
import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core  # noqa: E402

SELF = os.path.abspath(__file__)
SCHEMA = "v1"


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------
def _emit(command, ok=True, session=None, result=None, error=None):
    print(json.dumps({
        "ok": ok, "schema_version": SCHEMA, "command": command,
        "session": session, "result": result, "error": error,
    }, indent=2))


def _fail(command, klass, code, message, retryable, suggestion, session=None, exit_code=2):
    _emit(command, ok=False, session=session, result=None, error={
        "class": klass, "code": code, "message": message,
        "retryable": retryable, "suggestion": suggestion,
    })
    return exit_code


def _marker_for(args):
    return args.marker or f"CDX_DONE_{uuid.uuid4().hex[:6].upper()}"


def _oneline(s):
    """Collapse to a single line. Codex's composer treats an injected newline as
    submit-or-multiline, which can strand the rest of the prompt unsent; a chat
    message never needs literal newlines, so we always submit one clean line."""
    return " ".join((s or "").split())


def _wrap_task(task, marker):
    """Inject the completion marker + clarify-don't-guess discipline. This is the
    'as few parameters as possible' promise: the agent gives a plain task; Python
    adds the machine-readable completion contract. Single line by construction."""
    return _oneline(
        f"{task} "
        f"When the task is FULLY complete, print this token on its own line: {marker}. "
        f"If you need information or a decision from me, end your turn by asking "
        f"your question(s) instead of guessing."
    )


def _with_marker_reminder(text, marker):
    """Re-attach the completion marker to a follow-up message. After a multi-turn
    answer Codex inconsistently re-prints the marker, which (without --expect)
    degrades a real completion to no_signal. A terse standing reminder keeps the
    next completion deterministically marker_verified."""
    if not marker:
        return _oneline(text)
    return _oneline(f"{text} (When the task is FULLY complete, print {marker} on its own line.)")


def _verdict(command, session_id, pane_id, marker, expect, timeout):
    result, _ = _core.settle_and_analyze(
        pane_id, marker, expect, session_id, timeout, SELF)
    # Persist last state + plan (plans carry continuity and must survive).
    rec = _core.load_session(session_id)
    if rec is not None:
        rec["last_state"] = result.get("state")
        if result.get("plan"):
            rec["plan"] = result["plan"]
        _core.save_session(rec)
    _emit(command, ok=True, session=session_id, result=result)
    return 0


def _resolve(command, session_id):
    """Return (rec, live_pane_id) or raise via a returned (None, exit_code)."""
    rec = _core.load_session(session_id)
    if rec is None:
        return None, _fail(command, "not_found", "NO_SESSION",
                           f"No session '{session_id}'.", False,
                           "List sessions with `codex.py sessions`, or start one with `codex.py start`.",
                           session=session_id, exit_code=4)
    pane_id = _core.resolve_pane_id(rec)
    if pane_id is None:
        return None, _fail(command, "not_found", "SESSION_DEAD",
                           f"Session '{session_id}' pane is gone (Codex exited or was closed).",
                           False, "Start a fresh task with `codex.py start`.",
                           session=session_id, exit_code=4)
    return (rec, pane_id), 0


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------
def cmd_start(args):
    marker = _marker_for(args)
    label = args.label or ("cdx-" + uuid.uuid4().hex[:4])
    session_id = label if label.startswith("cdx-") else ("cdx-" + uuid.uuid4().hex[:4])
    info = _core.spawn_codex(label, cwd=args.cwd)
    rec = {"session": session_id, "label": label, "terminal_id": info["terminal_id"],
           "pane_id": info["pane_id"], "tab_id": info.get("tab_id"), "agent": info["agent"],
           "marker": marker, "plan_mode": bool(args.plan), "created": time.time(),
           "last_state": "spawned", "plan": None}
    _core.save_session(rec)
    pane_id = info["pane_id"]

    if args.plan:
        _core.send_text_enter(pane_id, "/plan")
        # Wait for plan mode to actually engage (status bar shows "Plan mode")
        # rather than a blind sleep, so the task lands in plan mode.
        deadline = time.time() + 8
        while time.time() < deadline:
            if "plan mode" in _core.read_screen(pane_id, 14).lower():
                break
            time.sleep(0.5)

    # First send is race-prone right after spawn; verify it landed (re-send if eaten).
    _core.send_task_verified(pane_id, _wrap_task(args.task, marker))
    if args.no_wait:
        _emit("start", ok=True, session=session_id, result={
            "state": "working", "reason": "no_wait", "summary": "Task sent; not waiting.",
            "plan": None, "questions": [], "options": [], "marker_found": False,
            "artifacts": [], "transcript_tail": "",
            "next_action": {"intent": "wait",
                            "command": f"python3 {SELF} await --session {session_id}",
                            "why": "Re-enter the wait when ready."}})
        return 0
    return _verdict("start", session_id, pane_id, marker, args.expect, args.timeout)


def cmd_send(args):
    resolved, code = _resolve("send", args.session)
    if resolved is None:
        return code
    rec, pane_id = resolved
    _core.send_task_verified(pane_id, _with_marker_reminder(args.message, rec.get("marker")))
    return _verdict("send", args.session, pane_id, rec.get("marker"), args.expect, args.timeout)


def cmd_reply(args):
    # Validate arguments BEFORE resolving the session, so a usage error is always
    # exit 2 (not masked as exit 4 when the session also happens to be missing).
    modes = [bool(args.text), args.choice is not None, args.approve, args.reject]
    if sum(modes) != 1:
        return _fail("reply", "usage", "BAD_REPLY",
                     "Provide exactly one of --text, --choice N, --approve, --reject.",
                     False, 'e.g. codex.py reply --session ID --choice 1',
                     session=args.session, exit_code=2)
    resolved, code = _resolve("reply", args.session)
    if resolved is None:
        return code
    rec, pane_id = resolved
    if args.text:
        _core.send_task_verified(pane_id, _with_marker_reminder(args.text, rec.get("marker")))
    elif args.approve:
        _core.send_keys(pane_id, ["Enter"])           # option 1 (the ›-selected default)
        _core.await_started(pane_id)                  # ride past the Plan->Default redraw
    elif args.reject:
        _core.send_keys(pane_id, ["Esc"])             # cancel / back out of the menu
    else:  # --choice N: move down to the Nth option (default selection is 1), then submit
        n = max(1, args.choice)
        keys = ["Down"] * (n - 1) + ["Enter"]
        _core.send_keys(pane_id, keys)
        _core.await_started(pane_id)                  # the choice triggers more work
    return _verdict("reply", args.session, pane_id, rec.get("marker"), args.expect, args.timeout)


def cmd_await(args):
    resolved, code = _resolve("await", args.session)
    if resolved is None:
        return code
    rec, pane_id = resolved
    return _verdict("await", args.session, pane_id, rec.get("marker"), args.expect, args.timeout)


def cmd_status(args):
    resolved, code = _resolve("status", args.session)
    if resolved is None:
        return code
    rec, pane_id = resolved
    st = _core.current_status(pane_id)
    if st is None:
        return _fail("status", "not_found", "SESSION_DEAD",
                     f"Session '{args.session}' pane is gone.", False,
                     "Start a fresh task with `codex.py start`.", session=args.session, exit_code=4)
    if st not in _core.SETTLED:
        result = {"state": "working", "reason": "working",
                  "summary": "Codex is still working.", "plan": rec.get("plan"),
                  "questions": [], "options": [], "marker_found": False, "artifacts": [],
                  "transcript_tail": _core._clean_tail(_core.read_tail(pane_id)) or "",
                  "next_action": {"intent": "wait",
                                  "command": f"python3 {SELF} await --session {args.session}",
                                  "why": "Re-enter the wait."}}
    else:
        time.sleep(_core.SETTLE_DELAY)
        result = _core.analyze(st, _core.read_tail(pane_id), rec.get("marker"),
                               args.expect, args.session, SELF)
    _emit("status", ok=True, session=args.session, result=result)
    return 0


def cmd_end(args):
    rec = _core.load_session(args.session)
    if rec is None:
        return _fail("end", "not_found", "NO_SESSION", f"No session '{args.session}'.",
                     False, "Nothing to clean up.", session=args.session, exit_code=4)
    pane_id = _core.resolve_pane_id(rec)
    closed = False
    if pane_id is not None:
        try:
            _core.release_agent(pane_id)
        except _core.HerdrError:
            pass
        try:
            # Closing the (sole) codex pane auto-closes its dedicated tab — verified.
            # We deliberately do NOT close_tab by stored id: tab ids renumber when a
            # lower tab closes, so a stale tab_id can close a SIBLING session's tab.
            _core.close_pane(pane_id)
            closed = True
        except _core.HerdrError:
            pass
    _core.delete_session(args.session)
    _emit("end", ok=True, session=args.session, result={
        "state": "ended", "reason": "cleaned_up",
        "summary": f"Session ended; pane {'closed' if closed else 'already gone'}, state deleted.",
        "plan": None, "questions": [], "options": [], "marker_found": False, "artifacts": [],
        "transcript_tail": "", "next_action": {"intent": "nothing", "command": None, "why": ""}})
    return 0


def cmd_sessions(args):
    panes = {p.get("terminal_id"): p for p in _core.list_panes()}
    live, pruned = [], []
    for rec in _core.all_sessions():
        p = panes.get(rec.get("terminal_id"))
        if p is None:
            _core.delete_session(rec["session"])
            pruned.append(rec["session"])
        else:
            live.append({"session": rec["session"], "label": rec.get("label"),
                         "pane_id": p["pane_id"], "agent": p.get("agent"),
                         "agent_status": p.get("agent_status"),
                         "last_state": rec.get("last_state")})
    _emit("sessions", ok=True, result={"live": live, "pruned": pruned})
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="codex.py",
        description="Drive a Codex sub-agent from Claude Code through one interface.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_wait_flags(sp):
        sp.add_argument("--expect", action="append", default=[], metavar="PATH",
                        help="Artifact to verify exists on completion (repeatable).")
        sp.add_argument("--timeout", type=int, default=600, help="Wait timeout, seconds.")

    s = sub.add_parser("start", help="Spawn Codex and send the first task.")
    s.add_argument("--task", required=True, help="What Codex should do (plain language).")
    s.add_argument("--plan", action="store_true", help="Enter plan mode (/plan) before the task.")
    s.add_argument("--cwd", default=None, help="Working directory for the pane.")
    s.add_argument("--label", default=None, help="Session label (auto if omitted).")
    s.add_argument("--marker", default=None, help="Completion marker (auto-generated if omitted).")
    s.add_argument("--no-wait", action="store_true", help="Send the task but don't wait.")
    add_wait_flags(s)
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("send", help="Send a follow-up to a live session.")
    s.add_argument("--session", required=True)
    s.add_argument("--message", required=True)
    add_wait_flags(s)
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("reply", help="Respond to an awaiting state.")
    s.add_argument("--session", required=True)
    s.add_argument("--text", default=None, help="Free-text answer to a question.")
    s.add_argument("--choice", type=int, default=None, help="Pick option N from a menu/widget.")
    s.add_argument("--approve", action="store_true", help="Approve (select option 1 / Yes).")
    s.add_argument("--reject", action="store_true", help="Reject / cancel (Esc).")
    add_wait_flags(s)
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("await", help="Re-enter the wait without sending input.")
    s.add_argument("--session", required=True)
    add_wait_flags(s)
    s.set_defaults(fn=cmd_await)

    s = sub.add_parser("status", help="One-shot snapshot, no waiting.")
    s.add_argument("--session", required=True)
    s.add_argument("--expect", action="append", default=[], metavar="PATH")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("end", help="Close the pane and clean up the session.")
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_end)

    s = sub.add_parser("sessions", help="List live sessions; prune dead ones.")
    s.set_defaults(fn=cmd_sessions)
    return p


def main():
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except _core.HerdrError as e:
        klass = "environment" if e.code == "HERDR_DOWN" else "internal"
        ec = 3 if e.code == "HERDR_DOWN" else 5
        return _fail(args.cmd, klass, e.code, e.message,
                     retryable=(e.code == "HERDR_DOWN"),
                     suggestion=("Check `herdr status`; ask the user to launch herdr if the server is down."
                                 if e.code == "HERDR_DOWN" else "Retry; if it persists, inspect the pane with `herdr pane read`."),
                     exit_code=ec)
    except Exception as e:  # never crash with a raw traceback to the agent
        return _fail(getattr(args, "cmd", "?"), "internal", "UNEXPECTED",
                     f"{type(e).__name__}: {e}", True,
                     "Retry once; if it persists, inspect the pane with `codex.py status` or `herdr pane read`.",
                     exit_code=5)


if __name__ == "__main__":
    sys.exit(main())
