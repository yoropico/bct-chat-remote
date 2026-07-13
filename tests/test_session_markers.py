#!/usr/bin/env python3
"""SessionStart drops a marker (the daemon's refcount) and SessionEnd removes it.
The session id comes from the hook's JSON payload on stdin; an interactive run has
none and must not leave markers behind."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer  # noqa: E402


def run_hook(verb, home, sock_spec, payload):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT, verb], env=env, input=payload,
                          capture_output=True, text=True, timeout=30)


class SessionMarkerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(self.state)
        with open(os.path.join(self.state, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"participantID": "C1A6063F-0124-4229-9CE3-D757348A70F2", "name": "HOST"}, f)
        self.marker = os.path.join(self.state, "sessions", "sess-abc")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_session_start_marks_and_session_end_unmarks(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        payload = json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionStart"})
        try:
            r = run_hook("session-start", self.home, f"tcp:127.0.0.1:{srv.port}", payload)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(self.marker))

            r = run_hook("session-end", self.home, f"tcp:127.0.0.1:{srv.port}",
                         json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionEnd"}))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(self.marker))
        finally:
            srv.close()

    def test_interactive_run_leaves_no_marker(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            r = run_hook("session-start", self.home, f"tcp:127.0.0.1:{srv.port}", "")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.isdir(os.path.join(self.state, "sessions")))
        finally:
            srv.close()

    def test_session_end_without_a_socket_is_a_silent_noop(self):
        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
