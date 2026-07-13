# External Participant Presence (client side) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A remote host proves it is alive (4-min heartbeat tied to its live claude sessions) so BCT stops evicting it while quiet, and a failed join request cannot nag the dock for 30 minutes.

**Architecture:** Three additions to the single-file python client. A `heartbeat` verb runs as a detached, single-instance daemon that pings `chat-list` every 4 min; the SessionStart hook spawns it and drops a per-session marker, SessionEnd removes the marker, and the daemon exits once no marker remains (or the socket dies, or 12 h pass). A `join-cooldown.json` gates every *automatic* join request for 30 min after a denial/expiry. And `identity.json` is no longer deleted when BCT rejects it — a dead UUID is harmless, and the rejoin path needs the name stored beside it.

**Tech Stack:** Python 3 stdlib only (the client runs on hosts with nothing installed — no deps, ever). unittest + the existing `FakeChatServer` harness in `tests/test_tcp_transport.py`. Ships as a claude-code plugin (`hooks/hooks.json`, `.claude-plugin/plugin.json`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-13-chat-external-presence-design.md` in the **bomi-terminal** repo (worktree `feat/chat-external-presence`). Its §3, §4, §6 are this plan; §1, §2, §5 are BCT-side.
- **Pure stdlib.** No third-party imports, ever. The client is scp'd onto hosts with nothing installed.
- **Windows is a first-class host** (`rsbglee`): no `AF_UNIX` (TCP transport via `BCT_CHAT_SOCK=tcp:host:port`), no `os.uname`, cp949 default locale (all file I/O and stdio already pinned to UTF-8 — keep it that way), and **never call `os.kill(pid, 0)`** — on Windows that terminates the process. Liveness is checked by file mtime, never by signalling a pid.
- Exact values: `HEARTBEAT_INTERVAL = 240`, `HEARTBEAT_MAX_UPTIME = 43200` (12 h), `JOIN_COOLDOWN = 1800` (30 min), pid file counts as dead when its mtime is older than `2 * HEARTBEAT_INTERVAL`.
- `NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"` — matched verbatim against the bridge's error.
- Anything other than `NOT_INVITED` from the bridge counts as "still a member": a hiccup must never drop an identity or raise a banner (the 1.2.0 rule).
- Test command: `python3 -m unittest discover -s tests` (15 tests green today).
- Branch off `fix/session-start-revalidates-identity` (PR #1) — this work builds on the 1.2.0 fix and amends one of its assertions.

---

### Task 1: Rejoin cooldown

**Files:**
- Modify: `scripts/bct-chat.py`
- Test: `tests/test_cooldown.py` (create)

**Interfaces:**
- Consumes: existing `load`/`save`/`forget`/`rpc`/`do_join`/`claim_pending`.
- Produces:
  - `COOLDOWN` = `~/.bct-chat/join-cooldown.json`, `JOIN_COOLDOWN = 1800`
  - `may_request_join() -> bool`
  - `note_join_failure(outcome: str)` — writes `{"lastFailedAt": <epoch>, "outcome": outcome}`
  - `clear_cooldown()`
  - `request_join_if_allowed(name: str) -> bool` — non-blocking join request, honours the cooldown
  - `cooldown_remaining() -> int` (seconds; 0 when clear) — used for the user-facing message

- [ ] **Step 1: Write the failing test**

Create `tests/test_cooldown.py`:

```python
#!/usr/bin/env python3
"""A denied/expired join request must not nag the dock: no automatic re-request
for 30 min. A human typing `join` at the remote's shell always wins."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_tcp_transport import CLIENT, FakeChatServer  # noqa: E402

NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


def run(args, home, sock_spec):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
    env["BCT_CHAT_SOCK"] = sock_spec
    return subprocess.run([sys.executable, CLIENT] + args, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.state = os.path.join(self.home, ".bct-chat")
        os.makedirs(self.state)
        self.cooldown = os.path.join(self.state, "join-cooldown.json")
        self.pending = os.path.join(self.state, "pending-join.json")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def write_cooldown(self, ago):
        with open(self.cooldown, "w", encoding="utf-8") as f:
            json.dump({"lastFailedAt": time.time() - ago, "outcome": "expired"}, f)

    def cmds(self, srv):
        return [r["cmd"] for r in srv.received]

    def test_expired_request_arms_the_cooldown(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-1"}
            return {"ok": False, "error": "expired"}          # chat-join-poll

        with open(self.pending, "w", encoding="utf-8") as f:
            json.dump({"requestID": "REQ-1", "name": "HOST"}, f)
        srv = FakeChatServer(handler)
        try:
            run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            with open(self.cooldown, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["outcome"], "expired")
        finally:
            srv.close()

    def test_session_start_within_cooldown_does_not_request(self):
        self.write_cooldown(ago=60)                            # 1 min ago: still cooling
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-2"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.pending))
        finally:
            srv.close()

    def test_session_start_after_cooldown_requests_again(self):
        self.write_cooldown(ago=1801)                          # 30 min + 1 s: expired
        srv = FakeChatServer(lambda req: {"ok": True, "text": "REQ-3"})
        try:
            r = run(["session-start"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))
        finally:
            srv.close()

    def test_manual_join_ignores_the_cooldown(self):
        self.write_cooldown(ago=60)
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-4"}
            return {"ok": True, "text": "approved\n" + "C1A6063F-0124-4229-9CE3-D757348A70F2"}

        srv = FakeChatServer(handler)
        try:
            r = run(["join", "HOST"], self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("chat-join", self.cmds(srv))
            self.assertFalse(os.path.exists(self.cooldown))    # approval clears it
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_cooldown -v 2>&1 | tail -15`
Expected: FAIL — `test_session_start_within_cooldown_does_not_request` sees `chat-join` (no cooldown exists yet), `test_expired_request_arms_the_cooldown` finds no `join-cooldown.json`.

- [ ] **Step 3: Implement the cooldown**

In `scripts/bct-chat.py`, add beside the other state paths:

```python
COOLDOWN = os.path.join(STATE_DIR, "join-cooldown.json")
JOIN_COOLDOWN = 1800            # 30 min — a request the user denied or ignored must not nag
```

Add after `forget()`:

```python
def cooldown_remaining():
    """Seconds until an automatic join request is allowed again (0 = now)."""
    obj = load(COOLDOWN)
    if not obj:
        return 0
    left = JOIN_COOLDOWN - (time.time() - obj.get("lastFailedAt", 0))
    return int(left) if left > 0 else 0


def may_request_join():
    return cooldown_remaining() == 0


def note_join_failure(outcome):
    save(COOLDOWN, {"lastFailedAt": time.time(), "outcome": outcome})


def clear_cooldown():
    forget(COOLDOWN)


def request_join_if_allowed(name):
    """Automatic (non-blocking) join request, gated by the cooldown. The manual
    `join` verb bypasses this — a human at the remote's shell always wins."""
    if not may_request_join():
        print(f"입장 재요청 쿨다운 중 — {cooldown_remaining() // 60}분 후 재시도", file=sys.stderr)
        return False
    do_join(name, wait_approval=False)
    return True
```

In `claim_pending()`, record the outcome — replace its body's tail:

```python
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        forget(PENDING)
        clear_cooldown()                      # seated — the slate is clean
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        forget(PENDING)
        note_join_failure(r["error"])         # arm the 30-min cooldown
    return False
```

In `do_join()`, clear the cooldown on a manual, approved join (the `wait_approval=True` path already calls `claim_pending`, which clears it) and on entry when invoked manually — the simplest correct rule is: the `join` verb clears the cooldown before requesting. In `main()`:

```python
    if verb == "join":
        clear_cooldown()                      # manual intent overrides the cooldown
        do_join(" ".join(rest) or default_name())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m unittest tests.test_cooldown -v 2>&1 | tail -8`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/bct-chat.py tests/test_cooldown.py
git commit -m "feat: 30-min cooldown on automatic rejoin — a denied request must not nag the dock"
```

---

### Task 2: Keep the identity when BCT rejects it

**Files:**
- Modify: `scripts/bct-chat.py` (`membership_live`, `authed`, `session_start`)
- Test: `tests/test_session_start_rejoin.py` (flip one assertion, add one)

**Interfaces:**
- Consumes: `may_request_join()`, `request_join_if_allowed()` (Task 1).
- Produces: `identity.json` now survives a `NOT_INVITED`; it is overwritten only by a new approval. The heartbeat (Task 3) depends on this — it needs an identity to send on every tick.

- [ ] **Step 1: Update the tests**

In `tests/test_session_start_rejoin.py`, `test_stale_identity_rerequests_join`, replace the last assertion:

```python
            self.assertFalse(os.path.exists(self.identity))   # dead id must not linger
```

with:

```python
            # The dead id is KEPT until a new approval overwrites it: the rejoin path
            # needs the name stored beside it, and the heartbeat needs something to send.
            with open(self.identity, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["participantID"], STALE_ID)
```

and add:

```python
    def test_stale_identity_within_cooldown_does_not_request(self):
        def handler(req):
            if req["cmd"] == "chat-join":
                return {"ok": True, "text": "REQ-NOPE"}
            return {"ok": False, "error": NOT_INVITED}

        self.write_identity()
        with open(os.path.join(self.state, "join-cooldown.json"), "w", encoding="utf-8") as f:
            json.dump({"lastFailedAt": time.time() - 60, "outcome": "denied"}, f)
        srv = FakeChatServer(handler)
        try:
            r = run_session_start(self.home, f"tcp:127.0.0.1:{srv.port}")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("chat-join", self.cmds(srv))
            self.assertTrue(os.path.exists(self.identity))
        finally:
            srv.close()
```

(add `import time` at the top of the file)

- [ ] **Step 2: Run to verify the flipped assertion fails**

Run: `python3 -m unittest tests.test_session_start_rejoin -v 2>&1 | tail -12`
Expected: FAIL — `test_stale_identity_rerequests_join` errors opening `identity.json` (1.2.0 deletes it).

- [ ] **Step 3: Implement**

`membership_live()` — stop deleting:

```python
def membership_live():
    """Does BCT still know this identity? A BCT restart resets the room, but
    identity.json outlives it — so ask the bridge, never trust the file. The dead
    identity is KEPT (it only ever earns NOT_INVITED, the rejoin needs the name
    beside it, and the heartbeat needs something to send); a new approval
    overwrites it. Any other error (bridge hiccup) counts as live: better silent
    than a spurious join banner."""
    r = rpc("chat-list", [], identity())      # read-only probe; consumes no messages
    return r.get("ok") or r.get("error") != NOT_INVITED
```

`session_start()` — route the request through the cooldown:

```python
    if identity() and membership_live():
        return
    obj = load(IDENTITY)
    request_join_if_allowed(obj["name"] if obj else default_name())
```

`authed()` — same gate, and no deletion:

```python
def authed(cmd, args):
    """RPC with identity; auto re-join on identity invalidation (BCT restart/eviction)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity())
    if not r.get("ok") and r.get("error") == NOT_INVITED:
        obj = load(IDENTITY)
        if obj:
            if not may_request_join():
                return r                      # cooling down — surface NOT_INVITED as-is
            print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
            do_join(obj["name"])              # blocking: a live verb wants an answer
            r = rpc(cmd, args, identity())
    return r
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests 2>&1 | tail -5`
Expected: OK — the 15 existing tests plus Task 1's 4 plus the new one.

- [ ] **Step 5: Commit**

```bash
git add scripts/bct-chat.py tests/test_session_start_rejoin.py
git commit -m "fix: keep the identity when BCT rejects it — the heartbeat and the rejoin both need it"
```

---

### Task 3: The heartbeat daemon

**Files:**
- Modify: `scripts/bct-chat.py`
- Test: `tests/test_heartbeat.py` (create)

**Interfaces:**
- Consumes: `identity()`, `rpc()`, `sock_available()`, `request_join_if_allowed()`, `NOT_INVITED`.
- Produces:
  - `SESSIONS_DIR` = `~/.bct-chat/sessions`, `PIDFILE` = `~/.bct-chat/heartbeat.pid`
  - `HEARTBEAT_INTERVAL = 240`, `HEARTBEAT_MAX_UPTIME = 43200`
  - `mark_session(sid)` / `unmark_session(sid)` / `live_sessions() -> list[str]`
  - `heartbeat_alive() -> bool` — pid file present and touched within `2 * HEARTBEAT_INTERVAL`
  - `spawn_heartbeat()` — detached, single-instance
  - `do_heartbeat(interval, max_uptime)` — the loop; verb `heartbeat [--interval N] [--max-uptime N]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_heartbeat.py`:

```python
#!/usr/bin/env python3
"""The daemon proves the host is alive so BCT's 10-min prune cannot evict it while
its claude sessions are merely quiet — and it gets out of the way the moment those
sessions are gone, the tunnel dies, or it has run too long."""
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


def start_daemon(home, sock_spec, interval="0.2", max_uptime="30"):
    env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID", "BCT_CHAT_SOCK")}
    env["HOME"] = home
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
        # Tunnel down: two consecutive failed ticks and it gets out of the way.
        self.proc = start_daemon(self.home, f"tcp:127.0.0.1:{free_port()}")
        self.assertTrue(wait_for(lambda: self.proc.poll() is not None))
        self.assertEqual(self.proc.returncode, 0)

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_heartbeat -v 2>&1 | tail -12`
Expected: every test fails — `unknown verb: heartbeat` (exit 1).

- [ ] **Step 3: Implement the daemon**

In `scripts/bct-chat.py`, add `import subprocess` to the import line, and these constants:

```python
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
HEARTBEAT_INTERVAL = 240        # 4 min — comfortably inside BCT's 10-min prune window
HEARTBEAT_MAX_UPTIME = 43200    # 12 h — backstop for a marker leaked by a crashed session
```

Add the session markers + daemon:

```python
def mark_session(sid):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    open(os.path.join(SESSIONS_DIR, sid), "w").close()


def unmark_session(sid):
    forget(os.path.join(SESSIONS_DIR, sid))


def live_sessions():
    """One marker per live claude session on this host — the daemon's refcount."""
    try:
        return os.listdir(SESSIONS_DIR)
    except OSError:
        return []


def heartbeat_alive():
    """Is a daemon running? Its pid file's mtime is refreshed every tick, so a stale
    file (crashed daemon) ages out. NEVER probe the pid with os.kill(pid, 0) — on
    Windows that TERMINATES the process."""
    try:
        return time.time() - os.stat(PIDFILE).st_mtime < 2 * HEARTBEAT_INTERVAL
    except OSError:
        return False


def spawn_heartbeat():
    if heartbeat_alive():
        return
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "heartbeat"], **kwargs)
    except OSError:
        pass                        # best-effort; never block session start


def pidfile_owner():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def do_heartbeat(interval, max_uptime):
    """Prove this host is alive while any claude session on it is. BCT prunes an
    external after 10 min of silence and (before the retire/reseat change) that
    destroyed its unread cursor — so a live-but-quiet host must keep ticking."""
    if heartbeat_alive() and pidfile_owner() != os.getpid():
        return                      # another daemon has it
    me = os.getpid()
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(me))
    started = time.time()
    fails = 0
    while True:
        if not live_sessions():
            break                   # every claude on this host is gone — get out of the way
        if time.time() - started > max_uptime:
            break                   # leaked marker backstop
        if pidfile_owner() not in (me, 0):
            break                   # a newer daemon took over
        os.utime(PIDFILE, None)     # liveness for heartbeat_alive()
        if not sock_available():
            fails += 1
        else:
            r = rpc("chat-list", [], identity())        # read-only: its only job is touch()
            if not r.get("ok") and r.get("error") == NOT_INVITED:
                obj = load(IDENTITY)
                request_join_if_allowed(obj["name"] if obj else default_name())
                fails = 0
            elif not r.get("ok") and str(r.get("error", "")).startswith("socket"):
                fails += 1
            else:
                fails = 0
                claim_pending()      # an approval may have landed since the last tick
        if fails >= 2:
            break                   # tunnel is down; the next session start respawns us
        time.sleep(interval)
    forget(PIDFILE)
```

Wire the verb in `main()` (before the final `else`):

```python
    elif verb == "heartbeat":
        interval, max_uptime = HEARTBEAT_INTERVAL, HEARTBEAT_MAX_UPTIME
        if "--interval" in rest:
            interval = float(rest[rest.index("--interval") + 1])
        if "--max-uptime" in rest:
            max_uptime = float(rest[rest.index("--max-uptime") + 1])
        do_heartbeat(interval, max_uptime)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_heartbeat -v 2>&1 | tail -10`
Expected: 5 tests PASS. If `test_exits_when_the_socket_is_unreachable` hangs, the two-strike rule is not firing — `sock_available()` must return False fast (its TCP connect timeout is 3 s).

- [ ] **Step 5: Commit**

```bash
git add scripts/bct-chat.py tests/test_heartbeat.py
git commit -m "feat: heartbeat daemon — a quiet remote proves it is alive"
```

---

### Task 4: Hook wiring — spawn on session start, unmark on session end

**Files:**
- Modify: `scripts/bct-chat.py` (`session_start`, new `session-end` verb, `hook_session_id`)
- Modify: `hooks/hooks.json`
- Test: `tests/test_session_markers.py` (create)

**Interfaces:**
- Consumes: `mark_session`, `unmark_session`, `spawn_heartbeat` (Task 3).
- Produces: `hook_session_id() -> str` (reads the hook's JSON payload from stdin; `""` when run interactively), verb `session-end`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_markers.py`:

```python
#!/usr/bin/env python3
"""SessionStart drops a marker (the daemon's refcount) and SessionEnd removes it.
The session id comes from the hook's JSON payload on stdin; an interactive run has
none and must not leave markers behind."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

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
        r = run_hook("session-end", self.home, "tcp:127.0.0.1:1",
                     json.dumps({"session_id": "sess-abc"}))
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m unittest tests.test_session_markers -v 2>&1 | tail -10`
Expected: FAIL — `unknown verb: session-end`, and no marker is written.

- [ ] **Step 3: Implement**

In `scripts/bct-chat.py`:

```python
def hook_session_id():
    """claude-code pipes the hook payload as JSON on stdin. An interactive run has a
    tty there — never block on it."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return str(json.loads(sys.stdin.read() or "{}").get("session_id", ""))
    except (OSError, ValueError):
        return ""
```

Rewrite `session_start()`:

```python
def session_start():
    """SessionStart hook: silent no-ops by design, but never silently absent — if the
    room no longer knows us, raise a fresh join request (cooldown permitting), and keep
    a heartbeat running for as long as this host has a live claude session."""
    if os.environ.get("BCT_PANE_ID"):
        return                      # BCT pane — statusline auto-invite owns this
    ensure_stable_copy()
    sid = hook_session_id()
    if not sock_available():
        return                      # no ssh session forwarding the socket
    if sid:
        mark_session(sid)           # before spawning: the daemon exits on an empty set
    if load(PENDING):
        claim_pending()
    elif not (identity() and membership_live()):
        obj = load(IDENTITY)
        request_join_if_allowed(obj["name"] if obj else default_name())
    if sid:
        spawn_heartbeat()


def session_end():
    """SessionEnd hook: drop this session's marker. The daemon is NOT killed — another
    claude session on this host may still be in the room; it exits on its own once the
    marker set empties."""
    sid = hook_session_id()
    if sid:
        unmark_session(sid)
```

In `main()`:

```python
    elif verb == "session-end":
        session_end()
```

Add the SessionEnd hook to `hooks/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" session-start || python \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" session-start",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" session-end || python \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" session-end",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Note the `||` fallback exists because `python3` on Windows is usually the Microsoft-Store stub. The hook reads stdin, so the fallback re-reads an already-consumed stdin and gets `""` → no marker. That is only reachable when `python3` genuinely fails to start, in which case the first process never consumed stdin. Leave the pattern as-is (it matches SessionStart).

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests 2>&1 | tail -5`
Expected: OK — 15 (existing) + 4 (cooldown) + 1 (rejoin cooldown case) + 5 (heartbeat) + 3 (markers).

- [ ] **Step 5: Commit**

```bash
git add scripts/bct-chat.py hooks/hooks.json tests/test_session_markers.py
git commit -m "feat: session markers — SessionStart spawns the heartbeat, SessionEnd drops the refcount"
```

---

### Task 5: Ship 1.3.0 and verify on the real Windows remote

**Files:**
- Modify: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` (1.2.0 → 1.3.0)
- Modify: `README.md`, `skills/claude-group-chat-remote/SKILL.md`

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: the released plugin. The BCT side (retire/reseat) ships independently — an un-updated BCT just sees ordinary `chat-list` pings.

- [ ] **Step 1: Bump the version**

```bash
sed -i '' 's/"version": "1.2.0"/"version": "1.3.0"/' .claude-plugin/plugin.json .claude-plugin/marketplace.json
grep -n '"version"' .claude-plugin/plugin.json .claude-plugin/marketplace.json
```

- [ ] **Step 2: Document the behaviour**

In `README.md`, under "First join — approval in BCT", add:

```markdown
## Presence — why a quiet host stays in the room

While any claude session is running on the host, the client keeps a detached
heartbeat (`bct-chat.py heartbeat`, one `chat-list` every 4 min) so BCT's 10-minute
silence prune cannot evict it between tasks. The daemon exits when the host's last
claude session ends, when the forwarded socket dies, or after 12 h.

When it does drop out, BCT retires the host rather than deleting it: the identity
and the unread cursor survive as long as the room does, so the next session start
seats it again **without an approval banner** and delivers everything it missed.

A join request that is denied — or ignored until it expires after 5 min — arms a
**30-minute cooldown**: no automatic re-request until it lapses. A human running
`bct-chat.py join` at the remote's shell bypasses it.
```

In `skills/claude-group-chat-remote/SKILL.md`, add one line to the verb table: the heartbeat is automatic and the skill should never invoke it.

- [ ] **Step 3: Push to the Windows host and verify the daemon really runs**

```bash
D="C:/Users/bglee/.bct-chat/plugin"
scp -q scripts/bct-chat.py "rsbglee:$D/scripts/bct-chat.py"
scp -q hooks/hooks.json "rsbglee:$D/hooks/hooks.json"
scp -q .claude-plugin/plugin.json "rsbglee:$D/.claude-plugin/plugin.json"
scp -q .claude-plugin/marketplace.json "rsbglee:$D/.claude-plugin/marketplace.json"
ssh rsbglee 'powershell -NoProfile -Command "claude plugin marketplace update bct-chat-remote; claude plugin update bct-chat-remote@bct-chat-remote"'
```

Then start a claude session on the host and check:

```bash
ssh rsbglee 'powershell -NoProfile -Command "Get-Content $env:USERPROFILE\.bct-chat\heartbeat.pid; Get-ChildItem $env:USERPROFILE\.bct-chat\sessions"'
```

Expected: a pid file whose mtime advances every 4 min, and one marker per live session. Leave the session idle for >10 min with the BCT dock open: the host must STAY on the roster (before this change it dropped with "연결 끊김").

- [ ] **Step 4: Full suite, then commit and PR**

```bash
python3 -m unittest discover -s tests 2>&1 | tail -3
git add -A
git commit -m "chore: bump to 1.3.0 — heartbeat presence + rejoin cooldown"
gh pr create --title "feat: heartbeat presence + 30-min rejoin cooldown (1.3.0)" --body "Implements §3, §4, §6 of the external-presence spec. BCT side (retire/reseat) ships separately."
```
