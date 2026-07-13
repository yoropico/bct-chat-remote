#!/usr/bin/env python3
"""A denied/expired join request must not nag the dock: no automatic re-request
for 30 min. A human typing `join` at the remote's shell always wins."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer  # noqa: E402

NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


def run(args, home, sock_spec):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT] + args, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(self.state)
        self.cooldown = os.path.join(self.state, "join-cooldown.json")
        self.pending = os.path.join(self.state, "pending-join.json")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_cooldown(self, ago):
        with open(self.cooldown, "w", encoding="utf-8") as f:
            json.dump({"lastFailedAt": time.time() - ago, "outcome": "expired"}, f)

    def cmds(self, srv):
        return [r["cmd"] for r in srv.received]

    def test_expired_request_arms_the_cooldown(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-1"}
            return {"ok": False, "error": "expired"}          # chat-join-poll

        with open(self.pending, "w", encoding="utf-8") as f:
            json.dump({"requestID": "REQ-1", "name": "HOST"}, f)
        srv = FakeChatServer(handler)
        try:
            run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            with open(self.cooldown, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["outcome"], "expired")
        finally:
            srv.close()

    def test_session_start_within_cooldown_does_not_request(self):
        self.write_cooldown(ago=60)                            # 1 min ago: still cooling
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-2"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.pending))
        finally:
            srv.close()

    def test_session_start_after_cooldown_requests_again(self):
        self.write_cooldown(ago=1801)                          # 30 min + 1 s: expired
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-3"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))
        finally:
            srv.close()

    def test_manual_join_ignores_the_cooldown(self):
        self.write_cooldown(ago=60)
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-4"}
            return {"ok": True, "text": "approved\n" + "C1A6063F-0124-4229-9CE3-D757348A70F2"}

        srv = FakeChatServer(handler)
        try:
            r = run(["join", "HOST"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.cooldown))    # approval clears it
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
