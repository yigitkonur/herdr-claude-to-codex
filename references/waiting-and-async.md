# Waiting and async — the background-wait pattern in depth

This is the file to read once thoroughly. Everything multi-agent in herdr hinges on `wait` working the way you expect.

## Two wait commands, one will trick you

```
herdr agent wait <target>           --status <idle|working|blocked|unknown>   [--timeout MS]
herdr wait      agent-status <id>   --status <idle|working|blocked|done|unknown> [--timeout MS]
```

They sound alike. They are not the same. **Use `agent wait` 99% of the time.**

### `agent wait` — hybrid, forgiving, race-safe

- Checks the **current** state first. If the pane is already at the target status, returns immediately.
- If not, listens for the next status-change event matching the target.
- For `--status idle` specifically, **`done` also satisfies it** — because internally `done` *is* idle (just with the seen-flag false). One enum, two API representations.
- Enum accepted: `idle | working | blocked | unknown`. **`done` is not in this enum** — and you don't need it, because asking for `idle` catches `done` too.

### `wait agent-status` — strict event-only, transition-only

- Does **not** check current state. Waits for the next event.
- The event must match the target exactly. `--status idle` does **not** accept `done`.
- Enum accepted: `idle | working | blocked | done | unknown`. `done` is valid here.
- Will time out if the desired transition simply never happens (e.g. waiting for `idle` after the pane is sitting at `done` — there is no transition event from `done` to `idle`, only a seen-flag flip).

If you ask for `idle` with `wait agent-status` after the agent has gone to `done`, you will wait forever (until timeout). That is the most common way users misuse this. **Don't.**

## Why `done` is not `idle` in events but is in `agent wait`

Internally the state machine has four values: `Idle | Working | Blocked | Unknown`. The fifth API value `Done` is *derived* — it means "internal state is Idle, but the human hasn't yet acknowledged this output (seen flag is false)". When the human visually focuses the pane, the seen flag flips to true and the API now reports `Idle`. **The internal state never changed**, so no `pane.agent_status_changed` event is emitted. `agent wait --status idle` matches on the internal state and is satisfied; `wait agent-status --status idle` matches on an API-level event and never sees one.

The practical rule: don't try to chase `done → idle`. That transition is invisible from an event standpoint. Treat `done` and `idle` as "the agent is no longer busy."

## The background-wait pattern (Claude Code's superpower)

When you call your Bash tool with `run_in_background: true`, the command runs detached. The harness emits a system-reminder notification **the moment that command exits** — with its stdout and stderr attached. For `herdr agent wait`, the command exits exactly when the target status is reached (or timeout).

```bash
# Run via Bash tool, with run_in_background: true
herdr agent wait <pane_id> --status idle --timeout 600000
```

This returns to you in milliseconds with a background task id. You're free to do anything. When the agent in `<pane_id>` reaches idle/done, the wait command exits, the harness fires the notification, and you can `pane read` the result.

**Result:** real async multi-agent coordination using nothing but bash and Claude Code's existing background-task notifications. No `Monitor` tool. No polling. No event loop in your head.

### Timeout sizing

| Task class | Timeout |
|---|---|
| Quick smoke test, short Q&A | `60000` (1 min) |
| Code edit, single file refactor | `300000` (5 min) |
| Multi-file refactor, test gen | `600000` (10 min) |
| Large build, big repo refactor | `1800000` (30 min) |
| Multi-hour offline job | `3600000` (1 hr; pick max you can tolerate) |

Pick generously — if it returns early because the agent finished, you saved time. If it hits timeout, you'll get the notification with the timeout error and can handle it.

**Exit codes (verified):** a wait that reaches its target status exits `0` → the harness notification reads "completed (exit 0)". A wait that **times out** exits `1` → notification reads "failed (exit 1)". This is the clean signal behind the blocked-vs-idle race: the winning watcher notifies as *completed*, the losing one eventually notifies as *failed* on timeout — that failure is expected, not an error. And remember: a wait returning means **the agent's turn ended**, which is not the same as the task being complete — it may have asked a question or shown a menu. Classify the screen afterward — check for your completion marker (for Codex, `scripts/codex.py` does this classification for you).

### One background wait per sub-agent

Each sub-agent gets its own background wait. Don't try to `wait` for multiple agents in one bash:

```bash
# DON'T: loses per-agent notification granularity
herdr agent wait $A --status idle --timeout 600000 && \
herdr agent wait $B --status idle --timeout 600000
```

Instead, two separate background Bash calls. Two `background_task_id`s. Two notifications. You handle each as it arrives.

### Cancellation

If you no longer need a background wait, you can use the harness's `KillShell` (or equivalent) on the background task id — the wait process dies, no notification fires (or a "killed" one does, depending on the harness). Or set a tight `--timeout` upfront; the wait will exit on its own.

## Race conditions and how to avoid them

### Race 1 — wait started before send (false positive)

```bash
# WRONG: backgrounding wait first
herdr agent wait $PANE --status idle --timeout 600000     # run_in_background — exits in 20ms
sleep 1
herdr pane run $PANE "task"
```

The wait checks current state, sees `idle` (no task yet), returns. You get a notification immediately. False positive. Your code thinks the task is done before it started.

**Fix:** always send first, then start the wait.

```bash
herdr pane run $PANE "task"          # foreground — starts the task
herdr agent wait $PANE --status idle --timeout 600000    # background — exits when done
```

### Race 2 — first `agent get` right after `agent start`

The integration hook (Codex/Pi/Claude/OpenCode/Hermes) needs ~3–5 seconds to spawn its agent and report itself for the first time. Before then, `agent get $PANE` returns `agent_not_found`.

**Fix:** after spawning, run one foreground `agent wait --status idle --timeout 15000`. This call uses the hybrid mechanism — it will block until the agent registers itself with state `idle` (the initial state after launch). After this returns, the agent is fully addressable.

```bash
PANE=$(herdr agent start codex-worker --split right --no-focus -- codex | python3 -c "...")
herdr agent wait $PANE --status idle --timeout 15000   # foreground; covers registration
# Now safe to query, send, etc.
```

### Race 3 — `wait --status working` for a fast agent

Some agents go from `idle → working → done` in under 50 ms. `wait agent-status --status working` is pure event-based — if you missed the transition, you wait until timeout.

**Don't try to confirm "it started working" with a wait.** The `agent wait --status idle` you launch right after the send will end when the agent finishes (whether or not you saw the working transition). Stop worrying about the intermediate state.

## Working-state edge cases

- An agent can transition `working → blocked → working → done` (permission prompt in the middle). Your `wait --status idle` will return only when it reaches the final idle/done.
- If you want to *intercept* the permission prompt, run a **second** background wait targeting `--status blocked`. Whichever exits first tells you what happened.

```bash
herdr pane run $PANE "destructive task"
# Background watcher 1: idle/done = agent finished (auto-accepted permission)
herdr agent wait $PANE --status idle --timeout 600000        # run_in_background
# Background watcher 2: blocked = permission prompt opened
herdr agent wait $PANE --status blocked --timeout 600000     # run_in_background
```

You'll get whichever notification fires first. Then handle:

- Blocked first: read the pane, send the answer, then start a new `--status idle` background wait.
- Idle first: the prompt didn't trigger or auto-accepted — done.

Cancel the loser when the winner fires (if your harness supports it), or just let it time out harmlessly.

## `wait output` — match a substring or regex in pane output

```
herdr wait output <pane_id> --match <text-or-pattern>
                            [--source visible|recent|recent-unwrapped]
                            [--lines N]
                            [--regex]
                            [--raw]      # don't strip ANSI before matching
                            [--timeout MS]
```

Backed by the `pane.output_matched` event. Useful for non-agent panes (build, test runner, dev server) where you can't use `agent wait`.

Example — wait for a build to finish (success or fail):

```bash
herdr wait output $BUILD --match "Build (succeeded|failed)" --regex --timeout 1800000
# run_in_background: true
```

When the regex appears in the pane's `recent` scrollback, the wait exits and you're notified.

## When to use polling instead of wait

Almost never. But:

- **`done → idle` (seen-flag flip)** — no event fires; if you need this transition, you must poll `agent get`. Don't need it for finish-detection.
- **`custom_status` updates** — possibly no event for status-only updates (not verified). If you need to react, poll.
- **`revision` changes** — `revision` stays at 0 in default integrations. Don't poll for revision changes; you'll wait forever.

For everything else, prefer event-driven wait.

## Combining with `events.subscribe` for multi-pane real-time

For watching many panes, `events.subscribe` over a Python socket is more efficient than many separate `wait` background tasks. See `events-and-subscribe.md`.

## Quick decision tree

| Situation | Command |
|---|---|
| Agent finishing one task, you want to keep working | `herdr agent wait $P --status idle --timeout 600000` (background) |
| Permission prompt might appear | Same plus a second background `--status blocked` watch |
| Pane is a build/test runner (not an agent) | `herdr wait output $P --match "..." --regex` (background) |
| Need to react to several pane-status changes in one stream | Direct `events.subscribe` over a long-lived socket (Python) |
| You're waiting on `agent_status` strictly equal to `done` event | `herdr wait agent-status $P --status done` (rare; you probably want `agent wait --status idle`) |

## Pre-flight rules of thumb

- Send (or `pane run`) first. Then start the wait.
- `agent wait --status idle` for finish detection (catches done).
- `agent wait --status blocked` for permission interception.
- `agent wait --status working` is event-only; only useful if you start it *before* the send and want to confirm start.
- `wait agent-status` for strict event-driven flows (rare).
- All long waits go through Bash with `run_in_background: true`.
- Pick `--timeout` honest to the task class; you'll get a notification either way.
