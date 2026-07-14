2026-07-14 | design: split CAPTURE from WAKE in the remote-chat receive model.
  Why: 3 parallel audits found ~18 defects, and most shared one root cause — the hooks
  ARE the listener. So: no hook run = deaf; hook killed after chat-read advanced BCT's
  cursor = message gone (no ack verb exists). Fix is architectural, not a patch pile:
  the always-running heartbeat daemon (today a deaf chat-list ticker) becomes the ear and
  writes each mention to a durable local inbox BEFORE issuing the next listen; the hooks
  go local-only (atomic os.rename claim, zero RPC). That deletes four defect classes at
  once — hook-timeout-vs-RPC-budget loss, the 30-40s per-turn standby tax, multi-session
  delivery races, and the double listener. Accepted cost: the daemon now holds state, so
  daemon resilience IS receive resilience — hence killing the fails>=2 suicide, the 12h
  max-uptime (marker GC supersedes it; keeping it would kill a LIVE session's ear), and
  making every hook a respawn point. Deliberately NOT solved: cold-idle wake — Claude Code
  has no channel to inject input into an idle session, so we guarantee capture and
  degrade the wake instead of pretending. Spec: docs/superpowers/specs/2026-07-14-chat-remote-inbox-receive-design.md
2026-07-14 14:51 | [session end] reason=other
2026-07-14 14:52 | [session end] reason=other
2026-07-14 | plan: 10-task TDD plan for the receive rework, with two deliberate deviations from the spec.
  (1) session-start goes LOCAL-ONLY (spec §4.4 still had it issuing joins): it now only writes the
  marker and spawns the daemon, and the daemon's first tick does the join. That removes the LAST
  socket call from any hook, so "hook timeout < RPC budget" (D7) becomes unrepresentable rather than
  merely widened — and D3 (no socket at SessionStart) disappears instead of being patched.
  (2) Marker GC (D12) needs a *claude* pid, which a hook cannot read: hooks.json's `||` forces an
  `sh -c` wrapper, so getppid() is a shell that exits immediately. mark_session() resolves the
  grandparent via one `ps` call on POSIX and stores 0 on Windows; GC is by pid where we have one and
  by a 7-day mtime TTL where we do not. Erring long is deliberate — a lingering marker costs a
  phantom seat, while GC'ing a LIVE session's marker would cost that session its ear.
  Plan: docs/superpowers/plans/2026-07-14-chat-remote-inbox-receive.md
2026-07-14 | build: tasks 1-5 of the receive rework landed (subagent-driven, review after every task).
  The reviews earned their keep — four defects that unit-green code was hiding: rpc()'s "overall
  deadline" only covered recv, so a 2s request really took 3.9s; the inbox cap counted a message a hook
  had ALREADY claimed as "dropped"; os.rename PRESERVES mtime, so a stolen sidecar was sweep-eligible
  the instant it was born (a live hook's file could be deleted mid-operation) — closed by ownership
  (proc_alive on the pid in the sidecar's name), not by a timing threshold; and the daemon hand-copied
  the join guard sequence, which is precisely how the duplicate-chat-join defect this rework exists to
  kill would have crept back in. Added docs/SPEC.md: the repo had none, so the pre-commit spec-gate had
  nothing to check a behaviour change against, and every task would have had to lie with a
  "Spec-Impact: none" trailer. Paused after task 5 (96 tests green, tree clean, HEAD 26bd025).
