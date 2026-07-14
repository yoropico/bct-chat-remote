# Remote-chat receive rework (daemon-as-ear + local inbox) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework `bct-chat-remote` so a mention is never lost: the always-running daemon becomes the only listener and writes every mention to a durable local inbox, while the Claude Code hooks become local-only readers of that inbox.

**Architecture:** Split CAPTURE (one daemon per host, holds `chat-listen`, writes `~/.bct-chat/inbox/<ts>-<pid>.json` before issuing the next listen) from DELIVERY (Stop / UserPromptSubmit hooks: zero RPC, atomic `os.rename` claim, compose digest, print). The 635-line single file becomes modular source under `src/bctchat/` plus a *generated* single-file artifact `scripts/bct-chat.py` (the single-file property is load-bearing for `scp`, the stable copy, `__file__` re-exec, and `REPLY_HINT`). Membership moves from a flat 30-min cooldown to a bounded join budget that suspends after 3 refusals.

**Tech Stack:** Python 3 stdlib only (no third-party imports, ever). `unittest` for tests. GitHub Actions for CI (ubuntu/macos/windows).

**Spec:** `docs/superpowers/specs/2026-07-14-chat-remote-inbox-receive-design.md` (approved). Read §3–§8 before starting.

## Global Constraints

- **Pure stdlib.** No dependency may be added to the client. `ctypes` is stdlib and is allowed.
- **The artifact is generated, never hand-edited.** `scripts/bct-chat.py` is the output of `python3 scripts/build.py`. Every source change is made in `src/bctchat/*.py` and the artifact is regenerated and committed in the same commit. `tests/test_build.py` enforces this.
- **The artifact is a single namespace** (concatenation, not a package). Never emit `from bctchat.wire import rpc` into the artifact — the existing tests monkeypatch module globals (`mod.rpc`, `mod.sock_available`, `mod.subprocess`) and a `from`-import would silently break that while the suite stayed green.
- **Never call `os.kill(pid, 0)` on Windows** — CPython's `os.kill` there calls `TerminateProcess` for any signal, i.e. it would kill the process it is probing. Use `proc_alive()` (Task 2).
- **Hook verbs always exit 0.** `hooks.json` falls back `python3 … || python …` on any nonzero exit, which re-runs the hook with stdin already drained. `session-start`, `session-end`, `stop-hook`, `prompt-submit` must swallow every `Exception` *and* `SystemExit`.
- **Stop/UserPromptSubmit hooks perform ZERO RPC.** After Task 7 no hook verb may open a socket. This is what kills defect class D7 (hook timeout < RPC budget) permanently.
- **Every write under the state dir is atomic** (temp file + `os.replace`, which is atomic on Windows too).
- Test isolation is `BCT_CHAT_HOME`, never `HOME` — `ntpath.expanduser()` ignores `HOME` on Windows and would clobber the developer's real `~/.bct-chat`.
- Korean user-facing strings stay Korean, byte-for-byte where they already exist (`NO_NEW = "(새 메시지 없음)"`, `NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"`, `REPLY_HINT`). Code comments and commit messages are English.
- Regression gate: `python3 -m unittest discover -s tests` — must be green before every commit. Baseline today: 63 tests.
- BCT's wire protocol does not change. Only existing verbs: `chat-join`, `chat-join-poll`, `chat-list`, `chat-read`, `chat-peek`, `chat-listen`, `chat-leave`, `chat-send`.

## Spec refinements (decided during planning — deviations from the spec text)

1. **`session-start` becomes local-only too** (spec §4.4 still had it issuing joins). It now does exactly: `ensure_stable_copy()` → `mark_session(sid)` → `ensure_daemon()`. The daemon requests membership on its first tick, so the join still happens within a second of session start, and the SessionStart hook can no longer be killed mid-RPC. This deletes the last hook-side socket call and makes D3 (no socket at SessionStart) vanish rather than be patched.
2. **Marker GC needs a *claude* pid, which a hook cannot read directly.** The hook process's parent is the `sh -c` wrapper (hooks.json uses `||`), which exits immediately. So `mark_session()` resolves the grandparent via a single `ps` call on POSIX and stores it; on Windows (no cheap ancestor walk) it stores `0`. GC rule: `pid > 0 and not proc_alive(pid)` → remove; `pid == 0` → fall back to a 7-day mtime TTL (every hook of that session refreshes the mtime). A lingering marker only costs a phantom seat; evicting a *live* session's marker would cost its capture layer — so the fallback deliberately errs long. Documented in the CHANGELOG's limitations.

---

## File structure

| File | Responsibility |
|---|---|
| `src/bctchat/config.py` | paths (incl. `BCT_CHAT_HOME`), sentinels, tunables, `chat_mode()` |
| `src/bctchat/wire.py` | `tcp_target`, `connect`, `sock_available`, `rpc` |
| `src/bctchat/state.py` | atomic `load/save/forget`, `proc_alive`, session markers, `ensure_stable_copy` |
| `src/bctchat/inbox.py` | `inbox_put/claim/ack/wait`, orphan recovery, cap + drop counter |
| `src/bctchat/membership.py` | identity, pending + TTL, join budget, `ensure_membership`, `do_join`, `authed`, `do_leave` |
| `src/bctchat/presence.py` | pidfile, `heartbeat_alive`, `ensure_daemon`, marker GC, the daemon loop |
| `src/bctchat/delivery.py` | hook payload, digest, chain counter, the four hook verbs |
| `src/bctchat/cli.py` | argparse, verbs, `main` |
| `scripts/build.py` | strips intra-package imports, concatenates → `scripts/bct-chat.py` |
| `scripts/bct-chat.py` | **generated artifact** (committed; what ships and what the tests exec) |
| `tests/test_build.py` | the committed artifact is in sync with `src/` |
| `tests/test_unix_transport.py` | AF_UNIX transport + zombie socket (today: zero coverage) |
| `tests/test_inbox.py` | put / claim race / ack / wait / orphans / cap |
| `tests/test_membership.py` | budget, PENDING TTL, `authed` no-double-join, leave sticks |
| `.github/workflows/ci.yml` | ubuntu / macos / windows matrix |

Rewritten in place: `tests/test_heartbeat.py` (daemon), `tests/test_push_hooks.py` + `tests/test_standby_window.py` (delivery), `tests/test_cooldown.py` + `tests/test_session_start_rejoin.py` (membership), `tests/test_listen.py` (inbox-backed verbs).

---

### Task 1: Module split + generated artifact (no behaviour change)

Mechanical refactor. The 63 existing tests must stay green *without being edited* — that is the proof the split changed nothing.

**Files:**
- Create: `src/bctchat/config.py`, `wire.py`, `state.py`, `membership.py`, `presence.py`, `delivery.py`, `cli.py`
- Create: `scripts/build.py`
- Create: `tests/test_build.py`
- Modify: `scripts/bct-chat.py` (becomes generated output — byte content changes, behaviour does not)

**Interfaces:**
- Consumes: nothing.
- Produces: `python3 scripts/build.py` regenerates the artifact; `build_artifact() -> str` in `scripts/build.py`; module list `MODULES`.

- [ ] **Step 1: Write the failing build-sync test**

```python
# tests/test_build.py
#!/usr/bin/env python3
"""The shipped artifact is generated from src/. A stale artifact is a bug: the
tests, the scp target, the stable copy and the plugin hook all exec the artifact,
so src/ drifting from it means the reviewed code is not the running code."""
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(REPO, "scripts", "build.py")
ARTIFACT = os.path.join(REPO, "scripts", "bct-chat.py")


class BuildTests(unittest.TestCase):
    def test_committed_artifact_matches_src(self):
        r = subprocess.run([sys.executable, BUILD, "--stdout"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(ARTIFACT, encoding="utf-8") as f:
            self.assertEqual(f.read(), r.stdout,
                             "scripts/bct-chat.py is stale — run: python3 scripts/build.py")

    def test_artifact_has_no_intra_package_imports(self):
        with open(ARTIFACT, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("from bctchat", src)
        self.assertNotIn("import bctchat", src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `python3 -m unittest tests.test_build -v`
Expected: FAIL — `scripts/build.py` does not exist (`returncode != 0`).

- [ ] **Step 3: Create the source modules by moving code verbatim**

Split the current `scripts/bct-chat.py` with **zero edits to any function body**. Each module starts with a one-line docstring and imports only stdlib plus intra-package names via `from bctchat.<mod> import *`-style explicit imports (they are stripped at build time, so their only job is to make `src/` importable for a linter/IDE — the artifact never sees them).

Content mapping (source line numbers refer to the current `scripts/bct-chat.py`):

- `config.py` — the module docstring (lines 2–10), the `sys.stdout.reconfigure` block (13–16), `STATE_DIR`, `IDENTITY`, `PENDING`, `COOLDOWN`, `JOIN_COOLDOWN`, `SOCK`, `NO_NEW`, `NO_MENTION`, `NOT_INVITED`, `SESSIONS_DIR`, `PIDFILE`, `HEARTBEAT_INTERVAL`, `HEARTBEAT_MAX_UPTIME` (18–30), `STABLE` (141), `SESSION_ID_RE` (335), `REPLY_HINT` (441–443). Plus, new, at the very top of the file body:

```python
ARTIFACT = os.path.abspath(__file__)   # the concatenated single file; what spawn_heartbeat re-execs
```

  (In the artifact, `config.py`'s `__file__` *is* the artifact — this is what keeps the `__file__` re-exec contract intact after the split.)
- `wire.py` — `tcp_target`, `default_name`, `sock_available`, `connect`, `rpc` (33–86)
- `state.py` — `load`, `save`, `forget`, `ensure_stable_copy`, `mark_session`, `unmark_session`, `live_sessions` (89–107, 144–164, 200–214)
- `membership.py` — `cooldown_remaining`, `may_request_join`, `note_join_failure`, `clear_cooldown`, `request_join_if_allowed`, `identity`, `membership_live`, `claim_pending`, `do_join`, `authed` (110–138, 167–197, 314–332, 425–438)
- `presence.py` — `heartbeat_alive`, `spawn_heartbeat`, `pidfile_owner`, `do_heartbeat` (217–311)
- `delivery.py` — `hook_session_id`, `session_start`, `session_end`, `compose_digest`, `drain_stdin`, `pending_digest`, `standby_enabled`, `standby_listen_digest`, `stop_hook`, `prompt_submit_hook` (338–422, 446–538)
- `cli.py` — `die`, `main` (541–631)

Every module begins with:

```python
import json, os, re, socket, subprocess, sys, time
```

(duplicated stdlib imports in the concatenation are a no-op — this keeps each module independently importable without the build having to reason about which names each one needs).

`spawn_heartbeat()` in `presence.py` changes exactly one expression — `os.path.abspath(__file__)` becomes `ARTIFACT`:

```python
        subprocess.Popen([sys.executable, ARTIFACT, "heartbeat"], **kwargs)
```

- [ ] **Step 4: Write the build script**

```python
#!/usr/bin/env python3
"""Concatenate src/bctchat/*.py into the single-file artifact scripts/bct-chat.py.

The artifact is ONE namespace, not a package: the tests, and any operator with a
python REPL, monkeypatch module globals (mod.rpc, mod.sock_available). A real
`from bctchat.wire import rpc` would bind those names at import time and silently
defeat every such patch — so intra-package imports are stripped, never emitted.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "bctchat")
ARTIFACT = os.path.join(REPO, "scripts", "bct-chat.py")

# Concatenation order = dependency order. config first (it defines ARTIFACT/paths),
# cli last (it defines main()).
MODULES = ["config", "wire", "state", "inbox", "membership", "presence", "delivery", "cli"]

HEADER = "#!/usr/bin/env python3\n"
FOOTER = '\n\nif __name__ == "__main__":\n    main(sys.argv[1:])\n'


def is_intra_import(line):
    s = line.strip()
    return s.startswith("from bctchat") or s.startswith("import bctchat")


def build_artifact():
    out = [HEADER]
    for name in MODULES:
        path = os.path.join(SRC, name + ".py")
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.readlines() if not is_intra_import(l)]
        body = "".join(lines).strip("\n")
        out.append(f"\n\n# ---- {name} " + "-" * (66 - len(name)) + "\n" + body + "\n")
    return "".join(out).rstrip("\n") + FOOTER


def main(argv):
    text = build_artifact()
    if "--stdout" in argv:
        sys.stdout.write(text)
        return
    tmp = ARTIFACT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, ARTIFACT)
    os.chmod(ARTIFACT, 0o755)
    print(f"wrote {ARTIFACT} ({len(text)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
```

Note: `inbox` is already in `MODULES` — Task 4 creates it. Until then, create an empty placeholder `src/bctchat/inbox.py` containing only a docstring, so the build works from Task 1 onward.

- [ ] **Step 5: Generate the artifact and run the FULL suite unchanged**

Run:
```bash
python3 scripts/build.py
python3 -m unittest discover -s tests
```
Expected: `Ran 64 tests ... OK` (63 existing + the 2 build tests, minus none — expect ≥65; the count only ever grows). Every pre-existing test passes **without being edited**. If any pre-existing test fails, the split changed behaviour — fix the split, never the test.

- [ ] **Step 6: Verify the artifact still runs standalone**

Run: `python3 scripts/bct-chat.py 2>&1 | head -1`
Expected: the usage line, `usage: bct-chat.py <join|send|read|...`

- [ ] **Step 7: Commit**

```bash
git add src scripts/build.py scripts/bct-chat.py tests/test_build.py
git commit -m "refactor: modular src/bctchat + generated single-file artifact (no behaviour change)"
```

---

### Task 2: State hardening — `BCT_CHAT_HOME`, atomic writes, `proc_alive`

**Files:**
- Modify: `src/bctchat/config.py` (STATE_DIR), `src/bctchat/state.py`
- Modify: every file under `tests/` that sets `HOME` (all of them) → `BCT_CHAT_HOME`
- Test: `tests/test_state.py` (create)

**Interfaces:**
- Consumes: Task 1's modules.
- Produces:
  - `STATE_DIR = os.environ.get("BCT_CHAT_HOME") or os.path.expanduser("~/.bct-chat")`
  - `atomic_write(path: str, text: str) -> None`
  - `save(path: str, obj) -> None` (now atomic)
  - `proc_alive(pid: int) -> bool` — POSIX `os.kill(pid, 0)`; Windows `OpenProcess`; `pid <= 0` → `False`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
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
from test_heartbeat import load_fresh_module  # noqa: E402


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
        self.assertTrue(self.mod.proc_alive(os.getpid()))
        self.assertFalse(self.mod.proc_alive(999999))
        self.assertFalse(self.mod.proc_alive(0))
        self.assertFalse(self.mod.proc_alive(-1))

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_state -v`
Expected: FAIL — `STATE_DIR` still derives from `HOME`; `proc_alive` is not defined.

- [ ] **Step 3: Implement**

`src/bctchat/config.py` — replace the `STATE_DIR` line:

```python
# BCT_CHAT_HOME overrides the state dir. This is what makes the test suite safe on
# Windows: ntpath.expanduser() ignores HOME and reads USERPROFILE, so an HOME-isolated
# test would run against the developer's REAL ~/.bct-chat and SIGKILL their live daemon.
STATE_DIR = os.environ.get("BCT_CHAT_HOME") or os.path.expanduser("~/.bct-chat")
```

`src/bctchat/state.py` — add / replace:

```python
def atomic_write(path, text):
    """temp + os.replace: os.replace is atomic on POSIX and on Windows. A hook killed
    mid-write can then never leave a 0-byte identity.json or a truncated stable copy
    (which is the very file the skill tells claude to run)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        forget(tmp)
        raise


def save(path, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    atomic_write(path, json.dumps(obj, ensure_ascii=False))


def proc_alive(pid):
    """Is this pid a live process? NEVER os.kill(pid, 0) on Windows — CPython maps
    os.kill to TerminateProcess there for ANY signal, i.e. probing would kill it."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return False
```

`ensure_stable_copy()` — replace its write with the atomic one, move it inside the caller's guard (the move happens in Task 7; here just make the write atomic) and widen the guard:

```python
def ensure_stable_copy():
    """Plugin installs live under a versioned cache path; keep one canonical copy at
    ~/.bct-chat/bct-chat.py for the skill prose and manual use. Guarded against EVERY
    exception, not just OSError: a truncated (pre-atomic-write) copy raises
    UnicodeDecodeError, which would exit the hook nonzero and trigger hooks.json's
    `|| python` re-run with stdin already drained."""
    me = ARTIFACT
    if me == os.path.abspath(STABLE):
        return
    try:
        with open(me, encoding="utf-8") as f:
            src = f.read()
        try:
            with open(STABLE, encoding="utf-8") as f:
                if f.read() == src:
                    return
        except Exception:
            pass
        atomic_write(STABLE, src)
        os.chmod(STABLE, 0o755)
    except Exception:
        pass                        # best-effort; never block session start
```

- [ ] **Step 4: Migrate every test to `BCT_CHAT_HOME`**

In `tests/test_heartbeat.py`, `load_fresh_module()` and `start_daemon()`, and in `tests/test_tcp_transport.py`, `run_client()`: set `BCT_CHAT_HOME=<home>/.bct-chat` **in addition to** `HOME=<home>` (keeping `HOME` costs nothing on POSIX and the explicit var is what makes Windows correct). `load_fresh_module` becomes:

```python
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
```

and in every `subprocess` helper (`start_daemon`, `run_client`) add `env["BCT_CHAT_HOME"] = os.path.join(home, ".bct-chat")`.

- [ ] **Step 5: Rebuild, run the full suite**

Run:
```bash
python3 scripts/build.py && python3 -m unittest discover -s tests
```
Expected: OK, all green (test count now ~70).

- [ ] **Step 6: Commit**

```bash
git add -A src tests scripts/bct-chat.py
git commit -m "fix(state): BCT_CHAT_HOME isolation, atomic writes, Windows-safe proc_alive"
```

---

### Task 3: Wire hardening — real availability probe, bounded `rpc`, AF_UNIX coverage

Fixes D4 (zombie socket reads as healthy), D16 (`rpc` has no overall deadline, cannot tolerate a keepalive or two frames in one read, misreports a silent close), D18 (the production transport has zero tests).

**Files:**
- Modify: `src/bctchat/wire.py`
- Test: `tests/test_unix_transport.py` (create), `tests/test_wire.py` (create)

**Interfaces:**
- Produces:
  - `tcp_target(spec: str) -> tuple[str, int] | None` — validates the port range, understands `tcp:[::1]:9000`
  - `sock_available() -> bool` — **connects** on both transports
  - `rpc(cmd, args, pane_id="", timeout=10) -> dict` — overall deadline; skips blank keepalive lines; parses the first complete JSON line; a server that closes without replying is a socket error, not "malformed response"

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_wire.py
#!/usr/bin/env python3
"""The wire is the only thing between us and a bridge we do not control: it must
bound its own time, tolerate a keepalive or a coalesced frame, and tell a closed
connection apart from a corrupt one."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402


class RawServer:
    """Answers one request with exactly the bytes it was given (or nothing)."""

    def __init__(self, script):
        self.script = script                # bytes to send, or None to close silently
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                try:
                    conn.recv(65536)
                    if self.script is not None:
                        conn.sendall(self.script)
                except OSError:
                    pass

    def close(self):
        self.sock.close()


class WireTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def mod_for(self, port):
        os.environ["BCT_CHAT_SOCK"] = f"tcp:127.0.0.1:{port}"
        try:
            return load_fresh_module(self.home)
        finally:
            os.environ.pop("BCT_CHAT_SOCK", None)

    def test_tcp_target_parses_host_port(self):
        m = self.mod_for(1)
        self.assertEqual(m.tcp_target("tcp:1.2.3.4:9000"), ("1.2.3.4", 9000))
        self.assertEqual(m.tcp_target("tcp:9000"), ("127.0.0.1", 9000))
        self.assertEqual(m.tcp_target("tcp:[::1]:9000"), ("::1", 9000))
        self.assertIsNone(m.tcp_target("/home/u/.bct-chat.sock"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:notaport"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:0"))
        self.assertIsNone(m.tcp_target("tcp:1.2.3.4:70000"))

    def test_rpc_skips_keepalive_newlines(self):
        srv = RawServer(b"\n\n" + json.dumps({"ok": True, "text": "hi"}).encode() + b"\n")
        try:
            self.assertEqual(self.mod_for(srv.port).rpc("chat-list", []),
                             {"ok": True, "text": "hi"})
        finally:
            srv.close()

    def test_rpc_takes_the_first_of_two_coalesced_frames(self):
        srv = RawServer(b'{"ok": true, "text": "first"}\n{"ok": true, "text": "second"}\n')
        try:
            self.assertEqual(self.mod_for(srv.port).rpc("chat-list", [])["text"], "first")
        finally:
            srv.close()

    def test_rpc_reports_a_silent_close_as_a_socket_error(self):
        srv = RawServer(None)               # accepts, replies with nothing, closes
        try:
            r = self.mod_for(srv.port).rpc("chat-list", [])
            self.assertFalse(r["ok"])
            self.assertIn("socket error", r["error"])
            self.assertNotIn("malformed", r["error"])
        finally:
            srv.close()

    def test_rpc_honours_an_overall_deadline_on_a_dribbling_bridge(self):
        # A byte every 0.2s, never a newline: the per-recv timeout would never fire.
        class Dribble(RawServer):
            def _serve(self):
                import time
                while True:
                    try:
                        conn, _ = self.sock.accept()
                    except OSError:
                        return
                    with conn:
                        try:
                            conn.recv(65536)
                            for _ in range(100):
                                conn.sendall(b" ")
                                time.sleep(0.2)
                        except OSError:
                            pass

        srv = Dribble(b"")
        try:
            import time as t
            m = self.mod_for(srv.port)
            started = t.time()
            r = m.rpc("chat-list", [], timeout=2)
            self.assertFalse(r["ok"])
            self.assertIn("socket error", r["error"])
            self.assertLess(t.time() - started, 6, "rpc ignored its overall deadline")
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
```

```python
# tests/test_unix_transport.py
#!/usr/bin/env python3
"""AF_UNIX is the PRODUCTION transport on macOS and Linux and had zero coverage —
which is why a zombie socket file (an ssh reconnect without StreamLocalBindUnlink)
read as 'healthy' for so long."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402
from test_tcp_transport import CLIENT  # noqa: E402

unix_only = unittest.skipUnless(hasattr(socket, "AF_UNIX"), "no AF_UNIX on this platform")


class UnixChatServer:
    def __init__(self, path, handler):
        self.handler = handler
        self.received = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                buf = b""
                try:
                    while not buf.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    continue
                if not buf.strip():
                    continue                 # availability probe: connect + close
                req = json.loads(buf.decode())
                self.received.append(req)
                conn.sendall((json.dumps(self.handler(req)) + "\n").encode())

    def close(self):
        self.sock.close()


@unix_only
class UnixTransportTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.path = os.path.join(self.home, "bct.sock")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_client(self, args):
        env = {k: v for k, v in os.environ.items() if k not in ("BCT_PANE_ID",)}
        env.update(HOME=self.home,
                   BCT_CHAT_HOME=os.path.join(self.home, ".bct-chat"),
                   BCT_CHAT_SOCK=self.path)
        return subprocess.run([sys.executable, CLIENT] + args,
                              env=env, capture_output=True, text=True, timeout=30)

    def mod(self):
        os.environ["BCT_CHAT_SOCK"] = self.path
        try:
            return load_fresh_module(self.home)
        finally:
            os.environ.pop("BCT_CHAT_SOCK", None)

    def test_read_over_unix_round_trip(self):
        srv = UnixChatServer(self.path, lambda req: {"ok": True, "text": "hello-unix"})
        try:
            r = self.run_client(["read"])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "hello-unix")
            self.assertEqual(srv.received[-1]["cmd"], "chat-read")
        finally:
            srv.close()

    def test_sock_available_true_when_a_listener_is_there(self):
        srv = UnixChatServer(self.path, lambda req: {"ok": True, "text": ""})
        try:
            self.assertTrue(self.mod().sock_available())
        finally:
            srv.close()

    def test_zombie_socket_file_is_not_available(self):
        # The file exists, nothing is listening: os.path.exists() said "healthy" and
        # the client then sat silently outside the room forever (D4).
        open(self.path, "w").close()
        self.assertFalse(self.mod().sock_available())

    def test_missing_socket_is_not_available(self):
        self.assertFalse(self.mod().sock_available())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_wire tests.test_unix_transport -v`
Expected: FAIL — `tcp_target("tcp:1.2.3.4:0")` returns a target; `rpc` reports "malformed response" for a silent close; `sock_available()` returns True for the zombie socket file.

- [ ] **Step 3: Implement `src/bctchat/wire.py`**

```python
import json, os, re, socket, subprocess, sys, time


def tcp_target(spec):
    """$BCT_CHAT_SOCK=tcp:<host>:<port> -> (host, port); None means a unix path.
    IPv6 goes in brackets: tcp:[::1]:9000."""
    if not spec.startswith("tcp:"):
        return None
    rest = spec[4:]
    if rest.startswith("["):
        host, sep, port = rest[1:].partition("]:")
        if not sep:
            return None
    else:
        host, _, port = rest.rpartition(":")
    if not port.isdigit():
        return None
    port = int(port)
    if not 1 <= port <= 65535:
        return None
    return (host or "127.0.0.1", port)


def default_name():
    return socket.gethostname()


def connect(timeout=10):
    t = tcp_target(SOCK)
    if t is not None:
        return socket.create_connection(t, timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    return s


def sock_available():
    """CONNECT — never os.path.exists(). A stale socket file left by an ssh reconnect
    without StreamLocalBindUnlink still exists on disk; connecting to it is the only
    way to learn that nobody is home (D4)."""
    try:
        connect(timeout=3).close()
        return True
    except OSError:
        return False


def rpc(cmd, args, pane_id="", timeout=10):
    """One request, one line-JSON reply, bounded by an OVERALL deadline — not a
    per-recv timeout, which a bridge dribbling bytes could keep resetting forever.
    Blank lines are keepalives and are skipped; if two frames arrive in one read we
    answer from the first; a bridge that accepts and closes without replying is a
    socket error, not a malformed response."""
    if tcp_target(SOCK) is None and not os.path.exists(SOCK):
        return {"ok": False, "error": f"socket not found: {SOCK} (ssh RemoteForward up?)"}
    deadline = time.time() + timeout
    try:
        s = connect(min(timeout, 10))
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    try:
        s.sendall((json.dumps({"paneID": pane_id, "cmd": cmd, "args": args}) + "\n").encode())
        buf = b""
        while True:
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue                  # keepalive
                try:
                    return json.loads(line.decode("utf-8"))
                except ValueError:
                    return {"ok": False, "error": "malformed response from bridge"}
            left = deadline - time.time()
            if left <= 0:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            s.settimeout(left)
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                return {"ok": False, "error": "socket error: timed out waiting for the bridge"}
            if not chunk:
                return {"ok": False, "error": "socket error: bridge closed without a reply"}
            buf += chunk
    except OSError as e:
        return {"ok": False, "error": f"socket error: {e}"}
    finally:
        try:
            s.close()
        except OSError:
            pass
```

- [ ] **Step 4: Rebuild and run**

Run: `python3 scripts/build.py && python3 -m unittest discover -s tests`
Expected: OK (all new wire/unix tests pass, the 63 originals stay green).

- [ ] **Step 5: Commit**

```bash
git add -A src scripts/bct-chat.py tests
git commit -m "fix(wire): connect-probe availability, overall rpc deadline, keepalive/frame tolerance, AF_UNIX tests"
```

---

### Task 4: The inbox

A directory queue. Pure local filesystem — no socket, no RPC. This is the module the whole rework hangs on.

**Files:**
- Create: `src/bctchat/inbox.py` (replacing the Task 1 placeholder)
- Modify: `src/bctchat/config.py` (paths + tunables)
- Test: `tests/test_inbox.py` (create)

**Interfaces:**
- Produces:
  - `inbox_put(text: str, name: str) -> str` — writes `inbox/<time_ns>-<pid>.json` atomically, enforces the cap, returns the path
  - `inbox_claim() -> tuple[str, dict] | None` — atomically `os.rename`s the oldest item into `processing/`; returns `(processing_path, item)`
  - `inbox_ack(path: str) -> None` — deletes the processing file
  - `inbox_wait(seconds: float, poll: float = 1.0) -> tuple[str, dict] | None` — local poll, zero tokens
  - `recover_orphans() -> int` — `processing/` files older than `ORPHAN_AGE` go back to `inbox/`
  - `take_dropped() -> int` — read-and-clear the "oldest N dropped" counter
  - item shape: `{"text": str, "capturedAt": float, "name": str}`
- Config added: `INBOX_DIR`, `PROCESSING_DIR`, `DROPPED`, `INBOX_CAP = 50`, `ORPHAN_AGE = 120`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_inbox.py
#!/usr/bin/env python3
"""The inbox is the durability boundary: the daemon only advances BCT's cursor after
an item is on local disk, and exactly one hook may ever claim a given item."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_put_then_claim_round_trips_the_text(self):
        self.mod.inbox_put("yoros: @svr 봐줘", "svr")
        path, item = self.mod.inbox_claim()
        self.assertEqual(item["text"], "yoros: @svr 봐줘")
        self.assertEqual(item["name"], "svr")
        self.assertGreater(item["capturedAt"], 0)
        self.assertTrue(os.path.exists(path))                 # in processing/, not gone
        self.assertEqual(os.listdir(self.mod.INBOX_DIR), [])  # claimed out of the inbox
        self.mod.inbox_ack(path)
        self.assertFalse(os.path.exists(path))

    def test_claim_on_an_empty_inbox_is_none(self):
        self.assertIsNone(self.mod.inbox_claim())

    def test_claim_is_fifo(self):
        self.mod.inbox_put("first", "svr")
        time.sleep(0.01)
        self.mod.inbox_put("second", "svr")
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "first")
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "second")

    def test_two_claimers_never_get_the_same_item(self):
        # Two sessions' Stop hooks fire at once. The loser's rename raises; it must
        # take the NEXT item or none — never a duplicate of the winner's.
        self.mod.inbox_put("only-one", "svr")
        a = self.mod.inbox_claim()
        b = self.mod.inbox_claim()
        self.assertIsNotNone(a)
        self.assertIsNone(b)

    def test_a_corrupt_item_is_dropped_not_returned(self):
        os.makedirs(self.mod.INBOX_DIR, exist_ok=True)
        with open(os.path.join(self.mod.INBOX_DIR, "1-1.json"), "w") as f:
            f.write("{not json")
        self.mod.inbox_put("good", "svr")
        path, item = self.mod.inbox_claim()
        self.assertEqual(item["text"], "good")

    def test_orphan_recovery_returns_a_dead_hooks_item(self):
        self.mod.inbox_put("orphaned", "svr")
        path, _ = self.mod.inbox_claim()                 # the hook then dies, never acks
        os.utime(path, (time.time() - 300, time.time() - 300))
        self.assertEqual(self.mod.recover_orphans(), 1)
        self.assertEqual(self.mod.inbox_claim()[1]["text"], "orphaned")

    def test_orphan_recovery_leaves_a_fresh_claim_alone(self):
        self.mod.inbox_put("in-flight", "svr")
        self.mod.inbox_claim()
        self.assertEqual(self.mod.recover_orphans(), 0)

    def test_cap_drops_the_oldest_and_counts_it(self):
        self.mod.INBOX_CAP = 3
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
            time.sleep(0.002)
        texts = []
        while True:
            got = self.mod.inbox_claim()
            if not got:
                break
            texts.append(got[1]["text"])
        self.assertEqual(texts, ["m2", "m3", "m4"])
        self.assertEqual(self.mod.take_dropped(), 2)
        self.assertEqual(self.mod.take_dropped(), 0)      # read-and-clear

    def test_wait_returns_as_soon_as_an_item_lands(self):
        import threading
        threading.Timer(0.2, lambda: self.mod.inbox_put("late", "svr")).start()
        started = time.time()
        got = self.mod.inbox_wait(5, poll=0.05)
        self.assertEqual(got[1]["text"], "late")
        self.assertLess(time.time() - started, 3)

    def test_wait_times_out_to_none(self):
        started = time.time()
        self.assertIsNone(self.mod.inbox_wait(0.3, poll=0.05))
        self.assertGreaterEqual(time.time() - started, 0.3)

    def test_put_is_atomic_no_partial_file_visible(self):
        self.mod.inbox_put("x", "svr")
        names = os.listdir(self.mod.INBOX_DIR)
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".json"))
        self.assertFalse(any(n.endswith(".tmp") for n in names))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_inbox -v`
Expected: FAIL — `AttributeError: module has no attribute 'inbox_put'`.

- [ ] **Step 3: Add the config entries**

`src/bctchat/config.py`:

```python
INBOX_DIR = os.path.join(STATE_DIR, "inbox")
PROCESSING_DIR = os.path.join(STATE_DIR, "processing")
DROPPED = os.path.join(STATE_DIR, "dropped.json")
INBOX_CAP = 50              # a deeper queue means nobody has been home for a long time
ORPHAN_AGE = 120            # a processing/ item older than this belonged to a dead hook
```

- [ ] **Step 4: Implement `src/bctchat/inbox.py`**

```python
"""The local mention inbox: the durability boundary between the daemon's ear and the
hooks' mouth. The daemon does not issue its next chat-listen until the item is here,
so a message whose server-side cursor has advanced is always already on local disk."""
import json, os, re, socket, subprocess, sys, time


def _items(d):
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []


def _bump_dropped(n):
    obj = load(DROPPED) or {}
    save(DROPPED, {"n": int(obj.get("n", 0)) + n})


def take_dropped():
    """Read-and-clear the count of mentions the cap threw away, for the next digest."""
    obj = load(DROPPED) or {}
    n = int(obj.get("n", 0))
    if n:
        forget(DROPPED)
    return n


def inbox_put(text, name):
    """One mention -> one file. Atomic: a reader can never see a half-written item."""
    os.makedirs(INBOX_DIR, exist_ok=True)
    names = _items(INBOX_DIR)
    excess = len(names) - (INBOX_CAP - 1)
    if excess > 0:
        for n in names[:excess]:
            forget(os.path.join(INBOX_DIR, n))
        _bump_dropped(excess)
    path = os.path.join(INBOX_DIR, f"{time.time_ns()}-{os.getpid()}.json")
    atomic_write(path, json.dumps({"text": text, "capturedAt": time.time(), "name": name},
                                  ensure_ascii=False))
    return path


def inbox_claim():
    """Take the oldest item, atomically. os.rename is the whole concurrency design:
    two hooks racing, the loser's rename raises and it moves on to the next item —
    exactly one session ever delivers a given mention, with no lock to leak."""
    os.makedirs(PROCESSING_DIR, exist_ok=True)
    for n in _items(INBOX_DIR):
        src = os.path.join(INBOX_DIR, n)
        dst = os.path.join(PROCESSING_DIR, f"{os.getpid()}-{n}")
        try:
            os.rename(src, dst)
        except OSError:
            continue                    # another hook won it
        item = load(dst)
        if not isinstance(item, dict) or "text" not in item:
            forget(dst)                 # corrupt: drop it, never hand it to claude
            continue
        return (dst, item)
    return None


def inbox_ack(path):
    forget(path)


def recover_orphans():
    """A hook that died between claim and print left its item in processing/. Return it
    to the inbox: at-least-once delivery (a rare duplicate) beats a silent loss, and it
    is what makes hooks.json's `|| python` re-run harmless."""
    n = 0
    now = time.time()
    for name in _items(PROCESSING_DIR):
        p = os.path.join(PROCESSING_DIR, name)
        try:
            if now - os.stat(p).st_mtime < ORPHAN_AGE:
                continue
            os.rename(p, os.path.join(INBOX_DIR, name.split("-", 1)[1]))
            n += 1
        except OSError:
            pass
    return n


def inbox_wait(seconds, poll=1.0):
    """Standby's hold: a local poll on a directory. Zero RPC, zero tokens."""
    deadline = time.time() + seconds
    while True:
        got = inbox_claim()
        if got:
            return got
        if time.time() >= deadline:
            return None
        time.sleep(min(poll, max(0.0, deadline - time.time())))
```

- [ ] **Step 5: Rebuild and run**

Run: `python3 scripts/build.py && python3 -m unittest discover -s tests`
Expected: OK — `tests.test_inbox` green, everything else still green.

- [ ] **Step 6: Commit**

```bash
git add -A src scripts/bct-chat.py tests
git commit -m "feat(inbox): durable local mention queue with atomic claim and orphan recovery"
```

---

### Task 5: Membership — the join budget

Replaces the flat 30-minute cooldown. Fixes D5 (`authed()` double-joins), D6 (a wedged `pending-join.json` makes rejoin unreachable), D8 (`leave` doesn't stick), D9 (a denied host nags for 12 hours).

**Files:**
- Modify: `src/bctchat/membership.py`, `src/bctchat/config.py`
- Delete: `tests/test_cooldown.py` (superseded), `tests/test_session_start_rejoin.py` (its subject moves to the daemon — Task 6)
- Test: `tests/test_membership.py` (create)

**Interfaces:**
- Produces:
  - `join_state() -> dict` — `{"attempts": int, "nextAttemptAt": float, "suspended": bool, "lastOutcome": str}`
  - `may_request_join() -> bool` — not suspended **and** `time.time() >= nextAttemptAt`
  - `note_join_outcome(outcome: str) -> None` — `denied`/`expired` → attempts+1, back off `60 → 300 → 1800`, suspend at 3
  - `clear_join_state() -> None` — approval, or a manual `join` (human intent always wins)
  - `identity() -> str`, `load_identity() -> dict | None`
  - `claim_pending() -> bool` — approved → identity + clear state; denied/expired → `note_join_outcome`; older than `PENDING_TTL` → discard
  - `ensure_membership(wait_approval=False) -> bool` — **the single automatic-join entry point.** PENDING first, then the budget. Never fires a second `chat-join` while one is outstanding.
  - `do_join(name, wait_approval=True) -> None` — success is decided by `identity()` existing, not by PENDING vanishing
  - `authed(cmd, args, timeout=10) -> dict`
  - `do_leave() -> None` — `chat-leave`, drop identity + pending, `suspended: True`, **keep the session markers**
- Config added: `JOIN_STATE = os.path.join(STATE_DIR, "join-state.json")`, `JOIN_BACKOFF = (60, 300, 1800)`, `JOIN_MAX_ATTEMPTS = 3`, `PENDING_TTL = 600`
- Config removed: `COOLDOWN`, `JOIN_COOLDOWN`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_membership.py
#!/usr/bin/env python3
"""Membership must be self-healing but never a nag: a BCT restart gets us back in
without a human, three refusals stop the asking for good, and `leave` stays left."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.sock_available = lambda: True
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def rpc_map(self, responses):
        def fake(cmd, args, pane_id="", timeout=10):
            self.calls.append((cmd, args))
            r = responses[cmd]
            return r(args) if callable(r) else r
        self.mod.rpc = fake

    def test_ensure_membership_requests_a_join_when_unseated(self):
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-1"}})
        self.assertTrue(self.mod.ensure_membership())
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-1")
        self.assertGreater(self.mod.load(self.mod.PENDING)["requestedAt"], 0)

    def test_ensure_membership_polls_an_outstanding_request_instead_of_re_requesting(self):
        # D5: a second chat-join orphans the approval the user is about to grant.
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time()})
        self.rpc_map({"chat-join-poll": {"ok": False, "error": "pending"},
                      "chat-join": {"ok": True, "text": "REQ-2"}})
        self.mod.ensure_membership()
        self.assertEqual([c for c, _ in self.calls], ["chat-join-poll"])

    def test_an_approval_seats_us_and_wipes_the_budget(self):
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time()})
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 2, "nextAttemptAt": 0,
                                            "suspended": False, "lastOutcome": "denied"})
        self.rpc_map({"chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        self.assertTrue(self.mod.claim_pending())
        self.assertEqual(self.mod.identity(), IDENT)
        self.assertIsNone(self.mod.load(self.mod.PENDING))
        self.assertIsNone(self.mod.load(self.mod.JOIN_STATE))

    def test_a_stale_pending_is_discarded_even_if_the_poll_is_unrecognised(self):
        # D6: BCT restarted and no longer knows the requestID — the old code wedged
        # pending-join.json forever and the rejoin branch became unreachable.
        self.mod.save(self.mod.PENDING, {"requestID": "REQ-1", "name": "svr",
                                         "requestedAt": time.time() - 700})
        self.rpc_map({"chat-join-poll": {"ok": False, "error": "unknown request"},
                      "chat-join": {"ok": True, "text": "REQ-2"}})
        self.mod.ensure_membership()
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-2")

    def test_three_refusals_suspend_the_asking_for_good(self):
        # D9: the flat cooldown re-asked every 30 min for the daemon's whole life.
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ"},
                      "chat-join-poll": {"ok": False, "error": "denied"}})
        for expected in (60, 300, 1800):
            self.assertTrue(self.mod.ensure_membership())      # requests
            self.mod.claim_pending()                            # -> denied
            st = self.mod.join_state()
            self.assertAlmostEqual(st["nextAttemptAt"] - time.time(), expected, delta=5)
            st["nextAttemptAt"] = 0                             # fast-forward the backoff
            self.mod.save(self.mod.JOIN_STATE, st)
        self.assertTrue(self.mod.join_state()["suspended"])
        joins = len([c for c, _ in self.calls if c == "chat-join"])
        self.assertFalse(self.mod.ensure_membership())
        self.assertEqual(len([c for c, _ in self.calls if c == "chat-join"]), joins)

    def test_a_manual_join_clears_a_suspension(self):
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": time.time() + 9999,
                                            "suspended": True, "lastOutcome": "denied"})
        self.rpc_map({"chat-join": {"ok": True, "text": "REQ-9"},
                      "chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        self.mod.clear_join_state()                 # what the `join` verb does first
        self.mod.do_join("svr", wait_approval=False)
        self.assertEqual(self.mod.load(self.mod.PENDING)["requestID"], "REQ-9")

    def test_authed_re_joins_once_on_not_invited(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "DEAD", "name": "svr"})
        seq = {"n": 0}

        def send(args):
            seq["n"] += 1
            return {"ok": False, "error": NOT_INVITED} if seq["n"] == 1 else {"ok": True, "text": ""}

        self.rpc_map({"chat-send": send,
                      "chat-join": {"ok": True, "text": "REQ-1"},
                      "chat-join-poll": {"ok": True, "text": f"approved\n{IDENT}"}})
        r = self.mod.authed("chat-send", ["hi"])
        self.assertTrue(r["ok"])
        self.assertEqual(self.mod.identity(), IDENT)
        self.assertEqual(len([c for c, _ in self.calls if c == "chat-join"]), 1)

    def test_authed_surfaces_not_invited_while_suspended(self):
        self.mod.save(self.mod.IDENTITY, {"participantID": "DEAD", "name": "svr"})
        self.mod.save(self.mod.JOIN_STATE, {"attempts": 3, "nextAttemptAt": 0,
                                            "suspended": True, "lastOutcome": "denied"})
        self.rpc_map({"chat-send": {"ok": False, "error": NOT_INVITED}})
        r = self.mod.authed("chat-send", ["hi"])
        self.assertEqual(r["error"], NOT_INVITED)
        self.assertEqual([c for c, _ in self.calls], ["chat-send"])

    def test_leave_suspends_and_keeps_the_markers(self):
        # D8: the old leave dropped the identity but left the daemon running, which
        # re-requested membership four minutes later — in the room the user just left.
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.mark_session("sess-1")
        self.rpc_map({"chat-leave": {"ok": True, "text": ""}})
        self.mod.do_leave()
        self.assertEqual(self.mod.identity(), "")
        self.assertTrue(self.mod.join_state()["suspended"])
        self.assertEqual(self.mod.live_sessions(), ["sess-1"])
        self.assertFalse(self.mod.ensure_membership())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_membership -v`
Expected: FAIL — no `ensure_membership`, no `join_state`, no `do_leave`.

- [ ] **Step 3: Implement `src/bctchat/membership.py`** (replaces the cooldown functions entirely)

```python
"""Membership: identity, the outstanding request, and the budget that decides whether
we may ask again. ONE automatic-join entry point (ensure_membership) — the old code had
three callers racing each other, and a second chat-join while one is outstanding orphans
the approval the user is in the middle of granting."""
import json, os, re, socket, subprocess, sys, time


def load_identity():
    return load(IDENTITY)


def identity():
    obj = load_identity()
    return obj.get("participantID", "") if obj else ""


def join_state():
    obj = load(JOIN_STATE)
    if not isinstance(obj, dict):
        return {"attempts": 0, "nextAttemptAt": 0, "suspended": False, "lastOutcome": ""}
    return {"attempts": int(obj.get("attempts", 0)),
            "nextAttemptAt": float(obj.get("nextAttemptAt", 0)),
            "suspended": bool(obj.get("suspended", False)),
            "lastOutcome": str(obj.get("lastOutcome", ""))}


def clear_join_state():
    forget(JOIN_STATE)


def suspended():
    return join_state()["suspended"]


def may_request_join():
    st = join_state()
    return not st["suspended"] and time.time() >= st["nextAttemptAt"]


def note_join_outcome(outcome):
    """A refusal is information, not a reason to keep asking. Back off, then stop:
    three denied/expired outcomes and we never ask again on our own — only a human
    running `bct-chat.py join` at the remote's shell resumes it."""
    st = join_state()
    st["attempts"] += 1
    st["lastOutcome"] = outcome
    idx = min(st["attempts"], len(JOIN_BACKOFF)) - 1
    st["nextAttemptAt"] = time.time() + JOIN_BACKOFF[idx]
    st["suspended"] = st["attempts"] >= JOIN_MAX_ATTEMPTS
    save(JOIN_STATE, st)


def pending():
    """The outstanding request, or None. A TTL — not the poll's reply — is what
    ultimately retires it: an unrecognized error (BCT restarted and forgot the id)
    used to wedge the file forever, and every auto-join caller prefers PENDING over
    requesting, so the rejoin branch became unreachable (D6)."""
    obj = load(PENDING)
    if not isinstance(obj, dict) or "requestID" not in obj:
        return None
    if time.time() - float(obj.get("requestedAt", 0)) > PENDING_TTL:
        forget(PENDING)
        return None
    return obj


def claim_pending():
    obj = pending()
    if not obj:
        return False
    r = rpc("chat-join-poll", [obj["requestID"]])
    if r.get("ok") and (r.get("text") or "").startswith("approved\n"):
        save(IDENTITY, {"participantID": r["text"].split("\n", 1)[1], "name": obj["name"]})
        forget(PENDING)
        clear_join_state()                    # seated — the slate is clean
        return True
    if not r.get("ok") and r.get("error") in ("denied", "expired"):
        forget(PENDING)
        note_join_outcome(r["error"])
    return False


def ensure_membership(wait_approval=False):
    """The ONLY automatic path into the room. Returns True if we are seated or a request
    is now outstanding."""
    if identity() and not wait_approval:
        return True
    if pending():
        return claim_pending() or True        # a request is in flight: poll it, never re-ask
    if not may_request_join():
        return False
    obj = load_identity()
    do_join(obj["name"] if obj else default_name(), wait_approval=wait_approval)
    return True


def do_join(name, wait_approval=True):
    r = rpc("chat-join", [name])
    if not r.get("ok"):
        die(r.get("error", "join failed"))
    save(PENDING, {"requestID": r["text"], "name": name, "requestedAt": time.time()})
    if not wait_approval:
        print(f"join requested ({r['text']}) — approve in the BCT chat dock", file=sys.stderr)
        return
    print("입장 요청됨 — BCT 채팅 도크에서 승인해 주세요 (5분 내)", file=sys.stderr)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(2)
        claim_pending()
        if identity():
            # Success is "we have an identity", NOT "PENDING vanished": the daemon polls
            # too and may legitimately have claimed the approval out from under us.
            print("입장 승인됨", file=sys.stderr)
            return
        if not load(PENDING):
            die("denied or expired")
    die("승인 대기 시간 초과")


def authed(cmd, args, timeout=10):
    """RPC with identity; one bounded re-join on identity invalidation (BCT restart or
    an eviction). Goes through ensure_membership, so it can never fire a chat-join while
    a request is already outstanding (D5)."""
    if not identity():
        claim_pending()
    r = rpc(cmd, args, identity(), timeout=timeout)
    if not r.get("ok") and r.get("error") == NOT_INVITED and load_identity():
        if not may_request_join():
            return r                          # suspended or backing off — surface it as-is
        print("identity invalid (BCT 재시작/내보내기) — 재입장 요청", file=sys.stderr)
        ensure_membership(wait_approval=True)
        if not identity():
            return r
        r = rpc(cmd, args, identity(), timeout=timeout)
    return r


def do_leave():
    """Leaving must STAY left. The old leave dropped the identity and walked away, so the
    daemon re-requested membership four minutes later. Suspending the budget is what makes
    the daemon stand down (and ensure_daemon() refuse to respawn it) — while the session
    markers survive, because they describe which claude sessions are alive, and that is
    still true after leaving the room."""
    r = rpc("chat-leave", [], identity())
    forget(IDENTITY)
    forget(PENDING)
    st = join_state()
    st["suspended"] = True
    st["lastOutcome"] = "left"
    save(JOIN_STATE, st)
    if not r.get("ok") and r.get("error") != NOT_INVITED:
        die(r.get("error", "error"))
```

Also: `membership_live()` and `request_join_if_allowed()` are deleted (their callers now use `ensure_membership()`), and `die()` moves from `cli.py` to `membership.py`'s neighbourhood — keep `die()` in `cli.py` but list `cli` **after** `membership` in the build order? No: `die()` is called at *runtime*, not import time, so the single-namespace artifact resolves it regardless of order. Leave `die()` in `cli.py`.

- [ ] **Step 4: Delete the superseded tests**

```bash
git rm tests/test_cooldown.py tests/test_session_start_rejoin.py
```

Their subjects are covered by `tests/test_membership.py` (budget, rejoin, PENDING) and, for the session-start-side rejoin, by Task 6's daemon tests.

- [ ] **Step 5: Rebuild and run**

Run: `python3 scripts/build.py && python3 -m unittest discover -s tests`
Expected: OK. `tests/test_heartbeat.py` will still reference the old `request_join_if_allowed` path — if it fails here, do **not** patch it: it is rewritten wholesale in Task 6. If it blocks this commit, mark it `@unittest.skip("rewritten in Task 6: daemon-as-ear")` on the failing test class and remove the skip in Task 6.

- [ ] **Step 6: Commit**

```bash
git add -A src scripts/bct-chat.py tests
git commit -m "feat(membership): bounded join budget, PENDING TTL, single auto-join path, leave stays left"
```

---

### Task 6: Presence — markers with liveness, and the daemon as the ear

The heart of the rework. Fixes D1 (single spawn point, 2-strike suicide), D2 (a signal-killed daemon reads as live for 8 minutes), D12 (leaked markers).

**Files:**
- Modify: `src/bctchat/presence.py`, `src/bctchat/state.py` (markers), `src/bctchat/config.py`
- Test: rewrite `tests/test_heartbeat.py` → `tests/test_daemon.py`; rewrite `tests/test_session_markers.py`

**Interfaces:**
- Produces:
  - `mark_session(sid) -> None` — writes `{"pid": <claude pid or 0>, "startedAt": <epoch>}`; refreshes mtime on every call
  - `claude_pid() -> int` — best-effort ancestor resolution (POSIX `ps`; Windows → 0)
  - `gc_markers() -> int` — removes markers whose pid is dead, or (pid 0) whose mtime is older than `MARKER_TTL`
  - `heartbeat_alive() -> bool` — pidfile mtime < `PIDFILE_STALE` (90s) **and** `proc_alive(pidfile_owner())`
  - `ensure_daemon() -> None` — spawn unless one is alive, or we are suspended-and-unseated
  - `do_daemon(presence_interval=PRESENCE_INTERVAL, listen_timeout=LISTEN_TIMEOUT) -> None`
- Config added: `PIDFILE_STALE = 90`, `PRESENCE_INTERVAL = 240`, `LISTEN_TIMEOUT = 40`, `BACKOFF_MIN = 60`, `BACKOFF_MAX = 300`, `JOIN_POLL = 15`, `MARKER_TTL = 7 * 86400`
- Config removed: `HEARTBEAT_MAX_UPTIME`, `HEARTBEAT_INTERVAL` (→ `PRESENCE_INTERVAL`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_daemon.py
#!/usr/bin/env python3
"""The daemon is now the EAR: capture reliability is daemon reliability. It must hold a
listen, land the mention on disk BEFORE the next listen, never die of a dead tunnel, and
get out of the way the instant its sessions are gone, a newer daemon takes over, or the
user has left the room."""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"


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


if __name__ == "__main__":
    unittest.main()
```

`load_fresh_module` moves out of `test_heartbeat.py` (which is being deleted) into a shared helper so the other suites keep importing it:

```python
# tests/test_heartbeat_helpers.py  -- rename of the old helper home
#!/usr/bin/env python3
"""Shared test helper: a fresh in-process bct-chat module whose STATE_DIR resolves
under a temp home. BCT_CHAT_HOME — never HOME — is the isolation knob (Windows'
expanduser ignores HOME and would hand back the developer's real profile)."""
import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(REPO, "scripts", "bct-chat.py")


def load_fresh_module(home):
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
```

Update the `from test_heartbeat import load_fresh_module` line in `tests/test_state.py`, `tests/test_wire.py`, `tests/test_unix_transport.py`, `tests/test_inbox.py`, `tests/test_membership.py`, `tests/test_push_hooks.py`, `tests/test_session_markers.py` to `from test_heartbeat_helpers import load_fresh_module`, and `git rm tests/test_heartbeat.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_daemon -v`
Expected: FAIL — no `do_daemon`, no `gc_markers`, no `ensure_daemon`.

- [ ] **Step 3: Implement the markers (`src/bctchat/state.py`)**

```python
def claude_pid():
    """Best-effort pid of the claude process this hook belongs to. The hook's own parent
    is the `sh -c` wrapper hooks.json needs for its `||` fallback, and that shell exits the
    moment we do — so we look one further up. POSIX only: on Windows there is no cheap
    ancestor walk, and 0 means 'no pid liveness for this marker' (gc_markers falls back to
    MARKER_TTL there). A wrong guess would GC a LIVE session's marker, so we only trust an
    ancestor that still exists at the moment we ask."""
    if os.name == "nt":
        return 0
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(os.getppid())],
                             capture_output=True, text=True, timeout=3)
        pid = int(out.stdout.strip())
    except Exception:
        return 0
    return pid if pid > 1 and proc_alive(pid) else 0


def mark_session(sid):
    """One marker per live claude session on this host — the daemon's refcount. The pid
    lets a crashed session's marker be collected; the mtime (refreshed by every hook of
    that session) is the fallback where no pid is available."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    save(os.path.join(SESSIONS_DIR, sid), {"pid": claude_pid(), "startedAt": time.time()})


def unmark_session(sid):
    forget(os.path.join(SESSIONS_DIR, sid))


def live_sessions():
    try:
        return sorted(os.listdir(SESSIONS_DIR))
    except OSError:
        return []
```

- [ ] **Step 4: Implement `src/bctchat/presence.py`**

```python
"""Capture. One daemon per host holds chat-listen continuously and lands every mention in
the inbox BEFORE issuing the next listen. It is the only thing in the system that talks to
the room on its own, which is why its exit conditions are exactly three — no live session,
a newer daemon, or a room the user has left — and why a dead tunnel is something it WAITS
for rather than dies of."""
import json, os, re, socket, subprocess, sys, time


def pidfile_owner():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def heartbeat_alive():
    """mtime AND a real liveness probe. mtime alone called a signal-killed daemon alive for
    8 minutes (D2) — its pidfile keeps a fresh mtime, and nothing respawns a corpse that
    still looks warm."""
    try:
        if time.time() - os.stat(PIDFILE).st_mtime >= PIDFILE_STALE:
            return False
    except OSError:
        return False
    return proc_alive(pidfile_owner())


def ensure_daemon():
    """Every hook is a spawn point (D1: SessionStart used to be the only one). Cheap: the
    hooks do no RPC now, so they can afford this check on every turn."""
    if heartbeat_alive():
        return
    if suspended() and not identity():
        return                          # the user left / denied us out — do not resurrect
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, ARTIFACT, "daemon"], **kwargs)
    except OSError:
        pass                            # best-effort; never block a hook


def gc_markers():
    """A crashed claude leaks its marker, and a leaked marker used to keep a phantom host
    in the room for 12 hours (D12). Collect it by pid where we have one, by age where we
    do not — erring long, because evicting a LIVE session's marker would cost it its ear."""
    n = 0
    for sid in live_sessions():
        p = os.path.join(SESSIONS_DIR, sid)
        obj = load(p) or {}
        pid = int(obj.get("pid", 0) or 0)
        try:
            age = time.time() - os.stat(p).st_mtime
        except OSError:
            continue
        dead = (not proc_alive(pid)) if pid > 0 else (age > MARKER_TTL)
        if dead:
            forget(p)
            n += 1
    return n


def do_daemon(presence_interval=None, listen_timeout=None):
    """The ear. Exit conditions are exactly three; everything else is waited out."""
    presence_interval = PRESENCE_INTERVAL if presence_interval is None else presence_interval
    listen_timeout = LISTEN_TIMEOUT if listen_timeout is None else listen_timeout
    if heartbeat_alive() and pidfile_owner() != os.getpid():
        return                          # another daemon has it
    me = os.getpid()
    atomic_write(PIDFILE, str(me))
    backoff = BACKOFF_MIN
    seated = False
    last_tick = 0.0
    try:
        while True:
            gc_markers()
            if not live_sessions():
                break                   # every claude on this host is gone
            if pidfile_owner() not in (me, 0):
                break                   # a newer daemon took over — its file, not ours to touch
            if suspended() and not identity():
                break                   # the user left the room
            try:
                os.utime(PIDFILE, None)
            except OSError:
                pass
            recover_orphans()
            try:
                if not sock_available():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                if not seated:
                    ensure_membership()                 # pending-first, budget-gated
                    if not identity():
                        time.sleep(JOIN_POLL)
                        continue
                    r = rpc("chat-list", [], identity())
                    seated = bool(r.get("ok"))
                    last_tick = time.time()
                    if not seated:
                        time.sleep(JOIN_POLL)
                        continue
                if time.time() - last_tick >= presence_interval:
                    r = rpc("chat-list", [], identity())     # prune defence; read-only
                    last_tick = time.time()
                    if not r.get("ok") and r.get("error") == NOT_INVITED:
                        seated = False
                        continue
                r = rpc("chat-listen", [], identity(), timeout=listen_timeout)
                if not r.get("ok"):
                    if r.get("error") == NOT_INVITED:
                        seated = False                  # BCT restarted or evicted us
                        continue
                    time.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                backoff = BACKOFF_MIN
                text = r.get("text") or ""
                if text and text not in (NO_NEW, NO_MENTION):
                    obj = load_identity() or {}
                    inbox_put(text, obj.get("name", default_name()))   # BEFORE the next listen
            except (Exception, SystemExit):
                # A bad tick is a failed tick, nothing more. The daemon's whole job is to
                # keep listening; a die() out of a chained join, or a full disk, must never
                # take the ear down with it.
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
    finally:
        if pidfile_owner() == me:
            forget(PIDFILE)             # only ever release a pidfile we still own
```

- [ ] **Step 5: Rewrite `tests/test_session_markers.py`**

Keep every existing test whose subject survives (marker created on session-start, removed on session-end, path-traversal session ids rejected, refcount semantics), changing only two things: markers are now JSON objects (`self.mod.load(marker_path)["pid"]`, not an empty file), and `session_start` no longer performs any RPC (assert `mod.rpc` is never called; assert `ensure_daemon` was invoked instead). Delete any test that asserts session-start issues a `chat-join` — that job is the daemon's now (Task 6 covers it) — and add:

```python
    def test_session_start_does_no_rpc(self):
        self.mod.rpc = lambda *a, **k: self.fail("SessionStart must not touch the socket")
        self.mod.subprocess = FakeSub          # as in test_daemon.LivenessTests
        with open(os.devnull) as f:
            sys.stdin = f
            self.mod.session_start()
```

- [ ] **Step 6: Rebuild and run**

Run: `python3 scripts/build.py && python3 -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add -A src scripts/bct-chat.py tests
git commit -m "feat(presence): daemon-as-ear — holds chat-listen, inbox before next listen, no suicide, marker GC"
```

---

### Task 7: Delivery — local-only hooks, chain cap, modes

Fixes D7 (permanently — no hook may open a socket), D10 (a working session pays 0s per turn; the ping-pong is capped), D11 (capture is 100%; only the wake degrades).

**Files:**
- Modify: `src/bctchat/delivery.py`, `src/bctchat/config.py`, `hooks/hooks.json`
- Test: rewrite `tests/test_push_hooks.py`; delete `tests/test_standby_window.py` (its subject — a socket-holding Stop hook — no longer exists)

**Interfaces:**
- Produces:
  - `chat_mode() -> str` — `"standby"` if `BCT_CHAT_MODE=standby`; `"work"` otherwise. Legacy: `BCT_CHAT_STANDBY` in `("0","off","false","no")` → `"work"`.
  - `hook_payload() -> dict` — reads stdin **once**, returns the parsed hook payload (`{}` on a tty / any failure)
  - `hook_session_id(payload) -> str` — sanitised, or `""`
  - `compose_digest(item: dict, dropped: int = 0) -> str` — caps at `DIGEST_MAX_LINES` / `DIGEST_MAX_BYTES`, prepends `(오래된 멘션 N건 생략)` when `dropped`
  - `stop_hook()`, `prompt_submit_hook()`, `session_start()`, `session_end()`
- Config added: `CHAIN = os.path.join(STATE_DIR, "chain.json")`, `CHAIN_CAP = 3`, `STANDBY_HOLD = 900`, `DIGEST_MAX_LINES = 200`, `DIGEST_MAX_BYTES = 16384`

- [ ] **Step 1: Write the failing tests** (rewrite of `tests/test_push_hooks.py`)

```python
# tests/test_push_hooks.py
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
        self.mod.rpc = lambda *a, **k: self.fail("a delivery hook opened a socket")
        self.mod.sock_available = lambda: self.fail("a delivery hook probed the socket")
        self.mod.hook_payload = lambda: {}
        FakeSub.spawned = []
        self.mod.subprocess = FakeSub
        os.environ.pop("BCT_PANE_ID", None)
        os.environ.pop("BCT_CHAT_MODE", None)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        os.environ.pop("BCT_CHAT_MODE", None)

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

    def test_a_user_turn_resets_the_chain(self):
        for i in range(5):
            self.mod.inbox_put(f"m{i}", "svr")
        self.mod.hook_payload = lambda: {"stop_hook_active": True}
        self.mod.save(self.mod.CHAIN, {"n": 3})
        self.assertEqual(self.run_verb(self.mod.stop_hook), "")
        self.mod.hook_payload = lambda: {"stop_hook_active": False}
        self.assertIn("m0", self.run_verb(self.mod.stop_hook))

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

    def test_hooks_are_a_no_op_inside_a_bct_pane(self):
        os.environ["BCT_PANE_ID"] = "pane-1"
        try:
            self.mod.inbox_put("x", "svr")
            self.assertEqual(self.run_verb(self.mod.stop_hook), "")
            self.assertEqual(self.run_verb(self.mod.prompt_submit_hook), "")
        finally:
            os.environ.pop("BCT_PANE_ID", None)

    def test_the_digest_is_capped(self):
        self.mod.DIGEST_MAX_LINES = 5
        self.mod.inbox_put("\n".join(f"line {i}" for i in range(100)), "svr")
        reason = json.loads(self.run_verb(self.mod.stop_hook))["reason"]
        self.assertLess(len(reason.splitlines()), 12)
        self.assertIn("생략", reason)

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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_push_hooks -v`
Expected: FAIL — `stop_hook` still calls `pending_digest()` → `sock_available()` → the test's `fail()`.

- [ ] **Step 3: Implement `src/bctchat/delivery.py`**

```python
"""Delivery. LOCAL ONLY — no socket, no RPC, no exception. The hooks read an inbox the
daemon fills; a hook that is killed can therefore lose nothing, because BCT's cursor moved
long before, in the daemon, and only after the message was already on this disk."""
import json, os, re, socket, subprocess, sys, time

INBOX_POLL = 1.0


def chat_mode():
    """work (default): the Stop hook returns in milliseconds. standby: it waits on the
    inbox locally for up to STANDBY_HOLD — zero tokens, zero RPC."""
    v = os.environ.get("BCT_CHAT_MODE", "").strip().lower()
    if v in ("standby", "work"):
        return v
    legacy = os.environ.get("BCT_CHAT_STANDBY", "").strip().lower()
    if legacy in ("0", "off", "false", "no"):
        return "work"
    return "work"


def hook_payload():
    """claude-code pipes the hook payload as JSON on stdin. Read it ONCE (a second read
    gets nothing), never block on a tty, and treat any malformed shape as an empty payload."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        obj = json.loads(sys.stdin.read() or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def hook_session_id(payload):
    """The session id becomes a filename under sessions/, so it is never trusted as-is:
    basename + a strict charset, and anything else is treated as absent."""
    sid = os.path.basename(str(payload.get("session_id", "")))
    if sid in ("", ".", "..") or not SESSION_ID_RE.match(sid):
        return ""
    return sid


def compose_digest(item, dropped=0):
    """Mirror BCT's local chatInjection shape: identity line, the room lines exactly as BCT
    returned them, then the reply instruction. Capped — a backlog that rotted for hours must
    not dump 4000 lines into a turn."""
    name = item.get("name") or default_name()
    lines = [f"[bct-chat] 단체 채팅방 — 당신은 @{name} 입니다. 새 메시지:"]
    if dropped:
        lines.append(f"(오래된 멘션 {dropped}건 생략)")
    body = [l for l in (item.get("text") or "").splitlines() if l]
    if len(body) > DIGEST_MAX_LINES:
        body = body[-DIGEST_MAX_LINES:]
        lines.append(f"(앞부분 생략 — 최근 {DIGEST_MAX_LINES}줄만)")
    lines += body
    lines.append(REPLY_HINT)
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > DIGEST_MAX_BYTES:
        text = text.encode("utf-8")[:DIGEST_MAX_BYTES].decode("utf-8", "ignore") + "\n(생략)"
    return text


def chain_count(active):
    """`stop_hook_active` says the turn is only continuing because WE blocked it. Count the
    automatic re-engagements and stop at CHAIN_CAP: two standby remotes mentioning each
    other must not bill turns forever with no human in the loop (D10)."""
    if not active:
        return 0
    obj = load(CHAIN) or {}
    return int(obj.get("n", 0))


def deliver(item, path, dropped):
    print(json.dumps({"decision": "block", "reason": compose_digest(item, dropped)},
                     ensure_ascii=False))
    inbox_ack(path)


def stop_hook():
    """Always exits 0: hooks.json falls back python3 -> python on ANY nonzero exit, which
    would re-run the whole hook with stdin already drained."""
    hook_payload_obj = {}
    try:
        hook_payload_obj = hook_payload()
        if os.environ.get("BCT_PANE_ID"):
            return                       # BCT pane — native push owns delivery
        ensure_daemon()                  # every hook is a spawn point
        active = bool(hook_payload_obj.get("stop_hook_active"))
        n = chain_count(active)
        if n >= CHAIN_CAP:
            return                       # capped: the item stays in the inbox for the user
        recover_orphans()
        got = inbox_claim()
        if got is None and chat_mode() == "standby":
            got = inbox_wait(STANDBY_HOLD, poll=INBOX_POLL)
        if got is None:
            if not active:
                forget(CHAIN)
            return                       # graceful degradation: the daemon keeps listening
        save(CHAIN, {"n": n + 1})
        deliver(got[1], got[0], take_dropped())
    except (Exception, SystemExit):
        pass


def prompt_submit_hook():
    """The digest rides along as CONTEXT with the user's prompt — this is what reaches a
    session that was never woken (cold idle). Never holds. Always exits 0."""
    try:
        hook_payload()
        if os.environ.get("BCT_PANE_ID"):
            return
        ensure_daemon()
        recover_orphans()
        got = inbox_claim()
        if got is None:
            return
        forget(CHAIN)                    # a user prompt ends any automatic chain
        print(compose_digest(got[1], take_dropped()))
        inbox_ack(got[0])
    except (Exception, SystemExit):
        pass


def session_start():
    """LOCAL ONLY: stable copy, marker, daemon. The join itself is the daemon's first tick —
    which is what fixes D3 (a claude started before the tunnel is up used to early-return
    with no marker and no daemon, and nothing ever repaired that session)."""
    try:
        if os.environ.get("BCT_PANE_ID"):
            return                       # BCT pane — statusline auto-invite owns this
        ensure_stable_copy()
        sid = hook_session_id(hook_payload())
        if not sid:
            return
        mark_session(sid)                # before spawning: the daemon exits on an empty set
        ensure_daemon()
    except (Exception, SystemExit):
        pass


def session_end():
    """Drop this session's marker. The daemon is NOT killed — another claude session on this
    host may still be in the room; it exits on its own once the marker set empties."""
    try:
        sid = hook_session_id(hook_payload())
        if sid:
            unmark_session(sid)
    except (Exception, SystemExit):
        pass
```

Delete from the module: `pending_digest`, `standby_enabled`, `standby_listen_digest`, `drain_stdin` (superseded by `hook_payload`).

- [ ] **Step 4: Update `hooks/hooks.json`**

The Stop hook's hold is now purely local, so its timeout is a simple function of `STANDBY_HOLD` (900s) + slack. `SessionStart` and `UserPromptSubmit` do no RPC at all, so 10s is generous.

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
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" stop-hook || python \"${CLAUDE_PLUGIN_ROOT}/scripts/bct-chat.py\" stop-hook",
            "timeout": 960
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
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Add to `tests/test_push_hooks.py`:

```python
class HooksJsonTests(unittest.TestCase):
    def test_stop_timeout_covers_the_standby_hold(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "hooks", "hooks.json"), encoding="utf-8") as f:
            hooks = json.load(f)["hooks"]
        stop = hooks["Stop"][0]["hooks"][0]["timeout"]
        home = tempfile.mkdtemp()
        try:
            mod = load_fresh_module(home)
            self.assertGreater(stop, mod.STANDBY_HOLD,
                               "the Stop hook would be killed mid-hold")
        finally:
            shutil.rmtree(home, ignore_errors=True)
```

- [ ] **Step 5: Delete the superseded standby test**

```bash
git rm tests/test_standby_window.py
```

Its subject — a Stop hook that holds a `chat-listen` socket — no longer exists. Standby is now a local inbox wait, covered by `test_standby_mode_waits_for_an_item` above.

- [ ] **Step 6: Rebuild and run**

Run: `python3 scripts/build.py && python3 -m unittest discover -s tests`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add -A src scripts/bct-chat.py tests hooks/hooks.json
git commit -m "feat(delivery): local-only hooks read the inbox — 0s turn cost, chain cap, work/standby modes"
```

---

### Task 8: CLI — argparse and inbox-aware verbs

Fixes D13 (`wait` polls `chat-read`, *consuming* the cursor and stealing mentions from the push path) and the silent argument bugs (`wait --timeuot 60` means 300s; `heartbeat --interval -1` reaches `time.sleep(-1)`).

**Files:**
- Modify: `src/bctchat/cli.py`
- Test: rewrite `tests/test_listen.py` → `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv)` on `argparse`. Verbs: `join [name…]`, `leave`, `send <message…>`, `read`, `wait [--timeout N]`, `listen [--timeout N]`, `list`, `session-start`, `session-end`, `stop-hook`, `prompt-submit`, `daemon [--interval N] [--listen-timeout N]` (alias: `heartbeat`).
- `read` drains the inbox first, then `chat-read`. `wait` and `listen` wait on the **inbox** (the daemon owns the socket).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli.py
#!/usr/bin/env python3
"""The user-facing verbs must be inbox-aware, or they appear to have lost the very
messages the daemon just captured — and their arguments must be parsed, not guessed
(`wait --timeuot 60` silently meant 300s)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_heartbeat_helpers import load_fresh_module  # noqa: E402

IDENT = "C1A6063F-0124-4229-9CE3-D757348A70F2"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.mod = load_fresh_module(self.home)
        self.mod.save(self.mod.IDENTITY, {"participantID": IDENT, "name": "svr"})
        self.mod.sock_available = lambda: True
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.mod.main(argv)
        return buf.getvalue()

    def test_read_drains_the_inbox_before_the_socket(self):
        self.mod.inbox_put("yoros: @svr 봐줘", "svr")
        self.mod.rpc = lambda cmd, *a, **k: (self.calls.append(cmd),
                                             {"ok": True, "text": self.mod.NO_NEW})[1]
        out = self.run_main(["read"])
        self.assertIn("yoros: @svr 봐줘", out)
        self.assertEqual(self.calls, ["chat-read"])
        self.assertIsNone(self.mod.inbox_claim())          # drained, not re-delivered

    def test_wait_waits_on_the_inbox_not_on_chat_read(self):
        # D13: the old 2s chat-read poll CONSUMED the cursor and stole mentions from
        # the push path the daemon now depends on.
        import threading
        self.mod.rpc = lambda cmd, *a, **k: self.fail("wait must not poll the socket")
        threading.Timer(0.2, lambda: self.mod.inbox_put("pushed", "svr")).start()
        out = self.run_main(["wait", "--timeout", "5"])
        self.assertIn("pushed", out)

    def test_listen_waits_on_the_inbox(self):
        self.mod.rpc = lambda *a, **k: self.fail("listen must not hold the socket")
        self.mod.inbox_put("already here", "svr")
        self.assertIn("already here", self.run_main(["listen", "--timeout", "5"]))

    def test_a_misspelled_flag_is_an_error_not_a_default(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                self.mod.main(["wait", "--timeuot", "60"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_a_negative_interval_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                self.mod.main(["daemon", "--interval", "-1"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_send_goes_through_authed(self):
        self.mod.rpc = lambda cmd, *a, **k: (self.calls.append(cmd), {"ok": True, "text": ""})[1]
        self.run_main(["send", "hello", "room"])
        self.assertEqual(self.calls, ["chat-send"])

    def test_heartbeat_is_still_accepted_as_an_alias_for_daemon(self):
        seen = []
        self.mod.do_daemon = lambda **kw: seen.append(kw)
        self.run_main(["heartbeat", "--interval", "5"])
        self.assertEqual(seen[0]["presence_interval"], 5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest tests.test_cli -v`
Expected: FAIL — `wait` still polls `chat-read`; `--timeuot` is silently ignored.

- [ ] **Step 3: Implement `src/bctchat/cli.py`**

```python
"""The verbs. argparse, not hand-rolled scanning: `wait --timeuot 60` used to mean 300s
and `heartbeat --interval -1` reached time.sleep(-1)."""
import argparse, json, os, re, socket, subprocess, sys, time


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def positive(v):
    f = float(v)
    if f <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return f


def drain_inbox():
    """Everything the daemon captured, oldest first. The user's `read` must show it, or the
    verbs appear to have lost the very messages the daemon just saved."""
    out = []
    while True:
        got = inbox_claim()
        if not got:
            return out
        out.append(got[1].get("text") or "")
        inbox_ack(got[0])


def do_read():
    for text in drain_inbox():
        print(text)
    r = authed("chat-read", [])
    if not r.get("ok"):
        die(r.get("error", "error"))
    text = r.get("text", "")
    if text and text != NO_NEW:
        print(text)


def do_wait(timeout):
    """Wait on the INBOX. The daemon owns the socket; a second chat-read poller here would
    consume the cursor out from under it (D13)."""
    got = inbox_wait(timeout, poll=1.0)
    if not got:
        die(f"timeout: no new message within {int(timeout)}s")
    print(got[1].get("text") or "")
    inbox_ack(got[0])


def build_parser():
    p = argparse.ArgumentParser(prog="bct-chat.py", description="BCT group-chat external client")
    sub = p.add_subparsers(dest="verb", required=True)
    j = sub.add_parser("join"); j.add_argument("name", nargs="*")
    sub.add_parser("leave")
    s = sub.add_parser("send"); s.add_argument("message", nargs="+")
    sub.add_parser("read")
    sub.add_parser("list")
    for v in ("wait", "listen"):
        w = sub.add_parser(v)
        w.add_argument("--timeout", type=positive, default=300)
    for v in ("session-start", "session-end", "stop-hook", "prompt-submit"):
        sub.add_parser(v)
    for v in ("daemon", "heartbeat"):
        d = sub.add_parser(v)
        d.add_argument("--interval", type=positive, default=PRESENCE_INTERVAL)
        d.add_argument("--listen-timeout", type=positive, default=LISTEN_TIMEOUT)
        d.add_argument("--max-uptime", type=positive, default=None,
                       help=argparse.SUPPRESS)     # accepted and ignored: back-compat
    return p


def main(argv):
    a = build_parser().parse_args(argv)
    v = a.verb
    if v == "join":
        clear_join_state()                         # a human at the shell always wins
        do_join(" ".join(a.name) or default_name())
    elif v == "leave":
        do_leave()
    elif v == "send":
        r = authed("chat-send", [" ".join(a.message)])
        if not r.get("ok"):
            die(r.get("error", "error"))
    elif v == "read":
        do_read()
    elif v == "list":
        r = authed("chat-list", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif v in ("wait", "listen"):
        do_wait(a.timeout)
    elif v == "session-start":
        session_start()
    elif v == "session-end":
        session_end()
    elif v == "stop-hook":
        stop_hook()
    elif v == "prompt-submit":
        prompt_submit_hook()
    elif v in ("daemon", "heartbeat"):
        do_daemon(presence_interval=a.interval, listen_timeout=a.listen_timeout)
```

Note the import line for this module is `import argparse, json, os, re, socket, subprocess, sys, time` — `argparse` is stdlib and is the only new import in the whole rework.

- [ ] **Step 4: Delete the superseded test and rebuild**

```bash
git rm tests/test_listen.py
python3 scripts/build.py && python3 -m unittest discover -s tests
```
Expected: OK. `tests/test_tcp_transport.py::test_read_over_tcp_round_trip` still passes (an empty inbox + a `chat-read` reply).

- [ ] **Step 5: Commit**

```bash
git add -A src scripts/bct-chat.py tests
git commit -m "feat(cli): argparse verbs; read/wait/listen are inbox-aware (the daemon owns the socket)"
```

---

### Task 9: CI — the three-platform matrix

The repo exists to support Windows and has never once executed its test suite there.

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the workflow**

```yaml
name: ci

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.9", "3.12"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - name: Build the artifact and assert it is in sync with src/
        run: |
          python scripts/build.py
          git diff --exit-code scripts/bct-chat.py
      - name: Test
        run: python -m unittest discover -s tests -v
```

Python 3.9 is the floor: it is what an unmanaged Debian/RHEL remote still ships, and the client must run there with no venv.

- [ ] **Step 2: Verify the suite passes under the floor version locally**

Run: `python3 -m unittest discover -s tests`
Expected: OK. Then read the diff for any syntax newer than 3.9 (no `match`, no `X | Y` type unions at runtime, no `str.removeprefix`… — the code uses none, but check).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the suite on ubuntu/macos/windows and assert the artifact is in sync"
```

---

### Task 10: Docs and release

**Files:**
- Modify: `README.md`, `skills/claude-group-chat-remote/SKILL.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`

- [ ] **Step 1: Bump the version and write the CHANGELOG entry (same commit — the devmode gate enforces this)**

`.claude-plugin/plugin.json`: `"version": "2.0.0"`.

`CHANGELOG.md`, newest first:

```markdown
## 2.0.0

Receive rework — the daemon is now the ear, and a mention is never lost.

- **Capture and delivery are split.** The presence daemon holds `chat-listen`
  continuously and writes every mention to a durable local inbox
  (`~/.bct-chat/inbox/`) *before* issuing the next listen. The Stop and
  UserPromptSubmit hooks no longer touch the socket at all: they atomically claim
  an inbox item and deliver it. A hook that is killed can no longer lose a message
  whose server-side cursor had already moved — the loss class is gone, not patched.
- **Turn cost is ~0 in work mode** (the default). The Stop hook returns in
  milliseconds. `BCT_CHAT_MODE=standby` makes it wait on the inbox locally for up
  to 15 minutes — near-real-time, zero tokens, zero RPC. `BCT_CHAT_STANDBY` is
  retired (`BCT_CHAT_STANDBY=0` still maps to work mode).
- **The daemon no longer dies of a dead tunnel.** It backs off (60s → 300s) and
  waits, every hook is a respawn point, and a signal-killed daemon is detected in
  ~90s instead of 8 minutes. Its exit conditions are exactly three: no live claude
  session on this host, a newer daemon, or a room the user has left.
- **Membership is a bounded budget, not an endless cooldown.** Three denied or
  expired requests suspend automatic joining for good; only a human running
  `bct-chat.py join` at the remote's shell resumes it. `leave` stays left.
- **Robustness:** every state write is atomic; a zombie socket file is detected by
  connecting, not by `os.path.exists`; `rpc` has an overall deadline and tolerates
  keepalives and coalesced frames; process liveness never calls `os.kill` on
  Windows (it terminates); `BCT_CHAT_HOME` isolates the state dir, which is what
  makes the suite safe to run on Windows at all.
- **Source is now modular** (`src/bctchat/`), and `scripts/bct-chat.py` is the
  *generated* single-file artifact — still one file to `scp`, still `python3
  ~/.bct-chat/bct-chat.py`. `python3 scripts/build.py` regenerates it; CI (ubuntu /
  macos / windows) fails if the committed artifact is stale.

Known limitations: a cold-idle session (one that has never taken a turn) cannot be
woken — Claude Code has no channel for an external process to inject input into an
idle session. The mention is captured and delivered at that session's next prompt.
On Windows a crashed session's marker is collected by a 7-day TTL rather than by pid.
```

- [ ] **Step 2: Update `README.md`**

Replace the receive-model section with the split model: the daemon captures; the hooks deliver. Document `BCT_CHAT_MODE=work|standby` (default `work`), `BCT_CHAT_HOME`, and that `scripts/bct-chat.py` is generated (contributors edit `src/bctchat/` and run `python3 scripts/build.py`). Keep the `scp scripts/bct-chat.py <host>:` instruction verbatim — it is a load-bearing contract.

- [ ] **Step 3: Update `skills/claude-group-chat-remote/SKILL.md`**

The verbs claude is told to run are unchanged (`send`, `read`, `list`) — verify each command in the skill prose still exists in `build_parser()`. Add one line: mentions now arrive automatically at turn boundaries whether or not the session is idle, and `read` also drains anything captured while the session was away.

- [ ] **Step 4: Run the full gate one last time**

Run:
```bash
python3 scripts/build.py && git diff --exit-code scripts/bct-chat.py && python3 -m unittest discover -s tests
```
Expected: no diff (artifact in sync) and OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: 2.0.0 — daemon-as-ear receive model, work/standby modes, generated artifact"
```

---

## Post-plan verification (before the PR)

The unit suite cannot prove the thing this rework exists for. Do this on `rsbglee` against the live BCT, per the two-repo deploy note in memory (stable copy **and** plugin cache):

1. Deploy: `scp scripts/bct-chat.py rsbglee:~/.bct-chat/bct-chat.py` and update the plugin cache copy.
2. Start a claude session; confirm `~/.bct-chat/heartbeat.pid` appears and its mtime advances.
3. Mention the host from BCT while the remote session sits idle at the prompt → confirm a file lands in `~/.bct-chat/inbox/` within a second.
4. Type anything at the idle session → the digest arrives with the prompt (UserPromptSubmit path).
5. Kill the daemon with `SIGKILL`; take one turn → confirm a hook respawned it within that turn.
6. Break the tunnel for 10 minutes; restore it → confirm the daemon reseats itself with no restart of claude.
7. `python3 ~/.bct-chat/bct-chat.py leave` → confirm no join banner appears in BCT for the next 10 minutes.
