# Reading output — `pane read` deeply

The mirror of sending: getting bytes back out of a pane so you can react.

## The full signature

```
herdr pane read <pane_id>
  [--source visible | recent | recent-unwrapped]   default: recent
  [--lines N]
  [--format text | ansi]                           default: text
  [--ansi]                                          shorthand for --format ansi
  [--raw]                                           alias for --ansi (skips strip_ansi)
```

`agent read` is the agent-namespace twin — same flags, same output, but accepts agent names/types as the target.

## Three sources, three different shapes of the same data

| Source | What you get | Best for |
|---|---|---|
| `visible` | Only what's currently rendered in the viewport (the "screen") | TUI snapshots, "what does the agent show right now" |
| `recent` (default) | Scrollback included; **wrapped to physical lines** | Visually reproducing the pane (line breaks where the terminal broke them) |
| `recent-unwrapped` | Scrollback included; **logical lines** (long lines preserved) | Parsing, grep, log search, machine reading |

The same content can produce wildly different line counts. From a 200-line lorem-ipsum dump in an 80-column pane:

| Source | Lines |
|---|---|
| `visible` | 17 (one viewport) |
| `recent` | 621 (logical lines wrapped to physical lines, each long line ~3 rows) |
| `recent-unwrapped` | 212 (the actual lines + a few headers) |

For agent automation, `recent-unwrapped` is usually the right choice: long lines stay long, easy to `grep` for patterns, easy to find structured output (JSON, file paths) without dealing with wrap breaks.

## `--lines N` — tail-style

Limits the response to the **last N lines** of the chosen source. Defaults to the entire pane state (capped by the scrollback limit, ~10 MB).

```bash
herdr pane read $PANE --lines 30                  # last 30 of recent (default)
herdr pane read $PANE --source visible            # current viewport, no truncation
herdr pane read $PANE --source recent-unwrapped --lines 100
```

For "what did the agent just say", `--source visible --lines 40` is the workhorse — gives you the current screen and ignores history.

For "find a specific pattern in the run", `--source recent-unwrapped --lines 500 | grep '...'` is the workhorse.

## Format: `text` (default) vs `ansi`/`raw`

Default `--format text` strips all ANSI escape sequences before returning. Output is clean readable text — what you want for `grep`, parsing, presenting to the user.

`--ansi` / `--raw` / `--format ansi` (all three identical) preserve every escape sequence. Output looks like:

```
\x1b[31mERROR\x1b[0m: file not found
```

Useful only when:

- You're replaying the output into another terminal (and need the colors).
- You need to detect ANSI sequences themselves (rare).
- The TUI uses cursor positioning and other escape codes that you need to reconstruct screen state.

99% of the time, leave format on text.

## Output shape — CLI prints PLAIN TEXT (verified)

This is the one place herdr's CLI breaks its own JSON convention, and it trips people constantly:

**The CLI `herdr pane read` prints the pane's text DIRECTLY to stdout — not JSON.** Do **not** pipe it to `jq`; there is no JSON envelope. Capture it as-is:

```bash
OUTPUT=$(herdr pane read $PANE --source recent-unwrapped --lines 100)
echo "$OUTPUT" | grep "TASK_DONE"
```

(`herdr agent read` behaves identically — plain text.)

**The raw IPC method `pane.read` DOES return JSON, and nests the payload under `result.read` (not `result` directly):**

```json
{ "result": { "type": "pane_read", "read": {
    "pane_id": "w...-3", "source": "recent", "format": "text",
    "text": "...", "revision": 17, "truncated": false } } }
```

So from a raw socket you read `result.read.text`, e.g. `python3 -c "...json.load(...)['result']['read']['text']"`. From the CLI you just read stdout. `truncated: true` means the data exceeded what was returned (rare; 2 MB frame cap).

## Common usage recipes

### "What did the agent just reply?"

```bash
herdr pane read $PANE --source visible --lines 50
```

Get the visible viewport. The agent's last response is usually in the last ~20 lines (depending on the TUI). If you don't see what you expected, increase `--lines` (you might be reading before the screen has updated).

### "Find a structured marker in the log"

```bash
# Agent was told to print DONE or ERR when finished
output=$(herdr pane read $PANE --source recent-unwrapped --lines 200)
if echo "$output" | grep -q "DONE"; then ...; fi
```

### "Tail-style live monitoring"

Polling is wasteful; use `wait output` instead:

```bash
herdr wait output $PANE --match "Build succeeded" --timeout 600000    # run_in_background
```

When the substring appears in the pane's recent buffer, the wait exits and the notification fires. Then `pane read` to confirm and pull surrounding context.

### "Diff what changed between two snapshots"

```bash
SNAP1=$(herdr pane read $PANE --source visible)
# ...wait...
SNAP2=$(herdr pane read $PANE --source visible)
diff <(echo "$SNAP1") <(echo "$SNAP2")
```

Rarely needed; subscribe-driven flows are usually cleaner.

## Scrollback limit

Per-pane scrollback is capped at **~10 MB** (configurable, but the default). Older lines are discarded. For very chatty agents over long sessions, important early output may be gone — read often.

The `recent`/`recent-unwrapped` sources reach into the scrollback. The `visible` source only sees the viewport, so scrollback discard doesn't affect it.

## Edge cases

### Just-spawned pane has no content yet

`herdr pane read` returns an empty `text` field — not an error, just empty. If you're reading right after spawning, give the shell a moment (or `agent wait --status idle --timeout 5000` once).

### Pane is gone

If the pane was closed (shell exited, `pane close`), `pane read` returns `pane_not_found`. Handle as you would any disappeared pane.

### Output contains escape sequences you didn't expect

Default `--format text` strips them. If you see `\x1b[` literals in the output, you accidentally used `--ansi`. Switch back to default.

### Output truncated

If `truncated: true` in the response, your read hit a buffer limit. Decrease `--lines`, change source (`visible` is always small), or read multiple slices.

## When to use `pane read` vs `wait output`

| Goal | Approach |
|---|---|
| Snapshot the current state (e.g. after agent finished) | `pane read` once |
| Get all output for parsing | `pane read --source recent-unwrapped --lines N` once |
| React when a specific marker appears | `wait output --match "..."` in background, then read |
| Tail live output continuously | Not really supported; use `wait output` repeatedly or subscribe to `pane.output_matched` events |

Polling `pane read` in a loop works but is wasteful. The event/wait infrastructure is built precisely so you don't have to.

## Source code subtlety: ANSI stripping happens before line counting

`--lines N` counts lines *after* ANSI strip (if you're in text mode). In `--ansi` mode, ANSI escape sequences may make a "line" longer but don't add line breaks themselves, so the counting is still by `\n`. No surprise here, but worth knowing if you have a pane heavy with cursor movement codes.

## Quick recipes

```bash
# Most common: "show me the agent's last reply"
herdr pane read $PANE --source visible --lines 40

# "Parse this — give me clean lines"
herdr pane read $PANE --source recent-unwrapped --lines 200

# "I need exact terminal contents including colors"
herdr pane read $PANE --source recent --ansi --lines 100

# "Tail the build log"
herdr wait output $PANE --match "(succeeded|failed)" --regex --timeout 600000   # background, then pane read
```

## Don't do these

- **Read in a tight polling loop.** Burns RPCs. Use `wait output` instead.
- **Read with `--ansi` for parsing.** You'll be fighting escape codes. Default text is what you want.
- **Read `recent` and grep across line wraps.** `recent` wraps long lines; use `recent-unwrapped` for pattern matching.
- **Expect bytes back immediately after sending.** TUIs may take 100 ms+ to render. If the response isn't there yet, wait (or `agent wait --status working/idle` for status-based triggers).
