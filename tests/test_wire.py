#!/usr/bin/env python3
"""The wire is the only thing between us and a bridge we do not control: it must
bound its own time, tolerate a keepalive or a coalesced frame, and tell a closed
connection apart from a corrupt one."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402


class RawServer:
    """Answers one request with exactly the bytes it was given (or nothing)."""

    def __init__(self, script):
        self.script = script                # bytes to send, or None to close silently
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    conn.recv(65536)
                    if self.script is not None:
                        conn.sendall(self.script)
                except OSError:
                    pass

    def close(self):
        self.sock.close()


class WireTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def mod_for(self, port):
        os.environ["BCT_CHAT_SOCK"] = f"tcp:127.0.0.1:{port}"
        try:
            return load_fresh_module(self.home)
        finally:
            os.environ.pop("BCT_CHAT_SOCK", None)

    def test_tcp_target_parses_host_port(self):
        m = self.mod_for(1)
        self.assertEqual(m.tcp_target("tcp:1.2.3.4:9000"), ("1.2.3.4", 9000))
        self.assertEqual(m.tcp_target("tcp:9000"), ("127.0.0.1", 9000))
        self.assertEqual(m.tcp_target("tcp:[::1]:9000"), ("::1", 9000))
        self.assertIsNone(m.tcp_target("/home/u/.bct-chat.sock"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:notaport"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:0"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:70000"))

    def test_rpc_skips_keepalive_newlines(self):
        srv = RawServer(b"\n\n" + json.dumps({"ok": True, "text": "hi"}).encode() + b"\n")
        try:
            self.assertEqual(self.mod_for(srv.port).rpc("chat-list", []),
                             {"ok": True, "text": "hi"})
        finally:
            srv.close()

    def test_rpc_takes_the_first_of_two_coalesced_frames(self):
        srv = RawServer(b'{"ok": true, "text": "first"}\n{"ok": true, "text": "second"}\n')
        try:
            self.assertEqual(self.mod_for(srv.port).rpc("chat-list", [])["text"], "first")
        finally:
            srv.close()

    def test_rpc_reports_a_silent_close_as_a_socket_error(self):
        srv = RawServer(None)               # accepts, replies with nothing, closes
        try:
            r = self.mod_for(srv.port).rpc("chat-list", [])
            self.assertFalse(r["ok"])
            self.assertIn("socket error", r["error"])
            self.assertNotIn("malformed", r["error"])
        finally:
            srv.close()

    def test_rpc_honours_an_overall_deadline_on_a_dribbling_bridge(self):
        # A byte every 0.2s, never a newline: the per-recv timeout would never fire.
        class Dribble(RawServer):
            def _serve(self):
                import time
                while True:
                    try:
                        conn, _ = self.sock.accept()
                    except OSError:
                        return
                    with conn:
                        try:
                            conn.recv(65536)
                            for _ in range(100):
                                conn.sendall(b" ")
                                time.sleep(0.2)
                        except OSError:
                            pass

        srv = Dribble(b"")
        try:
            import time as t
            m = self.mod_for(srv.port)
            started = t.time()
            r = m.rpc("chat-list", [], timeout=2)
            self.assertFalse(r["ok"])
            self.assertIn("socket error", r["error"])
            self.assertLess(t.time() - started, 6, "rpc ignored its overall deadline")
        finally:
            srv.close()

    def test_rpc_deadline_covers_a_slow_connect_plus_the_send(self):
        # A slow accept must not leave sendall free to burn a fresh, full-size
        # timeout window of its own — one deadline covers connect AND the write,
        # not just the recv loop.
        class MuteServer:
            """Accepts a connection and never reads or replies. Paired with a
            payload too big to fit in the unread kernel buffers, this makes the
            client's own sendall actually block."""

            def __init__(self):
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.bind(("127.0.0.1", 0))
                self.sock.listen(4)
                self.port = self.sock.getsockname()[1]
                self.conns = []
                threading.Thread(target=self._serve, daemon=True).start()

            def _serve(self):
                while True:
                    try:
                        conn, _ = self.sock.accept()
                    except OSError:
                        return
                    self.conns.append(conn)      # accepted; never .recv(), never replied

            def close(self):
                self.sock.close()
                for c in self.conns:
                    try:
                        c.close()
                    except OSError:
                        pass

        import time as t
        srv = MuteServer()
        try:
            m = self.mod_for(srv.port)
            real_connect = m.connect

            def slow_connect(timeout=10):
                t.sleep(1.2)                     # burns most of a timeout=2 budget
                return real_connect(timeout)

            m.connect = slow_connect
            big_args = ["x" * (4 * 1024 * 1024)]  # too big for sendall to finish unread
            started = t.time()
            r = m.rpc("chat-list", big_args, timeout=2)
            elapsed = t.time() - started
            self.assertFalse(r["ok"])
            self.assertIn("socket error", r["error"])
            self.assertLess(elapsed, 3.0,
                             f"rpc's deadline did not cover connect+send ({elapsed:.2f}s)")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
