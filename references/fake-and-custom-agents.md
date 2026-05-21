# Fake agents, custom integrations, the agent registry

How to make any pane appear as an agent (useful for testing, custom tools, debug overrides), and how to clean up after.

## What "agent registry" means

A pane has an `agent` field that's `null` by default. When an integration hook fires `pane.report_agent` for the first time on a pane, the server registers that pane as an agent and sets `agent`, `agent_status`, etc. From that point on, `agent get`, `agent send <name>`, `agent wait` all work.

Five "real" integrations (`pi`, `claude`, `codex`, `opencode`, `hermes`) auto-fire their hooks. Anything else — including plain bash, vim, or any custom tool — does not. You can register manually.

## Manual registration: `pane report-agent`

```bash
herdr pane report-agent <pane_id> \
  --source <unique-id> \
  --agent <type-label> \
  --state idle|working|blocked|unknown \
  [--message TEXT]               # human note, optional
  [--custom-status TEXT]         # 32-char sidebar message, optional
  [--seq N]                      # monotonic sequence, optional
```

Example — register a bash pane as a custom agent:

```bash
herdr pane report-agent w6522ea4d2775bf-4 \
  --source my-bash-wrapper \
  --agent mytool \
  --state idle \
  --custom-status "Ready"
```

After this:

- `pane get` shows `"agent": "mytool"`, `"agent_status": "idle"`, `"custom_status": "Ready"`.
- `agent get mytool` works (returns the bash pane's metadata in agent form).
- `agent send mytool "..."` works (sends to the bash pane's PTY).
- `agent list` includes it.

## What this is good for

- **Testing**: simulate an agent in `idle`, `working`, or `blocked` for testing your orchestration code.
- **Custom integrations**: wrap a non-Anthropic/etc. CLI in a script that calls `pane report-agent` at each state change, and you've integrated it.
- **Debug overrides**: temporarily override a stuck agent's status to unblock something.
- **Mock blocked**: simulate a permission prompt for testing your auto-approver.

## Multi-source per pane

A pane can have multiple agent registrations from different sources:

```bash
herdr pane report-agent $PANE --source A --agent foo --state working
herdr pane report-agent $PANE --source B --agent bar --state idle
```

The pane now has two source-keyed registrations. `agent get` returns one of them (the most-recent or the highest-priority — exact rule is fuzzy in observation; don't rely on it). Real integrations all use distinct sources (`herdr:pi`, `herdr:claude`, etc.) so this rarely matters for production use.

When you create fake agents for testing, **use unique sources** (`test-source-1`, `test-source-2`) to avoid collisions with real integration registrations.

## State field — what's valid

```
idle | working | blocked | unknown
```

**Note `done` is not valid here.** `done` is API-derived from `idle + seen=false` and can't be set directly. To make a pane appear as `done` in the API, register as `idle` — the seen flag will be false initially, so the API shows `done`.

The other four states map directly:
- `working` → API shows `working`
- `blocked` → API shows `blocked` (your auto-approver might intercept)
- `unknown` → API shows `unknown`

## Cleaning up: `release_agent` and `clear_agent_authority`

The CLI **doesn't expose deregister**. There's no `pane unregister-agent`. Two hidden IPC methods do:

### `pane.release_agent` — remove one (source, agent) pair

```python
import socket, json
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/Users/you/.config/herdr/herdr.sock")
sock.sendall((json.dumps({
    "id": "x",
    "method": "pane.release_agent",
    "params": {"pane_id": "w...-4", "source": "my-bash-wrapper", "agent": "mytool"}
}) + "\n").encode())
```

### `pane.clear_agent_authority` — remove all sources (or one source's all)

```python
sock.sendall((json.dumps({
    "id": "x",
    "method": "pane.clear_agent_authority",
    "params": {"pane_id": "w...-4"}                         # all sources
}) + "\n").encode())

# Or only one source:
sock.sendall((json.dumps({
    "id": "x",
    "method": "pane.clear_agent_authority",
    "params": {"pane_id": "w...-4", "source": "my-bash-wrapper"}
}) + "\n").encode())
```

After clearing, the pane is back to `agent: null`, `agent_status: unknown`. `agent get` returns `agent_not_found`. Pane itself remains; only the agent registration is gone.

## Pane closing also clears

When a pane is closed (shell exits, `pane close`), all registrations associated with it are auto-cleaned. You don't need to release/clear before closing. Only when the pane will live on without the agent identity do you need explicit cleanup.

## Custom integration recipe

Build a wrapper around a custom CLI:

```bash
#!/bin/bash
# my-tool-with-herdr.sh — runs my-tool while reporting state to herdr

PANE_ID="${HERDR_PANE_ID:-}"
if [ -z "$PANE_ID" ]; then
    exec my-tool "$@"   # no herdr → run plain
fi

cleanup() {
    echo '{"id":"x","method":"pane.release_agent","params":{"pane_id":"'$PANE_ID'","source":"my-tool-wrapper","agent":"my-tool"}}' \
      | nc -U ~/.config/herdr/herdr.sock > /dev/null
}
trap cleanup EXIT

herdr pane report-agent $PANE_ID --source my-tool-wrapper --agent my-tool --state idle
herdr pane report-agent $PANE_ID --source my-tool-wrapper --agent my-tool --state working

my-tool "$@"
EXIT_CODE=$?

herdr pane report-agent $PANE_ID --source my-tool-wrapper --agent my-tool --state idle
exit $EXIT_CODE
```

Now `my-tool-with-herdr.sh` in a pane shows up as an agent, reports state, and cleans up.

## `--custom-status` — the sidebar string

A 32-character human-readable annotation visible in the herdr TUI sidebar next to the pane.

```bash
herdr pane report-agent $PANE --source X --agent Y --state working --custom-status "Step 3/10"
```

Limits:

- Trimmed of leading/trailing whitespace.
- Control characters stripped (no `\n`, `\t`, escape codes).
- Truncated to 32 chars.
- If empty after these, becomes `None` (visible as "no status").

To clear it: pass empty string:

```bash
herdr pane report-agent $PANE --source X --agent Y --state idle --custom-status ""
```

Real integrations rarely set `custom_status`. Best for progress bars, "step X/Y" indicators, "auth required" hints — any short context the user might want to see.

## `--seq N` — monotonic dedup

A custom integration with possible out-of-order reports (concurrent threads, retries) can pass `--seq N`. The server stores the last seq per (pane, source) and ignores any incoming report with a lower seq. Useful for race-resistant updates.

Default integrations don't use this; you mostly won't either.

## `--message TEXT` — log annotation

Free-form human-readable note attached to the report. Not exposed in standard `pane get` outputs (or only intermittently). Mostly for diagnostic/integration logs. Skip in normal use.

## Faking `blocked` for permission-handler tests

```bash
herdr pane report-agent $PANE --source fake-tester --agent fake-agent --state working
# ... wait a beat ...
herdr pane report-agent $PANE --source fake-tester --agent fake-agent --state blocked
# Now any background watcher on --status blocked will notify.
```

Useful for testing the auto-approver path without an actual permission-needing agent.

## Faking `done` (idle+unseen)

The seen flag is internal and can't be set directly. The way to fake `done` is to register `idle` and not visually focus the pane (which is the default for an automated flow). The API will show `done` until the human focuses.

For testing auto-approve and waits, register `idle` and observe — `wait --status idle` (via `agent wait`) will match immediately because the internal state is Idle.

## Don't accidentally hijack a real agent's registration

If you `pane report-agent` on a pane that already has a real agent running (Claude, say), your registration **adds** under a new source; the real one isn't deleted. But `agent get` may report your fake source's state instead of Claude's depending on which is more recent. This can confuse the real integration's behavior.

**Rule:** only `pane report-agent` on panes you control (just spawned, or empty bash panes you want to fake). Never on someone else's running agent.

## Tear-down checklist for tests / experiments

```bash
# Any panes you faked agents on:
for PANE in $FAKED_PANES; do
    echo '{"id":"x","method":"pane.clear_agent_authority","params":{"pane_id":"'$PANE'"}}' \
        | nc -U ~/.config/herdr/herdr.sock > /dev/null
done

# Or close the pane entirely:
herdr pane close $PANE
```

Stale fake registrations can confuse later sessions ("why is this bash pane in agent list?"). Always clean up.

## Common mistakes

- **`pane report-agent` with `--state done`** — fails; `done` is not a valid input state. Use `--state idle`.
- **Forgetting to clean up** — fakes pile up; `agent list` shows phantoms.
- **Using a hot source name** like `herdr:pi` — collides with the real Pi integration. Pick a unique-to-your-tool source.
- **Reporting on a pane mid-real-agent** — the real agent and your fake fight. Don't.

## Quick reference

```bash
# Register
herdr pane report-agent $PANE --source <unique> --agent <label> --state idle|working|blocked|unknown [--custom-status "..."]

# Update (same source, server merges)
herdr pane report-agent $PANE --source <unique> --agent <label> --state working

# Release (CLI doesn't expose; use IPC)
echo '{"id":"x","method":"pane.release_agent","params":{"pane_id":"'$PANE'","source":"<unique>","agent":"<label>"}}' | nc -U ~/.config/herdr/herdr.sock

# Wipe all sources on a pane
echo '{"id":"x","method":"pane.clear_agent_authority","params":{"pane_id":"'$PANE'"}}' | nc -U ~/.config/herdr/herdr.sock
```
