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
| `sessions/<session-id>` | `{"pid", "startedAt"}` — one marker per live claude session on this host (the daemon's refcount) |
| `heartbeat.pid` | the presence daemon's pidfile; it holds the daemon's pid and its mtime is the liveness signal |
| `bct-chat.py` | the stable copy |
| `inbox/<time_ns>-<pid>.json` | a captured mention, not yet claimed by a hook |
| `processing/<pid>-<time_ns>-<pid>.json` | a mention claimed by a hook, not yet acked |
| `dropped.json` | `{"n"}` — mentions the inbox cap has thrown away, not yet reported |
| `chain.json` | `{"n"}` — consecutive automatic re-engagements of the Stop hook (§5's chain cap) |

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
| `daemon` | the presence daemon (§7) — spawned automatically by the hooks; not for humans |
| `session-start`, `session-end`, `stop-hook`, `prompt-submit` | the Claude Code hook verbs |

## 5. Hook behaviour

The four hook verbs are wired in `hooks/hooks.json`.

- **Invariant: no hook verb opens a socket.** `session-start`, `session-end`,
  `stop-hook` and `prompt-submit` perform zero RPC. Delivery is a local inbox read
  (§6); capture is the daemon's job alone (§7). This is what makes "hook timeout <
  RPC budget" *unrepresentable* rather than merely widened: the old `stop-hook` ran
  `chat-peek` → `chat-read`, and `chat-read` advances BCT's server-side cursor with
  no ack verb to replay it — so a hook killed after the cursor moved lost the
  message outright. A hook that touches no socket can lose nothing, because the
  cursor moved long before, in the daemon, and only after the message was already on
  local disk.
- **Invariant: a hook verb always exits 0.** `hooks.json` falls back from
  `python3` to `python` on ANY nonzero exit, and that re-run would see stdin
  already drained — no session id, no marker, no daemon. Every hook swallows
  `Exception` and `SystemExit` alike.
- **Invariant: a hook is silent unless it has something to deliver.**
- **Invariant: no hook ever *increments* `dropped.json`.** Hooks DO touch the file —
  `take_dropped()` reads and clears it for the next digest, which is its designed
  consumer — but the counter itself has exactly one writer, the daemon's cap eviction
  (§6); a hook calling `inbox_put()` or `_bump_dropped()` would reintroduce a
  lost-update race.
- Inside a BCT pane (`$BCT_PANE_ID` set) every hook verb is a complete no-op — it
  claims nothing, spawns nothing. BCT's native chat injection owns delivery there.
- Every hook is a **daemon spawn point** (`ensure_daemon()`), and every hook that
  knows its session id **re-marks** it (`sessions/<id>`) before spawning. Re-marking
  makes the marker self-healing: `claude_pid()` is a best-effort ancestor walk, and
  if it ever resolved wrong, marker GC would collect a LIVE session's marker, the
  daemon would exit, and that session would be deaf for good with nothing left to
  repair it. It also refreshes the mtime that the pid-0 (Windows) `MARKER_TTL`
  fallback rides on. Marker before spawn, always — a daemon that starts ahead of its
  own marker sees an empty session set and exits at once.

| Verb | Behaviour |
|---|---|
| `session-start` | keep the stable copy current, mark the session, `ensure_daemon()`. An interactive run (no session id on stdin) leaves no marker and spawns nothing. |
| `session-end` | drop this session's marker. The daemon is not killed — another session on this host may still be in the room; it exits on its own once the marker set empties. |
| `stop-hook` | claim one mention from the inbox and **block** the finishing turn with the digest, so claude answers the room in place. |
| `prompt-submit` | claim one mention and print the digest as **context** alongside the user's prompt — this is what reaches a session that was never woken. Never holds. |

**Modes** — `$BCT_CHAT_MODE`, read by `stop-hook` only:

| Mode | Empty inbox at turn end | Cost |
|---|---|---|
| `work` (default) | return immediately | ~0 s and 0 tokens per turn |
| `standby` | wait locally on the inbox for up to `STANDBY_HOLD` (900 s) | 0 tokens, 0 RPC — a directory poll, not a socket |

A working session pays nothing per turn; only an idle session that opted into
`standby` waits, and it waits on the local filesystem. `BCT_CHAT_STANDBY` survives as
a legacy variable, and it means what it says: a truthy value (anything other than the
disable spellings `0`/`off`/`false`/`no`, or unset) turns standby on; `BCT_CHAT_MODE`
wins whenever it holds a recognized value. `hooks.json` gives `Stop` a 960 s timeout to
cover the 900 s hold with slack; the other three hooks do no RPC and never hold, so
10 s is generous. Claude Code's hook config schema imposes no maximum on `timeout`, and
the command spawn passes the value straight through as the process deadline — but
whether a *running* Claude Code actually honours a 960 s `Stop` timeout has **not been
confirmed against the real harness**; this is inferred from the schema and spawn code,
not observed. If Claude Code clamps it, standby simply ends early — no mention is lost
either way, because `inbox_wait()` claims and returns in the same breath and
`recover_orphans()` reclaims anything caught in that window. (Claude Code does clamp
its *`SessionEnd` shutdown wait* to 60 s, so `SessionEnd` must stay well under that.)

**Chain cap.** `stop_hook_active` in the Stop payload means the turn is only
continuing because *we* blocked it. `chain.json` counts those automatic
re-engagements; at `CHAIN_CAP` (3) the hook stops delivering **and** stops holding,
and the mention **stays in the inbox** for the user's next prompt. Two standby
remotes mentioning each other must not bill turns forever with no human in the loop.
Any user-driven turn resets the chain: `prompt-submit` clears the counter, and a Stop
with `stop_hook_active` false reads the count as 0 regardless of the file.

Delivery is **at-least-once** (§6): a hook that dies between claim and print leaves
its item in `processing/`, and the orphan sweep — run by every hook and by the daemon
— returns it to the inbox. A mention is never dropped without being delivered.

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

## 7. Presence — the daemon is the ear

One daemon per host, detached, single-instance via `heartbeat.pid`. It is the only
thing in the system that talks to the room on its own initiative, and the **only**
writer of the inbox (§6).

- **It holds `chat-listen` continuously** (`LISTEN_TIMEOUT` 40 s, covering BCT's
  ~30 s server-side hold) and lands each heard mention in the inbox **before** it
  issues the next listen. That ordering is the whole durability guarantee: BCT's
  cursor advances when `chat-listen` returns, and there is no ack verb to replay
  a message with — so the message must be on local disk before anything can move
  the cursor again. The silence sentinel (`NO_MENTION`/`NO_NEW`) is never an inbox
  item. A bridge that answers the window instantly instead of holding it is not
  busy-waited: the re-arm is floored at a tenth of the requested window (≤1 s).
  For the same reason an `inbox_put()` that **raises** (ENOSPC, EINTR) does not
  lose the mention: the daemon holds the text in memory and retries the put at the
  top of the next pass, before it can issue another listen. A transient disk
  failure costs a delay, not a message; a persistent one falls into the same
  backoff as any other failed tick, so it never spins hot.
- **Presence tick**: interleaved with the listen, a read-only `chat-list` every
  `PRESENCE_INTERVAL` (240 s) — never `chat-read`, which would consume the cursor.
  It exists because BCT prunes an external participant after 10 minutes of
  silence, so a live-but-quiet host must keep proving it is there.
- **Exit conditions are exactly three**: no live session marker is left on this
  host, a newer daemon owns the pidfile, or the user has left the room (the join
  budget is suspended *and* we hold no identity). Nothing else gets it out: a dead
  tunnel, a failed tick, a `die()` raised out of a chained join, a full disk — all
  are waited out with exponential backoff (`BACKOFF_MIN` 60 s → `BACKOFF_MAX`
  300 s), never died of. The daemon now holds the capture layer, so **daemon
  reliability is receive reliability**: the old two-strike suicide and 12-hour
  max-uptime backstop are gone, because either one silently un-ears a live
  session, and no hook is listening to notice.
- **The backoff wait is chunked** (`PIDFILE_STALE / 2` per chunk), refreshing the
  pidfile's mtime on every chunk. The mtime is the only evidence a daemon is alive
  and `PIDFILE_STALE` (90 s) is far shorter than `BACKOFF_MAX` (300 s), so a single
  long sleep would make a perfectly healthy daemon — one waiting out a dead tunnel,
  the very case this backoff exists for — read as **dead** to `heartbeat_alive()`.
  Every hook's `ensure_daemon()` would then spawn a rival (every hook is a spawn
  point), and the incumbent would only stand down minutes later when it woke. The
  chunks also re-check `live_sessions()`, so a daemon whose last session ended
  mid-backoff gets out of the way promptly instead of lingering for up to 5 minutes.
- It never deletes a pidfile it does not own (`pidfile_owner() == os.getpid()` gates
  every release). On the no-live-session exit it releases the pidfile **before**
  re-checking the markers, and hands the room back to a fresh `ensure_daemon()` if
  one reappeared: a session that marks itself during that shutdown sees
  `heartbeat_alive()` still true, declines to spawn, and would otherwise be left
  holding a marker with no ear at all.
- **Seat**: it trusts `identity.json` until the *wire* disagrees. A `NOT_INVITED`
  reply to its listen or its tick is that disagreement, and only then does it
  re-join — through `ensure_membership(force=True)` (§8), never a bare
  `do_join()`, and with `force` scoped to the exact identity the wire disproved.
  While unseated it polls at `JOIN_POLL` (15 s) and re-probes the seat with one
  `chat-list`; a spent budget means it asks for nothing at all.
- **Only the wire seats it.** An identity is a reason to probe, never proof of a
  seat, and the *absence* of one is the only local fact that means "no seat yet".
  In particular the daemon must **not** read "the id BCT gave me back is the id it
  just disproved" as still-unseated: BCT's retire/reseat deliberately hands a
  re-approved external participant back the **same** `participantID`, preserving
  its identity *and* its unread cursor across a prune (that is the whole point of
  it — a live-but-quiet remote must not lose its backlog). Gating on that livelocks:
  the approval writes the same id straight back, the gate stays true forever,
  `ensure_membership()` short-circuits on its identity fast path, and the daemon
  spins every `JOIN_POLL` — alive, seated on the server, and permanently deaf. The
  `chat-list` probe is the seat detector; if the wire answers `NOT_INVITED` again,
  the dead identity is simply re-armed for the next `force`. Cost: one extra
  `chat-list` per `JOIN_POLL` while an approval is pending.

### Liveness and markers

- `heartbeat_alive()` is `heartbeat.pid`'s mtime younger than `PIDFILE_STALE`
  (90 s) **and** `proc_alive(pidfile_owner())`. Mtime alone called a signal-killed
  daemon alive for minutes — an ssh-logout `SIGTERM` leaves the pidfile with a
  fresh mtime — and nothing respawns a corpse that still looks warm.
- `ensure_daemon()` spawns unless a daemon is already alive, or we are suspended
  *and* unseated (never resurrect a daemon into a room the user has left). Every
  hook is a spawn point, not just `session-start`: the hooks do no RPC any more,
  so they can afford the check on every turn.
- A session marker holds `{"pid", "startedAt"}`. `claude_pid()` resolves the pid
  best-effort from the *grandparent* of the hook process (`hooks.json`'s `||`
  forces an `sh -c` wrapper whose own pid dies with the hook, so `os.getppid()` is
  useless). A wrong answer is not symmetric — a pid that is not this session's
  claude makes `gc_markers()` collect a **live** session's marker the moment that
  stranger exits, the daemon then exits on an empty marker set, and nothing
  re-creates the marker, so that session is deaf for the rest of its life — while a
  0 only costs a marker that ages out on `MARKER_TTL`. So it errs toward 0: the
  ancestor is trusted only when `ps` says it is both alive **and** plausibly claude
  (`comm` matching claude/node — "still exists at the moment we ask" was never a
  safety net, since a short-lived wrapper is alive at that moment too). It is 0 on
  Windows (no cheap ancestor walk), 0 on a host with no `ps`, and 0 for any
  ancestor that fails either test.
- `gc_markers()` runs at the top of every daemon pass and collects a marker whose
  pid is dead, or — where no pid could be resolved — whose mtime is older than
  `MARKER_TTL` (7 days). It errs long on purpose: a leaked marker only costs a
  phantom seat, while GC'ing a **live** session's marker costs that session its
  ear. A marker with an unreadable pid reads as pid 0 (TTL), never as dead.

## 8. Membership

- The identity outlives a BCT restart, but BCT's memory of it does not — so a
  dead-but-present `identity.json` is expected, and something on the wire (a
  `NOT_INVITED` reply) is what has to notice it, never the file alone.
- `ensure_membership()` is the **single** automatic-join entry point. Both
  automatic callers — the presence daemon (§7) and `authed()`'s reactive re-join —
  go through it (the hooks make no automatic join at all any more), so a second
  `chat-join` can never fire while one is already outstanding: an outstanding
  `pending-join.json` is always polled (`chat-join-poll`), never re-requested.
  `ensure_membership()` normally trusts a truthy `identity()` and returns early
  without touching the wire; `force=True` skips that fast path for a caller that
  already has fresh wire evidence the stored identity is dead, not merely absent —
  the daemon's `NOT_INVITED` reply is that evidence, and it is the only such
  caller. It forces only while `identity()` still holds the exact id the wire
  disproved: forcing past the fast path with a live seat would request a second
  one.
- `pending-join.json` retires on its own once older than `PENDING_TTL` (10
  min) — not only on an `approved`/`denied`/`expired` reply. A reply the
  client doesn't recognize (e.g. BCT restarted and forgot the request id) must
  not wedge the file forever and make the rejoin branch unreachable. A
  `pending-join.json` with no `requestedAt` (written before this field
  existed) is backfilled as just-requested rather than read as ~55 years old
  and discarded — which would fire a fresh `chat-join` and orphan an approval
  already in flight.
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

It is capped: `DIGEST_MAX_LINES` (200) keeps only the most recent lines, and
`DIGEST_MAX_BYTES` (16 KB) bounds the identity line and room lines — a backlog that
rotted for hours must not dump 4000 lines into a turn. `REPLY_HINT` is appended
*after* that byte cap is applied, never before it, so it always survives: a single
mention line longer than `DIGEST_MAX_BYTES` truncates the room text, never the
instruction for how to answer it. Anything the inbox cap threw away since the last
digest is announced first, as `(오래된 멘션 N건 생략)`. `stop-hook` emits the digest as
`{"decision": "block", "reason": …}`; `prompt-submit` prints it plain.

## 10. Platforms

macOS, Linux (AF_UNIX) and Windows (`tcp:`). Process liveness is always probed via
`proc_alive()`, never `os.kill(pid, 0)` directly on Windows: CPython maps `os.kill`
to `TerminateProcess` there for any signal, so probing a process would kill it.
`proc_alive()` uses `OpenProcess`/`CloseHandle` via `ctypes` on Windows instead, and
treats a NULL handle with `ERROR_ACCESS_DENIED` as alive (owned by another session
or user) rather than dead — only a genuine "no such process" error reads as dead.
