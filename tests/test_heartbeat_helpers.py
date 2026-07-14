#!/usr/bin/env python3
"""Shared test helper: a fresh in-process bct-chat module whose STATE_DIR resolves
under a temp home. BCT_CHAT_HOME — never HOME — is the isolation knob (Windows'
expanduser ignores HOME and would hand back the developer's real profile)."""
import importlib.util
import os
import signal
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "scripts", "bct-chat.py")


def _load_proc_alive():
    # A plain import (no BCT_CHAT_HOME override) is safe: proc_alive() is pure and
    # module-level code never touches disk, only computes STATE_DIR as a string.
    spec = importlib.util.spec_from_file_location("bct_chat_probe", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.proc_alive


proc_alive = _load_proc_alive()


def reaped_pid():
    """A pid guaranteed dead on this host. The constant 999999 used to stand in for
    "certainly dead", but Linux's default pid_max is 4194304 — 999999 is an ordinary,
    possibly-live pid there (macOS caps at 99999, which is why this never flaked there).
    Spawning and waiting on a real child reaps it immediately, which is dead everywhere."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


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
    Test-only cleanup on the developer's machine, not the shipped client — but the liveness
    probe still goes through proc_alive(), never a bare os.kill(pid, 0): this codebase
    documents that as forbidden (Windows maps ANY os.kill signal to TerminateProcess), and
    a test helper that broke its own rule would undercut the very thing it teaches."""
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
        if not proc_alive(pid):
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
