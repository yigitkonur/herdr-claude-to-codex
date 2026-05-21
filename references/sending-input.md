# Sending input — `run`, `send-text`, `send-keys`, and the hidden `send_input`

There are four ways to put bytes into a pane. They look similar but have subtly different semantics. Knowing which one to pick saves race conditions and silent failures.

## The four sends, ranked by daily usefulness

```
1. pane run         <id> <command>        # text + Enter, atomic, single IPC. Use this 80% of the time.
2. pane send-text   <id> <text>           # raw text, no Enter. For TUI prompts where you'll Enter later.
3. pane send-keys   <id> <key>...         # special keys (Enter, Tab, Up, C-c). Tiny vocabulary.
4. pane.send_input  (IPC only, hidden)    # text + arbitrary keys array atomic. CLI's `pane run` is a wrapper.
```

`agent send <target> <text>` is the agent-namespace twin of `pane send-text` — identical bytes on the wire, only the target lookup differs.

## `pane run` — the canonical command-sender

```bash
herdr pane run <pane_id> "command to run"
```

Maps to `pane.send_input` IPC with `text="command to run"`, `keys=["Enter"]`. Writes the text into the pane's PTY, then sends an Enter key — **atomically in one IPC call**. The two halves cannot be split or interrupted.

Use `pane run` when:

- Sending a shell command to a bash pane (`pane run $BASH "make test"`)
- Sending a slash command to a TUI agent (`pane run $CLAUDE "/help"`, `pane run $PI "/clear"`)
- Sending a normal user prompt to an agent (`pane run $CODEX "Refactor X to remove duplication"`)

The atomicity matters: with `pane send-text` + `pane send-keys Enter` you get two IPCs and a tiny race window where another bash call could squeeze bytes in between. `pane run` closes that.

### Argument joining

CLI args after the pane id are joined with single spaces:

```bash
herdr pane run $PANE Refactor src/foo.py to deduplicate
# Effectively: pane run $PANE "Refactor src/foo.py to deduplicate"
```

Quote the whole prompt to be safe:

```bash
herdr pane run $PANE "Refactor src/foo.py. Reply DONE when finished."
```

## `pane send-text` — text only, no Enter

```bash
herdr pane send-text <pane_id> "text"
```

Writes bytes into the PTY. **Does not send Enter.** Use when you want to compose a multi-step input that you'll commit later — e.g. type some text, then Tab, then more text, then Enter.

For agent prompts where you just want "type and submit", `pane run` is strictly better (atomic).

## `pane send-keys` — special keys

```bash
herdr pane send-keys <pane_id> Enter
herdr pane send-keys <pane_id> Up Up Enter        # multiple, sent in order
herdr pane send-keys <pane_id> C-c                 # Ctrl+C
```

The key vocabulary is **tiny**. Here's the exhaustive list (from the source's `parse_api_key`):

```
Enter        enter
Tab          tab
Esc          esc
Backspace    backspace
Up           up
Down         down
Left         left
Right        right
C-c          c-c          ctrl+c           ← the ONLY Ctrl chord supported
<single ASCII char>                         ← any printable single character
```

Everything else fails with `invalid_key`. Specifically these are **not** supported:

- Any other Ctrl chord (`C-a`, `ctrl+a`, `Ctrl-c` with capital C, `ctrl-c` with hyphen) — only `Ctrl+C` works, and only in those three case-forms.
- Any Alt/Meta/Super modifier.
- Shift+Tab (`S-Tab`, `shift+tab`).
- Function keys (`F1`...`F12`).
- `Home`, `End`, `PageUp`, `PageDown`, `Delete`, `Insert`.
- `Space` as a name (use a literal " " or include space in `send-text`).
- `Escape` (must be `Esc`), `Return` (must be `Enter`), `BackSpace` (must be `Backspace`).

The keys array is **atomic fail-all** — if any one entry is invalid, the whole call rejects and no key is sent. Validate locally first or only send keys you're sure of.

### Common pattern: clearing a TUI input

A TUI prompt with garbage typed into it can be reset with `C-c` (most TUIs treat Ctrl+C as "cancel current input" without exiting the program):

```bash
herdr pane send-keys $PANE C-c
```

### Common pattern: navigating a menu

```bash
herdr pane send-keys $PANE Down Down Enter       # select third menu item
herdr pane send-keys $PANE Tab Tab Enter         # advance focus twice, then activate
```

## `pane.send_input` — the hidden atomic combo

Not exposed by CLI. Reach via raw IPC for the full flexibility:

```python
import socket, json
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/Users/you/.config/herdr/herdr.sock")
req = {
    "id": "x",
    "method": "pane.send_input",
    "params": {
        "pane_id": "w...-3",
        "text": "/help",        # optional
        "keys": ["Enter"]        # optional
    }
}
sock.sendall((json.dumps(req) + "\n").encode())
resp = b""
while not resp.endswith(b"\n"):
    chunk = sock.recv(4096)
    if not chunk: break
    resp += chunk
sock.close()
```

Both `text` and `keys` are optional. Server writes the text first, then plays the keys in order — all in one IPC. `pane run` is just this with `keys=["Enter"]` hard-coded.

Use cases the CLI can't cover:

- text + Tab + more text + Enter (form fill with autocomplete)
- text + Up Up Enter (re-run a history command with prefix)
- keys without text (just navigation)
- text without keys (= `send-text`; rarely worth the raw call)

If you find yourself doing two `send-text`s in a row, that's a code smell — combine to one `pane.send_input` call.

## Newlines: the bash-pane trap

The most dangerous foot-gun in the entire send surface. When you pass text containing real newline bytes (`\n`, 0x0A) to a pane:

- **TUI panes** (Claude, Pi, Codex, etc.) usually handle multi-line via **bracketed paste**: the server wraps your text in `\x1b[200~` ... `\x1b[201~`, the TUI recognizes this as a paste and keeps the text in the buffer for the user to Enter when ready.
- **Bash panes** typically do **not** have bracketed paste enabled by default. The raw `\n` bytes go straight to the shell, which interprets each as Enter and **executes each line as a separate command**.

So:

```bash
# In a TUI: safe — multi-line text appears in the prompt, awaiting Enter
herdr pane send-text $CLAUDE "Line 1\nLine 2\nLine 3"   # actually 21 chars including literal \n

# But with a real newline (not the escape sequence):
herdr pane send-text $BASH "$(printf 'line1\nline2')"
# Bash sees: line1<Enter>line2<Enter>  →  runs line1, then line2
```

**Rules of thumb:**

- Sending to **a TUI agent** (Codex, Claude, Pi): multi-line text is fine; the TUI will treat it as one paste and not execute until you Enter.
- Sending to **a bash pane**: keep text on one line. If you need multi-step, multiple `pane run` calls — one per command — is the safe path.
- If you must send multi-line to bash and the shell *has* bracketed paste enabled (modern zsh, bash 5+ with `enable-bracketed-paste on`), the server's `encode_api_text` wraps automatically — no Enter explosion. But **don't assume this** for unknown panes; test with a harmless multi-line first.

### Sanitization rule

When forwarding user input to a bash pane, strip `\n` before sending:

```bash
SAFE_INPUT="$(printf '%s' "$USER_INPUT" | tr -d '\n')"
herdr pane run $BASH "$SAFE_INPUT"
```

For TUI prompts, don't strip — multi-line might be intentional.

## Bracketed paste in detail

The server's `encode_api_text` function checks the pane's bracketed-paste state (reported by the terminal/Ghostty). If enabled, the text gets wrapped:

```
\x1b[200~  ...your text...  \x1b[201~
```

TUIs see this and know "this is a paste, buffer the whole thing, don't execute on intermediate newlines." Plain shells without bracketed paste support see the wrap as literal junk characters mixed with their input — they execute on every newline.

You cannot disable this from the API; herdr's choice is automatic based on the pane's terminal state. In practice: trust TUIs, distrust default bash.

## Performance note

CLI calls add ~25 ms each (subprocess startup). For high-frequency sends, talk directly to the socket; raw IPC is ~5 ms per call. For typical multi-agent orchestration this is invisible.

## Quick decision tree

| You want to | Use |
|---|---|
| Send a command to a bash pane (run it) | `pane run $PID "cmd"` |
| Send a slash command to a TUI agent | `pane run $PID "/help"` |
| Send a normal prompt to an agent | `pane run $PID "your prompt"` |
| Type text but **not** submit yet | `pane send-text $PID "..."` (no Enter) |
| Press special keys (Enter, Tab, arrows, Ctrl+C) | `pane send-keys $PID Enter` |
| Combine text + multiple keys atomically | `pane.send_input` IPC directly |
| Forward unknown user input to bash | Strip newlines first, then `pane run` |
| Forward user input to a TUI agent | `pane run` (or `agent send`) — multi-line OK |

## Anti-patterns

- **`pane send-text "cmd" && pane send-keys Enter`** — works but is two IPCs and racy. Use `pane run`.
- **Multi-line text into a bash pane without checking bracketed paste** — silent multi-command execution.
- **`pane send-keys $PANE F1`** — `F1` isn't in the vocabulary. Fails atomically.
- **`pane send-keys $PANE Ctrl-c`** — wrong syntax. Use `C-c` or `ctrl+c` (lowercase plus).
- **Trying to send a complex shortcut (Shift+Tab, Alt+X)** — not supported. Find another way to drive the TUI.

## When the CLI can't do what you need

If you need a key chord that isn't in `parse_api_key`, you can sometimes send the raw escape sequence via `pane send-text`:

```bash
herdr pane send-text $PANE $'\e[Z'    # Shift+Tab CSI sequence; TUI-dependent
```

This bypasses the herdr key parser and lets the terminal/TUI interpret the bytes directly. Fragile, TUI-dependent, but occasionally necessary for menu navigation. Test before relying on it.
