#!/usr/bin/env python3
"""session-start must self-install/refresh ~/.bct-chat/bct-chat.py (stable copy)."""
import os, shutil, subprocess, sys, tempfile, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "scripts", "bct-chat.py")


def run_session_start(client_path, home):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
    return subprocess.run([sys.executable, client_path, "session-start"],
                          env=env, capture_output=True, text=True, timeout=30)


class StableCopyTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.stable = os.path.join(self.home, ".bct-chat", "bct-chat.py")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_installs_copy_when_missing(self):
        r = run_session_start(CLIENT, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.stable))
        with open(self.stable, encoding="utf-8") as f, open(CLIENT, encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())

    def test_refreshes_stale_copy(self):
        os.makedirs(os.path.dirname(self.stable))
        with open(self.stable, "w", encoding="utf-8") as f:
            f.write("# stale\n")
        r = run_session_start(CLIENT, self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.stable, encoding="utf-8") as f, open(CLIENT, encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())

    def test_running_the_stable_copy_is_a_noop(self):
        run_session_start(CLIENT, self.home)          # install first
        before = os.stat(self.stable).st_mtime_ns
        r = run_session_start(self.stable, self.home)  # run the copy itself
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.stat(self.stable).st_mtime_ns, before)

    def test_survives_non_utf8_locale(self):
        # Korean Windows defaults open() to cp949; the client source is UTF-8.
        # PYTHONCOERCECLOCALE=0 + LC_ALL=C pins an ASCII locale to reproduce.
        env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
        env.update(HOME=self.home, BCT_CHAT_HOME=os.path.join(self.home, ".bct-chat"),
                   LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        r = subprocess.run([sys.executable, "-X", "utf8=0", CLIENT, "session-start"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.stable, "rb") as f, open(CLIENT, "rb") as g:
            self.assertEqual(f.read(), g.read())

    def test_bct_pane_guard_skips_copy(self):
        env = dict(os.environ, HOME=self.home,
                   BCT_CHAT_HOME=os.path.join(self.home, ".bct-chat"), BCT_PANE_ID="deadbeef")
        r = subprocess.run([sys.executable, CLIENT, "session-start"],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.stable))


if __name__ == "__main__":
    unittest.main()
