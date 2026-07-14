#!/usr/bin/env python3
"""① idle standby window: when nothing is pending and BCT_CHAT_STANDBY is not
disabled, stop_hook holds one chat-listen and delivers a pushed mention as a
block digest. Off-switch and pending-mention paths never open the window."""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class StandbyWindowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.sock_available = lambda: True
        self.mod.drain_stdin = lambda: None
        self.rpc_timeouts = {}
        os.environ.pop("BCT_PANE_ID", None)
        os.environ.pop("BCT_CHAT_STANDBY", None)

    def tearDown(self):
        os.environ.pop("BCT_CHAT_STANDBY", None)

    def run_verb(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def rpc_map(self, responses):
        calls = []
        def fake(cmd, args, pane_id="", timeout=10):
            calls.append(cmd)
            self.rpc_timeouts[cmd] = timeout
            return responses[cmd]
        self.mod.rpc = fake
        return calls

    def test_enabled_by_default_and_off_switch(self):
        os.environ.pop("BCT_CHAT_STANDBY", None)
        self.assertTrue(self.mod.standby_enabled())
        for off in ("0", "off", "false", "NO"):
            os.environ["BCT_CHAT_STANDBY"] = off
            self.assertFalse(self.mod.standby_enabled(), off)
        os.environ["BCT_CHAT_STANDBY"] = "1"
        self.assertTrue(self.mod.standby_enabled())

    def test_idle_window_delivers_pushed_mention(self):
        calls = self.rpc_map({"chat-peek": {"ok": True, "text": "0 0"},
                              "chat-listen": {"ok": True, "text": "yoros: @svr 대기중이야?"}})
        obj = json.loads(self.run_verb(self.mod.stop_hook))
        self.assertEqual(obj["decision"], "block")
        self.assertIn("당신은 @svr", obj["reason"])
        self.assertIn("yoros: @svr 대기중이야?", obj["reason"])
        self.assertIn("chat-listen", calls)
        self.assertEqual(self.rpc_timeouts["chat-listen"], 40)

    def test_idle_window_sentinel_is_silent(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "0 0"},
                      "chat-listen": {"ok": True, "text": self.mod.NO_MENTION}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_pending_mention_skips_listen(self):
        calls = self.rpc_map({"chat-peek": {"ok": True, "text": "1 1"},
                              "chat-read": {"ok": True, "text": "yoros: @svr 봐줘"}})
        obj = json.loads(self.run_verb(self.mod.stop_hook))
        self.assertEqual(obj["decision"], "block")
        self.assertNotIn("chat-listen", calls)

    def test_off_switch_skips_listen(self):
        os.environ["BCT_CHAT_STANDBY"] = "0"
        calls = self.rpc_map({"chat-peek": {"ok": True, "text": "0 0"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertNotIn("chat-listen", calls)


if __name__ == "__main__":
    unittest.main()
