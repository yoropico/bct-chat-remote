# SPEC — bct-chat-remote (the client contract)

What this plugin promises. Behaviour lives here; rationale lives in
`docs/superpowers/specs/`, and history in `CHANGELOG.md`.

Scope: the client half only. BCT (the Swift app that hosts the room) ships from
its own repo; this document never redefines its wire protocol.

## 1. Shipping contract

- `scripts/bct-chat.py` is a **single file, pure stdlib, python3**. It runs on an
  unmanaged remote with no venv and no install step: `scp scripts/bct-chat.py
  <host>:` is a supported deployment.
- It is **generated** from `src/bctchat/*.py` by `python3 scripts/build.py`, and
  the generated artifact is committed. `tests/test_build.py` fails if the two
  drift.
- The artifact is a flat concatenation in **one namespace** — not a package. Its
  module globals are the monkeypatch surface the test suite relies on.
- On any run it keeps a canonical copy at `~/.bct-chat/bct-chat.py` (the path the
  skill prose and `REPLY_HINT` tell claude to invoke), and it re-execs that same
  file to spawn its daemon.

## 2. State

All under `~/.bct-chat/` (override the whole directory with `$BCT_CHAT_HOME`):

| Path | Meaning |
|---|---|
| `identity.json` | `{"participantID", "name"}` — our seat in the room |
| `pending-join.json` | an outstanding join request; discarded once older than `PENDING_TTL` |
| `join-state.json` | `{"attempts", "nextAttemptAt", "suspended", "lastOutcome"}` — the join budget |
| `sessions/<session-id>` | one marker per live claude session on this host (the daemon's refcount) |
| `heartbeat.pid` | the presence daemon's pidfile; its mtime is the liveness signal |
| `bct-chat.py` | the stable copy |
| `inbox/<time_ns>-<pid>.json` | a captured mention, not yet claimed by a hook |
| `processing/<pid>-<time_ns>-<pid>.json` | a mention claimed by a hook, not yet acked |
| `dropped.json` | `{"n"}` — mentions the inbox cap has thrown away, not yet reported |

Every write under this directory is atomic (temp file + `os.replace`, atomic on both
POSIX and Windows): a hook killed mid-write can never leave a 0-byte `identity.json`
or a truncated stable copy.

## 3. Transport

`$BCT_CHAT_SOCK` (default `~/.bct-chat.sock`, the ssh-`RemoteForward`ed BCT
control socket). `tcp:<host>:<port>` selects TCP — the supported transport on
Windows, where CPython has no AF_UNIX; IPv6 hosts are bracketed
(`tcp:[::1]:9000`). Wire: one line of JSON in (`{"paneID","cmd","args"}`), one
line of JSON out (`{"ok","text","error"}`).

- `sock_available()` decides availability by **connecting**, never by checking
  that a unix socket path exists on disk — a stale socket file left by an ssh
  reconnect without `StreamLocalBindUnlink` still exists but nothing is
  listening, and a plain existence check would read that as healthy.
- `rpc()` is bounded by one **overall deadline** covering the whole call —
  connecting, writing the request, and every read — not a per-`recv` timeout
  that a bridge dribbling bytes (e.g. a keepalive) could keep resetting
  forever. A slow accept can't leave a stale, fresh-sized timeout in force for
  the request write either: the socket is re-armed with whatever of the
  deadline remains immediately before `sendall`. Blank lines are keepalives
  and are skipped; if a read coalesces two JSON frames, `rpc()` answers from
  the first and discards the rest. A bridge that accepts the connection and
  closes it without replying is reported as a socket error, not a malformed
  response.

## 4. Verbs

| Verb | Behaviour |
|---|---|
| `join [name]` | request a seat; blocks up to 5 min for the user's approval in BCT's chat dock. Manual intent overrides any cooldown. |
| `leave` | leave the room and drop the identity |
| `send <msg>` | post to the room |
| `read` | print unseen messages (advances the server-side cursor) |
| `list` | the roster |
| `wait [--timeout N]` | poll until a new message arrives |
| `listen` | hold one server-push `chat-listen` window (~30 s server-side) |
| `heartbeat` | the presence daemon (spawned automatically; not for humans) |
| `session-start`, `session-end`, `stop-hook`, `prompt-submit` | the Claude Code hook verbs |

## 5. Hook behaviour

The four hook verbs are wired in `hooks/hooks.json`.

- **Invariant: a hook verb always exits 0.** `hooks.json` falls back from
  `python3` to `python` on ANY nonzero exit, and that re-run would see stdin
  already drained — no session id, no marker, no daemon, and a duplicate join
  banner. Every hook swallows `Exception` and `SystemExit` alike.
- **Invariant: a hook is silent unless it has something to deliver.** A broken
  tunnel must never disturb a turn.
- Inside a BCT pane (`$BCT_PANE_ID` set) every hook verb is a no-op — BCT's
  native chat injection owns delivery there.
- `session-start`: keep the stable copy current, mark this session live, and make
  sure the host is in the room and a daemon is running.
- `session-end`: drop this session's marker. The daemon is not killed — another
  session on this host may still be in the room.
- `stop-hook`: when the room has mentioned us, block the finishing turn with the
  digest so claude answers the room in place.
- `prompt-submit`: same detection, but the digest rides along as context with the
  user's next prompt — this is what reaches a session that was idle.

## 6. Inbox

The durability boundary between capture and delivery. Pure local filesystem — no
socket, no RPC. The presence daemon (§7) does not issue its next `chat-listen`
until a heard mention is durably here, so BCT's server-side cursor only ever
advances after the message is already on local disk. Hooks are local-only
readers of this queue: no socket, no RPC.

- **Put**: one mention, one file (`inbox/<time_ns>-<pid>.json`, atomic
  temp+`os.replace`), shape `{"text", "capturedAt", "name"}`. Never partially
  visible.
- **Claim**: an `os.rename` of the oldest inbox file into `processing/`. Exactly
  one reader ever wins a given item — verified under real concurrent-thread
  stress, not just sequential calls, that two racing `os.rename`s on the same
  source are mutually exclusive. A corrupt or unparsable item is dropped on
  claim, never handed to claude.
- **Ack**: deletes the `processing/` file. Idempotent.
- **Orphan recovery**: a `processing/` item older than `ORPHAN_AGE` (120 s, well
  past any hook's own timeout) is renamed back into `inbox/`. Delivery is
  **at-least-once, never at-most-once** — a hook that died between claim and ack
  leaves nothing stuck, at the cost of a rare duplicate if it merely ran long.
- **Cap**: `INBOX_CAP` (50) items. A `put` past the cap evicts the oldest first,
  counting what it dropped in `dropped.json`, read-and-cleared by
  `take_dropped()`. Eviction claims its victim by the same `os.rename`
  arbitration `inbox_claim()` uses, not a bare `os.remove`: eviction must count
  only the files it actually removed, which requires eviction and claim to
  compete through the same primitive so that losing is observable — a bare
  `os.remove`'s failure is silently swallowed, which is exactly how a naive
  version double-counts an item that `inbox_claim()` already delivered as also
  dropped. `take_dropped()` and the daemon's own counter bump race each other
  the same way, by the same primitive, so neither a concurrent reader nor a
  concurrent bump can lose or double-report a count. `dropped.json` has exactly
  one writer (the daemon's cap eviction); a corrupt item found on claim is
  discarded uncounted rather than bumping this counter from a hook, which would
  reopen it to concurrent writers.
- **Sidecar sweep**: `.claim`/`.bump`/`.evict`/`.tmp` files left by a process
  that died mid rename-steal or mid atomic write are swept once their owner is
  gone. Every sidecar name carries the pid that created it
  (`<path>.<pid>.<kind>`); the sweep parses that pid and skips any sidecar
  whose owner is still alive, whatever its mtime — a `.claim`/`.bump` sidecar
  is born by `os.rename`, which preserves the source's mtime rather than
  resetting it, so it can read as already older than `ORPHAN_AGE` the instant
  it's created if `dropped.json` sat unwritten for a while before the steal. A
  mtime-only test cannot tell that case apart from a genuinely abandoned
  sidecar, and there is no way to close the gap with timing (e.g. refreshing
  the mtime right after the rename), since the rename and the refresh can
  never be made atomic with each other — a sweep can always land in between.
  Checking the owner's liveness instead of racing the clock is what actually
  closes the race. Once the owner is confirmed dead, `ORPHAN_AGE` is still
  checked as a second condition, so a pid recycled by an unrelated new process
  can't make an otherwise-fresh sidecar look sweepable.

## 7. Presence

One daemon per host. It exists because BCT prunes an external participant after
10 minutes of silence, so a live-but-quiet host must keep proving it is there.

- Spawned by `session-start`, detached, single-instance via `heartbeat.pid`.
- Ticks every 240 s with a read-only `chat-list` (never `chat-read` — that would
  consume the cursor).
- Exits when no session marker is left on this host, when a newer daemon owns the
  pidfile, after two consecutive failed ticks, or after 12 h.
- It never deletes a pidfile it does not own.

## 8. Membership

- The identity outlives a BCT restart, but BCT's memory of it does not — so a
  dead-but-present `identity.json` is expected, and something on the wire (a
  `NOT_INVITED` reply) is what has to notice it, never the file alone.
- `ensure_membership()` is the **single** automatic-join entry point. Every
  automatic caller (the SessionStart hook, the presence daemon, `authed()`'s
  reactive re-join) goes through it, so a second `chat-join` can never fire
  while one is already outstanding: an outstanding `pending-join.json` is
  always polled (`chat-join-poll`), never re-requested.
- `pending-join.json` retires on its own once older than `PENDING_TTL` (10
  min) — not only on an `approved`/`denied`/`expired` reply. A reply the
  client doesn't recognize (e.g. BCT restarted and forgot the request id) must
  not wedge the file forever and make the rejoin branch unreachable.
- The **join budget** (`join-state.json`) replaces the old flat 30-minute
  cooldown. A `denied`/`expired` outcome backs off `60s → 300s → 1800s`
  (`JOIN_BACKOFF`); after `JOIN_MAX_ATTEMPTS` (3) such outcomes the budget is
  **suspended for good** — no automatic path asks again. Only a human running
  `bct-chat.py join` at the remote's shell (`clear_join_state()`) resumes it;
  an approval also clears the budget, since being seated makes the count
  moot.
- `leave` (`do_leave()`) drops the identity and the pending request, then
  suspends the budget itself — this is what keeps a `leave` (or a kick) from
  having the daemon re-request membership in the room the user just left. The
  session markers (§2) are untouched: they track live claude sessions on this
  host, which `leave` doesn't change.
- A `NOT_INVITED` reply to a user verb (`authed()`) triggers one blocking
  re-join through `ensure_membership(wait_approval=True)`, then the verb is
  retried — unless the budget is suspended or backing off, in which case
  `NOT_INVITED` is surfaced as-is. Success is decided by an identity now
  existing, never by `pending-join.json` merely vanishing (the daemon polls
  the same request and may legitimately claim the approval first).

## 9. Delivery format

The digest mirrors BCT's local chat injection: an identity line
(`[bct-chat] 단체 채팅방 — 당신은 @<name> 입니다. 새 메시지:`), the room lines exactly
as BCT returned them, then `REPLY_HINT` — the instruction telling claude to answer
with `python3 ~/.bct-chat/bct-chat.py send "<답변>"`.

## 10. Platforms

macOS, Linux (AF_UNIX) and Windows (`tcp:`). Process liveness is always probed via
`proc_alive()`, never `os.kill(pid, 0)` directly on Windows: CPython maps `os.kill`
to `TerminateProcess` there for any signal, so probing a process would kill it.
`proc_alive()` uses `OpenProcess`/`CloseHandle` via `ctypes` on Windows instead, and
treats a NULL handle with `ERROR_ACCESS_DENIED` as alive (owned by another session
or user) rather than dead — only a genuine "no such process" error reads as dead.
