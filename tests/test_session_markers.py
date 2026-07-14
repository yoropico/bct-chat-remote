#!/usr/bin/env python3
"""SessionStart drops a marker (the daemon's refcount) and makes sure a daemon is running;
SessionEnd removes the marker. The hook itself touches NO socket — capturing the room is
the daemon's job, and a hook that RPC'd could be killed mid-call with no ack verb to
recover what it had already consumed. The session id comes from the hook's JSON payload on
stdin; an interactive run has none and must not leave markers behind."""
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
from test_heartbeat_helpers import CLIENT, load_fresh_module, reap_daemon, wait_for  # noqa: E402


class FakeSub:
    """subprocess stand-in: records the daemon spawn instead of performing it."""
    DEVNULL = -3
    spawned = []

    @staticmethod
    def Popen(argv, **kwargs):
        FakeSub.spawned.append(argv)


def run_hook(verb, home, sock_spec, payload):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
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
        self.pidfile = os.path.join(self.state, "heartbeat.pid")
        FakeSub.spawned = []

    def tearDown(self):
        reap_daemon(self.pidfile)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_session_start_marks_and_session_end_unmarks(self):
        payload = json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionStart"})
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1", payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.marker))

        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc", "hook_event_name": "SessionEnd"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_the_marker_carries_a_pid_and_a_start_time(self):
        """The marker is the daemon's refcount AND the evidence its session is still
        alive: gc_markers() collects one whose pid is dead (a crashed claude used to keep
        a phantom host in the room), and falls back to the mtime where no pid could be
        resolved (Windows)."""
        payload = json.dumps({"session_id": "sess-abc"})
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1", payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.marker, encoding="utf-8") as f:
            obj = json.load(f)
        self.assertIn("pid", obj)
        self.assertIsInstance(obj["pid"], int)
        self.assertGreater(obj["startedAt"], 0)

    def test_session_start_does_no_rpc(self):
        mod = load_fresh_module(self.home)
        mod.rpc = lambda *a, **k: self.fail("SessionStart must not touch the socket")
        mod.sock_available = lambda: self.fail("SessionStart must not touch the socket")
        mod.subprocess = FakeSub
        mod.hook_payload = lambda: {"session_id": "sess-abc"}
        mod.session_start()
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(len(FakeSub.spawned), 1, "SessionStart did not ensure a daemon")

    def test_mark_session_precedes_the_daemon_spawn(self):
        """Ordering invariant: mark_session() must run before ensure_daemon(), because the
        daemon's very first loop pass checks live_sessions() before it does anything else —
        a daemon spawned ahead of its own marker sees an empty set and exits instantly,
        leaving the session with no ear.

        An end-to-end subprocess-timing test cannot catch a two-line reorder here (child
        interpreter start-up dwarfs the microseconds between the two calls), so this drives
        session_start() in-process and asserts the call order directly."""
        mod = load_fresh_module(self.home)
        order = []
        real_mark = mod.mark_session

        def tracked_mark(sid):
            order.append("mark")
            real_mark(sid)

        mod.mark_session = tracked_mark
        mod.ensure_daemon = lambda: order.append("spawn")
        mod.hook_payload = lambda: {"session_id": "sess-abc"}
        mod.session_start()
        self.assertEqual(order, ["mark", "spawn"],
                         "mark_session()/ensure_daemon() call order regressed")

    def test_session_start_spawns_a_daemon_that_stays_up(self):
        """The real spawn, end to end: a detached daemon must actually result, and it must
        still be there a moment later — with no session marker it would exit instantly, and
        with the tunnel down it must WAIT rather than die (the old two-strike suicide)."""
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1",   # nothing listening
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(wait_for(lambda: os.path.exists(self.pidfile)),
                        "session-start spawned no daemon")
        with open(self.pidfile, encoding="utf-8") as f:
            pid = int(f.read().strip())
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            self.fail("the daemon exited on a dead tunnel instead of backing off")

    def test_interactive_run_leaves_no_marker(self):
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.state, "sessions")))
        self.assertFalse(os.path.exists(self.pidfile))

    def test_session_end_without_a_socket_is_a_silent_noop(self):
        os.makedirs(os.path.dirname(self.marker), exist_ok=True)
        open(self.marker, "w").close()
        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_session_end_survives_non_object_json_payload(self):
        """A non-object top-level payload (a list, a bare string) makes
        hook_session_id()'s old `.get("session_id", "")` raise AttributeError —
        uncaught by the narrow `except (OSError, ValueError)` — which used to
        escape session_end() and hand hooks.json's `||` fallback a nonzero exit.
        The hook verb must exit 0 regardless of what shape stdin holds."""
        for payload in ("[1, 2]", '"hi"', "null", "42"):
            with self.subTest(payload=payload):
                r = run_hook("session-end", self.home, "tcp:127.0.0.1:1", payload)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_session_start_survives_a_malformed_pending_join(self):
        """A pending-join.json missing "requestID" used to make session_start()'s chained
        claim_pending() raise KeyError (not a SystemExit — the old narrow
        `except SystemExit` let it escape and exit 1). SessionStart no longer touches
        membership at all, so this payload can't reach it; the hook must still exit 0 and
        leave the file alone for the daemon."""
        pending = os.path.join(self.state, "pending-join.json")
        with open(pending, "w", encoding="utf-8") as f:
            json.dump({"name": "HOST"}, f)          # no "requestID"
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(pending))

    def test_session_start_survives_traversal_shaped_session_id(self):
        """session_id values shaped like a path (containing "/") or exactly ".."
        used to reach mark_session() unsanitized, which joins them onto
        SESSIONS_DIR and opens the result for writing — raising
        FileNotFoundError ("foo/bar": no such intermediate directory "foo") or
        IsADirectoryError (".." resolves back to the state dir itself). Both used
        to exit 1. After the fix, the hook must exit 0 either way. "foo/bar" is
        sanitized down to the bare "bar" by os.path.basename() — a safe,
        contained id (no directory named "foo" is ever created) — so it is
        still accepted and marked normally. "..", after basename()ing, is
        exactly ".." — explicitly rejected by the check — so it is treated as an
        absent id: no marker, no sessions/ dir, no daemon."""
        r = run_hook("session-start", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "foo/bar"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isdir(os.path.join(self.state, "sessions", "foo")),
                         '"foo/bar" must never create a "foo" directory')
        self.assertTrue(os.path.exists(os.path.join(self.state, "sessions", "bar")),
                        '"foo/bar" should sanitize down to a contained marker "bar"')
        reap_daemon(self.pidfile)   # "bar" is a valid id, so this spawned a daemon too

        # Fresh HOME for the ".." case — the part above already legitimately spawned and
        # reaped a daemon under self.home, and a reaped (SIGTERM'd, no signal handler)
        # daemon does NOT clean up its own pidfile on the way out, so re-using self.home
        # here would false-negative on a stale leftover file.
        dotdot_home = tempfile.mkdtemp()
        dotdot_state = os.path.join(dotdot_home, ".bct-chat")
        os.makedirs(dotdot_state)
        dotdot_pidfile = os.path.join(dotdot_state, "heartbeat.pid")
        try:
            r = run_hook("session-start", dotdot_home, "tcp:127.0.0.1:1",
                         json.dumps({"session_id": ".."}))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.isdir(os.path.join(dotdot_state, "sessions")),
                             'session_id=".." should be treated as absent — no sessions/ dir')
            self.assertFalse(os.path.exists(dotdot_pidfile),
                             'session_id=".." should not have spawned a daemon')
        finally:
            reap_daemon(dotdot_pidfile)
            shutil.rmtree(dotdot_home, ignore_errors=True)

    def test_session_end_traversal_session_id_cannot_delete_outside_sessions_dir(self):
        """session_id="../identity.json" must not let session-end delete
        ~/.bct-chat/identity.json. Unsanitized, unmark_session() joins it onto
        SESSIONS_DIR (.../sessions/../identity.json), which normalizes right back
        to the real identity file and removes it. os.path.basename() must strip
        the traversal so at worst a same-named file *inside* sessions/ is
        touched — never anything outside it."""
        identity_path = os.path.join(self.state, "identity.json")
        self.assertTrue(os.path.exists(identity_path))     # written by setUp
        # A prior session-start already created sessions/ — the traversal needs a
        # real directory to walk through and back out of; without it the OS can't
        # even resolve the ".." component and the exploit doesn't reproduce.
        os.makedirs(os.path.join(self.state, "sessions"), exist_ok=True)
        payload = json.dumps({"session_id": "../identity.json"})
        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1", payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(identity_path),
                        "traversal-shaped session_id deleted a file outside sessions/")

    def test_reap_falls_back_when_sigkill_is_unavailable(self):
        """signal.SIGKILL does not exist as a module attribute on Windows — the
        reaper's fallback kill must not reference it directly (use
        getattr(signal, "SIGKILL", signal.SIGTERM) or equivalent), or this
        (portable, test-only) cleanup helper itself breaks the suite on a
        first-class host. Simulate the missing attribute here and force the
        SIGKILL-fallback branch by spawning a child that ignores SIGTERM."""
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
        os.makedirs(self.state, exist_ok=True)
        with open(self.pidfile, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
        had_sigkill = hasattr(signal, "SIGKILL")
        real_sigkill = getattr(signal, "SIGKILL", None)
        if had_sigkill:
            del signal.SIGKILL
        try:
            reap_daemon(self.pidfile)               # must not raise AttributeError
        finally:
            if had_sigkill:
                signal.SIGKILL = real_sigkill
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_a_bct_pane_hook_is_a_noop(self):
        env = dict(os.environ, HOME=self.home, BCT_CHAT_HOME=self.state,
                   BCT_PANE_ID="deadbeef", BCT_CHAT_SOCK="tcp:127.0.0.1:1")
        r = subprocess.run([sys.executable, CLIENT, "session-start"], env=env,
                           input=json.dumps({"session_id": "sess-abc"}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.marker))
        self.assertFalse(os.path.exists(self.pidfile))

    def test_a_bct_pane_session_end_is_a_noop(self):
        """SPEC §5 claims every hook verb is a complete no-op inside a BCT pane —
        session_end() must not touch a marker there either, even though BCT's native
        push, not this hook, owns delivery for that pane."""
        os.makedirs(os.path.dirname(self.marker), exist_ok=True)
        open(self.marker, "w").close()
        env = dict(os.environ, HOME=self.home, BCT_CHAT_HOME=self.state,
                   BCT_PANE_ID="deadbeef", BCT_CHAT_SOCK="tcp:127.0.0.1:1")
        r = subprocess.run([sys.executable, CLIENT, "session-end"], env=env,
                           input=json.dumps({"session_id": "sess-abc"}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.marker), "a BCT pane must not drop the marker")


class ClaudePidTests(unittest.TestCase):
    """claude_pid() resolves the session's claude as the hook's GRANDPARENT (hooks.json's
    `||` forces exactly one `sh -c` layer, and that shell dies with the hook). A wrong
    answer is not symmetric: a pid that is not this session's claude makes gc_markers()
    collect a LIVE session's marker the moment that stranger exits — live_sessions()
    empties, the daemon exits, and nothing re-creates the marker, so that session is deaf.
    A 0 only costs a marker that ages out on MARKER_TTL. So it must err toward 0."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.marker = os.path.join(self.home, ".bct-chat", "sessions", "sess-abc")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def install_ps(self, ppid_out, comm_out="", exc=None):
        """`ps` stand-in. Answers by requested format: "ppid=" for the ancestor walk,
        "comm=" for the plausibility check. `exc` simulates a host with no ps at all."""
        calls = []

        class FakeSub:
            DEVNULL = -3

            @staticmethod
            def run(argv, **kwargs):
                calls.append(argv)
                if exc:
                    raise exc
                fmt = argv[argv.index("-o") + 1]
                out = ppid_out if fmt.startswith("ppid") else comm_out
                return type("R", (), {"stdout": out, "returncode": 0 if out else 1})()

        self.mod.subprocess = FakeSub
        return calls

    def test_a_live_claude_ancestor_is_trusted(self):
        self.install_ps(f"{os.getpid()}\n", "/usr/local/bin/node\n")
        self.assertEqual(self.mod.claude_pid(), os.getpid())

    def test_an_implausible_ancestor_is_refused(self):
        # A short-lived wrapper is alive at the moment we ask, so "still exists" was never
        # the safety net the docstring claimed. The command has to look like claude too.
        self.install_ps(f"{os.getpid()}\n", "sh\n")
        self.assertEqual(self.mod.claude_pid(), 0)

    def test_a_dead_ancestor_is_refused(self):
        self.install_ps("999999\n", "claude\n")
        self.assertEqual(self.mod.claude_pid(), 0)

    def test_a_host_without_ps_resolves_no_pid(self):
        self.install_ps("", exc=FileNotFoundError("no ps"))
        self.assertEqual(self.mod.claude_pid(), 0)

    def test_windows_resolves_no_pid_and_never_spawns_ps(self):
        class WindowsOs:
            name = "nt"

            def __getattr__(self, k):
                return getattr(os, k)

        calls = self.install_ps(f"{os.getpid()}\n", "node\n")
        self.mod.os = WindowsOs()
        try:
            self.assertEqual(self.mod.claude_pid(), 0)
        finally:
            self.mod.os = os
        self.assertEqual(calls, [], "there is no cheap ancestor walk on Windows — don't try")

    def test_a_marker_with_no_resolved_pid_survives_until_its_ttl(self):
        """Erring toward 0 is only safe because gc_markers() then falls back to the TTL. If
        a pid-0 marker were collected on sight, claude_pid()'s own conservatism would cost
        the session the very ear it is protecting."""
        self.mod.claude_pid = lambda: 0
        self.mod.mark_session("sess-abc")
        with open(self.marker, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pid"], 0)
        self.assertEqual(self.mod.gc_markers(), 0)
        self.assertIn("sess-abc", self.mod.live_sessions())
        old = time.time() - self.mod.MARKER_TTL - 1
        os.utime(self.marker, (old, old))
        self.assertEqual(self.mod.gc_markers(), 1)


if __name__ == "__main__":
    unittest.main()
