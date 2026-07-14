#!/usr/bin/env python3


# ---- config ------------------------------------------------------------
"""bct-chat.py — external participant client for BCT's group chat.

Speaks the line-JSON wire ({"paneID","cmd","args"} -> {"ok","text","error"})
over a unix socket (default ~/.bct-chat.sock — the ssh-RemoteForward'ed BCT
control socket; override with $BCT_CHAT_SOCK). On hosts without AF_UNIX
(Windows CPython), forward a TCP port instead and set
$BCT_CHAT_SOCK=tcp:<host>:<port>. Pure stdlib.
Spec: docs/superpowers/specs/2026-07-12-chat-external-participants-design.md
"""
import json, os, re, socket, subprocess, sys, time

ARTIFACT = os.path.abspath(__file__)   # the concatenated single file; what spawn_heartbeat re-execs

if hasattr(sys.stdout, "reconfigure"):
    # Wire and room text are UTF-8; never trust the locale (Korean Windows = cp949).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# BCT_CHAT_HOME overrides the state dir. This is what makes the test suite safe on
# Windows: ntpath.expanduser() ignores HOME and reads USERPROFILE, so an HOME-isolated
# test would run against the developer's REAL ~/.bct-chat and SIGKILL their live daemon.
STATE_DIR = os.environ.get("BCT_CHAT_HOME") or os.path.expanduser("~/.bct-chat")
IDENTITY = os.path.join(STATE_DIR, "identity.json")
PENDING = os.path.join(STATE_DIR, "pending-join.json")
COOLDOWN = os.path.join(STATE_DIR, "join-cooldown.json")
JOIN_COOLDOWN = 1800            # 30 min — a request the user denied or ignored must not nag
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NO_MENTION = "(새 멘션 없음)"          # chat-listen timeout sentinel (server push)
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
HEARTBEAT_INTERVAL = 240        # 4 min — comfortably inside BCT's 10-min prune window
HEARTBEAT_MAX_UPTIME = 43200    # 12 h — backstop for a marker leaked by a crashed session

STABLE = os.path.join(STATE_DIR, "bct-chat.py")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

REPLY_HINT = ('당신이 멘션되었습니다 — `python3 ~/.bct-chat/bct-chat.py send "<답변>"` 으로 답하세요. '
              '(명단: `python3 ~/.bct-chat/bct-chat.py list`, 새 메시지 확인: '
              '`python3 ~/.bct-chat/bct-chat.py read`)')


# ---- wire --------------------------------------------------------------
"""Wire transport: unix socket (or TCP fallback) connection to the BCT bridge."""
import json, os, re, socket, subprocess, sys, time


def tcp_target(spec):
    """$BCT_CHAT_SOCK=tcp:<host>:<port> -> (host, port); None means a unix path.
    IPv6 goes in brackets: tcp:[::1]:9000."""
    if not spec.startswith("tcp:"):
        return None
    rest = spec[4:]
    if rest.startswith("["):
        host, sep, port = rest[1:].partition("]:")
        if not sep:
            return None
    else:
        host, _, port = rest.rpartition(":")
    if not port.isdigit():
        return None
    port = int(port)
    if not 1 <= port <= 65535:
        return None
    return (host or "127.0.0.1", port)


def default_name():
    return socket.gethostname()


def connect(timeout=10):
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
    except OSError:
        s.close()
        raise
    return s


def sock_available():
    """CONNECT — never os.path.exists(). A stale socket file left by an ssh reconnect
    without StreamLocalBindUnlink still exists on disk; connecting to it is the only
    way to learn that nobody is home (D4)."""
    try:
        connect(timeout=3).close()
        return True
    except OSError:
        return False


def rpc(cmd, args, pane_id="", timeout=10):
    """One request, one line-JSON reply, bounded by an OVERALL deadline — not a
    per-recv timeout, which a bridge dribbling bytes could keep resetting forever.
    The deadline covers connect, the request write, and every read: connect gets
    whatever of the budget remains (capped at 10s), and the socket is re-armed
    with the remaining budget immediately before sendall so a slow accept can't
    leave a stale, oversized timeout in force for the write. Blank lines are
    keepalives and are skipped; if two frames arrive in one read we answer from
    the first; a bridge that accepts and closes without replying is a socket
    error, not a malformed response."""
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    deadline = time.time() + timeout
    try:
        s = connect(min(max(deadline - time.time(), 0), 10))
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    try:
        left = deadline - time.time()
        if left <= 0:
            return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
        s.settimeout(left)
        s.sendall((json.dumps({"paneID": pane_id, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while True:
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue                  # keepalive
                try:
                    return json.loads(line.decode("utf-8"))
                except ValueError:
                    return {"ok": False, "error": "malformed response from bridge"}
            left = deadline - time.time()
            if left <= 0:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            s.settimeout(left)
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            if not chunk:
                return {"ok": False, "error": "socket error: bridge closed without a reply"}
            buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    finally:
        try:
            s.close()
        except OSError:
            pass


# ---- state -------------------------------------------------------------
"""Local state: identity/pending/cooldown JSON files, the stable copy, session markers."""
import json, os, re, socket, subprocess, sys, time


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def atomic_write(path, text):
    """temp + os.replace: os.replace is atomic on POSIX and on Windows. A hook killed
    mid-write can then never leave a 0-byte identity.json or a truncated stable copy
    (which is the very file the skill tells claude to run)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        forget(tmp)
        raise


def save(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False))


def forget(path):
    try:
        os.remove(path)
    except OSError:
        pass


def proc_alive(pid):
    """Is this pid a live process? NEVER os.kill(pid, 0) on Windows — CPython maps
    os.kill to TerminateProcess there for ANY signal, i.e. probing would kill it."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # AttributeError/ImportError/OSError all mean "could not ask" here — never
        # fall through to the POSIX branch below, which would reach os.kill.
        try:
            import ctypes
            from ctypes import wintypes
            SYNCHRONIZE = 0x00100000
            ERROR_ACCESS_DENIED = 5   # NULL handle + this code: alive, owned by someone else
            # use_last_error=True so ctypes.get_last_error() below reflects THIS call's
            # GetLastError(), not a stale/unrelated one — ctypes.windll skips that tracking.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # ctypes defaults restype to a 32-bit c_int; HANDLE is pointer-sized on Win64,
            # so an untyped call truncates/misreads the return value there.
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if h:
                kernel32.CloseHandle(h)
                return True
            # NULL handle: ACCESS_DENIED means the process exists but is owned by a
            # different session/user — alive, not gone. Any other error (e.g.
            # ERROR_INVALID_PARAMETER, 87) means no such process.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        except (AttributeError, ImportError, OSError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return False


def ensure_stable_copy():
    """Plugin installs live under a versioned cache path; keep one canonical copy at
    ~/.bct-chat/bct-chat.py for the skill prose and manual use. Guarded against EVERY
    exception, not just OSError: a truncated (pre-atomic-write) copy raises
    UnicodeDecodeError, which would exit the hook nonzero and trigger hooks.json's
    `|| python` re-run with stdin already drained."""
    me = ARTIFACT
    if me == os.path.abspath(STABLE):
        return
    try:
        with open(me, encoding="utf-8") as f:
            src = f.read()
        try:
            with open(STABLE, encoding="utf-8") as f:
                if f.read() == src:
                    return
        except Exception:
            pass
        atomic_write(STABLE, src)
        os.chmod(STABLE, 0o755)
    except Exception:
        pass                        # best-effort; never block session start


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


# ---- inbox -------------------------------------------------------------
"""Placeholder — populated by Task 4."""


# ---- membership --------------------------------------------------------
"""Join/cooldown/identity: requesting, polling and holding room membership."""
import json, os, re, socket, subprocess, sys, time


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


def authed(cmd, args, timeout=10):
    """RPC with identity; auto re-join on identity invalidation (BCT restart/eviction)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity(), timeout=timeout)
    if not r.get("ok") and r.get("error") == NOT_INVITED:
        obj = load(IDENTITY)
        if obj:
            if not may_request_join():
                return r                      # cooling down — surface NOT_INVITED as-is
            print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
            do_join(obj["name"])              # blocking: a live verb wants an answer
            r = rpc(cmd, args, identity(), timeout=timeout)
    return r


# ---- presence ----------------------------------------------------------
"""Heartbeat daemon: proves this host is alive while any claude session on it is."""
import json, os, re, socket, subprocess, sys, time


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
        subprocess.Popen([sys.executable, ARTIFACT, "heartbeat"], **kwargs)
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
    atomic_write(PIDFILE, str(me))
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
                        # Poll an existing pending request first — never fire a fresh
                        # chat-join while one is outstanding, or the new requestID
                        # orphans any approval already in flight for the old one.
                        if load(PENDING):
                            claim_pending()
                        else:
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


# ---- delivery ----------------------------------------------------------
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


# ---- cli ---------------------------------------------------------------
"""Entry point: argv dispatch for the join/send/read/... verbs and hook shims."""
import json, os, re, socket, subprocess, sys, time


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main(argv):
    if not argv:
        die("usage: bct-chat.py <join|send|read|wait|listen|list|leave|session-start|session-end|stop-hook|prompt-submit> …")
    verb, rest = argv[0], argv[1:]
    if verb == "join":
        clear_cooldown()                      # manual intent overrides the cooldown
        do_join(" ".join(rest) or default_name())
    elif verb == "session-start":
        session_start()
    elif verb == "session-end":
        session_end()
    elif verb == "stop-hook":
        stop_hook()
    elif verb == "prompt-submit":
        prompt_submit_hook()
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
    elif verb == "listen":
        # Server-push standby: chat-listen holds the connection until a mention
        # is posted (or ~30s server-side). One call = one turn; the standby loop
        # re-invokes. 40s socket timeout tolerates the ~30s server hold + slack.
        r = authed("chat-listen", [], timeout=40)
        if not r.get("ok"):
            die(r.get("error", "error"))
        txt = r.get("text", "")
        if txt and txt not in (NO_NEW, NO_MENTION):
            print(txt)
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
