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
2026-07-15 | build: receive rework complete (tasks 6-10 + final whole-branch review). 146 tests green.
  The daemon is now the ear (holds chat-listen, lands each mention in the inbox BEFORE the next listen);
  the hooks open no socket at all — a static AST reachability test proves the wire is unreachable from
  every hook entry point on EVERY path, exercised or not. Reviews caught, in order of how badly each
  would have hurt: (1) `identity() == dead_id` LIVELOCKED against BCT's real reseat, which returns the
  SAME participantID by design — the daemon would sit alive, seated, and permanently deaf; (2) backoff
  slept 300s in one call while PIDFILE_STALE is 90s, so a HEALTHY daemon waiting out a dead tunnel read
  as a corpse and every hook spawned a duplicate; (3) `read` acked each item BEFORE printing any of them,
  so a crash mid-drain lost messages that were never shown and could not be recovered; (4) os.kill()
  raises OverflowError past pid_t — NOT an OSError — and gc_markers() runs outside the daemon's per-tick
  guard, so one tampered marker was a fourth exit condition: daemon dies, hook respawns it, it re-reads
  the same marker, dies again, host permanently deaf. Guard moved into proc_alive(), the one chokepoint
  to os.kill/ctypes. Deliberately NOT solved: cold-idle wake (no channel exists to inject input into an
  idle Claude Code session) — we capture durably and deliver at that session's first prompt.
  Unverified: hooks.json's 960s Stop timeout has never been OBSERVED being honoured; a clamp only ends
  standby early and cannot lose a mention. On the live-verification list.
2026-07-15 | LIVE-VERIFIED the receive rework on rsbglee (Windows remote) vs BCT 0.6.9. Deployed the
  2.0.0 stable copy; drove the full path against the real bridge. Confirmed live:
  - connect-based sock_available() correctly reports a DEAD bridge (BCT down, ssh -R tunnel still up =
    a zombie socket) as unavailable — the exact case os.path.exists() used to call healthy.
  - the daemon captured a REAL BCT mention into ~/.bct-chat/inbox/ before its next listen; the inbox
    filename carried the Windows sequence tie-breaker (...-<pid>-000000000000.json) the CI fix added.
  - stop-hook delivered that captured mention as correct block-JSON (Korean intact) with BCT_CHAT_SOCK
    pointed at a DEAD port — proving ZERO RPC in the hook live, not just by the AST test — exit 0, then
    drained the inbox (claim -> deliver -> ack).
  Found & fixed live (c02c6f4, 148 tests): do_join() decided success by "do we have an identity", so a
  remote whose identity had gone stale across a BCT restart announced 입장 승인됨 for a request the user
  had not looked at yet, then kept failing NOT_INVITED. Unit tests never saw it (they start from a clean
  state dir) and "did the identity change" would not catch it either — BCT's reseat returns the SAME
  participantID. Now decided by who claimed the approval.
  NOT verified live (test-harness limits, not product gaps): daemon survival across SIGKILL under a REAL
  persistent claude session — a daemon spawned by a hook running under ONE-SHOT ssh dies when the ssh
  job object tears down; a real remote claude is a persistent parent, so this is a harness artifact, but
  worth a check on an actual remote session. And whether Claude Code honours the 960s Stop timeout.
  DEPLOY STATE: rsbglee's stable copy is 2.0.0, but its PLUGIN CACHE is still 1.3.0 — the HOOKS on a
  real remote session run from the cache, not the stable copy, so real sessions stay on 1.3.0 until the
  plugin is updated on the remote (the stable-copy vs plugin-cache gotcha). Stable copy + manual verbs +
  the skill already use 2.0.0.
2026-07-15 | DEPLOYED 2.0.0 to rsbglee — both halves of the stable-copy/plugin-cache split.
  The remote installs bct-chat-remote from a DIRECTORY-source marketplace (C:\Users\bglee\.bct-chat\plugin),
  not GitHub — so the deploy is: (1) stable copy scp'd to ~/.bct-chat/bct-chat.py (used by manual verbs +
  the skill; this is what all the live tests ran against), and (2) the plugin-source directory replaced
  with a clean 2.0.0 tar (git archive HEAD -> tar -x on the remote, contents cleared first to drop the
  rework's deleted test files), then `claude plugin marketplace update bct-chat-remote` +
  `claude plugin update bct-chat-remote@bct-chat-remote` (the plain name 404s; the plugin@marketplace form
  is required). Cache now holds 2.0.0 (plugin.json 2.0.0, artifact 61796 bytes with inbox_put, stop-hook
  timeout 960) alongside 1.1.0..1.3.0. "Restart to apply" — the remote's NEXT claude session runs the 2.0.0
  hooks; until then a running session stays on 1.3.0. Also aligned marketplace.json metadata.version
  1.6.1->2.0.0 (PR#9) — Task 10 had bumped only plugin.json, and marketplace.json is outside the spec-gate.
  Fixes that landed after the main rework merge (all live/CI-found): stale-identity-as-approval (PR#8,
  found driving the live remote), Windows CI portability incl. the inbox filename-collision message-loss
  (in PR#7), and the same-pid thread-claim test that only modelled a degenerate case (PR#8 follow-up).
