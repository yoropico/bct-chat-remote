#!/usr/bin/env python3
"""A join request that failed must not nag the dock with automatic re-requests for
30 min. A DENIED request is respected even across a session restart (an explicit
"no"); an EXPIRED one is dropped on a genuine session (re)start (it timed out or was
lost to churn — fresh intent should re-request). A human typing `join` always wins."""
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

    def write_cooldown(self, ago, outcome="expired"):
        with open(self.cooldown, "w", encoding="utf-8") as f:
            json.dump({"lastFailedAt": time.time() - ago, "outcome": outcome}, f)

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

    def test_session_start_within_denied_cooldown_does_not_request(self):
        # A DENIED request is an explicit "no": its cooldown is respected even on a
        # session restart, so no fresh banner nags the user who just said no.
        self.write_cooldown(ago=60, outcome="denied")         # 1 min ago: still cooling
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-2"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.pending))
            self.assertTrue(os.path.exists(self.cooldown))    # denied cooldown left intact
        finally:
            srv.close()

    def test_session_start_within_expired_cooldown_rerequests(self):
        # An EXPIRED request is not an explicit "no" — it timed out, or was lost to a BCT
        # restart during churn. A genuine session (re)start is fresh intent, so session-start
        # drops that cooldown and re-requests instead of silently sitting out its 30 min.
        self.write_cooldown(ago=60, outcome="expired")        # still within the 30-min window
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-EXP"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))         # cooldown cleared -> re-requested
            self.assertFalse(os.path.exists(self.cooldown))    # the expired cooldown was dropped
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
