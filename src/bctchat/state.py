"""Local state: identity/pending/cooldown JSON files, the stable copy, session markers."""
import json, os, re, socket, subprocess, sys, time

from bctchat.config import *


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
