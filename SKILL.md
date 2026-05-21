---
name: skill-herdr
description: herdr sub-agent orchestration. ALWAYS invoke when delegating to a Codex sub-agent ("have codex do X", "run this in the background", "start this task", "send this payload", "continue this session") or to Pi/Claude/OpenCode/Hermes, running agents in parallel, spawning a side pane, waiting for an agent to finish, reading another agent's output, approving its prompts, or when the user mentions herdr (pane, agent, send, wait, run, split) or HERDR_PANE_ID is set. For Codex, drive everything through ONE tool — scripts/codex.py (start/send/reply/await/status/end/sessions): run it in the background, read its single JSON verdict, do result.next_action. Python encodes the spawn-readiness race, verified send, full-width capture, marker+verification completion, never-truncated plans, session continuity across pane-slot shifts, structured pause reasons (question vs plan-menu vs blocked widget), and cleanup. References cover the herdr substrate (namespaces, parallel fleets, raw IPC, traps) beyond a single Codex.
---

# herdr — drive sub-agents from Claude Code

## What this is (read this once)

You (Claude Code) can spawn another AI agent — **Codex** first-class, also Pi/Claude/OpenCode/Hermes — into a **side pane** managed by **herdr**, and drive it entirely from Bash.

For **Codex**, you only need **one tool: `scripts/codex.py`**. It absorbs every sharp edge (spawn timing, lost sends, "did it finish or just pause?", plan truncation, pane renumbering, cleanup) and hands you back **one structured JSON verdict**. The whole mental model is three steps:

1. Run a `codex.py` verb **in the background** (Bash tool, `run_in_background: true`).
2. When it exits, the harness **notifies you** with its stdout — one JSON envelope.
3. Read `result.state` and run `result.next_action.command`. Repeat until `state: completed`, then `end`.

You never parse a terminal screen, poll a status, or sequence raw sends. The Python layer did the babysitting; you make the decisions. Everything beyond a single Codex (parallel fleets, other agents, raw herdr primitives) lives in `references/`.

## Invoke this skill the moment any of these is true

- The user delegates work to Codex (or any agent): *"have codex do X"*, *"let pi handle that"*, *"run it in another pane"*, *"spawn a sub-agent"*.
- The user says *"in the background"*, *"start this task"*, *"send this payload"*, *"continue this session"*, *"wait for it to finish"*.
- The user wants two or more agents working at once.
- `HERDR_PANE_ID` is set in the environment (you're already inside a herdr session — `env | grep HERDR_PANE_ID`).
- The user types `herdr ...` and expects you to drive it.

## Verify herdr is alive first

```bash
herdr status              # server must be "running"
```

If it's not running, **tell the user** — you cannot start it from Bash (it needs an interactive TUI). Everything below assumes a live server on the default socket (`~/.config/herdr/herdr.sock`).

## Drive Codex with one tool — `codex.py`

`SKILL_DIR` is this skill's directory (e.g. `~/.claude/skills/skill-herdr`); use the absolute path if a relative one doesn't resolve.

| Verb | What it does | Maps to the user saying |
|---|---|---|
| `start --task "<p>"` | Spawn Codex full-width in its own tab, inject the task (+ auto marker + clarify-don't-guess discipline), wait, analyze → returns a `session` id + verdict | "start this task" / "run it in the background" / "have codex do X" |
| `send --session <id> --message "<p>"` | Send a follow-up to a live session, wait, analyze | "send this payload" / "continue this session" |
| `reply --session <id> (--text "…" \| --choice N \| --approve \| --reject)` | Answer an awaiting state (question / menu / plan / widget), wait, analyze | answering a question, approving a plan |
| `await --session <id>` | Re-enter the wait with no new input (e.g. after a longer task) | "is it done yet?" |
| `status --session <id>` | One-shot snapshot, **no** waiting | quick check |
| `end --session <id>` | Close the pane + delete state (graceful cleanup) | "we're done with it" |
| `sessions` | List live sessions; prune dead ones | "what's running?" |

**Minimum viable call** — `codex.py start --task "<plain language task>"`. No marker, no prompt engineering, no flags. Python injects the completion contract and waits.

**Common flags:** `--plan` (plan mode), `--expect <path>` (repeatable; artifacts to verify on completion), `--timeout <sec>` (default 600), `--cwd <dir>`, `--label <name>`, `--marker <STR>` (auto-generated if omitted), `--no-wait` (send and return immediately).

## The canonical loop (memorize this)

```bash
# 1. Start in the BACKGROUND (Bash tool, run_in_background: true). Returns a session id.
python3 ${SKILL_DIR}/scripts/codex.py start \
  --task "Refactor src/foo.py to remove duplication; keep behavior identical." \
  --expect src/foo.py --timeout 600

# 2. Keep working. When codex.py exits, the harness notifies you with its JSON verdict.
# 3. Read result.state and run result.next_action.command. For example:
#      completed              -> done. Run `end`.
#      awaiting_clarification -> reply --text "<answer>"  (questions are in result.questions)
#      awaiting_approval      -> reply --approve           (plan is in result.plan)
#      permission_gate        -> reply --approve / --reject
#      working (timeout)      -> await --session <id>      (give it more time)
#      no_signal              -> status --session <id> / read result.transcript_tail
# 4. After completion, clean up:
python3 ${SKILL_DIR}/scripts/codex.py end --session <id>
```

Between step 1 and the notification you are **free to do other work** — answer the user, edit files, start another Codex. The notification means "your sub-agent reached a decision point, come look." **Always background the blocking verbs** (`start`, `send`, `reply`, `await`) so you get the auto-notification instead of blocking your turn.

## The verdict — one JSON envelope (stdout only)

```jsonc
{ "ok": true, "schema_version": "v1", "command": "start", "session": "cdx-3f9a",
  "result": {
    "state":  "completed|awaiting_clarification|awaiting_approval|permission_gate|working|no_signal|exited",
    "reason": "marker_verified|marker_unverified|artifacts_present|reported_done|free_text_question|multiple_choice|plan_approval|permission_request|working|timeout|no_signal|pane_gone",
    "summary": "<=200 chars: what happened",
    "plan": "<full plan text — NEVER truncated — when present>",
    "questions": ["…"], "options": [{"key":"1","label":"…","recommended":true}],
    "marker_found": true, "artifacts": [{"path":"…","exists":true,"bytes":4561}],
    "transcript_tail": "<cleaned last message — chrome/MCP-noise stripped>",
    "next_action": {"intent":"answer|approve|choose|verify|wait|start|nothing",
                    "command":"python3 …/codex.py reply --session cdx-3f9a --approve", "why":"…"}
  }, "error": null }
```

Read the table, then act on `next_action`:

| `state` / `reason` | What it means | What to do |
|---|---|---|
| `completed` / `marker_verified` | Marker printed **and** every `--expect` artifact exists | Done. `end` the session. |
| `completed` / `marker_unverified` | Marker printed but a promised file is **missing** | Verify before trusting — read `transcript_tail`; maybe `send` a fix. |
| `completed` / `artifacts_present` | No marker, but expected files exist | Confirm content; usually done. |
| `completed` / `reported_done` | No marker/`--expect`, but the agent's last message reports it finished | Verify — read `transcript_tail`; pass `--expect` next time for a hard check. |
| `awaiting_clarification` / `free_text_question` | Codex asked a question (turn ended at idle) | `reply --text "<answer>"` — `result.questions` lists them. |
| `awaiting_clarification` / `multiple_choice` | A pick-one menu, or Codex's blocked "Question N/N … submit" widget | `reply --choice N` — `result.options` lists keys. |
| `awaiting_approval` / `plan_approval` | Plan-mode approval menu | `reply --approve` (or `--reject` to keep planning). Plan is in `result.plan`. |
| `permission_gate` / `permission_request` | Tool/command permission prompt | Read `transcript_tail`, then `reply --approve` / `--reject`. |
| `working` / `timeout` | Didn't settle within `--timeout` (still running) | `await --session <id>` (optionally a larger `--timeout`). |
| `no_signal` / `no_signal` | Turn ended with no marker/question/menu, and Codex didn't resume | Read `transcript_tail`; may be done without a marker, or stuck. `status` to recheck. |
| `exited` / `pane_gone` | The pane/process is gone | `start` a fresh task (no resume in v1). |

**Clarifications can chain.** Codex may ask several questions in a row — e.g. a free-text question, then a multiple-choice widget. Each `reply` can surface the *next* one (you'll get `awaiting_clarification` again with fresh `questions`/`options`). That's expected, not a swallowed answer — just keep following `next_action` until `state` is `completed` (or `awaiting_approval`).

**Exit codes** (the bash exit you see in the notification): `0` = a valid verdict was produced (any `state` — read `result.state`); `2` = usage error; `3` = herdr environment error (server down/socket missing — `error.code: HERDR_DOWN`, retryable); `4` = session/pane not found or died; `5` = internal. Stdout is **always** the single JSON envelope; diagnostics go to stderr.

## What the Python layer guarantees (so you don't re-learn it the hard way)

Every one of these was a live failure mode that `codex.py` now handles for you:

- **Spawn readiness.** Codex reports `idle` ~0.7s after spawn but isn't input-ready until ~2s (MCP handshakes / TUI paint); a task sent into that window is silently lost. `start` waits for a genuinely ready, stable composer.
- **Verified send.** Every submit is confirmed (composer cleared / Codex went `working`) and re-sent if it was eaten. Prompts are sent **single-line** (an embedded newline can submit early and strand the rest).
- **Full-width capture.** Codex runs in its **own full-width tab**; a narrow split hard-wraps and ellipsizes plans and option labels ("Yes, impleme…"), corrupting what gets parsed. Output is read from **scrollback** (`agent.read source:recent`), so long plans and end-of-task reports are captured in full, not just the ~37 visible lines.
- **Completion = marker AND verification.** The injected marker is matched only as a **standalone output line** (never the prompt echo), and `--expect` artifacts are checked on disk — so a marker echoed in the prompt or a "done" with a missing file can't fool you.
- **Plans are never truncated.** `result.plan` carries the entire plan block verbatim — it's continuity.
- **Pauses are classified, not dumped.** `idle`/`done` ≠ "complete": a turn ends the same way whether Codex finished, asked a question, or showed a plan menu. The analyzer distinguishes them (and Codex's blocked multiple-choice widget) and gives you `next_action`, instead of a bare `"blocked"`.
- **Transient idle blips.** Codex emits short idle blips **between work bursts** mid-task; `start`/`await` give it a grace window to resume before ever reporting `no_signal`, so you don't declare victory (or defeat) early.
- **Session continuity.** Sessions are keyed on the **stable terminal_id** and re-resolved every call, so closing one pane (which renumbers other panes' slot ids) never breaks another session's handle. Pass only the durable `session` id; come back later with `send`/`reply`/`await`.
- **Cleanup.** `end` closes the pane (which auto-closes its dedicated tab) and deletes session state; `sessions` prunes anything dead.

## Plan mode

`codex.py start --plan --task "…"` switches Codex to plan mode before sending the task. The verdict comes back `awaiting_approval` / `plan_approval` with the **full plan in `result.plan`** and the menu in `result.options`. Approve with `reply --approve` (selects "Yes, implement"), keep planning with `reply --reject`, or pick another path with `reply --choice N`. After approval, `await` rides the implementation through its work bursts to `completed`.

## How the background notification fires (the mechanism)

1. You call your Bash tool with a `codex.py` verb and `run_in_background: true`. It returns a `background_task_id`; your turn continues.
2. `codex.py` drives Codex through the herdr socket and **blocks** until Codex settles (or `--timeout`). The process is nearly idle while waiting.
3. On settle it prints the JSON envelope and exits. The harness sees the exit and **emits a notification** with that stdout — no polling on your side.
4. Exit `0` carries a verdict (any state, including `working`/`timeout`); a non-zero exit is a usage/env/not-found/internal error per the exit-code map above.

## Going beyond one Codex

`codex.py` is the perfected path for a **single Codex session** — the focus of this skill. When you need more — parallel fleets, other agents (Pi / Claude / OpenCode / Hermes), or custom tooling — drop to **raw herdr**, documented in `references/`. The whole substrate is there: the agent-vs-pane namespace, the send-keys vocabulary, the status model, events / subscribe, pane lifecycle, and the full CLI + hidden IPC. You compose the raw `herdr` commands yourself; `codex.py`'s heuristics (explained in `references/codex-and-agents.md`) are the reference for what robust orchestration has to handle.

## Reference index (load by name, only when relevant)

**Codex-specific (read these for the tool above):**
- **`codex-and-agents.md`** — everything verified live about driving Codex: the event→state mapping, the spawn-readiness window, `agent.read source:recent` vs `visible`, full-width-tab capture, single-line sends, the three pause shapes (question / plan-menu / blocked widget), idle-blips-between-work-bursts, YOLO permissions, and exactly which of these `codex.py` encodes. **Read before reasoning about a Codex verdict.**
- **`scripting-patterns.md`** — `codex.py` + `_core.py` internals and the verb/envelope/exit-code contract in depth.

**herdr substrate (for fleets, other agents, custom tooling):**
- **`architecture.md`** — server/client model, the workspace→tab→pane→terminal hierarchy, the five ID flavors, the JSON line-delimited protocol. *Read first if you lack the mental shape.*
- **`agent-vs-pane.md`** — the namespace split, target resolution, ambiguity errors, asymmetric rename. *Open on `agent_not_found` / `agent_target_ambiguous`.*
- **`status-model.md`** — internal 4-state vs API 5-state, `done = idle + unseen`, aggregation. *Open when `done` confuses you.*
- **`waiting-and-async.md`** — `agent wait` vs `wait agent-status`, the background-wait pattern, race analysis, timeout sizing. *Open for non-trivial waiting.*
- **`sending-input.md`** — `pane run` vs `send-text` vs `send-keys` vs hidden `pane.send_input`, full key vocabulary, newline/PTY semantics. *Open before multi-line / special keys / slash commands.*
- **`reading-output.md`** — `--source visible`/`recent`/`recent-unwrapped`, formats, scrollback limits. *Open when parsing output by hand.*
- **`events-and-subscribe.md`** — hidden `events.subscribe`/`events.wait`, the 13 event types, one-subscribe-per-socket rule. *Open for real-time multi-pane monitoring.*
- **`permission-handling.md`** — bypass / smart auto-approve / scoped allow / human-in-the-loop, key mappings. *Open when running an agent without permission bypass.*
- **`pane-lifecycle.md`** — `pane split`, `agent start` params, `--cwd`, shell-exit vs `pane close`, teardown. *Open when spawning/restructuring/cleaning panes by hand.*
- **`multi-agent-patterns.md`** — ten concrete recipes (delegate, parallel, pipeline, watcher, auto-approve, cross-review, routing, context offload, recursion, teardown). *Open when designing more than one-shot delegation.*
- **`fake-and-custom-agents.md`** — `pane report-agent`, registry behavior, `release_agent`/`clear_agent_authority` cleanup. *Open when building custom tooling or test fakes.*
- **`cli-and-ipc-reference.md`** — every CLI subcommand and IPC method (incl. the five hidden ones), request/response shapes, the API-discovery trick, error codes. *Open when a flag isn't obvious or you need raw IPC.*
- **`pitfalls-and-traps.md`** — silent failure modes with mechanism + recovery, and a diagnostic ladder. *Skim once early; return when something behaves strangely.*

## Hard rules

- **For Codex, reach for `codex.py` first** — it encodes the lessons below so you don't have to.
- **Background the blocking verbs** (`start`/`send`/`reply`/`await`) with `run_in_background: true` — that's what gives you the auto-notification.
- **Act on `result.next_action`, not on a screen scrape.** The verdict already classified the pause; `transcript_tail` is context, not the decision.
- **`idle`/`done` ≠ "complete."** Trust `state`/`reason`, not raw status. (This is why the marker discipline exists.)
- **Pass only the durable `session` id** across calls — never a captured pane_id (slots renumber).
- **Always `end` a session when finished** — leaking panes/tabs clutters the workspace.
- **Never `agent attach` from Bash**, never run two `events.subscribe` on one socket, never rename a pane to a reserved type name (`pi`/`claude`/`codex`/`opencode`/`hermes`) — details in `pitfalls-and-traps.md`.
- **If `herdr status` shows the server down, tell the user** — you can't start it from Bash.
