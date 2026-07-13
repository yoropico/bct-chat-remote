# Chat Standby Push — Kit Side — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `listen` verb that a standby remote claude runs in a loop — it holds a server-push connection (BCT's `chat-listen`) and prints a mention the instant it is posted, byte-accurately, with zero client polling.

**Architecture:** `listen` calls the companion BCT `chat-listen` verb, which blocks server-side until a mention arrives (≤30 s) or times out. Because the hold exceeds the client's default 10 s socket timeout, `connect`/`rpc`/`authed` gain an optional longer `timeout`. One `listen` call = one claude turn; the skill's standby loop re-invokes it.

**Tech Stack:** Pure-stdlib python3 (Windows `python` fallback), unittest, Claude Code plugin.

**Spec:** `bomi-terminal/docs/superpowers/specs/2026-07-14-chat-standby-push-design.md` §4–5 (companion BCT plan: `bomi-terminal/docs/superpowers/plans/2026-07-14-chat-standby-push-bct.md`).

## Global Constraints

- Repo `/Users/bglee/Project/bct-chat-remote`, branch `feat/chat-standby-push` off `main` (already created).
- `scripts/bct-chat.py` stays a SINGLE file (deployment invariant; 300-line budget waived).
- Test: `python3 -m unittest discover -s tests -v` → `OK`.
- Requires the companion BCT build with `chat-listen`; against an older BCT it returns `unknown chat verb` (an error) → `listen` surfaces it via the normal error path.
- Korean strings verbatim. CHANGELOG entry in the SAME commit as the version bump (devmode gate). Version → 1.5.0.

---

### Task 1: `listen` verb + longer-timeout transport

**Files:**
- Modify: `scripts/bct-chat.py` (`connect`, `rpc`, `authed` — add `timeout`; add `NO_MENTION` const; add `listen` verb in `main()` + usage line)
- Test: `tests/test_listen.py` (create)

**Interfaces:**
- Consumes: existing `rpc`/`authed`/`identity`/`NO_NEW`/`die`.
- Produces: `connect(timeout=10)`, `rpc(cmd, args, pane_id="", timeout=10)`, `authed(cmd, args, timeout=10)`; `NO_MENTION = "(새 멘션 없음)"`; verb `listen`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_listen.py`:

```python
#!/usr/bin/env python3
"""`listen` = one server-push turn: print a returned mention digest, stay silent
on the reconnect sentinel, surface errors. A held connection needs a longer
socket timeout than the default 10s."""
import contextlib, io, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class ListenTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})

    def run_main(self, argv):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.mod.main(argv)
        except SystemExit:
            pass
        return buf.getvalue()

    def test_prints_returned_mention(self):
        seen = {}
        def fake(cmd, args, timeout=10):
            seen["cmd"], seen["timeout"] = cmd, timeout
            return {"ok": True, "text": "yoros: @svr 봐줘"}
        self.mod.authed = fake
        out = self.run_main(["listen"])
        self.assertIn("yoros: @svr 봐줘", out)
        self.assertEqual(seen["cmd"], "chat-listen")
        self.assertGreaterEqual(seen["timeout"], 35)      # long hold tolerated

    def test_silent_on_reconnect_sentinel(self):
        self.mod.authed = lambda c, a, timeout=10: {"ok": True, "text": self.mod.NO_MENTION}
        self.assertEqual(self.run_main(["listen"]).strip(), "")

    def test_silent_on_no_new(self):
        self.mod.authed = lambda c, a, timeout=10: {"ok": True, "text": self.mod.NO_NEW}
        self.assertEqual(self.run_main(["listen"]).strip(), "")

    def test_rpc_accepts_timeout_kwarg(self):
        # connect/rpc must accept the kwarg without connecting (socket absent).
        self.mod.SOCK = os.path.join(self.home, "nope.sock")
        r = self.mod.rpc("chat-listen", [], IDENT, timeout=40)
        self.assertFalse(r.get("ok"))                     # no socket, but no TypeError
```

- [ ] **Step 2: Run to verify failure**

```bash
cd /Users/bglee/Project/bct-chat-remote && python3 -m unittest tests.test_listen -v
```
Expected: errors — `NO_MENTION` missing / `rpc() got unexpected keyword 'timeout'` / no `listen` verb.

- [ ] **Step 3: Implement in `scripts/bct-chat.py`**

Replace `connect`:

```python
def connect(timeout=10):
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    return s
```

Change `rpc`'s signature + its `connect()` call:

```python
def rpc(cmd, args, pane_id="", timeout=10):
```
and inside, `s = connect()` → `s = connect(timeout)`.

Change `authed`'s signature + its two `rpc(...)` calls to thread the timeout:

```python
def authed(cmd, args, timeout=10):
```
and both `r = rpc(cmd, args, identity())` → `r = rpc(cmd, args, identity(), timeout=timeout)`.

Add the constant next to `NO_NEW` (near the top):

```python
NO_MENTION = "(새 멘션 없음)"          # chat-listen timeout sentinel (server push)
```

Add the verb in `main()` after the `wait` branch:

```python
    elif verb == "listen":
        # Server-push standby: chat-listen holds the connection until a mention
        # is posted (or ~30s server-side). One call = one turn; the standby loop
        # re-invokes. 40s socket timeout tolerates the hold + the 35s conn cap.
        r = authed("chat-listen", [], timeout=40)
        if not r.get("ok"):
            die(r.get("error", "error"))
        txt = r.get("text", "")
        if txt and txt not in (NO_NEW, NO_MENTION):
            print(txt)
```

Extend the usage line in `main()` to include `listen`:

```python
        die("usage: bct-chat.py <join|send|read|wait|listen|list|leave|session-start|session-end|stop-hook|prompt-submit> …")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m unittest tests.test_listen -v
```
Expected: `OK` (4 tests).

- [ ] **Step 5: Full suite**

```bash
python3 -m unittest discover -s tests -v
```
Expected: `OK` (the timeout kwarg is additive; existing callers pass no timeout → default 10).

- [ ] **Step 6: Commit**

```bash
git add scripts/bct-chat.py tests/test_listen.py
git commit -m "feat: listen verb — server-push standby via chat-listen (long-poll)"
```

---

### Task 2: Skill standby doc + 1.5.0 release

**Files:**
- Modify: `skills/claude-group-chat-remote/SKILL.md`, `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`

- [ ] **Step 1: SKILL.md** — in the Commands block add the `listen` line, and add a Standby note. After the `wait` command line:

```markdown
python3 ~/.bct-chat/bct-chat.py listen                # standby: blocks until a mention is PUSHED (instant), prints it
```

And append to the Etiquette section:

```markdown
- **Standby (실시간 대기):** when told to stand by in the room, run `listen` in a loop —
  it holds a server-push connection and returns the instant you are mentioned (no 2s poll).
  Each return is one turn: handle the mention, reply with `send`, then run `listen` again.
  An empty return is a reconnect timeout — just run `listen` again. Requires a BCT build with
  the `chat-listen` verb (older BCT → `listen` errors; use `wait` instead).
```

- [ ] **Step 2: README.md** — under the bundle/verbs description, add a bullet:

```markdown
- `listen` — standby server-push: blocks until you are mentioned, delivered instantly and
  byte-accurately over the socket (no polling); run it in a loop to stand by in the room.
```

- [ ] **Step 3: CHANGELOG.md** — add at top:

```markdown
## 1.5.0 — 2026-07-14

- Standby server push: a new `listen` verb holds a `chat-listen` connection that BCT pushes
  to the instant you are mentioned — zero polling latency, byte-accurate over the socket.
  Run it in a loop to stand by in the room. Requires the companion BCT release with the
  `chat-listen` bridge verb.
```

- [ ] **Step 4: Version bump** — set `"version": "1.5.0"` in BOTH `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`. Extend `plugin.json`'s `description` to mention standby push alongside the existing hooks.

- [ ] **Step 5: Verify JSON + full suite**

```bash
python3 -c "import json; [json.load(open(p)) for p in ['.claude-plugin/plugin.json', '.claude-plugin/marketplace.json', 'hooks/hooks.json']]; print('json ok')"
python3 -m unittest discover -s tests -v
```
Expected: `json ok`, then `OK`.

- [ ] **Step 6: Commit**

```bash
git add skills/claude-group-chat-remote/SKILL.md README.md CHANGELOG.md .claude-plugin/plugin.json .claude-plugin/marketplace.json
git commit -m "chore: standby listen docs + bump to 1.5.0"
```

---

## Self-review notes

- Spec §4 → Task 1 (`listen` verb, byte-accurate, timeout sentinel). §5 → Task 2 (skill standby loop).
- `NO_MENTION` = `"(새 멘션 없음)"` matches the BCT plan's `ChatListenerRegistry.reconnectSentinel` verbatim.
- The `timeout` kwarg is additive with a default, so every existing `rpc`/`authed` call site is unchanged; only `listen` passes the long 40 s.
- Loop convergence: each `listen` consumes via the shared cursor server-side, so a delivered mention is not re-pushed; the skill's loop is claude-driven (one turn per return).
