#!/usr/bin/env python3
"""Shared test helper: a fresh in-process bct-chat module whose STATE_DIR resolves
under a temp home. BCT_CHAT_HOME — never HOME — is the isolation knob (Windows'
expanduser ignores HOME and would hand back the developer's real profile)."""
import importlib.util
import os
import signal
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "scripts", "bct-chat.py")


def load_fresh_module(home):
    old = {k: os.environ.get(k) for k in ("HOME", "BCT_CHAT_HOME")}
    os.environ["HOME"] = home
    os.environ["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
    try:
        spec = importlib.util.spec_from_file_location(f"bct_chat_{id(home)}", CLIENT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def reap_daemon(pidfile):
    """A test that lets a real daemon spawn must kill it: the daemon no longer exits on a
    dead tunnel (that is the point of the rework), so nothing else would ever reap it.
    Test-only cleanup on the developer's machine, not the shipped client — os.kill(pid, 0)
    is fine to use as a liveness probe here."""
    try:
        with open(pidfile, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return                  # already dead
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return              # exited
        time.sleep(0.05)
    try:
        # Windows has no signal.SIGKILL at all (not just no delivery semantics for it) —
        # referencing it directly would AttributeError on a first-class host that never
        # needed this fallback branch to fire.
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def wait_for(pred, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False
