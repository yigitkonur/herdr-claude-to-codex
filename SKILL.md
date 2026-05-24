---
name: skill-herdr
description: herdr sub-agent orchestration. ALWAYS invoke when delegating to a Codex sub-agent ("have codex do X", "run this in the background", "start this task", "send this payload", "continue this session") or to Pi/Claude/OpenCode/Hermes, running agents in parallel, spawning a side pane, waiting for an agent to finish, reading another agent's output, approving its prompts, or when the user mentions herdr (pane, agent, send, wait, run, split) or HERDR_PANE_ID is set. For Codex, drive everything through ONE tool — scripts/codex.py (start/send/reply/await/status/end/sessions) — run it in the background, read its single JSON verdict, do result.next_action. Python encodes the spawn-readiness race, verified send, full-width capture, marker+verification completion, never-truncated plans, session continuity across pane-slot shifts, structured pause reasons (question vs plan-menu vs blocked widget), and cleanup. References cover the herdr substrate (namespaces, parallel fleets, raw IPC, traps) beyond a single Codex.
---

# herdr — drive a Codex sub-agent from Claude Code

## What this is

You can spawn **Codex** (also Pi/Claude/OpenCode/Hermes) into a **herdr** side pane and drive it from Bash. For Codex, you need **one tool: `scripts/codex.py`** — it absorbs every sharp edge (spawn timing, lost sends, "finished vs paused?", plan truncation, pane renumbering, cleanup) and returns **one JSON verdict**. The whole model is three steps:

1. Run a `codex.py` verb **in the background** (Bash tool, `run_in_background: true`).
2. On exit, the harness **notifies you** with its stdout — one JSON envelope.
3. Read `result.state`; run `result.next_action.command`. Repeat until `completed`, then `end`.

You never scrape a screen, poll status, or sequence raw sends. Everything beyond one Codex (fleets, other agents, raw herdr) lives in `references/`.

## Invoke this skill when

The user delegates to Codex or another agent (*"have codex do X"*, *"let pi handle it"*, *"run it in another pane"*, *"in the background"*, *"send this payload"*, *"continue this session"*, *"wait for it to finish"*), wants two+ agents at once, types `herdr …`, or you're inside a herdr pane (`HERDR_ENV=1` / `HERDR_PANE_ID` set).

## First, verify herdr

`herdr status` must show **running**. If not, tell the user — you can't start it from Bash (it needs a TUI). Default socket: `~/.config/herdr/herdr.sock`. Inside a herdr pane, `HERDR_ENV=1` and `HERDR_PANE_ID` (e.g. `p_5`) are set.

## The one tool — `codex.py`

`SKILL_DIR` = this skill's dir (e.g. `~/.claude/skills/skill-herdr`); use the absolute path if a relative one doesn't resolve.

| Verb | Does |
|---|---|
| `start --task "<p>"` | Spawn Codex (own full-width tab) + inject task (auto marker + "ask, don't guess") + wait + analyze → returns a `session` id + verdict |
| `send --session <id> --message "<p>"` | Follow-up to a live session + wait + analyze |
| `reply --session <id> (--text "…" \| --choice N \| --approve \| --reject)` | Answer a question / pick an option / approve / reject + wait + analyze |
| `await --session <id>` | Re-enter the wait, no new input (e.g. after a longer task) |
| `status --session <id>` | One-shot snapshot, **no** wait |
| `end --session <id>` | Close the pane + delete state (cleanup) |
| `sessions` | List live sessions; prune dead ones |

**Minimum call:** `codex.py start --task "<plain-language task>"` — Python injects the completion contract; you supply no markers or prompt scaffolding.
**Flags:** `--plan` (plan mode) · `--expect <path>` (repeatable; verify artifacts on completion) · `--timeout <sec>` (default 600) · `--cwd <dir>` · `--label <name>` · `--slug <safe-name>` · `--isolated-space` · `--keep-isolated-space` · `--marker <STR>` · `--no-wait`.

## The canonical loop

```bash
# Background it (run_in_background: true) so the harness notifies you on exit.
python3 ${SKILL_DIR}/scripts/codex.py start \
  --task "Refactor src/foo.py to remove duplication; keep behavior identical." --expect src/foo.py
# Read the verdict, run result.next_action.command, repeat until state: completed, then:
python3 ${SKILL_DIR}/scripts/codex.py end --session <id>
```

Between launch and notification you're **free to do other work**. **Always background the blocking verbs** (`start`/`send`/`reply`/`await`) — `codex.py` blocks server-side until Codex settles, then prints the envelope and exits, which is what triggers the auto-notification.

## The verdict — one JSON envelope (stdout only)

```jsonc
{ "ok": true, "schema_version": "v1", "command": "start", "session": "cdx-3f9a",
  "result": {
    "state":  "completed|awaiting_clarification|awaiting_approval|permission_gate|working|no_signal|exited",
    "reason": "marker_verified|marker_unverified|artifacts_present|reported_done|free_text_question|multiple_choice|plan_approval|permission_request|working|timeout|no_signal|pane_gone",
    "summary": "<=200 chars", "plan": "<full plan text — NEVER truncated — when present>",
    "questions": ["…"], "options": [{"key":"1","label":"…","recommended":true}],
    "marker_found": true, "artifacts": [{"path":"…","exists":true,"bytes":4561}],
    "transcript_tail": "<cleaned final message — chrome/noise stripped, bounded>",
    "next_action": {"intent":"answer|approve|choose|verify|wait|start|nothing","command":"…","why":"…"}
  }, "error": null }
```

Act on `next_action`, per state/reason:

| `state` / `reason` | What it means | What to do |
|---|---|---|
| `completed` / `marker_verified` | Marker printed **and** every `--expect` artifact exists | Done. `end`. |
| `completed` / `marker_unverified` | Marker printed but a promised file is **missing** | Verify — read `transcript_tail`; maybe `send` a fix. |
| `completed` / `artifacts_present` | No marker, but expected files exist | Confirm content; usually done. |
| `completed` / `reported_done` | No marker/`--expect`, but the agent's last message reports it finished | Verify — read `transcript_tail`; pass `--expect` next time for a hard check. |
| `awaiting_clarification` / `free_text_question` | Codex asked a question | `reply --text "<answer>"` (questions in `result.questions`). |
| `awaiting_clarification` / `multiple_choice` | A pick-one menu, or the blocked "Question N/N … submit" widget | `reply --choice N` (keys in `result.options`). |
| `awaiting_approval` / `plan_approval` | Plan-mode approval menu | `reply --approve` (or `--reject`). Plan in `result.plan`. |
| `permission_gate` / `permission_request` | Tool/command permission prompt | Read `transcript_tail`, then `reply --approve` / `--reject`. |
| `working` / `timeout` | Didn't settle within `--timeout` (still running) | `await --session <id>` (optionally larger `--timeout`). |
| `no_signal` / `no_signal` | Turn ended with no marker/question/menu, no resume | Read `transcript_tail`; may be done w/o a marker, or stuck. `status` to recheck. |
| `exited` / `pane_gone` | The pane/process is gone | `start` a fresh task (no resume in v1). |

**Clarifications can chain** — Codex may ask several in a row (e.g. a free-text question, then a multiple-choice widget); each `reply` can surface the next one. That's expected, not a swallowed answer — keep following `next_action` until `completed` (or `awaiting_approval`).

**Exit codes:** `0` = valid verdict (any `state` — read `result.state`); `2` = usage; `3` = herdr env (`HERDR_DOWN`, retryable); `4` = session/pane not found/dead; `5` = internal. Stdout is **always** the single envelope; diagnostics go to stderr.

## What `codex.py` handles for you (each a live-verified failure mode)

- **Spawn readiness** — waits for a genuinely input-ready composer (a task sent during Codex's ~2s MCP/TUI init is silently lost).
- **Verified single-line send** — confirms each submit landed and re-sends if it was eaten; prompts are one line (an embedded newline can submit early).
- **Full-width capture** — Codex gets its own full-width tab (a narrow split mangles plans/option labels); output is read from scrollback, so long plans and end-of-task reports aren't truncated.
- **Structured naming / isolation** — optional `--slug` names the tab as `<caller-space>-<caller-tab>-<slug>`; `--isolated-space` creates an unfocused workspace for the run and `end` closes it unless `--keep-isolated-space` is set.
- **Completion = marker AND artifacts** — the marker matches only a standalone output line (never the prompt echo); `--expect` files are checked on disk.
- **Plans never truncated**; **pauses classified** (question / plan-menu / blocked widget), not dumped; **idle blips** between work bursts get a grace window before `no_signal`.
- **Session continuity** — keyed on the stable terminal_id and re-resolved each call, so pane-slot renumbering never breaks a handle; pass only the durable `session` id.
- **Bounded, clean verdict** — `transcript_tail` is the agent's final message with chrome/MCP-noise/diff-log stripped, capped by lines **and** chars (no bloat).
- **Cleanup** — `end` closes the pane (and its dedicated tab) and deletes state; `sessions` prunes dead ones.

## Plan mode

`codex.py start --plan --task "…"` → `awaiting_approval`/`plan_approval` with the **full plan in `result.plan`** and the menu in `result.options`. `reply --approve` to implement, `--reject` to keep planning, `--choice N` for another path. After approving, `await` rides the build through its work bursts to `completed`.

## Beyond one Codex

For parallel fleets, other agents (Pi/Claude/OpenCode/Hermes), or custom tooling, drop to **raw herdr** — you compose the `herdr` commands yourself. Start with **`references/herdr-cli.md`** (the CLI surface: workspaces / tabs / panes / read / split / wait, from inside a pane); the full substrate (agent-vs-pane namespace, send-keys vocabulary, status model, events/subscribe, pane lifecycle, hidden IPC, traps) is the rest of `references/`.

## References (load by name, only when relevant)

- **`codex-and-agents.md`** — everything verified live about driving Codex: the *why* behind every verdict (event→state mapping, spawn-readiness, `agent.read recent` vs `visible`, full-width capture, single-line sends, the three pause shapes, idle-blips, YOLO permissions). **Read before reasoning about a Codex verdict.**
- **`scripting-patterns.md`** — `codex.py` + `_core.py` internals and the verb/envelope/exit-code contract in depth.
- **`herdr-cli.md`** — controlling herdr **itself** from inside a pane (workspaces/tabs/panes/read/split/wait) — the generic CLI surface and the gateway beyond one Codex. Confirms `HERDR_ENV=1`; links the [socket API](https://herdr.dev/docs/socket-api/).
- **herdr substrate** (for fleets/other agents/custom tooling): `architecture` · `agent-vs-pane` · `status-model` · `waiting-and-async` · `sending-input` · `reading-output` · `events-and-subscribe` · `permission-handling` · `pane-lifecycle` · `multi-agent-patterns` · `fake-and-custom-agents` · `cli-and-ipc-reference` · `pitfalls-and-traps`. Open the one that matches your problem; `pitfalls-and-traps.md` is the diagnostic ladder.

## Hard rules

- For Codex, reach for `codex.py` first, and **background** the blocking verbs (that's what gives you the notification).
- Use `--slug` for deterministic HERDR tab names; add `--isolated-space` only when you want a separate workspace per run.
- Act on `result.next_action`, not a screen scrape. `idle`/`done` ≠ "complete" — trust `state`/`reason`.
- Pass only the durable `session` id across calls (never a captured pane_id — slots renumber). Always `end` when finished.
- Never `agent attach` from Bash; never run two `events.subscribe` on one socket; never rename a pane to a reserved type name (`pi`/`claude`/`codex`/`opencode`/`hermes`) — see `pitfalls-and-traps.md`.
- If `herdr status` shows the server down, tell the user — you can't start it from Bash.
