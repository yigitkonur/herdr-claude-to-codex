#!/usr/bin/env python3
"""_core.py — shared engine for codex.py (NOT an agent-facing entry point).

Everything codex.py needs lives here: the herdr Unix-socket RPC, the durable
session registry (keyed on the STABLE terminal_id so pane-slot renumbering can
never break a handle), the spawn/send/wait primitives, and the analyzer that
turns a settled pane into structured reasoning (state/reason/plan/questions/
options/next_action) instead of a bare status word.

Run nothing here directly. codex.py imports it.

Verified facts this encodes (from live Codex testing):
  - pane.read returns plain payload nested at result.read.text (NOT result.text).
  - agent.start nests the new pane at result.agent.pane_id (NOT result.pane).
  - pane_id's -N suffix is a slot index that shifts when a lower pane closes;
    terminal_id is stable -> we re-resolve pane_id from terminal_id every call.
  - A turn ending (idle/done) does NOT mean complete: it can be a finished task,
    a free-text question, or a plan-approval menu — all report idle/done.
  - Codex's structured multiple-choice widget ("Question N/N ... enter to submit
    answer") reports BLOCKED, not idle; a plan-approval menu reports idle/done.
  - The completion marker is echoed in the prompt, so it must be matched only as
    a STANDALONE output line, never as a substring.
  - The status event can precede the menu render by >1s -> re-check loop.
"""
import json
import os
import re
import socket
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SOCKET_PATH = os.environ.get(
    "HERDR_SOCKET_PATH", os.path.expanduser("~/.config/herdr/herdr.sock")
)
STATE_DIR = os.path.expanduser("~/.cache/skill-herdr/sessions")
SETTLED = {"idle", "done", "blocked"}
TAIL_LINES = 60                # read enough to capture a plan block + menu
SETTLE_DELAY = 0.8             # status event can beat the screen paint
RECHECK_TRIES = 5              # re-read while a long plan/menu finishes painting
RECHECK_DELAY = 1.0
# A bare turn-end (idle/done, no marker/question/menu) is ambiguous: Codex often
# emits short idle blips BETWEEN work bursts while implementing (verified). Before
# concluding "no_signal", give it this long to resume working.
NO_SIGNAL_GRACE = 6.0
REGISTER_TIMEOUT = 20          # codex SessionStart hook registers within ~5s
# Codex registers (SessionStart -> idle) BEFORE it can accept input: it keeps
# doing MCP handshakes / TUI paint after, and a task sent in that window is
# silently lost (verified). wait_until_ready() gates on a painted, stable,
# churn-free composer; send_task_verified() then confirms the submit landed.

# A numbered option line: "1. ...", "› 1. ...", "2) ...".
_MENU_RE = re.compile(r"^\s*[›>]?\s*(\d+)[.)]\s+(.+?)\s*$")
# Codex's structured multiple-choice widget signatures.
_WIDGET_RE = re.compile(r"enter to submit answer|Question\s+\d+\s*/\s*\d+", re.I)
# Plan-approval menu signature.
_PLAN_MENU_RE = re.compile(r"Implement this plan\?|stay in Plan mode", re.I)
# Plan body headers (where a plan block starts).
_PLAN_HEAD_RE = re.compile(r"^\s*(#+\s|Proposed Plan|## Summary|## Plan)", re.I)
# TUI chrome to strip from the transcript tail.
_CHROME_RES = [
    re.compile(r"^[\s│└├┘┐┌┤┴┬┼─—═╗╝╔╚╰╯╭╮↑↓›]*$"),       # separators / box / blank-ish
    re.compile(r"gpt-[\d.]+.*(Context|window|used)", re.I),  # status bar
    re.compile(r"Context\s+\d+%\s+used", re.I),             # status bar (alt form)
    re.compile(r"esc to interrupt|\? for shortcuts", re.I),
    re.compile(r"Worked for \d|view transcript|ctrl ?\+ ?t", re.I),  # turn footer
    # MCP / network init noise — harmless, never the agent's real message.
    re.compile(r"MCP server|Starting MCP|MCP startup|mcp-d4s|mcp_servers|handshaking", re.I),
    re.compile(r"StreamableHttp|streamable_http|http_client|rmcp|reqwest|Transport", re.I),
    re.compile(r"Bad Gateway|proxy rejected|HTTP request failed|"
               r"error sending request|Client error:|Unexpected status\s+\d+", re.I),
    re.compile(r"http://127|https?://\S+/mcp", re.I),
    # promotional / app chrome
    re.compile(r"Tip:|Build faster with the Codex|codex app|chatgpt\.com/codex", re.I),
    # Codex startup banner box — leaks on short turns before output scrolls it off.
    # A FULLY boxed row (both ends │); code-gutter lines have a leading │ only.
    re.compile(r"^\s*│.*│\s*$"),
    re.compile(r">_ OpenAI Codex|/model to change|permissions:\s*YOLO", re.I),
    # Codex's own internal skill-file reads — noise, not the user's task activity.
    re.compile(r"superpowers:|using-superpowers|executing-plans|verification-before-completion", re.I),
    # our own injected completion-contract boilerplate, echoed back in history.
    # The TUI wraps our single-line prompt, so match every fragment it splits into.
    re.compile(r"print this token|end your turn by asking|instead of guessing|"
               r"FULLY complete|If you need information or a decision|"
               r"by asking your question|^\s*guessing\.?\s*$|^\s*line:\s", re.I),
    re.compile(r"^\s*(Working|Esc to|Press enter|tab to|ctrl\+)", re.I),
]
# The composer's rotating placeholder/suggestion line (varies run to run); these
# are NOT user/agent content. Used to recognise an EMPTY composer.
_PLACEHOLDER_HINTS = re.compile(
    r"Find and fix a bug|Implement \{feature\}|Write tests for|Summarize recent|"
    r"Run /review|Explain (this|the)|What does|Add a |Refactor ", re.I)
_UNCERTAINTY_RE = re.compile(
    r"\b(i think|probably|not sure|unclear|i assume|i'?ll assume|might be|"
    r"could be|let me know|please confirm|which would you|do you want)\b", re.I
)
# The persistent bottom status bar — present once the TUI is painted and ready.
_STATUSBAR_RE = re.compile(r"gpt-[\d.]+.*(Context|window|used)|Context\s+\d+%\s+used", re.I)
# The composer input prompt glyph at the start of the input line.
_PROMPT_RE = re.compile(r"^\s*[›>]\s?")


# ---------------------------------------------------------------------------
# Socket RPC
# ---------------------------------------------------------------------------
class HerdrError(Exception):
    """Raised for environment / protocol failures (server down, pane gone)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def rpc(method, params, socket_path=SOCKET_PATH, timeout=10):
    if not os.path.exists(socket_path):
        raise HerdrError("HERDR_DOWN",
                         f"herdr socket not found at {socket_path}; is the server running?")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(socket_path)
    except OSError as e:
        raise HerdrError("HERDR_DOWN", f"cannot connect to {socket_path}: {e}")
    try:
        s.sendall((json.dumps({"id": "x", "method": method, "params": params}) + "\n").encode())
        buf = b""
        s.settimeout(timeout)
        while not buf.endswith(b"\n"):
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    # A connected-but-empty/garbled reply is a server hangup (an environment
    # fault), not our internal bug — map it to HERDR_DOWN (exit 3), not exit 5.
    if not buf.strip():
        raise HerdrError("HERDR_DOWN",
                         f"herdr closed the connection with no response (method={method})")
    try:
        return json.loads(buf.decode())
    except (ValueError, UnicodeDecodeError):
        raise HerdrError("HERDR_DOWN",
                         f"herdr returned an unparseable response (method={method})")


def current_status(pane_id, socket_path=SOCKET_PATH):
    """Return the pane's current API status, or None if the pane is gone or the
    reply is unreadable. Never raises on a malformed/empty result — it is called
    bare inside the settle/verify loops and must degrade to 'gone', not crash."""
    resp = rpc("pane.get", {"pane_id": pane_id}, socket_path)
    try:
        return resp["result"]["pane"].get("agent_status")
    except (KeyError, TypeError):
        return None


def _read(pane_id, source, lines, socket_path=SOCKET_PATH):
    """agent.read (supports 'recent' scrollback) with a pane.read visible
    fallback. Returns plain text, or '' if the pane is unreadable."""
    resp = rpc("agent.read", {"target": pane_id, "source": source, "lines": lines}, socket_path)
    if "error" in resp:
        resp = rpc("pane.read",
                   {"pane_id": pane_id, "source": "visible", "lines": lines, "format": "text"},
                   socket_path)
        if "error" in resp:
            return ""
    return resp.get("result", {}).get("read", {}).get("text", "")


def read_tail(pane_id, lines=TAIL_LINES, socket_path=SOCKET_PATH):
    """Full recent transcript via 'recent' scrollback — captures plans and
    end-of-task reports that scroll past the small visible screen. The visible
    screen alone (≈37 lines) silently truncates long plans; 'recent' does not."""
    return _read(pane_id, "recent", max(lines, 240), socket_path)


def read_screen(pane_id, lines=40, socket_path=SOCKET_PATH):
    """Just the current visible screen — precise for composer / readiness checks
    where stale scrollback content would be misleading."""
    return _read(pane_id, "visible", lines, socket_path)


def send_text_enter(pane_id, text, socket_path=SOCKET_PATH):
    """Atomic text + Enter (pane.send_input)."""
    return rpc("pane.send_input", {"pane_id": pane_id, "text": text, "keys": ["Enter"]}, socket_path)


def send_text(pane_id, text, socket_path=SOCKET_PATH):
    """Type text into the composer WITHOUT submitting (no Enter)."""
    return rpc("pane.send_input", {"pane_id": pane_id, "text": text, "keys": []}, socket_path)


def send_keys(pane_id, keys, socket_path=SOCKET_PATH):
    return rpc("pane.send_keys", {"pane_id": pane_id, "keys": keys}, socket_path)


def release_agent(pane_id, source="herdr:codex", agent="codex", socket_path=SOCKET_PATH):
    return rpc("pane.release_agent",
               {"pane_id": pane_id, "source": source, "agent": agent}, socket_path)


def close_pane(pane_id, socket_path=SOCKET_PATH):
    return rpc("pane.close", {"pane_id": pane_id}, socket_path)


def list_panes(socket_path=SOCKET_PATH):
    resp = rpc("pane.list", {}, socket_path)
    return resp.get("result", {}).get("panes", [])


def wait_for_settle(pane_id, timeout, socket_path=SOCKET_PATH):
    """Subscribe and block until the pane reaches a settled status, or timeout.
    Returns the settled status, or None on timeout."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(socket_path)
    req = {"id": "w", "method": "events.subscribe",
           "params": {"subscriptions": [{"type": "pane.agent_status_changed", "pane_id": pane_id}]}}
    s.sendall((json.dumps(req) + "\n").encode())
    s.settimeout(timeout)
    deadline = time.time() + timeout
    buf = b""
    try:
        while time.time() < deadline:
            try:
                chunk = s.recv(8192)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                st = json.loads(line.decode()).get("data", {}).get("agent_status")
                if st in SETTLED:
                    return st
    finally:
        s.close()
    return None


# ---------------------------------------------------------------------------
# Composer state — is text sitting UNSENT in the input box?
# ---------------------------------------------------------------------------
def composer_text(tail):
    """Best-effort: the bottom-most input-composer line content (what is typed but
    not yet submitted), with the prompt glyph stripped. The composer sits just
    above the gpt status bar. Numbered menu lines ('› 1. ...') are NOT the
    composer. Returns '' if no composer line is visible."""
    lines = [ln.rstrip() for ln in tail.splitlines()]
    sb = None
    for i, ln in enumerate(lines):
        if _STATUSBAR_RE.search(ln):
            sb = i
    region = lines[:sb] if sb is not None else lines
    for ln in reversed(region):
        if _PROMPT_RE.match(ln):
            body = _PROMPT_RE.sub("", ln).rstrip()
            if _MENU_RE.match(ln):       # a menu option, not the composer
                continue
            return body
    return ""


def composer_holds(tail, text):
    """True if (the start of) `text` is still sitting in the composer unsent.
    Robust to the rotating placeholder: an empty composer shows a short
    suggestion that won't contain our distinctive opening words."""
    chunk = " ".join(text.split())[:24].lower()
    if not chunk:
        return False
    comp = " ".join(composer_text(tail).split()).lower()
    if not comp or _PLACEHOLDER_HINTS.search(comp):
        return False
    return chunk[:16] in comp


def wait_until_ready(pane_id, timeout=REGISTER_TIMEOUT, socket_path=SOCKET_PATH):
    """Block until Codex is genuinely input-ready: settled status AND the status
    bar is painted AND MCP startup churn has cleared, stable across two reads.
    Returns True if ready, False on timeout (caller should still try — the
    verified send is the safety net)."""
    deadline = time.time() + timeout
    stable = 0
    while time.time() < deadline:
        st = current_status(pane_id, socket_path)
        if st is None:
            return False
        tail = read_screen(pane_id, 16, socket_path)
        ready = (st in ("idle", "done")
                 and bool(_STATUSBAR_RE.search(tail))
                 and "Starting MCP servers" not in tail)
        if ready:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        time.sleep(0.6)
    return False


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------
def _pane_id_for_terminal(terminal_id, socket_path=SOCKET_PATH):
    for p in list_panes(socket_path):
        if p.get("terminal_id") == terminal_id:
            return p["pane_id"]
    return None


def spawn_codex(label, cwd=None, argv=None, socket_path=SOCKET_PATH):
    """Spawn Codex in a FULL-WIDTH pane of its own and wait until it is genuinely
    input-ready. Returns {pane_id, terminal_id, tab_id, agent, registered}.

    Why a dedicated tab: agent.start splits the focused tab, and a narrow split
    (~28 cols when several panes share a tab) makes Codex hard-wrap and ellipsize
    its plans and menu options ("Yes, impleme…"), corrupting what we parse. We
    create a tab, start Codex in it, then close the leftover root shell so Codex
    fills the tab (~130 cols verified) — clean plans, clean option labels.
    """
    tc = rpc("tab.create", {}, socket_path)
    if "error" in tc:
        raise HerdrError("SPAWN_FAILED", f"tab.create failed: {tc['error']}")
    tab_id = tc["result"]["tab"]["tab_id"]
    root_pane = tc["result"]["root_pane"]["pane_id"]
    rpc("tab.focus", {"tab_id": tab_id}, socket_path)

    params = {"name": label, "focus": True, "argv": argv or ["codex"]}
    if cwd:
        params["cwd"] = cwd
    resp = rpc("agent.start", params, socket_path)
    if "error" in resp:
        rpc("tab.close", {"tab_id": tab_id}, socket_path)
        raise HerdrError("SPAWN_FAILED", f"agent.start failed: {resp['error']}")
    ag = resp["result"]["agent"]          # NOTE: result.agent, not result.pane
    terminal_id = ag.get("terminal_id")

    # Close the leftover root shell so Codex expands to the full tab width. This
    # renumbers pane slots, so always re-resolve Codex's pane_id by terminal_id.
    rpc("pane.close", {"pane_id": root_pane}, socket_path)
    time.sleep(0.5)
    pane_id = _pane_id_for_terminal(terminal_id, socket_path) or ag["pane_id"]

    registered = False
    deadline = time.time() + REGISTER_TIMEOUT
    while time.time() < deadline:
        g = rpc("pane.get", {"pane_id": pane_id}, socket_path)
        if "result" in g and g["result"]["pane"].get("agent"):
            registered = True
            break
        time.sleep(0.5)
    # Registration != input-ready: Codex keeps doing MCP handshakes / TUI paint
    # after its SessionStart->idle, and a send in that window is silently lost.
    wait_until_ready(pane_id, REGISTER_TIMEOUT, socket_path)
    return {"pane_id": pane_id, "terminal_id": terminal_id, "tab_id": tab_id,
            "agent": ag.get("agent") or "codex", "registered": registered}


def send_task_verified(pane_id, text, socket_path=SOCKET_PATH, tries=4):
    """Submit `text` to the composer and CONFIRM it actually went through, rather
    than trusting a single fire-and-forget send. Two phases per attempt:

      A. Ensure the text is in the composer (type it if it isn't).
      B. Press Enter, then verify submission: Codex went `working`, OR the
         composer no longer holds our text (it cleared / scrolled into history).

    Retries the whole cycle if a send was eaten during init churn or an embedded
    newline left the input multi-line and unsent. Returns True if it landed.

    `text` MUST be a single line (callers join multi-line prompts with spaces) —
    an embedded newline can submit the first line early and strand the rest.
    """
    for _ in range(tries):
        tail = read_screen(pane_id, 24, socket_path)
        if not composer_holds(tail, text):
            send_text(pane_id, text, socket_path)   # type only; no submit yet
            time.sleep(0.7)
            tail = read_screen(pane_id, 24, socket_path)
        if composer_holds(tail, text):
            send_keys(pane_id, ["Enter"], socket_path)
            time.sleep(1.1)
            if current_status(pane_id, socket_path) == "working":
                return True
            if not composer_holds(read_screen(pane_id, 24, socket_path), text):
                return True
        time.sleep(0.8)
    # Final adjudication.
    if current_status(pane_id, socket_path) == "working":
        return True
    return not composer_holds(read_screen(pane_id, 24, socket_path), text)


def await_started(pane_id, timeout=10, socket_path=SOCKET_PATH):
    """After a menu action that should trigger work (approve a plan / pick a
    choice), wait briefly for Codex to ENTER `working`. Approving switches Plan->
    Default with a screen redraw; without this, the settle that follows can read
    the blank transitional screen and wrongly conclude no_signal before the
    implementation even starts. Returns True if `working` was observed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if current_status(pane_id, socket_path) == "working":
            return True
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# Session registry (keyed on STABLE terminal_id)
# ---------------------------------------------------------------------------
def _session_file(session_id):
    return os.path.join(STATE_DIR, f"{session_id}.json")


def save_session(rec):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(_session_file(rec["session"]), "w") as f:
        json.dump(rec, f, indent=2)


def load_session(session_id):
    try:
        with open(_session_file(session_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def delete_session(session_id):
    try:
        os.remove(_session_file(session_id))
    except FileNotFoundError:
        pass


def all_sessions():
    if not os.path.isdir(STATE_DIR):
        return []
    out = []
    for fn in os.listdir(STATE_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(STATE_DIR, fn)) as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                pass
    return out


def resolve_pane_id(rec, socket_path=SOCKET_PATH):
    """Re-resolve the LIVE pane_id from the stable terminal_id. pane_id slots
    shift when other panes close, so we never trust a stored pane_id directly.
    Returns the live pane_id, or None if the terminal is gone (session dead)."""
    term = rec.get("terminal_id")
    for p in list_panes(socket_path):
        if p.get("terminal_id") == term:
            if p["pane_id"] != rec.get("pane_id"):
                rec["pane_id"] = p["pane_id"]   # heal the stored slot id
                save_session(rec)
            return p["pane_id"]
    return None


# ---------------------------------------------------------------------------
# Analyzer — the heart. Turn (status, screen) into structured reasoning.
# ---------------------------------------------------------------------------
def _bottom(tail, n=10):
    return [ln.strip() for ln in tail.splitlines() if ln.strip()][-n:]


_MARKER_DOC_RE = re.compile(
    r"print .*token|print exactly|token on its own line|completion signal|"
    r"will only be printed|print this", re.I)


def _marker_on_own_line(marker, tail):
    """Marker as a STANDALONE output line — the real completion signal, NOT the
    echoed prompt and NOT the token Codex writes into a plan's "Completion Signal"
    section as documentation. A documented occurrence is preceded (within ~2 lines)
    by framing like "print this token on its own line:"; skip those and keep looking
    for a genuinely-printed one."""
    if not marker:
        return False
    lines = tail.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lstrip("•›>*- ").strip() == marker:
            ctx = " ".join(lines[max(0, i - 2):i])
            if _MARKER_DOC_RE.search(ctx):
                continue                    # documented in a plan, not printed
            return True
    return False


def _parse_options(tail):
    """Extract numbered menu options from the bottom of the screen.
    Returns [{key,label,recommended}]. recommended = the ›-marked / (Recommended) one."""
    opts = []
    for ln in tail.splitlines():
        m = _MENU_RE.match(ln)
        if not m:
            continue
        key, label = m.group(1), m.group(2).strip()
        rec = ("›" in ln) or ("(recommended)" in label.lower())
        # Trim trailing column text some menus append after 2+ spaces.
        label = re.split(r"\s{2,}", label)[0].strip()
        opts.append({"key": key, "label": label, "recommended": rec})
    # De-dupe by key, keep first.
    seen, out = set(), []
    for o in opts:
        if o["key"] not in seen:
            seen.add(o["key"])
            out.append(o)
    return out


def _extract_questions(tail):
    """Free-text and numbered questions at the bottom (lines ending with '?')."""
    qs = []
    for ln in _bottom(tail, 14):
        if ln.endswith("?"):
            # strip leading bullets/glyphs and any "1." / "›" numbering
            q = re.sub(r"^\s*[•›>*\-]+\s*", "", ln)
            q = re.sub(r"^\s*\d+[.)]\s*", "", q).strip()
            if q:
                qs.append(q)
    return qs


def _extract_plan(tail):
    """Capture the plan block (from a plan header down to the menu/end), full.
    Plans carry continuity, so this is NEVER truncated by callers."""
    lines = tail.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if _PLAN_HEAD_RE.match(ln):
            start = i
            break
    if start is None:
        return None
    out = []
    for ln in lines[start:]:
        if _PLAN_MENU_RE.search(ln) or "enter to submit" in ln.lower():
            break
        out.append(ln.rstrip())
    # Drop trailing separator/box-drawing/blank lines the plan block runs into.
    sep = re.compile(r"^[\s│└├┘┐┌┤┴┬┼─—═╗╝╔╚╰╯╭╮]*$")
    while out and sep.match(out[-1]):
        out.pop()
    text = "\n".join(out).strip()
    return text or None


def _clean_tail(tail, keep=12, marker=None):
    """Strip TUI chrome and keep the agent's real last message (token-efficient).
    Drops the composer placeholder line and the bare completion-marker echo —
    both are signalled elsewhere in the envelope and would only add noise."""
    kept = []
    drop_cont = False   # inside a wrapped ›-echo block: drop its continuation lines
    for ln in tail.splitlines():
        if any(rx.search(ln) for rx in _CHROME_RES):
            continue
        stripped = ln.lstrip()
        # Input-area lines (prompt echo, rotating placeholder, live composer) begin
        # with the › glyph and are never agent output. Drop the line AND its wrapped
        # continuations (the TUI wraps a long prompt and only the first line carries
        # the glyph). Keep numbered menu options; don't touch ASCII '>' blockquotes.
        if stripped.startswith("›") and not _MENU_RE.match(ln):
            drop_cont = True
            continue
        if drop_cont:
            # a continuation is an indented follow-on line that is not itself an
            # agent-output glyph (•, └) or a new ›/menu line; stop at the first such.
            if ln[:1].isspace() and stripped[:1] not in ("•", "└", "›"):
                continue
            drop_cont = False
        # the bare marker token on its own line (marker_found already reports it)
        if marker and ln.strip().lstrip("•›>*- ").strip() == marker:
            continue
        kept.append(ln.rstrip())
    # collapse blank runs
    out, blank = [], False
    for ln in kept:
        if not ln.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    lines = "\n".join(out).strip().splitlines()
    return "\n".join(lines[-keep:])


def analyze(status, tail, marker=None, expect=None, session_id=None, self_cmd="codex.py"):
    """Return the structured `result` block for the envelope."""
    expect = expect or []
    artifacts = [{"path": p, "exists": os.path.exists(p),
                  "bytes": (os.path.getsize(p) if os.path.exists(p) else 0)} for p in expect]
    # `tail` is the full recent transcript. Current state lives at the BOTTOM
    # (the live screen); scoping the detectors there avoids matching a stale
    # menu/marker/question left in scrollback from an earlier turn. The plan,
    # which can be long, is extracted from the FULL transcript and never cut.
    bottom = "\n".join(tail.splitlines()[-48:])
    is_widget = bool(_WIDGET_RE.search(bottom))
    is_plan_menu = bool(_PLAN_MENU_RE.search(bottom))
    options = _parse_options(bottom)
    questions = _extract_questions(bottom)
    # Extract the plan ONLY while the approval menu is up. Once approved, the
    # "Implement this plan?" stop-marker is gone and a plan header lingers in
    # scrollback, so an unguarded extract would swallow the whole implementation
    # log into `plan` (token bloat + would overwrite the clean stored plan).
    plan = _extract_plan(tail) if is_plan_menu else None
    marker_found = _marker_on_own_line(marker, bottom)
    clean = _clean_tail(tail, marker=marker) or ""

    def _na(intent, args, why):
        cmd = None
        if intent != "nothing":
            base = f"python3 {self_cmd} {args}"
            cmd = base.replace("<id>", session_id or "<session>")
        return {"intent": intent, "command": cmd, "why": why}

    state = reason = None
    next_action = _na("nothing", "", "")
    summary = ""

    if status == "blocked":
        if is_widget:
            state, reason = "awaiting_clarification", "multiple_choice"
            summary = "Codex is asking a structured multiple-choice question."
            next_action = _na("choose", f"reply --session <id> --choice <N>",
                              "Pick an option key from result.options.")
        else:
            state, reason = "permission_gate", "permission_request"
            summary = "Codex is waiting at a tool/command permission prompt."
            next_action = _na("approve", f"reply --session <id> --approve",
                              "Review result.transcript_tail, then approve or send --reject.")
    elif status in ("idle", "done"):
        if is_plan_menu:
            state, reason = "awaiting_approval", "plan_approval"
            summary = "Codex proposed a plan and is waiting for approval."
            questions = []   # "Implement this plan?" is the menu prompt, not a clarification
            next_action = _na("approve", f"reply --session <id> --approve",
                              "Approve to implement, or --reject to keep planning. Plan is in result.plan.")
        elif questions:
            # Open questions (lines ending in '?') are answered with text — even
            # when numbered. Numbering alone does NOT make them pick-one options.
            state, reason = "awaiting_clarification", "free_text_question"
            summary = "Codex asked a question and is waiting for your answer."
            next_action = _na("answer", f'reply --session <id> --text "<answer>"',
                              "Answer the question(s) in result.questions (text addresses all of them).")
        elif options:
            # Numbered, NOT ending in '?' -> a real pick-one menu.
            state, reason = "awaiting_clarification", "multiple_choice"
            summary = "Codex presented a numbered choice."
            next_action = _na("choose", f"reply --session <id> --choice <N>",
                              "Pick an option key from result.options.")
        elif marker_found:
            missing = [a["path"] for a in artifacts if not a["exists"]]
            if missing:
                state, reason = "completed", "marker_unverified"
                summary = f"Marker printed but expected artifact(s) missing: {', '.join(missing)}."
                next_action = _na("verify", f"status --session <id>",
                                  "Marker present but a promised file is absent — verify before trusting.")
            else:
                state, reason = "completed", "marker_verified"
                summary = "Task complete: marker printed and expected artifacts present."
        elif expect and all(a["exists"] for a in artifacts):
            state, reason = "completed", "artifacts_present"
            summary = "Expected artifacts exist (no marker printed)."
            next_action = _na("verify", f"status --session <id>",
                              "No completion marker, but expected files exist — confirm content.")
        else:
            state, reason = "no_signal", "no_signal"
            summary = "Turn ended with no completion marker, question, or menu."
            uncertain = bool(_UNCERTAINTY_RE.search("\n".join(_bottom(tail, 8))))
            why = ("Last message reads uncertain — likely an implicit question; read the tail."
                   if uncertain else "Read result.transcript_tail to judge; may be done without a marker.")
            next_action = _na("verify", f"status --session <id>", why)
    else:
        state, reason = "working", "working"
        summary = "Codex is still working."
        next_action = _na("wait", f"await --session <id>", "Re-enter the wait.")

    return {
        "state": state, "reason": reason, "summary": summary[:200],
        "plan": plan,                       # never truncated by callers
        "questions": questions, "options": options,
        "marker_found": marker_found, "artifacts": artifacts,
        "transcript_tail": clean,
        "next_action": next_action,
    }


def settle_and_analyze(pane_id, marker, expect, session_id, timeout, self_cmd,
                       socket_path=SOCKET_PATH):
    """Wait for Codex to settle, then analyze — but treat a bare turn-end as
    possibly transient. Returns (result_dict, timed_out:bool).

    A turn ending at idle/done with no marker/question/menu does NOT mean the
    task is over: Codex emits idle blips between work bursts, and a just-submitted
    action (approve/answer) leaves the pane briefly at the old settled state
    before it flips to working. So on `no_signal` we (a) re-read for render lag,
    then (b) give Codex a grace window to resume; only if it stays put do we
    report no_signal. All bounded by `timeout`.
    """
    start = time.time()
    status = current_status(pane_id, socket_path)
    if status is None:
        return ({"state": "exited", "reason": "pane_gone",
                 "summary": "The Codex pane is gone (process exited or closed).",
                 "plan": None, "questions": [], "options": [], "marker_found": False,
                 "artifacts": [], "transcript_tail": "",
                 "next_action": {"intent": "start",
                                 "command": f'python3 {self_cmd} start --task "<your task>"',
                                 "why": "Session ended (Codex exited or pane closed); start a fresh task."}}, False)
    timed_out = False
    if status not in SETTLED:
        settled = wait_for_settle(pane_id, timeout, socket_path)
        if settled is None:
            timed_out = True
            status = current_status(pane_id, socket_path) or "working"
        else:
            status = settled

    def _remaining():
        return timeout - (time.time() - start)

    while True:
        time.sleep(SETTLE_DELAY)
        result = analyze(status, read_tail(pane_id, socket_path=socket_path),
                         marker, expect, session_id, self_cmd)
        if result["reason"] != "no_signal" or timed_out or _remaining() <= 0:
            break
        # (a) render lag — a menu/marker may still be painting after the event.
        tries = 0
        while result["reason"] == "no_signal" and tries < RECHECK_TRIES and _remaining() > 0:
            time.sleep(RECHECK_DELAY)
            result = analyze(status, read_tail(pane_id, socket_path=socket_path),
                             marker, expect, session_id, self_cmd)
            tries += 1
        if result["reason"] != "no_signal":
            break
        # (b) transient turn-end. Within the grace budget, keep re-reading the
        # SCREEN — a menu/marker/question may still be painting (a plan-approval
        # menu can lag its plan text by several seconds) — AND watch for Codex
        # resuming work. Conclude no_signal only if neither materializes.
        grace_end = time.time() + min(NO_SIGNAL_GRACE, max(0.0, _remaining()))
        resumed = False
        while time.time() < grace_end and _remaining() > 0:
            time.sleep(RECHECK_DELAY)
            result = analyze(status, read_tail(pane_id, socket_path=socket_path),
                             marker, expect, session_id, self_cmd)
            if result["reason"] != "no_signal":
                break                  # an actionable state painted late
            if current_status(pane_id, socket_path) == "working":
                resumed = True
                break
        if result["reason"] != "no_signal":
            break                      # caught the late-painted state
        if not resumed:
            break                      # stayed settled -> genuine no_signal
        nxt = wait_for_settle(pane_id, max(0.0, _remaining()), socket_path)
        if nxt is None:
            timed_out = True
            status = current_status(pane_id, socket_path) or "working"
        else:
            status = nxt               # resumed; re-analyze after the next settle

    if timed_out:
        result["state"], result["reason"] = "working", "timeout"
        result["summary"] = "Codex did not settle within the timeout; still working."
        result["next_action"] = {"intent": "wait",
                                  "command": f"python3 {self_cmd} await --session {session_id}",
                                  "why": "Re-enter the wait with a longer --timeout if needed."}
    return result, timed_out
