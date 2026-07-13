#!/usr/bin/env python3
"""SessionStart drops a marker (the daemon's refcount) and SessionEnd removes it.
The session id comes from the hook's JSON payload on stdin; an interactive run has
none and must not leave markers behind."""
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer  # noqa: E402


def run_hook(verb, home, sock_spec, payload):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT, verb], env=env, input=payload,
                          capture_output=True, text=True, timeout=30)


class SessionMarkerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(self.state)
        with open(os.path.join(self.state, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"participantID": "C1A6063F-0124-4229-9CE3-D757348A70F2", "name": "HOST"}, f)
        self.marker = os.path.join(self.state, "sessions", "sess-abc")
        self.heartbeat_pidfile = os.path.join(self.state, "heartbeat.pid")

    def tearDown(self):
        self._reap_heartbeat_daemon()
        shutil.rmtree(self.home, ignore_errors=True)

    def _reap_heartbeat_daemon(self):
        """session-start may have spawned a live, detached heartbeat daemon (its
        default interval is 240s, so it won't exit on its own for the life of the
        test run). Kill it here — test-only cleanup on macOS, not the shipped
        client, so os.kill(pid, 0) is fine to use as a liveness probe."""
        try:
            with open(self.heartbeat_pidfile, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return                  # already dead
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return              # exited
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def test_session_start_marks_and_session_end_unmarks(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        payload = json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionStart"})
        try:
            r = run_hook("session-start", self.home, f"tcp:127.0.0.1:{srv.port}", payload)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(self.marker))

            r = run_hook("session-end", self.home, f"tcp:127.0.0.1:{srv.port}",
                         json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionEnd"}))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(self.marker))
        finally:
            srv.close()

    def test_interactive_run_leaves_no_marker(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            r = run_hook("session-start", self.home, f"tcp:127.0.0.1:{srv.port}", "")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.isdir(os.path.join(self.state, "sessions")))
        finally:
            srv.close()

    def test_session_end_without_a_socket_is_a_silent_noop(self):
        os.makedirs(os.path.dirname(self.marker), exist_ok=True)
        open(self.marker, "w").close()
        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_mark_session_precedes_spawn_heartbeat_and_the_daemon_survives(self):
        """Guards the ordering invariant: mark_session() must run before
        spawn_heartbeat(), because the daemon's very first loop iteration checks
        live_sessions() before it does anything else — a daemon spawned before its
        own marker exists sees an empty session set and exits (almost) instantly
        instead of sticking around to heartbeat.

        An end-to-end subprocess-timing test cannot reliably catch a two-line
        reorder here: interpreter start-up for the child (tens of ms) dwarfs the
        gap between the two calls in the parent (microseconds), so the marker
        routinely gets written before the mis-ordered child even reaches its
        first check — a flaky, not-actually-verifying test. So this test drives
        session_start() in-process (like test_heartbeat.load_fresh_module) and
        (a) asserts the call order directly via monkeypatched wrappers — this is
        what deterministically fails under either mutation — while (b) each
        wrapper still calls straight through to the REAL implementation, so
        spawn_heartbeat() performs a genuine subprocess.Popen and we can also
        confirm a live daemon actually resulted, not just a recorded call."""
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        old_home = os.environ.get("HOME")
        old_sock = os.environ.get("BCT_CHAT_SOCK")
        old_pane = os.environ.pop("BCT_PANE_ID", None)
        os.environ["HOME"] = self.home
        os.environ["BCT_CHAT_SOCK"] = f"tcp:127.0.0.1:{srv.port}"
        try:
            spec = importlib.util.spec_from_file_location(f"bct_chat_{id(self)}", CLIENT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            order = []
            real_mark, real_spawn = mod.mark_session, mod.spawn_heartbeat

            def tracked_mark(sid):
                order.append("mark")
                real_mark(sid)

            def tracked_spawn():
                order.append("spawn")
                real_spawn()

            mod.mark_session = tracked_mark
            mod.spawn_heartbeat = tracked_spawn
            mod.hook_session_id = lambda: "sess-abc"

            mod.session_start()

            self.assertEqual(order, ["mark", "spawn"],
                              "mark_session()/spawn_heartbeat() call order regressed")

            pid = None
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    with open(self.heartbeat_pidfile, encoding="utf-8") as f:
                        pid = int(f.read().strip())
                    break
                except (OSError, ValueError):
                    time.sleep(0.05)
            self.assertIsNotNone(pid, "no heartbeat daemon was spawned")

            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                self.fail("heartbeat daemon exited almost instantly — spawned "
                          "before its session marker existed?")
        finally:
            srv.close()
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_sock is None:
                os.environ.pop("BCT_CHAT_SOCK", None)
            else:
                os.environ["BCT_CHAT_SOCK"] = old_sock
            if old_pane is not None:
                os.environ["BCT_PANE_ID"] = old_pane


if __name__ == "__main__":
    unittest.main()
