# Permission handling — the `blocked` state

How to handle agents (Claude, Codex) that pause for permission prompts. Whether you should intercept them at all, and how to do it cleanly when you should.

## When `blocked` appears

The integration hook (Claude's, Codex's, etc.) detects that the agent is at a **tool/command permission gate** (its `PermissionRequest` event) and emits `pane.report_agent --state blocked`. The agent sits at the prompt waiting for Yes / No / Yes-allow-all.

**Critical distinction (verified live):** `blocked` is ONLY for tool/command permission gates. A **plan-approval menu** ("Implement this plan? 1. Yes / 2. … / 3. No", from Codex `/plan`) and any **question the agent asks you** report as `idle`/`done`, NOT `blocked` — they're conversational turn-ends. A `--status blocked` watcher will NOT catch a plan menu. Also: Codex auto-approves ordinary shell commands inside its workspace sandbox, so genuine `blocked` is rarer than you'd expect — most "waiting on you" moments are `idle`/`done` with a question or menu on screen. Use `scripts/await_done.py` (returns `waiting_choice` for menus, `waiting_question` for questions, `blocked` for real gates), or watch for `idle` AND `blocked` together. Reserve `--status blocked` for true permission gates.

Transitions look like:

```
idle → working → blocked → (your answer) → working → done
```

The `blocked` is interruptible by a key press to the pane. Claude's prompt typically looks like:

```
Do you want to create /tmp/foo.txt?
❯ 1. Yes
  2. Yes, allow all edits in /tmp during this session
  3. No
Esc to cancel · Tab to amend
```

## Strategy 1 — Sidestep the whole thing

Easiest. Run the agent with permissions bypassed:

```bash
# Claude
herdr agent start helper --split right --no-focus -- "claude --dangerously-skip-permissions"

# Codex — has its own equivalent
```

If the user is okay with this (i.e. they trust the sub-agent's task or the sandboxing), do it. No `blocked` ever fires. Your background `--status idle` wait covers the full task end-to-end.

**When to use:** routine refactors, file ops in clearly-scoped directories, anything where the sub-agent can't do irreversible damage. The user's overall Claude Code session already runs with whatever permission level they chose; sub-agents shouldn't need stricter rules.

## Strategy 2 — Auto-approve, smart

Run the agent with normal permissions, but intercept `blocked` and decide programmatically.

```bash
# 1. Send the task
herdr pane run $CLAUDE "Refactor src/foo.py. Save in place."

# 2. Background watcher: blocked
herdr agent wait $CLAUDE --status blocked --timeout 600000      # run_in_background
# 3. Background watcher: idle (auto-accept path or no prompt)
herdr agent wait $CLAUDE --status idle    --timeout 600000      # run_in_background
```

Both bash calls return immediately with `background_task_id`. You wait for either notification:

- **Blocked fires first** → permission prompt appeared. Read the pane, decide, answer.
- **Idle fires first** → no prompt or it was auto-bypassed. Done.

On `blocked` notification:

```bash
SCREEN=$(herdr pane read $CLAUDE --source visible --lines 30)

# Decide based on what's being asked.
# Examples of simple rules:

if echo "$SCREEN" | grep -qiE "create.*\.(py|md|txt|json|ts|js)"; then
    # Approve writes of safe extensions
    herdr pane send-keys $CLAUDE Enter
elif echo "$SCREEN" | grep -qiE "rm\s+-rf|sudo|drop table|delete from"; then
    # Definitely no
    herdr pane send-keys $CLAUDE Down Down Enter   # select "No"
else
    # Ambiguous — escalate to the user
    # (in Claude Code you'd ask the user via your own prompt)
    echo "Unclear permission prompt; needs human review:"
    echo "$SCREEN"
    # then send the user's answer
fi

# After handling, the agent transitions to working then idle.
# The original `--status idle` background wait will fire eventually.
```

**Note:** the menu layout (which key gets which answer) depends on the agent and prompt. Claude usually has Yes as the first option (Enter accepts), No as the last (Down Down Enter to select). Read the screen and figure out what each option says before mapping keys — the prompt usually labels them clearly.

### A pitfall — TUI prompt nuances

Codex's prompt may have a different default selection. Pi's prompts vary. **Always read the screen first**, don't blindly press Enter.

Some prompts have an "Esc to cancel" option. Sending Esc is equivalent to No in most cases, but check.

## Strategy 3 — Pre-route safe directories

Many permission prompts have an option like "Yes, allow all edits in /tmp during this session." Selecting it once eliminates many subsequent prompts. If you know the agent will be doing lots of writes in a known-safe place, prefer this option:

```bash
# After reading the prompt:
if echo "$SCREEN" | grep -q "allow all edits in /tmp"; then
    # Select option 2 (Yes-allow-all) instead of option 1
    herdr pane send-keys $CLAUDE Down Enter
fi
```

One Down moves from option 1 to option 2. Enter selects.

## Strategy 4 — Pure human-in-the-loop

Don't intercept at all. Run the agent with normal permissions; let the user (via the herdr TUI in another terminal) approve each prompt manually. Your Claude Code session just runs the `--status idle` background wait and notifies when finished.

**When to use:** sensitive operations where you don't want to make permission decisions for the user.

## When NOT to background a blocked wait

If the sub-agent's task is short (< 30 s expected), permission interception may add complexity without value. Just `--dangerously-skip-permissions` and move on.

If the task is long (> 5 min) and may need multiple permission decisions, you'll want a continuous loop:

```python
# Pseudo: keep handling prompts until idle
while True:
    # Race blocked vs idle in background
    blocked_id = bash_bg("herdr agent wait $PANE --status blocked --timeout 600000")
    idle_id    = bash_bg("herdr agent wait $PANE --status idle    --timeout 600000")
    
    # Wait for either notification (this is automatic — harness pings you)
    result = wait_for_first_notification(blocked_id, idle_id)
    
    if result == "blocked":
        screen = pane_read($PANE)
        decide_and_send_key($PANE, screen)
        # Loop continues: re-arm watchers
    elif result == "idle":
        break  # done
```

In Claude Code: each notification handler decides whether to re-arm the watchers and continue, or to stop.

## Workspace-level sidebar — `blocked` rises to the top

When any pane goes blocked, the workspace's aggregated `agent_status` becomes `blocked` (priority 4, highest). The herdr TUI sidebar highlights the workspace so a human watching gets visual cues.

If you have a human watching the TUI alongside your script-driven session, they can step in to answer blocked prompts you didn't handle. Cooperative.

## The exact pane key mapping (Claude default permission prompts)

| Want | Keys |
|---|---|
| Yes | `Enter` (option 1 is default-selected) |
| Yes, allow all (in scoped dir) | `Down`, `Enter` |
| No | `Down`, `Down`, `Enter` |
| Cancel / abort | `Esc` |

For Codex / Pi the menu may differ — read first, decide, send. Don't hardcode.

## Diagnosing "agent says it's blocked but no prompt visible"

Rare. If the integration hook reported blocked but `pane read` shows no prompt:

1. The hook may have crashed mid-state. Read again in a second or two.
2. The pane may have scrolled past the prompt. Read with `--source recent-unwrapped --lines 100` and search.
3. The pane was bumped to a different agent state and the report-agent is stale. Check `agent get $PANE` — if it now reads `idle`, the prompt was answered (perhaps by the user) and you missed it.

When in doubt, `agent get` to see the current state; if it's idle/done, proceed as if nothing happened.

## Cleanup of stuck blocked state

If an agent is `blocked` and you don't want to respond, you have options:

- Send `Esc` to the pane — most agents treat this as cancel.
- `pane send-keys $PANE C-c` — Ctrl+C, usually aborts the operation.
- `pane close $PANE` — kills the pane entirely (last resort).

After cancel/abort, the agent typically returns to `idle` or `working` (writing an "operation cancelled" message). Your `--status idle` background wait will eventually fire.

## Summary

| Situation | Strategy |
|---|---|
| Trusted task, scoped scope | `--dangerously-skip-permissions`, no interception |
| Need to inspect each prompt | Background `--status blocked` watcher + decide on read |
| Bursty writes in one dir | Select "Yes, allow all" once |
| Human will watch the TUI | Don't intercept; let them answer |
| Short task | Skip permissions, save the complexity |

For most Claude Code uses, **Strategy 1** (`--dangerously-skip-permissions`) is the right answer. Reach for **Strategy 2** (smart auto-approve) when the user wants traceable decisions or when the sub-agent will be active for long stretches.
