# Design: Monitor-driven Codex orchestration (`codex.py watch`)

Date: 2026-05-28 · Status: approved (4 design decisions confirmed) · Plugin: claude-code-herdr-plugin

## Motivation

Today `codex.py` is a one-shot model: background a blocking verb (`await`), get one
JSON verdict, react, re-invoke. The orchestrator drives the loop by hand and re-issues
waits. Claude Code's **Monitor tool** streams one notification per stdout line from a
long-running command — a far better fit for driving an interactive sub-agent. This
redesign turns the skill into an **event-driven** system: one watcher streams a verdict
per real state change; the orchestrator reacts (answer / approve / revise) with short
side commands; the watcher streams the next state.

Live testing against Codex **v0.134** also surfaced correctness bugs the watcher must
fix to be trustworthy (below).

## Confirmed decisions

1. **Architecture** — add a streaming `watch` verb (Monitor-driven, JSONL, one event per
   state change) as the new primary path. Keep `start/send/reply/await/status/end`.
2. **Autonomy** — auto-approve **permission gates** (Codex runs YOLO); **surface plans and
   questions** as events for the orchestrator to approve / revise / answer.
3. **Self-close** — on verified success (marker printed AND every `--expect` artifact
   exists) emit `completed` then **auto-close the pane**; `--keep` overrides.
4. **Auto-plan** — if the task contains the word `plan` (whole word, case-insensitive:
   `plan`/`plans`/`planning`), auto-engage `/plan` before the task. `--no-plan` overrides.

## Architecture

### `codex.py watch --session <id>` — the streaming watcher

A long-running, **read-only** process armed via the Monitor tool. It subscribes to the
pane's `pane.agent_status_changed` events and, on each *settled* transition, runs the
analyzer and emits **one compact single-line JSON envelope** (JSONL) to stdout — but only
when the state *signature* changes (state + reason + hash of questions/options/plan/
marker), so it never spams the same waiting state. After emitting an actionable state it
blocks on the event stream until Codex leaves `settled` (i.e. the orchestrator replied and
work resumed), then waits for the next settle. Terminal states (`completed`, `exited`)
stop the watch; `completed` + auto-close also closes the pane.

Coverage (Monitor "silence is not success"): the watcher emits on **every** terminal/
actionable state — `awaiting_clarification`, `awaiting_approval`, `permission_gate`
(only if not auto-approved), `completed`, `no_signal`, `exited`, `timeout` — never only
the happy path.

### Reply mechanism (works while `watch` runs)

The orchestrator reacts to a `watch` event by running a separate short command:
`codex.py reply --session <id> (--text|--choice|--approve|--reject)`. This sends keys to
the pane; the still-running `watch` observes the resume and streams the next state. This
requires the **lock fix**.

### Lock fix (required)

Today every verb holds one global `flock` for the whole command, including the multi-minute
wait — so `reply` cannot run while `watch`/`await` blocks, and fleets serialize. New model:
the lock guards only **herdr-mutating critical sections** (spawn, send-input), never the
read-only wait/watch. `watch` and `status` take **no** exclusive lock; `start/send/reply`
take it only around the actual input send. This unblocks reply-during-watch and parallel
sessions.

### Intelligence (the value a script adds over raw herdr)

- **Auto-plan** — `start` scans the task for the word `plan`; if present (and not
  `--no-plan`), runs `/plan` first so Codex plans before coding.
- **Plan-as-event** — when the "Implement this plan?" menu lands, the event carries the
  **full plan** (`result.plan`), the menu `options`, and `next_action` showing the exact
  approve / reject / revise commands. The orchestrator decides; the script presses keys.
- **Auto-approve gates** — a `permission_gate` is auto-approved by the watcher (send the
  approve key) and reported as an informational `auto_approved` event, not an actionable
  pause. Disable with `--no-auto-approve`.
- **Self-close** — `completed` + marker + all `--expect` present → close the pane and emit
  a final `ended` event, unless `--keep`.

### `--print-monitor-cmd` helper

`codex.py start ... --print-monitor-cmd` (and `watch --print-monitor-cmd`) emit, on
stderr or a dedicated field, the exact Monitor tool invocation (command + description +
timeout/persistent) so the orchestrator can arm the watch without hand-assembling it.

## Bug fixes folded in (live-confirmed on Codex v0.134)

1. **Plan-mode premature `no_signal`** — plan generation takes minutes and emits idle
   blips; the long plan + "Implement this plan?" menu paints after the old ~12s grace
   window, so `start --plan` returned `no_signal` and never captured the plan. Fix: the
   settle logic rides idle-blips and waits for an actionable state (menu/question/marker)
   far more patiently; in plan mode a bare turn-end is treated as "still painting".
2. **Stale-scrollback misclassification** — the analyzer read interactive state from deep
   `recent` scrollback, so a just-answered question/menu (still in the buffer during a
   mid-generation idle blip) was re-reported (observed: `reply --choice` returned
   `free_text_question` repeating the answered question). Fix: detect interactive state
   (questions / options / widget / plan-menu / marker) from the **visible screen**, while
   still capturing the full plan and transcript from `recent`.
3. **Lock** — see above; also fixes "can't `status` while `await` blocks".

Confirmed working (no change needed): `reply --approve/--choice` `send_keys` DO drive
v0.134 menus (the prior composer-text bug was composer-specific); the analyzer correctly
classifies the live plan-approval menu (`awaiting_approval`, full plan, options) and the
blocked multiple-choice widget (`multiple_choice`).

## CLI contract (per /audit-agentic-cli)

- Stdout: pure JSONL — one envelope per line for `watch`; single pretty envelope for
  one-shot verbs (unchanged). Stderr: diagnostics. Exit codes unchanged (0/2/3/4/5).
- Every `watch` line is the existing `{ok, schema_version, command, session, result,
  error}` envelope with `command:"watch"`; `result` is the analyzer block. New
  informational event kinds: `auto_approved`, `ended`.
- Each event carries `next_action.command` so the orchestrator always knows the next move.

## Components / files

- `_core.py` — analyzer: add visible-screen `screen` param scoping interactive detection;
  patience for plan/long-generation; `auto_approve` helper. Lock helpers.
- `codex.py` — new `watch` verb (event loop, signature de-dup, auto-approve, self-close,
  `--print-monitor-cmd`); `start` auto-plan detection + flags (`--no-plan`,
  `--no-auto-approve`, `--keep` already exists, `--print-monitor-cmd`); narrow the lock.
- `test_analyze.py` — new deterministic cases: stale-question, stale-menu, plan-mode
  patience signature, visible-vs-recent scoping, auto-plan keyword detection.
- `SKILL.md` + `references/` + `README.md` — teach the Monitor-driven flow as primary.

## Test plan (live, Codex v0.134, stated expectation before each)

- `2+2` → completes; with a file `--expect`, `marker_verified` + auto-close.
- "build a max-100-line HTML site for a dentist; ask me 2–5 questions" → `watch` streams
  `awaiting_clarification` events; answer via `reply --text`; ends `completed`.
- "build a 100-line HTML carpenter site but do a comprehensive **plan** first and present
  it" → auto-plan engages; `watch` streams `awaiting_approval` with full plan; `reply
  --approve` → builds → `completed` + auto-close.
- Concurrency: `status`/`reply` runs while `watch` is live (lock fix).

## Backward compatibility

Additive. Existing verbs keep working; existing sessions unaffected. Default behavior of
`start` gains auto-plan + auto-approve + self-close (documented; override flags provided).
Bump minor version.
