#!/usr/bin/env python3
"""The daemon proves the host is alive so BCT's 10-min prune cannot evict it while
its claude sessions are merely quiet — and it gets out of the way the moment those
sessions are gone, the tunnel dies, or it has run too long."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer, free_port  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


def start_daemon(home, sock_spec, interval="0.2", max_uptime="30"):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.Popen([sys.executable, CLIENT, "heartbeat",
                             "--interval", interval, "--max-uptime", max_uptime],
                            env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(pred, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        self.sessions = os.path.join(self.state, "sessions")
        os.makedirs(self.sessions)
        with open(os.path.join(self.state, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"participantID": IDENT, "name": "HOST"}, f)
        self.marker = os.path.join(self.sessions, "sess-1")
        open(self.marker, "w").close()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_ticks_touch_the_bridge_with_the_identity(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-list" for r in srv.received)))
            tick = [r for r in srv.received if r["cmd"] == "chat-list"][0]
            self.assertEqual(tick["paneID"], IDENT)
        finally:
            srv.close()

    def test_exits_when_the_last_session_marker_is_gone(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-list" for r in srv.received)))
            os.remove(self.marker)                       # every claude on this host exited
            self.assertTrue(wait_for(lambda: self.proc.poll() is not None))
            self.assertEqual(self.proc.returncode, 0)
        finally:
            srv.close()

    def test_exits_when_the_socket_is_unreachable(self):
        # Tunnel down: two consecutive failed ticks and it gets out of the way.
        self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{free_port()}")
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None))
        self.assertEqual(self.proc.returncode, 0)

    def test_not_invited_drives_a_rejoin_request(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-HB"}
            return {"ok": False, "error": "이 패널은 대화방에 초대되지 않았습니다"}

        srv = FakeChatServer(handler)
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-join" for r in srv.received)))
            pending = os.path.join(self.state, "pending-join.json")
            self.assertTrue(wait_for(lambda: os.path.exists(pending)))
        finally:
            srv.close()

    def test_a_second_daemon_does_not_start_while_one_is_live(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            pidfile = os.path.join(self.state, "heartbeat.pid")
            self.assertTrue(wait_for(lambda: os.path.exists(pidfile)))
            second = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            second.wait(timeout=10)
            self.assertEqual(second.returncode, 0)        # yielded to the live one
            with open(pidfile, encoding="utf-8") as f:
                self.assertEqual(int(f.read().strip()), self.proc.pid)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
