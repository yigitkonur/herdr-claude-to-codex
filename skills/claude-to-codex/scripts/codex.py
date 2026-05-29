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
import contextlib
import fcntl
import json
import os
import re
import shlex
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core  # noqa: E402
import name_herdr_tab  # noqa: E402

SELF = os.path.abspath(__file__)
SCHEMA = "v1"
LOCK_PATH = os.path.join(_core.STATE_DIR, "codex.py.lock")


@contextlib.contextmanager
def _spawn_lock():
    """Serialize ONLY the spawn critical section across concurrent codex.py calls.

    Spawning does a check-then-create on labels (build_label queries tab/pane/
    workspace lists, picks a free suffix, then creates) and on worktree branches —
    two concurrent spawns could pick the same name. We hold an exclusive flock just
    for that section. We deliberately do NOT hold it during the blocking wait/watch
    or input sends, so a long-running `watch` never blocks a concurrent `reply`/
    `status`, and parallel sessions don't serialize. Pane-slot renumbering from
    concurrent spawns is already handled by re-resolving pane_id from terminal_id."""
    os.makedirs(_core.STATE_DIR, exist_ok=True)
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------
def _emit(command, ok=True, session=None, result=None, error=None):
    print(json.dumps({
        "ok": ok, "schema_version": SCHEMA, "command": command,
        "session": session, "result": result, "error": error,
    }, indent=2))


def _emit_line(command, ok=True, session=None, result=None, error=None):
    """One-line JSON envelope — the streaming form `watch` emits (JSONL: each line
    is one event/notification for the Monitor tool). Flushed so events arrive live."""
    print(json.dumps({
        "ok": ok, "schema_version": SCHEMA, "command": command,
        "session": session, "result": result, "error": error,
    }), flush=True)


_PLAN_INTENT_RE = re.compile(r"\bplan(s|ning)?\b", re.I)


def _wants_plan(task):
    """True if the task mentions planning (the word plan/plans/planning). Used to
    auto-engage /plan so a 'do a plan first' task lands in plan mode without a flag."""
    return bool(_PLAN_INTENT_RE.search(task or ""))


def _effective_plan(args):
    """Resolve plan mode: --no-plan wins, then --plan, else infer from the task text."""
    if getattr(args, "no_plan", False):
        return False
    if getattr(args, "plan", False):
        return True
    return _wants_plan(getattr(args, "task", ""))


def _content_sig(result):
    """Signature of a verdict's actionable content, so `watch` emits once per real
    state change — not on every re-read of the same waiting state."""
    return json.dumps([result.get("state"), result.get("reason"),
                       result.get("questions"),
                       [o.get("key") for o in result.get("options", [])],
                       bool(result.get("marker_found")), bool(result.get("plan"))],
                      sort_keys=True)


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


def _env_truthy(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _request(method, params):
    resp = _core.rpc(method, params)
    if "error" in resp:
        err = resp["error"]
        raise _core.HerdrError("HERDR_API", f"{method} failed: {err.get('message', err)}")
    return resp["result"]


def _usage_error(command, code, message, suggestion):
    return _fail(command, "usage", code, message, False, suggestion, exit_code=2)


def _resolve_worktree(args, slug):
    """Materialize a worktree if --worktree was requested. Returns (info, error)
    where info is None if no --worktree, or {repo, branch, path, caller_branch,
    keep_worktree}. Errors are usage errors (no git repo / spawn-time failure)."""
    want = bool(getattr(args, "worktree", False)) or _env_truthy("CODEX_WORKTREE")
    keep_wt = bool(getattr(args, "keep_worktree", False)) or _env_truthy("CODEX_KEEP_WORKTREE")
    if not want:
        return None, 0
    repo = _core.repo_root(args.cwd or os.getcwd())
    if not repo:
        return None, _usage_error("start", "NO_REPO",
                                  "--worktree requires being inside a git repository.",
                                  "Run inside a checkout, or pass --cwd to a path inside one.")
    caller_branch = _core.current_branch(repo)
    branch = _core.unique_branch(repo, f"codex/{slug}")
    # Path mirrors the unique branch name so worktree dirs don't collide either.
    safe = branch.replace("/", "-")
    path = os.path.join(repo, ".worktrees", safe)
    try:
        wt_path = _core.worktree_create(repo, branch, path, base="HEAD")
    except _core.HerdrError as e:
        return None, _fail("start", "environment", e.code, e.message, True,
                           "Inspect repo state with `git worktree list`, then retry.",
                           exit_code=3)
    return {"repo": repo, "branch": branch, "path": wt_path,
            "caller_branch": caller_branch, "keep": keep_wt}, 0


def _resolve_spawn_plan(args):
    """Validate flags and build a structured per-mode spawn plan. Returns
    (plan_dict, 0) or (None, exit_code). Plan keys:
      mode      -- pane | tab | space
      slug      -- validated slug
      label     -- final user-facing label (pane label for pane mode, tab label
                   for tab/space modes)
      cwd       -- working directory for the spawned pane (overridden by
                   worktree path when --worktree is set)
      keep      -- True to skip resource teardown on `end`
      pane      -- pane-mode extras: {caller_tab_id}
      tab       -- tab-mode extras:  {workspace_id}
      space     -- space-mode extras: {workspace_label, inner_label}
      worktree  -- None, or {repo, branch, path, caller_branch, keep}
    """
    mode = (args.mode or os.environ.get("CODEX_IN") or "pane").strip().lower()
    if mode not in name_herdr_tab.MODES:
        return None, _usage_error("start", "BAD_MODE",
                                  f"--in must be one of {', '.join(name_herdr_tab.MODES)}; got {mode!r}.",
                                  "Use --in pane (default), --in tab, or --in space.")
    keep = bool(args.keep) or _env_truthy("CODEX_KEEP")
    try:
        name_herdr_tab.validate_slug(args.slug)
    except name_herdr_tab.NamingError as e:
        return None, _usage_error("start", "BAD_SLUG", str(e),
                                  "Use 1-3 lowercase words with [a-z0-9-], e.g. fix-spawn-race.")
    worktree, code = _resolve_worktree(args, args.slug)
    if code:
        return None, code
    try:
        info = name_herdr_tab.build_label(_request, args.slug, mode=mode)
    except (KeyError, name_herdr_tab.NamingError, _core.HerdrError) as e:
        # Naming failure happens after worktree materialization; roll the
        # worktree back so we don't leak it on a non-spawn error.
        if worktree:
            _core.worktree_remove(worktree["repo"], worktree["path"],
                                  branch=worktree["branch"])
        return None, _fail("start", "environment", "NAMING_FAILED", str(e), True,
                           "Check HERDR_PANE_ID and `herdr status`, then retry.", exit_code=3)
    cwd = worktree["path"] if worktree else args.cwd
    plan = {"mode": mode, "slug": args.slug, "label": info["label"],
            "cwd": cwd, "keep": keep, "worktree": worktree}
    if mode == "pane":
        plan["pane"] = {"caller_tab_id": info["target_tab_id"]}
    elif mode == "tab":
        plan["tab"] = {"workspace_id": info["target_workspace_id"]}
    else:  # space
        plan["space"] = {"workspace_label": info["workspace_label"],
                         "inner_label": info["label"]}
    return plan, 0


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------
def _spawn_for_mode(plan):
    """Dispatch the actual spawn per mode. Returns the spawn info dict (with an
    extra `workspace_id` key for space mode). Raises _core.HerdrError on failure."""
    mode = plan["mode"]
    label = plan["label"]
    cwd = plan["cwd"]
    if mode == "pane":
        return _core.spawn_codex_pane(plan["pane"]["caller_tab_id"], label, cwd=cwd)
    if mode == "tab":
        return _core.spawn_codex_tab(plan["tab"]["workspace_id"], label, cwd=cwd)
    return _core.spawn_codex_space(plan["space"]["workspace_label"],
                                   plan["space"]["inner_label"], cwd=cwd)


def cmd_start(args):
    marker = _marker_for(args)
    do_plan = _effective_plan(args)
    if do_plan and not args.plan:
        print(f"[codex.py] task mentions a plan; engaging plan mode (use --no-plan to disable).",
              file=sys.stderr)
    # Only the spawn (check-then-create on labels/branches) needs the cross-process
    # lock; the send and the wait below run lock-free so a watch/reply can run in
    # parallel.
    with _spawn_lock():
        plan, code = _resolve_spawn_plan(args)
        if plan is None:
            return code
        label = plan["label"]
        session_id = "cdx-" + uuid.uuid4().hex[:4]
        try:
            info = _spawn_for_mode(plan)
        except _core.HerdrError:
            # Spawn failure leaves no herdr resources for pane/tab modes (the helpers
            # roll back internally) but the worktree may have been materialized
            # already — release it so we don't leak it on a failed start.
            wt = plan.get("worktree")
            if wt:
                _core.worktree_remove(wt["repo"], wt["path"], branch=wt["branch"])
            raise
        rec = {"session": session_id, "label": label, "terminal_id": info["terminal_id"],
               "pane_id": info["pane_id"], "tab_id": info.get("tab_id"), "agent": info["agent"],
               "marker": marker, "plan_mode": do_plan, "created": time.time(),
               "last_state": "spawned", "plan": None,
               "slug": plan["slug"], "mode": plan["mode"], "keep": plan["keep"],
               "workspace_id": info.get("workspace_id") if plan["mode"] == "space"
                                else (plan.get("tab", {}).get("workspace_id")
                                      if plan["mode"] == "tab" else None),
               "caller_tab_id": plan.get("pane", {}).get("caller_tab_id")
                                if plan["mode"] == "pane" else None,
               "worktree": plan.get("worktree"),
               "keep_worktree": bool(plan.get("worktree") and plan["worktree"].get("keep"))}
        _core.save_session(rec)
    # Lock released. A concurrent spawn may have renumbered pane slots between the
    # spawn and the first send, so re-resolve the live pane_id from the stable
    # terminal_id before using it (otherwise the task could be sent to the wrong pane).
    pane_id = _core.resolve_pane_id(rec) or info["pane_id"]

    if do_plan:
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
            "monitor": _monitor_hint(session_id, args.expect, args.timeout),
            "next_action": {"intent": "monitor",
                            "command": _watch_cmd(session_id, args.expect),
                            "why": "Arm the Monitor tool with result.monitor to stream "
                                   "state changes (questions/plan/completion) as events."}})
        return 0
    return _verdict("start", session_id, pane_id, marker, args.expect, args.timeout)


def _watch_cmd(session_id, expect):
    """The `codex.py watch` command line for a session (used as the Monitor command)."""
    extra = "".join(f" --expect {shlex.quote(p)}" for p in (expect or []))
    return f"python3 {SELF} watch --session {session_id}{extra}"


def _monitor_hint(session_id, expect, timeout):
    """A ready-to-use Monitor tool invocation for the orchestrator to arm a watch."""
    return {"command": _watch_cmd(session_id, expect),
            "description": f"codex {session_id}: stream state changes",
            "timeout_ms": max(60000, int(timeout) * 1000),
            "persistent": False}


def _emit_sent(command, session_id):
    """Minimal 'input sent, not waiting' envelope — for the watch flow, where an
    armed watch streams the next state so the send/reply itself need not block."""
    _emit(command, ok=True, session=session_id, result={
        "state": "working", "reason": "no_wait", "summary": "Input sent; not waiting.",
        "plan": None, "questions": [], "options": [], "marker_found": False,
        "artifacts": [], "transcript_tail": "",
        "next_action": {"intent": "wait", "command": None,
                        "why": "Input sent; an armed watch will stream the next state."}})
    return 0


def cmd_send(args):
    resolved, code = _resolve("send", args.session)
    if resolved is None:
        return code
    rec, pane_id = resolved
    _core.send_task_verified(pane_id, _with_marker_reminder(args.message, rec.get("marker")))
    if getattr(args, "no_wait", False):
        return _emit_sent("send", args.session)
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
    if getattr(args, "no_wait", False):
        return _emit_sent("reply", args.session)
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
                               args.expect, args.session, SELF,
                               screen=_core.read_screen(pane_id))
    _emit("status", ok=True, session=args.session, result=result)
    return 0


def _legacy_mode(rec):
    """Map a pre-cutover session record onto the new shape. Returns (mode, keep,
    workspace_id). Detects --in space by presence of isolated_workspace_id."""
    if rec.get("isolated_workspace_id"):
        return "space", bool(rec.get("keep_isolated_workspace")), rec.get("isolated_workspace_id")
    return "tab", False, None


def _teardown(rec):
    """Close the pane (and workspace/worktree per mode) and delete session state.
    Shared by `end` and `watch`'s auto-close. Returns a summary dict
    {mode, pane_state, workspace_state, worktree_summary}."""
    session = rec["session"]
    mode = rec.get("mode")
    if mode is None:
        mode, keep, workspace_id = _legacy_mode(rec)
    else:
        keep = bool(rec.get("keep"))
        workspace_id = rec.get("workspace_id") if mode == "space" else None
    pane_id = _core.resolve_pane_id(rec)
    pane_state = "already gone"
    if pane_id is not None:
        try:
            _core.release_agent(pane_id)
        except _core.HerdrError:
            pass
        if keep:
            pane_state = "kept"
        else:
            try:
                # Closing the codex pane auto-closes its dedicated tab when it is the
                # sole pane (verified). We deliberately do NOT close_tab by stored id:
                # tab ids renumber when a lower tab closes, so a stale tab_id can close
                # a sibling session's tab.
                _core.close_pane(pane_id)
                pane_state = "closed"
            except _core.HerdrError:
                pass
    workspace_state = None
    if mode == "space" and workspace_id:
        if keep:
            workspace_state = "kept"
        else:
            try:
                _core.close_workspace(workspace_id)
                workspace_state = "closed"
            except _core.HerdrError:
                workspace_state = "close_attempted"
    wt = rec.get("worktree")
    keep_wt = bool(rec.get("keep_worktree"))
    worktree_summary = None
    if wt:
        if keep_wt:
            worktree_summary = {"kept": True, "branch": wt["branch"], "path": wt["path"],
                                "reason": "keep_worktree"}
        else:
            status = _core.worktree_status(wt["repo"], wt["path"], wt.get("caller_branch", ""))
            # ahead == -1 means we couldn't determine (e.g. caller branch missing)
            # -- treat as unmerged and keep, so we never delete work we can't reason about.
            clean_and_merged = (status["ahead"] == 0 and not status["dirty"])
            if clean_and_merged:
                outcome = _core.worktree_remove(wt["repo"], wt["path"], branch=wt["branch"])
                worktree_summary = {"kept": False, "branch": wt["branch"], "path": wt["path"],
                                    "removed": outcome["removed"],
                                    "branch_deleted": outcome["branch_deleted"],
                                    "errors": outcome["errors"]}
            else:
                reason = "dirty_tree" if status["dirty"] else "unmerged_commits"
                if status["ahead"] == -1:
                    reason = "unknown_state"
                worktree_summary = {"kept": True, "branch": wt["branch"], "path": wt["path"],
                                    "ahead": status["ahead"], "dirty": status["dirty"],
                                    "reason": reason}
    _core.delete_session(session)
    return {"mode": mode, "pane_state": pane_state,
            "workspace_state": workspace_state, "worktree_summary": worktree_summary}


def _teardown_summary(td):
    summary = f"({td['mode']}); pane {td['pane_state']}"
    if td["workspace_state"]:
        summary += f", workspace {td['workspace_state']}"
    ws = td["worktree_summary"]
    if ws:
        if ws.get("kept"):
            summary += f", worktree kept ({ws.get('reason')})"
        elif ws.get("removed"):
            summary += ", worktree removed"
        else:
            summary += ", worktree removal attempted"
    return summary


def cmd_end(args):
    rec = _core.load_session(args.session)
    if rec is None:
        return _fail("end", "not_found", "NO_SESSION", f"No session '{args.session}'.",
                     False, "Nothing to clean up.", session=args.session, exit_code=4)
    # Closing the pane/tab/workspace can shift herdr's focus (esp. space mode);
    # capture+restore so teardown never moves the human's view.
    with _core.preserve_focus():
        td = _teardown(rec)
    summary = "Session ended " + _teardown_summary(td) + ", state deleted."
    _emit("end", ok=True, session=args.session, result={
        "state": "ended", "reason": "cleaned_up", "summary": summary,
        "plan": None, "questions": [], "options": [], "marker_found": False, "artifacts": [],
        "transcript_tail": "", "next_action": {"intent": "nothing", "command": None, "why": ""},
        "worktree": td["worktree_summary"]})
    return 0


def _watch_event(session, state, reason, summary, intent="wait", command=None, why="",
                 result=None, **extra):
    """Build + emit one watch JSONL event with a sane envelope shape."""
    base = {"state": state, "reason": reason, "summary": summary,
            "plan": None, "questions": [], "options": [], "marker_found": False,
            "artifacts": [], "transcript_tail": "",
            "next_action": {"intent": intent, "command": command, "why": why}}
    if result:
        base = {**base, **{k: result.get(k) for k in
                           ("plan", "questions", "options", "marker_found",
                            "artifacts", "transcript_tail") if k in result}}
    base.update(extra)
    _emit_line("watch", session=session, result=base)


def cmd_watch(args):
    """Stream one JSON line per real state change until the task completes or the
    pane exits. Built to be armed with the Monitor tool: each stdout line is one
    event/notification. Read-only except for auto-approving permission gates and
    auto-closing on verified success — it never blocks a concurrent reply."""
    # Resolve with a SINGLE-LINE error (watch's whole contract is JSONL; the shared
    # _resolve emits a pretty multi-line envelope, which would break a line reader).
    rec = _core.load_session(args.session)
    if rec is None:
        _emit_line("watch", ok=False, session=args.session, error={
            "class": "not_found", "code": "NO_SESSION", "message": f"No session '{args.session}'.",
            "retryable": False, "suggestion": "Start one with `codex.py start`."})
        return 4
    marker = rec.get("marker")
    auto_approve = not args.no_auto_approve
    auto_close = not args.no_close
    deadline = time.time() + args.timeout

    def _remaining():
        return max(0.0, deadline - time.time())

    last_sig = None
    while _remaining() > 0:
        pane_id = _core.resolve_pane_id(rec)
        if pane_id is None:
            _watch_event(args.session, "exited", "pane_gone",
                         "The Codex pane is gone (process exited or closed).",
                         intent="start",
                         command=f'python3 {SELF} start --task "<your task>" --slug <name>',
                         why="Session ended; start a fresh task (no resume in v1).")
            return 0
        result, _timed = _core.settle_and_analyze(
            pane_id, marker, args.expect, args.session, _remaining(), SELF)
        cur = _core.load_session(args.session)          # persist last state + plan
        if cur is not None:
            cur["last_state"] = result.get("state")
            if result.get("plan"):
                cur["plan"] = result["plan"]
            _core.save_session(cur)
            rec = cur
        state = result.get("state")

        # Permission gate -> auto-approve without surfacing (Codex runs YOLO; gates
        # are rare and low-risk). Disable with --no-auto-approve to surface them.
        if state == "permission_gate" and auto_approve:
            _core.send_keys(pane_id, ["Enter"])
            _watch_event(args.session, "auto_approved", "permission_request",
                         "Auto-approved a permission gate; Codex resumed.",
                         why="Watch continues; the next state will stream.", result=result)
            last_sig = None
            _core.wait_for_working(pane_id, _remaining())
            continue

        sig = _content_sig(result)
        if sig != last_sig:
            _emit_line("watch", session=args.session, result=result)
            last_sig = sig

        if state == "completed":
            verified = result.get("reason") == "marker_verified" or (
                bool(args.expect) and all(a.get("exists") for a in result.get("artifacts", [])))
            if auto_close and verified:
                with _core.preserve_focus():
                    td = _teardown(rec)
                _watch_event(args.session, "ended", "auto_closed",
                             "Task verified complete; " + _teardown_summary(td) + ", state deleted.",
                             intent="nothing", result=result,
                             worktree=td["worktree_summary"])
            return 0
        if state == "exited":
            return 0
        if state == "working":          # timeout while still working -> keep riding
            continue
        # awaiting_* / no_signal: wait for the orchestrator's reply to take effect
        # (Codex resumes working), then loop to stream the next state. If nothing
        # happens before the deadline, end the watch cleanly.
        if _core.wait_for_working(pane_id, _remaining()) is None:
            return 0
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
    s.add_argument("--slug", required=True,
                   help="Structured HERDR slug, e.g. fix-spawn-race. Required: pane labels, "
                        "tab labels, workspace labels, and the worktree branch all derive from it.")
    s.add_argument("--in", dest="mode", choices=list(name_herdr_tab.MODES), default=None,
                   help="Where to spawn Codex: pane (default; split caller's tab), tab (new tab in "
                        "caller's workspace), or space (new workspace). Also CODEX_IN=<mode>.")
    s.add_argument("--keep", action="store_true",
                   help="Skip resource teardown on `end` (keep pane/tab/workspace). "
                        "Also CODEX_KEEP=1.")
    s.add_argument("--worktree", action="store_true",
                   help="Materialize a git worktree on codex/<slug> from HEAD and use it as cwd. "
                        "Requires a git repo. Also CODEX_WORKTREE=1.")
    s.add_argument("--keep-worktree", action="store_true",
                   help="Never auto-remove the worktree on `end` (overrides the merge check). "
                        "Also CODEX_KEEP_WORKTREE=1.")
    s.add_argument("--plan", action="store_true",
                   help="Force plan mode (/plan) before the task. Plan mode is also engaged "
                        "automatically when the task mentions a plan.")
    s.add_argument("--no-plan", action="store_true",
                   help="Never engage plan mode, even if the task mentions a plan.")
    s.add_argument("--cwd", default=None, help="Working directory for the pane.")
    s.add_argument("--marker", default=None, help="Completion marker (auto-generated if omitted).")
    s.add_argument("--no-wait", action="store_true", help="Send the task but don't wait.")
    add_wait_flags(s)
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("send", help="Send a follow-up to a live session.")
    s.add_argument("--session", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--no-wait", action="store_true",
                   help="Send but don't wait for a verdict (let an armed watch report).")
    add_wait_flags(s)
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("reply", help="Respond to an awaiting state.")
    s.add_argument("--session", required=True)
    s.add_argument("--text", default=None, help="Free-text answer to a question.")
    s.add_argument("--choice", type=int, default=None, help="Pick option N from a menu/widget.")
    s.add_argument("--approve", action="store_true", help="Approve (select option 1 / Yes).")
    s.add_argument("--reject", action="store_true", help="Reject / cancel (Esc).")
    s.add_argument("--no-wait", action="store_true",
                   help="Send the reply but don't wait for a verdict (let an armed watch report).")
    add_wait_flags(s)
    s.set_defaults(fn=cmd_reply)

    s = sub.add_parser("await", help="Re-enter the wait without sending input.")
    s.add_argument("--session", required=True)
    add_wait_flags(s)
    s.set_defaults(fn=cmd_await)

    s = sub.add_parser("watch", help="Stream one JSON line per state change (for the Monitor tool).")
    s.add_argument("--session", required=True)
    s.add_argument("--no-auto-approve", action="store_true",
                   help="Surface permission gates instead of auto-approving them.")
    s.add_argument("--no-close", action="store_true",
                   help="Do not auto-close the pane on verified success.")
    s.add_argument("--expect", action="append", default=[], metavar="PATH",
                   help="Artifact to verify exists on completion (repeatable).")
    s.add_argument("--timeout", type=int, default=1800,
                   help="Max watch lifetime, seconds (default 1800).")
    s.set_defaults(fn=cmd_watch)

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
