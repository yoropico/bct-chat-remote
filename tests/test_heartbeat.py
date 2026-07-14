#!/usr/bin/env python3
"""The daemon proves the host is alive so BCT's 10-min prune cannot evict it while
its claude sessions are merely quiet — and it gets out of the way the moment those
sessions are gone, the tunnel dies, or it has run too long."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer, free_port  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


def load_fresh_module(home):
    """A standalone bct-chat module instance whose STATE_DIR/PIDFILE/etc (computed at
    import time) resolve under `home`. BCT_CHAT_HOME — not HOME — is the isolation
    knob: on Windows, expanduser("~") ignores HOME and would hand back the developer's
    real profile."""
    old = {k: os.environ.get(k) for k in ("HOME", "BCT_CHAT_HOME")}
    os.environ["HOME"] = home
    os.environ["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
    try:
        spec = importlib.util.spec_from_file_location(f"bct_chat_{id(home)}", CLIENT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def start_daemon(home, sock_spec, interval="0.2", max_uptime="30"):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.Popen([sys.executable, CLIENT, "heartbeat",
                             "--interval", interval, "--max-uptime", max_uptime],
                            env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(pred, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        self.sessions = os.path.join(self.state, "sessions")
        os.makedirs(self.sessions)
        with open(os.path.join(self.state, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"participantID": IDENT, "name": "HOST"}, f)
        self.marker = os.path.join(self.sessions, "sess-1")
        open(self.marker, "w").close()
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_ticks_touch_the_bridge_with_the_identity(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-list" for r in srv.received)))
            tick = [r for r in srv.received if r["cmd"] == "chat-list"][0]
            self.assertEqual(tick["paneID"], IDENT)
        finally:
            srv.close()

    def test_exits_when_the_last_session_marker_is_gone(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-list" for r in srv.received)))
            os.remove(self.marker)                       # every claude on this host exited
            self.assertTrue(wait_for(lambda: self.proc.poll() is not None))
            self.assertEqual(self.proc.returncode, 0)
        finally:
            srv.close()

    def test_exits_when_the_socket_is_unreachable(self):
        # Tunnel down: the two-strike rule, not a first-failure exit. A single
        # flaky tick must not take the daemon down (interval=1s: still alive
        # well inside that first sleep proves one miss alone didn't kill it);
        # only a second consecutive miss gets it out of the way, pidfile and all.
        self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{free_port()}", interval="1")
        time.sleep(0.4)
        self.assertIsNone(self.proc.poll(),
                           "died on the first failed tick — two-strike rule regressed")
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None, timeout=10))
        self.assertEqual(self.proc.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "heartbeat.pid")))

    def test_two_consecutive_socket_failures_exactly_trip_the_break(self):
        # Deterministic companion to the timing-based test above: drive
        # do_heartbeat() in-process with a mocked sock_available() and count
        # exactly how many misses it tolerates before breaking out.
        mod = load_fresh_module(self.home)      # self.marker (setUp) already lives under it
        calls = []

        def fake_sock_available():
            calls.append(1)
            return False

        mod.sock_available = fake_sock_available
        mod.do_heartbeat(0, 100)
        self.assertEqual(len(calls), 2)   # exactly two misses, not one
        self.assertFalse(os.path.exists(mod.PIDFILE))

    def test_not_invited_drives_a_rejoin_request(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-HB"}
            return {"ok": False, "error": "이 패널은 대화방에 초대되지 않았습니다"}

        srv = FakeChatServer(handler)
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-join" for r in srv.received)))
            pending = os.path.join(self.state, "pending-join.json")
            self.assertTrue(wait_for(lambda: os.path.exists(pending)))
        finally:
            srv.close()

    def test_a_second_daemon_does_not_start_while_one_is_live(self):
        srv = FakeChatServer(lambda req: {"ok": True, "text": "roster"})
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            pidfile = os.path.join(self.state, "heartbeat.pid")
            self.assertTrue(wait_for(lambda: os.path.exists(pidfile)))
            second = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            second.wait(timeout=10)
            self.assertEqual(second.returncode, 0)        # yielded to the live one
            with open(pidfile, encoding="utf-8") as f:
                self.assertEqual(int(f.read().strip()), self.proc.pid)
        finally:
            srv.close()

    def test_system_exit_inside_a_tick_does_not_block_a_respawn(self):
        # die() inside do_join (chained from a NOT_INVITED tick whose own rejoin
        # then errors) raises SystemExit inside the loop body. That must not leak
        # a fresh-mtime pid file behind: if it did, heartbeat_alive() would report
        # the (now-dead) daemon as live for up to 2*HEARTBEAT_INTERVAL and a fresh
        # spawn would be refused — exactly the eviction this daemon exists to stop.
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": False, "error": "boom"}
            return {"ok": False, "error": NOT_INVITED}

        srv = FakeChatServer(handler)
        pidfile = os.path.join(self.state, "heartbeat.pid")
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: self.proc.poll() is not None, timeout=15))
            self.assertEqual(self.proc.returncode, 0, self.proc.stderr.read())
            self.assertFalse(os.path.exists(pidfile))

            # And a fresh daemon must actually start ticking, not be told a stale
            # "live" pid file means someone else already owns it.
            before = len(srv.received)
            second = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            try:
                self.assertTrue(wait_for(lambda: len(srv.received) > before))
            finally:
                if second.poll() is None:
                    second.kill()
                second.wait(timeout=5)
        finally:
            srv.close()

    def test_heartbeat_alive_is_mtime_based_not_existence_based(self):
        # Must FAIL if heartbeat_alive() is ever reduced to os.path.exists(PIDFILE):
        # a crashed daemon's pid file has to age out, not read as live forever.
        mod = load_fresh_module(self.home)
        os.makedirs(os.path.dirname(mod.PIDFILE), exist_ok=True)
        with open(mod.PIDFILE, "w", encoding="utf-8") as f:
            f.write("1")
        os.utime(mod.PIDFILE, None)                                   # fresh mtime
        self.assertTrue(mod.heartbeat_alive())
        stale = time.time() - (2 * mod.HEARTBEAT_INTERVAL + 1)
        os.utime(mod.PIDFILE, (stale, stale))                         # older than the threshold
        self.assertFalse(mod.heartbeat_alive())

    def test_spawn_heartbeat_refuses_when_a_live_pidfile_exists(self):
        mod = load_fresh_module(self.home)
        os.makedirs(os.path.dirname(mod.PIDFILE), exist_ok=True)
        with open(mod.PIDFILE, "w", encoding="utf-8") as f:
            f.write("999999")     # not this process — liveness is mtime-based, not pid-based
        os.utime(mod.PIDFILE, None)

        class FakeSubprocess:
            DEVNULL = subprocess.DEVNULL
            calls = []

            def Popen(self, *a, **k):
                self.calls.append((a, k))

        mod.subprocess = FakeSubprocess()
        mod.spawn_heartbeat()
        self.assertEqual(mod.subprocess.calls, [])

    def test_yielding_to_a_newer_daemon_does_not_delete_its_pidfile(self):
        # If ownership of the pid file changes out from under this loop (a race
        # with another daemon instance), this instance must break without
        # deleting or disturbing the file — it is no longer this instance's to
        # release, and the winner would crash on its next os.utime() otherwise.
        mod = load_fresh_module(self.home)
        winner_pid = os.getpid() + 12345           # any pid that isn't ours or 0
        claimed = []

        def fake_sock_available():
            if not claimed:
                with open(mod.PIDFILE, "w", encoding="utf-8") as f:
                    f.write(str(winner_pid))        # another daemon claims the pidfile
                claimed.append(1)
            return True

        mod.sock_available = fake_sock_available
        mod.rpc = lambda *a, **k: {"ok": True, "text": "roster"}
        mod.do_heartbeat(0, 100)

        with open(mod.PIDFILE, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), str(winner_pid))

    def test_utime_survives_pidfile_vanishing_mid_tick(self):
        # Refreshing the pid file's mtime must not crash if the file has
        # vanished underneath it (another process/cleanup raced with us).
        mod = load_fresh_module(self.home)
        removed = []

        def fake_live_sessions():
            if not removed:
                os.remove(mod.PIDFILE)
                removed.append(1)
            return ["sess-1"]

        mod.live_sessions = fake_live_sessions
        mod.sock_available = lambda: False          # tunnel down too; two strikes and out
        mod.do_heartbeat(0, 100)                     # must not raise
        self.assertFalse(os.path.exists(mod.PIDFILE))

    def test_pending_join_is_polled_not_reraised(self):
        # A prior tick's chat-join already left a pending request outstanding.
        # The NOT_INVITED branch must poll it (chat-join-poll), never fire a
        # second chat-join — that would overwrite pending-join.json with a
        # fresh requestID and orphan any approval the user is about to click
        # on the old one.
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-1"}
            if req["cmd"] == "chat-join-poll":
                return {"ok": True, "text": "pending"}   # neither approved nor denied/expired
            return {"ok": False, "error": NOT_INVITED}    # chat-list: still not seated

        srv = FakeChatServer(handler)
        pending = os.path.join(self.state, "pending-join.json")
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: os.path.exists(pending)))
            with open(pending, encoding="utf-8") as f:
                first_req = json.load(f)["requestID"]
            # Let several more ticks pass while the poll keeps saying "pending".
            self.assertTrue(wait_for(
                lambda: sum(1 for r in srv.received if r["cmd"] == "chat-join-poll") >= 3,
                timeout=5))
            self.assertEqual(sum(1 for r in srv.received if r["cmd"] == "chat-join"), 1)
            with open(pending, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["requestID"], first_req)
        finally:
            srv.close()

    def test_approved_pending_is_claimed_and_join_stops(self):
        # Once the bridge reports the pending request approved, the daemon
        # must claim it: save the new identity, drop pending-join.json, and
        # never fire another chat-join.
        new_id = "B2C3D4E5-1111-2222-3333-444455556666"
        state = {"approved": False}

        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-2"}
            if req["cmd"] == "chat-join-poll":
                state["approved"] = True
                return {"ok": True, "text": f"approved\n{new_id}"}
            if state["approved"]:
                return {"ok": True, "text": "roster"}      # seated now
            return {"ok": False, "error": NOT_INVITED}

        srv = FakeChatServer(handler)
        pending = os.path.join(self.state, "pending-join.json")
        identity_path = os.path.join(self.state, "identity.json")
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: any(r["cmd"] == "chat-join-poll" for r in srv.received)))
            self.assertTrue(wait_for(lambda: not os.path.exists(pending)))
            with open(identity_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["participantID"], new_id)
            time.sleep(0.6)     # a few more ticks; must not re-request
            self.assertEqual(sum(1 for r in srv.received if r["cmd"] == "chat-join"), 1)
        finally:
            srv.close()

    @unittest.skip("rewritten in Task 6: daemon-as-ear — join-cooldown.json is now "
                    "join-state.json (Task 5's bounded budget); the daemon's own "
                    "stop-nagging-after-denial behaviour is preserved (see "
                    "test_membership.MembershipTests), only this test's file/shape "
                    "assertions are stale.")
    def test_denied_pending_arms_cooldown_and_stops_join(self):
        # A denied/expired poll must arm the 30-min cooldown, and subsequent
        # ticks must send no further chat-join until it expires.
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-3"}
            if req["cmd"] == "chat-join-poll":
                return {"ok": False, "error": "denied"}
            return {"ok": False, "error": NOT_INVITED}

        srv = FakeChatServer(handler)
        cooldown = os.path.join(self.state, "join-cooldown.json")
        try:
            self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertTrue(wait_for(lambda: os.path.exists(cooldown)))
            with open(cooldown, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["outcome"], "denied")
            time.sleep(0.6)     # a few more ticks; cooldown must hold
            self.assertEqual(sum(1 for r in srv.received if r["cmd"] == "chat-join"), 1)
        finally:
            srv.close()

    def test_heartbeat_interval_without_a_value_dies_cleanly(self):
        # Matches wait --timeout's validation: no raw IndexError traceback.
        env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
        env["HOME"] = self.home
        env["BCT_CHAT_HOME"] = os.path.join(self.home, ".bct-chat")
        env["BCT_CHAT_SOCK"] = f"tcp:127.0.0.1:{free_port()}"
        r = subprocess.run([sys.executable, CLIENT, "heartbeat", "--interval"],
                            env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("--interval", r.stderr)


if __name__ == "__main__":
    unittest.main()
