# Status model — internal vs API, `done`, the seen flag

The status field looks like a five-value enum. It's really a four-value enum with a derived fifth value. This file explains why, and why that matters for designing waits.

## Two layers

**Internal `AgentState`** (in the server, in events):
```
Idle | Working | Blocked | Unknown
```

**API `AgentStatus`** (in `pane get`, `agent get`, event payloads):
```
Idle | Working | Blocked | Done | Unknown
```

`Done` is API-only. It's a *derivation* — there's no `Done` in the internal state machine.

## How `done` is derived

The server's mapping function (paraphrased from source):

```
(Internal, seen) → API
(Idle, false)    → Done       ← idle, but user hasn't acknowledged this output yet
(Idle, true)     → Idle       ← idle, user has acknowledged
(Working, _)     → Working
(Blocked, _)     → Blocked
(Unknown, _)     → Unknown
```

**`Done` = `Idle + seen_flag == false`.**

## What "seen" means

Every pane has a `seen` boolean. It flips to **false** when the agent transitions to Idle (new output you should look at). It flips to **true** when the human visually focuses the pane in the TUI.

If you're driving herdr from Bash and never opening the TUI, the seen flag stays false forever after an agent finishes. The API will keep saying `done`.

There is **no API endpoint** that sets seen=true. It's a UI signal only. Don't expect to be able to "mark done as seen" from your scripts.

## Why this matters for waits

### `agent wait --status idle` does the right thing

The internal state machine has only `Idle | Working | Blocked | Unknown`. `agent wait` operates against the internal state. When you ask for `--status idle`, the wait is satisfied when the internal state hits `Idle` — regardless of whether the API would show this as `Idle` or `Done`.

You don't need to ask for `done`; you can't even (the enum for `agent wait` doesn't include it). Just ask for `idle`.

### `wait agent-status --status idle` is strict and will trick you

`wait agent-status` operates against **API-level events** (`pane.agent_status_changed`). Its enum includes `done`. When the agent finishes:

- Internal: `Working → Idle`
- Event fires: `agent_status: done` (because seen is false)
- Later, when seen flips to true: **no event** (because internal state didn't change)

So `wait agent-status --status idle` will wait for an `idle` event that may never come. It will time out.

**This is the single most common misuse of waits.** When in doubt, use `agent wait --status idle` and stop thinking about `done`.

## The full table

| You want | Use |
|---|---|
| "Is the agent done with this turn?" | `agent wait --status idle` |
| "Did a permission prompt appear?" | `agent wait --status blocked` |
| "Did the output specifically show `done` (not just any finish)?" | `wait agent-status --status done` (rare) |
| "Confirm the agent started working" | Theoretically `agent wait --status working` started **before** the send — but for fast agents the transition is too quick and you'll miss it. Don't rely on this. |

## Workspace-level priority (sidebar attention)

When the herdr TUI shows a workspace's status, it picks the **highest-attention** state across all panes in that workspace. The priority order:

```
Blocked   (4) — highest
Done      (3) — = Idle+unseen, i.e. "new output, take a look"
Working   (2)
Idle      (1) — = Idle+seen
Unknown   (0) — lowest
```

So a workspace where one pane is Working and another is Done will be shown as **Done** — the unseen output dominates. This is the UX principle: "user attention should follow finished but unseen work over still-running work."

There is no event for workspace-level status changes; the TUI computes it on the fly. If your script needs to know "is any pane in workspace X done," poll the per-pane status or subscribe to per-pane events.

## What "Unknown" means

`Unknown` covers three distinct situations, indistinguishable from the API:

1. Pane has no integration hook running (plain bash) and isn't registered.
2. Pane has an integration but the hook hasn't fired its first report yet (early start, ~3–5 s).
3. Pane was previously an agent but the hook crashed or hasn't reported in too long.

If you see `Unknown` for a pane you expect to be an agent, **wait a few seconds** — case 2 is the most common. After 10 s if still Unknown, the integration probably isn't installed (case 1) or has crashed (case 3).

For Claude Code's purpose, always do one foreground `agent wait --status idle --timeout 15000` after spawning. That covers case 2 cleanly.

## What "Blocked" means

`Blocked` is set by the integration hook when the agent is waiting for human input that isn't a regular prompt — most often a **permission prompt** (Claude Code asking "approve this tool call?", Codex asking "okay to write this file?").

The hook detects "we're about to call a tool but need approval" and emits `pane.report_agent --state blocked`. When the human (or your auto-approver) responds and the agent proceeds, the hook resets to `working` or `idle`.

You can intercept `Blocked` with `agent wait --status blocked` (background it). On notification, read the pane and respond. See `permission-handling.md`.

## What "Working" means

Active processing — the agent is generating tokens, calling tools, doing things. No user input expected.

You almost never `wait` for `working` specifically (it's a transient state). The exception: a sanity check that the task actually started (run `wait --status working` **before** the send, foreground, short timeout). For most agents this is overkill; trust that `pane run` worked and just wait for `idle`.

## `custom_status` — a separate field, not part of the enum

The API also exposes `custom_status: Option<String>` — a 32-char human-readable status note set by `pane report-agent --custom-status "..."`. Used in the sidebar to display things like "Step 3 of 5" or "Refactoring auth".

It does **not** correspond to any enum value. The agent status enum is independent. `custom_status` is just an extra annotation.

Default integrations don't set `custom_status` heavily; mostly used for custom-built integrations. Read it with `pane get | jq .result.pane.custom_status`.

## `revision` — what about this field?

`revision: u64` is in every pane/agent response. Looks like a monotonic counter. **It isn't.** The default integration hooks don't send a `--seq` value, so `revision` stays at `0` forever for all real agents.

Don't poll for revision changes; you'll wait forever. Use `events.subscribe` (`pane.agent_status_changed`) for change detection.

A custom integration *can* increment `revision` if it passes `--seq N` to `pane report-agent`, and the server will then enforce monotonic dedup (out-of-order reports are dropped). Useful for race-resistant custom integrations; irrelevant for built-ins.

## Common confusions and one-line answers

> "Status is `done`. Why isn't `agent get` showing `idle`?"
Because the human hasn't focused the pane in the TUI. seen=false; API shows `done` until that changes.

> "I asked `wait agent-status --status idle` and it timed out."
Because `done → idle` doesn't emit an event. Use `agent wait --status idle`.

> "The agent's status is `unknown` right after I spawned it."
The integration hook hasn't fired yet. Wait ~3–5 s, or run `agent wait --status idle --timeout 15000` once.

> "Workspace shows `done` even though most panes are `working`."
Workspace status takes the max-attention priority. `done` (Idle+unseen) ranks above `working`.

> "Revision is always 0."
Default integrations don't increment it. Use events for change detection.

## Mental shorthand

- Internal status = ground truth.
- API status = internal + the seen flag.
- `done` = idle + nobody's looked at it.
- `agent wait --status idle` = the right thing to ask for, almost always.
- `wait agent-status --status idle` = the wrong thing, almost always.
