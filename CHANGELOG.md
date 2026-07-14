# Changelog

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
