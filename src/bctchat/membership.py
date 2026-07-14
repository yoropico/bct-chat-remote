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
    it stick: session_start() will happily respawn the heartbeat daemon after a leave
    (spawn_heartbeat() has no suspension check, and there is no ensure_daemon() gate yet —
    that's Task 6), but the respawned daemon finds may_request_join() False and never
    re-requests — while the session markers survive, because they describe which claude
    sessions are alive, and that is still true after leaving the room."""
    r = rpc("chat-leave", [], identity())
    forget(IDENTITY)
    forget(PENDING)
    st = join_state()
    st["suspended"] = True
    st["lastOutcome"] = "left"
    save(JOIN_STATE, st)
    if not r.get("ok") and r.get("error") != NOT_INVITED:
        die(r.get("error", "error"))
