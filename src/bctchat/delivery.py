"""Delivery. LOCAL ONLY — no socket, no RPC, no exception. The hooks read an inbox the
daemon fills; a hook that is killed can therefore lose nothing, because BCT's cursor moved
long before, in the daemon, and only after the message was already on this disk.

That is the whole point of this module. The old stop_hook() ran chat-peek -> chat-read, and
chat-read advances BCT's server-side cursor with no ack verb to replay it: the Stop hook's
timeout was smaller than that path's worst-case RPC budget, so a hook killed after the
cursor moved lost the message outright. Not "rarely" — structurally. No hook opens a socket
now, so "hook timeout < RPC budget" is not a thing that can happen here: it is
unrepresentable, not merely widened."""
import json, os, re, socket, subprocess, sys, time

INBOX_POLL = 1.0


def chat_mode():
    """work (default): the Stop hook returns in milliseconds. standby: it waits on the
    inbox locally for up to STANDBY_HOLD — zero tokens, zero RPC.

    BCT_CHAT_MODE wins whenever it holds a recognized value. BCT_CHAT_STANDBY is the
    legacy variable, and it means what it says: a truthy value (anything other than
    the disable spellings below, or unset) turns standby ON. Treating every legacy
    value as "work" — the bug this replaces — silently defeated any pre-existing
    BCT_CHAT_STANDBY=1 the moment this code shipped."""
    v = os.environ.get("BCT_CHAT_MODE", "").strip().lower()
    if v in ("standby", "work"):
        return v
    legacy = os.environ.get("BCT_CHAT_STANDBY", "").strip().lower()
    if legacy in ("0", "off", "false", "no", ""):
        return "work"
    return "standby"


def hook_payload():
    """claude-code pipes the hook payload as JSON on stdin. Read it ONCE (a second read
    gets nothing), never block on a tty, and treat any malformed shape as an empty payload."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        obj = json.loads(sys.stdin.read() or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def hook_session_id(payload):
    """The session id becomes a filename under sessions/, so it is never trusted as-is:
    basename + a strict charset, and anything else is treated as absent."""
    sid = os.path.basename(str(payload.get("session_id", "")))
    if sid in ("", ".", "..") or not SESSION_ID_RE.match(sid):
        return ""
    return sid


def remark_session(payload):
    """Re-assert this session's marker on every hook that knows its id.

    mark_session()'s only other caller is session_start(), and gc_markers() collects any
    marker whose owning pid reads dead. claude_pid() is a best-effort ancestor walk: if it
    ever resolves to the wrong pid, a LIVE session's marker is collected the moment that
    stranger exits — live_sessions() empties, the daemon exits, and nothing re-creates the
    marker, so that session is deaf for the rest of its life with no way to repair itself.
    Re-marking from the delivery hooks is what makes the marker self-healing, and it
    refreshes the mtime that the pid-0 (Windows) MARKER_TTL fallback rides on."""
    sid = hook_session_id(payload)
    if sid:
        mark_session(sid)


def compose_digest(item, dropped=0):
    """Mirror BCT's local chatInjection shape: identity line, the room lines exactly as BCT
    returned them, then the reply instruction. Capped — a backlog that rotted for hours must
    not dump 4000 lines into a turn.

    The byte cap is applied to the identity+room-lines body ONLY, and REPLY_HINT is
    appended after truncation, never before it: a single mention line longer than
    DIGEST_MAX_BYTES must not slice REPLY_HINT away along with it, which would hand
    claude a blocked turn with a mention and no instruction for how to answer it."""
    name = item.get("name") or default_name()
    lines = [f"[bct-chat] 단체 채팅방 — 당신은 @{name} 입니다. 새 메시지:"]
    if dropped:
        lines.append(f"(오래된 멘션 {dropped}건 생략)")
    body = [l for l in (item.get("text") or "").splitlines() if l]
    if len(body) > DIGEST_MAX_LINES:
        body = body[-DIGEST_MAX_LINES:]
        lines.append(f"(앞부분 생략 — 최근 {DIGEST_MAX_LINES}줄만)")
    lines += body
    text = "\n".join(lines)
    hint_bytes = ("\n" + REPLY_HINT).encode("utf-8")
    budget = max(DIGEST_MAX_BYTES - len(hint_bytes), 0)
    text_bytes = text.encode("utf-8")
    if len(text_bytes) > budget:
        text = text_bytes[:budget].decode("utf-8", "ignore") + "\n(생략)"
    return text + "\n" + REPLY_HINT


def chain_count(active):
    """`stop_hook_active` says the turn is only continuing because WE blocked it. Count the
    automatic re-engagements and stop at CHAIN_CAP: two standby remotes mentioning each
    other must not bill turns forever with no human in the loop (D10).

    A turn the user drove (active False) is not a chain at all — it reads 0 without even
    consulting the file, so any human prompt starts the count over."""
    if not active:
        return 0
    obj = load(CHAIN) or {}
    try:
        return int(obj.get("n", 0))
    except (AttributeError, TypeError, ValueError):
        return 0                     # a corrupt counter must not cost us the delivery


def deliver(item, path, dropped):
    """Print, THEN ack. A crash in between leaves the item in processing/, where the orphan
    sweep returns it to the inbox: at-least-once (a rare duplicate) beats a silent loss."""
    print(json.dumps({"decision": "block", "reason": compose_digest(item, dropped)},
                     ensure_ascii=False))
    inbox_ack(path)


def stop_hook():
    """Always exits 0: hooks.json falls back python3 -> python on ANY nonzero exit, which
    would re-run the whole hook with stdin already drained."""
    try:
        payload = hook_payload()
        if os.environ.get("BCT_PANE_ID"):
            return                       # BCT pane — native push owns delivery
        try:
            remark_session(payload)      # before the spawn: the daemon exits on an empty set
        except (Exception, SystemExit):
            pass                         # a marker failure must not cost this turn's delivery
        ensure_daemon()                  # every hook is a spawn point
        active = bool(payload.get("stop_hook_active"))
        n = chain_count(active)
        if n >= CHAIN_CAP:
            return                       # capped: the item stays in the inbox for the user
        recover_orphans()
        got = inbox_claim()
        if got is None and chat_mode() == "standby":
            got = inbox_wait(STANDBY_HOLD, poll=INBOX_POLL)
        if got is None:
            if not active:
                forget(CHAIN)
            return                       # graceful degradation: the daemon keeps listening
        save(CHAIN, {"n": n + 1})
        deliver(got[1], got[0], take_dropped())
    except (Exception, SystemExit):
        pass


def prompt_submit_hook():
    """The digest rides along as CONTEXT with the user's prompt — this is what reaches a
    session that was never woken (cold idle). Never holds. Always exits 0."""
    try:
        payload = hook_payload()
        if os.environ.get("BCT_PANE_ID"):
            return
        try:
            remark_session(payload)
        except (Exception, SystemExit):
            pass                         # a marker failure must not cost this turn's delivery
        ensure_daemon()
        recover_orphans()
        forget(CHAIN)                    # a user prompt ends any automatic chain — even one
                                          # that finds an empty inbox; the human engaging IS
                                          # the reset, not the mention that happens to follow
        got = inbox_claim()
        if got is None:
            return
        print(compose_digest(got[1], take_dropped()))
        inbox_ack(got[0])
    except (Exception, SystemExit):
        pass


def session_start():
    """LOCAL ONLY: stable copy, marker, daemon. The join itself is the daemon's first tick —
    which is what fixes D3 (a claude started before the tunnel is up used to early-return
    with no marker and no daemon, and nothing ever repaired that session)."""
    try:
        if os.environ.get("BCT_PANE_ID"):
            return                       # BCT pane — statusline auto-invite owns this
        ensure_stable_copy()
        sid = hook_session_id(hook_payload())
        if not sid:
            return                       # an interactive run is not a session — no marker
        mark_session(sid)                # before spawning: the daemon exits on an empty set
        ensure_daemon()
    except (Exception, SystemExit):
        pass


def session_end():
    """Drop this session's marker. The daemon is NOT killed — another claude session on this
    host may still be in the room; it exits on its own once the marker set empties."""
    try:
        if os.environ.get("BCT_PANE_ID"):
            return                       # BCT pane — every hook verb is a no-op there
        sid = hook_session_id(hook_payload())
        if sid:
            unmark_session(sid)
    except (Exception, SystemExit):
        pass
