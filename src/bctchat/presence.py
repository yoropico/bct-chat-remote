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
        except (AttributeError, TypeError, ValueError, OverflowError):
            # OverflowError is not hypothetical: json parses a literal `Infinity` (and any
            # 1e400-style overflow) to float inf, and int(inf) raises it. This function runs
            # OUTSIDE the daemon's per-tick guard, so anything escaping here is a fourth exit
            # condition — the docstring's "nothing in here may raise" has to be literally true.
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
