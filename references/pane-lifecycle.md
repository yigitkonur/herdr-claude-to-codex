# Pane lifecycle — spawning, structure, closing

How panes come into being and how they leave. Operationally important; mechanism-wise simple.

## How panes are born

Four entry points:

```
pane split       <id> --direction right|down [--cwd PATH]            # split an existing pane
tab create       [--workspace ID] [--cwd PATH] [--label TEXT]        # new tab + auto root pane
workspace create [--cwd PATH] [--label TEXT]                          # new workspace + auto tab + auto pane
agent start      <name> --split right|down -- <cli-args...>         # split + run, all-in-one — most common for sub-agents
```

For Claude Code multi-agent use, **`agent start` is the canonical path**:

```bash
herdr agent start codex-worker --split right --no-focus -- codex
```

This single call:
1. Splits an existing pane (the focused one, or the current one if you're inside herdr)
2. In the new pane, runs `codex` (the bash command after `--`)
3. The integration hook will fire when Codex starts, registering the new pane as an agent
4. Returns the new pane's metadata in the JSON response

Capture the `pane_id` from the response:

```bash
RESP=$(herdr agent start codex-worker --split right --no-focus -- codex)
PANE=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
```

## `--split` direction

- `right` — new pane appears to the right of the existing one (vertical split, horizontal layout)
- `down` — new pane appears below (horizontal split, vertical stack)

Pick one. Most users prefer `right` for a "main + helper" layout. `down` is useful for log/output panes underneath an active one.

## `--no-focus` matters for Claude Code

By default `agent start` focuses the new pane. **You almost never want this from a Bash subprocess** — focus is a UI concept (which pane gets keyboard input from the human). Use `--no-focus` to leave the human's focus where it was.

```bash
herdr agent start helper --split right --no-focus -- codex   # safe; doesn't steal focus
```

If you do want to focus the new pane (rare; usually because you want the human to see it light up), omit `--no-focus`.

## `--cwd` — where the new pane starts

By default the new pane inherits the parent pane's working directory. Override:

```bash
herdr agent start worker --split right --no-focus --cwd /Users/me/project -- codex
```

The shell will start in `/Users/me/project`, and the agent inherits this cwd. Useful when delegating to an agent that should work in a specific project root.

The pane's `cwd` field in `pane get` will track changes if the shell supports OSC 7 (modern zsh yes, default bash no). For agents, the cwd at spawn time is what matters; later `cd` commands inside the pane may or may not be reflected in the field.

## `--workspace` / `--tab` (rare)

You can target a specific workspace or tab instead of the focused one:

```bash
herdr agent start helper --workspace w6522... --tab w6522...:1 --split right --no-focus -- codex
```

Useful only when scripting multi-workspace setups. Most flows live in one workspace; defaults are fine.

## Layout in a real session

After a few spawns, your pane tree might look like:

```
workspace
└── tab 1
    ├── pane A (Claude Code — you)
    ├── pane B (Codex)         ← spawned by you
    ├── pane C (Pi)            ← spawned by you
    └── pane D (build log)
```

Layout (which is next to which) follows the order you split things. You don't need to micromanage; herdr places them sensibly.

## How panes die

Two paths:

1. **Shell exit** — when the shell process in the pane exits (`exit`, end of script, killed), herdr auto-cleans the pane. Pane disappears, `pane_id` becomes invalid. This is the normal "agent finished and left" path.

2. **`pane close`** — graceful shutdown initiated externally:
   ```bash
   herdr pane close <pane_id>
   ```
   Sends SIGHUP to the shell, which usually triggers a clean exit (the agent inside catches it and tears down). Pane disappears.

A sub-process inside the pane exiting (the agent CLI, say) does **not** kill the pane — only the shell exiting does. After `codex` exits, the shell prompt returns and the pane lives on with an idle shell. That's why "close pane when done with sub-agent" is a real step — leaving stale shells around accumulates.

## The right way to retire a sub-agent

```bash
# Option A — clean: tell the agent to exit, then close the pane
herdr pane run $PANE "/exit"                # most TUIs accept /exit
sleep 1                                      # let it finish
herdr pane close $PANE                       # belt and suspenders

# Option B — fast: just close the pane
herdr pane close $PANE                       # SIGHUP cascades; agent dies cleanly
```

Both are acceptable. Option B is one fewer call; Option A is more polite to agents that might be persisting state on `/exit`.

## Closing in a multi-agent cleanup

```bash
for PANE in "$P1" "$P2" "$P3"; do
    herdr pane close "$PANE"
done
```

Bulk close. If a pane is already gone (shell exited on its own), you'll get `pane_not_found` — harmless, ignore.

## `pane.exited` vs `pane.closed` events

Two distinct events:

- `pane.exited` — fires when the **shell process inside the pane** exits (whether voluntary or from SIGHUP).
- `pane.closed` — fires when the **pane structure itself** is removed.

Typically both fire in quick succession (shell exits → server cleans up pane → both events emitted). In some edge cases (server delays, error paths), one can fire without the other. For your purposes treat them as the same event ("pane is gone").

Subscribe to either if you need to react to pane departures.

## Avoiding pane accumulation

It's easy to leave panes behind after a long Claude Code session, especially if you tend to spawn helpers reflexively. Periodic check:

```bash
herdr pane list | jq '.result.panes[] | {pane_id, label, agent, agent_status}'
```

Close anything you don't need. Old idle panes don't consume much, but they clutter the visual layout in the TUI.

## Special pane operations

```
pane rename <pane_id> <label> | --clear     # set or clear the UI label
pane focus  <pane_id>                        # move user's keyboard focus to it (rare from script)
```

`pane focus` is mostly for human attention-direction (signal "look here, something happened"). For automated flows, use sparingly — focusing a pane while the user is typing into another one is rude.

## `pane.report-agent` lifecycle interactions

If you've created **fake agents** via `pane report-agent` (e.g. registering a bash pane as an agent for testing), those registrations persist until you clear them or the pane dies. To clean while keeping the pane:

```bash
echo '{"id":"x","method":"pane.clear_agent_authority","params":{"pane_id":"<id>"}}' | nc -U ~/.config/herdr/herdr.sock
```

When the pane closes, all registrations auto-clean. See `fake-and-custom-agents.md`.

## Recovery patterns

### "I closed the wrong pane"

There's no undo. The pane is gone, the shell process is dead, in-flight work is lost. Spawn a fresh one with the same setup.

### "Pane status is unknown but I thought I spawned an agent"

Hook hasn't fired yet (registration delay) or it crashed. Wait 5–10 s; if still unknown after a `agent wait --status idle --timeout 15000`, the integration likely isn't installed or is broken. Verify:

```bash
herdr integration status
# Should show pi/claude/codex/etc. as "current"
```

### "Pane is at idle for a long time; did the agent die?"

`pane read` to see the current state of the screen. If the shell prompt is there (not the agent's TUI prompt), the agent process is gone — re-launch with `pane run $PANE <agent_cli>`. If the agent's prompt is there waiting for input, send the next task.

## Performance / scale

Each pane is a PTY + shell process + scrollback buffer (capped ~10 MB). Twenty panes is fine; 200 panes will start to feel heavy. Don't make spawning a habit — reuse a pane for follow-ups when possible.

## Cheatsheet

```bash
# Spawn an agent in a side pane (most common)
herdr agent start <label> --split right --no-focus -- <agent_cli>

# Spawn in a specific cwd
herdr agent start <label> --split right --no-focus --cwd /path -- <agent_cli>

# Spawn into a specific workspace/tab (rare)
herdr agent start <label> --workspace <ws> --tab <tab> --split right --no-focus -- <agent_cli>

# Split a non-agent pane (e.g. for a build/log)
herdr pane split <existing_pane> --direction right --cwd /path --no-focus

# Close cleanly
herdr pane close <pane_id>

# Verify what's still around
herdr pane list | jq '.result.panes[] | {pane_id, agent, label}'
```
