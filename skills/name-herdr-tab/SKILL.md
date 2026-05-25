---
name: name-herdr-tab
description: Build deterministic HERDR tab labels for spawned agents. Use when naming HERDR tabs or workspaces from a caller pane, generating a slug, resolving caller workspace/tab names, or avoiding HERDR tab label collisions.
---

# Name HERDR Tab

Use `scripts/name_herdr_tab.py` (or import `build_label`) when an agent needs a deterministic HERDR label for a pane, tab, or workspace it is about to create:

```bash
python3 scripts/name_herdr_tab.py --slug fix-spawn-race --mode pane
python3 scripts/name_herdr_tab.py --slug fix-spawn-race --mode tab
python3 scripts/name_herdr_tab.py --slug fix-spawn-race --mode space
```

Labels never overwrite anything; collisions are resolved with `-2`, `-3`, ... suffixes.

## Per-Mode Label Shape

The label format depends on the spawn target (chosen by `codex.py --in`):

| `--mode` | Labeled entity | Label format | Why |
|---|---|---|---|
| `pane` | new pane (via `pane.rename`) | `<slug>` | Caller's tab/space already give the human context; pane label stays short. |
| `tab` | new tab (via `tab.create label=…`) | `<caller-space>-<caller-tab>-<slug>` | Tab sits among the caller's other tabs; full caller context disambiguates. |
| `space` | new workspace + inner tab | workspace: `<caller-tab-name>`; inner tab: `<slug>` | Workspace label already carries caller context, so the inner tab needs only the slug. |

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
5. If the workspace label is `~`, use the caller tab name for the space segment.
6. Sanitize labels to lowercase `[a-z0-9-]`; use the id only when the label is missing or empty.

## Collision Suffix

The scope depends on the mode:

- `pane` — walk `pane.list` filtered to the caller's `tab_id`; suffix until free.
- `tab` — walk `tab.list` within the target workspace; suffix until free.
- `space` — the new workspace has no peer tabs at creation time, so the inner tab has no collisions to resolve; workspace-label collisions follow herdr's own rules.

## Worktree Mode (`codex.py --worktree`)

When the caller adds `--worktree`, the slug doubles as the branch name:

- Branch = `codex/<slug>` (collision-resolved to `codex/<slug>-2`, ...).
- Worktree path = `<repo>/.worktrees/codex-<slug>` (path tracks branch).
- The chosen `--mode` label still applies (pane/tab/space). The worktree changes only `cwd`.
