# Codex (and other agents) — verified behavior

Everything in this file was confirmed by driving a live Codex CLI (v0.132.0,
gpt-5.x) through herdr while authoring this skill. Where a finding generalizes
to Claude/Pi/OpenCode/Hermes it's noted; where it's Codex-specific it's flagged.

> **You normally don't apply any of this by hand — `scripts/codex.py` encodes it.**
> This file is the *why* behind that tool: the live-verified Codex behaviors its
> heuristics are built on. Read it to reason about a `codex.py` verdict, to debug
> a surprising state, or to drive Codex raw. The mapping of each behavior to the
> code is summarized in the last section, **"What codex.py automates."**

## How an integration maps agent events to herdr states

Each supported agent installs a hook that reports a state on certain events. For
Codex the mapping is exactly (from `~/.codex/hooks.json` → `herdr-agent-state.sh`):

| Agent event | herdr state reported |
|---|---|
| `SessionStart` | `idle` |
| `UserPromptSubmit` | `working` |
| `PreToolUse` | `working` |
| `PermissionRequest` | `blocked` |
| `Stop` (turn ended) | `idle` |

Read this table carefully — it explains every status you will ever see:

- A turn **starting** (you submit a prompt, or a tool fires) → `working`.
- A turn **ending for ANY reason** → `idle` (surfaced as `done` until seen).
- A **permission gate** specifically → `blocked`.

Claude's and Pi's hooks follow the same shape (start→working, end→idle, permission→blocked). The event *names* differ per agent but the three buckets are universal.

## The consequence you must internalize: `idle`/`done` ≠ "task complete"

Because **every** turn-end maps to `idle`, these four very different situations all report identical status:

| What actually happened | Status reported | How to tell them apart |
|---|---|---|
| Task fully finished | `idle`/`done` | Your completion marker is on screen |
| Agent asked you a question | `idle`/`done` | Screen ends with a question; no marker |
| Agent presented a choice menu (e.g. plan mode) | `idle`/`done` | Screen shows a numbered menu (`1.`/`2.`/`›`) |
| Agent gave up / errored | `idle`/`done` | Screen shows an error or apology; no marker |

**Verified timeline** (one Codex pane): sent "ask me 3 questions, then stop" → `working` at 1.4 s → `done` at 7.5 s. The `done` here meant *"I asked my questions and I'm waiting"* — the task had not started. Answering moved it `done → working` again. Only after the real build finished did `done` mean "complete" — and the only signal distinguishing the two `done`s was the `BUILD_COMPLETE` marker on screen.

This is why the canonical pattern always (a) bakes a marker into the prompt and (b) classifies after the wait (`codex.py`'s analyzer). Never equate a wait returning with the task being done.

## `/plan` mode — and why plan approval is `idle`, not `blocked`

Sending `/plan` to Codex switches it into Plan mode (shown bottom-right as "… Plan mode"). In plan mode, after you give a task, Codex produces a plan and shows:

```
Implement this plan?
› 1. Yes, implement this plan          Switch to Default and start coding.
  2. Yes, clear context and implement  Fresh thread.
  3. No, stay in Plan mode             Continue planning.
Press enter to confirm or esc to go back
```

**Verified:** this approval menu reports as `idle`/`done`, **NOT `blocked`.** A background `agent wait --status blocked` did *not* fire here; the `agent wait --status idle` watcher did. Mechanism: presenting a plan is a turn-end (`Stop` → `idle`), a conversational checkpoint — not a `PermissionRequest`.

So (refined after a second round of live testing — Codex has THREE wait shapes, not two):
- **Free-text questions and plan-approval menus** → `idle`/`done`. Navigate with `send-keys` (Enter selects the `›`-marked option 1; `Down`/`Up` for others; `Esc` to back out). `codex.py` → `awaiting_approval` (plan menu) or `awaiting_clarification` (question).
- **Structured multiple-choice clarifying questions** — Codex's `Question N/N … enter to submit answer` widget — report **`blocked`**, NOT idle/done. Verified live: in plan mode Codex asked "Which address should the footer use? 1. Use placeholder … 4. None of the above / enter to submit answer" and the pane status was `blocked`. Answer with `send-keys $PANE Enter` (submits the `›`-selected option) or `Down`/`Up` first. `codex.py` classifies this as `awaiting_clarification`/`multiple_choice` (still actionable — pick an option key via `reply --choice N`).
- **Tool/command permission gates** (`PermissionRequest`) → `blocked`. Same handling.

**Takeaway:** `blocked` ≠ "only a tool-permission gate." It also covers Codex's structured multiple-choice clarifying widget. Don't assume `blocked` means "dangerous command" — read the screen; it may just be a multiple-choice question. And a plan menu is the opposite — `idle`/`done`, not `blocked`. The reliable rule across all of them: a wait returning means a prompt is on screen; read it (or let `codex.py` classify it).

Driving the menu, verified: `herdr pane send-keys $PANE Enter` selected "Yes, implement this plan", and Codex transitioned to `working` and executed. The same `Enter` submits the multiple-choice widget's selected option.

## `blocked` is rarer than you'd think (Codex sandbox)

Codex runs ordinary shell commands inside its workspace **without** a permission prompt — verified: it ran `pwd`, `git status`, `wc -l`, file writes, all auto-approved, never going `blocked`. So in a default Codex setup, most "the agent is waiting on me" moments are `idle`/`done` with a question or menu, not `blocked`.

You'll see `blocked` mainly when Codex tries something outside its approval policy (e.g. acting outside the workspace, or with a stricter sandbox config). When in doubt, race a background `--status blocked` AND `--status idle` wait; whichever fires tells you which kind of pause it is.

## The screen-render lag gotcha (important for any screen-reading script)

**Verified:** the `pane.agent_status_changed` event can arrive a few hundred milliseconds **before** the TUI finishes painting a menu. If you read the screen the instant the status settles, you can catch a half-rendered screen — e.g. the plan text is there but the `1./2./3.` menu hasn't drawn yet, so a menu-detector misses it.

Fix (baked into `codex.py`): after the status settles, **wait ~0.8 s before reading the screen**. `_core.py` does this via a `SETTLE_DELAY`. If you write your own screen-reading logic, add the same small delay or you'll occasionally misclassify a menu as "nothing here."

## Spawning Codex through herdr

For Codex, use **`scripts/codex.py start`** — it spawns full-width and waits for a *genuinely* ready composer (see the two findings below). The raw `agent start` path still works for other agents and fleets, but for Codex it skips the two things that bite.

```bash
# Codex (recommended): full-width tab + readiness + verified send, all handled.
python3 scripts/codex.py start --task "…"

# Raw (other agents / debugging). NOTE the shape: agent start nests the pane
# under result.AGENT (pane split uses result.pane).
RESP=$(herdr agent start codex-worker --split right --no-focus -- codex)
PANE=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
```

Note: a freshly spawned Codex may print a harmless MCP startup warning (e.g. a failed local MCP server, or a 502 from a remote MCP). It does **not** affect orchestration — `codex.py` strips it from `transcript_tail`; if reading raw, ignore it.

### Finding 1 (critical): registration ≠ input-ready — a send in the init window is LOST

Codex's `SessionStart` hook fires almost immediately, so `agent wait --status idle` returns in **under a second**. But Codex is **not yet ready to accept input** then — it is still doing MCP handshakes and painting its TUI. **Verified:** a task sent in that window is silently swallowed; the pane sits at `working/timeout` with the prompt stranded in the composer, and nothing ever runs.

The reliable readiness signal is not "idle" — it's **the bottom status bar painted** (`gpt-5.x … Context N% used`) **AND the transient `Starting MCP servers` line gone, stable across two reads** (~2.1s in practice). `codex.py` gates every spawn on this (`wait_until_ready`). If you drive raw, don't trust the first idle — wait for the status bar and a stable composer, then verify your send actually landed (next finding).

### Finding 2: full-width tab, or your plans and menus get mangled

`agent start` splits the focused tab. With several panes sharing a tab (~28 cols), Codex **hard-wraps and ellipsizes** its output: plan steps wrap every few words and option labels truncate to `Yes, impleme…` — corrupting anything you parse. **Verified:** spawning Codex in its **own tab** and closing the leftover root shell gives it the full width (~130 cols), and plans/options render clean. `codex.py` does exactly this (`tab.create` → `agent.start` → `pane.close` the root shell). Reading is from **scrollback** (`agent.read --source recent`), not the ~37-line visible screen — see "Reading Codex output" below.

## Driving Codex once it's running

| You want | Do |
|---|---|
| Give it a task | `herdr pane run $PANE "…task… When done print TASK_DONE."` |
| Switch to plan mode | `herdr pane run $PANE "/plan"` (wait ~2 s; bottom-right shows "Plan mode") |
| Approve a plan menu | `herdr pane send-keys $PANE Enter` (selects option 1) |
| Decline a plan menu | `herdr pane send-keys $PANE Down Down Enter` (option 3 "stay in Plan mode") or `Esc` |
| Answer a question | `herdr pane run $PANE "your answer"` |
| Interrupt a long run | `herdr pane send-keys $PANE Esc` (Codex shows "esc to interrupt" while working) |
| See current screen | `herdr pane read $PANE --source visible --lines 40` (plain text) |

## `revision` stays 0 even though Codex sends a seq

Codex's hook sends a nanosecond `seq` with every `pane.report_agent`. Yet the pane's `revision` field stays `0` — verified for both Codex and Claude panes. So `seq` (used server-side for dedup/ordering) is not the same as the response's `revision`. **Don't use `revision` for change detection for any agent.** Use events / `agent wait`.

## Quick reference: provoking each state for testing

Want to exercise your orchestration logic against each state? These reliably produce them:

```bash
# working  -> send any task; it's working while generating
herdr pane run $PANE "count to 100 slowly"

# idle/done as a QUESTION -> ask it to ask you something
herdr pane run $PANE "Ask me one clarifying question, then stop and wait."

# idle/done as a MENU -> plan mode + a task
herdr pane run $PANE "/plan"; sleep 2; herdr pane run $PANE "Refactor foo.py"

# idle/done as COMPLETE -> task with a marker
herdr pane run $PANE "Print hello, then print MY_MARKER"

# blocked -> hard to force in default Codex sandbox; try an out-of-workspace op,
#            or run claude with normal (non-bypassed) permissions and a file write.
```

## Other agents — what differs

- **Claude (`claude`)** — same three-bucket mapping. Run sub-agent Claudes with `--dangerously-skip-permissions` to skip the `blocked` dance entirely (recommended for routine sub-tasks). Permission prompts (when not bypassed) show a numbered Yes/No menu, navigable like Codex's.
- **Pi (`pi`)** — same mapping; snappy turns. Slash commands and prompts behave like Codex.
- **OpenCode / Hermes** — same registry mechanism (their hooks report idle/working/blocked). Less battle-tested here; assume the same model and verify by subscribing to the pane's events (see `events-and-subscribe.md`) if a status surprises you.

When any agent's status surprises you, subscribe to the pane's events (`events.subscribe`; see `events-and-subscribe.md`) and drive it — the event stream shows exactly what the integration reported and when.

## Reading Codex output — `recent`, not `visible`

`pane.read`/`agent.read` accept three sources: `visible`, `recent`, `recent_unwrapped`. **Verified:** `visible` returns only the current screen (~37 lines), so a long plan or end-of-task report **scrolls off and is unrecoverable** — `pane.read` has no scrollback access (`scrollback`/`all`/`history` all error). `agent.read --source recent` returns the full recent transcript (hundreds of lines), which is how a complete plan gets captured. `recent_unwrapped` did **not** remove the visual wrapping in testing — width comes from the pane being full-width (Finding 2), not from this flag.

`codex.py` reads `recent` for analysis (full plans/reports) and `visible` only for the precise current-screen checks (composer state, readiness). When you read raw and need the whole thing, use `--source recent` with a generous `--lines`.

## Sending input — single line, and verify it landed

Two verified hazards when injecting a prompt into Codex's composer:

1. **Embedded newlines are dangerous.** A multi-line prompt can submit its first line early and strand the rest in the composer, splitting one task into two messages. **Send prompts as a single line** (`codex.py` collapses whitespace before sending). The completion instruction "print this token on its own line" still works — that governs Codex's *output*, not the input.
2. **A send can be eaten** during init churn even after the composer looks ready. Don't fire-and-forget: confirm it landed — Codex went `working`, **or** the composer no longer holds your text — and re-send if not. `codex.py` does this two-phase verified submit (`send_task_verified`); the giveaway of a lost send is your prompt sitting after the `›` composer glyph with status still idle.

## `idle` blips happen BETWEEN work bursts — don't conclude too early

**Verified:** while implementing a multi-step task, Codex emits short `idle`/`done` blips between bursts of work (and right after you approve a plan, the pane briefly still shows the *old* settled menu state before flipping to `working`). If you read at that instant you'll see "no marker, no question, no menu" and wrongly conclude the task ended (`no_signal`) — when in fact files were being written and Codex resumed seconds later.

The fix (baked into `codex.py`'s `settle_and_analyze`): on a bare turn-end with no marker/question/menu, **give Codex a grace window to resume `working`** before reporting `no_signal`; if it resumes, wait for the next settle. This is the deeper reason the completion **marker** matters — only the marker (or a verified artifact) distinguishes a real finish from a between-bursts pause. Never equate "a wait returned" with "the task is done."

## Codex permissions: YOLO mode auto-approves (so `blocked` is rare)

The Codex used here runs with `permissions: YOLO mode` (shown in its startup banner) — it auto-approves ordinary workspace shell commands and file writes, so a plain tool-permission gate (`PermissionRequest → blocked`) almost never fires. In a default Codex setup, most "waiting on you" moments are `idle`/`done` with a question or plan menu, or the **blocked multiple-choice widget** — not a command gate. Don't design around frequent permission prompts for Codex; do handle the widget (it reports `blocked`).

## Cleanup: close the pane, not the tab

Each `codex.py` session gets its **own tab** (Finding 2). **Verified:** closing a tab's sole pane **auto-closes the tab**, and **tab ids renumber** when a lower tab closes — exactly like pane-slot ids. So closing by a *stored* tab id is unsafe: it can hit a sibling session's renumbered tab and kill it. `end` therefore closes only the pane (resolved fresh via the stable terminal_id), which cleans up the tab safely. Same lesson as the pane-slot trap: never act on a stored slot/tab index; re-resolve from the stable terminal_id.

## What codex.py automates (behavior → code)

| Verified behavior (this file) | Where `codex.py`/`_core.py` handles it |
|---|---|
| Registration ≠ input-ready (lost sends) | `wait_until_ready` gates every spawn |
| Narrow split mangles plans/options | full-width tab spawn (`spawn_codex`) |
| `visible` truncates long plans/reports | reads `agent.read --source recent` |
| Newline-submits-early / eaten sends | single-line prompts + `send_task_verified` |
| `idle`/`done` ≠ complete; pause shapes | the `analyze` classifier → `state`/`reason`/`next_action` |
| Plan-approval menu is idle, widget is blocked | branch order in `analyze` (plan-menu → questions → options) |
| Idle blips between work bursts | resume-grace loop in `settle_and_analyze` |
| Marker echoed in prompt | marker matched only as a standalone output line |
| Completion needs verification, not just a keyword | marker AND `--expect` artifact checks |
| Render lag (event precedes paint) | `SETTLE_DELAY` + re-check loop |
| Plan menu / marker paints late after the turn-end | `no_signal` grace re-reads the SCREEN (not just status) |
| Plan mode documents the marker in a "Completion Signal" section | `_marker_on_own_line` rejects a marker framed by "print this token" |
| Approve redraws Plan→Default (blank transitional screen) | `await_started` waits for `working` before settling |
| Marker dropped on a multi-turn follow-up | `send`/`reply --text` re-inject a terse marker reminder |
| Pane-slot / tab-id renumbering | session keyed on terminal_id; `end` closes pane only |
| MCP/502 noise, startup banner box, prompt echo, internal-skill reads | all stripped from `transcript_tail` |
