# `agent` vs `pane` — the most important concept in herdr

If you internalize one thing from this skill, make it this. It's the single biggest source of "but why didn't that work?"

## The split

herdr exposes **two CLI namespaces** that look similar but address different things:

- `pane <verb> ...` — works on **every terminal pane**, whether or not anything special is running in it. Bash pane, vim pane, an unused shell — all addressable.
- `agent <verb> ...` — works on **the subset of panes that have reported themselves as agents**. Reporting happens automatically when Pi/Claude/Codex/OpenCode/Hermes integration hooks fire on first cycle; it does not happen for plain bash.

So:

| Call | Bash pane | Codex pane |
|---|---|---|
| `pane get $ID` | ✓ returns pane | ✓ returns pane |
| `agent get $ID` | ✗ `agent_not_found` | ✓ returns agent metadata |

Same pane id, two different namespaces, different results.

## When to use which

```
pane: structural and low-level
  - split, close, rename (label), list (all), get (any), read (any), send-text, send-keys, run, report-agent

agent: aware of the AI inside
  - list (only reporting agents), get, read, send (target-by-name), rename (name+label), focus, wait (status-aware), start (split+run combo)

both: send / read / get (different scopes; agent-scoped also accepts name targets)
```

Most of what you'll write uses `pane` for spawning/closing/sending and `agent wait` for status. The other agent verbs are conveniences.

## Why `agent get $BASH` fails

The agent registry is opt-in. A pane joins it only when something writes `pane.report_agent` to it. Built-in integrations do this automatically for the five supported CLIs. A bash pane never reports itself, so `agent get` rejects it with `agent_not_found`.

This is **not a bug** — it's how herdr keeps `agent list` clean: only real agents show up. If you want a bash pane to be agent-addressable, see `fake-and-custom-agents.md` (`pane report-agent`).

## `agent send` vs `pane send-text` — same byte, different lookup

Both call the same underlying byte-write into the pane's PTY. The difference:

- `pane send-text $PID "text"` — `$PID` must be a pane id (long or short) or terminal id.
- `agent send $TARGET "text"` — `$TARGET` can be a pane id, terminal id, or **agent name/type** (`pi`, `claude`, or a custom rename).

Use `agent send` when you want to address by name. Use `pane send-text` when you have an id and want to avoid the agent registry entirely. Behavior on the wire is identical.

## Target resolution — four layers, one match wins

When you pass a `<target>` to an `agent` command, the server tries to match it against, in order, all panes:

1. **terminal_id** (e.g. `term_abc...`) — unique AND **stable for the pane's whole life** (survives pane renumbering). Accepted by `agent` commands only.
2. **agent.name** (custom rename) — must be unique across panes; stable across renumbering.
3. **agent.agent** (type detected/reported, e.g. `pi`, `claude`) — must be unique across panes.
4. **pane_id** (long `w...-N` or short `p_X`) — unique at any instant, but the `-N` suffix is a **slot index that shifts when a lower-slot pane closes** (verified). Accepted by both `pane` and `agent` commands.

(Reminder: `pane` commands — `pane run/read/close/send-keys` — resolve ONLY pane_ids, not terminal_id/name. So terminal_id is your stable handle but only via `agent` commands; see traps F and G in `pitfalls-and-traps.md`.)

Then:
- 0 matches → `agent_not_found`
- 1 match → that pane is the target
- 2+ matches → `agent_target_ambiguous`, with a candidate list in the error

The resolver does **not** prefer one layer over another. It searches all four in parallel and counts.

### The ambiguity trap (very common)

```bash
herdr agent rename $PI "codex"           # pi's name is now "codex"
# Later you spawn codex:
herdr agent start codex-real --split right --no-focus -- codex   # this pane's agent.agent = "codex"

herdr agent send codex "hello"
# → ambiguous: pi pane (name=codex) AND codex pane (agent=codex) both match
```

The error payload contains a `candidates: ...` list. You can parse it and pick by status, cwd, or whatever heuristic suits the situation. But the cleaner fix is **don't rename to a reserved type name** in the first place.

**Reserved (avoid as rename labels):** `pi`, `claude`, `codex`, `opencode`, `hermes`.
**Safe naming patterns:** `task-build`, `worker-frontend`, `qa-claude`, `codex-A`, `pi-dev`. Any string that isn't a known type.

### Stable scripts: use ids, not names

Names are pleasant for short interactive use. For anything scripted — and **especially for multi-agent flows where panes can come and go** — use the pane id captured at spawn time. Names can become ambiguous as agents are added; ids cannot.

```bash
# Pleasant but fragile if you'll add more codex panes later
herdr agent send codex "..."

# Robust
herdr agent send w6522ea4d2775bf-2 "..."
```

## Asymmetric renames — a sub-trap

There are two rename commands that touch overlapping fields:

```
pane rename  <pane_id>  <label>|--clear     # sets ONLY pane.label
agent rename <target>   <name> |--clear     # sets BOTH agent.name AND pane.label
```

So `agent rename` does *more* than `pane rename`. If you want only the UI label (no impact on target resolution), use `pane rename`. If you want target-addressability by name (and don't mind the sidebar label changing), use `agent rename`.

After `agent rename $PANE "worker-a"`:
- `agent.name = "worker-a"` (now `agent send worker-a` works)
- `pane.label = "worker-a"` (visible in `pane list`)
- `agent.agent` (type) unchanged — still `pi` or whatever

After `pane rename $PANE "ui-label"`:
- `pane.label = "ui-label"`
- `agent.name` unchanged

`--clear` on either resets the field(s) that command touches.

## The full table

| Want to | Command |
|---|---|
| Spawn a new pane | `pane split` or `agent start` (combo) |
| Spawn a new pane and run an agent in it | `agent start <label> --split right --no-focus -- <agent_cli>` |
| Send literal text, no Enter | `pane send-text $PID "..."` or `agent send $TARGET "..."` |
| Send a command (text + Enter atomically) | `pane run $PID "..."` |
| Send special keys | `pane send-keys $PID <key>...` |
| Get pane status (works on bash too) | `pane get $PID` |
| Get agent status (only reporting agents) | `agent get $TARGET` |
| Read pane contents | `pane read $PID` (or `agent read $TARGET`) |
| Wait for an agent to finish | `agent wait $TARGET --status idle` |
| Wait for a pattern in output | `wait output $PID --match "..."` |
| Close a pane | `pane close $PID` |
| Make a bash pane appear as an agent | `pane report-agent $PID --source X --agent Y --state idle` |

## Verification recipe

Lost? Run these in order:

```bash
herdr pane list | jq '.result.panes[] | {pane_id, agent, agent_status, label}'
# Shows every pane, with which (if any) are registered as agents.

herdr agent list | jq '.result.agents'
# Shows ONLY agent-registered panes.
```

If a pane is in `pane list` but missing from `agent list`, no integration is running in it — `agent get`/`agent wait`/`agent send-by-name` will all fail. Either spawn an agent in it, or `pane report-agent` to fake one.

## Mental shorthand

- Pane = the container.
- Agent = a property the container can opt into.
- `pane` commands are about containers; `agent` commands are about that property.
- When `agent_not_found` appears: the container exists, but the agent property doesn't. Fix by running the agent inside it (or faking the property).
