# Chat Local↔Remote Bidirectional Delivery — Kit Side — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A remote claude receives room mentions without polling — a `Stop` hook blocks the turn end with the digest when mentioned, and a `UserPromptSubmit` hook delivers it as context on the next user prompt.

**Architecture:** Both hooks share one detector: `chat-peek` (new BCT bridge verb, wire reply `"<count> <0|1>"`) decides WHETHER to deliver without touching the cursor; only then does `chat-read` consume, and the digest is composed client-side mirroring BCT's local injection shape. Everything stays in the single-file `bct-chat.py` client.

**Tech Stack:** Pure-stdlib python3 (Windows `python` fallback), unittest, Claude Code hooks (`hooks/hooks.json`).

**Spec:** `bomi-terminal/docs/superpowers/specs/2026-07-13-chat-remote-bidirectional-design.md` §4 (companion BCT plan: `bomi-terminal/docs/superpowers/plans/2026-07-14-chat-remote-bidi-bct.md`).

## Global Constraints

- Repo: `/Users/bglee/Project/bct-chat-remote`, branch `feat/turn-boundary-push` off `master`.
- `scripts/bct-chat.py` stays a SINGLE file — `ensure_stable_copy()` ships exactly one file to `~/.bct-chat/`; the 300-line modularity budget is explicitly waived here (existing deployment invariant).
- Hook verbs must ALWAYS exit 0 and never raise — hooks.json's `python3 … || python …` fallback re-runs the whole verb on ANY nonzero exit (see `session_start()`'s docstring; same `(Exception, SystemExit)` idiom).
- Test command: `python3 -m unittest discover -s tests -v` → expect `OK`.
- Requires the companion BCT build with `chat-peek`; against an older BCT, `chat-peek` returns `unknown chat verb` (an error) → hooks stay silent by design.
- Korean strings verbatim as written here.

---

### Task 1: `pending_digest` detector + `stop-hook` / `prompt-submit` verbs

**Files:**
- Modify: `scripts/bct-chat.py` (helpers after `compose`-area utilities ~`authed()`, verb dispatch in `main()`)
- Test: `tests/test_push_hooks.py` (create)

**Interfaces:**
- Consumes: existing `rpc(cmd, args, pane_id)`, `identity()` (reads `identity.json`'s `participantID`), `load(IDENTITY)` (`{"participantID", "name"}`), `sock_available()`, `NO_NEW`, `default_name()`.
- Produces: `compose_digest(name, read_text) -> str`; `pending_digest() -> str | None`; verbs `stop-hook` and `prompt-submit` in `main()`. Wire dependency: `chat-peek` → `{"ok": true, "text": "<count> <0|1>"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_push_hooks.py`:

```python
#!/usr/bin/env python3
"""Turn-boundary push: the Stop hook blocks with the digest ONLY when the room
mentions us (peek → read), and both hook verbs are silent + exit-0 on every
failure path — a broken tunnel must never break claude's turn end."""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class PushHookTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.sock_available = lambda: True
        self.mod.drain_stdin = lambda: None          # unittest stdin is not a hook pipe
        os.environ.pop("BCT_PANE_ID", None)

    def run_verb(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def rpc_map(self, responses):
        calls = []
        def fake(cmd, args, pane_id=""):
            calls.append(cmd)
            return responses[cmd]
        self.mod.rpc = fake
        return calls

    def test_mention_blocks_with_digest(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "2 1"},
                      "chat-read": {"ok": True, "text": "yoros: @svr 봐줘\nnavy: 진행중"}})
        out = self.run_verb(self.mod.stop_hook)
        obj = json.loads(out)
        self.assertEqual(obj["decision"], "block")
        self.assertIn("당신은 @svr", obj["reason"])
        self.assertIn("yoros: @svr 봐줘", obj["reason"])
        self.assertIn("bct-chat.py send", obj["reason"])

    def test_no_mention_allows_and_does_not_read(self):
        calls = self.rpc_map({"chat-peek": {"ok": True, "text": "3 0"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.assertEqual(calls, ["chat-peek"])       # cursor untouched — no chat-read

    def test_socket_absent_is_silent(self):
        self.mod.sock_available = lambda: False
        self.mod.rpc = lambda *a, **k: self.fail("rpc must not be called")
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_bct_pane_guard(self):
        os.environ["BCT_PANE_ID"] = "deadbeef"
        try:
            self.mod.rpc = lambda *a, **k: self.fail("rpc must not be called")
            self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        finally:
            os.environ.pop("BCT_PANE_ID", None)

    def test_peek_error_or_old_bct_is_silent(self):
        self.rpc_map({"chat-peek": {"ok": False, "error": "unknown chat verb: chat-peek"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_read_failure_after_peek_is_silent(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "1 1"},
                      "chat-read": {"ok": False, "error": "socket error: boom"}})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")

    def test_prompt_submit_prints_plain_digest(self):
        self.rpc_map({"chat-peek": {"ok": True, "text": "1 1"},
                      "chat-read": {"ok": True, "text": "yoros: @svr 어때"}})
        out = self.run_verb(self.mod.prompt_submit_hook)
        self.assertTrue(out.startswith("[bct-chat]"))
        self.assertNotIn('"decision"', out)          # context, not control JSON


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bglee/Project/bct-chat-remote && python3 -m unittest tests.test_push_hooks -v
```
Expected: ERROR — `module has no attribute 'stop_hook'` (and `drain_stdin`).

- [ ] **Step 3: Implement in `scripts/bct-chat.py`** (insert after `authed()`, before `die()`)

```python
REPLY_HINT = ('당신이 멘션되었습니다 — `python3 ~/.bct-chat/bct-chat.py send "<답변>"` 으로 답하세요. '
              '(명단: `python3 ~/.bct-chat/bct-chat.py list`, 새 메시지 확인: '
              '`python3 ~/.bct-chat/bct-chat.py read`)')


def compose_digest(name, read_text):
    """Mirror BCT's local chatInjection shape: identity line, the unseen lines
    exactly as chat-read returned them, then the reply instruction."""
    lines = [f"[bct-chat] 단체 채팅방 — 당신은 @{name} 입니다. 새 메시지:"]
    lines += [l for l in read_text.splitlines() if l]
    lines.append(REPLY_HINT)
    return "\n".join(lines)


def drain_stdin():
    """Hook payloads arrive on stdin; read them off so the writer never blocks,
    but never block on a tty ourselves."""
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass


def pending_digest():
    """The digest to deliver, or None. chat-peek decides (cursor-preserving —
    a non-mention backlog stays unseen for a future mention delivery, same
    semantics as BCT's local push); only a mentioned backlog is consumed via
    chat-read. Every failure path — no socket, no identity, an old BCT without
    chat-peek, a read error — is None: the hooks must never disturb a turn."""
    if os.environ.get("BCT_PANE_ID"):
        return None                  # BCT-pane claude — native push owns delivery
    if not sock_available() or not identity():
        return None
    r = rpc("chat-peek", [], identity())
    parts = (r.get("text") or "").split() if r.get("ok") else []
    if len(parts) != 2 or parts[1] != "1":
        return None
    rd = rpc("chat-read", [], identity())
    text = rd.get("text") or ""
    if not rd.get("ok") or not text or text == NO_NEW:
        return None
    obj = load(IDENTITY) or {}
    return compose_digest(obj.get("name", default_name()), text)


def stop_hook():
    """Stop hook: block the turn end with the digest when mentioned — claude
    answers the room in place. Always exits 0 (see session_start docstring)."""
    drain_stdin()
    try:
        d = pending_digest()
        if d:
            print(json.dumps({"decision": "block", "reason": d}, ensure_ascii=False))
    except (Exception, SystemExit):
        pass


def prompt_submit_hook():
    """UserPromptSubmit hook: same detection, but the digest rides along as
    CONTEXT (plain stdout) with the user's prompt — covers a fully idle claude
    the moment the user next engages. Always exits 0."""
    drain_stdin()
    try:
        d = pending_digest()
        if d:
            print(d)
    except (Exception, SystemExit):
        pass
```

In `main()`, add the verbs (after the `session-end` branch) and extend the usage line:

```python
    elif verb == "stop-hook":
        stop_hook()
    elif verb == "prompt-submit":
        prompt_submit_hook()
```

```python
        die("usage: bct-chat.py <join|send|read|wait|list|leave|session-start|session-end|stop-hook|prompt-submit> …")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m unittest tests.test_push_hooks -v
```
Expected: `OK` (7 tests).

- [ ] **Step 5: Full kit suite**

```bash
python3 -m unittest discover -s tests -v
```
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add scripts/bct-chat.py tests/test_push_hooks.py
git commit -m "feat: stop-hook/prompt-submit verbs — turn-boundary mention delivery via chat-peek"
```

---

### Task 2: Hook registration

**Files:**
- Modify: `hooks/hooks.json`
- Test: `tests/test_push_hooks.py` (append one test)

**Interfaces:**
- Consumes: Task 1's verbs.
- Produces: `Stop` and `UserPromptSubmit` entries Claude Code discovers at plugin load.

- [ ] **Step 1: Write the failing test** (append to `PushHookTests`)

```python
    def test_hooks_json_registers_push_hooks(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        for event, verb in (("Stop", "stop-hook"), ("UserPromptSubmit", "prompt-submit")):
            cmd = hooks[event][0]["hooks"][0]["command"]
            self.assertIn(verb, cmd)
            self.assertIn("|| python ", cmd)         # Windows MS-Store-stub fallback
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m unittest tests.test_push_hooks -v` → KeyError: 'Stop'.

- [ ] **Step 3: Implement** — in `hooks/hooks.json`, add alongside `SessionStart`/`SessionEnd` (same wrapper discipline):

```json
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" stop-hook || python \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" stop-hook",
            "timeout": 15
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" prompt-submit || python \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" prompt-submit",
            "timeout": 15
          }
        ]
      }
    ]
```

- [ ] **Step 4: Run tests** — `python3 -m unittest tests.test_push_hooks -v` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add hooks/hooks.json tests/test_push_hooks.py
git commit -m "feat: register Stop/UserPromptSubmit hooks for turn-boundary delivery"
```

---

### Task 3: Docs + version

**Files:**
- Modify: `skills/claude-group-chat-remote/SKILL.md` (Etiquette "Reception is PULL" bullet), `README.md`, `CHANGELOG.md`

- [ ] **Step 1: SKILL.md** — replace the reception bullet with:

```markdown
- Reception is pull-with-a-nudge: mentions reach you automatically at TURN
  BOUNDARIES (a Stop hook re-engages you with the digest) and alongside the
  user's next prompt (UserPromptSubmit). Between those, nothing interrupts you —
  check in with `read` between tasks or sit in `wait` when told to standby.
```

- [ ] **Step 2: README.md** — under the bundle list, extend the hook bullet:

```markdown
- `Stop`/`UserPromptSubmit` hooks that deliver room mentions at turn boundaries
  (requires a BCT build with the `chat-peek` verb; older BCTs → hooks stay silent)
```

- [ ] **Step 3: CHANGELOG.md** — add at top:

```markdown
## 1.4.0

- Turn-boundary mention delivery: `stop-hook` blocks a finishing turn with the
  room digest when you are mentioned (peek → read, cursor-preserving detection);
  `prompt-submit` rides the digest along as context on the user's next prompt.
  Requires the companion BCT release with the `chat-peek` bridge verb.
```

- [ ] **Step 4: Full suite + commit**

```bash
python3 -m unittest discover -s tests -v
git add skills/claude-group-chat-remote/SKILL.md README.md CHANGELOG.md
git commit -m "docs: turn-boundary delivery notes; bump to 1.4.0"
```

---

## Self-review notes

- Spec §4 fully covered: Stop hook (Task 1), UserPromptSubmit (Task 1), registration (Task 2), digest shape client-side (Task 1 `compose_digest`), always-exit-0 invariant (tests for socket-absent/error paths), loop convergence relies on cursor consumption + BCT's room pause (no `stop_hook_active` special-casing — by design, spec §4).
- Wire parsing matches the BCT plan's Task 3 output exactly: `"<count> <0|1>"` split on whitespace, `parts[1] == "1"`.
- `BCT_PANE_ID` guard mirrors `session_start()` — a claude inside a BCT pane gets native pane push, never hook delivery.
