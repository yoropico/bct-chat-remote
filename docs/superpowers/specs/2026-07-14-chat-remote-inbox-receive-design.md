# Design — remote-chat receive rework: daemon-as-ear + local inbox

Date: 2026-07-14
Repo: `bct-chat-remote` (the plugin/kit half; BCT itself is a separate repo)
Status: approved, pending implementation plan

## 1. Why

Three independent read-only audits of `scripts/bct-chat.py` (635 lines, single
module) converged on the same shape of problem: **the happy path is tight, but
every off-nominal path drops the host out of the room silently and permanently.**
No banner, no stderr, no retry. The user's only recovery is to restart claude.

The defects cluster into three groups.

### 1.1 Silent permanent failures

| # | Defect | Where |
|---|---|---|
| D1 | Daemon has exactly ONE spawn point (`SessionStart`) and dies after 2 failed ticks. An 8-minute tunnel blip permanently un-hearts a long-lived session. The delivery hooks use raw `rpc()`, not `authed()`, so after BCT prunes the host every hook gets `NOT_INVITED` and swallows it — that session never receives another mention for its whole life. | `bct-chat.py:306`, `:406`, `:475`, `:501` |
| D2 | A daemon killed by a signal (ssh logout SIGTERM) leaves a pidfile with a *fresh* mtime. `heartbeat_alive()` is mtime-only, so the corpse reads as live for 8 minutes — exactly the window in which the user reconnects and restarts claude, which is the only spawn opportunity. | `:217-228` |
| D3 | If the socket is absent at `SessionStart`, the hook early-returns: no marker, no join, no daemon. Nothing ever repairs that session (a claude started inside tmux before the tunnel is up). | `:386` |
| D4 | `sock_available()` on the unix path is `os.path.exists` only. A zombie socket file (ssh reconnect without `StreamLocalBindUnlink`) reads as healthy; `membership_live()` then treats the resulting `ECONNREFUSED` as "membership live" (by design: any non-`NOT_INVITED` error counts as live), so no join is requested. Completely asymptomatic total failure. | `:47-55`, `:172-180` |
| D5 | `authed()` fires a second `chat-join` while a request is already outstanding — the daemon and `session_start` both guard against this; `authed()` does not. The user approves banner #1; the client is polling request #2; it expires; 30-minute cooldown. **The user approved and the host still never seats.** | `:430-437` |
| D6 | `claim_pending()` deletes `pending-join.json` only on `approved`/`denied`/`expired`. Any other reply (BCT restarted and no longer knows the request id) wedges the file forever. Both auto-join callers prefer polling PENDING over requesting, so the rejoin branch becomes unreachable. Silently absent from the room, forever. | `:194-196` |
| D7 | Hook timeouts are smaller than the verbs' worst-case RPC budget (`prompt-submit`: 23s vs 15s; `stop-hook`: 56s vs 45s). `chat-read` advances the server cursor, then the hook is killed before printing. There is no ack verb, so that backlog is permanently lost. | `hooks.json:31,42` |

### 1.2 Wrong policy

| # | Defect | Where |
|---|---|---|
| D8 | `leave` removes the identity but not the session markers, and does not stop the daemon. Four minutes later the daemon gets `NOT_INVITED` and re-requests membership in the room the user just left. A kick behaves identically. | `:605-610`, `:287-296` |
| D9 | The 30-minute cooldown rate-limits the nag but never ends it: a denied host re-requests every 30 min for the daemon's 12-hour life (~24 banners). | `:110-138` |
| D10 | The Stop-hook standby window opens at EVERY turn end, not just idle ones — a remote claude doing pure coding work pays up to 40s of hook hold on every answer. `stop_hook_active` is never inspected, so answering a mention costs a second window. Two standby remotes mentioning each other can ping-pong billed turns with no human in the loop; the only brake lives in BCT's repo. | `:511-525` |
| D11 | Idle coverage is ~30s per turn — for a session that answers once every 10 minutes, ~5%. A session parked at the prompt after 50 turns is as deaf as a cold-idle one; the CHANGELOG claims only cold-idle is uncovered. | design |
| D12 | Session markers carry no liveness data and are never GC'd. A crashed claude leaks its marker; the daemon then keeps the host present in the room, with nobody home, for up to 12h. | `:200-214` |
| D13 | `wait` polls `chat-read` every 2s, which *consumes* the cursor — it steals mentions from the cursor-preserving push path. | `:573-589` |

### 1.3 Robustness / platform

| # | Defect | Where |
|---|---|---|
| D14 | `save()` and `ensure_stable_copy()` truncate-then-write with no temp+rename. A kill inside the 10s SessionStart budget leaves a 0-byte `identity.json` (→ spurious rejoin, cursor lost) or a truncated `~/.bct-chat/bct-chat.py` — which is the file the skill tells claude to run. | `:97-100`, `:159-162` |
| D15 | `ensure_stable_copy()` sits OUTSIDE `session_start()`'s catch-all and guards only `OSError`. A non-UTF-8 (truncated, per D14) stable copy raises `UnicodeDecodeError` → the hook exits nonzero → hooks.json's `|| python` fallback re-runs it with stdin already drained → no session id → no marker, no daemon. Every subsequent SessionStart on that host takes the same path. | `:383`, `:150-157` |
| D16 | `rpc()`'s recv loop has a per-`recv` timeout, not an overall deadline (a dribbling bridge blocks forever), cannot tolerate a keepalive newline or two frames in one read, and reports a server that closes without replying as "malformed response". `chat-listen`'s long hold is exactly where a bridge author later adds a keepalive. | `:75-84` |
| D17 | Tests isolate state with `env["HOME"]`. **Windows' `ntpath.expanduser()` ignores `HOME`** and reads `USERPROFILE` — so on the platform this repo exists to support, running the suite clobbers the developer's real `~/.bct-chat/identity.json` and SIGKILLs their live daemon. | `tests/*` |
| D18 | All 44 tests use the `tcp:` transport. The AF_UNIX path — the *production* transport on macOS and Linux — has zero coverage, which is why D4 was never caught. There is no CI at all. | `tests/*` |

## 2. Goals / non-goals

**Goals**
- A mention is never lost, regardless of what dies when.
- A remote claude that has fallen out of the room gets back in without a human restarting it.
- Turn-boundary cost for a working session is ~0.
- A dedicated standby participant is reachable in near-real-time without burning tokens.
- Windows, Linux and macOS are all genuinely tested in CI.
- The single-file deployable (`scp`, `__file__` re-exec, stable copy, `REPLY_HINT`) is preserved byte-for-byte in its contract.

**Non-goals**
- Waking a cold-idle session (one that has never taken a turn). Claude Code offers no channel for an external process to inject input into an idle session. We capture the message durably and deliver it at the first opportunity; we do not pretend to solve the wake.
- Changing BCT's wire protocol. Everything here uses verbs that already exist (`chat-join`, `chat-join-poll`, `chat-list`, `chat-read`, `chat-peek`, `chat-listen`, `chat-leave`, `chat-send`).

## 3. Architecture

The root cause behind D1/D7/D10/D11 and the multi-session races is that **capture
and wake are the same act**: the hooks do the listening. If the hook doesn't run,
nothing is heard; if the hook dies after the cursor moved, the message is gone.

Split them.

```
  ┌─────────────── CAPTURE (one daemon per host) ────────────────┐
  │  holds chat-listen continuously                              │
  │  mention arrives -> write inbox/<ns>-<pid>.json (temp+rename)│
  │  interleaves a chat-list presence tick every 240s            │
  └──────────────────────────┬───────────────────────────────────┘
                             │  local filesystem only
  ┌──────────────────────────┴───────── DELIVERY (per session) ──┐
  │  Stop / UserPromptSubmit hooks. NO socket, NO RPC.           │
  │  atomically claim an inbox item -> compose digest -> deliver │
  │  work mode:    hold 0s   (turn cost ~0)                      │
  │  standby mode: wait on the inbox locally, up to 15 min       │
  └──────────────────────────────────────────────────────────────┘
```

Consequences, in order of importance:

- **D7 dies as a class.** The hooks perform zero RPC, so "hook timeout < RPC
  budget" is not a thing that can happen. The `hooks.json` Stop timeout becomes
  a simple function of the standby hold, and in work mode the hook returns in
  milliseconds.
- **D11 dies.** Capture coverage is 100% because the daemon is always listening.
  Wake is best-effort and mode-dependent, which is the honest split.
- **D10 dies.** Work mode holds for 0 seconds.
- **The multi-session races die.** One listener per host (the daemon), and
  delivery is decided by an atomic `rename` — exactly one session wins each
  message, deterministically.
- **The loss class dies.** The cursor only advances after the message is on
  local disk.

The cost: **the daemon now holds state, so daemon reliability IS receive
reliability.** That makes the D1/D2/D3 fixes a precondition, not a nice-to-have.

## 4. Components

### 4.1 Inbox — `~/.bct-chat/inbox/`

A directory queue. One file = one mention event.

- Name: `<time.time_ns()>-<pid>.json` (lexically sortable ≈ chronological).
- Body: `{"text": "<the room lines exactly as BCT returned them>", "capturedAt": <epoch>, "name": "<our participant name at capture time>"}`.
- Written temp-then-`os.replace()` into place. **The daemon does not issue the
  next `chat-listen` until the file is in place** — so a message whose
  server-side cursor has advanced is always already durable locally.
- Cap: 50 items. Beyond that the oldest are dropped and a `(오래된 멘션 N건 생략)`
  marker is prepended to the next delivered digest. A queue that deep means
  nobody has been home for a long time; unbounded growth is worse.

`~/.bct-chat/processing/` holds items a hook has claimed but not yet delivered.

### 4.2 Capture — the daemon (`presence`)

```
mark = own the pidfile (see 4.5)
loop:
    gc_dead_markers()                     # D12 — BEFORE the liveness test below
    if not live_sessions(): exit          # every claude on this host is gone
    if pidfile owner is not me: exit      # a newer daemon took over
    if suspended and not seated: exit     # the user left / denied us out (4.4)
    touch pidfile                         # liveness for heartbeat_alive()
    if not seated: run the join budget (see 4.4); continue
    if 240s since last presence tick: chat-list       # prune defence
    r = chat-listen  (holds ~30s server-side)
    if r is a mention: inbox_put(r.text)  # BEFORE the next listen
    on socket failure: exponential backoff 60s -> 300s, retry forever
```

**The `fails >= 2` suicide is deleted.** A dead tunnel is a thing to wait for, not
to die of — which is what fixes D3 (a claude started before the tunnel is up now
gets a marker and a daemon that simply waits).

**The 12-hour `HEARTBEAT_MAX_UPTIME` backstop is also deleted.** Its only purpose
was to bound the damage from a leaked session marker, and marker GC (4.5) now
removes leaked markers directly. Keeping it would be actively harmful in the new
design: a genuinely live session whose daemon hits 12h would lose its capture
layer, and if that session is idle no hook fires to respawn it. Exit conditions
are now exactly three: no live markers, a newer daemon, or suspended-and-unseated.

### 4.3 Delivery — the hooks (`delivery`)

Both hooks are local-only. Neither opens a socket.

```
stop_hook():
    ensure_daemon()                       # D1: every hook is a spawn point
    chain = stop_hook_active from the Stop payload
    if chain_count >= 3: return           # D10: loop cap; item stays in the inbox
    item = inbox_claim()                  # os.rename -> processing/; atomic, race-safe
    if not item and mode == standby:
        item = inbox_wait(hold)           # local poll, default 15 min, 0 tokens
    if not item: return                   # graceful degradation: daemon keeps listening
    print block-JSON(digest(item)); inbox_ack(item)

prompt_submit_hook():
    ensure_daemon()
    item = inbox_claim()                  # never holds
    if item: print digest(item); inbox_ack(item)
```

- `inbox_claim()` is `os.rename(inbox/x, processing/<pid>-x)`. Two sessions
  racing: the loser's rename raises, it takes the next item or none. No locks.
- `inbox_ack()` deletes the processing file.
- **Orphan recovery**: any `processing/` file older than 120s belonged to a hook
  that died; it is moved back to `inbox/`. This makes delivery *at-least-once* —
  a rare duplicate instead of a silent loss. That is the correct direction, and it
  also makes the `|| python` fallback (which can re-run a signal-killed hook)
  harmless.
- `compose_digest()` caps the room text at 200 lines / 16 KB with an elision
  marker (bounds the rotted-backlog dump).

**Chain counting (D10):** the Stop payload's `stop_hook_active` tells us the turn
is only continuing because we blocked it. We keep a per-chain counter in
`~/.bct-chat/chain.json` (reset whenever `stop_hook_active` is false, i.e. a
genuine user-initiated turn ended). At 3 automatic re-engagements the hook stops
delivering and stops holding; the mention stays in the inbox and is delivered on
the user's next prompt. Message preserved, spend bounded.

### 4.4 Membership — the join budget

Replaces the flat 30-minute cooldown (D9) and the un-guarded `authed()` (D5).

State in `~/.bct-chat/join-state.json`:
`{"attempts": N, "nextAttemptAt": <epoch>, "suspended": <bool>, "lastOutcome": "..."}`

- Automatic attempt backoff: 60s → 300s → 1800s.
- Three consecutive `denied`/`expired` outcomes → `suspended: true`. **No further
  automatic requests, ever**, until a human runs `bct-chat.py join` at the remote's
  shell (which clears the whole state — manual intent always wins).
- `leave` sets `suspended: true` and drops `identity.json`/`pending-join.json`.
  It does **not** touch the session markers — those describe which claude sessions
  are alive, which is still true after leaving the room. The daemon stands down on
  its next tick via the `suspended and not seated` exit, and `ensure_daemon()`
  refuses to respawn it while suspended-and-unseated, so the hooks cannot
  resurrect it either. A manual `join` clears `suspended`, at which point the next
  hook (or the `join` verb itself) spawns the daemon again. That is what makes
  `leave` stick (D8) without destroying the refcount that a later rejoin needs.
  A kick is indistinguishable from a BCT restart on the wire, so the budget
  absorbs it: at most 3 backed-off re-requests instead of one every 30 minutes
  forever.
- `pending-join.json` gains `requestedAt`; it is discarded after a 10-minute TTL
  regardless of what `chat-join-poll` says. This is what unwedges D6 — an
  unrecognized poll error can no longer make the rejoin branch unreachable.
- Every automatic-join caller (`session_start`, the daemon, and now `authed()`)
  goes through ONE function that checks PENDING first. D5's duplicate `chat-join`
  becomes structurally impossible.
- `do_join(wait_approval=True)` decides success by **whether `identity.json` now
  exists**, not by whether PENDING vanished — the daemon may legitimately have
  claimed the approval first. Fixes the false `died: denied or expired` on a
  join the user actually approved.

### 4.5 Daemon liveness and markers

- `heartbeat_alive()`: pidfile mtime < **90s** (the daemon touches it every tick,
  and its tick is bounded by the listen hold), *plus* a real liveness check on the
  recorded pid. Stale-corpse detection drops from 8 minutes to ~90 seconds (D2).
- **Process liveness must never use `os.kill(pid, 0)` on Windows** — CPython's
  `os.kill` there calls `TerminateProcess` for any signal, i.e. it would kill the
  daemon it is probing. Cross-platform helper:
  - POSIX: `os.kill(pid, 0)`.
  - Windows: `ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)` — a
    NULL handle means the process is gone. (`ctypes` is stdlib; the pure-stdlib
    constraint holds.)
  This helper is what also makes marker GC possible on both platforms.
- Session markers become `{"pid": <pid>, "startedAt": <epoch>}` instead of empty
  files. The daemon GCs markers whose pid is dead on every tick (D12), so a
  crashed claude no longer keeps a phantom host in the room for 12 hours.

### 4.6 Wire (`wire`)

- `sock_available()` **connects** on the unix path too, instead of
  `os.path.exists` — a zombie socket file is no longer "healthy" (D4).
- `rpc()` gets an **overall deadline** (not a per-`recv` timeout), skips blank
  keepalive lines, and parses the first complete JSON line rather than the whole
  buffer (D16). A server that accepts and closes without replying is reported as
  a connection failure, not as "malformed response".
- `tcp_target()` gains validation and IPv6 handling, and gets unit tests (it has
  none today).

### 4.7 Verbs

The daemon now consumes mentions, so the user-facing verbs must be inbox-aware or
they will appear to have lost messages:

- `read` — drains the inbox first, then does `chat-read` for the rest.
- `listen` — waits on the **inbox**, not the socket (the daemon owns the socket).
- `wait` — same; the 2s `chat-read` poll that stole mentions from the push path
  (D13) is deleted.
- `send`, `list`, `join`, `leave` — unchanged except for the membership rework.
- CLI argument parsing moves to `argparse` (stdlib): today `wait --timeuot 60`
  silently means 300s, `heartbeat --interval -1` reaches `time.sleep(-1)`, and
  `--flag=value` is silently ignored.

### 4.8 Modes

`BCT_CHAT_MODE=work|standby`, default `work`. Legacy `BCT_CHAT_STANDBY=0` maps to
`work` (back-compat; `BCT_CHAT_STANDBY` is otherwise retired).

| | Stop-hook hold | Turn cost | Idle coverage |
|---|---|---|---|
| `work` (default) | 0s | ~0 | next turn boundary / next prompt |
| `standby` | up to 15 min, local wait | ~0 tokens | effectively real-time |

`hooks.json`'s Stop timeout is set to the standby hold + slack (16 min). In work
mode the hook returns in milliseconds, so a large timeout costs nothing.

## 5. Atomicity

Every write in the state dir goes temp + `os.replace()`: `save()`, the inbox, the
markers, `ensure_stable_copy()` (D14). `os.replace` is atomic on Windows too.

`ensure_stable_copy()` moves INSIDE `session_start()`'s catch-all and widens its
guard from `OSError` to `Exception`, so a corrupt stable copy can no longer make
the hook exit nonzero and trigger the stdin-drained re-run (D15).

## 6. Cross-platform

- **`BCT_CHAT_HOME`** overrides the state directory. Production behaviour is
  unchanged (default `~/.bct-chat`); tests isolate with it instead of `HOME`,
  which is what makes the suite safe to run on Windows at all (D17).
- Transport: AF_UNIX on macOS/Linux, `tcp:host:port` on Windows (CPython has no
  AF_UNIX there). **Both** transports get test coverage; today only `tcp:` does
  (D18).
- Process liveness via the `ctypes`/`OpenProcess` helper (4.5) — never `os.kill`
  on Windows.
- `ensure_stable_copy()` writes with an explicit newline policy so the
  content-equality check doesn't thrash on CRLF.
- **CI**: GitHub Actions matrix over `ubuntu-latest`, `macos-latest`,
  `windows-latest`. There is no CI today, so the Windows support claim has never
  been executed anywhere.

## 7. Module split

Approved shape: **modular source, generated single-file artifact.** The single-file
property is load-bearing in four places (`ensure_stable_copy` copies one file;
README's `scp scripts/bct-chat.py`; `spawn_heartbeat` re-execs `__file__`;
`REPLY_HINT` tells claude to run `~/.bct-chat/bct-chat.py`), so it cannot simply
become a package.

```
src/bctchat/config.py       paths, sentinels, tunables, mode
src/bctchat/wire.py         tcp_target, connect, sock_available, rpc
src/bctchat/state.py        load/save/forget (atomic), markers, stable copy, proc liveness
src/bctchat/inbox.py        put / claim / ack / wait / orphan recovery / cap
src/bctchat/membership.py   identity, pending, join budget, do_join, authed
src/bctchat/presence.py     pidfile, heartbeat_alive, spawn/ensure_daemon, the daemon loop
src/bctchat/delivery.py     hook_session_id, digest, chain counter, the four hook verbs
src/bctchat/cli.py          argparse, verbs, main
scripts/build.py            strips intra-package imports, concatenates -> scripts/bct-chat.py
tests/test_build.py         asserts the committed artifact is in sync with src/
```

The artifact is a **single namespace** (concatenation, not a package), so the
existing tests' module-global monkeypatching (`mod.rpc`, `mod.sock_available`,
`mod.subprocess`, …) keeps working. This is not incidental: if the split used
`from wire import rpc`, those patches would silently stop taking effect and the
suite would stay green while testing nothing.

The build-sync test wires into the devmode pre-commit gate, so a stale artifact
cannot be committed.

## 8. Error handling

| Failure | Behaviour |
|---|---|
| Tunnel down | Daemon backs off (60s→300s) and waits forever. No suicide. Hooks still deliver anything already in the inbox. |
| Daemon dead | Any hook (`SessionStart`, `Stop`, `UserPromptSubmit`) respawns it — hooks are now cheap enough to afford the check. |
| Hook dies mid-delivery | The `processing/` orphan sweep returns the item to the inbox after 120s. At-least-once. |
| BCT restarted | Daemon's `chat-list` gets `NOT_INVITED` → join budget → backed-off re-request. PENDING TTL prevents a wedge. |
| User denies / kicks / leaves | Budget suspends after 3 outcomes; `leave` suspends immediately. Only a manual `join` resumes. |
| Inbox full (50) | Oldest dropped, elision marker in the next digest. |
| Corrupt state file | `load()` returns `None`; the caller treats it as absent. Atomic writes make torn files unreachable in the first place. |

## 9. Testing

Existing 44 tests keep running against the generated artifact. New coverage:

- AF_UNIX transport (currently zero) and the zombie-socket case.
- Inbox: atomic put, two hooks racing `inbox_claim()`, orphan recovery, the cap.
- Daemon: respawn from each hook; backoff instead of suicide; no exit on a dead
  tunnel; marker GC with a dead pid; 90s stale-pidfile detection; exit when
  suspended-and-unseated, and `ensure_daemon()` refusing to respawn it there.
- Membership: PENDING TTL expiry with an unrecognized poll error (D6); `authed()`
  refusing to double-join (D5); budget exhaustion after 3 denials (D9); `leave`
  staying left (D8); `do_join` not misreporting a daemon-claimed approval.
- Delivery: chain cap at 3; work mode holds 0s; standby mode returns as soon as
  the inbox fills; digest cap.
- Wire: chunked response, keepalive newline, two frames in one read, overall
  deadline, server closing without a reply.
- Build: artifact in sync with `src/`.
- CI matrix: ubuntu / macos / windows.

## 10. Known limitations (documented, not fixed)

- **Cold idle.** A session that has never taken a turn has no Stop hook to hold,
  so it cannot be woken. The daemon still captures the mention; it is delivered on
  that session's first prompt. There is no supported channel to inject input into
  an idle Claude Code session, so this is a platform limit, not an implementation
  gap.
- **Standby hold expiry.** After 15 minutes the hook exits and the session goes
  idle. Capture continues; only the wake degrades. This is the deliberate
  "bounded hold + graceful degradation" choice.
- **Duplicate delivery.** The orphan sweep can re-deliver a message whose hook
  died after printing. Chosen over silent loss.
