#!/usr/bin/env python3
"""Deterministic unit test of _core.analyze across every state branch.
No spawning — feeds crafted screen tails and asserts (state, reason, next.intent).
Run: python3 scripts/test_analyze.py   (exit 0 = all pass)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills", "name-herdr-tab", "scripts")))
import _core
import codex
import name_herdr_tab

MARK = "CDX_DONE_ABC123"
existing = tempfile.NamedTemporaryFile(delete=False); existing.write(b"hi"); existing.close()
missing = "/tmp/does_not_exist_zzz_%d.txt" % os.getpid()

STATUSBAR = "  gpt-5.5 xhigh · ~/proj · Context 3% used"

cases = []
def case(name, status, tail, marker, expect, want_state, want_reason, want_intent):
    cases.append((name, status, tail, marker, expect, want_state, want_reason, want_intent))

# 1. completed / marker_verified
case("marker_verified", "done",
     f"• Added foo.py (+1 -0)\n• {MARK}\n› Explain this codebase\n{STATUSBAR}",
     MARK, [existing.name], "completed", "marker_verified", "nothing")

# 2. completed / marker_unverified (marker printed, promised file absent)
case("marker_unverified", "done",
     f"• Done.\n• {MARK}\n{STATUSBAR}",
     MARK, [missing], "completed", "marker_unverified", "verify")

# 3. awaiting_clarification / free_text_question (prose '?')
case("free_text_question", "done",
     f"I can do that. Which database should I target?\n› Summarize recent commits\n{STATUSBAR}",
     MARK, [], "awaiting_clarification", "free_text_question", "answer")

# 4. free_text even when NUMBERED (lines end with '?') -> still answer, not choose
case("numbered_questions", "done",
     "A couple things:\n1. What should the app be called?\n2. What tone do you want?\n"
     f"› Find and fix a bug\n{STATUSBAR}",
     MARK, [], "awaiting_clarification", "free_text_question", "answer")

# 5. awaiting_approval / plan_approval (plan menu, idle/done)
PLAN = ("## Plan\n1. Create index.html with the hero section\n"
        "2. Add styles.css for layout\n3. Wire up the contact form\n\n"
        "Implement this plan?\n› 1. Yes, implement this plan          Switch to Default\n"
        "  2. Yes, clear context and implement  Fresh thread.\n"
        "  3. No, stay in Plan mode             Continue planning.\n"
        "Press enter to confirm or esc to go back")
case("plan_approval", "done", PLAN + f"\n{STATUSBAR}",
     MARK, [], "awaiting_approval", "plan_approval", "approve")

# 6. awaiting_clarification / multiple_choice (blocked + widget)
WIDGET = ("Which address should the footer use?\n"
          "› 1. Use placeholder address\n  2. Leave it blank\n  3. Ask the client\n"
          "  4. None of the above\nQuestion 1/1 · enter to submit answer")
case("widget_multiple_choice", "blocked", WIDGET + f"\n{STATUSBAR}",
     MARK, [], "awaiting_clarification", "multiple_choice", "choose")

# 7. permission_gate / permission_request (blocked, no widget)
case("permission_gate", "blocked",
     f"Codex wants to run: rm -rf build/\nAllow this command?\n{STATUSBAR}",
     MARK, [], "permission_gate", "permission_request", "approve")

# 8. multiple_choice idle (numbered, NOT ending in '?')
case("idle_menu", "done",
     "Pick an option:\n1. Static site\n2. React app\n3. Plain HTML\n"
     f"› Implement {{feature}}\n{STATUSBAR}",
     MARK, [], "awaiting_clarification", "multiple_choice", "choose")

# 9. working
case("working", "working", f"• Editing files...\n{STATUSBAR}",
     MARK, [], "working", "working", "wait")

# 10. no_signal (idle, nothing actionable, no marker)
case("no_signal", "done",
     f"Sure, that's interesting.\n› Summarize recent commits\n{STATUSBAR}",
     MARK, [], "no_signal", "no_signal", "verify")

# 11. completed / artifacts_present (no marker but expected file exists)
case("artifacts_present", "done",
     f"All set, wrote the file.\n› Find and fix a bug\n{STATUSBAR}",
     None, [existing.name], "completed", "artifacts_present", "verify")

# 12. completed / reported_done — no marker, no --expect, but a confident done line
# (Codex skips the marker ~60% of the time; this rescues a success from no_signal.)
case("reported_done", "done",
     f"• Added /tmp/x.py (+2 -0)\n• Created /tmp/x.py with add(a, b) and verified it works.\n{STATUSBAR}",
     MARK, [], "completed", "reported_done", "verify")

# 13. no_signal — a FUTURE/intent line must NOT be mistaken for completion
case("intent_not_done", "done",
     f"I'll create the file and verify it next.\n{STATUSBAR}",
     MARK, [], "no_signal", "no_signal", "verify")

fails = 0
for name, status, tail, marker, expect, ws, wr, wi in cases:
    r = _core.analyze(status, tail, marker, expect, "cdx-test", "codex.py")
    got = (r["state"], r["reason"], r["next_action"]["intent"])
    ok = got == (ws, wr, wi)
    if not ok:
        fails += 1
        print(f"FAIL {name}: got {got}, want {(ws, wr, wi)}")
        print(f"     summary={r['summary']!r} options={r['options']} questions={r['questions']}")
    else:
        extra = ""
        if r["options"]:
            extra = f" opts={[o['key'] for o in r['options']]}"
        if r["plan"]:
            extra += " plan=Y"
        print(f"ok   {name}: {got}{extra}")

# Plan must be captured & untruncated WHILE the approval menu is up.
pr = _core.analyze("done", PLAN + f"\n{STATUSBAR}", MARK, [], "x", "codex.py")
assert pr["plan"] and "Wire up the contact form" in pr["plan"], "plan truncated/missing!"
print(f"ok   plan_full: {len(pr['plan'])} chars, untruncated")

# Regression: AFTER approval the menu is gone but a plan header lingers in
# scrollback. `plan` must NOT balloon with the implementation log — it is only
# meaningful while the menu is up. (Caught in a live plan-mode completion run.)
post_approval = (
    "# Coffee Menu Page\n## Summary\nCreate /tmp/x.html as a static page.\n"
    "## Key Changes\n- Add one HTML file.\n## Assumptions\n- Fictional brand.\n"
    "----------------\n> Implement the plan.\n"
    "* Added /tmp/x.html (+61 -0)\n   1 +<!doctype html>\n   2 +<html>\n"
    f"* {MARK}\n> Write tests for @filename\n{STATUSBAR}")
pa = _core.analyze("done", post_approval, MARK, [], "x", "codex.py")
assert pa["plan"] is None, f"plan should be None post-approval, got {len(pa['plan'] or '')} chars"
assert pa["state"] == "completed", f"expected completed post-approval, got {pa['state']}"
print("ok   post_approval_plan_none: plan not ballooned on a completed verdict")

# transcript_tail must be clean: drop the Codex banner box, the ›-prefixed prompt
# echo / composer placeholder, and Codex's internal-skill reads — keep the real
# agent line. (All three leaked in live short-turn retests.)
noisy = (
    "| >_ OpenAI Codex (v0.132.0)                    |\n"
    "| model:       gpt-5.5 xhigh   /model to change |\n"
    "| permissions: YOLO mode                        |\n".replace("|", "│")
    + "› Build a single-file HTML personal-portfolio page at /tmp/x.html, inline CSS\n"
    + "  only, at most 100 lines total. Do not create the file until I answer.\n"  # wrapped echo
    + "* Explored\n  L Read SKILL.md (superpowers:using-superpowers skill)\n".replace("L", "└")
    + "* Which color theme should the portfolio use?\n"
    + "› Summarize recent commits\n" + STATUSBAR)
ct = _core.analyze("done", noisy, MARK, [], "x", "codex.py")["transcript_tail"]
assert "OpenAI Codex" not in ct and "│" not in ct, "banner box leaked into transcript_tail"
assert "Build a single-file" not in ct, "prompt echo leaked into transcript_tail"
assert "at most 100 lines total" not in ct, "WRAPPED prompt-echo continuation leaked"
assert "superpowers" not in ct, "internal-skill read leaked into transcript_tail"
assert "Summarize recent commits" not in ct, "composer placeholder leaked into transcript_tail"
assert "Which color theme should the portfolio use?" in ct, "real agent line was dropped!"
print("ok   transcript_clean: banner/echo/skill-read/placeholder stripped, real line kept")

# Regression (#27): a plan's "Completion Signal" documents the marker on its own
# line; that DOCUMENTED occurrence must NOT count as completion (live, it caused a
# premature marker_unverified during a mid-implementation idle blip).
plan_doc = (
    "## Completion Signal\n"
    f"After the file is created and verification passes, print exactly this token on its own line:\n{MARK}\n"
    "## Assumptions\n- The menu content can be fictional.\n- Inline CSS is fine.\n" + STATUSBAR)
assert _core._marker_on_own_line(MARK, plan_doc) is False, "documented plan marker wrongly counted as completion"
real_done = f"• Created /tmp/x.html.\n  Verified: 63 lines, inline CSS only.\n{MARK}\n{STATUSBAR}"
assert _core._marker_on_own_line(MARK, real_done) is True, "a genuinely printed marker should count"
print("ok   marker_doc_vs_printed: plan-documented marker rejected, printed marker accepted")

# Content retention (live finding): tool gutters ("│ …") / elisions ("… +N lines")
# are stripped, and a detailed report is NOT truncated away (keep is generous).
longrep = ("• Ran python3 - <<'PY'\n  │ import inspect\n  │ … +22 lines\n  └ ok\n"
           "• Created /tmp/x.py. Detailed report:\n"
           + "\n".join(f"- function f{i}: returns {i}" for i in range(15)) + f"\n{STATUSBAR}")
ct = _core.analyze("done", longrep, MARK, [], "x", "codex.py")["transcript_tail"]
assert "import inspect" not in ct and "+22 lines" not in ct, "tool gutter/elision leaked into tail"
assert "function f0" in ct and "function f14" in ct, "detailed report was truncated — lost content"
print("ok   content_kept: gutters/elisions stripped, full report retained")

# Flood guard (edge-hunt finding): a task that floods many long lines stays under
# the LINE cap yet could be multi-KB — transcript_tail must also be char-bounded.
flood = "\n".join(f"LINE-{i}: the quick brown fox jumps over the lazy dog." for i in range(200)) + f"\n{STATUSBAR}"
fct = _core.analyze("done", flood, MARK, [], "x", "codex.py")["transcript_tail"]
assert len(fct) <= _core.TAIL_CHARS + 120, f"transcript_tail not char-bounded: {len(fct)} chars"
print(f"ok   flood_bounded: transcript_tail = {len(fct)} chars (<= {_core.TAIL_CHARS} cap)")

# Regression: start should create and address a background tab without focusing
# it. Focusing here steals the human's active tab as soon as a helper starts.
calls = []
orig_rpc = _core.rpc
orig_list_panes = _core.list_panes
orig_wait_until_ready = _core.wait_until_ready
orig_sleep = _core.time.sleep
try:
    def fake_rpc(method, params, socket_path=_core.SOCKET_PATH, timeout=10):
        calls.append((method, params))
        if method == "tab.create":
            return {"result": {"tab": {"tab_id": "1:2"}, "root_pane": {"pane_id": "1-2"}}}
        if method == "agent.start":
            return {"result": {"agent": {"pane_id": "1-3", "terminal_id": "term-cdx", "agent": "codex"}}}
        if method == "pane.close":
            return {"result": {}}
        if method == "pane.get":
            return {"result": {"pane": {"agent": "codex"}}}
        raise AssertionError(f"unexpected rpc: {method}")
    _core.rpc = fake_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "1-3", "terminal_id": "term-cdx"}]
    _core.wait_until_ready = lambda *a, **k: True
    _core.time.sleep = lambda _seconds: None
    info = _core.spawn_codex("cdx-test", workspace_id="1")
    assert info["pane_id"] == "1-3"
    assert ("tab.focus", {"tab_id": "1:2"}) not in calls, "spawn should not focus the helper tab"
    assert calls[0] == ("tab.create", {"focus": False, "label": "cdx-test", "workspace_id": "1"})
    assert calls[1][0] == "agent.start", calls[1]
    assert calls[1][1]["tab_id"] == "1:2" and calls[1][1]["focus"] is False
    print("ok   spawn_background: tab and agent start stay unfocused")
finally:
    _core.rpc = orig_rpc
    _core.list_panes = orig_list_panes
    _core.wait_until_ready = orig_wait_until_ready
    _core.time.sleep = orig_sleep

# Regression: verified send must never append a long task repeatedly just
# because composer detection cannot prove where the wrapped input landed.
orig_status = _core.current_status
orig_read_screen = _core.read_screen
orig_send_text_enter = _core.send_text_enter
orig_send_keys = _core.send_keys
orig_sleep = _core.time.sleep
try:
    sent = []
    keyed = []
    _core.current_status = lambda *a, **k: "idle"
    _core.read_screen = lambda *a, **k: f"› Find and fix a bug\n{STATUSBAR}"
    _core.send_text_enter = lambda pane_id, text, socket_path=_core.SOCKET_PATH: sent.append(text)
    _core.send_keys = lambda pane_id, keys, socket_path=_core.SOCKET_PATH: keyed.append(keys)
    _core.time.sleep = lambda _seconds: None
    assert _core.send_task_verified("1-3", "Do the important task", tries=4) is True
    assert sent == ["Do the important task"], f"task text was sent {len(sent)} times"
    assert keyed == [], "placeholder-only composer should not receive Enter before the task"
    print("ok   send_once: task text is written at most once")
finally:
    _core.current_status = orig_status
    _core.read_screen = orig_read_screen
    _core.send_text_enter = orig_send_text_enter
    _core.send_keys = orig_send_keys
    _core.time.sleep = orig_sleep

# Slug validation and label assembly rules shared by the naming skill and codex.py.
for slug in ("fix-spawn-race", "audit-ui", "clone-repo"):
    assert name_herdr_tab.validate_slug(slug) == slug
for slug in ("Fix-Spawn", "fix_spawn", "fix--race", "one-two-three-four", "codex", "audit-"):
    try:
        name_herdr_tab.validate_slug(slug)
    except name_herdr_tab.NamingError:
        pass
    else:
        raise AssertionError(f"invalid slug accepted: {slug}")
print("ok   slug_validation: accepts safe slugs and rejects unsafe/reserved ones")

def naming_request(method, params):
    if method == "pane.get":
        assert params == {"pane_id": "p_5"}
        return {"pane": {"workspace_id": "w1", "tab_id": "w1:2"}}
    if method == "workspace.get":
        return {"workspace": {"workspace_id": "w1", "label": "Client Space!"}}
    if method == "tab.get":
        return {"tab": {"tab_id": "w1:2", "label": "Review PR"}}
    if method == "tab.list":
        return {"tabs": [
            {"label": "client-space-review-pr-fix-spawn-race"},
            {"label": "client-space-review-pr-fix-spawn-race-2"},
        ]}
    raise AssertionError(f"unexpected naming method {method}")

label_info = name_herdr_tab.build_label(
    naming_request, "fix-spawn-race", env={"HERDR_PANE_ID": "p_5"})
assert label_info["space_name"] == "client-space"
assert label_info["tab_name"] == "review-pr"
assert label_info["label"] == "client-space-review-pr-fix-spawn-race-3"
print("ok   label_assembly: caller labels sanitized and collision suffix applied")

# Regression: isolated start creates a workspace, names the tab, spawns there,
# then closes the temporary root shell pane created with the workspace.
orig_rpc = _core.rpc
orig_list_panes = _core.list_panes
orig_wait_until_ready = _core.wait_until_ready
orig_send_task_verified = _core.send_task_verified
orig_read_screen = _core.read_screen
orig_settle = _core.settle_and_analyze
orig_sleep = _core.time.sleep
orig_save_session = _core.save_session
saved = {}
calls = []
try:
    def fake_rpc(method, params, socket_path=_core.SOCKET_PATH, timeout=10):
        calls.append((method, params))
        if method == "pane.get":
            if params["pane_id"] == "p_5":
                return {"result": {"pane": {"workspace_id": "w1", "tab_id": "w1:2"}}}
            return {"result": {"pane": {"agent": "codex"}}}
        if method == "workspace.get":
            return {"result": {"workspace": {"workspace_id": "w1", "label": "Client Space"}}}
        if method == "tab.get":
            return {"result": {"tab": {"tab_id": "w1:2", "label": "Review PR"}}}
        if method == "workspace.create":
            assert params == {"focus": False, "label": "review-pr", "cwd": "/repo"}
            return {"result": {"workspace": {"workspace_id": "wiso"}, "root_pane": {"pane_id": "wiso-1"}}}
        if method == "tab.list":
            assert params == {"workspace_id": "wiso"}
            return {"result": {"tabs": []}}
        if method == "tab.create":
            assert params == {"focus": False, "label": "client-space-review-pr-audit-ui",
                              "workspace_id": "wiso"}
            return {"result": {"tab": {"tab_id": "wiso:2"}, "root_pane": {"pane_id": "wiso-2"}}}
        if method == "agent.start":
            assert params["name"] == "client-space-review-pr-audit-ui"
            assert params["workspace_id"] == "wiso" and params["tab_id"] == "wiso:2"
            assert params["focus"] is False and params["cwd"] == "/repo"
            return {"result": {"agent": {"pane_id": "wiso-3", "terminal_id": "term-cdx", "agent": "codex"}}}
        if method == "pane.close":
            return {"result": {}}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    class Args:
        task = "do work"
        plan = False
        cwd = "/repo"
        label = None
        slug = "audit-ui"
        isolated_space = True
        keep_isolated_space = False
        marker = "CDX_DONE_TEST"
        no_wait = False
        expect = []
        timeout = 1

    os.environ["HERDR_PANE_ID"] = "p_5"
    _core.rpc = fake_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "wiso-3", "terminal_id": "term-cdx"}]
    _core.wait_until_ready = lambda *a, **k: True
    _core.send_task_verified = lambda *a, **k: True
    _core.read_screen = lambda *a, **k: ""
    _core.settle_and_analyze = lambda *a, **k: ({"state": "completed", "reason": "marker_verified",
                                                "summary": "", "plan": None, "questions": [],
                                                "options": [], "marker_found": True,
                                                "artifacts": [], "transcript_tail": "",
                                                "next_action": {"intent": "nothing",
                                                                "command": None, "why": ""}}, False)
    _core.time.sleep = lambda _seconds: None
    _core.save_session = lambda rec: saved.update(rec)
    assert codex.cmd_start(Args) == 0
    assert saved["label"] == "client-space-review-pr-audit-ui"
    assert saved["isolated_workspace_id"] == "wiso"
    assert ("pane.close", {"pane_id": "wiso-1"}) in calls
    print("ok   isolated_start: workspace created, tab labeled, root shell closed")
finally:
    os.environ.pop("HERDR_PANE_ID", None)
    _core.rpc = orig_rpc
    _core.list_panes = orig_list_panes
    _core.wait_until_ready = orig_wait_until_ready
    _core.send_task_verified = orig_send_task_verified
    _core.read_screen = orig_read_screen
    _core.settle_and_analyze = orig_settle
    _core.time.sleep = orig_sleep
    _core.save_session = orig_save_session

# Regression: end closes an isolated workspace unless the session asks to keep it.
orig_load_session = _core.load_session
orig_resolve = _core.resolve_pane_id
orig_release = _core.release_agent
orig_close_pane = _core.close_pane
orig_close_workspace = _core.close_workspace
orig_delete = _core.delete_session
closed_workspaces = []
try:
    _core.load_session = lambda session: {"session": session, "terminal_id": "term-cdx",
                                          "isolated_workspace_id": "wiso",
                                          "keep_isolated_workspace": False}
    _core.resolve_pane_id = lambda rec: "wiso-3"
    _core.release_agent = lambda pane_id: None
    _core.close_pane = lambda pane_id: None
    _core.close_workspace = lambda workspace_id: closed_workspaces.append(workspace_id)
    _core.delete_session = lambda session: None
    class EndArgs:
        session = "cdx-test"
    assert codex.cmd_end(EndArgs) == 0
    assert closed_workspaces == ["wiso"]
    print("ok   isolated_end: workspace closed on cleanup")
finally:
    _core.load_session = orig_load_session
    _core.resolve_pane_id = orig_resolve
    _core.release_agent = orig_release
    _core.close_pane = orig_close_pane
    _core.close_workspace = orig_close_workspace
    _core.delete_session = orig_delete

os.unlink(existing.name)
print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'} ({len(cases)} cases)")
sys.exit(1 if fails else 0)
