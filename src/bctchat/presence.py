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
                        # This tick already IS the fresh wire evidence ensure_membership()'s
                        # identity()-truthy fast path would otherwise trust blindly (a dead
                        # id kept on disk for its name) — so drive the same pending-first,
                        # budget-gated sequence directly instead of going through it. Poll an
                        # outstanding request first — never fire a fresh chat-join while one
                        # is outstanding, or the new requestID orphans any approval already
                        # in flight for the old one (D5). The budget itself is what makes a
                        # `leave` stick (D8) and a denied host stop nagging (D9): once
                        # suspended, may_request_join() is False and this tick does nothing.
                        if pending():
                            claim_pending()
                        elif may_request_join():
                            obj = load_identity()
                            do_join(obj["name"] if obj else default_name(), wait_approval=False)
                        fails = 0
                    elif not r.get("ok") and str(r.get("error", "")).startswith("socket"):
                        fails += 1
                    else:
                        fails = 0
                        claim_pending()  # an approval may have landed since the last tick
            except (Exception, SystemExit):
                # e.g. do_join -> die() on a chat-join error. A failed tick, nothing more —
                # never let it kill the daemon outright.
                fails += 1
            if fails >= 2:
                break               # tunnel is down; the next session start respawns us
            time.sleep(interval)
    finally:
        if pidfile_owner() == me:
            forget(PIDFILE)         # only ever release a pid file we still own
