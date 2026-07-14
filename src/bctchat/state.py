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
    os.makedirs(STATE_DIR, exist_ok=True)
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
            SYNCHRONIZE = 0x00100000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
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
