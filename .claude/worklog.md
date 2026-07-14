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
