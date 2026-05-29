#!/usr/bin/env python3
"""Deterministic unit test of _core.analyze across every state branch.
No spawning — feeds crafted screen tails and asserts (state, reason, next.intent).
Run: python3 scripts/test_analyze.py   (exit 0 = all pass)."""
import json, sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core
import codex
import name_herdr_tab

MARK = "CDX_DONE_ABC123"
existing = tempfile.NamedTemporaryFile(delete=False); existing.write(b"hi"); existing.close()
missing = "/tmp/does_not_exist_zzz_%d.txt" % os.getpid()

STATUSBAR = "  gpt-5.5 xhigh · ~/proj · Context 3% used"

cases = []
def case(name, status, tail, marker, expect, want_state, want_reason, want_intent, screen=None):
    cases.append((name, status, tail, marker, expect, want_state, want_reason, want_intent, screen))

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

# 14. stale question in scrollback, fresh VISIBLE screen -> ignore the stale question.
# (Live bug: reply --choice settled on a blip and re-reported the answered question.)
case("stale_question_scrollback", "done",
     "Which database should I target?\n• Proposed Plan\n# Add Feature\n## Summary\n"
     f"Building the feature.\n{STATUSBAR}",
     MARK, [], "no_signal", "no_signal", "verify",
     screen=f"# Add Feature\n## Summary\nBuilding the feature.\n{STATUSBAR}")

# 15. stale numbered menu in scrollback, fresh VISIBLE screen -> not multiple_choice.
case("stale_menu_scrollback", "done",
     "Pick one:\n1. Modern\n2. Classic\n3. Vintage\n• Going with Modern.\n"
     f"• Working on the layout.\n{STATUSBAR}",
     MARK, [], "no_signal", "no_signal", "verify",
     screen=f"• Going with Modern.\n• Working on the layout.\n{STATUSBAR}")

# 16. plan-approval menu surfacing as `blocked` (build-dependent) -> still plan_approval.
case("blocked_plan_menu", "blocked", PLAN + f"\n{STATUSBAR}",
     MARK, [], "awaiting_approval", "plan_approval", "approve")

# 17. composer rotating placeholder ending in '?' is NOT an agent question.
case("placeholder_not_question", "done",
     f"• Here is a summary of the code.\n› What does this codebase do?\n{STATUSBAR}",
     MARK, [], "no_signal", "no_signal", "verify")

fails = 0
for name, status, tail, marker, expect, ws, wr, wi, screen in cases:
    r = _core.analyze(status, tail, marker, expect, "cdx-test", "codex.py", screen=screen)
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

# Contrast: the recent transcript alone (no `screen`) re-reports a stale, already-answered
# question — the live bug; passing the fresh visible `screen` suppresses it.
stale_tail = ("Which database should I target?\n• Proposed Plan\n# Add Feature\n"
              "## Summary\nBuilding the feature.\n" + STATUSBAR)
bug = _core.analyze("done", stale_tail, MARK, [], "x", "codex.py")
assert bug["reason"] == "free_text_question", f"stale-tail bug repro changed: {bug['reason']}"
fix = _core.analyze("done", stale_tail, MARK, [], "x", "codex.py",
                    screen="# Add Feature\n## Summary\nBuilding the feature.\n" + STATUSBAR)
assert fix["reason"] == "no_signal", f"screen-scoped analyze should ignore stale Q, got {fix['reason']}"
print("ok   stale_question_scoped_to_screen: visible-screen scoping suppresses stale scrollback Q")

# Auto-plan detection: the word plan/plans/planning (whole word) engages plan mode;
# planet/casual prose does not. --no-plan wins; --plan forces.
import types as _types
assert codex._wants_plan("do a comprehensive plan first and present it")
assert codex._wants_plan("Plan the migration") and codex._wants_plan("update the planning doc")
assert not codex._wants_plan("build a website for a dentist")
assert not codex._wants_plan("explain the planet's orbit")
_fa = _types.SimpleNamespace(no_plan=False, plan=False, task="just build it")
assert codex._effective_plan(_fa) is False
_fa.task = "make a plan first"; assert codex._effective_plan(_fa) is True
_fa.no_plan = True; assert codex._effective_plan(_fa) is False              # --no-plan wins
_fa.no_plan, _fa.plan, _fa.task = False, True, "build x"
assert codex._effective_plan(_fa) is True                                   # --plan forces
print("ok   auto_plan_detection: 'plan' word engages plan mode; --no-plan wins, --plan forces")

# A watch state-change signature changes when the actionable content changes, so
# `watch` emits once per real transition (not on every re-read of the same state).
_q1 = {"state": "awaiting_clarification", "reason": "free_text_question",
       "questions": ["A?"], "options": [], "marker_found": False, "plan": None}
_q2 = dict(_q1, questions=["B?"])
assert codex._content_sig(_q1) == codex._content_sig(dict(_q1))
assert codex._content_sig(_q1) != codex._content_sig(_q2)
print("ok   watch_signature: same state same sig; changed content -> new sig")

# preserve_focus: restores the pre-existing focused workspace if the body shifts it,
# and is a no-op when focus is unchanged (so the human's view never moves).
_orig_rpc = _core.rpc
def _pf_rpc(seq, calls):
    def rpc(method, params, socket_path=_core.SOCKET_PATH, timeout=10):
        if method == "workspace.list":
            ws = seq.pop(0) if seq else seq_last[0]
            seq_last[0] = ws
            return {"result": {"workspaces": [{"workspace_id": ws, "focused": True}]}}
        if method == "workspace.focus":
            calls.append(params["workspace_id"]); return {"result": {}}
        return {"result": {}}
    seq_last = [seq[-1] if seq else None]
    return rpc
try:
    calls = []
    _core.rpc = _pf_rpc(["A", "B"], calls)   # focus A before, B after the body -> restore A
    with _core.preserve_focus():
        pass
    assert calls == ["A"], f"expected restore to A, got {calls}"
    calls2 = []
    _core.rpc = _pf_rpc(["A", "A"], calls2)  # unchanged -> no focus call
    with _core.preserve_focus():
        pass
    assert calls2 == [], f"no shift must not focus, got {calls2}"
finally:
    _core.rpc = _orig_rpc
print("ok   preserve_focus: restores original focus on shift, no-op when unchanged")

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

# Regression: an UNRECOGNIZED rotating placeholder must NOT strand the task. (Live bug:
# the plan-mode placeholder "Use /skills to list available skills" was not in the hint
# set, so the old gate read it as real text, pressed Enter, and NEVER typed the task ->
# Context 0%, task lost. Type-first + composer_holds(text) fixes it.)
orig_status = _core.current_status; orig_read_screen = _core.read_screen
orig_ste = _core.send_text_enter; orig_sk = _core.send_keys; orig_sleep2 = _core.time.sleep
try:
    sent2 = []
    _core.current_status = lambda *a, **k: "idle"
    _core.read_screen = lambda *a, **k: f"› Try a brand new prompt idea here\n{STATUSBAR}"  # NOT in hints
    _core.send_text_enter = lambda pane_id, text, socket_path=_core.SOCKET_PATH: sent2.append(text)
    _core.send_keys = lambda *a, **k: None
    _core.time.sleep = lambda _s: None
    assert _core.send_task_verified("1-3", "Do the important task", tries=4) is True
    assert sent2 == ["Do the important task"], f"unfamiliar placeholder stranded the task: sent={sent2}"
    print("ok   send_unknown_placeholder: task typed even when the composer shows an unfamiliar placeholder")
finally:
    _core.current_status = orig_status; _core.read_screen = orig_read_screen
    _core.send_text_enter = orig_ste; _core.send_keys = orig_sk; _core.time.sleep = orig_sleep2

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
        return {"workspace": {"workspace_id": "w1", "label": "~"}}
    if method == "tab.get":
        return {"tab": {"tab_id": "w1:2", "label": "Review PR"}}
    if method == "tab.list":
        return {"tabs": [
            {"label": "review-pr-review-pr-fix-spawn-race"},
            {"label": "review-pr-review-pr-fix-spawn-race-2"},
        ]}
    raise AssertionError(f"unexpected naming method {method}")

label_info = name_herdr_tab.build_label(
    naming_request, "fix-spawn-race", env={"HERDR_PANE_ID": "p_5"})
assert label_info["space_name"] == "review-pr"
assert label_info["tab_name"] == "review-pr"
assert label_info["label"] == "review-pr-review-pr-fix-spawn-race-3"
print("ok   label_assembly: caller labels sanitized, home workspace mapped, collision suffix applied")

# Regression: --in space creates a workspace, names the inner tab with the slug,
# spawns there, then closes the temporary root shell pane created with the
# workspace. Pinned shape after the spawn-modes redesign.
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
        if method == "workspace.list":
            return {"result": {"workspaces": []}}
        if method == "workspace.create":
            assert params == {"focus": False, "label": "review-pr", "cwd": "/repo"}
            return {"result": {"workspace": {"workspace_id": "wiso"}, "root_pane": {"pane_id": "wiso-1"}}}
        if method == "tab.create":
            assert params == {"focus": False, "label": "audit-ui", "workspace_id": "wiso"}
            return {"result": {"tab": {"tab_id": "wiso:2"}, "root_pane": {"pane_id": "wiso-2"}}}
        if method == "agent.start":
            assert params["name"] == "audit-ui"
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
        slug = "audit-ui"
        mode = "space"
        keep = False
        marker = "CDX_DONE_TEST"
        no_wait = False
        expect = []
        timeout = 1

    os.environ["HERDR_PANE_ID"] = "p_5"
    _core.rpc = fake_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "wiso-1", "terminal_id": "term-cdx"}]
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
    assert saved["label"] == "audit-ui"
    assert saved["pane_id"] == "wiso-1"
    assert saved["mode"] == "space"
    assert saved["workspace_id"] == "wiso"
    assert saved["keep"] is False
    assert ("pane.close", {"pane_id": "wiso-1"}) in calls
    print("ok   space_start: workspace created, inner tab labeled with slug, root shell closed")
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

# Regression: --in pane spawns via agent.start --split right + pane.rename,
# with NO tab.create and NO workspace.create. Caller's tab/workspace untouched.
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
        if method == "pane.list":
            return {"result": {"panes": [{"pane_id": "w1-3", "tab_id": "w1:2", "label": None}]}}
        if method == "agent.start":
            assert params["split"] == "right" and params["focus"] is False
            assert params["tab_id"] == "w1:2" and params["name"] == "quick-side"
            return {"result": {"agent": {"pane_id": "w1-4", "terminal_id": "term-pane",
                                         "agent": "codex"}}}
        if method == "pane.rename":
            assert params == {"pane_id": "w1-4", "label": "quick-side"}
            return {"result": {}}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    class Args:
        task = "do work"
        plan = False
        cwd = None
        slug = "quick-side"
        mode = "pane"
        keep = False
        marker = "CDX_DONE_TEST"
        no_wait = False
        expect = []
        timeout = 1

    os.environ["HERDR_PANE_ID"] = "p_5"
    _core.rpc = fake_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "w1-4", "terminal_id": "term-pane"}]
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
    methods = [m for m, _ in calls]
    assert "tab.create" not in methods, "pane mode must NOT create a tab"
    assert "workspace.create" not in methods, "pane mode must NOT create a workspace"
    assert "pane.rename" in methods, "pane mode must apply a sidebar label via pane.rename"
    assert saved["mode"] == "pane" and saved["label"] == "quick-side"
    assert saved["caller_tab_id"] == "w1:2"
    assert saved["workspace_id"] is None
    print("ok   pane_start: agent.start --split right + pane.rename, no tab/workspace create")
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

# Regression: --in tab uses the composed label and the caller's workspace, no
# workspace.create. The full-width-tab spawn flow (tab.create + agent.start +
# close root pane) is shared with space mode and exercised by space_start above.
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
        if method == "tab.list":
            assert params == {"workspace_id": "w1"}
            return {"result": {"tabs": []}}
        if method == "tab.create":
            assert params == {"focus": False, "label": "client-space-review-pr-audit-ui",
                              "workspace_id": "w1"}
            return {"result": {"tab": {"tab_id": "w1:3"}, "root_pane": {"pane_id": "w1-9"}}}
        if method == "agent.start":
            assert params["name"] == "client-space-review-pr-audit-ui"
            assert params["workspace_id"] == "w1" and params["tab_id"] == "w1:3"
            assert params["focus"] is False
            return {"result": {"agent": {"pane_id": "w1-10", "terminal_id": "term-tab",
                                         "agent": "codex"}}}
        if method == "pane.close":
            return {"result": {}}
        raise AssertionError(f"unexpected rpc: {method} {params}")

    class Args:
        task = "do work"
        plan = False
        cwd = None
        slug = "audit-ui"
        mode = "tab"
        keep = False
        marker = "CDX_DONE_TEST"
        no_wait = False
        expect = []
        timeout = 1

    os.environ["HERDR_PANE_ID"] = "p_5"
    _core.rpc = fake_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "w1-10", "terminal_id": "term-tab"}]
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
    methods = [m for m, _ in calls]
    assert "workspace.create" not in methods, "tab mode must NOT create a workspace"
    assert "pane.rename" not in methods, "tab mode labels via tab.create, not pane.rename"
    assert saved["mode"] == "tab" and saved["label"] == "client-space-review-pr-audit-ui"
    assert saved["workspace_id"] == "w1"
    print("ok   tab_start: tab.create in caller workspace with composed label, no workspace.create")
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

# Regression: pane-mode collision suffixing walks against existing pane labels
# within the target tab (NOT tab labels, NOT all panes).
def pane_naming_rpc(method, params):
    if method == "pane.get":
        assert params == {"pane_id": "p_5"}
        return {"pane": {"workspace_id": "w1", "tab_id": "w1:2"}}
    if method == "workspace.get":
        return {"workspace": {"workspace_id": "w1", "label": "Client Space"}}
    if method == "tab.get":
        return {"tab": {"tab_id": "w1:2", "label": "Review PR"}}
    if method == "pane.list":
        return {"panes": [
            {"pane_id": "w1-3", "tab_id": "w1:2", "label": "audit-ui"},
            {"pane_id": "w1-4", "tab_id": "w1:2", "label": "audit-ui-2"},
            {"pane_id": "w1-5", "tab_id": "w1:9", "label": "audit-ui-3"},  # different tab
        ]}
    raise AssertionError(f"unexpected naming method {method}")

pane_info = name_herdr_tab.build_label(
    pane_naming_rpc, "audit-ui", mode="pane", env={"HERDR_PANE_ID": "p_5"})
assert pane_info["label"] == "audit-ui-3", f"pane collision suffix wrong: {pane_info['label']}"
assert pane_info["target_tab_id"] == "w1:2"
print("ok   pane_collision_suffix: -3 chosen (peers -2 in same tab; -3 in other tab is OK)")

# Regression: NO spawn path ever changes focus. Any tab.focus / workspace.focus /
# pane.focus call, OR any params with focus=True, leaks the human's view. Sweep
# the recorded calls from the per-mode tests (re-run the dispatchers minimally
# rather than relying on prior test state).
focus_violations = []
def focus_audit_rpc(method, params, socket_path=_core.SOCKET_PATH, timeout=10):
    if method.endswith(".focus") or params.get("focus") is True:
        focus_violations.append((method, params))
    if method == "pane.get":
        if params.get("pane_id") == "p_5":
            return {"result": {"pane": {"workspace_id": "w1", "tab_id": "w1:2"}}}
        return {"result": {"pane": {"agent": "codex"}}}
    if method == "workspace.get":
        return {"result": {"workspace": {"workspace_id": "w1", "label": "Client Space"}}}
    if method == "tab.get":
        return {"result": {"tab": {"tab_id": "w1:2", "label": "Review PR"}}}
    if method == "pane.list":
        return {"result": {"panes": []}}
    if method == "tab.list":
        return {"result": {"tabs": []}}
    if method == "workspace.list":
        return {"result": {"workspaces": []}}
    if method == "tab.create":
        return {"result": {"tab": {"tab_id": "w1:3"}, "root_pane": {"pane_id": "w1-9"}}}
    if method == "workspace.create":
        return {"result": {"workspace": {"workspace_id": "ws-iso"},
                           "root_pane": {"pane_id": "ws-iso-1"}}}
    if method == "agent.start":
        return {"result": {"agent": {"pane_id": "x-99", "terminal_id": "tt", "agent": "codex"}}}
    if method == "pane.close" or method == "pane.rename":
        return {"result": {}}
    raise AssertionError(f"unexpected rpc: {method}")

orig_rpc = _core.rpc
orig_list_panes = _core.list_panes
orig_wait_until_ready = _core.wait_until_ready
orig_send_task_verified = _core.send_task_verified
orig_read_screen = _core.read_screen
orig_settle = _core.settle_and_analyze
orig_sleep = _core.time.sleep
orig_save_session = _core.save_session
try:
    os.environ["HERDR_PANE_ID"] = "p_5"
    _core.rpc = focus_audit_rpc
    _core.list_panes = lambda socket_path=_core.SOCKET_PATH: [{"pane_id": "x-99", "terminal_id": "tt"}]
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
    _core.save_session = lambda rec: None
    for mode in ("pane", "tab", "space"):
        class A:
            task = "do work"; plan = False; cwd = None; slug = "no-focus"; keep = False
            marker = "M"; no_wait = False; expect = []; timeout = 1
        A.mode = mode
        codex.cmd_start(A)
    assert not focus_violations, f"focus stolen: {focus_violations}"
    print("ok   no_focus_invariant: pane/tab/space all spawn unfocused (no .focus rpc, no focus=True)")
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

# Regression: worktree round-trip — when the branch is fully merged AND the
# working tree is clean, `end` removes the worktree and deletes the branch.
import subprocess, shutil
def _gx(args, cwd):
    return subprocess.check_call(["git", "-c", "user.email=t@t", "-c", "user.name=t"] + args,
                                 cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

repo = tempfile.mkdtemp(prefix="codex-wt-merged-")
try:
    _gx(["init", "-q", "-b", "main"], repo)
    _gx(["commit", "--allow-empty", "-q", "-m", "init"], repo)
    wt_path = _core.worktree_create(repo, "codex/wt-merged", os.path.join(repo, ".worktrees", "codex-wt-merged"))
    _gx(["commit", "--allow-empty", "-q", "-m", "feature"], wt_path)
    _gx(["merge", "--no-ff", "-q", "-m", "merge", "codex/wt-merged"], repo)

    orig_load = _core.load_session
    orig_resolve = _core.resolve_pane_id
    orig_release = _core.release_agent
    orig_close_pane = _core.close_pane
    orig_delete = _core.delete_session
    rec = {"session": "cdx-wtm", "terminal_id": "tt", "mode": "tab",
           "workspace_id": "w1", "keep": False, "keep_worktree": False,
           "worktree": {"repo": repo, "branch": "codex/wt-merged",
                        "path": wt_path, "caller_branch": "main", "keep": False}}
    captured = {}
    try:
        _core.load_session = lambda s: rec
        _core.resolve_pane_id = lambda r: "w1-10"
        _core.release_agent = lambda p: None
        _core.close_pane = lambda p: None
        _core.delete_session = lambda s: None
        # Capture stdout to read the verdict envelope
        import io
        buf = io.StringIO()
        sys_stdout = sys.stdout
        sys.stdout = buf
        try:
            class EndArgs:
                session = "cdx-wtm"
            assert codex.cmd_end(EndArgs) == 0
        finally:
            sys.stdout = sys_stdout
        envelope = json.loads(buf.getvalue())
        wt_summary = envelope["result"]["worktree"]
        assert wt_summary and wt_summary["kept"] is False and wt_summary["removed"] is True
        assert wt_summary["branch_deleted"] is True
        # Filesystem + git ref verification
        assert not os.path.exists(wt_path), "worktree dir should be gone"
        out = subprocess.check_output(["git", "-C", repo, "branch", "--list", "codex/wt-merged"]).decode()
        assert out.strip() == "", "branch should be deleted"
        print("ok   worktree_merged_end: merged+clean worktree removed and branch deleted")
    finally:
        _core.load_session = orig_load
        _core.resolve_pane_id = orig_resolve
        _core.release_agent = orig_release
        _core.close_pane = orig_close_pane
        _core.delete_session = orig_delete
finally:
    shutil.rmtree(repo, ignore_errors=True)

# Regression: worktree kept when the branch has unmerged commits (or is dirty).
repo = tempfile.mkdtemp(prefix="codex-wt-unmerged-")
try:
    _gx(["init", "-q", "-b", "main"], repo)
    _gx(["commit", "--allow-empty", "-q", "-m", "init"], repo)
    wt_path = _core.worktree_create(repo, "codex/wt-unmerged", os.path.join(repo, ".worktrees", "codex-wt-unmerged"))
    _gx(["commit", "--allow-empty", "-q", "-m", "unmerged-feature"], wt_path)
    # Deliberately NOT merging.
    orig_load = _core.load_session
    orig_resolve = _core.resolve_pane_id
    orig_release = _core.release_agent
    orig_close_pane = _core.close_pane
    orig_delete = _core.delete_session
    rec = {"session": "cdx-wtu", "terminal_id": "tt", "mode": "tab",
           "workspace_id": "w1", "keep": False, "keep_worktree": False,
           "worktree": {"repo": repo, "branch": "codex/wt-unmerged",
                        "path": wt_path, "caller_branch": "main", "keep": False}}
    try:
        _core.load_session = lambda s: rec
        _core.resolve_pane_id = lambda r: "w1-10"
        _core.release_agent = lambda p: None
        _core.close_pane = lambda p: None
        _core.delete_session = lambda s: None
        import io
        buf = io.StringIO()
        sys_stdout = sys.stdout
        sys.stdout = buf
        try:
            class EndArgs:
                session = "cdx-wtu"
            assert codex.cmd_end(EndArgs) == 0
        finally:
            sys.stdout = sys_stdout
        envelope = json.loads(buf.getvalue())
        wt_summary = envelope["result"]["worktree"]
        assert wt_summary and wt_summary["kept"] is True
        assert wt_summary["ahead"] == 1 and wt_summary["dirty"] is False
        assert wt_summary["reason"] == "unmerged_commits"
        # Filesystem + git ref verification — both survive
        assert os.path.exists(wt_path), "worktree dir should be kept"
        out = subprocess.check_output(["git", "-C", repo, "branch", "--list", "codex/wt-unmerged"]).decode()
        assert "codex/wt-unmerged" in out, "branch should be kept"
        print("ok   worktree_unmerged_end: unmerged worktree kept, branch preserved, reason reported")
    finally:
        _core.load_session = orig_load
        _core.resolve_pane_id = orig_resolve
        _core.release_agent = orig_release
        _core.close_pane = orig_close_pane
        _core.delete_session = orig_delete
finally:
    shutil.rmtree(repo, ignore_errors=True)

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
