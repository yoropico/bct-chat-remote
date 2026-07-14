"""Wire transport: unix socket (or TCP fallback) connection to the BCT bridge."""
import json, os, re, socket, subprocess, sys, time

from bctchat.config import *


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


def connect(timeout=10):
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    return s


def rpc(cmd, args, pane_id="", timeout=10):
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    try:
        s = connect(timeout)
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
