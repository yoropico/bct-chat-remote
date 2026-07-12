#!/usr/bin/env python3
"""bct-chat.py — external participant client for BCT's group chat.

Speaks the line-JSON wire ({"paneID","cmd","args"} -> {"ok","text","error"})
over a unix socket (default ~/.bct-chat.sock — the ssh-RemoteForward'ed BCT
control socket; override with $BCT_CHAT_SOCK). On hosts without AF_UNIX
(Windows CPython), forward a TCP port instead and set
$BCT_CHAT_SOCK=tcp:<host>:<port>. Pure stdlib.
Spec: docs/superpowers/specs/2026-07-12-chat-external-participants-design.md
"""
import json, os, socket, sys, time

if hasattr(sys.stdout, "reconfigure"):
    # Wire and room text are UTF-8; never trust the locale (Korean Windows = cp949).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STATE_DIR = os.path.expanduser("~/.bct-chat")
IDENTITY = os.path.join(STATE_DIR, "identity.json")
PENDING = os.path.join(STATE_DIR, "pending-join.json")
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


def tcp_target(spec):
    """$BCT_CHAT_SOCK=tcp:<host>:<port> -> (host, port); None means unix path."""
    if not spec.startswith("tcp:"):
        return None
    host, _, port = spec[4:].rpartition(":")
    if not port.isdigit():
        return None
    return (host or "127.0.0.1", int(port))


def default_name():
    return socket.gethostname()


def sock_available():
    t = tcp_target(SOCK)
    if t is None:
        return os.path.exists(SOCK)
    try:
        socket.create_connection(t, timeout=3).close()
        return True
    except OSError:
        return False


def connect():
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=10)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(SOCK)
    return s


def rpc(cmd, args, pane_id=""):
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    try:
        s = connect()
        s.sendall((json.dumps({"paneID": pane_id, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        s.close()
        try:
            return json.loads(buf.decode())
        except ValueError:
            return {"ok": False, "error": "malformed response from bridge"}
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}


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


STABLE = os.path.join(STATE_DIR, "bct-chat.py")


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


def identity():
    obj = load(IDENTITY)
    return obj.get("participantID", "") if obj else ""


def claim_pending():
    """If a session-start hook left a requestID, try to claim the identity."""
    obj = load(PENDING)
    if not obj:
        return False
    r = rpc("chat-join-poll", [obj["requestID"]])
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        try:
            os.remove(PENDING)
        except OSError:
            pass
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        try:
            os.remove(PENDING)
        except OSError:
            pass
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


def authed(cmd, args):
    """RPC with identity; auto re-join on identity invalidation (BCT restart/kick)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity())
    if not r.get("ok") and r.get("error") == NOT_INVITED:
        obj = load(IDENTITY)
        if obj:
            try:
                os.remove(IDENTITY)
            except OSError:
                pass
            print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
            do_join(obj["name"])
            r = rpc(cmd, args, identity())
    return r


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main(argv):
    if not argv:
        die("usage: bct-chat.py <join|send|read|wait|list|leave|session-start> …")
    verb, rest = argv[0], argv[1:]
    if verb == "join":
        do_join(" ".join(rest) or default_name())
    elif verb == "session-start":
        # Fired by the claude-code SessionStart hook: silent no-ops by design.
        if os.environ.get("BCT_PANE_ID"):
            return                      # BCT pane — statusline auto-invite owns this
        ensure_stable_copy()
        if not sock_available():
            return                      # no ssh session forwarding the socket
        if identity() or load(PENDING):
            claim_pending()
            return                      # already joined / already requested
        do_join(default_name(), wait_approval=False)
    elif verb == "send":
        msg = " ".join(rest)
        if not msg:
            die('send needs "<message>"')
        r = authed("chat-send", [msg])
        if not r.get("ok"):
            die(r.get("error", "error"))
    elif verb == "read":
        r = authed("chat-read", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "wait":
        timeout = 300
        if "--timeout" in rest:
            i = rest.index("--timeout")
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                die("wait --timeout <seconds>")
            timeout = int(rest[i + 1])
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = authed("chat-read", [])
            if not r.get("ok"):
                die(r.get("error", "error"))
            if r.get("text") and r["text"] != NO_NEW:
                print(r["text"])
                return
            time.sleep(2)
        die(f"timeout: no new message within {timeout}s")
    elif verb == "list":
        r = authed("chat-list", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "leave":
        r = rpc("chat-leave", [], identity())
        for p in (IDENTITY, PENDING):
            try:
                os.remove(p)
            except OSError:
                pass
        if not r.get("ok") and r.get("error") != NOT_INVITED:
            die(r.get("error", "error"))
    else:
        die(f"unknown verb: {verb}")


if __name__ == "__main__":
    main(sys.argv[1:])
