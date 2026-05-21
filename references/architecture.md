# Architecture — what herdr actually is

A 5-minute read that gives you the shape of the system. Helpful background; not needed for the canonical pattern.

## One sentence

A Rust daemon that multiplexes terminal panes — like tmux — but with **first-class awareness of AI coding agents running inside those panes**, exposing a JSON-RPC API over a Unix socket so anything (including you, Claude Code) can spawn, drive, and monitor agents programmatically.

## Two processes, one socket

```
herdr server   (headless daemon, Rust)
   ▲
   │  Unix Domain Socket, JSON line-delimited
   │  ~/.config/herdr/herdr.sock   (mode 0600)
   ▼
herdr CLI      (each invocation is a short-lived client)
```

- The **server** holds all state: workspaces, tabs, panes, terminal pty connections, agent registrations, event subscriptions.
- The **CLI** is a thin client. Each `herdr <cmd>` opens the socket, sends one JSON request, reads one JSON response, exits. (Subscribe is the exception — it holds the socket open for streaming events.)
- A separate interactive **TUI client** uses a different socket (`herdr-client.sock`) and a different protocol (binary frames, for rendering). You don't use it from Claude Code; it's for humans watching the panes.

The 0600 permission means: any process running as the same user can talk to the server. That's the security model — single user, anything-with-uid-can-do-anything. Don't run untrusted code on a machine with a live herdr server.

## The hierarchy

```
session                         (= one server process; named or "default")
└── workspace                   (top-level user concept; one workspace per project, typically)
    └── tab                     (a tab inside a workspace)
        └── pane                (a split inside a tab)
            └── terminal        (the actual pty + shell process)
```

For Claude Code use, you'll almost always be inside the **default session**. Named sessions exist but are launched only by interactive `herdr --session <name>` (no IPC method to create one). If you need to drive a different session, point at its socket via `HERDR_SOCKET_PATH=...`.

Within a session, you can have multiple workspaces, but most people work in one. Panes are the unit you actually create and operate on.

## The five ID flavors

Every pane is addressable by **five different strings**, all accepted by all `<target>` arguments in `agent` commands:

| Flavor | Example | Where you get it |
|---|---|---|
| Long pane id | `w6522ea4d2775bf-2` | `pane list`, `pane get`, response of `pane split`/`agent start` |
| Short pane id | `p_10` | Injected into every pane's env as `$HERDR_PANE_ID`; legacy form but fully supported |
| Terminal id | `term_6524a2ff42e9b5` | `pane get` response; always unique |
| Agent type | `pi`, `claude`, `codex`, `opencode`, `hermes` | When that agent's integration hook has registered |
| Agent name | `worker-a` (whatever you set) | After `agent rename` (custom label) |

All five flavors resolve to the same pane at any instant. Names and types can collide (the ambiguity error). But two stability facts matter (both verified):
- **terminal_id is stable for the pane's whole life** — it never changes. It's accepted by `agent` commands only, not `pane` commands.
- **pane_id's `-N` suffix is a slot index that shifts when a lower-slot pane closes.** Capture `w...-3`, close `w...-2`, and your pane is now `w...-2`. Accepted by both `pane` and `agent` commands.

**For scripts:** capture BOTH the pane_id (for `pane run/read/close`) and the terminal_id (stable; for `agent` commands and for re-resolving the pane_id after any close). `scripts/codex.py` does this — its session registry keys on the stable terminal_id and re-resolves the pane_id every call. Don't close a lower pane and keep using another's old pane_id without re-resolving. See `agent-vs-pane.md` and `pitfalls-and-traps.md` traps F/G.

## What's in a pane?

The interesting fields of `pane get`:

```jsonc
{
  "pane_id": "w...-3",            // long form
  "terminal_id": "term_abc",       // pty + shell process
  "workspace_id": "w...",
  "tab_id": "w...:1",
  "focused": false,                // is the UI cursor on this pane?
  "cwd": "/path/to/project",       // updated when shell reports OSC 7 (modern zsh yes, default bash no)
  "label": null,                   // set by `pane rename` or via `agent rename`
  "agent": "codex",                // null if no integration has reported here
  "agent_status": "blocked",       // 5 API values: idle, working, blocked, done, unknown
  "custom_status": "Waiting for approval",   // sidebar message, up to 32 chars
  "revision": 0                    // sequence number; default integrations don't increment, so always 0
}
```

`pane.label` is what shows in the herdr sidebar.
`pane.agent` is set by the integration hook automatically.
`pane.agent_status` reflects what the hook last reported, transformed through the seen-flag (see `status-model.md`).

## The protocol — JSON line-delimited, no length prefix

The wire format on `~/.config/herdr/herdr.sock` is dead simple:

```
{"id":"my-req-1","method":"pane.list","params":{}}\n
```

Response:
```
{"id":"my-req-1","result":{...}}\n
```

Error:
```
{"id":"my-req-1","error":{"code":"...","message":"..."}}\n
```

Multi-frame streams (e.g. `events.subscribe`) emit one JSON object per line, separated by `\n`. The connection stays open until the client closes it.

Max frame size is 2 MB. The CLI's `pane read` will use `truncated: true` to flag when its output had to be clipped (rare in practice).

You don't need to know this for daily use — `herdr <cmd>` is enough. But you'll need it when calling hidden IPC methods (see `cli-and-ipc-reference.md`).

## API discovery via invalid method

A useful trick: send any garbage `method` to the socket and the server's error message includes the **full list of valid method names**. This is how to enumerate even the hidden ones.

```bash
echo '{"id":"x","method":"INVALID","params":{}}' | nc -U ~/.config/herdr/herdr.sock
# Error message lists: ping, server.stop, ..., pane.send_input, pane.clear_agent_authority,
# pane.release_agent, events.subscribe, events.wait, ...
```

There are 38 IPC methods at the time of writing. **Five of them are not exposed by the CLI** (`pane.send_input`, `pane.clear_agent_authority`, `pane.release_agent`, `events.subscribe`, `events.wait`) but are usable via raw socket. See `cli-and-ipc-reference.md`.

## Sessions and the socket path

Default socket: `~/.config/herdr/herdr.sock` (mode 0600). Named-session sockets: `~/.config/herdr/sessions/<name>/herdr.sock`. You can drive any of them by setting `HERDR_SOCKET_PATH=<path>` before running `herdr` (or by passing the path to a Python client).

Session isolation is purely socket-level. The same user can talk to any session; the server processes don't talk to each other. State (pane lists, agent registrations) is per-session. The config file (`~/.config/herdr/config.toml`) is shared across all sessions.

## Where `herdr CLI` calls actually fail

The CLI itself almost never fails for its own reasons. Failures originate from the server and come back as error responses. Common error codes:

| Code | Meaning |
|---|---|
| `pane_not_found` | The pane id resolved to nothing |
| `agent_not_found` | Target in the `agent` namespace doesn't exist (integration hasn't registered) |
| `agent_target_ambiguous` | Two or more panes matched the target; candidates listed in the message |
| `invalid_request` | Schema validation failed (missing field, bad enum value) — message lists valid values |
| `invalid_key` | An entry in `pane send-keys` is outside the tiny accepted vocabulary |
| `invalid_agent` | Empty/bad agent label in `pane report-agent` |
| `pane_send_failed` | The PTY rejected the write (rare) |

When you see one, the error message contains enough to diagnose. Parse and react.

## Performance notes

- The CLI binary adds ~25 ms of subprocess startup overhead per call. Background waits hold the connection open server-side; the CLI process is idle (cheap).
- For very high-frequency calls, talking directly to the socket from Python is about 5× faster than the CLI. Almost never worth it; CLI is fine for everything in the canonical patterns.

## What's where (file layout)

- Server socket: `~/.config/herdr/herdr.sock`
- Client (TUI) socket: `~/.config/herdr/herdr-client.sock`
- Config: `~/.config/herdr/config.toml`
- Logs: `~/.config/herdr/{herdr,herdr-client,herdr-server}.log` (rotated)
- Named-session dirs: `~/.config/herdr/sessions/<name>/`
- Integration hooks (per-agent):
  - `~/.pi/agent/extensions/herdr-agent-state.ts`
  - `~/.claude/hooks/herdr-agent-state.sh`
  - `~/.codex/herdr-agent-state.sh`
  - `~/.config/opencode/plugins/herdr-agent-state.js`
  - `~/.hermes/plugins/herdr-agent-state/__init__.py`

## Inversion: what you don't need to know

- The TUI rendering protocol (binary, render-frame). Irrelevant from Bash.
- How `cwd` is detected (OSC 7 escape sequence). Just `cwd` field — read it, don't set it.
- The ghostty terminal emulator embedded inside herdr. Doesn't affect anything you do.
- The internal-vs-API status mapping. Use `agent wait --status idle` and forget about it. (Full story in `status-model.md` if curious.)

## Concluding mental shape

Server holds state. CLI is a fancy wrapper around `connect → write JSON → read JSON → close`. Panes are the atoms. Agents are an opt-in property of panes. Subscribe/wait are how you stream/await state changes. Everything else is a detail.
