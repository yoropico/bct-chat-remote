#!/usr/bin/env python3
"""session-start must trust BCT, not identity.json.

A BCT restart resets the room but the remote's identity.json outlives it. If
session-start takes the file as proof of membership it returns silently, no join
banner is raised, and the host is simply absent from the room. Ask the bridge.
"""
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
STALE_ID = "03AD4294-8854-4208-9417-269788BF9E64"


def run_session_start(home, sock_spec):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT, "session-start"],
                          env=env, capture_output=True, text=True, timeout=30)


class SessionStartRejoinTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(self.state)
        self.identity = os.path.join(self.state, "identity.json")
        self.pending = os.path.join(self.state, "pending-join.json")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_identity(self, pid=STALE_ID):
        with open(self.identity, "w", encoding="utf-8") as f:
            json.dump({"participantID": pid, "name": "RS-BGLEE-REMOTE"}, f)

    def cmds(self, srv):
        return [r["cmd"] for r in srv.received]

    def test_stale_identity_rerequests_join(self):
        # BCT restarted: it no longer knows this participant.
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-9"}
            return {"ok": False, "error": NOT_INVITED}

        self.write_identity()
        srv = FakeChatServer(handler)
        try:
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))
            with open(self.pending, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["requestID"], "REQ-9")
            # The dead id is KEPT until a new approval overwrites it: the rejoin path
            # needs the name stored beside it, and the heartbeat needs something to send.
            with open(self.identity, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["participantID"], STALE_ID)
        finally:
            srv.close()

    def test_stale_identity_within_cooldown_does_not_request(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-NOPE"}
            return {"ok": False, "error": NOT_INVITED}

        self.write_identity()
        with open(os.path.join(self.state, "join-cooldown.json"), "w", encoding="utf-8") as f:
            json.dump({"lastFailedAt": time.time() - 60, "outcome": "denied"}, f)
        srv = FakeChatServer(handler)
        try:
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertTrue(os.path.exists(self.identity))
        finally:
            srv.close()

    def test_live_identity_is_silent_noop(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.write_identity()
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.pending))
            with open(self.identity, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["participantID"], STALE_ID)
        finally:
            srv.close()

    def test_transient_error_keeps_identity(self):
        # Anything other than "not invited" (bridge hiccup) must not drop the
        # identity or spam the user with a join banner.
        srv = FakeChatServer(lambda req: {"ok": False, "error": "internal error"})
        try:
            self.write_identity()
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertTrue(os.path.exists(self.identity))
        finally:
            srv.close()

    def test_pending_request_is_not_duplicated(self):
        # Awaiting approval: poll, never raise a second banner.
        def handler(req):
            if req["cmd"] == "chat-join-poll":
                return {"ok": True, "text": "pending"}
            return {"ok": True, "text": "REQ-DUP"}

        with open(self.pending, "w", encoding="utf-8") as f:
            json.dump({"requestID": "REQ-1", "name": "RS-BGLEE-REMOTE"}, f)
        srv = FakeChatServer(handler)
        try:
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            with open(self.pending, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["requestID"], "REQ-1")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
