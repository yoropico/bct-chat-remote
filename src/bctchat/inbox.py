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


_SIDECAR_RE = re.compile(r"\.(\d{1,10})\.(?:evict|claim|bump|tmp)$")
# Digits bounded to 10: a real pid is never anywhere near that long (Linux's pid_max
# tops out at 4194304, 7 digits), but an unbounded \d+ would match a hand-planted
# absurd pid (e.g. "x.99999999999999999999.tmp") and hand it to int() -> os.kill(),
# which raises OverflowError, not OSError — proc_alive()'s guard only catches OSError
# subclasses, so that error would propagate out of the sweep instead of reading as dead.


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
    independent condition — not as a defense against pid recycling. Recycling can't
    cause a WRONG delete: if the pid in a sidecar's name has since been reused by a
    new, unrelated process, proc_alive() reads it as alive and the sidecar is skipped
    regardless of mtime, which only DELAYS cleanup (the sidecar lingers until that
    new process is also gone), never triggers an early one. ORPHAN_AGE is just an
    extra staleness margin applied once the owner is already confirmed dead.

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
