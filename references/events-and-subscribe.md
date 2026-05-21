# Events, subscribe, and hidden IPC

The CLI hides the most powerful primitive. `events.subscribe` over a raw socket is what unlocks centralized real-time monitoring across many panes. Most multi-agent flows don't need it — `agent wait` (background) is enough. Reach for subscribe when you have many panes, many event types, or need a single stream.

## The hidden IPC methods

There are 38 server methods; **5 are not exposed by the CLI**. Discover them with the invalid-method trick:

```bash
echo '{"id":"x","method":"INVALID","params":{}}' | nc -U ~/.config/herdr/herdr.sock
# Error message lists all valid methods.
```

CLI-not-exposed methods:

| Method | What it does |
|---|---|
| `pane.send_input` | text + arbitrary keys array atomically — covered in `sending-input.md` |
| `pane.clear_agent_authority` | Remove all (or one source's) agent registrations from a pane — covered in `fake-and-custom-agents.md` |
| `pane.release_agent` | Remove one specific (source, agent) registration — covered in `fake-and-custom-agents.md` |
| `events.subscribe` | Open a long-lived event stream (this file) |
| `events.wait` | Block for one event matching a filter — underlies CLI `wait agent-status` / `wait output` |

Talk to any of these by writing JSON-line requests to `~/.config/herdr/herdr.sock`.

## `events.subscribe` — the streaming primitive

```python
import socket, json

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/Users/you/.config/herdr/herdr.sock")
req = {
    "id": "sub-1",
    "method": "events.subscribe",
    "params": {
        "subscriptions": [
            {"type": "pane.agent_status_changed", "pane_id": "w...-2"},
            {"type": "pane.agent_status_changed", "pane_id": "w...-3"},
            {"type": "pane.created"}
        ]
    }
}
sock.sendall((json.dumps(req) + "\n").encode())

# First message back is the ACK:
# {"id":"sub-1","result":{"type":"subscription_started"}}

# After that, events stream in, one JSON object per line, until you close the socket.
buf = b""
while True:
    chunk = sock.recv(8192)
    if not chunk: break
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        if line:
            obj = json.loads(line.decode())
            if "data" in obj:
                handle_event(obj)
```

The ACK (`subscription_started`) comes first. It's a strong ordering guarantee — any event you get *after* the ACK was generated after you subscribed. Events before the ACK are not delivered.

## The 13 event types

```
workspace.created     workspace.closed       workspace.focused
tab.created           tab.closed             tab.focused          tab.renamed
pane.created          pane.closed            pane.focused         pane.exited
pane.agent_detected
pane.output_matched   pane.agent_status_changed
```

Most of these need no filter (they fire for any matching event globally).

**Two require filters in the subscription request:**

- `pane.agent_status_changed` requires `pane_id`. Optionally `agent_status` to receive only specific transitions.
- `pane.output_matched` requires `pane_id`, `source`, and `match` (substring or regex). Optionally `lines`, `strip_ansi`.

Example with filter:
```json
{
  "type": "pane.output_matched",
  "pane_id": "w...-2",
  "source": "recent",
  "match": {"regex": "ERROR.*line [0-9]+"},
  "strip_ansi": true
}
```

## Event payload shape

```json
{
  "event": "pane.agent_status_changed",
  "data": {
    "agent": "pi",
    "agent_status": "done",
    "pane_id": "w...-2",
    "workspace_id": "w..."
  }
}
```

The top-level `id` field is absent — events are unsolicited. Match `event` for the type, dig into `data` for the payload.

## The one-subscribe-per-socket rule

**Critical:** If you send a second `events.subscribe` request on a socket that already has an active subscription, both subscriptions die silently — no ACK on the second, no further events on the first. The socket effectively becomes dead.

Two ways to subscribe to many things:

1. **One subscribe, big array.** Pack everything you care about into the `subscriptions` array in a single request. This is what you should usually do.
2. **One socket per subscribe.** Open multiple sockets for orthogonal concerns.

You cannot dynamically add new subscriptions to an existing socket. If you need to subscribe to a newly-spawned pane mid-stream, open a second socket for it.

## When subscribe beats background-wait

`agent wait` in background mode is enough for most multi-agent work. Subscribe wins when:

- You're watching **many panes** simultaneously and want **one consumer** for all events rather than N background tasks.
- You need to react to events the CLI's `wait` commands don't cover (`pane.created`, `tab.focused`, `pane.exited`).
- You want a long-lived event log for diagnostics.
- You need to filter event flows with custom logic that doesn't fit the simple `--match` / `--status` shape.

For "spawn 3 agents, wait for each" — background waits are simpler.

## What events do NOT fire

Important holes:

- **`done → idle` transition** — when the seen flag flips from false to true, the internal state doesn't change (it was Idle the whole time), so no `pane.agent_status_changed` event is emitted. You can't react to "the human saw it" via events.
- **`custom_status` changes alone** — if a `pane.report_agent` call only updates `custom_status` (not state), it's unclear whether an event fires. Don't rely on it.
- **`revision` updates** — not an event source; `revision` is a response field.
- **Workspace-level status aggregation** — no event for the aggregated workspace status. Subscribe to per-pane events and aggregate yourself if you need it.

## ACK ordering — useful guarantee

After `events.subscribe`, the server's first response is **always** `{"result":{"type":"subscription_started"}}` (with the matching `id`). Any event payloads come after this.

So the canonical client loop:

```python
sock.sendall((json.dumps(req) + "\n").encode())
ack = read_one_json_line(sock)
assert ack.get("result", {}).get("type") == "subscription_started"
# Now safe to expect events
```

If the subscription is malformed (e.g. missing `pane_id` for `pane.agent_status_changed`), you get an `error` instead of the ACK. Catch and report.

## Subscription persistence

A subscription lives only as long as the socket. When you close the connection, the subscription is gone. There's no way to "unsubscribe but keep the socket open" — the API doesn't expose it. Close the socket to stop.

If the server restarts (rare), all subscriptions are lost; reconnect and re-subscribe.

## Full Python client snippet

```python
import socket, json, threading, queue

def subscribe_stream(subscriptions, socket_path="/Users/you/.config/herdr/herdr.sock"):
    q = queue.Queue()
    def reader():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)
        sock.sendall((json.dumps({
            "id": "s",
            "method": "events.subscribe",
            "params": {"subscriptions": subscriptions}
        }) + "\n").encode())
        buf = b""
        try:
            while True:
                chunk = sock.recv(8192)
                if not chunk: break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        q.put(json.loads(line))
        finally:
            sock.close()
            q.put(None)  # sentinel
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return q

# Usage:
q = subscribe_stream([
    {"type": "pane.agent_status_changed", "pane_id": "w...-2"},
    {"type": "pane.created"},
])
while True:
    msg = q.get()
    if msg is None: break
    if "data" in msg:
        print("Event:", msg["event"], msg["data"])
```

## `events.wait` — block for one matching event

Single-shot variant of subscribe. Blocks the request until one event matches the filter, then returns that event. CLI `wait agent-status` and (likely) `wait output` are wrappers.

```json
{
  "method": "events.wait",
  "params": {
    "match_event": {"type": "pane.agent_status_changed", "pane_id": "w...-2", "agent_status": "idle"},
    "timeout_ms": 60000
  }
}
```

Use directly when you want strict event semantics without the CLI wrapper's defaults. Not commonly needed.

## When *not* to subscribe

- You're watching one pane. `agent wait` in background is simpler.
- You're watching for "command finished". Same.
- You don't have a long-lived process to consume the stream. Subscriptions die when the socket closes.

For most Claude Code use, **background waits + occasional `pane read`** is the right toolkit. Subscribe shines when you want one orchestrator program watching everything.

## Anti-patterns

- **Two subscribes on one socket.** Silent death. One socket per subscribe (or pack everything in one array).
- **Subscribe and then expect to send other IPC commands on the same socket.** Subscribe locks the connection to events. Open a separate socket for commands.
- **Subscribe without parsing the ACK.** You'll race against late ACK bytes and confuse yourself. Always consume the ACK first.
- **Subscribe to `pane.agent_status_changed` without `pane_id`.** Server returns `invalid_request` — `pane_id` is required for this event type.

## When you need it in Claude Code

Mostly: you don't. Stick to `agent wait` background. Reach for direct `events.subscribe` only when you're building a watcher process that the user explicitly wants — e.g. "watch these 10 panes and tell me when any blocked" — and even then, 10 background waits work fine.

The hidden value of subscribe is **knowing it exists**: when a more sophisticated pattern is needed (custom event filtering, single consumer for many event types), you have an escape hatch.
