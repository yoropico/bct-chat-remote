#!/usr/bin/env python3
"""The user-facing verbs must be inbox-aware, or they appear to have lost the very
messages the daemon just captured — and their arguments must be parsed, not guessed
(`wait --timeuot 60` silently meant 300s)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.sock_available = lambda: True
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(argv)
        return buf.getvalue()

    def test_read_drains_the_inbox_before_the_socket(self):
        self.mod.inbox_put("yoros: @svr 봐줘", "svr")
        self.mod.rpc = lambda cmd, *a, **k: (self.calls.append(cmd),
                                             {"ok": True, "text": self.mod.NO_NEW})[1]
        out = self.run_main(["read"])
        self.assertIn("yoros: @svr 봐줘", out)
        self.assertEqual(self.calls, ["chat-read"])
        self.assertIsNone(self.mod.inbox_claim())          # drained, not re-delivered

    def test_wait_waits_on_the_inbox_not_on_chat_read(self):
        # D13: the old 2s chat-read poll CONSUMED the cursor and stole mentions from
        # the push path the daemon now depends on.
        import threading
        self.mod.rpc = lambda cmd, *a, **k: self.fail("wait must not poll the socket")
        threading.Timer(0.2, lambda: self.mod.inbox_put("pushed", "svr")).start()
        out = self.run_main(["wait", "--timeout", "5"])
        self.assertIn("pushed", out)

    def test_listen_waits_on_the_inbox(self):
        self.mod.rpc = lambda *a, **k: self.fail("listen must not hold the socket")
        self.mod.inbox_put("already here", "svr")
        self.assertIn("already here", self.run_main(["listen", "--timeout", "5"]))

    def test_a_misspelled_flag_is_an_error_not_a_default(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                self.mod.main(["wait", "--timeuot", "60"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_a_negative_interval_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                self.mod.main(["daemon", "--interval", "-1"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_send_goes_through_authed(self):
        self.mod.rpc = lambda cmd, *a, **k: (self.calls.append(cmd), {"ok": True, "text": ""})[1]
        self.run_main(["send", "hello", "room"])
        self.assertEqual(self.calls, ["chat-send"])

    def test_heartbeat_is_still_accepted_as_an_alias_for_daemon(self):
        seen = []
        self.mod.do_daemon = lambda **kw: seen.append(kw)
        self.run_main(["heartbeat", "--interval", "5"])
        self.assertEqual(seen[0]["presence_interval"], 5)


if __name__ == "__main__":
    unittest.main()
