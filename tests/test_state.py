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
from test_heartbeat_helpers import load_fresh_module, reaped_pid  # noqa: E402


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
        # A reaped child, not the constant 999999 — Linux's default pid_max (4194304)
        # makes 999999 an ordinary, possibly-live pid there (macOS caps at 99999).
        self.assertTrue(self.mod.proc_alive(os.getpid()))
        self.assertFalse(self.mod.proc_alive(reaped_pid()))
        self.assertFalse(self.mod.proc_alive(0))
        self.assertFalse(self.mod.proc_alive(-1))

    def test_proc_alive_is_false_past_pid_t_instead_of_raising(self):
        # os.kill() raises OverflowError past pid_t, and Windows' DWORD marshalling raises
        # ctypes.ArgumentError — NEITHER is an OSError, so neither of proc_alive()'s except
        # clauses would catch it. The escape route matters: gc_markers() calls proc_alive()
        # OUTSIDE the daemon's per-tick guard, so one tampered marker would kill the daemon
        # on every respawn and the host would go permanently deaf.
        for pid in (2 ** 31, 2 ** 32 - 1, 9999999999):
            self.assertFalse(self.mod.proc_alive(pid), pid)

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

    def test_proc_alive_windows_access_denied_means_alive(self):
        # A live process owned by another Windows session/user makes OpenProcess
        # return a NULL handle with GetLastError() == ERROR_ACCESS_DENIED (5) — that
        # process EXISTS. proc_alive() must not read this as "dead".
        self.assertTrue(self._proc_alive_with_fake_windows_api(last_error=5))

    def test_proc_alive_windows_invalid_parameter_means_dead(self):
        # ERROR_INVALID_PARAMETER (87): no process with this pid — genuinely dead.
        self.assertFalse(self._proc_alive_with_fake_windows_api(last_error=87))

    def _proc_alive_with_fake_windows_api(self, last_error):
        import ctypes

        class FakeFunc:
            """Stands in for a ctypes function pointer: accepts restype/argtypes
            assignment like the real thing, and always returns a NULL handle."""
            def __init__(self):
                self.restype = None
                self.argtypes = None

            def __call__(self, *args):
                return 0

        class FakeKernel32:
            def __init__(self):
                self.OpenProcess = FakeFunc()
                self.CloseHandle = FakeFunc()

        fake_kernel32 = FakeKernel32()
        real_name = os.name
        had_windll = hasattr(ctypes, "WinDLL")
        real_windll = getattr(ctypes, "WinDLL", None)
        had_gle = hasattr(ctypes, "get_last_error")
        real_gle = getattr(ctypes, "get_last_error", None)

        os.name = "nt"
        ctypes.WinDLL = lambda name, use_last_error=True: fake_kernel32
        ctypes.get_last_error = lambda: last_error
        try:
            return self.mod.proc_alive(12345)
        finally:
            os.name = real_name
            if had_windll:
                ctypes.WinDLL = real_windll
            else:
                del ctypes.WinDLL
            if had_gle:
                ctypes.get_last_error = real_gle
            else:
                del ctypes.get_last_error


if __name__ == "__main__":
    unittest.main()
