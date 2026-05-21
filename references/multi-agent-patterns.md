# Multi-agent orchestration patterns

Concrete recipes for the most common multi-agent scenarios in Claude Code. Each pattern names the situation, shows the bash, and notes the trap to avoid.

**Two conventions used throughout, both load-bearing (verified live):**

1. **Spawn shape.** `herdr agent start` nests the new pane under `result.agent.pane_id` (NOT `result.pane` — that's `pane split`'s shape). The recipes below show the raw `python3` extraction; in practice prefer `scripts/spawn.py --label X --split right -- <agent>`, which returns clean JSON `{pane_id, agent, registered}` and waits out the 3–5 s registration delay for you.

2. **Marker discipline.** A finished agent and an agent that just **asked you a question** both report `idle`/`done` — identical status. So every task prompt below ends with an explicit completion marker ("when fully done print TASK_DONE"). To await + auto-classify the outcome (complete vs question vs menu vs blocked), use `scripts/await_done.py <pane> --marker TASK_DONE` instead of raw `agent wait`. Treat raw `agent wait --status idle` returning as "the turn ended" — then read the screen to learn *why*.

## Pattern 1 — Delegate one task to Codex, work on something else in the meantime

The most common pattern. The user says "have codex refactor X while you write the tests" or you decide a task is better suited to Codex.

```bash
# 1. Spawn Codex in a side pane (atomic split + run + agent-start)
RESP=$(herdr agent start codex-worker --split right --no-focus -- codex)
PANE=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")

# 2. One quick wait swallows the 3-5s integration-hook registration delay (foreground OK)
herdr agent wait $PANE --status idle --timeout 15000

# 3. Hand it the task — atomic text+Enter
herdr pane run $PANE "Refactor /Users/me/src/foo.py to deduplicate the loop in lines 40-90. Save in place. When finished, reply with one line: DONE or BLOCKED."
```

Now, the breakthrough call. Use **Claude Code's Bash tool with `run_in_background: true`**:

```bash
herdr agent wait $PANE --status idle --timeout 600000
```

You will receive a `background_task_id` immediately. You're free to keep working — read files, run tests, talk to the user. **The harness will fire a system-reminder notification the moment the background bash exits**, which happens when Codex reaches idle (or done — `--status idle` matches both). The notification contains the wait's stdout, which tells you Codex finished.

When the notification arrives:

```bash
herdr pane read $PANE --source visible --lines 50
# or for full log search:
herdr pane read $PANE --source recent-unwrapped --lines 300
```

Then either reuse the pane for a follow-up or `herdr pane close $PANE` to retire it.

**Trap:** Don't background the wait *before* the `pane run`. The wait sees the pane in `idle` right now (no task has started) and the bash exits in 20 ms with a false-positive notification. Always: spawn → wait-registration → send → background-wait.

## Pattern 2 — Run several agents in parallel and gather as they finish

You want Codex, Pi, and Claude to each tackle a different sub-task. Each in its own pane, each waited on independently in the background. Notifications stream back as they finish — first-finished wins.

```bash
# Spawn three workers (down/right splits, or new tabs — your choice of layout)
P1=$(herdr agent start codex-worker --split right --no-focus -- codex | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
P2=$(herdr agent start pi-worker    --split down  --no-focus -- pi    | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
P3=$(herdr agent start claude-worker --split right --no-focus -- "claude --dangerously-skip-permissions" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")

# Wait for each agent's integration hook to register (foreground, ~5s each)
herdr agent wait $P1 --status idle --timeout 15000
herdr agent wait $P2 --status idle --timeout 15000
herdr agent wait $P3 --status idle --timeout 15000

# Hand each one its task
herdr pane run $P1 "Task A: <prompt for codex>"
herdr pane run $P2 "Task B: <prompt for pi>"
herdr pane run $P3 "Task C: <prompt for claude>"
```

Now the parallel async block — **three separate background Bash calls**:

```bash
# Each one returns immediately with its own background_task_id.
# Three independent notifications will arrive as each agent finishes.
herdr agent wait $P1 --status idle --timeout 900000    # run_in_background: true
herdr agent wait $P2 --status idle --timeout 900000    # run_in_background: true
herdr agent wait $P3 --status idle --timeout 900000    # run_in_background: true
```

You can keep working between notifications. When a notification arrives, `pane read` that pane and move on. The order of completion is whatever it is — handle each as it comes.

**Notes:**
- Each background bash runs independently. Three notifications, three reads.
- Don't try to combine all three waits into one bash with `&`/`wait` — you lose the per-agent notification granularity. One agent per background bash is the right unit.
- If you need a **fast-fail** (cancel siblings when first finishes), wrap each in a script that kills the others when it exits. Not built into herdr.

## Pattern 3 — Pipeline: agent A's output feeds agent B

Codex generates code → Pi reviews it → Claude writes the docs. Sequential delegation.

```bash
# Pane A: Codex generates
A=$(herdr agent start codex --split right --no-focus -- codex | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
herdr agent wait $A --status idle --timeout 15000
herdr pane run $A "Write a Python script that <X>. Save to /tmp/gen.py."
herdr agent wait $A --status idle --timeout 600000     # foreground OR background
# Read what it produced (the file, or the pane log if it printed)
cat /tmp/gen.py

# Pane B: Pi reviews
B=$(herdr agent start pi --split down --no-focus -- pi | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
herdr agent wait $B --status idle --timeout 15000
herdr pane run $B "Review /tmp/gen.py. List concrete bugs. Save findings to /tmp/review.md."
herdr agent wait $B --status idle --timeout 600000

# Pane C: Claude documents
C=$(herdr agent start docs --split right --no-focus -- "claude --dangerously-skip-permissions" | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['agent']['pane_id'])")
herdr agent wait $C --status idle --timeout 15000
herdr pane run $C "Read /tmp/gen.py and /tmp/review.md. Write user-facing docs in /tmp/docs.md."
herdr agent wait $C --status idle --timeout 600000
```

**Why use the filesystem as the bus.** It's the simplest stable interchange. Each agent reads/writes its own files; you don't have to parse one agent's pane scrollback to feed the next. Agents that print structured output (JSON in a known file) are infinitely easier to chain than agents you have to scrape.

**When to background.** If you have non-pipeline work to do in parallel (run tests, answer user questions), background each wait. If the pipeline is your only work and each step depends on the previous, foreground waits are simpler.

## Pattern 4 — Background watcher: pane output triggers an action

You're running a dev server or a long build in a pane. You want to be notified when an `ERROR` or a specific success marker appears, *without* polling.

```bash
# Existing pane that's running the build (you didn't spawn it just now, you split for it)
BUILD_PANE=w6522ea4d2775bf-4

# Watch for the error/success substring in background.
# When it appears, the wait exits and Claude Code is notified.
herdr wait output $BUILD_PANE --match "Build succeeded" --timeout 1800000    # run_in_background: true
# Or with regex:
herdr wait output $BUILD_PANE --match "ERROR.+at line [0-9]+" --regex --timeout 1800000    # run_in_background: true
```

`wait output` is a CLI wrapper around the `pane.output_matched` event. It blocks until the substring/regex appears in the pane's recent scrollback (or until timeout). When it returns, you know the pattern appeared; read the pane around that line to investigate.

**Pair it with `agent wait`** when watching a pane that *also* contains an agent: launch both background waits, react to whichever notifies first.

## Pattern 5 — Auto-approve permission prompts in the background

Codex/Claude with normal permissions will hit `blocked` state when they need approval. You can run a background watcher that wakes you the moment that happens, then decide.

```bash
# Send the task that may trigger permissions
herdr pane run $PANE "<task that needs file or shell permission>"

# In the background, wait for blocked (permission prompt)
herdr agent wait $PANE --status blocked --timeout 600000    # run_in_background: true
```

Notification arrives → you know the agent is at a prompt → read the pane:

```bash
herdr pane read $PANE --source visible --lines 30
# Decide based on what's being asked
# Send the chosen answer
herdr pane send-keys $PANE Enter             # default "Yes" if Yes is first
# or Down + Enter for "No" depending on layout
```

Then continue waiting for idle:

```bash
herdr agent wait $PANE --status idle --timeout 600000    # run_in_background: true
```

**For routinely-approved tasks**, run Claude/Codex with `--dangerously-skip-permissions` (Claude) or the equivalent bypass flag and skip this entire dance. Use the auto-approver only when you genuinely want a human-style gate that you're now mediating.

**Smart auto-approve** — read the prompt and decide based on the requested action:

```bash
# Pseudo:
SCREEN=$(herdr pane read $PANE --source visible --lines 30)
if echo "$SCREEN" | grep -q "write to /tmp/"; then
    herdr pane send-keys $PANE Enter           # Yes
elif echo "$SCREEN" | grep -q "rm -rf"; then
    herdr pane send-keys $PANE Down Down Enter # No
else
    # Ambiguous — escalate to the user
    echo "Permission prompt I can't auto-decide:"
    echo "$SCREEN"
fi
```

## Pattern 6 — Cross-agent review (one agent QAs another)

Codex implements; Claude reviews. Two panes, sequential, both async on Claude Code's side.

```bash
WORKER=$(herdr agent start codex --split right --no-focus -- codex | python3 -c "...")
REVIEWER=$(herdr agent start claude --split down  --no-focus -- "claude --dangerously-skip-permissions" | python3 -c "...")

herdr agent wait $WORKER   --status idle --timeout 15000
herdr agent wait $REVIEWER --status idle --timeout 15000

herdr pane run $WORKER "Implement /tmp/spec.md. Save to /tmp/impl.py. Reply only DONE."
herdr agent wait $WORKER --status idle --timeout 1200000   # background; await notification

# Notification comes back — kick the reviewer
herdr pane run $REVIEWER "Review /tmp/impl.py against /tmp/spec.md. Flag missing requirements, untested branches, and likely bugs. Save to /tmp/review.md."
herdr agent wait $REVIEWER --status idle --timeout 1200000  # background; await notification

# Both done — read both, present summary to user.
```

## Pattern 7 — Specialist routing

Different agents for different specialties. You make the decision; herdr provides the pipes.

| Task class | Best fit (typical) | Why |
|---|---|---|
| Tight refactor with deep static analysis | Codex (gpt-5.x with high effort) | Long context, strong code understanding |
| Quick "what's wrong with this" | Pi (also gpt-5.x) | Snappy responses |
| Multi-file structural changes | Claude Code (yourself) | You're already here |
| Documentation drafting | Claude in a side pane | Different context window, doesn't pollute yours |
| Test generation | Codex or Claude | Either works; Claude often more verbose |

Spawn the chosen specialist with `agent start`, give it the task, wait in background.

## Pattern 8 — Long task during your own session (offload, don't fork)

The user gives you a 20-minute job (large refactor across many files). You don't have to do it in your main context — context window cost, risk of compaction, and you lose focus on what the user is asking next. Instead, hand it to a side-pane Claude/Codex:

```bash
PANE=$(herdr agent start helper --split right --no-focus -- "claude --dangerously-skip-permissions" | python3 -c "...")
herdr agent wait $PANE --status idle --timeout 15000
herdr pane run $PANE "<the whole long task as one prompt; include file paths, success criteria, output format>"

# Background the wait, continue talking to the user about something else
herdr agent wait $PANE --status idle --timeout 1800000   # 30 min budget; run_in_background: true
```

This is the **context-protection** use of multi-agent: your context stays clean for whatever the user wants next; the side pane absorbs the long task's context entirely.

## Pattern 9 — Sub-agent that itself spawns its own panes (recursive coordination)

Yes — Codex/Pi/Claude in a pane can call `herdr` themselves. They can split panes, spawn their own helpers, and coordinate. There's no limit besides server-side pane count.

When designing for recursive coordination:

- Pass the `pane_id` of the spawning pane in the prompt if the sub-agent needs to know where to report back ("when done, write to /tmp/result-from-$PANE.txt").
- Each agent uses its own `$HERDR_PANE_ID` env var to identify *itself*.
- Avoid cycles (A spawns B spawns A) — there's no built-in detection.

## Pattern 10 — Tear-down checklist

After a session of multi-agent work, leave a clean workspace:

```bash
# Find panes you spawned (by label or by agent type)
herdr pane list | jq '.result.panes[] | select(.label and (.label | startswith("codex-")))'

# Close each
herdr pane close <pane_id>

# If you used pane report-agent to create fake agents, also clean their registry entries:
echo '{"id":"x","method":"pane.clear_agent_authority","params":{"pane_id":"<id>"}}' | nc -U ~/.config/herdr/herdr.sock

# Pane closing is graceful (SIGHUP); the sub-agent exits cleanly.
```

## Anti-patterns — never do these

- **Spawn a new pane every call.** Pane creation is cheap-ish but not free, and stale panes accumulate. Reuse a pane for follow-ups.
- **Skip the registration wait after `agent start`.** First `agent get` will fail (`agent_not_found`) with no clear reason. Always wait once.
- **Background the wait without sending first.** False-positive notification within 20 ms.
- **Use `wait agent-status --status idle` for "is it done"** — strict, will miss the done→idle non-event. Always `agent wait --status idle`.
- **Try to attach with `agent attach`** from a Bash subprocess — it spawns a ratatui TUI and panics without a real TTY.
- **Open multiple `events.subscribe` on the same socket** — second one silently kills the first. One socket, one subscribe; pack the array.

## Pattern selection at a glance

| What you want | Pattern |
|---|---|
| Delegate one task and keep working | Pattern 1 |
| Multiple agents at once | Pattern 2 |
| Sequential A→B→C with file outputs | Pattern 3 |
| Wait for output pattern in a long-running pane | Pattern 4 |
| Auto-handle permission prompts | Pattern 5 |
| Coder + reviewer pair | Pattern 6 |
| Pick the best agent for the job | Pattern 7 |
| Offload a long task to protect your context | Pattern 8 |
| Recursive sub-agent coordination | Pattern 9 |
| Cleanup after | Pattern 10 |

When in doubt, **start with Pattern 1** and grow from there. The other nine are compositions of the same three primitives: spawn-agent-start, pane-run, background-agent-wait, pane-read.
