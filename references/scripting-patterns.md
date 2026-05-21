# Scripting patterns — `codex.py` in depth

`scripts/codex.py` is the single agent-facing Codex interface; `scripts/_core.py`
is its engine (socket RPC, the session registry, spawn/send/wait, the analyzer) and
is **not** agent-facing — never run it directly. Both are stdlib-only Python (no
pip, no network); `codex.py` prints one JSON envelope to stdout and is safe to
background via your Bash tool's `run_in_background: true`. Run it; don't read it
into context.

Invoke as `python3 ${SKILL_DIR}/scripts/codex.py …` where `${SKILL_DIR}` is this
skill's directory (e.g. `~/.claude/skills/skill-herdr`). If a relative path fails
to resolve, `cd` into the skill dir first or use the absolute path.

For anything beyond a single Codex (parallel fleets, other agents, custom tooling),
compose **raw herdr** commands directly — the substrate is documented across the
other `references/` files.

---

## `codex.py` — the single Codex interface

```
codex.py start  --task "<p>" [--plan] [--expect PATH]... [--cwd DIR] [--label NAME]
                [--marker STR] [--timeout 600] [--no-wait]
codex.py send   --session <id> --message "<p>" [--expect PATH]... [--timeout 600]
codex.py reply  --session <id> (--text "…" | --choice N | --approve | --reject)
                [--expect PATH]... [--timeout 600]
codex.py await  --session <id> [--expect PATH]... [--timeout 600]
codex.py status --session <id> [--expect PATH]...      # one-shot, no wait
codex.py end    --session <id>                          # close pane + delete state
codex.py sessions                                       # list live, prune dead
```

**The contract (audit-agentic-cli):** pure JSON on stdout (diagnostics on stderr),
one stable envelope, semantic exit codes, non-interactive, every verdict carries a
runnable `next_action.command`. The minimum call is `start --task "…"` — Python
injects the completion marker + clarify-don't-guess discipline; you supply no
markers and craft no prompt scaffolding.

**Envelope (stdout):**
```jsonc
{ "ok": true, "schema_version": "v1", "command": "start", "session": "cdx-3f9a",
  "result": {
    "state": "completed|awaiting_clarification|awaiting_approval|permission_gate|working|no_signal|exited",
    "reason": "marker_verified|marker_unverified|artifacts_present|free_text_question|"
              "multiple_choice|plan_approval|permission_request|working|timeout|no_signal|pane_gone",
    "summary": "<=200 chars", "plan": "<full plan — never truncated>",
    "questions": ["…"], "options": [{"key":"1","label":"…","recommended":true}],
    "marker_found": true, "artifacts": [{"path":"…","exists":true,"bytes":4561}],
    "transcript_tail": "<cleaned last message>",
    "next_action": {"intent":"answer|approve|choose|verify|wait|start|nothing","command":"…","why":"…"}
  }, "error": null }
// failure: { "ok": false, "result": null, "error": {"class","code","message","retryable","suggestion"}, ... }
```

**Exit codes:** `0` valid verdict (any `state` — read `result.state`); `2` usage;
`3` herdr env (`HERDR_DOWN`, retryable); `4` session/pane not found or dead; `5`
internal. Stdout is always exactly the envelope.

**Canonical loop:** background `start` → read the notification's verdict → run
`result.next_action.command` (also backgrounded if it blocks) → repeat until
`completed` → `end`. See SKILL.md for the state→action table; see
`codex-and-agents.md` for the verified Codex behaviors each `reason` rests on.

**`_core.py` (engine, not agent-facing).** Transport rides the vendored
`scripts/herdr_client/` package (Apache-2.0; see its `NOTICE`): `rpc()` wraps
`HerdrClient.request()` and `wait_for_settle()` uses its `Subscription`. Key pieces,
if you adapt the tool: `rpc`/`wait_for_settle` (over `herdr_client`),
`spawn_codex` (full-width tab + `wait_until_ready`),
`send_task_verified` (two-phase verified submit), `read_tail`/`read_screen`
(`recent` vs `visible`), the session registry keyed on **terminal_id** with
`resolve_pane_id` (heals slot renumbering), `analyze` (the classifier), and
`settle_and_analyze` (wait + render-lag re-check + resume-grace). Constants worth
knowing: `SETTLE_DELAY` (paint lag), `RECHECK_TRIES`, `NO_SIGNAL_GRACE` (resume
window), `REGISTER_TIMEOUT`. A deterministic analyzer regression test lives at
`scripts/test_analyze.py` (feeds crafted screen tails — no spawning; run it after
touching `analyze`).

**Verified end-to-end (live Codex):** `marker_verified`, `free_text_question` →
`reply --text` → complete, `plan_approval` (full untruncated plan + clean options)
→ `reply --approve` → complete, `marker_unverified`, cross-process session
continuity through a forced pane-slot shift, `end` cleanup, and the exit-code map.
