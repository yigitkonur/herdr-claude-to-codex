# Scripting patterns — the bundled scripts in depth

All scripts in `scripts/` are stdlib-only Python (no pip, no network), print JSON
to stdout, take `--help`, and are safe to background via your Bash tool's
`run_in_background: true`. Each was validated live against Codex. Run them; don't
read them into context.

There are two layers:

- **`codex.py` — the single Codex interface (use this for Codex).** One tool, one
  JSON verdict, `next_action`-driven. It absorbs the operational complexity that
  the lower-level scripts leave to you. `_core.py` is its engine (socket RPC,
  session registry, spawn/send/wait, the analyzer) — **not** agent-facing; never
  run it directly.
- **The lower-level scripts (`spawn.py`, `await_done.py`, `wait_multi.py`,
  `auto_approve.py`, `watch.py`)** — the herdr substrate, for **parallel fleets,
  other agents (Pi/Claude/OpenCode/Hermes), and custom tooling**. Still valid;
  they're the layer underneath `codex.py`.

Invoke as `python3 ${SKILL_DIR}/scripts/<name>.py …` where `${SKILL_DIR}` is this
skill's directory (e.g. `~/.claude/skills/skill-herdr`). If a relative path fails
to resolve, `cd` into the skill dir first or use the absolute path.

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

**`_core.py` (engine, not agent-facing).** Key pieces, if you adapt the tool:
`rpc`/`wait_for_settle` (socket), `spawn_codex` (full-width tab + `wait_until_ready`),
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

---

# Lower-level scripts (fleets & other agents)

These predate `codex.py` and remain the right tool when you need **more than one
Codex**, a **non-Codex agent**, or **custom tooling**. For a single Codex session,
prefer `codex.py` above.

---

## `spawn.py` — create a sub-agent the right way

```
python3 scripts/spawn.py --label <unique> --split right|down [--cwd PATH]
        [--register-timeout 20] -- <agent cli and its flags...>
```

Removes two real papercuts:
- `herdr agent start` nests the new pane under `result.agent.pane_id` (not `result.pane` — that's `pane split`). Easy to read the wrong key.
- The integration hook needs ~3–5 s to register; query too soon and `agent get` returns `agent_not_found`. spawn.py polls until the agent registers.

**Output (stdout JSON):**
```json
{"pane_id":"w...-3","terminal_id":"term_...","agent":"codex","label":"codex-worker","registered":true}
```
Exit `0` registered, `1` spawned-but-registration-timed-out (pane_id still valid), `2` failure.

**Use it:** every time you create a sub-agent. Extract the id with
`... | python3 -c "import json,sys;print(json.load(sys.stdin)['pane_id'])"`.

Everything after `--` is the literal command for the pane, so flags pass through:
```bash
python3 scripts/spawn.py --label helper --split right -- claude --dangerously-skip-permissions
```

---

## `await_done.py` — wait, then CLASSIFY (the crown jewel)

```
python3 scripts/await_done.py <pane_id> [--marker TASK_DONE]
        [--timeout 600] [--tail-lines 25]
```

Encodes Rule 1 — `idle`/`done` ≠ complete. It waits for the pane to settle
(`idle`/`done`/`blocked`; hybrid, so an already-settled pane returns at once),
pauses ~0.8 s for the TUI to finish painting, reads the screen, and classifies:

| `outcome` | Meaning | Your move |
|---|---|---|
| `complete` | Your `--marker` is on screen | Done — move on |
| `waiting_question` | Screen ends in a question | Answer with `pane run $PANE "<answer>"` |
| `waiting_choice` | A numbered menu is shown (plan approval, etc.) | `pane send-keys $PANE Enter` (option 1) or navigate |
| `blocked` | Tool/command permission gate | See `permission-handling.md` |
| `idle_unclassified` | Turn ended, no marker, no question/menu | Read `.tail` and judge (often complete-without-marker, or gave up) |
| `timeout` | Didn't settle within `--timeout` | Investigate; agent may be stuck |

**Output (stdout JSON):**
```json
{"outcome":"waiting_question","status":"done","pane_id":"w...-2",
 "marker":null,"marker_found":false,"tail":"…last 25 visible lines…"}
```
Exit `0` settled (any outcome), `1` timeout, `2` bad args / pane gone.

**Use it:** every time you wait for a result. Background it; the notification
carries the JSON verdict. Always pass `--marker` matching whatever you told the
agent to print on completion.

**Verified outcomes:** `complete` (marker `DENTIST_DONE` found), `waiting_question`
(tail "…or a mix?"), `waiting_choice` (plan menu), `idle_unclassified` (finished
without a marker) — all reproduced live.

---

## `wait_multi.py` — one signal for a fleet

```
python3 scripts/wait_multi.py --mode any|all --status idle
        [--timeout 900] <pane_id> [pane_id ...]
```

For parallel work. `--mode any` returns on the FIRST pane to reach the status
(react to the earliest finisher); `--mode all` is a barrier (returns when every
pane is done). Uses ONE event socket — critical, since herdr silently breaks if
you open two subscriptions on one socket. Hybrid: panes already settled at start
count immediately. `--status idle` matches both `idle` and `done`.

**Output (stdout JSON):**
```json
// --mode any
{"mode":"any","done":["w...-3"],"pending":["w...-2"],"first":"w...-3","status":"done"}
// --mode all
{"mode":"all","done":["w...-2","w...-3"],"pending":[],"complete":true}
```
Exit `0` condition met, `1` timeout (`"timeout":true` + remaining `pending`), `2` bad args.

**Use it:** parallel fleets where you want a single notification rather than N
separate background waits. **Verified:** `--mode all` returned in 31 ms when both
panes were already idle; `--mode any` correctly reported the first finisher in a
live two-Codex race.

Note: `wait_multi` reports *which* pane(s) finished, not *why* — follow up with
`await_done.py` (or `pane read`) on the finisher to classify its outcome.

---

## `auto_approve.py` — unattended approval by rule

```
python3 scripts/auto_approve.py <pane_id>
        [--allow REGEX]... [--deny REGEX]... [--default allow|deny|escalate]
        [--approve-keys "Enter"] [--decline-keys "Esc"]
        [--loop] [--timeout 600]
```

Watches a pane; when it settles at a prompt — a permission gate (`blocked`) or a
choice menu (`idle`/`done` + numbered menu) — it reads the prompt, matches your
rules, and presses the key. **Safety: the default for an unmatched prompt is
`escalate` — it sends nothing and exits so you decide.** It presses keys only
when an explicit `--allow`/`--deny` rule matches.

Decision order: first matching `--deny` wins → first matching `--allow` → `--default`.
- `allow` → send approve keys (default `Enter` = option 1 / Yes)
- `deny` → send decline keys (default `Esc` = cancel)
- `escalate` → print the prompt, exit 3, hand it to you

`--loop` keeps handling prompts until the agent reaches a non-prompt idle (good
for a multi-step run that hits several gates). Without `--loop` it handles one
prompt and exits.

**Output (stdout JSON, one object per decision + a final summary):**
```json
{"event":"decision","action":"allow","matched":"Implement this plan","sent":["Enter"],"tail":"…"}
{"event":"summary","handled":1,"stopped_reason":"idle"}
```
Exit `0` clean finish, `1` timeout, `2` bad args, `3` escalated (prompt on stdout).

**Use it:** unattended runs that will hit approval prompts. **Verified:** with
`--allow 'plan|FAQ|tmp/' --loop`, it detected a plan-mode menu, matched, pressed
Enter to approve, and reported `handled=1, stopped_reason=idle` after Codex finished.

**Caution:** it presses keys based on heuristics + your regexes. Keep `--allow`
tight and prefer `--default escalate`. For genuinely routine sub-tasks, skipping
permissions at spawn (`claude --dangerously-skip-permissions`) is simpler and
safer than auto-approving.

---

## `watch.py` — observability

```
python3 scripts/watch.py [--timeout 300] [--json] <pane_id> [pane_id ...]
```

Streams `pane.agent_status_changed` plus pane lifecycle (`exited`/`closed`) events
with relative timestamps. Human format by default, `--json` for one object per
line. This is the instrument used to discover everything in `codex-and-agents.md`.

**Use it:** when a status surprises you, background `watch.py <pane>` and drive the
agent — the timeline shows exactly what the integration reported and when. Also
handy to confirm the working→done lifecycle of a task you're debugging.

---

## Composed recipes

### Delegate-and-classify (the everyday flow)
```bash
PANE=$(python3 scripts/spawn.py --label worker --split right -- codex \
       | python3 -c "import json,sys;print(json.load(sys.stdin)['pane_id'])")
herdr pane run $PANE "<task>. When fully done print TASK_DONE."
# background:
python3 scripts/await_done.py $PANE --marker TASK_DONE --timeout 600
# on notification: act on outcome (complete / waiting_question / waiting_choice / blocked)
```

### Parallel fan-out, react to first finisher
```bash
P1=$(python3 scripts/spawn.py --label a --split right -- codex | python3 -c "import json,sys;print(json.load(sys.stdin)['pane_id'])")
P2=$(python3 scripts/spawn.py --label b --split down  -- pi    | python3 -c "import json,sys;print(json.load(sys.stdin)['pane_id'])")
herdr pane run $P1 "Task A. Print A_DONE."
herdr pane run $P2 "Task B. Print B_DONE."
# background — one notification when the FIRST one finishes:
python3 scripts/wait_multi.py --mode any --status idle $P1 $P2 --timeout 900
# then classify the finisher:
# python3 scripts/await_done.py <first> --marker A_DONE  (or B_DONE)
```

### Long unattended run with auto-approval
```bash
herdr pane run $PANE "<big multi-step task that will hit approvals>. Print ALL_DONE."
# background — approve safe prompts, escalate anything unexpected, loop till idle:
python3 scripts/auto_approve.py $PANE \
    --allow 'tmp/|\.html|Implement this plan' \
    --deny  'rm |sudo|delete|drop table|--force' \
    --default escalate --loop --timeout 1800
# if it exits 3 (escalate), read its stdout and decide manually.
# when it exits 0 (idle), confirm completion:
python3 scripts/await_done.py $PANE --marker ALL_DONE --timeout 10
```

### Debug a confusing status
```bash
# background a watcher, then drive the agent in another call:
python3 scripts/watch.py $PANE --timeout 120 --json
herdr pane run $PANE "<the thing that behaves weirdly>"
# read the watcher's timeline to see the exact reported transitions.
```

## When NOT to use a script

- **One quick send + done-check** where you'll read the screen anyway → raw `herdr pane run` + `herdr pane read` is fine; no script needed.
- **Skipping permissions entirely** (sub-agent with `--dangerously-skip-permissions`) → no `auto_approve.py` needed.
- **A single sub-agent, single wait** → `await_done.py` still adds value (classification), but raw `agent wait --status idle` + a `pane read` works if you prefer.

The scripts earn their place by encoding non-obvious lessons (idle≠done, one-subscribe-per-socket, the spawn shape, the render lag) so you don't re-learn them the hard way. Reach for them whenever a flow is more than a single one-shot delegation.

## Adapting / extending

These are templates as much as tools. Common adaptations:
- `await_done.py` classification heuristics (the question/menu regexes) are conservative; tighten `MENU_RE` or add domain-specific markers if your agent's TUI differs.
- `auto_approve.py` key mappings (`--approve-keys`/`--decline-keys`) default to Enter/Esc; pass explicit keys if a particular agent's menu needs `Down`/`Up` navigation.
- All scripts read `HERDR_SOCKET_PATH` (env) or default to `~/.config/herdr/herdr.sock`; pass `--socket` to target a named session.

Read a script before adapting it — each is short (<200 lines) and commented at the decision points.
