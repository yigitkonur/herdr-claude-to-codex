#!/usr/bin/env python3
"""Deterministic unit test of _core.analyze across every state branch.
No spawning — feeds crafted screen tails and asserts (state, reason, next.intent).
Run: python3 scripts/test_analyze.py   (exit 0 = all pass)."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core

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

os.unlink(existing.name)
print(f"\n{'ALL PASS' if fails == 0 else str(fails)+' FAILED'} ({len(cases)} cases)")
sys.exit(1 if fails else 0)
