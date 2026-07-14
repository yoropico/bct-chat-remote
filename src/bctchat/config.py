"""bct-chat.py — external participant client for BCT's group chat.

Speaks the line-JSON wire ({"paneID","cmd","args"} -> {"ok","text","error"})
over a unix socket (default ~/.bct-chat.sock — the ssh-RemoteForward'ed BCT
control socket; override with $BCT_CHAT_SOCK). On hosts without AF_UNIX
(Windows CPython), forward a TCP port instead and set
$BCT_CHAT_SOCK=tcp:<host>:<port>. Pure stdlib.
Spec: docs/superpowers/specs/2026-07-12-chat-external-participants-design.md
"""
import json, os, re, socket, subprocess, sys, time

ARTIFACT = os.path.abspath(__file__)   # the concatenated single file; what ensure_daemon re-execs

if hasattr(sys.stdout, "reconfigure"):
    # Wire and room text are UTF-8; never trust the locale (Korean Windows = cp949).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# BCT_CHAT_HOME overrides the state dir. This is what makes the test suite safe on
# Windows: ntpath.expanduser() ignores HOME and reads USERPROFILE, so an HOME-isolated
# test would run against the developer's REAL ~/.bct-chat and SIGKILL their live daemon.
STATE_DIR = os.environ.get("BCT_CHAT_HOME") or os.path.expanduser("~/.bct-chat")
IDENTITY = os.path.join(STATE_DIR, "identity.json")
PENDING = os.path.join(STATE_DIR, "pending-join.json")
JOIN_STATE = os.path.join(STATE_DIR, "join-state.json")
JOIN_BACKOFF = (60, 300, 1800)  # seconds: 1 min, 5 min, 30 min — then suspended for good
JOIN_MAX_ATTEMPTS = 3           # denied/expired outcomes before the budget suspends itself
PENDING_TTL = 600               # 10 min — an unrecognized poll reply (BCT forgot the request
                                 # id) must still retire pending-join.json, not wedge it forever
SOCK = os.environ.get("BCT_CHAT_SOCK", os.path.expanduser("~/.bct-chat.sock"))
NO_NEW = "(새 메시지 없음)"
NO_MENTION = "(새 멘션 없음)"          # chat-listen timeout sentinel (server push)
NOT_INVITED = "이 패널은 대화방에 초대되지 않았습니다"
SESSIONS_DIR = os.path.join(STATE_DIR, "sessions")
MARKER_TTL = 7 * 86400          # a marker with no pid to probe (Windows) ages out this slowly:
                                 # GC'ing a LIVE session's marker costs it its ear, while a
                                 # leaked one only costs a phantom seat — so err long
PIDFILE = os.path.join(STATE_DIR, "heartbeat.pid")
PIDFILE_STALE = 90              # pidfile mtime older than this = no daemon (with proc_alive)
PRESENCE_INTERVAL = 240         # 4 min — comfortably inside BCT's 10-min prune window
LISTEN_TIMEOUT = 40             # BCT holds chat-listen ~30s; 40 covers the hold plus slack
BACKOFF_MIN = 60                # a dead tunnel is waited out, never died of
BACKOFF_MAX = 300
JOIN_POLL = 15                  # while unseated: how long between join/poll attempts

STABLE = os.path.join(STATE_DIR, "bct-chat.py")

INBOX_DIR = os.path.join(STATE_DIR, "inbox")
PROCESSING_DIR = os.path.join(STATE_DIR, "processing")
DROPPED = os.path.join(STATE_DIR, "dropped.json")
INBOX_CAP = 50              # a deeper queue means nobody has been home for a long time
ORPHAN_AGE = 120            # a processing/ item older than this belonged to a dead hook

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CLAUDE_COMM_RE = re.compile(r"claude|node", re.I)   # a hook ancestor plausible enough to
                                                     # trust as this session's claude — the
                                                     # CLI is a node program, and a marker
                                                     # GC'd on a wrong pid costs an ear

REPLY_HINT = ('당신이 멘션되었습니다 — `python3 ~/.bct-chat/bct-chat.py send "<답변>"` 으로 답하세요. '
              '(명단: `python3 ~/.bct-chat/bct-chat.py list`, 새 메시지 확인: '
              '`python3 ~/.bct-chat/bct-chat.py read`)')
