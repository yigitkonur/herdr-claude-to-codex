---
name: name-herdr-tab
description: Build deterministic HERDR tab labels for spawned agents. Use when naming HERDR tabs or workspaces from a caller pane, generating a slug, resolving caller workspace/tab names, or avoiding HERDR tab label collisions.
---

# Name HERDR Tab

Use `scripts/name_herdr_tab.py` when an agent needs a deterministic HERDR tab label:

```bash
python3 scripts/name_herdr_tab.py --slug fix-spawn-race
```

The label shape is:

```text
<caller-space-name>-<caller-tab-name>-<slug>
```

## Slug Rules

- Lowercase only.
- Charset: `[a-z0-9-]`.
- One to three hyphen-separated words.
- Each word starts and ends with `[a-z0-9]`.
- Reserved full words are rejected: `pi`, `claude`, `codex`, `opencode`, `hermes`, `herdr`, `default`, `none`, `null`.

Good: `fix-spawn-race`, `audit-ui`, `clone-repo`.

Bad: `Fix-Spawn`, `fix_spawn`, `fix--race`, `one-two-three-four`, `codex`.

## Caller Names

Resolve from the running HERDR pane:

1. Read `HERDR_PANE_ID`.
2. `pane.get` gives `workspace_id` and `tab_id`.
3. `workspace.get` gives the caller workspace label.
4. `tab.get` gives the caller tab label.
5. Sanitize labels to lowercase `[a-z0-9-]`; use the id only when the label is missing or empty.

## Collision Suffix

Check `tab.list` in the target workspace. If the assembled label exists, append `-2`, then `-3`, and so on.
