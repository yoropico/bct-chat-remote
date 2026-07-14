#!/usr/bin/env python3
"""The inbox is the durability boundary: the daemon only advances BCT's cursor after
an item is on local disk, and exactly one hook may ever claim a given item."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_put_then_claim_round_trips_the_text(self):
        self.mod.inbox_put("yoros: @svr 봐줘", "svr")
        path, item = self.mod.inbox_claim()
        self.assertEqual(item["text"], "yoros: @svr 봐줘")
        self.assertEqual(item["name"], "svr")
        self.assertGreater(item["capturedAt"], 0)
        self.assertTrue(os.path.exists(path))                 # in processing/, not gone
        self.assertEqual(os.listdir(self.mod.INBOX_DIR), [])  # claimed out of the inbox
        self.mod.inbox_ack(path)
        self.assertFalse(os.path.exists(path))

    def test_claim_on_an_empty_inbox_is_none(self):
        self.assertIsNone(self.mod.inbox_claim())

    def test_claim_is_fifo(self):
        self.mod.inbox_put("first", "svr")
        time.sleep(0.01)
        self.mod.inbox_put("second", "svr")
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "first")
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "second")

    def test_two_claimers_never_get_the_same_item(self):
        # Two sessions' Stop hooks fire at once. The loser's rename raises; it must
        # take the NEXT item or none — never a duplicate of the winner's.
        self.mod.inbox_put("only-one", "svr")
        a = self.mod.inbox_claim()
        b = self.mod.inbox_claim()
        self.assertIsNotNone(a)
        self.assertIsNone(b)

    def test_a_corrupt_item_is_dropped_not_returned(self):
        os.makedirs(self.mod.INBOX_DIR, exist_ok=True)
        with open(os.path.join(self.mod.INBOX_DIR, "1-1.json"), "w") as f:
            f.write("{not json")
        self.mod.inbox_put("good", "svr")
        path, item = self.mod.inbox_claim()
        self.assertEqual(item["text"], "good")

    def test_orphan_recovery_returns_a_dead_hooks_item(self):
        self.mod.inbox_put("orphaned", "svr")
        path, _ = self.mod.inbox_claim()                 # the hook then dies, never acks
        os.utime(path, (time.time() - 300, time.time() - 300))
        self.assertEqual(self.mod.recover_orphans(), 1)
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "orphaned")

    def test_orphan_recovery_leaves_a_fresh_claim_alone(self):
        self.mod.inbox_put("in-flight", "svr")
        self.mod.inbox_claim()
        self.assertEqual(self.mod.recover_orphans(), 0)

    def test_cap_drops_the_oldest_and_counts_it(self):
        self.mod.INBOX_CAP = 3
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
            time.sleep(0.002)
        texts = []
        while True:
            got = self.mod.inbox_claim()
            if not got:
                break
            texts.append(got[1]["text"])
        self.assertEqual(texts, ["m2", "m3", "m4"])
        self.assertEqual(self.mod.take_dropped(), 2)
        self.assertEqual(self.mod.take_dropped(), 0)      # read-and-clear

    def test_wait_returns_as_soon_as_an_item_lands(self):
        import threading
        threading.Timer(0.2, lambda: self.mod.inbox_put("late", "svr")).start()
        started = time.time()
        got = self.mod.inbox_wait(5, poll=0.05)
        self.assertEqual(got[1]["text"], "late")
        self.assertLess(time.time() - started, 3)

    def test_wait_times_out_to_none(self):
        started = time.time()
        self.assertIsNone(self.mod.inbox_wait(0.3, poll=0.05))
        self.assertGreaterEqual(time.time() - started, 0.3)

    def test_put_is_atomic_no_partial_file_visible(self):
        self.mod.inbox_put("x", "svr")
        names = os.listdir(self.mod.INBOX_DIR)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".json"))
        self.assertFalse(any(n.endswith(".tmp") for n in names))


if __name__ == "__main__":
    unittest.main()
