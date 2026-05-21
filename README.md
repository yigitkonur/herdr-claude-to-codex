# herdr-claude-to-codex

**A Claude Code skill that lets Claude drive a `codex` sub-agent end-to-end through one tool — over the **herdr** terminal multiplexer.**

Claude Code starts a task, hands it to Codex running in a side pane, and gets back **one structured JSON verdict** telling it exactly what happened and what to do next. No screen-scraping, no status polling, no babysitting — the Python layer absorbs every sharp edge and Claude just reads `result.state` and runs `result.next_action.command`.

```
Claude Code ──run a codex.py verb in the background──▶  Codex (in a herdr pane)
     ▲                                                        │
     └────────  one JSON verdict (state + next_action)  ◀─────┘
```

---

## Why this exists

Driving another AI agent from a script is deceptively hard. A terminal agent's `idle` status means *the turn ended* — but that could be "task finished," "I have a question," or "here's a plan, approve it?" — all indistinguishable by status alone. Plans scroll off the visible screen. Sends get eaten during startup. Menus render a beat after the status settles. Panes renumber when a sibling closes.

This skill encodes the answers to all of that (each one verified live against Codex) so the orchestrator doesn't have to:

| Hard problem | Handled by |
|---|---|
| `idle`/`done` ≠ "complete" (finished vs question vs plan-menu vs blocked widget) | the analyzer → `state` / `reason` / `next_action` |
| Task sent during Codex's MCP/TUI init is silently lost | wait for a genuinely ready, stable composer |
| Long plans scroll off the visible screen | read full scrollback (`agent.read --source recent`) |
| A narrow split mangles plans & option labels (`Yes, impleme…`) | spawn Codex full-width in its own tab |
| Completion via a keyword alone is unreliable | completion marker **AND** artifact verification |
| Codex emits idle blips *between* work bursts | a resume-grace loop that re-reads the screen |
| Plan approval redraws the screen blank | confirm Codex re-entered `working` before settling |
| A pane's slot id shifts when another pane closes | sessions keyed on the stable `terminal_id` |

The result: a token-efficient, resilient interface that **never gets stuck** and always tells the orchestrator the next move.

---

## Requirements

- **herdr** — the AI-aware terminal multiplexer ([ogulcancelik/herdr](https://github.com/ogulcancelik/herdr)). Its server must be running (`herdr status` shows `running`).
- **Codex CLI** — the agent being driven (verified against `v0.132.0`, gpt-5.x).
- **Python 3** — **no `pip` dependencies**. The herdr socket transport is a *vendored*, zero-dependency copy of [`herdr-python-client`](https://github.com/54rt1n/herdr-python-client) (Apache-2.0) under `scripts/herdr_client/`, so the skill is fully self-contained.
- **Claude Code** — the orchestrator that invokes the skill.

> The skill talks to herdr over its Unix socket (`~/.config/herdr/herdr.sock`). It also works for Pi / Claude / OpenCode / Hermes panes via the lower-level scripts, but `codex.py` is the perfected, first-class path for Codex.

---

## Install

Clone into your Claude Code skills directory so `SKILL.md` lands at the skill root:

```bash
git clone https://github.com/yigitkonur/herdr-claude-to-codex ~/.claude/skills/skill-herdr
```

Claude Code auto-discovers the skill and invokes it whenever you delegate to Codex ("have codex do X", "run this in the background", "continue this session", …).

---

## Quick start

The whole interaction is three steps: **background a verb → read the verdict → do `next_action`.**

```bash
SKILL=~/.claude/skills/skill-herdr

# 1. Start a task (run in the BACKGROUND so Claude Code is notified on completion).
python3 $SKILL/scripts/codex.py start \
  --task "Refactor src/foo.py to remove duplication; keep behavior identical." \
  --expect src/foo.py --timeout 600

# 2. The verdict comes back as one JSON envelope. Read result.state, then:
#      completed              -> done; run `end`
#      awaiting_clarification -> reply --text "<answer>"   (questions in result.questions)
#      awaiting_approval      -> reply --approve            (plan in result.plan)
#      permission_gate        -> reply --approve / --reject
#      working (timeout)      -> await --session <id>
#      no_signal              -> status --session <id>

# 3. Clean up when finished.
python3 $SKILL/scripts/codex.py end --session <id>
```

In Claude Code you don't run these by hand — you let it drive. You just say *"have codex build the landing page"* and it backgrounds `start`, waits for the notification, and acts on the verdict.

---

## The `codex.py` interface

```
codex.py start  --task "<p>" [--plan] [--expect PATH]... [--cwd DIR] [--label NAME]
                [--marker STR] [--timeout 600] [--no-wait]
codex.py send   --session <id> --message "<p>" [--expect PATH]... [--timeout 600]
codex.py reply  --session <id> (--text "…" | --choice N | --approve | --reject) [--expect PATH]...
codex.py await  --session <id> [--expect PATH]... [--timeout 600]
codex.py status --session <id> [--expect PATH]...      # one-shot, no wait
codex.py end    --session <id>                          # close pane + delete state
codex.py sessions                                       # list live, prune dead
```

The minimum is `codex.py start --task "…"` — Python injects the completion marker and the "ask me if unsure" discipline; you supply no scaffolding.

### The JSON verdict

Pure JSON on stdout, one stable envelope:

```jsonc
{ "ok": true, "schema_version": "v1", "command": "start", "session": "cdx-3f9a",
  "result": {
    "state":  "completed|awaiting_clarification|awaiting_approval|permission_gate|working|no_signal|exited",
    "reason": "marker_verified|marker_unverified|free_text_question|multiple_choice|plan_approval|permission_request|working|timeout|no_signal|pane_gone",
    "summary": "<=200 chars",
    "plan": "<full plan text — never truncated — when awaiting approval>",
    "questions": ["…"], "options": [{"key":"1","label":"…","recommended":true}],
    "marker_found": true, "artifacts": [{"path":"…","exists":true,"bytes":4561}],
    "transcript_tail": "<cleaned last message>",
    "next_action": {"intent":"answer|approve|choose|verify|wait|start|nothing","command":"…","why":"…"}
  }, "error": null }
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Valid verdict produced (any `state` — read `result.state`) |
| `2` | Usage error |
| `3` | herdr environment error (server down / socket missing — `error.code: HERDR_DOWN`, retryable) |
| `4` | Session / pane not found or died |
| `5` | Internal error |

---

## Plan mode

```bash
codex.py start --plan --task "Build a single-page site for a coffee shop."
```

Returns `awaiting_approval` / `plan_approval` with the **full, untruncated plan** in `result.plan` and the menu in `result.options`. Approve with `reply --approve`, keep planning with `reply --reject`, or pick a path with `reply --choice N`. After approval, `reply --approve` rides the implementation through its work bursts to `completed`.

---

## Beyond one Codex

`codex.py` is the perfected path for a **single Codex session** — the focus of this repo. For parallel fleets, other agents (Pi / Claude / OpenCode / Hermes), or custom tooling, drop to **raw herdr**: the `references/` deep-dives document the full substrate — the agent-vs-pane namespace, the send-keys vocabulary, the status model, events / subscribe, pane lifecycle, and the complete CLI + hidden IPC — so you can compose your own orchestration on top of it.

---

## Repository layout

```
SKILL.md                     # the skill contract Claude Code loads (start here)
README.md                    # this file
scripts/
  codex.py                   # the single Codex interface (agent-facing)
  _core.py                   # shared engine: registry, spawn/send/wait, analyzer
  test_analyze.py            # deterministic analyzer regression test (no spawning)
  herdr_client/              # vendored herdr socket client (Apache-2.0) — the transport
references/                  # 15 single-topic deep-dives (load on demand)
  codex-and-agents.md        # everything verified live about driving Codex
  scripting-patterns.md      # codex.py + _core.py internals and the contract
  architecture.md agent-vs-pane.md status-model.md waiting-and-async.md …
```

---

## Testing

The analyzer (the heart of the skill) has a deterministic, spawn-free regression test:

```bash
python3 scripts/test_analyze.py     # feeds crafted screen tails; asserts state/reason/next_action
python3 -m py_compile scripts/_core.py scripts/codex.py
```

It covers every verdict branch plus the hard-won edge cases: numbered-questions-aren't-a-menu, plan-never-truncated, plan-not-ballooned-on-completion, the blocked multiple-choice widget, transcript-tail cleanliness (banner / prompt-echo / internal-skill noise stripped), and rejecting a marker that a plan merely *documents* vs one actually *printed*.

Every behavior in `references/codex-and-agents.md` was confirmed by driving a live Codex through herdr.

---

## Credits

The herdr socket transport under `scripts/herdr_client/` is a vendored copy of
[`herdr-python-client`](https://github.com/54rt1n/herdr-python-client) by **Martin Bukowski**,
used under the **Apache License 2.0** (see `scripts/herdr_client/LICENSE` and `NOTICE`). The
library source is unmodified; `_core.py` builds the Codex orchestration on top of it.

## License

This skill is MIT, except `scripts/herdr_client/` which is Apache-2.0 (see its `LICENSE`/`NOTICE`).
