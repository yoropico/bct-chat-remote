#!/usr/bin/env python3
"""Membership must be self-healing but never a nag: a BCT restart gets us back in
without a human, three refusals stop the asking for good, and `leave` stays left."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


class FastClock:
    """A `time` stand-in whose sleeps really pass: do_join()'s 5-minute wait must be able to
    reach its deadline in a test without the test taking 5 minutes. A CONSTANT clock would
    hang the loop forever, which is the trap this class exists to avoid."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 1.0)

    def time_ns(self):
        return int(self.now * 1e9)


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.sock_available = lambda: True
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def rpc_map(self, responses):
        def fake(cmd, args, pane_id="", timeout=10):
            self.calls.append((cmd, args))
            r = responses[cmd]
            return r(args) if callable(r) else r
        self.mod.rpc = fake

    def test_ensure_membership_requests_a_join_when_unseated(self):
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-1"}})
        self.assertTrue(self.mod.ensure_membership())
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-1")
        self.assertGreater(self.mod.load(self.mod.PENDING)["requestedAt"], 0)

    def test_ensure_membership_polls_an_outstanding_request_instead_of_re_requesting(self):
        # D5: a second chat-join orphans the approval the user is about to grant.
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time()})
        self.rpc_map({"chat-join-poll": {"ok": False, "error": "pending"},
                      "chat-join": {"ok": True, "text": "REQ-2"}})
        self.mod.ensure_membership()
        self.assertEqual([c for c, _ in self.calls], ["chat-join-poll"])

    def test_an_approval_seats_us_and_wipes_the_budget(self):
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time()})
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 2, "nextAttemptAt": 0,
                                            "suspended": False, "lastOutcome": "denied"})
        self.rpc_map({"chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        self.assertTrue(self.mod.claim_pending())
        self.assertEqual(self.mod.identity(), IDENT)
        self.assertIsNone(self.mod.load(self.mod.PENDING))
        self.assertIsNone(self.mod.load(self.mod.JOIN_STATE))

    def test_a_stale_pending_is_discarded_even_if_the_poll_is_unrecognised(self):
        # D6: BCT restarted and no longer knows the requestID — the old code wedged
        # pending-join.json forever and the rejoin branch became unreachable.
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time() - 700})
        self.rpc_map({"chat-join-poll": {"ok": False, "error": "unknown request"},
                      "chat-join": {"ok": True, "text": "REQ-2"}})
        self.mod.ensure_membership()
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-2")

    def test_three_refusals_suspend_the_asking_for_good(self):
        # D9: the flat cooldown re-asked every 30 min for the daemon's whole life.
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ"},
                      "chat-join-poll": {"ok": False, "error": "denied"}})
        for expected in (60, 300, 1800):
            self.assertTrue(self.mod.ensure_membership())      # requests
            self.mod.claim_pending()                            # -> denied
            st = self.mod.join_state()
            self.assertAlmostEqual(st["nextAttemptAt"] - time.time(), expected, delta=5)
            st["nextAttemptAt"] = 0                             # fast-forward the backoff
            self.mod.save(self.mod.JOIN_STATE, st)
        self.assertTrue(self.mod.join_state()["suspended"])
        joins = len([c for c, _ in self.calls if c == "chat-join"])
        self.assertFalse(self.mod.ensure_membership())
        self.assertEqual(len([c for c, _ in self.calls if c == "chat-join"]), joins)

    def test_a_manual_join_clears_a_suspension(self):
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": time.time() + 9999,
                                            "suspended": True, "lastOutcome": "denied"})
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-9"},
                      "chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        self.mod.clear_join_state()                 # what the `join` verb does first
        self.mod.do_join("svr", wait_approval=False)
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-9")

    def test_a_stale_identity_is_not_mistaken_for_an_approval(self):
        # Found live on a Windows remote whose identity had gone stale across a BCT restart.
        # do_join() decided success by "do we have an identity", and the stale one satisfied
        # that on the first poll — so it announced 입장 승인됨 for a request the user had not
        # even looked at yet, and every verb afterwards kept failing with NOT_INVITED.
        # "Did the identity CHANGE" would not have caught it either: BCT's reseat deliberately
        # hands back the SAME participantID (that is how it preserves the unread cursor).
        self.mod.save(self.mod.IDENTITY, {"participantID": "STALE", "name": "svr"})
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-1"},
                      "chat-join-poll": {"ok": False, "error": "pending"}})
        self.mod.time = FastClock()                  # the 5-minute wait, without the wait
        with self.assertRaises(SystemExit):          # times out waiting, never claims success
            self.mod.do_join("svr", wait_approval=True)
        self.assertEqual(self.mod.identity(), "STALE")   # untouched — we never seated

    def test_an_approval_the_daemon_claimed_first_is_still_a_success(self):
        # The other side of the same coin: the daemon polls the same request. If it claims the
        # approval first, PENDING is gone and the budget is clear — that is success, not a
        # denial, and do_join() must not report "denied or expired".
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time()})

        def poll(args):
            self.mod.forget(self.mod.PENDING)                       # the daemon got there first
            self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
            return {"ok": False, "error": "unknown request"}

        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-1"}, "chat-join-poll": poll})
        self.mod.time = FastClock()
        self.mod.do_join("svr", wait_approval=True)                 # must not raise
        self.assertEqual(self.mod.identity(), IDENT)

    def test_authed_re_joins_once_on_not_invited(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "DEAD", "name": "svr"})
        seq = {"n": 0}

        def send(args):
            seq["n"] += 1
            return {"ok": False, "error": NOT_INVITED} if seq["n"] == 1 else {"ok": True, "text": ""}

        self.rpc_map({"chat-send": send,
                      "chat-join": {"ok": True, "text": "REQ-1"},
                      "chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        r = self.mod.authed("chat-send", ["hi"])
        self.assertTrue(r["ok"])
        self.assertEqual(self.mod.identity(), IDENT)
        self.assertEqual(len([c for c, _ in self.calls if c == "chat-join"]), 1)

    def test_authed_surfaces_not_invited_while_suspended(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "DEAD", "name": "svr"})
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": 0,
                                            "suspended": True, "lastOutcome": "denied"})
        self.rpc_map({"chat-send": {"ok": False, "error": NOT_INVITED}})
        r = self.mod.authed("chat-send", ["hi"])
        self.assertEqual(r["error"], NOT_INVITED)
        self.assertEqual([c for c, _ in self.calls], ["chat-send"])

    def test_leave_suspends_and_keeps_the_markers(self):
        # D8: the old leave dropped the identity but left the daemon running, which
        # re-requested membership four minutes later — in the room the user just left.
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.mark_session("sess-1")
        self.rpc_map({"chat-leave": {"ok": True, "text": ""}})
        self.mod.do_leave()
        self.assertEqual(self.mod.identity(), "")
        self.assertTrue(self.mod.join_state()["suspended"])
        self.assertEqual(self.mod.live_sessions(), ["sess-1"])
        self.assertFalse(self.mod.ensure_membership())


if __name__ == "__main__":
    unittest.main()
