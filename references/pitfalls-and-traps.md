# Pitfalls and traps — silent failure modes and recovery

The collected list of "but why didn't that work?" — with mechanism and fix for each.

## The big ones

These cover most production confusion. Memorize the symptoms.

### 0. `idle`/`done` returned ≠ the task finished (the #1 lesson, verified)

**Symptom:** Your wait returns "done", you treat the task as complete and move on — but the agent had actually **asked you a question** or **presented a plan-approval menu** and is sitting there waiting. Work silently stalls; the agent never gets its answer.

**Mechanism:** Every turn-end maps to `idle` (the integration's `Stop` event), whether the agent finished, asked something, or showed a menu. Status alone cannot distinguish them. Verified live: "ask me 3 questions then stop" produced `working → done` in 7.5 s, where `done` meant "waiting for your answer," not "complete."

**Fix:** (a) bake a completion marker into the prompt ("when fully done print TASK_DONE") and check the screen for it; and/or (b) for Codex, `scripts/codex.py` classifies the outcome (`completed` / `awaiting_clarification` / `awaiting_approval` / `permission_gate`). Never equate a wait returning with completion.

### 1. Wait started before send → false-positive notification in milliseconds

**Symptom:** You background `agent wait --status idle`, then send the task. Notification arrives within ~20 ms saying the agent is idle — but the task never ran.

**Mechanism:** `agent wait` is hybrid. It checks the *current* state first. When you started the wait, the pane was sitting in `idle` (no task yet). Immediate match. Wait exits.

**Fix:** Always send first, then start the wait. Sequentially.

```bash
herdr pane run $PANE "task"                                # send first
herdr agent wait $PANE --status idle --timeout 600000      # then background-wait
```

---

### 2. `wait agent-status --status idle` after `done` → permanent timeout

**Symptom:** You use `wait agent-status` instead of `agent wait`, ask for `--status idle`, and the wait times out after 10 minutes even though `agent get` shows the pane finished (`done`) ages ago.

**Mechanism:** `wait agent-status` is strict event-only. When an agent finishes (`Working → Idle`), the event payload says `agent_status: done` (because seen=false). There is *no further event* for `done → idle` — the seen-flag flip is internal-only. So `--status idle` waits forever.

**Fix:** Use `agent wait --status idle` instead. It operates on internal state, which is Idle in both cases. Catches done automatically.

---

### 3. Newline in `agent send` to a bash pane → multi-line execution

**Symptom:** You pass multi-line text to a bash pane. Each line gets executed as a separate command. Disaster if any line is dangerous.

**Mechanism:** Default bash doesn't have bracketed paste enabled. The server writes your text raw to the pane's PTY. `\n` bytes are received by the shell as Enter. Each Enter executes the current line.

**Fix:** Either:
- Send single-line commands (use `&&` to chain): `herdr pane run $BASH "cmd1 && cmd2"`
- Strip newlines before sending: `text=$(echo "$input" | tr -d '\n'); herdr pane run $BASH "$text"`
- Enable bracketed paste in the shell (zsh modern config has it; bash 5+ needs `enable-bracketed-paste on`)

**Note:** TUI panes (Claude, Pi, Codex) usually support bracketed paste — multi-line is safe there. This trap is specific to plain shell panes.

---

### 4. Two `events.subscribe` on one socket → both die silently

**Symptom:** You open a socket, subscribe to one event type, then call subscribe again with a second filter. No ACK on the second. The first stops emitting events too.

**Mechanism:** The server holds at most one active subscription per connection. The second call doesn't override cleanly — it leaves both in a broken state with no error returned to the client.

**Fix:** One subscribe per socket. Pack all the subscriptions into the single request's `subscriptions` array. Open a new socket if you need to add subscriptions later.

```python
# Right:
subscribe([
    {"type": "pane.agent_status_changed", "pane_id": "..."},
    {"type": "pane.created"},
    {"type": "pane.exited"}
])

# Wrong:
sub1 = subscribe([{"type": "pane.agent_status_changed", "pane_id": "..."}])
# ... later, on the same socket ...
sub2 = subscribe([{"type": "pane.created"}])    # both dead now
```

---

## Tier two — common, less catastrophic

### 5. `Ctrl-c` / `Ctrl+c` (capital C) → `invalid_key`

The accepted forms are `C-c`, `c-c`, and `ctrl+c` (lowercase plus). Any variation with capital `Ctrl`, capital `C` after `+`, or a hyphenated word modifier fails.

**Fix:** Use `C-c` (most common in docs).

---

### 6. Sending `F1`, `Home`, `End`, `Alt+x`, etc. → `invalid_key`

Vocabulary is tiny: Enter/Tab/Esc/Backspace/arrows + only `Ctrl+C` of all the chords. No F-keys, no Home/End, no Alt/Meta/Shift modifiers.

**Fix:** Find another way to drive the TUI. Some prompts accept slash commands instead of menu navigation (`/help` instead of pressing F1). Or send raw escape sequences via `pane send-text $PANE $'\e[Z'` (Shift+Tab CSI), which bypasses the herdr key parser.

---

### 7. `agent get` right after `agent start` → `agent_not_found`

The integration hook needs ~3–5 seconds after agent launch to register. During this window the pane exists but isn't in the agent registry yet.

**Fix:** After `agent start`, run one foreground `agent wait --status idle --timeout 15000`. This blocks until the agent registers (hybrid wait sees the future `idle` state when the hook reports it).

---

### 8. Rename to a reserved type name → ambiguity later

```bash
herdr agent rename $PI "codex"
# Later: another codex pane gets created
herdr agent send codex "..."   # agent_target_ambiguous — pi.name=codex AND codex.agent=codex
```

The resolver matches across name and type fields simultaneously. Don't pollute the name space with type names.

**Fix:** Rename only to unique custom labels. Treat `pi`, `claude`, `codex`, `opencode`, `hermes` as reserved.

To recover: `herdr agent rename $PI --clear` removes the rename.

---

### 9. `pane rename` doesn't make target resolution work

You renamed a pane's label but `agent send <new-label>` fails. `pane rename` only sets `pane.label`; it doesn't add to the agent name registry. `agent send` looks at `agent.name` and `agent.agent`, not `pane.label`.

**Fix:** Use `agent rename` instead (sets both `agent.name` and `pane.label`). If you actually want only the UI label, accept that target resolution won't pick it up.

---

### 10. `agent attach` from a Bash subprocess → ratatui panic

`agent attach` and `terminal attach` launch interactive TUIs. Without a real TTY (you're a Claude Code subprocess), ratatui panics with "Device not configured".

**Fix:** You can't `attach` from automation. To monitor a pane: `pane read` periodically, or subscribe to events. To intervene: send keys / text. Interactive attach is for humans only.

---

### 11. `revision` always stays at 0

The default integrations don't pass `--seq` to `pane.report_agent`, so the per-pane `revision` field never increments. Polling for revision change waits forever.

**Fix:** Use `events.subscribe` (`pane.agent_status_changed`) for change detection. Don't trust `revision` as a heartbeat.

---

### 12. `pane close` while shell is busy in a foreground job

Sometimes a shell with a long-running foreground process doesn't respond cleanly to SIGHUP. The pane may take seconds to die or hang.

**Fix:** Send `Ctrl+C` first to interrupt the foreground job, then close:

```bash
herdr pane send-keys $PANE C-c
sleep 1
herdr pane close $PANE
```

---

### 13. `pane.report-agent` without a unique source → state confusion

If multiple sources report on the same pane with the same source name, the server merges (overwrites) — but if you started fresh with a source name another integration was using, the real integration's state gets overwritten on its next report. Net effect: confusing flicker.

**Fix:** Always use a unique source string for your tooling (e.g. `my-script-pid-$$` or include a UUID). Don't reuse names like `herdr:claude`.

---

### 14. Long task background-wait timeout too short → mid-task notification

If you set `--timeout 300000` (5 min) on a wait but the task takes 15 min, you'll get a timeout notification and have to wait again. Multiple unnecessary notifications.

**Fix:** Pick `--timeout` generously. Background waits cost nothing to keep open. Use `1800000` (30 min) or even `3600000` (1 hr) for long jobs; the wait exits as soon as the agent reaches idle, regardless of timeout.

---

### 15. Forgot to capture `pane_id` from `agent start` → re-look-up needed

```bash
herdr agent start worker --split right --no-focus -- codex
# Oops, didn't capture the JSON output. Pane id?
```

**Fix:** Either always capture (preferred):

```bash
RESP=$(herdr agent start worker --split right --no-focus -- codex)
PANE=$(echo "$RESP" | jq -r .result.agent.pane_id)
```

Or list and filter:

```bash
herdr pane list | jq -r '.result.panes[] | select(.label == "worker") | .pane_id'
```

---

### 16. Inside the herdr pane, `HERDR_PANE_ID` is short form, but you wrote a script expecting long form

The env var is the legacy short form (`p_10`). All `herdr` commands accept both — but if you split string-match on `w...-N`, the short form won't match.

**Fix:** Use the env var as-is for `herdr` commands. Convert to long form by calling `herdr pane get $HERDR_PANE_ID` and reading the `pane_id` field of the response.

---

### 17. Server isn't running

You try `herdr pane list` and get a connection refused error.

**Fix:** Tell the user. You can't start the server yourself from a script — it requires an interactive TUI invocation. The user typically runs `herdr` once in a terminal to start the session, and then it stays running.

```bash
# Diagnostic:
herdr status server
# If "not running", instruct the user to launch `herdr` (no args) in a terminal first.
```

---

## Newly verified traps (live testing for this skill)

### A. `agent start` pane id is at `result.agent.pane_id`, not `result.pane.pane_id`

`pane split` returns the new pane under `result.pane`; **`agent start` returns it under `result.agent`.** Reading the wrong key gives a `KeyError`/empty and you can't address the pane you just made. Fix: read `result.agent.pane_id` after `agent start` (or use `scripts/codex.py`, which normalizes this).

### B. `herdr pane read` prints PLAIN TEXT, not JSON

Almost every other `herdr` subcommand returns JSON, but `pane read` (and `agent read`) print the pane's text straight to stdout. Piping to `jq` fails with a parse error. Fix: capture the output directly (`OUT=$(herdr pane read $PANE …)`). The raw IPC `pane.read` *does* return JSON, nested at `result.read.text`.

### C. Status event precedes screen render → menu missed

The `pane.agent_status_changed` event can arrive a few hundred ms before the TUI finishes painting a menu. Read the screen the instant the status settles and you may catch a half-drawn screen — a menu-detector sees no menu and misclassifies the prompt as "nothing here." Fix: pause ~0.8 s after the status settles before reading (`codex.py` does this via `SETTLE_DELAY`).

### D. Plan-approval menu is `idle`/`done`, not `blocked`

A "Implement this plan? 1./2./3." menu (Codex plan mode) reports as `idle`/`done` — a conversational turn-end — not `blocked`. A `--status blocked` watcher will NOT fire for it. `blocked` is specifically a tool/command permission gate. Fix: to catch plan menus, watch for `idle`/`done` and detect the menu on screen (or use `codex.py`, which returns `awaiting_approval`); reserve `--status blocked` for actual permission gates.

### E. Timed-out background wait notifies as "failed (exit 1)"

A wait that reaches its target exits `0` ("completed"); a wait that times out exits `1` ("failed"). In the blocked-vs-idle race pattern this is expected — the losing watcher times out and notifies as failed. Don't treat that failed notification as an error; it just means the other outcome won.

### F. pane_id is a SLOT that shifts when a lower pane closes (terminal_id is stable)

**Symptom:** You captured pane B's id as `w...-3`. You close pane A (`w...-2`). Now your commands to `w...-3` hit the wrong pane or `pane_not_found` — because closing A renumbered B from `-3` to `-2`.

**Mechanism (verified):** the `-N` suffix is a slot index among live panes in the tab. Closing a lower-slot pane compacts the list; higher panes shift down. The pane's `terminal_id` stays constant through this; only the pane_id changes.

**Fixes:**
- Don't close panes mid-orchestration. Close everything at the end, when stale ids no longer matter.
- If you must close mid-flow, re-resolve survivors' pane_ids afterward: `herdr pane list`, match on the `terminal_id` you recorded at spawn.
- For agent panes you'll address long-term, use a unique rename label (`agent rename`) or the `terminal_id` with `agent` commands — both survive renumbering. `pane` commands accept ONLY pane_id, so a non-agent (bash) pane has no stable handle except "don't close lower panes."

### G. `pane` commands reject terminal_id; only `agent` commands accept it

**Symptom:** `herdr pane read term_abc...` → `pane_not_found`, even though the terminal exists.

**Mechanism:** `pane.*` methods resolve only pane_ids. `agent.*` methods resolve terminal_id / name / type / pane_id. So terminal_id (the stable handle) works for `agent get/send/wait` but NOT for `pane run/read/close/send-keys`.

**Fix:** keep both handles from spawn — pane_id (for `pane` commands) and terminal_id (stable, for `agent` commands and for re-resolving pane_id after a close). `scripts/codex.py` captures both (its session registry keys on the stable terminal_id).

## Tier three — easy-to-miss subtleties

### 18. Status `unknown` on a fresh agent pane is normal

Don't panic. The hook hasn't fired yet. Wait or use `agent wait --status idle --timeout 15000`.

### 19. Workspace agent_status shows `done` even though most panes are working

By design. The aggregation prioritizes `done` (idle+unseen) over `working` because the user should be drawn to finished-but-unseen output.

### 20. `pane read --source recent` returns way more lines than expected

`recent` wraps long logical lines to physical lines. For grep/parse, use `recent-unwrapped`.

### 21. Reading right after sending → empty or stale content

The TUI may take 100 ms+ to render. If reading too soon, you'll see pre-send state. Either: wait briefly, wait for `working` state, or wait for `idle` then read.

### 22. `pane run "/something"` vs `agent send "/something"` followed by Enter

Both work for slash commands. `pane run` is atomic and faster (one RPC). Prefer it.

### 23. Trying to subscribe to "all panes" without specifying pane_id

`pane.agent_status_changed` requires `pane_id`. Server returns `invalid_request` with the missing-field message. There's no "all panes" subscription for this event type — you must enumerate.

### 24. Pane was renamed; agent send by old name fails

If you renamed and now `agent send old_name` returns `agent_not_found`, the rename succeeded. Use the new name.

### 25. Multiple agent registrations per pane — `agent get` is non-deterministic

A pane with two source-registrations may return either source's state. Use one source per pane for predictable behavior.

---

## Recovery scripts

### "Find and reset a stuck pane"

```bash
PANE=$(herdr pane list | jq -r '.result.panes[] | select(.agent_status == "working" and .label == "stuck") | .pane_id')
# Try Ctrl+C
herdr pane send-keys $PANE C-c
sleep 1
# Verify
herdr agent get $PANE | jq '.result.agent.agent_status'
# If still stuck, close
herdr pane close $PANE
```

### "Clean up fake agents from a test run"

```bash
for PANE in $(herdr pane list | jq -r '.result.panes[] | select(.label | startswith("test-")) | .pane_id'); do
    echo '{"id":"x","method":"pane.clear_agent_authority","params":{"pane_id":"'$PANE'"}}' \
      | nc -U ~/.config/herdr/herdr.sock > /dev/null
    herdr pane close $PANE
done
```

### "Resolve ambiguous target"

```bash
herdr agent send claude "test" 2>&1 | grep -o "pane_id=[^ ]*" | head -1 \
  | cut -d= -f2 \
  | xargs -I{} herdr agent send {} "test"
```

---

## When in doubt — diagnostic ladder

1. `herdr status` — is the server alive?
2. `herdr pane list` — does the pane exist?
3. `pane get $PANE | jq` — what's its current state?
4. `agent get $PANE | jq` — is it registered as an agent?
5. `pane read $PANE --source visible --lines 30` — what's on screen?
6. `herdr integration status` — are the hooks installed?

Most "but why?" questions are answered by one of these six.
