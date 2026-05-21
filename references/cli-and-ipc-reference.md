# CLI and IPC reference

Compact lookup. CLI commands first, then the 38 IPC methods (including the 5 not exposed by CLI). For *why* and *when*, go to the topic-specific references.

## CLI subcommands at a glance

```
herdr status [server|client]               # daemon status
herdr update                               # update binary
herdr server [stop|reload-config]          # daemon control
herdr session list|attach|stop|delete      # named sessions
herdr workspace list|create|get|focus|rename|close
herdr tab list|create|get|focus|rename|close
herdr agent list|get|read|send|rename|focus|wait|attach|start
herdr pane list|get|read|rename|send-text|send-keys|run|report-agent|split|close
herdr terminal attach
herdr wait output|agent-status
herdr integration install|uninstall|status
herdr config reset-keys
```

`<subcommand> --help` works for every level.

## Most-used commands (use this 90% of the time)

```bash
# Status check
herdr status

# Spawn an agent
herdr agent start <label> --split right|down [--cwd PATH] --no-focus -- <agent_cli_args>

# Send a command (text + Enter atomic)
herdr pane run <pane_id> "command"

# Send only special keys
herdr pane send-keys <pane_id> Enter | Tab | Up | Down | Left | Right | Esc | Backspace | C-c

# Read pane contents — PRINTS PLAIN TEXT to stdout (NOT JSON; never pipe to jq).
# (Raw IPC pane.read returns JSON nested at result.read.text. agent start returns
#  the pane at result.agent.pane_id; pane split returns it at result.pane.pane_id.)
herdr pane read <pane_id> --source visible|recent|recent-unwrapped [--lines N] [--ansi]

# Wait for an agent to finish (use with run_in_background: true).
# Exits 0 on success (target reached) -> notification "completed";
# exits 1 on timeout -> notification "failed". A wait returning means the TURN
# ENDED, not that the task is complete (could be a question/menu) — classify after.
herdr agent wait <target> --status idle|working|blocked|unknown --timeout MS

# Wait for output to match
herdr wait output <pane_id> --match "pattern" [--regex] --timeout MS

# Discover what's around
herdr pane list
herdr agent list

# Close a pane
herdr pane close <pane_id>
```

## Argument conventions

- `<pane_id>` accepts long form (`w...-2`), short form (`p_10`), or `terminal_id` (`term_...`).
- `<target>` (for `agent` commands) additionally accepts agent name or type — must be unique.
- `--cwd` overrides the inherited working directory.
- `--no-focus` keeps the user's UI focus where it was. **Use it for sub-agent spawns.**
- `--timeout MS` is milliseconds. `600000` = 10 minutes.
- All JSON responses go to stdout. Pipe to `jq` or parse in Python.

## The 38 IPC methods

API discovery via invalid method:

```bash
echo '{"id":"x","method":"INVALID","params":{}}' | nc -U ~/.config/herdr/herdr.sock
# Error message lists every valid method name.
```

### Workspace / tab / pane structure

```
workspace.create        workspace.list      workspace.get       workspace.focus
workspace.rename        workspace.close

tab.create              tab.list            tab.get             tab.focus
tab.rename              tab.close

pane.split              pane.list           pane.get            pane.read
pane.rename             pane.close          pane.focus
```

### Sending into panes

```
pane.send_text          # raw text, no Enter
pane.send_keys          # key sequence (limited vocabulary)
pane.send_input         # ★ HIDDEN — text + keys array, atomic
pane.run                # CLI-only convenience; equivalent to send_input with keys=["Enter"]
```

### Agent layer

```
agent.list              agent.get           agent.read          agent.send
agent.rename            agent.focus         agent.wait          agent.start
```

### Agent registry control

```
pane.report_agent       # register/update an agent on a pane
pane.release_agent      # ★ HIDDEN — remove one (source, agent) pair
pane.clear_agent_authority  # ★ HIDDEN — remove all (or one source's) registrations
```

### Events and waits

```
events.subscribe        # ★ HIDDEN — open streaming subscription
events.wait             # ★ HIDDEN — single-shot blocking wait
pane.wait_for_output    # underlies `wait output`; matches a substring/regex
```

### Server / integration

```
server.stop             server.reload_config
integration.install     integration.uninstall
```

The five ★ HIDDEN methods are usable via raw IPC. They're documented inline in other reference files (`sending-input.md`, `fake-and-custom-agents.md`, `events-and-subscribe.md`).

## JSON request format

Always one JSON object, terminated with `\n`:

```json
{"id":"<correlation>","method":"<dotted.name>","params":{...}}
```

Response is one JSON object, terminated with `\n`:

```json
// success
{"id":"<correlation>","result":{...}}

// error
{"id":"<correlation>","error":{"code":"<code>","message":"<text>"}}
```

For `events.subscribe`, the response is the ACK followed by streamed event objects:

```json
{"id":"<correlation>","result":{"type":"subscription_started"}}
{"event":"<event.type>","data":{...}}
{"event":"<event.type>","data":{...}}
...
```

(Events have no `id`.)

## Error codes

| Code | Cause |
|---|---|
| `pane_not_found` | Pane id doesn't exist |
| `agent_not_found` | Target not in agent registry |
| `agent_target_ambiguous` | Target matched 2+ panes; `message` lists candidates |
| `invalid_request` | Schema validation failed; `message` lists valid values |
| `invalid_key` | `send-keys` key not in vocabulary |
| `invalid_agent` | Empty/bad `--agent` in `pane report-agent` |
| `pane_send_failed` | PTY write rejected |

## Send a raw IPC call from bash

For the hidden five methods, easiest path:

```bash
echo '{"id":"x","method":"pane.send_input","params":{"pane_id":"'$PANE'","text":"/help","keys":["Enter"]}}' \
  | nc -U ~/.config/herdr/herdr.sock
```

Or `socat`:

```bash
socat - UNIX-CONNECT:/Users/you/.config/herdr/herdr.sock <<< '{"id":"x","method":"pane.list","params":{}}'
```

For event subscriptions (which need a persistent connection), use Python (see `events-and-subscribe.md`).

## Useful one-liners

```bash
# List all panes with their agent info
herdr pane list | jq '.result.panes[] | {pane_id, agent, agent_status, label}'

# Find a pane by label or agent type
herdr pane list | jq '.result.panes[] | select(.agent == "codex")'

# Get the current pane's pane_id (when running INSIDE herdr)
echo "$HERDR_PANE_ID"

# Show only "interesting" panes (blocked or new output)
herdr pane list | jq '.result.panes[] | select(.agent_status == "blocked" or .agent_status == "done")'

# Quick health check
herdr status && herdr pane list | jq -r '.result.panes | length' \
  | xargs -I{} echo "{} panes alive"
```

## Status enums (don't confuse them)

```
internal AgentState     (in events, agent wait):   Idle | Working | Blocked | Unknown
API AgentStatus         (in pane get, agent get):  Idle | Working | Blocked | Done | Unknown
```

`Done` is API-only, derived. See `status-model.md` for the rationale.

`agent wait --status` enum: `idle | working | blocked | unknown`
`wait agent-status --status` enum: `idle | working | blocked | done | unknown`
`pane report-agent --state` enum: `idle | working | blocked | unknown`

## Key vocabulary for `send-keys` (full list)

```
Enter, enter         Tab, tab         Esc, esc         Backspace, backspace
Up, up               Down, down       Left, left       Right, right
C-c, c-c, ctrl+c     <single ASCII character>
```

Everything else (any other Ctrl chord, all Alt/Meta/Shift modifiers, F-keys, Home/End/PageUp/PageDown/Delete/Insert) fails with `invalid_key`. The whole `keys` array atomically rejects if any single key is invalid.

## Source / format flags (for `pane read`)

```
--source visible            current viewport (small)
--source recent             scrollback, wrapped to physical lines
--source recent-unwrapped   scrollback, logical lines (best for parsing)

--format text               default; strips ANSI escapes
--format ansi  (or --ansi or --raw)   keeps ANSI escapes intact
```

## Common `agent start` invocations

```bash
# Codex on the right, don't steal focus
herdr agent start codex-worker --split right --no-focus -- codex

# Claude with permissions bypassed
herdr agent start helper --split right --no-focus -- "claude --dangerously-skip-permissions"

# Pi in a specific directory
herdr agent start pi-x --split down --no-focus --cwd /Users/me/proj-x -- pi

# A bash helper, not an agent
herdr pane split <existing_pane> --direction right --no-focus
# Or: agent start can run bash, but bash won't register as an agent.
```

## Performance hints

- CLI subprocess overhead: ~25 ms each call.
- Direct socket (Python) from a hot path: ~5 ms.
- Background wait: zero overhead while idle; only consumes a pipe and a tracked task id.
- Subscribe stream: zero overhead while idle; bandwidth is event-rate-limited.

For typical multi-agent flows, the CLI is fast enough. Direct IPC matters only when doing hundreds of calls per second (rare).

## Cheat: where each thing lives

| Need | File |
|---|---|
| Why `agent get` is different from `pane get` | `agent-vs-pane.md` |
| What `done` means | `status-model.md` |
| How background wait works with Claude Code | `waiting-and-async.md` |
| Send text vs run vs send-keys | `sending-input.md` |
| Parsing pane output | `reading-output.md` |
| `events.subscribe` details | `events-and-subscribe.md` |
| Handling `blocked` permission prompts | `permission-handling.md` |
| Spawning, splitting, closing | `pane-lifecycle.md` |
| Concrete multi-agent recipes | `multi-agent-patterns.md` |
| Making a bash pane look like an agent | `fake-and-custom-agents.md` |
| Silent failure modes and recovery | `pitfalls-and-traps.md` |
