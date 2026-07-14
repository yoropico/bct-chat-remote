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

ARTIFACT = os.path.abspath(__file__)   # the concatenated single file; what ensure_daemon re-execs

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
JOIN_STATE = os.path.join(STATE_DIR, "join-state.json")
JOIN_BACKOFF = (60, 300, 1800)  # seconds: 1 min, 5 min, 30 min — then suspended for good
JOIN_MAX_ATTEMPTS = 3           # denied/expired outcomes before the budget suspends itself
PENDING_TTL = 600               # 10 min — an unrecognized poll reply (BCT forgot the request
                                 # id) must still retire pending-join.json, not wedge it forever
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NO_MENTION = "(새 멘션 없음)"          # chat-listen timeout sentinel (server push)
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
MARKER_TTL = 7 * 86400          # a marker with no pid to probe (Windows) ages out this slowly:
                                 # GC'ing a LIVE session's marker costs it its ear, while a
                                 # leaked one only costs a phantom seat — so err long
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
PIDFILE_STALE = 90              # pidfile mtime older than this = no daemon (with proc_alive)
PRESENCE_INTERVAL = 240         # 4 min — comfortably inside BCT's 10-min prune window
LISTEN_TIMEOUT = 40             # BCT holds chat-listen ~30s; 40 covers the hold plus slack
BACKOFF_MIN = 60                # a dead tunnel is waited out, never died of
BACKOFF_MAX = 300
JOIN_POLL = 15                  # while unseated: how long between join/poll attempts

STABLE = os.path.join(STATE_DIR, "bct-chat.py")

INBOX_DIR = os.path.join(STATE_DIR, "inbox")
PROCESSING_DIR = os.path.join(STATE_DIR, "processing")
DROPPED = os.path.join(STATE_DIR, "dropped.json")
INBOX_CAP = 50              # a deeper queue means nobody has been home for a long time
ORPHAN_AGE = 120            # a processing/ item older than this belonged to a dead hook

CHAIN = os.path.join(STATE_DIR, "chain.json")
CHAIN_CAP = 3               # automatic re-engagements (stop_hook_active) before we stop
                             # delivering: two standby remotes mentioning each other would
                             # otherwise bill turns forever with no human in the loop
STANDBY_HOLD = 900          # standby's LOCAL inbox wait — a directory poll, not a socket.
                             # Bounded by hooks.json's Stop timeout (960s), which claude-code
                             # honours as-is: the hook config schema caps nothing
                             # (timeout: z.number().positive()) and the command spawn takes
                             # `timeout * 1000` ms straight, with no clamp
DIGEST_MAX_LINES = 200      # a backlog that rotted for hours must not dump 4000 lines
DIGEST_MAX_BYTES = 16384     # into a turn — cap the lines AND the bytes

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CLAUDE_COMM_RE = re.compile(r"claude|node", re.I)   # a hook ancestor plausible enough to
                                                     # trust as this session's claude — the
                                                     # CLI is a node program, and a marker
                                                     # GC'd on a wrong pid costs an ear

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


def ps_field(fmt, pid):
    """One `ps -o <fmt> -p <pid>`, as a stripped string. "" for every failure — no ps on
    this host, a timeout, or a pid that is already gone."""
    try:
        out = subprocess.run(["ps", "-o", fmt, "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def claude_pid():
    """Best-effort pid of the claude process this hook belongs to. The hook's own parent
    is the `sh -c` wrapper hooks.json needs for its `||` fallback, and that shell exits the
    moment we do — so we look one further up. POSIX only: on Windows there is no cheap
    ancestor walk, and 0 means 'no pid liveness for this marker' (gc_markers falls back to
    MARKER_TTL there).

    A wrong answer here is NOT symmetric, so this errs toward 0. Hand back a pid that is
    not this session's claude and gc_markers() collects a LIVE session's marker the moment
    that stranger exits: live_sessions() empties, the daemon exits, and nothing re-creates
    the marker — that session is deaf for the rest of its life. Hand back 0 and the marker
    merely ages out on MARKER_TTL (a phantom seat, nothing more).

    "Still exists at the moment we ask" was never enough of a check on its own — a
    short-lived wrapper is alive at that moment too. So the ancestor must ALSO look like
    claude (claude/node) before we trust it; anything else is 0."""
    if os.name == "nt":
        return 0
    try:
        pid = int(ps_field("ppid=", os.getppid()))
    except ValueError:
        return 0                        # no ps, or no such process
    if pid <= 1 or not proc_alive(pid):
        return 0
    lines = ps_field("comm=", pid).splitlines()      # a full path on macOS, argv[0] on Linux
    comm = os.path.basename(lines[0].strip()) if lines else ""
    return pid if CLAUDE_COMM_RE.search(comm) else 0


def mark_session(sid):
    """One marker per live claude session on this host — the daemon's refcount. The pid
    lets a crashed session's marker be collected; the mtime (refreshed by every hook of
    that session) is the fallback where no pid is available."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    save(os.path.join(SESSIONS_DIR, sid), {"pid": claude_pid(), "startedAt": time.time()})


def unmark_session(sid):
    forget(os.path.join(SESSIONS_DIR, sid))


def live_sessions():
    try:
        return sorted(os.listdir(SESSIONS_DIR))
    except OSError:
        return []


# ---- inbox -------------------------------------------------------------
"""The local mention inbox: the durability boundary between the daemon's ear and the
hooks' mouth. The daemon does not issue its next chat-listen until the item is here,
so a message whose server-side cursor has advanced is always already on local disk."""
import json, os, re, socket, subprocess, sys, time


def _items(d):
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []


def _evict(path):
    """Claim a cap-eviction candidate by os.rename, not os.remove. Eviction must count
    only the files it actually removed — that requires eviction and inbox_claim() to
    compete for the same file through the SAME primitive, so that losing is
    observable. os.rename raises on the loser; a bare os.remove's failure is exactly
    what forget() swallows via `except OSError: pass`, which is what let a naive
    version count an item as dropped even when inbox_claim() had already delivered
    it. So eviction uses the exact same rename arbitration inbox_claim does, never a
    bare remove."""
    trash = f"{path}.{os.getpid()}.evict"
    try:
        os.rename(path, trash)
    except OSError:
        return False              # inbox_claim already won this file — not a drop
    forget(trash)
    return True


def take_dropped():
    """Read-and-clear the count of mentions the cap threw away, for the next digest.
    Claimed by os.rename, same as an inbox item: two hooks racing here are mutually
    exclusive, not a split — the winner takes the whole count and the loser gets 0
    (never a double-report), and a concurrent inbox_put() bumping the counter must
    never have its update lost underneath a bare load+delete."""
    claim = f"{DROPPED}.{os.getpid()}.claim"
    try:
        os.rename(DROPPED, claim)
    except OSError:
        return 0
    obj = load(claim) or {}
    forget(claim)
    return int(obj.get("n", 0))


def _bump_dropped(n):
    """The daemon is the only writer (one daemon per host, ticking sequentially), but
    take_dropped() readers race it. A plain load-then-save here would let a reader's
    steal land between the read and the write and silently swallow this bump — so
    the read side is a steal too (os.rename, same arbitration as take_dropped): if a
    reader wins it, this call starts a fresh counter instead of resurrecting a value
    that has already been handed out. The new total is saved BEFORE the sidecar is
    forgotten, not after: a crash in between then leaves a harmless orphan sidecar
    (swept by recover_orphans()) rather than losing the accumulated count."""
    claim = f"{DROPPED}.{os.getpid()}.bump"
    stolen = False
    try:
        os.rename(DROPPED, claim)
        stolen = True
        obj = load(claim) or {}
    except OSError:
        obj = {}
    save(DROPPED, {"n": int(obj.get("n", 0)) + n})
    if stolen:
        forget(claim)


def inbox_put(text, name):
    """One mention -> one file. Atomic: a reader can never see a half-written item."""
    os.makedirs(INBOX_DIR, exist_ok=True)
    names = _items(INBOX_DIR)
    excess = len(names) - (INBOX_CAP - 1)
    if excess > 0:
        dropped = sum(1 for n in names[:excess]
                      if _evict(os.path.join(INBOX_DIR, n)))
        if dropped:
            _bump_dropped(dropped)
    path = os.path.join(INBOX_DIR, f"{time.time_ns()}-{os.getpid()}.json")
    atomic_write(path, json.dumps({"text": text, "capturedAt": time.time(), "name": name},
                                  ensure_ascii=False))
    return path


def inbox_claim():
    """Take the oldest item, atomically. os.rename is the whole concurrency design:
    two hooks racing, the loser's rename raises and it moves on to the next item —
    exactly one session ever delivers a given mention, with no lock to leak."""
    os.makedirs(PROCESSING_DIR, exist_ok=True)
    for n in _items(INBOX_DIR):
        src = os.path.join(INBOX_DIR, n)
        dst = os.path.join(PROCESSING_DIR, f"{os.getpid()}-{n}")
        try:
            os.rename(src, dst)
        except OSError:
            continue                    # another hook won it
        item = load(dst)
        if not isinstance(item, dict) or "text" not in item:
            # Corrupt: drop it, never hand it to claude — but do NOT _bump_dropped()
            # it. inbox_claim() runs from the hooks, the one place N processes really
            # do run concurrently, and _bump_dropped()'s load-modify-save is only
            # safe because the daemon is its single writer; counting from here would
            # reopen the lost-update race that invariant exists to prevent. atomic_write
            # means an item is never half-written, so a corrupt item needs disk damage
            # or outside tampering — not worth trading that invariant for.
            forget(dst)
            continue
        return (dst, item)
    return None


def inbox_ack(path):
    forget(path)


_SIDECAR_RE = re.compile(r"\.(\d+)\.(?:evict|claim|bump|tmp)$")


def _sweep_sidecars(d, now):
    """.evict/.claim/.bump/.tmp sidecars are left behind by a process that dies mid
    rename-steal or mid atomic_write; on a long-lived host they'd otherwise
    accumulate forever, so anything abandoned by a dead owner is swept.

    Every sidecar name carries the pid of the process that created it —
    `<path>.<pid>.<kind>`, the shape _evict()/take_dropped()/_bump_dropped()'s
    .evict/.claim/.bump and atomic_write's .tmp all share. Correctness rests on
    that pid, not on mtime: a .claim/.bump sidecar is born via
    os.rename(DROPPED, claim), not a write, so it inherits dropped.json's OLD mtime
    rather than getting a fresh one — it can read as already-older-than-ORPHAN_AGE
    the instant it's created, if dropped.json itself sat unwritten for a while
    before the steal. A mtime-only test could then delete a sidecar a live steal is
    still holding, and there is no way to close that window with timing (e.g.
    refreshing the mtime right after the rename) because the rename and the refresh
    can never be made atomic with each other — a sweep can always land in the gap
    between them. So this function never removes a sidecar whose owner
    (proc_alive()) is still alive, regardless of its mtime; that is what actually
    closes the race, not a staleness threshold.

    Once the owner is confirmed dead, ORPHAN_AGE is still checked as a second,
    independent condition, so a pid number that has since been recycled by an
    unrelated new process can't make an otherwise-fresh sidecar look sweepable the
    moment its original owner is gone.

    .evict sidecars need no further guard beyond the pid check: _evict() never
    reads one back, so a sweep that beats its forget() just makes that forget() a
    no-op — the True return still correctly means "we really removed it". .tmp
    files are born from a real write, so their mtime is genuine from the start; the
    pid guard still keeps a sweep from landing mid-write."""
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        m = _SIDECAR_RE.search(name)
        if not m:
            continue
        if proc_alive(int(m.group(1))):
            continue                   # owner is still working it — never sweep
        p = os.path.join(d, name)
        try:
            if now - os.stat(p).st_mtime >= ORPHAN_AGE:
                forget(p)
        except OSError:
            pass


def recover_orphans():
    """A hook that died between claim and print left its item in processing/. Return it
    to the inbox: at-least-once delivery (a rare duplicate) beats a silent loss, and it
    is what makes hooks.json's `|| python` re-run harmless. Also sweeps stale sidecar
    files (see _sweep_sidecars) — this is the one function that already runs
    periodically, so it doubles as the janitor."""
    os.makedirs(INBOX_DIR, exist_ok=True)   # else every rename below fails ENOENT
    n = 0
    now = time.time()
    for name in _items(PROCESSING_DIR):
        p = os.path.join(PROCESSING_DIR, name)
        try:
            if now - os.stat(p).st_mtime < ORPHAN_AGE:
                continue
            os.rename(p, os.path.join(INBOX_DIR, name.split("-", 1)[1]))
            n += 1
        except OSError:
            pass
    _sweep_sidecars(INBOX_DIR, now)
    _sweep_sidecars(PROCESSING_DIR, now)
    _sweep_sidecars(os.path.dirname(DROPPED), now)
    return n


def inbox_wait(seconds, poll=1.0):
    """Standby's hold: a local poll on a directory. Zero RPC, zero tokens."""
    deadline = time.time() + seconds
    while True:
        got = inbox_claim()
        if got:
            return got
        if time.time() >= deadline:
            return None
        time.sleep(min(poll, max(0.0, deadline - time.time())))


# ---- membership --------------------------------------------------------
"""Membership: identity, the outstanding request, and the budget that decides whether
we may ask again. ONE automatic-join entry point (ensure_membership) — the old code had
three callers racing each other, and a second chat-join while one is outstanding orphans
the approval the user is in the middle of granting."""
import json, os, re, socket, subprocess, sys, time


def load_identity():
    return load(IDENTITY)


def identity():
    obj = load_identity()
    return obj.get("participantID", "") if obj else ""


def join_state():
    zero = {"attempts": 0, "nextAttemptAt": 0, "suspended": False, "lastOutcome": ""}
    obj = load(JOIN_STATE)
    if not isinstance(obj, dict):
        return zero
    try:
        return {"attempts": int(obj.get("attempts", 0)),
                "nextAttemptAt": float(obj.get("nextAttemptAt", 0)),
                "suspended": bool(obj.get("suspended", False)),
                "lastOutcome": str(obj.get("lastOutcome", ""))}
    except (TypeError, ValueError):
        # Called from the daemon's tick via may_request_join(); an exception here
        # costs a failed tick, so a malformed field (e.g. {"attempts": "x"}) must
        # read as no-budget-recorded-yet, not blow up the caller.
        return zero


def clear_join_state():
    forget(JOIN_STATE)


def suspended():
    return join_state()["suspended"]


def may_request_join():
    st = join_state()
    return not st["suspended"] and time.time() >= st["nextAttemptAt"]


def note_join_outcome(outcome):
    """A refusal is information, not a reason to keep asking. Back off, then stop:
    three denied/expired outcomes and we never ask again on our own — only a human
    running `bct-chat.py join` at the remote's shell resumes it."""
    st = join_state()
    st["attempts"] += 1
    st["lastOutcome"] = outcome
    idx = min(st["attempts"], len(JOIN_BACKOFF)) - 1
    st["nextAttemptAt"] = time.time() + JOIN_BACKOFF[idx]
    st["suspended"] = st["attempts"] >= JOIN_MAX_ATTEMPTS
    save(JOIN_STATE, st)


def pending():
    """The outstanding request, or None. A TTL — not the poll's reply — is what
    ultimately retires it: an unrecognized error (BCT restarted and forgot the id)
    used to wedge the file forever, and every auto-join caller prefers PENDING over
    requesting, so the rejoin branch became unreachable (D6).

    A pending-join.json written before this budget existed has no requestedAt.
    Reading that absence as `0` would make the request read ~55 years old and
    get discarded on sight — on the upgrade tick that throws away a legitimately
    outstanding request and fires a fresh chat-join, orphaning an approval the
    user may be looking at right now (the exact defect this budget exists to
    kill). Backfill instead: treat a missing requestedAt as "just requested"."""
    obj = load(PENDING)
    if not isinstance(obj, dict) or "requestID" not in obj:
        return None
    if "requestedAt" not in obj:
        obj["requestedAt"] = time.time()
        save(PENDING, obj)
        return obj
    if time.time() - float(obj.get("requestedAt", 0)) > PENDING_TTL:
        forget(PENDING)
        return None
    return obj


def claim_pending():
    obj = pending()
    if not obj:
        return False
    r = rpc("chat-join-poll", [obj["requestID"]])
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        forget(PENDING)
        clear_join_state()                    # seated — the slate is clean
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        forget(PENDING)
        note_join_outcome(r["error"])
    return False


def ensure_membership(wait_approval=False, force=False):
    """The ONLY automatic path into the room. Returns True if we are seated or a request
    is now outstanding.

    `force=True` skips the identity()-truthy fast path below. It exists for a caller
    that already has fresh wire evidence the stored identity is dead — not merely
    absent — and must not let a truthy-but-stale identity.json short-circuit a rejoin
    (the presence daemon's NOT_INVITED tick is the only such caller)."""
    if identity() and not wait_approval and not force:
        return True
    if pending():
        return claim_pending() or True        # a request is in flight: poll it, never re-ask
    if not may_request_join():
        return False
    obj = load_identity()
    do_join(obj["name"] if obj else default_name(), wait_approval=wait_approval)
    return True


def do_join(name, wait_approval=True):
    r = rpc("chat-join", [name])
    if not r.get("ok"):
        die(r.get("error", "join failed"))
    save(PENDING, {"requestID": r["text"], "name": name, "requestedAt": time.time()})
    if not wait_approval:
        print(f"join requested ({r['text']}) — approve in the BCT chat dock", file=sys.stderr)
        return
    print("입장 요청됨 — BCT 채팅 도크에서 승인해 주세요 (5분 내)", file=sys.stderr)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        claim_pending()
        if identity():
            # Success is "we have an identity", NOT "PENDING vanished": the daemon polls
            # too and may legitimately have claimed the approval out from under us.
            print("입장 승인됨", file=sys.stderr)
            return
        if not load(PENDING):
            die("denied or expired")
    die("승인 대기 시간 초과")


def authed(cmd, args, timeout=10):
    """RPC with identity; one bounded re-join on identity invalidation (BCT restart or
    an eviction). Goes through ensure_membership, so it can never fire a chat-join while
    a request is already outstanding (D5)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity(), timeout=timeout)
    if not r.get("ok") and r.get("error") == NOT_INVITED and load_identity():
        if not may_request_join():
            return r                          # suspended or backing off — surface it as-is
        print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
        ensure_membership(wait_approval=True)
        if not identity():
            return r
        r = rpc(cmd, args, identity(), timeout=timeout)
    return r


def do_leave():
    """Leaving must STAY left. The old leave dropped the identity and walked away, so the
    daemon re-requested membership four minutes later. Suspending the budget is what makes
    it stick: suspended-and-unseated is both ensure_daemon()'s refusal to spawn and the
    running daemon's exit condition, and even a daemon that outlives them finds
    may_request_join() False and never re-requests — while the session markers survive,
    because they describe which claude sessions are alive, and that is still true after
    leaving the room."""
    r = rpc("chat-leave", [], identity())
    forget(IDENTITY)
    forget(PENDING)
    st = join_state()
    st["suspended"] = True
    st["lastOutcome"] = "left"
    save(JOIN_STATE, st)
    if not r.get("ok") and r.get("error") != NOT_INVITED:
        die(r.get("error", "error"))


# ---- presence ----------------------------------------------------------
"""Capture. One daemon per host holds chat-listen continuously and lands every mention in
the inbox BEFORE issuing the next listen. It is the only thing in the system that talks to
the room on its own, which is why its exit conditions are exactly three — no live session,
a newer daemon, or a room the user has left — and why a dead tunnel is something it WAITS
for rather than dies of."""
import json, os, re, socket, subprocess, sys, time


def pidfile_owner():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def heartbeat_alive():
    """mtime AND a real liveness probe. mtime alone called a signal-killed daemon alive for
    8 minutes (D2) — its pidfile keeps a fresh mtime, and nothing respawns a corpse that
    still looks warm."""
    try:
        if time.time() - os.stat(PIDFILE).st_mtime >= PIDFILE_STALE:
            return False
    except OSError:
        return False
    return proc_alive(pidfile_owner())


def ensure_daemon():
    """Every hook is a spawn point (D1: SessionStart used to be the only one). Cheap: the
    hooks do no RPC now, so they can afford this check on every turn."""
    if heartbeat_alive():
        return
    if suspended() and not identity():
        return                          # the user left / denied us out — do not resurrect
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, ARTIFACT, "daemon"], **kwargs)
    except OSError:
        pass                            # best-effort; never block a hook


def gc_markers():
    """A crashed claude leaks its marker, and a leaked marker used to keep a phantom host
    in the room for 12 hours (D12). Collect it by pid where we have one, by age where we
    do not — erring long, because evicting a LIVE session's marker would cost it its ear.

    Runs at the top of every loop pass, outside the tick's own try/except, so nothing in
    here may raise: a marker with a non-integer pid (disk damage, or the empty marker a
    pre-upgrade session left) reads as pid 0 — no liveness to probe — and ages out on the
    TTL instead."""
    n = 0
    for sid in live_sessions():
        p = os.path.join(SESSIONS_DIR, sid)
        try:
            pid = int((load(p) or {}).get("pid", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            pid = 0
        try:
            age = time.time() - os.stat(p).st_mtime
        except OSError:
            continue                    # vanished underneath us
        dead = (not proc_alive(pid)) if pid > 0 else (age > MARKER_TTL)
        if dead:
            forget(p)
            n += 1
    return n


def backoff_wait(backoff):
    """Wait out a failed tick, then widen the window. A dead tunnel is something to wait
    for, not to die of (D1: the old two-strike suicide cost a live session its ear over an
    8-minute blip) — but waiting must never become busy-waiting.

    The wait is CHUNKED, never one long sleep. The pidfile's mtime is the only evidence
    this daemon is alive, and PIDFILE_STALE (90 s) is far shorter than BACKOFF_MAX (300 s):
    a daemon that stops refreshing it while it waits out a dead tunnel — the exact scenario
    this backoff exists for — reads as a corpse to heartbeat_alive(), so every hook's
    ensure_daemon() spawns a rival (every hook is a spawn point) while the incumbent sleeps
    on. It is alive the whole time and must keep saying so.

    Re-checking live_sessions() between chunks is the other half: a daemon whose last
    session ended mid-backoff gets out of the way now, not up to BACKOFF_MAX later."""
    chunk = max(1.0, PIDFILE_STALE / 2.0)
    left = backoff
    while left > 0:
        time.sleep(min(chunk, left))
        left -= chunk
        try:
            os.utime(PIDFILE, None)
        except OSError:
            pass                        # vanished underneath us; the owner check catches it
        if not live_sessions():
            break                       # nobody left to wait for
    return min(backoff * 2, BACKOFF_MAX)


def do_daemon(presence_interval=None, listen_timeout=None):
    """The ear. Exit conditions are exactly three; everything else is waited out."""
    presence_interval = PRESENCE_INTERVAL if presence_interval is None else presence_interval
    listen_timeout = LISTEN_TIMEOUT if listen_timeout is None else listen_timeout
    if heartbeat_alive() and pidfile_owner() != os.getpid():
        return                          # another daemon has it
    me = os.getpid()
    atomic_write(PIDFILE, str(me))
    backoff = BACKOFF_MIN
    # Trust identity.json until the WIRE disagrees: a daemon restart with a valid seat must
    # not re-probe (let alone re-request) its way back into a room it is already in.
    seated = bool(identity())
    dead_id = ""                        # the identity a NOT_INVITED reply has disproven
    unlanded = None                     # a mention heard but not yet landed (inbox_put raised)
    last_tick = time.time()
    try:
        while True:
            gc_markers()
            if not live_sessions():
                # Nobody left to hear for. Release the pidfile HERE rather than in the
                # finally, then look again: a session that marks itself between the check
                # and the release calls ensure_daemon(), sees heartbeat_alive() (we are
                # still alive, our pidfile still fresh), declines to spawn — and would be
                # left holding a marker with no ear. Releasing first makes us the loser of
                # that race instead of it, and we simply hand the marker on.
                if pidfile_owner() == me:
                    forget(PIDFILE)
                if live_sessions():
                    ensure_daemon()     # a no-op if one is somehow already alive
                return
            if pidfile_owner() not in (me, 0):
                break                   # a newer daemon took over — its file, not ours to touch
            if suspended() and not identity():
                break                   # the user left the room
            try:
                os.utime(PIDFILE, None)
            except OSError:
                pass                    # vanished underneath us; the owner check catches it
            try:
                if unlanded is not None:
                    # A mention we heard but could not land (ENOSPC, EINTR). BCT's cursor
                    # has ALREADY advanced past it and there is no ack verb to replay it,
                    # so it is retried BEFORE anything else can move the cursor again: a
                    # failed put costs a delay, never the message. A persistent failure
                    # raises straight back into the backoff below — no hot spin.
                    inbox_put(*unlanded)
                    unlanded = None
                recover_orphans()
                if not sock_available():
                    backoff = backoff_wait(backoff)
                    continue
                if not seated:
                    # force=True ONLY for the identity the wire has actually disproven.
                    # ensure_membership()'s identity()-truthy fast path is right for every
                    # other case, and forcing past it while holding a live seat would fire
                    # a chat-join for a room we are already in. It stays the one automatic
                    # join entry point either way: never a bare do_join(), so a second
                    # request can never orphan an approval already in flight (D5), and a
                    # spent budget (denied, or a `leave`) simply asks for nothing.
                    if not ensure_membership(force=bool(dead_id) and identity() == dead_id):
                        time.sleep(JOIN_POLL)      # budget spent — nothing left to ask
                        continue
                    # An identity is a reason to PROBE, never proof of a seat — and the
                    # absence of one is the only thing that means "no seat yet". A
                    # reseat legitimately hands back the SAME participantID (BCT retires
                    # and re-seats an external participant deliberately, preserving its
                    # identity AND its unread cursor across a prune), so gating on
                    # `identity() == dead_id` here livelocked: the approval wrote that id
                    # straight back, the gate stayed true forever, ensure_membership()
                    # short-circuited on its identity fast path, and the daemon spun every
                    # JOIN_POLL — alive, seated on the server, permanently deaf. The
                    # chat-list below is the seat detector; if the wire says NOT_INVITED
                    # again, dead_id is simply re-armed. Cost: one extra chat-list per
                    # JOIN_POLL while an approval is pending.
                    if not identity():
                        time.sleep(JOIN_POLL)      # a request is in flight; no seat yet
                        continue
                    r = rpc("chat-list", [], identity())   # the wire, not the file, seats us
                    last_tick = time.time()
                    if not r.get("ok"):
                        if r.get("error") == NOT_INVITED:
                            dead_id = identity()   # still dead — keep force armed for it
                        time.sleep(JOIN_POLL)
                        continue
                    dead_id = ""                   # cleared only by a seat the wire confirms
                    seated = True
                if time.time() - last_tick >= presence_interval:
                    r = rpc("chat-list", [], identity())   # prune defence; read-only
                    last_tick = time.time()
                    if not r.get("ok") and r.get("error") == NOT_INVITED:
                        seated, dead_id = False, identity()
                        continue
                started = time.time()
                r = rpc("chat-listen", [], identity(), timeout=listen_timeout)
                if not r.get("ok"):
                    if r.get("error") == NOT_INVITED:
                        seated, dead_id = False, identity()   # BCT restarted or evicted us
                        continue
                    backoff = backoff_wait(backoff)
                    continue
                backoff = BACKOFF_MIN
                text = r.get("text") or ""
                if text and text not in (NO_NEW, NO_MENTION):
                    obj = load_identity() or {}
                    unlanded = (text, obj.get("name", default_name()))
                    inbox_put(*unlanded)        # BEFORE the next listen
                    unlanded = None             # landed; nothing to retry
                    continue                    # a busy room drains at full speed
                # Silence. A push window is supposed to HOLD (~30s server-side); one that
                # answers instantly is a bridge too old to hold it, and re-arming against
                # that in a tight loop turns this daemon into a busy-wait on the user's
                # remote. Floor the re-arm at a tenth of the window we asked for (≤1s).
                # It costs a live room nothing: an unheard mention stays unread server-side
                # until some listen collects it, so pausing here loses no message.
                held, floor = time.time() - started, min(1.0, listen_timeout / 10.0)
                if held < floor:
                    time.sleep(floor - held)
            except (Exception, SystemExit):
                # A bad tick is a failed tick, nothing more. The daemon's whole job is to
                # keep listening; a die() out of a chained join, or a full disk, must never
                # take the ear down with it.
                backoff = backoff_wait(backoff)
    finally:
        if pidfile_owner() == me:
            forget(PIDFILE)             # only ever release a pidfile we still own


# ---- delivery ----------------------------------------------------------
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
    inbox locally for up to STANDBY_HOLD — zero tokens, zero RPC."""
    v = os.environ.get("BCT_CHAT_MODE", "").strip().lower()
    if v in ("standby", "work"):
        return v
    legacy = os.environ.get("BCT_CHAT_STANDBY", "").strip().lower()
    if legacy in ("0", "off", "false", "no"):
        return "work"
    return "work"


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
    not dump 4000 lines into a turn."""
    name = item.get("name") or default_name()
    lines = [f"[bct-chat] 단체 채팅방 — 당신은 @{name} 입니다. 새 메시지:"]
    if dropped:
        lines.append(f"(오래된 멘션 {dropped}건 생략)")
    body = [l for l in (item.get("text") or "").splitlines() if l]
    if len(body) > DIGEST_MAX_LINES:
        body = body[-DIGEST_MAX_LINES:]
        lines.append(f"(앞부분 생략 — 최근 {DIGEST_MAX_LINES}줄만)")
    lines += body
    lines.append(REPLY_HINT)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > DIGEST_MAX_BYTES:
        text = text.encode("utf-8")[:DIGEST_MAX_BYTES].decode("utf-8", "ignore") + "\n(생략)"
    return text


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
        remark_session(payload)          # before the spawn: the daemon exits on an empty set
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
        remark_session(payload)
        ensure_daemon()
        recover_orphans()
        got = inbox_claim()
        if got is None:
            return
        forget(CHAIN)                    # a user prompt ends any automatic chain
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
        sid = hook_session_id(hook_payload())
        if sid:
            unmark_session(sid)
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
        clear_join_state()                    # manual intent always wins — clears a suspension too
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
        do_leave()
    elif verb == "daemon":
        do_daemon()                           # spawned by the hooks; not for humans
    else:
        die(f"unknown verb: {verb}")

if __name__ == "__main__":
    main(sys.argv[1:])
