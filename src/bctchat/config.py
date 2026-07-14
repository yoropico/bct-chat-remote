"""bct-chat.py — external participant client for BCT's group chat.

Speaks the line-JSON wire ({"paneID","cmd","args"} -> {"ok","text","error"})
over a unix socket (default ~/.bct-chat.sock — the ssh-RemoteForward'ed BCT
control socket; override with $BCT_CHAT_SOCK). On hosts without AF_UNIX
(Windows CPython), forward a TCP port instead and set
$BCT_CHAT_SOCK=tcp:<host>:<port>. Pure stdlib.
Spec: docs/superpowers/specs/2026-07-12-chat-external-participants-design.md
"""
import json, os, re, socket, subprocess, sys, time

ARTIFACT = os.path.abspath(__file__)   # the concatenated single file; what spawn_heartbeat re-execs

if hasattr(sys.stdout, "reconfigure"):
    # Wire and room text are UTF-8; never trust the locale (Korean Windows = cp949).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STATE_DIR = os.path.expanduser("~/.bct-chat")
IDENTITY = os.path.join(STATE_DIR, "identity.json")
PENDING = os.path.join(STATE_DIR, "pending-join.json")
COOLDOWN = os.path.join(STATE_DIR, "join-cooldown.json")
JOIN_COOLDOWN = 1800            # 30 min — a request the user denied or ignored must not nag
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NO_MENTION = "(새 멘션 없음)"          # chat-listen timeout sentinel (server push)
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
HEARTBEAT_INTERVAL = 240        # 4 min — comfortably inside BCT's 10-min prune window
HEARTBEAT_MAX_UPTIME = 43200    # 12 h — backstop for a marker leaked by a crashed session

STABLE = os.path.join(STATE_DIR, "bct-chat.py")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

REPLY_HINT = ('당신이 멘션되었습니다 — `python3 ~/.bct-chat/bct-chat.py send "<답변>"` 으로 답하세요. '
              '(명단: `python3 ~/.bct-chat/bct-chat.py list`, 새 메시지 확인: '
              '`python3 ~/.bct-chat/bct-chat.py read`)')
