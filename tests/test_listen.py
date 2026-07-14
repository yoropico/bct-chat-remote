#!/usr/bin/env python3
"""`listen` = one server-push turn: print a returned mention digest, stay silent
on the reconnect sentinel, surface errors. A held connection needs a longer
socket timeout than the default 10s."""
import contextlib, io, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class ListenTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})

    def run_main(self, argv):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.mod.main(argv)
        except SystemExit:
            pass
        return buf.getvalue()

    def test_prints_returned_mention(self):
        seen = {}
        def fake(cmd, args, timeout=10):
            seen["cmd"], seen["timeout"] = cmd, timeout
            return {"ok": True, "text": "yoros: @svr 봐줘"}
        self.mod.authed = fake
        out = self.run_main(["listen"])
        self.assertIn("yoros: @svr 봐줘", out)
        self.assertEqual(seen["cmd"], "chat-listen")
        self.assertGreaterEqual(seen["timeout"], 35)      # long hold tolerated

    def test_silent_on_reconnect_sentinel(self):
        self.mod.authed = lambda c, a, timeout=10: {"ok": True, "text": self.mod.NO_MENTION}
        self.assertEqual(self.run_main(["listen"]).strip(), "")

    def test_silent_on_no_new(self):
        self.mod.authed = lambda c, a, timeout=10: {"ok": True, "text": self.mod.NO_NEW}
        self.assertEqual(self.run_main(["listen"]).strip(), "")

    def test_rpc_accepts_timeout_kwarg(self):
        # connect/rpc must accept the kwarg without connecting (socket absent).
        self.mod.SOCK = os.path.join(self.home, "nope.sock")
        r = self.mod.rpc("chat-listen", [], IDENT, timeout=40)
        self.assertFalse(r.get("ok"))                     # no socket, but no TypeError
