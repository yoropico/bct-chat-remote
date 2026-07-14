#!/usr/bin/env python3
"""BCT_CHAT_SOCK=tcp:<host>:<port> must speak the same line-JSON wire over TCP
(Windows CPython has no AF_UNIX; the ssh RemoteForward becomes a local TCP port)."""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "scripts", "bct-chat.py")


class FakeChatServer:
    """Line-JSON TCP server; tolerates probe connections that close without data."""

    def __init__(self, handler):
        self.handler = handler
        self.received = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                buf = b""
                try:
                    while not buf.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    continue
                if not buf.strip():
                    continue                      # availability probe: connect+close
                req = json.loads(buf.decode())
                self.received.append(req)
                conn.sendall((json.dumps(self.handler(req)) + "\n").encode())

    def close(self):
        self.sock.close()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_client(args, home, sock_spec):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT] + args,
                          env=env, capture_output=True, text=True, timeout=30)


class TcpTransportTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_read_over_tcp_round_trip(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "hello-from-tcp"})
        try:
            r = run_client(["read"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "hello-from-tcp")
            self.assertEqual(srv.received[-1]["cmd"], "chat-read")
        finally:
            srv.close()

    def test_read_prints_emoji_under_non_utf8_locale(self):
        # Room text may hold emoji; Korean-Windows stdout defaults to cp949.
        srv = FakeChatServer(lambda req: {"ok": True, "text": "hi 🎉"})
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
            env.update(HOME=self.home, BCT_CHAT_HOME=os.path.join(self.home, ".bct-chat"),
                       BCT_CHAT_SOCK=f"tcp:127.0.0.1:{srv.port}",
                       LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
            r = subprocess.run([sys.executable, "-X", "utf8=0", CLIENT, "read"],
                               env=env, capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
            self.assertIn("hi 🎉", r.stdout.decode("utf-8"))
        finally:
            srv.close()

    def test_tcp_unreachable_reports_socket_error(self):
        r = run_client(["read"], self.home, f"tcp:127.0.0.1:{free_port()}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("socket error", r.stderr)
        self.assertNotIn("socket not found", r.stderr)

    def test_session_start_tcp_reachable_requests_join(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-1"})
        try:
            r = run_client(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(srv.received[-1]["cmd"], "chat-join")
            pending = os.path.join(self.home, ".bct-chat", "pending-join.json")
            with open(pending) as f:
                self.assertEqual(json.load(f)["requestID"], "REQ-1")
        finally:
            srv.close()

    def test_session_start_tcp_unreachable_is_silent_noop(self):
        # Pinned invariant: no listener -> exit 0, no join request left behind.
        r = run_client(["session-start"], self.home, f"tcp:127.0.0.1:{free_port()}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bct-chat", "pending-join.json")))

    def test_default_name_without_os_uname(self):
        # os.uname does not exist on Windows; the default join name must not use it.
        spec = importlib.util.spec_from_file_location("bct_chat", CLIENT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real_uname = getattr(os, "uname", None)
        if real_uname is not None:
            del os.uname
        try:
            self.assertEqual(mod.default_name(), socket.gethostname())
        finally:
            if real_uname is not None:
                os.uname = real_uname


if __name__ == "__main__":
    unittest.main()
