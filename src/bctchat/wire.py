"""Wire transport: unix socket (or TCP fallback) connection to the BCT bridge."""
import json, os, re, socket, subprocess, sys, time


def tcp_target(spec):
    """$BCT_CHAT_SOCK=tcp:<host>:<port> -> (host, port); None means a unix path.
    IPv6 goes in brackets: tcp:[::1]:9000."""
    if not spec.startswith("tcp:"):
        return None
    rest = spec[4:]
    if rest.startswith("["):
        host, sep, port = rest[1:].partition("]:")
        if not sep:
            return None
    else:
        host, _, port = rest.rpartition(":")
    if not port.isdigit():
        return None
    port = int(port)
    if not 1 <= port <= 65535:
        return None
    return (host or "127.0.0.1", port)


def default_name():
    return socket.gethostname()


def connect(timeout=10):
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
    except OSError:
        s.close()
        raise
    return s


def sock_available():
    """CONNECT — never os.path.exists(). A stale socket file left by an ssh reconnect
    without StreamLocalBindUnlink still exists on disk; connecting to it is the only
    way to learn that nobody is home (D4)."""
    try:
        connect(timeout=3).close()
        return True
    except OSError:
        return False


def rpc(cmd, args, pane_id="", timeout=10):
    """One request, one line-JSON reply, bounded by an OVERALL deadline — not a
    per-recv timeout, which a bridge dribbling bytes could keep resetting forever.
    The deadline covers connect, the request write, and every read: connect gets
    whatever of the budget remains (capped at 10s), and the socket is re-armed
    with the remaining budget immediately before sendall so a slow accept can't
    leave a stale, oversized timeout in force for the write. Blank lines are
    keepalives and are skipped; if two frames arrive in one read we answer from
    the first; a bridge that accepts and closes without replying is a socket
    error, not a malformed response."""
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    deadline = time.time() + timeout
    try:
        s = connect(min(max(deadline - time.time(), 0), 10))
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    try:
        left = deadline - time.time()
        if left <= 0:
            return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
        s.settimeout(left)
        s.sendall((json.dumps({"paneID": pane_id, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while True:
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue                  # keepalive
                try:
                    return json.loads(line.decode("utf-8"))
                except ValueError:
                    return {"ok": False, "error": "malformed response from bridge"}
            left = deadline - time.time()
            if left <= 0:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            s.settimeout(left)
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            if not chunk:
                return {"ok": False, "error": "socket error: bridge closed without a reply"}
            buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    finally:
        try:
            s.close()
        except OSError:
            pass
