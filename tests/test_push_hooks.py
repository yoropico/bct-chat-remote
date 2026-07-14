#!/usr/bin/env python3
"""Delivery is local-only: the hooks read the inbox and never open a socket. That is what
makes 'hook timeout < RPC budget' unrepresentable — the defect class that lost mentions
outright, because chat-read had already moved BCT's cursor when the hook was killed."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class FakeSub:
    DEVNULL = -3
    spawned = []

    @staticmethod
    def Popen(argv, **kwargs):
        FakeSub.spawned.append(argv)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        # A bare self.fail() is NOT enough to enforce "no hook opens a socket": it raises
        # AssertionError, and every hook verb swallows (Exception, SystemExit) by design —
        # so a hook that opened a socket would fail silently INSIDE its own try and the
        # test would still pass. Record the breach where the swallow cannot reach it, and
        # assert on the record in tearDown.
        self.socket_calls = []

        def no_socket(*a, **k):
            self.socket_calls.append(a)
            raise AssertionError("a delivery hook opened a socket")

        self.mod.rpc = no_socket
        self.mod.sock_available = no_socket
        self.mod.hook_payload = lambda: {}
        FakeSub.spawned = []
        self.mod.subprocess = FakeSub
        os.environ.pop("BCT_PANE_ID", None)
        os.environ.pop("BCT_CHAT_MODE", None)
        os.environ.pop("BCT_CHAT_STANDBY", None)

    def tearDown(self):
        breaches = self.socket_calls
        shutil.rmtree(self.home, ignore_errors=True)
        os.environ.pop("BCT_CHAT_MODE", None)
        os.environ.pop("BCT_PANE_ID", None)
        os.environ.pop("BCT_CHAT_STANDBY", None)
        self.assertEqual(breaches, [], "a delivery hook touched the socket")

    def run_verb(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_stop_hook_blocks_with_the_digest_from_the_inbox(self):
        self.mod.inbox_put("yoros: @svr 봐줘\nnavy: 진행중", "svr")
        obj = json.loads(self.run_verb(self.mod.stop_hook))
        self.assertEqual(obj["decision"], "block")
        self.assertIn("당신은 @svr", obj["reason"])
        self.assertIn("yoros: @svr 봐줘", obj["reason"])
        self.assertIn("bct-chat.py send", obj["reason"])

    def test_an_empty_inbox_is_a_silent_zero_second_return_in_work_mode(self):
        started = time.time()
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertLess(time.time() - started, 1, "work mode held the turn end")

    def test_a_delivered_item_is_acked_and_never_delivered_twice(self):
        self.mod.inbox_put("once", "svr")
        self.assertIn("once", self.run_verb(self.mod.stop_hook))
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertEqual(os.listdir(self.mod.PROCESSING_DIR), [])

    def test_an_item_whose_print_dies_is_not_lost(self):
        """deliver() prints the digest and THEN acks (deletes the processing/ file) — that
        ordering IS the at-least-once contract. Swap it (ack, then print) and a crash
        between the two turns a harmless duplicate into a silent loss, and no test that
        merely calls print() would catch the swap: print() never fails under
        redirect_stdout, so it can't stand in for "the output step blew up". json.dumps()
        — which deliver() calls to build the block-JSON before printing it — is the lever
        that actually can fail without print() itself being touched."""
        self.mod.inbox_put("boom", "svr")

        # self.mod.json IS the real stdlib json module (one shared object, not a
        # per-module copy), so a blanket "always raise" stub would also break
        # save(CHAIN, ...) — which stop_hook() calls BEFORE deliver() — and the item
        # would never even reach deliver()'s print/ack step at all, making this test
        # pass for the wrong reason. Raise only for the block-JSON shape deliver()
        # builds, and restore the real dumps() via addCleanup so no other test run in
        # this process (json.dumps is global) is affected.
        real_dumps = self.mod.json.dumps

        def raiser(obj, *a, **k):
            if isinstance(obj, dict) and obj.get("decision") == "block":
                raise OSError("stdout gone")
            return real_dumps(obj, *a, **k)

        self.addCleanup(setattr, self.mod.json, "dumps", real_dumps)
        self.mod.json.dumps = raiser
        self.run_verb(self.mod.stop_hook)
        self.assertEqual(len(os.listdir(self.mod.PROCESSING_DIR)), 1,
                          "an item whose delivery blew up must stay recoverable")

    def test_a_prompt_submit_item_whose_digest_build_fails_is_not_lost(self):
        """The equivalent guard for prompt_submit_hook()'s own print-then-ack: it builds
        the digest with compose_digest(), prints it, and only then acks. A failure while
        building the digest must leave the item in processing/, not acked away."""
        self.mod.inbox_put("boom", "svr")

        def raiser(*a, **k):
            raise OSError("digest build blew up")

        self.mod.compose_digest = raiser
        self.run_verb(self.mod.prompt_submit_hook)
        self.assertEqual(len(os.listdir(self.mod.PROCESSING_DIR)), 1,
                          "an item whose digest build blew up must stay recoverable")

    def test_standby_mode_waits_for_an_item(self):
        os.environ["BCT_CHAT_MODE"] = "standby"
        self.mod.STANDBY_HOLD = 3
        self.mod.INBOX_POLL = 0.05
        import threading
        threading.Timer(0.3, lambda: self.mod.inbox_put("late one", "svr")).start()
        out = self.run_verb(self.mod.stop_hook)
        self.assertIn("late one", json.loads(out)["reason"])

    def test_standby_hold_expiry_is_a_silent_return(self):
        os.environ["BCT_CHAT_MODE"] = "standby"
        self.mod.STANDBY_HOLD = 0.2
        self.mod.INBOX_POLL = 0.05
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_the_chain_cap_stops_a_two_remote_ping_pong(self):
        # D10: two standby remotes mentioning each other can bill turns forever with no
        # human in the loop. After CHAIN_CAP automatic re-engagements we stop delivering
        # — and the mention STAYS in the inbox for the user's next prompt.
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
        self.mod.hook_payload = lambda: {"stop_hook_active": False}
        self.assertIn("m0", self.run_verb(self.mod.stop_hook))
        self.mod.hook_payload = lambda: {"stop_hook_active": True}
        self.assertIn("m1", self.run_verb(self.mod.stop_hook))
        self.assertIn("m2", self.run_verb(self.mod.stop_hook))
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")     # capped
        self.assertIsNotNone(self.mod.inbox_claim())                # message preserved

    def test_a_capped_standby_stop_hook_does_not_hold_either(self):
        """Capped means capped: a standby session at the cap must return NOW, not sit on a
        15-minute local hold it is forbidden to deliver the result of."""
        os.environ["BCT_CHAT_MODE"] = "standby"
        self.mod.STANDBY_HOLD = 30          # would dominate the elapsed check if we held
        self.mod.hook_payload = lambda: {"stop_hook_active": True}
        self.mod.save(self.mod.CHAIN, {"n": self.mod.CHAIN_CAP})
        started = time.time()
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertLess(time.time() - started, 1, "a capped stop hook held the turn end")

    def test_a_user_turn_resets_the_chain(self):
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
        self.mod.hook_payload = lambda: {"stop_hook_active": True}
        self.mod.save(self.mod.CHAIN, {"n": 3})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.mod.hook_payload = lambda: {"stop_hook_active": False}
        self.assertIn("m0", self.run_verb(self.mod.stop_hook))

    def test_a_prompt_submit_resets_the_chain(self):
        """The other half of the reset: the user engaging IS the human in the loop the cap
        was waiting for, so the next Stop hook starts a fresh chain."""
        self.mod.inbox_put("m0", "svr")
        self.mod.save(self.mod.CHAIN, {"n": self.mod.CHAIN_CAP})
        self.assertIn("m0", self.run_verb(self.mod.prompt_submit_hook))
        self.assertIsNone(self.mod.load(self.mod.CHAIN))

    def test_a_prompt_submit_resets_the_chain_even_with_an_empty_inbox(self):
        """The reset must not be conditional on there being anything to deliver: the user
        prompting IS the reset, regardless of whether a mention happens to be waiting.
        An empty inbox must not leave a stale chain.json capping the user's own turn."""
        self.mod.save(self.mod.CHAIN, {"n": self.mod.CHAIN_CAP})
        self.assertEqual(self.run_verb(self.mod.prompt_submit_hook), "")
        self.assertIsNone(self.mod.load(self.mod.CHAIN))

    def test_prompt_submit_prints_the_digest_as_context(self):
        self.mod.inbox_put("yoros: @svr 확인", "svr")
        out = self.run_verb(self.mod.prompt_submit_hook)
        self.assertIn("yoros: @svr 확인", out)
        self.assertNotIn("decision", out)          # plain stdout, not block-JSON

    def test_prompt_submit_never_holds(self):
        os.environ["BCT_CHAT_MODE"] = "standby"
        started = time.time()
        self.assertEqual(self.run_verb(self.mod.prompt_submit_hook), "")
        self.assertLess(time.time() - started, 1)

    def test_every_hook_is_a_daemon_spawn_point(self):
        self.run_verb(self.mod.stop_hook)
        self.run_verb(self.mod.prompt_submit_hook)
        self.assertGreaterEqual(len(FakeSub.spawned), 2)

    def test_a_delivery_hook_re_marks_its_session_before_it_spawns(self):
        """mark_session()'s only other caller is session_start(), and gc_markers() collects
        a marker whose owning pid reads dead. If claude_pid() ever resolves wrong, a LIVE
        session's marker is collected, the daemon exits, and that session goes deaf with
        nothing left to repair it. Every hook that knows its session id re-marks — that
        makes the marker self-healing, and refreshes the mtime the pid:0 TTL fallback rides
        on. Ordering matters for the same reason it does in session_start(): a daemon
        spawned ahead of its own marker sees an empty session set and exits instantly."""
        marker = os.path.join(self.mod.SESSIONS_DIR, "sess-1")
        real_mark = self.mod.mark_session          # capture ONCE: re-wrapping a wrapper recurses
        order = []
        self.mod.mark_session = lambda sid: (order.append("mark"), real_mark(sid))
        self.mod.ensure_daemon = lambda: order.append("spawn")
        self.mod.hook_payload = lambda: {"session_id": "sess-1"}
        for hook in (self.mod.stop_hook, self.mod.prompt_submit_hook):
            with self.subTest(hook=hook.__name__):
                self.mod.forget(marker)
                order.clear()
                self.run_verb(hook)
                self.assertEqual(order, ["mark", "spawn"])
                self.assertTrue(os.path.exists(marker), "the hook left no marker")

    def test_a_hook_without_a_session_id_marks_nothing(self):
        """An interactive run (no payload on stdin) is not a session — it must not leave a
        marker behind, which would hold a phantom seat open for MARKER_TTL."""
        self.run_verb(self.mod.stop_hook)
        self.assertEqual(self.mod.live_sessions(), [])

    def test_hooks_are_a_no_op_inside_a_bct_pane(self):
        os.environ["BCT_PANE_ID"] = "pane-1"
        self.mod.inbox_put("x", "svr")
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertEqual(self.run_verb(self.mod.prompt_submit_hook), "")
        self.assertEqual(FakeSub.spawned, [], "a BCT pane must not spawn a daemon")
        self.assertIsNotNone(self.mod.inbox_claim(), "a BCT pane consumed the inbox")

    def test_the_digest_is_capped(self):
        self.mod.DIGEST_MAX_LINES = 5
        self.mod.inbox_put("\n".join(f"line {i}" for i in range(100)), "svr")
        reason = json.loads(self.run_verb(self.mod.stop_hook))["reason"]
        self.assertLess(len(reason.splitlines()), 12)
        self.assertIn("생략", reason)

    def test_reply_hint_survives_a_line_longer_than_the_byte_cap(self):
        """A single mention line longer than DIGEST_MAX_BYTES must not take REPLY_HINT
        down with it when the byte cap slices the text — that would hand claude a
        blocked turn showing a mention with no instruction for how to answer it. The
        byte budget applies to the body only; REPLY_HINT is appended after truncation."""
        self.mod.DIGEST_MAX_BYTES = 200
        self.mod.inbox_put("x" * 5000, "svr")
        reason = json.loads(self.run_verb(self.mod.stop_hook))["reason"]
        self.assertIn(self.mod.REPLY_HINT, reason)

    def test_dropped_mentions_are_announced_in_the_next_digest(self):
        self.mod.INBOX_CAP = 2
        for i in range(4):
            self.mod.inbox_put(f"m{i}", "svr")
            time.sleep(0.002)
        reason = json.loads(self.run_verb(self.mod.stop_hook))["reason"]
        self.assertIn("오래된 멘션 2건 생략", reason)

    def test_a_dead_hooks_item_comes_back(self):
        self.mod.inbox_put("orphan", "svr")
        path, _ = self.mod.inbox_claim()               # a hook that died before printing
        os.utime(path, (time.time() - 300,) * 2)
        self.assertIn("orphan", self.run_verb(self.mod.stop_hook))

    def test_chat_mode_defaults_to_work(self):
        self.assertEqual(self.mod.chat_mode(), "work")
        for standby in ("standby", "STANDBY", " Standby "):
            os.environ["BCT_CHAT_MODE"] = standby
            self.assertEqual(self.mod.chat_mode(), "standby")
        os.environ["BCT_CHAT_MODE"] = "nonsense"
        self.assertEqual(self.mod.chat_mode(), "work")

    def test_the_legacy_standby_variable_means_what_it_says(self):
        """BCT_CHAT_STANDBY is the pre-BCT_CHAT_MODE variable. Both arms of its old
        handling returned "work", so it had silently stopped doing anything at all — a
        user who had set BCT_CHAT_STANDBY=1 to opt INTO standby got demoted to work mode
        with no warning. A truthy value must enable standby; only a recognized disable
        spelling (or leaving it unset) should fall back to work."""
        for truthy in ("1", "true", "yes", "on", "anything"):
            os.environ["BCT_CHAT_STANDBY"] = truthy
            self.assertEqual(self.mod.chat_mode(), "standby", f"BCT_CHAT_STANDBY={truthy!r}")
        for falsy in ("0", "off", "false", "no"):
            os.environ["BCT_CHAT_STANDBY"] = falsy
            self.assertEqual(self.mod.chat_mode(), "work", f"BCT_CHAT_STANDBY={falsy!r}")

    def test_chat_mode_wins_over_the_legacy_variable_when_set(self):
        os.environ["BCT_CHAT_STANDBY"] = "1"                # legacy says standby
        os.environ["BCT_CHAT_MODE"] = "work"                # explicit says work
        self.assertEqual(self.mod.chat_mode(), "work")

    def test_a_broken_inbox_never_breaks_the_turn(self):
        """The whole point of the exit-0 invariant: hooks.json falls back `python3 … ||
        python …` on ANY nonzero exit, and the re-run reads a stdin that is already
        drained. So no failure below the pane guard may escape — not an exception, not a
        SystemExit (a chained die())."""
        for boom in (OSError("disk"), SystemExit(1)):
            with self.subTest(boom=type(boom).__name__):
                def raiser(*a, **k):
                    raise boom

                self.mod.inbox_claim = raiser
                self.assertEqual(self.run_verb(self.mod.stop_hook), "")
                self.assertEqual(self.run_verb(self.mod.prompt_submit_hook), "")


class NoSocketReachabilityTests(unittest.TestCase):
    """The binding constraint, enforced STATICALLY over the whole call graph.

    The runtime tests above can only prove it for the paths they exercise; an error path
    nobody thought to drive could still reach the wire. So walk the artifact's AST from
    each hook verb through every function it can transitively call, and assert the wire
    is unreachable — that is what makes 'hook timeout < RPC budget' unrepresentable
    rather than merely untested."""

    FORBIDDEN = {"rpc", "sock_available", "authed", "do_join", "claim_pending",
                 "ensure_membership", "do_leave", "connect", "create_connection"}

    def setUp(self):
        import ast
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "scripts", "bct-chat.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        self.ast = ast
        self.funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def called_names(self, fn):
        names = set()
        for node in self.ast.walk(fn):
            if isinstance(node, self.ast.Call):
                f = node.func
                if isinstance(f, self.ast.Name):
                    names.add(f.id)
                elif isinstance(f, self.ast.Attribute):
                    names.add(f.attr)          # socket.socket(), sock.connect(), …
        return names

    def test_no_hook_verb_can_reach_the_wire(self):
        for entry in ("stop_hook", "prompt_submit_hook", "session_start", "session_end"):
            with self.subTest(verb=entry):
                seen, stack = set(), [entry]
                while stack:
                    name = stack.pop()
                    if name in seen:
                        continue
                    seen.add(name)
                    if name in self.funcs:
                        stack.extend(self.called_names(self.funcs[name]))
                self.assertEqual(sorted(seen & self.FORBIDDEN), [],
                                 f"{entry} can reach the socket")
                self.assertIn("ensure_daemon" if entry != "session_end" else "unmark_session",
                              seen, "the walk did not actually traverse the verb")


class HooksJsonTests(unittest.TestCase):
    def hooks(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "hooks", "hooks.json"), encoding="utf-8") as f:
            return json.load(f)["hooks"]

    def test_stop_timeout_covers_the_standby_hold(self):
        stop = self.hooks()["Stop"][0]["hooks"][0]["timeout"]
        home = tempfile.mkdtemp()
        try:
            mod = load_fresh_module(home)
            self.assertGreater(stop, mod.STANDBY_HOLD,
                               "the Stop hook would be killed mid-hold")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_every_hook_verb_is_registered_with_the_python3_fallback(self):
        hooks = self.hooks()
        for event, verb in (("Stop", "stop-hook"), ("UserPromptSubmit", "prompt-submit"),
                            ("SessionStart", "session-start"), ("SessionEnd", "session-end")):
            cmd = hooks[event][0]["hooks"][0]["command"]
            self.assertIn(verb, cmd)
            self.assertIn("|| python ", cmd)         # Windows MS-Store-stub fallback

    def test_the_local_only_hooks_need_no_rpc_budget(self):
        """SessionStart/SessionEnd/UserPromptSubmit do zero RPC and never hold, so their
        timeout is a generous bound on local file I/O — not a wire budget. Claude Code
        additionally clamps its SessionEnd shutdown wait to 60s, so a large value there
        would be a lie."""
        hooks = self.hooks()
        for event in ("SessionStart", "SessionEnd", "UserPromptSubmit"):
            self.assertLessEqual(hooks[event][0]["hooks"][0]["timeout"], 60)


if __name__ == "__main__":
    unittest.main()
