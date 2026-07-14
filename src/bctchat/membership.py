"""Join/cooldown/identity: requesting, polling and holding room membership."""
import json, os, re, socket, subprocess, sys, time

from bctchat.config import *
from bctchat.wire import *
from bctchat.state import *
from bctchat.cli import *


def cooldown_remaining():
    """Seconds until an automatic join request is allowed again (0 = now)."""
    obj = load(COOLDOWN)
    if not obj:
        return 0
    left = JOIN_COOLDOWN - (time.time() - obj.get("lastFailedAt", 0))
    return int(left) if left > 0 else 0


def may_request_join():
    return cooldown_remaining() == 0


def note_join_failure(outcome):
    save(COOLDOWN, {"lastFailedAt": time.time(), "outcome": outcome})


def clear_cooldown():
    forget(COOLDOWN)


def request_join_if_allowed(name):
    """Automatic (non-blocking) join request, gated by the cooldown. The manual
    `join` verb bypasses this — a human at the remote's shell always wins."""
    if not may_request_join():
        print(f"입장 재요청 쿨다운 중 — {cooldown_remaining() // 60}분 후 재시도", file=sys.stderr)
        return False
    do_join(name, wait_approval=False)
    return True


def identity():
    obj = load(IDENTITY)
    return obj.get("participantID", "") if obj else ""


def membership_live():
    """Does BCT still know this identity? A BCT restart resets the room, but
    identity.json outlives it — so ask the bridge, never trust the file. The dead
    identity is KEPT (it only ever earns NOT_INVITED, the rejoin needs the name
    beside it, and the heartbeat needs something to send); a new approval
    overwrites it. Any other error (bridge hiccup) counts as live: better silent
    than a spurious join banner."""
    r = rpc("chat-list", [], identity())      # read-only probe; consumes no messages
    return r.get("ok") or r.get("error") != NOT_INVITED


def claim_pending():
    """If a session-start hook left a requestID, try to claim the identity."""
    obj = load(PENDING)
    if not obj:
        return False
    r = rpc("chat-join-poll", [obj["requestID"]])
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        forget(PENDING)
        clear_cooldown()                      # seated — the slate is clean
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        forget(PENDING)
        note_join_failure(r["error"])         # arm the 30-min cooldown
    return False


def do_join(name, wait_approval=True):
    r = rpc("chat-join", [name])
    if not r.get("ok"):
        die(r.get("error", "join failed"))
    req_id = r["text"]
    save(PENDING, {"requestID": req_id, "name": name})
    if not wait_approval:
        print(f"join requested ({req_id}) — approve in the BCT chat dock", file=sys.stderr)
        return
    print("입장 요청됨 — BCT 채팅 도크에서 승인해 주세요 (5분 내)", file=sys.stderr)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        if claim_pending():
            print("입장 승인됨", file=sys.stderr)
            return
        if not os.path.exists(PENDING):
            die("denied or expired")
    die("승인 대기 시간 초과")


def authed(cmd, args, timeout=10):
    """RPC with identity; auto re-join on identity invalidation (BCT restart/eviction)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity(), timeout=timeout)
    if not r.get("ok") and r.get("error") == NOT_INVITED:
        obj = load(IDENTITY)
        if obj:
            if not may_request_join():
                return r                      # cooling down — surface NOT_INVITED as-is
            print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
            do_join(obj["name"])              # blocking: a live verb wants an answer
            r = rpc(cmd, args, identity(), timeout=timeout)
    return r
