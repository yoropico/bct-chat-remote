#!/usr/bin/env python3
"""AF_UNIX is the PRODUCTION transport on macOS and Linux and had zero coverage —
which is why a zombie socket file (an ssh reconnect without StreamLocalBindUnlink)
read as 'healthy' for so long."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402
from test_tcp_transport import CLIENT  # noqa: E402

unix_only = unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no AF_UNIX on this platform")


class UnixChatServer:
    def __init__(self, path, handler):
        self.handler = handler
        self.received = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

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
                    continue                 # availability probe: connect + close
                req = json.loads(buf.decode())
                self.received.append(req)
                conn.sendall((json.dumps(self.handler(req)) + "\n").encode())

    def close(self):
        self.sock.close()


@unix_only
class UnixTransportTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "bct.sock")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_client(self, args):
        env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID",)}
        env.update(HOME=self.home,
                   BCT_CHAT_HOME=os.path.join(self.home, ".bct-chat"),
                   BCT_CHAT_SOCK=self.path)
        return subprocess.run([sys.executable, CLIENT] + args,
                              env=env, capture_output=True, text=True, timeout=30)

    def mod(self):
        os.environ["BCT_CHAT_SOCK"] = self.path
        try:
            return load_fresh_module(self.home)
        finally:
            os.environ.pop("BCT_CHAT_SOCK", None)

    def test_read_over_unix_round_trip(self):
        srv = UnixChatServer(self.path, lambda req: {"ok": True, "text": "hello-unix"})
        try:
            r = self.run_client(["read"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "hello-unix")
            self.assertEqual(srv.received[-1]["cmd"], "chat-read")
        finally:
            srv.close()

    def test_sock_available_true_when_a_listener_is_there(self):
        srv = UnixChatServer(self.path, lambda req: {"ok": True, "text": ""})
        try:
            self.assertTrue(self.mod().sock_available())
        finally:
            srv.close()

    def test_zombie_socket_file_is_not_available(self):
        # The file exists, nothing is listening: os.path.exists() said "healthy" and
        # the client then sat silently outside the room forever (D4).
        open(self.path, "w").close()
        self.assertFalse(self.mod().sock_available())

    def test_missing_socket_is_not_available(self):
        self.assertFalse(self.mod().sock_available())


if __name__ == "__main__":
    unittest.main()
