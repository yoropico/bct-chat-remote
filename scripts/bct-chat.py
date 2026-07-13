#!/usr/bin/env python3
"""bct-chat.py — external participant client for BCT's group chat.

Speaks the line-JSON wire ({"paneID","cmd","args"} -> {"ok","text","error"})
over a unix socket (default ~/.bct-chat.sock — the ssh-RemoteForward'ed BCT
control socket; override with $BCT_CHAT_SOCK). On hosts without AF_UNIX
(Windows CPython), forward a TCP port instead and set
$BCT_CHAT_SOCK=tcp:<host>:<port>. Pure stdlib.
Spec: docs/superpowers/specs/2026-07-12-chat-external-participants-design.md
"""
import json, os, socket, subprocess, sys, time

if hasattr(sys.stdout, "reconfigure"):
    # Wire and room text are UTF-8; never trust the locale (Korean Windows = cp949).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STATE_DIR = os.path.expanduser("~/.bct-chat")
IDENTITY = os.path.join(STATE_DIR, "identity.json")
PENDING = os.path.join(STATE_DIR, "pending-join.json")
COOLDOWN = os.path.join(STATE_DIR, "join-cooldown.json")
JOIN_COOLDOWN = 1800            # 30 min — a request the user denied or ignored must not nag
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
HEARTBEAT_INTERVAL = 240        # 4 min — comfortably inside BCT's 10-min prune window
HEARTBEAT_MAX_UPTIME = 43200    # 12 h — backstop for a marker leaked by a crashed session


def tcp_target(spec):
    """$BCT_CHAT_SOCK=tcp:<host>:<port> -> (host, port); None means unix path."""
    if not spec.startswith("tcp:"):
        return None
    host, _, port = spec[4:].rpartition(":")
    if not port.isdigit():
        return None
    return (host or "127.0.0.1", int(port))


def default_name():
    return socket.gethostname()


def sock_available():
    t = tcp_target(SOCK)
    if t is None:
        return os.path.exists(SOCK)
    try:
        socket.create_connection(t, timeout=3).close()
        return True
    except OSError:
        return False


def connect():
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=10)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(SOCK)
    return s


def rpc(cmd, args, pane_id=""):
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    try:
        s = connect()
        s.sendall((json.dumps({"paneID": pane_id, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        try:
            return json.loads(buf.decode())
        except ValueError:
            return {"ok": False, "error": "malformed response from bridge"}
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def forget(path):
    try:
        os.remove(path)
    except OSError:
        pass


def cooldown_remaining():
    """Seconds until an automatic join request is allowed again (0 = now)."""
    obj = load(COOLDOWN)
    if not obj:
        return 0
    left = JOIN_COOLDOWN - (time.time() - obj.get("lastFailedAt", 0))
    return int(left) if left > 0 else 0


def may_request_join():
    return cooldown_remaining() == 0


def note_join_failure(outcome):
    save(COOLDOWN, {"lastFailedAt": time.time(), "outcome": outcome})


def clear_cooldown():
    forget(COOLDOWN)


def request_join_if_allowed(name):
    """Automatic (non-blocking) join request, gated by the cooldown. The manual
    `join` verb bypasses this — a human at the remote's shell always wins."""
    if not may_request_join():
        print(f"입장 재요청 쿨다운 중 — {cooldown_remaining() // 60}분 후 재시도", file=sys.stderr)
        return False
    do_join(name, wait_approval=False)
    return True


STABLE = os.path.join(STATE_DIR, "bct-chat.py")


def ensure_stable_copy():
    """Plugin installs live under a versioned cache path; keep one canonical
    copy at ~/.bct-chat/bct-chat.py for the skill prose and manual use."""
    me = os.path.abspath(__file__)
    if me == os.path.abspath(STABLE):
        return
    try:
        with open(me, encoding="utf-8") as f:
            src = f.read()
        try:
            with open(STABLE, encoding="utf-8") as f:
                if f.read() == src:
                    return
        except OSError:
            pass
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STABLE, "w", encoding="utf-8") as f:
            f.write(src)
        os.chmod(STABLE, 0o755)
    except OSError:
        pass                        # best-effort; never block session start


def identity():
    obj = load(IDENTITY)
    return obj.get("participantID", "") if obj else ""


def membership_live():
    """Does BCT still know this identity? A BCT restart resets the room, but
    identity.json outlives it — so ask the bridge, never trust the file. The dead
    identity is KEPT (it only ever earns NOT_INVITED, the rejoin needs the name
    beside it, and the heartbeat needs something to send); a new approval
    overwrites it. Any other error (bridge hiccup) counts as live: better silent
    than a spurious join banner."""
    r = rpc("chat-list", [], identity())      # read-only probe; consumes no messages
    return r.get("ok") or r.get("error") != NOT_INVITED


def claim_pending():
    """If a session-start hook left a requestID, try to claim the identity."""
    obj = load(PENDING)
    if not obj:
        return False
    r = rpc("chat-join-poll", [obj["requestID"]])
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        forget(PENDING)
        clear_cooldown()                      # seated — the slate is clean
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        forget(PENDING)
        note_join_failure(r["error"])         # arm the 30-min cooldown
    return False


def mark_session(sid):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    open(os.path.join(SESSIONS_DIR, sid), "w").close()


def unmark_session(sid):
    forget(os.path.join(SESSIONS_DIR, sid))


def live_sessions():
    """One marker per live claude session on this host — the daemon's refcount."""
    try:
        return os.listdir(SESSIONS_DIR)
    except OSError:
        return []


def heartbeat_alive():
    """Is a daemon running? Its pid file's mtime is refreshed every tick, so a stale
    file (crashed daemon) ages out. NEVER probe the pid with os.kill(pid, 0) — on
    Windows that TERMINATES the process."""
    try:
        return time.time() - os.stat(PIDFILE).st_mtime < 2 * HEARTBEAT_INTERVAL
    except OSError:
        return False


def spawn_heartbeat():
    if heartbeat_alive():
        return
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "heartbeat"], **kwargs)
    except OSError:
        pass                        # best-effort; never block session start


def pidfile_owner():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def do_heartbeat(interval, max_uptime):
    """Prove this host is alive while any claude session on it is. BCT prunes an
    external after 10 min of silence and (before the retire/reseat change) that
    destroyed its unread cursor — so a live-but-quiet host must keep ticking.

    The pid file is this daemon's only coordination primitive, so two rules are
    load-bearing: (1) release it in `finally`, and only when we still own it — a
    daemon that yields to a newer instance must never touch, let alone delete,
    the winner's file; (2) a tick that dies (die()/SystemExit from a chained
    do_join, or any other exception — e.g. save() hitting a full disk) must not
    take the daemon down with it. Its whole job is to keep ticking, so a bad tick
    is just a failed tick and the existing two-strike rule applies."""
    if heartbeat_alive() and pidfile_owner() != os.getpid():
        return                      # another daemon has it
    me = os.getpid()
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(me))
    started = time.time()
    fails = 0
    try:
        while True:
            if not live_sessions():
                break               # every claude on this host is gone — get out of the way
            if time.time() - started > max_uptime:
                break               # leaked marker backstop
            if pidfile_owner() not in (me, 0):
                break               # a newer daemon took over — its file, not ours to touch
            try:
                os.utime(PIDFILE, None)   # liveness for heartbeat_alive()
            except OSError:
                pass                # vanished underneath us; the owner check above will catch it
            try:
                if not sock_available():
                    fails += 1
                else:
                    r = rpc("chat-list", [], identity())    # read-only: its only job is touch()
                    if not r.get("ok") and r.get("error") == NOT_INVITED:
                        obj = load(IDENTITY)
                        request_join_if_allowed(obj["name"] if obj else default_name())
                        fails = 0
                    elif not r.get("ok") and str(r.get("error", "")).startswith("socket"):
                        fails += 1
                    else:
                        fails = 0
                        claim_pending()  # an approval may have landed since the last tick
            except (Exception, SystemExit):
                # e.g. request_join_if_allowed -> do_join -> die() on a chat-join error.
                # A failed tick, nothing more — never let it kill the daemon outright.
                fails += 1
            if fails >= 2:
                break               # tunnel is down; the next session start respawns us
            time.sleep(interval)
    finally:
        if pidfile_owner() == me:
            forget(PIDFILE)         # only ever release a pid file we still own


def do_join(name, wait_approval=True):
    r = rpc("chat-join", [name])
    if not r.get("ok"):
        die(r.get("error", "join failed"))
    req_id = r["text"]
    save(PENDING, {"requestID": req_id, "name": name})
    if not wait_approval:
        print(f"join requested ({req_id}) — approve in the BCT chat dock", file=sys.stderr)
        return
    print("입장 요청됨 — BCT 채팅 도크에서 승인해 주세요 (5분 내)", file=sys.stderr)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        if claim_pending():
            print("입장 승인됨", file=sys.stderr)
            return
        if not os.path.exists(PENDING):
            die("denied or expired")
    die("승인 대기 시간 초과")


def hook_session_id():
    """claude-code pipes the hook payload as JSON on stdin. An interactive run has a
    tty there — never block on it."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return str(json.loads(sys.stdin.read() or "{}").get("session_id", ""))
    except (OSError, ValueError):
        return ""


def session_start():
    """SessionStart hook: silent no-ops by design, but never silently absent — if the
    room no longer knows us, raise a fresh join request (cooldown permitting), and keep
    a heartbeat running for as long as this host has a live claude session.

    Invariant: this verb must always exit 0. hooks.json falls back from python3 to
    python on ANY nonzero exit (Windows lacks a reliable "is python3 the MS Store
    stub" test), so a die() escaping here would re-run the whole hook with stdin
    already drained — no session id, no marker, no daemon, and a duplicate chat-join
    banner. So membership/join failures are swallowed here; only the interpreter
    failing to start may trigger the shell fallback. The user-facing verbs
    (send/read/wait/list/join/leave) keep die()'s normal nonzero-exit behaviour."""
    if os.environ.get("BCT_PANE_ID"):
        return                      # BCT pane — statusline auto-invite owns this
    ensure_stable_copy()
    sid = hook_session_id()
    if not sock_available():
        return                      # no ssh session forwarding the socket
    if sid:
        mark_session(sid)           # before spawning: the daemon exits on an empty set
    try:
        if load(PENDING):
            claim_pending()
        elif not (identity() and membership_live()):
            obj = load(IDENTITY)
            request_join_if_allowed(obj["name"] if obj else default_name())
    except SystemExit:
        pass                        # join failed (die()) — still spawn the heartbeat below
    if sid:
        spawn_heartbeat()


def session_end():
    """SessionEnd hook: drop this session's marker. The daemon is NOT killed — another
    claude session on this host may still be in the room; it exits on its own once the
    marker set empties."""
    sid = hook_session_id()
    if sid:
        unmark_session(sid)


def authed(cmd, args):
    """RPC with identity; auto re-join on identity invalidation (BCT restart/eviction)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity())
    if not r.get("ok") and r.get("error") == NOT_INVITED:
        obj = load(IDENTITY)
        if obj:
            if not may_request_join():
                return r                      # cooling down — surface NOT_INVITED as-is
            print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
            do_join(obj["name"])              # blocking: a live verb wants an answer
            r = rpc(cmd, args, identity())
    return r


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main(argv):
    if not argv:
        die("usage: bct-chat.py <join|send|read|wait|list|leave|session-start|session-end> …")
    verb, rest = argv[0], argv[1:]
    if verb == "join":
        clear_cooldown()                      # manual intent overrides the cooldown
        do_join(" ".join(rest) or default_name())
    elif verb == "session-start":
        session_start()
    elif verb == "session-end":
        session_end()
    elif verb == "send":
        msg = " ".join(rest)
        if not msg:
            die('send needs "<message>"')
        r = authed("chat-send", [msg])
        if not r.get("ok"):
            die(r.get("error", "error"))
    elif verb == "read":
        r = authed("chat-read", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "wait":
        timeout = 300
        if "--timeout" in rest:
            i = rest.index("--timeout")
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                die("wait --timeout <seconds>")
            timeout = int(rest[i + 1])
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = authed("chat-read", [])
            if not r.get("ok"):
                die(r.get("error", "error"))
            if r.get("text") and r["text"] != NO_NEW:
                print(r["text"])
                return
            time.sleep(2)
        die(f"timeout: no new message within {timeout}s")
    elif verb == "list":
        r = authed("chat-list", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "leave":
        r = rpc("chat-leave", [], identity())
        for p in (IDENTITY, PENDING):
            forget(p)
        if not r.get("ok") and r.get("error") != NOT_INVITED:
            die(r.get("error", "error"))
    elif verb == "heartbeat":
        interval, max_uptime = HEARTBEAT_INTERVAL, HEARTBEAT_MAX_UPTIME
        if "--interval" in rest:
            i = rest.index("--interval")
            if i + 1 >= len(rest):
                die("heartbeat --interval <seconds>")
            try:
                interval = float(rest[i + 1])
            except ValueError:
                die("heartbeat --interval <seconds>")
        if "--max-uptime" in rest:
            i = rest.index("--max-uptime")
            if i + 1 >= len(rest):
                die("heartbeat --max-uptime <seconds>")
            try:
                max_uptime = float(rest[i + 1])
            except ValueError:
                die("heartbeat --max-uptime <seconds>")
        do_heartbeat(interval, max_uptime)
    else:
        die(f"unknown verb: {verb}")


if __name__ == "__main__":
    main(sys.argv[1:])
