#!/usr/bin/env python3
"""The daemon is now the EAR: capture reliability is daemon reliability. It must hold a
listen, land the mention on disk BEFORE the next listen, never die of a dead tunnel, and
get out of the way the instant its sessions are gone, a newer daemon takes over, or the
user has left the room."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import CLIENT, load_fresh_module, reap_daemon, wait_for  # noqa: E402
from test_tcp_transport import FakeChatServer  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
NO_MENTION = "(새 멘션 없음)"


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.mark_session("sess-1")
        self.mod.sock_available = lambda: True
        self.mod.BACKOFF_MIN = 0.01
        self.mod.BACKOFF_MAX = 0.02
        self.mod.JOIN_POLL = 0.01
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def scripted_rpc(self, replies, stop_after=None):
        """Feed the daemon a fixed reply script; unmark the session after `stop_after`
        calls so the loop exits on its own (that IS the tested exit condition)."""
        def fake(cmd, args, pane_id="", timeout=10):
            self.calls.append(cmd)
            if stop_after is not None and len(self.calls) >= stop_after:
                self.mod.unmark_session("sess-1")
            r = replies.get(cmd, {"ok": True, "text": ""})
            return r(len(self.calls)) if callable(r) else r
        self.mod.rpc = fake

    def test_a_pushed_mention_lands_in_the_inbox(self):
        self.scripted_rpc({"chat-listen": {"ok": True, "text": "yoros: @svr 봐줘"}},
                          stop_after=2)
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "yoros: @svr 봐줘")

    def test_the_mention_is_on_disk_before_the_next_listen(self):
        # The cursor has already moved server-side when chat-listen returns. If we
        # issued the next listen before the file was in place, a crash in between
        # would lose the message with no ack verb to recover it.
        seen = []

        def fake(cmd, args, pane_id="", timeout=10):
            self.calls.append(cmd)
            if cmd == "chat-listen":
                seen.append(len(os.listdir(self.mod.INBOX_DIR))
                            if os.path.isdir(self.mod.INBOX_DIR) else 0)
                if len(self.calls) > 3:
                    self.mod.unmark_session("sess-1")
                return {"ok": True, "text": f"m{len(self.calls)}"}
            return {"ok": True, "text": ""}

        self.mod.rpc = fake
        self.mod.do_daemon(presence_interval=999, listen_timeout=1)
        self.assertEqual(seen, [0, 1, 2, 3])       # each listen sees the PREVIOUS one landed

    def test_the_silence_sentinel_is_not_an_inbox_item(self):
        self.scripted_rpc({"chat-listen": {"ok": True, "text": self.mod.NO_MENTION}},
                          stop_after=3)
        self.mod.do_daemon(presence_interval=999, listen_timeout=1)
        self.assertIsNone(self.mod.inbox_claim())

    def test_a_bridge_that_does_not_hold_the_window_is_not_busy_waited(self):
        # chat-listen is supposed to HOLD (~30s server-side). A bridge that answers the
        # silence sentinel instantly — an older BCT, say — must not turn the daemon into a
        # tight RPC loop on the user's remote. A mention is never delayed by this: the
        # mention path returns straight to the next listen.
        started = time.time()
        self.scripted_rpc({"chat-listen": {"ok": True, "text": self.mod.NO_MENTION}},
                          stop_after=3)
        self.mod.do_daemon(presence_interval=999, listen_timeout=1)
        self.assertEqual(self.calls, ["chat-listen"] * 3)
        self.assertGreaterEqual(time.time() - started, 3 * (1 / 10.0),
                                "an instantly-answered listen was re-armed with no floor")

    def test_a_dead_tunnel_backs_off_and_never_suicides(self):
        # D1: the old two-strike rule let an 8-minute blip permanently un-ear a session.
        ticks = {"n": 0}

        def unavailable():
            ticks["n"] += 1
            if ticks["n"] > 5:
                self.mod.unmark_session("sess-1")   # end the test, not the daemon
            return False

        self.mod.sock_available = unavailable
        self.mod.rpc = lambda *a, **k: self.fail("no RPC may be attempted with no socket")
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)
        self.assertGreater(ticks["n"], 2, "the daemon gave up instead of waiting")

    def test_presence_tick_interleaves_with_the_listen(self):
        self.scripted_rpc({"chat-listen": {"ok": True, "text": self.mod.NO_MENTION}},
                          stop_after=6)
        self.mod.do_daemon(presence_interval=0, listen_timeout=1)   # every tick
        self.assertIn("chat-list", self.calls)

    def test_not_invited_re_requests_through_the_budget(self):
        self.scripted_rpc({"chat-listen": {"ok": False, "error": NOT_INVITED},
                           "chat-list": {"ok": False, "error": NOT_INVITED},
                           "chat-join": {"ok": True, "text": "REQ-1"}},
                          stop_after=4)
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)
        self.assertIn("chat-join", self.calls)
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-1")

    def test_exits_when_the_last_session_marker_is_gone(self):
        self.mod.unmark_session("sess-1")
        self.mod.rpc = lambda *a, **k: self.fail("a daemon with no live session must not tick")
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)

    def test_exits_when_a_newer_daemon_owns_the_pidfile(self):
        self.mod.atomic_write(self.mod.PIDFILE, "999999")
        # A live-looking pidfile owned by someone else: refuse to tick, and NEVER delete
        # the winner's file on the way out.
        self.mod.heartbeat_alive = lambda: True
        self.mod.rpc = lambda *a, **k: self.fail("yielded daemon must not tick")
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)
        self.assertTrue(os.path.exists(self.mod.PIDFILE))
        self.assertEqual(self.mod.pidfile_owner(), 999999)

    def test_exits_when_suspended_and_unseated(self):
        self.mod.forget(self.mod.IDENTITY)
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": 0,
                                            "suspended": True, "lastOutcome": "left"})
        self.mod.rpc = lambda *a, **k: self.fail("a daemon the user left must not tick")
        self.mod.do_daemon(presence_interval=0.01, listen_timeout=1)

    def test_gc_removes_a_crashed_sessions_marker(self):
        self.mod.save(os.path.join(self.mod.SESSIONS_DIR, "dead-1"),
                      {"pid": 999999, "startedAt": time.time()})
        self.assertEqual(self.mod.gc_markers(), 1)
        self.assertNotIn("dead-1", self.mod.live_sessions())

    def test_gc_keeps_a_live_sessions_marker(self):
        self.mod.save(os.path.join(self.mod.SESSIONS_DIR, "live-1"),
                      {"pid": os.getpid(), "startedAt": time.time()})
        self.mod.gc_markers()
        self.assertIn("live-1", self.mod.live_sessions())

    def test_gc_keeps_a_pidless_marker_until_its_ttl(self):
        p = os.path.join(self.mod.SESSIONS_DIR, "nopid-1")
        self.mod.save(p, {"pid": 0, "startedAt": time.time()})
        self.mod.gc_markers()
        self.assertIn("nopid-1", self.mod.live_sessions())
        os.utime(p, (time.time() - self.mod.MARKER_TTL - 1,) * 2)
        self.assertEqual(self.mod.gc_markers(), 1)

    def test_a_corrupt_marker_is_not_collected_on_sight_and_never_kills_the_daemon(self):
        # gc_markers() runs at the top of every loop pass, outside the tick's own
        # try/except: a marker whose "pid" is not an int (disk damage, an old empty
        # marker from a pre-upgrade session) must read as "no pid liveness" and fall back
        # to the TTL — never raise out of the loop, and never evict a session that may
        # well still be live.
        p = os.path.join(self.mod.SESSIONS_DIR, "weird-1")
        self.mod.atomic_write(p, '{"pid": "not-an-int"}')
        self.assertEqual(self.mod.gc_markers(), 0)
        self.assertIn("weird-1", self.mod.live_sessions())


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_a_fresh_pidfile_owned_by_a_dead_pid_is_not_alive(self):
        # D2: an ssh-logout SIGTERM leaves a pidfile with a FRESH mtime; mtime alone
        # called the corpse alive for 8 minutes — exactly the window in which the user
        # reconnects and restarts claude, which used to be the only spawn opportunity.
        self.mod.atomic_write(self.mod.PIDFILE, "999999")
        self.assertFalse(self.mod.heartbeat_alive())

    def test_a_fresh_pidfile_owned_by_a_live_pid_is_alive(self):
        self.mod.atomic_write(self.mod.PIDFILE, str(os.getpid()))
        self.assertTrue(self.mod.heartbeat_alive())

    def test_a_stale_pidfile_is_not_alive(self):
        self.mod.atomic_write(self.mod.PIDFILE, str(os.getpid()))
        old = time.time() - self.mod.PIDFILE_STALE - 1
        os.utime(self.mod.PIDFILE, (old, old))
        self.assertFalse(self.mod.heartbeat_alive())

    def test_ensure_daemon_refuses_while_suspended_and_unseated(self):
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": 0,
                                            "suspended": True, "lastOutcome": "left"})
        spawned = []
        self.mod.subprocess = type("S", (), {"Popen": lambda *a, **k: spawned.append(a),
                                             "DEVNULL": -3})
        self.mod.ensure_daemon()
        self.assertEqual(spawned, [], "a daemon was respawned into a room the user left")

    def test_ensure_daemon_spawns_when_none_is_alive(self):
        spawned = []

        class FakeSub:
            DEVNULL = -3

            @staticmethod
            def Popen(argv, **kwargs):
                spawned.append(argv)

        self.mod.subprocess = FakeSub
        self.mod.ensure_daemon()
        self.assertEqual(len(spawned), 1)
        self.assertIn("daemon", spawned[0])

    def test_ensure_daemon_refuses_while_a_live_daemon_holds_the_pidfile(self):
        self.mod.atomic_write(self.mod.PIDFILE, str(os.getpid()))   # fresh AND alive
        spawned = []

        class FakeSub:
            DEVNULL = -3

            @staticmethod
            def Popen(argv, **kwargs):
                spawned.append(argv)

        self.mod.subprocess = FakeSub
        self.mod.ensure_daemon()
        self.assertEqual(spawned, [], "a second daemon was spawned alongside a live one")

    def test_ensure_daemon_spawns_for_a_suspended_but_still_seated_host(self):
        # Suspended-AND-unseated is the exit condition; a host that is suspended but
        # still holds a seat (a denied re-join budget, say) is still IN the room and
        # still needs its ear.
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": 0,
                                            "suspended": True, "lastOutcome": "denied"})
        spawned = []

        class FakeSub:
            DEVNULL = -3

            @staticmethod
            def Popen(argv, **kwargs):
                spawned.append(argv)

        self.mod.subprocess = FakeSub
        self.mod.ensure_daemon()
        self.assertEqual(len(spawned), 1)


class DaemonProcessTests(unittest.TestCase):
    """One end-to-end pass through the shipped `daemon` verb. The in-process tests above
    drive do_daemon() directly and would not notice a broken CLI dispatch, a detached
    spawn that cannot reach the wire, or a mention that never reaches real disk."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(os.path.join(self.state, "sessions"))
        with open(os.path.join(self.state, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"participantID": IDENT, "name": "HOST"}, f)
        with open(os.path.join(self.state, "sessions", "sess-1"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "startedAt": time.time()}, f)
        self.pidfile = os.path.join(self.state, "heartbeat.pid")
        self.inbox = os.path.join(self.state, "inbox")
        self.proc = None

    def tearDown(self):
        reap_daemon(self.pidfile)
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
            for pipe in (self.proc.stdout, self.proc.stderr):
                if pipe:
                    pipe.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_the_daemon_verb_lands_a_pushed_mention_in_the_inbox(self):
        listens = {"n": 0}

        def handler(req):
            if req["cmd"] == "chat-listen":
                listens["n"] += 1
                if listens["n"] == 1:
                    return {"ok": True, "text": "yoros: @HOST 봐줘"}
                time.sleep(2)          # BCT holds this window open; don't spin the daemon
                return {"ok": True, "text": NO_MENTION}
            return {"ok": True, "text": "roster"}

        srv = FakeChatServer(handler)
        env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
        env["HOME"] = self.home
        env["BCT_CHAT_HOME"] = self.state
        env["BCT_CHAT_SOCK"] = f"tcp:127.0.0.1:{srv.port}"
        try:
            self.proc = subprocess.Popen([sys.executable, CLIENT, "daemon"], env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
            self.assertTrue(wait_for(lambda: os.path.isdir(self.inbox)
                                     and any(n.endswith(".json") for n in os.listdir(self.inbox))),
                            "the daemon never landed the pushed mention on disk")
            item = [n for n in os.listdir(self.inbox) if n.endswith(".json")][0]
            with open(os.path.join(self.inbox, item), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["text"], "yoros: @HOST 봐줘")
            self.assertIsNone(self.proc.poll(), "the daemon exited after one mention")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
