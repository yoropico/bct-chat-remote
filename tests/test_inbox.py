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

    def test_claim_after_the_only_item_is_already_claimed_is_none(self):
        # Two SEQUENTIAL claims on a one-item inbox: the second call just finds an
        # empty inbox. This does not exercise arbitration at all — see
        # test_inbox_claim_is_mutually_exclusive_under_thread_contention below for
        # the real concurrency guarantee this test's old name promised.
        self.mod.inbox_put("only-one", "svr")
        a = self.mod.inbox_claim()
        b = self.mod.inbox_claim()
        self.assertIsNotNone(a)
        self.assertIsNone(b)

    def test_inbox_claim_is_mutually_exclusive_under_thread_contention(self):
        # Two threads in ONE process, released at the same instant onto the same
        # one-item inbox, pin the loser's arbitration path: os.rename raises for
        # whichever thread loses the race, and it correctly walks away with None.
        # CPython releases the GIL around the rename(2) syscall itself, so these two
        # threads' renames really do reach the kernel concurrently, arbitrated by
        # the same VFS directory lock a cross-process race would hit — this is a
        # real and worthwhile contract. It is still not a full stand-in for the
        # cross-PROCESS case the module's design ultimately rests on, though: the
        # two threads here share one process's fd table and cwd, which two
        # independent hook processes never do. Not flaky — os.rename's exclusivity
        # on a shared source either holds every time or the module's whole design is
        # broken, so a single trial suffices.
        import threading
        self.mod.inbox_put("only-one", "svr")
        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(i):
            barrier.wait()
            results[i] = self.mod.inbox_claim()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len([r for r in results if r is not None]), 1)
        self.assertEqual(len([r for r in results if r is None]), 1)

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

    def test_cap_eviction_counts_only_what_it_actually_removed(self):
        # Force the "inbox_claim() already won the race" outcome for every eviction
        # candidate: _evict() reports nothing was actually removed. Against a naive
        # `_bump_dropped(excess)` (counting the cap overflow regardless of what
        # _evict() actually removed) this would still report drops that never
        # happened; the fix bumps by the number _evict() actually removed.
        self.mod.INBOX_CAP = 3
        self.mod._evict = lambda path: False
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
            time.sleep(0.002)
        self.assertEqual(self.mod.take_dropped(), 0)

    def test_dropped_counter_survives_a_bump_take_interleaving(self):
        # Regression guard for the os.rename-steal design shared by _bump_dropped()
        # and take_dropped(): a naive load()+save() bump lets a concurrent
        # take_dropped() steal-and-clear the pre-bump value while the bump still
        # holds a stale copy of it, so the bump's save() resurrects a value already
        # handed to the reader — a double count. Pin the interleaving
        # deterministically by hooking load() to fire a nested take_dropped() at
        # the exact moment _bump_dropped() reads the value it is about to add to.
        self.mod._bump_dropped(3)
        taken = []
        fired = []
        real_load = self.mod.load

        def hook(path):
            result = real_load(path)   # captured BEFORE the nested take can act
            if not fired:
                fired.append(True)
                taken.append(self.mod.take_dropped())   # the "wrong moment"
            return result

        self.mod.load = hook
        try:
            self.mod._bump_dropped(5)
        finally:
            self.mod.load = real_load
        self.assertTrue(fired, "the interleaving was never injected")
        taken.append(self.mod.take_dropped())
        self.assertEqual(sum(taken), 3 + 5)

    def test_a_stolen_sidecar_survives_a_concurrent_sweep(self):
        # Regression guard for the rename-preserves-mtime hazard: .claim/.bump
        # sidecars are born via os.rename(DROPPED, claim), not a write, so they
        # inherit dropped.json's mtime rather than getting a fresh one. If
        # dropped.json sat untouched for a long time before the steal, the sidecar
        # is already older than ORPHAN_AGE the instant it's created. What keeps a
        # recover_orphans() sweep running concurrently, in the window between the
        # steal and its resolution, from deleting it out from under the stealer is
        # _sweep_sidecars()'s pid guard (see test_sweep_skips_a_sidecar_whose_owner_
        # pid_is_still_alive for that guard pinned directly) — take_dropped() is
        # still alive and its own pid is in the sidecar's name, so the sweep must
        # skip it no matter how stale its mtime reads. Pin the interleaving
        # deterministically by hooking load() to run a sweep at the moment
        # take_dropped() is mid-steal.
        self.mod._bump_dropped(7)
        old = time.time() - self.mod.ORPHAN_AGE - 500
        os.utime(self.mod.DROPPED, (old, old))  # simulate dropped.json sitting untouched

        state_dir = os.path.dirname(self.mod.DROPPED)
        swept = []
        fired = []
        real_load = self.mod.load

        def hook(path):
            result = real_load(path)
            if not fired:
                fired.append(True)
                before = set(os.listdir(state_dir))
                self.mod.recover_orphans()          # the concurrent sweep
                after = set(os.listdir(state_dir))
                swept.extend(before - after)
            return result

        self.mod.load = hook
        try:
            n = self.mod.take_dropped()
        finally:
            self.mod.load = real_load

        self.assertTrue(fired, "the interleaving was never injected")
        self.assertEqual(swept, [], "the live sidecar was swept out from under the steal")
        self.assertEqual(n, 7)

    def test_sweep_skips_a_sidecar_whose_owner_pid_is_still_alive(self):
        # Pins the pid guard directly, independent of any timing/injection trickery:
        # a sidecar named with a LIVE pid must survive the sweep no matter how old
        # its mtime is, and one named with a pid nothing holds must not.
        os.makedirs(self.mod.INBOX_DIR, exist_ok=True)
        live = os.path.join(self.mod.INBOX_DIR, f"item.json.{os.getpid()}.claim")
        dead = os.path.join(self.mod.INBOX_DIR, "item.json.999999.claim")
        open(live, "w").close()
        open(dead, "w").close()
        old = time.time() - self.mod.ORPHAN_AGE - 500
        os.utime(live, (old, old))
        os.utime(dead, (old, old))

        self.mod.recover_orphans()

        self.assertTrue(os.path.exists(live),
                         "a live owner's sidecar must survive the sweep regardless of mtime")
        self.assertFalse(os.path.exists(dead),
                          "a dead owner's ancient sidecar must be swept")

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
