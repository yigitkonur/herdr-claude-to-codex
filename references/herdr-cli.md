# Controlling herdr from inside it — the CLI surface

This is the canonical "drive herdr from inside a pane" guide (the upstream `herdr`
agent skill, folded in here). It is the **generic herdr-control** layer — workspaces,
tabs, panes, reads, and waits. To drive a single **Codex** sub-agent specifically,
prefer `scripts/codex.py` (see `SKILL.md` and `codex-and-agents.md`); this file is for
everything else (your own pane, sibling panes, servers, logs, fleets, other agents).

> **Reconciliation with this instance (verified on herdr v0.6.0):**
> - Inside herdr, **`HERDR_ENV=1`** is set (the upstream check) **and** `HERDR_PANE_ID`
>   is set (e.g. `p_5`) — either confirms you're in a herdr pane.
> - **IDs on this build are the LONG form**: workspace `w6522ea4d2775bf`, pane
>   `w6522ea4d2775bf-1`, short pane `p_5`, terminal `term_…`. The compact
>   `1` / `1:1` / `1-1` ids in the examples below are **illustrative** — this build
>   does not emit them. As the doc itself says: **always re-read real ids** from
>   `pane list` / `workspace list` / create/split responses, never hardcode an id.
> - A `herdr agent` namespace also exists here (`agent list`, `agent get <target>`)
>   in addition to the pane-based control shown below.

Before using this surface, check that `HERDR_ENV=1`. If it is not `1`, you are not
running inside a herdr-managed pane — do not try to control the focused herdr pane
from outside herdr.

You are running inside herdr, a terminal-native agent multiplexer: workspaces, tabs,
and panes — each pane a real terminal with its own shell, agent, server, or log
stream — all controllable from the CLI. So you can: see what other panes/agents are
doing, create tabs for subcontexts, split panes and run commands, start servers /
watch logs / run tests in sibling panes, wait for specific output before continuing,
wait for another agent to finish, and spawn more agents.

The `herdr` binary is on PATH; its workspace/tab/pane/wait commands talk to the
running herdr instance over a local unix socket. Raw protocol / full API reference:
https://herdr.dev/docs/socket-api/.

## Concepts

- **workspaces** — project contexts; each has one or more tabs. A workspace's label
  follows its first tab's root pane (usually the repo name).
- **tabs** — subcontexts inside a workspace; each has one or more panes.
- **panes** — terminal splits inside a tab; each runs its own process (shell, agent,
  server, anything).
- **agent status** — detected automatically; the public field is `agent_status` ∈
  `idle | working | blocked | done | unknown`. `done` = the agent finished but you
  haven't looked at that finished pane yet.
- **ids** — compact public ids for the live session (upstream examples: workspace `1`,
  tab `1:1`, pane `1-1`; **this build: `w…`, `w…:N`-style tabs, `w…-N` panes, `p_N`
  short**). Ids can compact when tabs/panes/workspaces close — they are **not durable**.
  Re-read them from `workspace list` / `tab list` / `pane list` / create/split responses.

## Discover yourself

```bash
herdr pane list          # the focused pane is yours; others are neighbors
herdr workspace list
```

## Tab management

```bash
herdr tab list --workspace <ws>
herdr tab create --workspace <ws>                 # default numbered name
herdr tab create --workspace <ws> --label "logs"  # name in one step
herdr tab rename <tab> "logs"
herdr tab focus <tab>
herdr tab close <tab>
```

## Read another pane

```bash
herdr pane read <pane> --source recent --lines 50
```

- `--source visible` = current viewport
- `--source recent` = recent scrollback as rendered
- `--source recent-unwrapped` = recent text with soft wraps joined back together
- `--format ansi` (or `--ansi`) = rendered ANSI snapshot, for TUI feedback loops

## Split a pane and run a command

`pane split` prints JSON with the new pane at `result.pane.pane_id`. Parse it, then run:

```bash
NEW_PANE=$(herdr pane split <pane> --direction right --no-focus \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$NEW_PANE" "npm run dev"
```

`--direction down` splits downward. `--no-focus` keeps your current pane focused.

## Wait for output

Block until text appears (servers, builds, tests). For `--source recent`, matching uses
unwrapped recent text, so pane width / soft wraps don't break matches; `pane read
--source recent-unwrapped` shows the same transcript the waiter matches.

```bash
herdr wait output <pane> --match "ready on port 3000" --timeout 30000
herdr wait output <pane> --match "server.*ready" --regex --timeout 30000   # regex
```

Times out → exit code `1`.

## Wait for an agent status

```bash
herdr wait agent-status <pane> --status done --timeout 60000
```

Use this for the same `done` / `idle` distinction the UI shows.

## Send text or keys

```bash
herdr pane send-text <pane> "hello"   # text, no Enter
herdr pane send-keys <pane> Enter     # keys
herdr pane run <pane> "echo hello"    # text + a real Enter, one request
```

## Workspace management

```bash
herdr workspace create --cwd /path/to/project              # default cwd-based name
herdr workspace create --cwd /path/to/project --label "api server"
herdr workspace create --no-focus
herdr workspace focus <ws>
herdr workspace rename <ws> "api server"
herdr workspace close <ws>
```

## Close a pane

```bash
herdr pane close <pane>
```

## Recipes

### Run a server and wait until ready
```bash
NEW_PANE=$(herdr pane split <pane> --direction right --no-focus \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$NEW_PANE" "npm run dev"
herdr wait output "$NEW_PANE" --match "ready" --timeout 30000
herdr pane read "$NEW_PANE" --source recent --lines 20
```

### Run tests in a separate pane and inspect
```bash
TEST_PANE=$(herdr pane split <pane> --direction down --no-focus \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$TEST_PANE" "cargo test"
herdr wait output "$TEST_PANE" --match "test result" --timeout 60000
herdr pane read "$TEST_PANE" --source recent --lines 30
```

### Check what another agent is working on
```bash
herdr pane list
herdr pane read <pane> --source recent --lines 80
```

### Watch a sibling pane robustly
```bash
herdr pane read <pane> --source recent --lines 40           # what's already there
herdr wait output <pane> --match "ready" --timeout 30000    # wait for the next expected output
herdr pane read <pane> --source recent-unwrapped --lines 40 # the transcript the waiter matched
```

### Spawn a new agent and give it a task (raw, pane-based)
```bash
A=$(herdr pane split <pane> --direction right --no-focus \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["pane"]["pane_id"])')
herdr pane run "$A" "claude"
herdr wait output "$A" --match ">" --timeout 15000
herdr pane run "$A" "review the test coverage in src/api/"
```
(For a single **Codex** sub-agent, `scripts/codex.py start --task "…"` does spawn +
readiness + verified send + classified verdict for you — prefer it over this raw form.)

### Coordinate with another agent
```bash
herdr wait agent-status <pane> --status done --timeout 120000
herdr pane read <pane> --source recent --lines 100
```

## Notes

- JSON on success: `workspace list/create`, `tab list/create/get/focus/rename/close`,
  `pane list/get/split`, `wait output`, `wait agent-status`.
- `pane read` prints **text**, not JSON (`--format ansi` returns an ANSI snapshot).
- `pane send-text`, `pane send-keys`, `pane run` print nothing on success.
- Parse new ids from responses: `workspace create` → `result.workspace`, `result.tab`,
  `result.root_pane`; `tab create` → `result.tab`, `result.root_pane`; `pane split` →
  `result.pane.pane_id`. (`agent start`, used by `codex.py`, nests at `result.agent` —
  a different shape; see `pitfalls-and-traps.md`.)
- Use `pane read` for output that already exists; `wait output` for output you expect next.
- `--no-focus` (split / tab create / workspace create) keeps your current pane focused.
- Without `--label`, `workspace create` keeps cwd-based naming and `tab create` keeps
  numbered naming; `--label` applies a custom name immediately.
- Inside herdr, `HERDR_ENV=1` (and `HERDR_PANE_ID` is set, e.g. `p_5`).
