#!/usr/bin/env python3
"""State-dir writes must be atomic (a hook killed inside its budget must never
leave a 0-byte identity.json or a truncated stable copy), the state dir must be
overridable without touching HOME (Windows' expanduser ignores HOME), and process
liveness must never be probed with os.kill on Windows (it TERMINATES)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402


class StateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_state_dir_follows_bct_chat_home(self):
        self.assertEqual(self.mod.STATE_DIR, os.path.join(self.home, ".bct-chat"))

    def test_save_is_atomic_no_temp_left_behind(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "X", "name": "n"})
        self.assertEqual(self.mod.load(self.mod.IDENTITY)["participantID"], "X")
        leftovers = [f for f in os.listdir(self.mod.STATE_DIR) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_never_truncates_on_a_failed_write(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "GOOD", "name": "n"})

        class Unserializable:
            pass

        with self.assertRaises(TypeError):
            self.mod.save(self.mod.IDENTITY, {"bad": Unserializable()})
        # The old file survives intact — the doomed write never touched it.
        self.assertEqual(self.mod.load(self.mod.IDENTITY)["participantID"], "GOOD")

    def test_proc_alive_true_for_self_false_for_reaped_pid(self):
        self.assertTrue(self.mod.proc_alive(os.getpid()))
        self.assertFalse(self.mod.proc_alive(999999))
        self.assertFalse(self.mod.proc_alive(0))
        self.assertFalse(self.mod.proc_alive(-1))

    def test_proc_alive_does_not_use_os_kill_on_windows(self):
        # CPython's os.kill on Windows calls TerminateProcess for ANY signal.
        real_name = os.name
        killed = []
        real_kill = getattr(os, "kill", None)
        os.name = "nt"
        os.kill = lambda pid, sig: killed.append(pid)
        try:
            self.mod.proc_alive(os.getpid())
        finally:
            os.name = real_name
            if real_kill is not None:
                os.kill = real_kill
        self.assertEqual(killed, [], "proc_alive called os.kill under os.name='nt'")


if __name__ == "__main__":
    unittest.main()
