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
