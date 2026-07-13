#!/usr/bin/env python3
"""Turn-boundary push: the Stop hook blocks with the digest ONLY when the room
mentions us (peek → read), and both hook verbs are silent + exit-0 on every
failure path — a broken tunnel must never break claude's turn end."""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class PushHookTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.sock_available = lambda: True
        self.mod.drain_stdin = lambda: None          # unittest stdin is not a hook pipe
        os.environ.pop("BCT_PANE_ID", None)

    def run_verb(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def rpc_map(self, responses):
        calls = []
        def fake(cmd, args, pane_id=""):
            calls.append(cmd)
            return responses[cmd]
        self.mod.rpc = fake
        return calls

    def test_mention_blocks_with_digest(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "2 1"},
                      "chat-read": {"ok": True, "text": "yoros: @svr 봐줘\nnavy: 진행중"}})
        out = self.run_verb(self.mod.stop_hook)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertIn("당신은 @svr", obj["reason"])
        self.assertIn("yoros: @svr 봐줘", obj["reason"])
        self.assertIn("bct-chat.py send", obj["reason"])

    def test_no_mention_allows_and_does_not_read(self):
        calls = self.rpc_map({"chat-peek": {"ok": True, "text": "3 0"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertEqual(calls, ["chat-peek"])       # cursor untouched — no chat-read

    def test_socket_absent_is_silent(self):
        self.mod.sock_available = lambda: False
        self.mod.rpc = lambda *a, **k: self.fail("rpc must not be called")
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_bct_pane_guard(self):
        os.environ["BCT_PANE_ID"] = "deadbeef"
        try:
            self.mod.rpc = lambda *a, **k: self.fail("rpc must not be called")
            self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        finally:
            os.environ.pop("BCT_PANE_ID", None)

    def test_peek_error_or_old_bct_is_silent(self):
        self.rpc_map({"chat-peek": {"ok": False, "error": "unknown chat verb: chat-peek"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_read_failure_after_peek_is_silent(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "1 1"},
                      "chat-read": {"ok": False, "error": "socket error: boom"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_prompt_submit_prints_plain_digest(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "1 1"},
                      "chat-read": {"ok": True, "text": "yoros: @svr 어때"}})
        out = self.run_verb(self.mod.prompt_submit_hook)
        self.assertTrue(out.startswith("[bct-chat]"))
        self.assertNotIn('"decision"', out)          # context, not control JSON

    def test_hooks_json_registers_push_hooks(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        for event, verb in (("Stop", "stop-hook"), ("UserPromptSubmit", "prompt-submit")):
            cmd = hooks[event][0]["hooks"][0]["command"]
            self.assertIn(verb, cmd)
            self.assertIn("|| python ", cmd)         # Windows MS-Store-stub fallback


if __name__ == "__main__":
    unittest.main()
