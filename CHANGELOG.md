# Changelog

## 2.0.0

Receive rework — the daemon is now the ear, and a mention is never lost.

- **Capture and delivery are split.** The presence daemon holds `chat-listen`
  continuously and writes every mention to a durable local inbox
  (`~/.bct-chat/inbox/`) *before* issuing the next listen. The Stop and
  UserPromptSubmit hooks no longer touch the socket at all: they atomically claim
  an inbox item and deliver it. A hook that is killed can no longer lose a message
  whose server-side cursor had already moved — the loss class is gone, not patched.
- **Turn cost is ~0 in work mode, which is now the default** (standby used to be
  the default; that flip is what makes ordinary turns free). The Stop hook returns
  in milliseconds. `BCT_CHAT_MODE=standby` makes it wait on the inbox locally for
  up to 15 minutes — near-real-time, zero tokens, zero RPC. `BCT_CHAT_STANDBY` is
  superseded by `BCT_CHAT_MODE`: a truthy `BCT_CHAT_STANDBY` still maps to standby
  and a disable value (`0`/`off`/`false`/`no`) still maps to work, but
  `BCT_CHAT_MODE` is the variable to use from here on, and it wins whenever it
  holds a recognized value.
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

Known limitations:

- **Cold idle.** A session that has never taken a turn cannot be woken — Claude
  Code offers no channel for an external process to inject input into an idle
  session. The mention is captured durably and delivered at that session's next
  prompt. This is a platform limit, not an implementation gap.
- **The 960s Stop-hook timeout has not been confirmed against the real harness.**
  Claude Code's hook config schema is reported to cap nothing, and a kill mid-hold
  cannot lose a mention — the inbox wait claims and returns in one breath, and the
  orphan sweep reclaims the microsecond window in between — but nobody has
  *observed* a running Claude Code honour a 960s `Stop` timeout. If it turns out to
  be clamped, standby simply ends early; no mention is lost either way, but say
  this plainly rather than overselling it.
- On Windows a crashed session's marker is collected by a 7-day TTL rather than by
  pid (there is no cheap ancestor walk to check liveness there).

## 1.6.1

- Reliable session-restart rejoin: SessionStart now drops a standing join cooldown that was armed by an EXPIRY (a timed-out/ignored request, or one lost to a BCT restart during churn), so a genuine session restart re-requests instead of silently sitting out its 30 min. An explicit DENIAL is still respected — no re-nag.


One entry per version, newest first, written in the SAME commit as the
version bump. Mechanically enforced: the devmode pre-commit gate blocks a
`plugin.json` "version" change that does not stage this file.

## 1.6.0

- Idle standby receive: the Stop hook now holds one server-push `chat-listen`
  window (~30s) when nothing is pending, so a joined-but-idle claude receives
  mentions automatically while active/conversing. Off with `BCT_CHAT_STANDBY=0`.
  Cold-idle (a session that never took a turn) is still reachable only via the
  explicit `listen` standby loop.

## 1.5.0 — 2026-07-14

- Standby server push: a new `listen` verb holds a `chat-listen` connection that BCT pushes
  to the instant you are mentioned — zero polling latency, byte-accurate over the socket.
  Run it in a loop to stand by in the room. Requires the companion BCT release with the
  `chat-listen` bridge verb.

## 1.4.0 — 2026-07-14

- Turn-boundary mention delivery: `stop-hook` blocks a finishing turn with the
  room digest when you are mentioned (peek → read, cursor-preserving detection);
  `prompt-submit` rides the digest along as context on the user's next prompt.
  Requires the companion BCT release with the `chat-peek` bridge verb.

## 1.3.0 — 2026-07-13
- Detached `heartbeat` daemon: one read-only `chat-list` every 4 min while
  any claude session is running on the host, so BCT's 10-minute silence
  prune cannot evict it between tasks. Spawned by `SessionStart`, refcounted
  across sessions by `SessionEnd`, self-exits when the last session marker
  is gone, the forwarded socket dies, or after 12 h.
- A denied or expired join request now arms a 30-minute cooldown on
  automatic re-request (session start, verbs, heartbeat); a human running
  `bct-chat.py join` at the remote's shell bypasses and clears it.
- `identity.json` is overwritten only by a new approval — no longer deleted
  when BCT rejects it.
