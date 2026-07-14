"""Hook entry points: SessionStart/SessionEnd/Stop/UserPromptSubmit digest delivery."""
import json, os, re, socket, subprocess, sys, time


def hook_session_id():
    """claude-code pipes the hook payload as JSON on stdin. An interactive run has a
    tty there — never block on it. The result is used directly as a filename under
    sessions/, so it is never trusted as-is: any parse/shape failure — a malformed
    payload, a non-object top-level value (a bare list/string/number/null all raise
    AttributeError out of a naive `.get()`) — is treated the same as a missing
    session_id, and the extracted value is then os.path.basename()'d and checked
    against a strict charset before being handed back. A value that fails that check
    (empty, ".", "..", or containing anything outside [A-Za-z0-9._-]) is treated as
    absent ("") rather than raised — the caller never needs its own try/except
    around this, and a traversal-shaped id can never reach a filesystem call."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        obj = json.loads(sys.stdin.read() or "{}")
        sid = str(obj.get("session_id", "")) if isinstance(obj, dict) else ""
    except Exception:
        return ""
    sid = os.path.basename(sid)
    if sid in ("", ".", "..") or not SESSION_ID_RE.match(sid):
        return ""
    return sid


def session_start():
    """SessionStart hook: silent no-ops by design, but never silently absent — if the
    room no longer knows us, raise a fresh join request (cooldown permitting), and keep
    a heartbeat running for as long as this host has a live claude session.

    Invariant: this verb must always exit 0. hooks.json falls back from python3 to
    python on ANY nonzero exit (Windows lacks a reliable "is python3 the MS Store
    stub" test), so a die() escaping here would re-run the whole hook with stdin
    already drained — no session id, no marker, no daemon, and a duplicate chat-join
    banner. So nothing past ensure_stable_copy() may escape as an exception or a
    SystemExit — the same (Exception, SystemExit) idiom do_heartbeat() already uses
    for its own tick loop, applied here to the whole rest of the hook (mark_session()
    included: a malformed session id could in principle still slip past
    hook_session_id()'s own sanitizing, and this is the backstop for that). A join
    failure specifically must still let spawn_heartbeat() run afterwards — a marker
    with no daemon is a worse regression than either symptom alone — so that inner
    step keeps its own narrower try immediately around the join call. The user-facing
    verbs (send/read/wait/list/join/leave) keep die()'s normal nonzero-exit
    behaviour."""
    if os.environ.get("BCT_PANE_ID"):
        return                      # BCT pane — statusline auto-invite owns this
    ensure_stable_copy()
    try:
        sid = hook_session_id()
        if not sock_available():
            return                  # no ssh session forwarding the socket
        if sid:
            mark_session(sid)       # before spawning: the daemon exits on an empty set
        # A genuine session (re)start is fresh intent: if the standing cooldown was armed
        # by a mere EXPIRY (an ignored request, or one lost to a BCT restart during churn),
        # drop it so the restart re-requests instead of silently sitting out its 30 min. An
        # explicit DENIAL is respected — never cleared here, so a restart cannot re-nag.
        _cd = load(COOLDOWN)
        if _cd and _cd.get("outcome") == "expired":
            clear_cooldown()
        try:
            if load(PENDING):
                claim_pending()
            elif not (identity() and membership_live()):
                obj = load(IDENTITY)
                request_join_if_allowed(obj["name"] if obj else default_name())
        except (Exception, SystemExit):
            pass                    # join failed — still spawn the heartbeat below
        if sid:
            spawn_heartbeat()
    except (Exception, SystemExit):
        pass                        # never let a hook verb trigger the python3->python fallback


def session_end():
    """SessionEnd hook: drop this session's marker. The daemon is NOT killed — another
    claude session on this host may still be in the room; it exits on its own once the
    marker set empties.

    Invariant: this verb must always exit 0 too — see session_start()'s docstring."""
    try:
        sid = hook_session_id()
        if sid:
            unmark_session(sid)
    except (Exception, SystemExit):
        pass


def compose_digest(name, read_text):
    """Mirror BCT's local chatInjection shape: identity line, the unseen lines
    exactly as chat-read returned them, then the reply instruction."""
    lines = [f"[bct-chat] 단체 채팅방 — 당신은 @{name} 입니다. 새 메시지:"]
    lines += [l for l in read_text.splitlines() if l]
    lines.append(REPLY_HINT)
    return "\n".join(lines)


def drain_stdin():
    """Hook payloads arrive on stdin; read them off so the writer never blocks,
    but never block on a tty ourselves."""
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass


def pending_digest():
    """The digest to deliver, or None. chat-peek decides (cursor-preserving —
    a non-mention backlog stays unseen for a future mention delivery, same
    semantics as BCT's local push); only a mentioned backlog is consumed via
    chat-read. Every failure path — no socket, no identity, an old BCT without
    chat-peek, a read error — is None: the hooks must never disturb a turn."""
    if os.environ.get("BCT_PANE_ID"):
        return None                  # BCT-pane claude — native push owns delivery
    if not sock_available() or not identity():
        return None
    r = rpc("chat-peek", [], identity())
    parts = (r.get("text") or "").split() if r.get("ok") else []
    if len(parts) != 2 or parts[1] != "1":
        return None
    rd = rpc("chat-read", [], identity())
    text = rd.get("text") or ""
    if not rd.get("ok") or not text or text == NO_NEW:
        return None
    obj = load(IDENTITY) or {}
    return compose_digest(obj.get("name", default_name()), text)


def standby_enabled():
    """① idle standby window is ON unless BCT_CHAT_STANDBY is a disable value."""
    v = os.environ.get("BCT_CHAT_STANDBY", "").strip().lower()
    return v not in ("0", "off", "false", "no")


def standby_listen_digest():
    """Hold ONE server-push chat-listen window (~30s). Return the digest (wrapped
    like pending_digest) if a mention was pushed, else None. Every failure path is
    None — a hook must never disturb a turn."""
    if os.environ.get("BCT_PANE_ID"):
        return None
    if not sock_available() or not identity():
        return None
    r = rpc("chat-listen", [], identity(), timeout=40)
    if not r.get("ok"):
        return None
    text = r.get("text") or ""
    if not text or text in (NO_NEW, NO_MENTION):
        return None
    obj = load(IDENTITY) or {}
    return compose_digest(obj.get("name", default_name()), text)


def stop_hook():
    """Stop hook: block the turn end with the digest when mentioned — claude
    answers the room in place. When nothing is pending and standby is enabled, hold one
    server-push window (~30s, ① idle standby) so an otherwise-idle joined claude still
    receives. The window never blocks empty (sentinel → exit), so there is no turn
    churn. Always exits 0 (see session_start docstring)."""
    drain_stdin()
    try:
        d = pending_digest()
        if d is None and standby_enabled():
            d = standby_listen_digest()
        if d:
            print(json.dumps({"decision": "block", "reason": d}, ensure_ascii=False))
    except (Exception, SystemExit):
        pass


def prompt_submit_hook():
    """UserPromptSubmit hook: same detection, but the digest rides along as
    CONTEXT (plain stdout) with the user's prompt — covers a fully idle claude
    the moment the user next engages. Always exits 0."""
    drain_stdin()
    try:
        d = pending_digest()
        if d:
            print(d)
    except (Exception, SystemExit):
        pass
