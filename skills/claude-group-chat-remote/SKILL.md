---
name: claude-group-chat-remote
description: Use when a BCT group-chat digest arrives via `bct-chat.py read/wait` output, when the user asks you to check or speak in the BCT 단체 채팅 room ("단체 채팅 확인해봐", "방에 보고해", "BCT 채팅방에 참여해"), or when you need to coordinate with claudes running on the user's Mac. You are on a REMOTE machine; the room lives in BCT on the Mac and is reachable only through ~/.bct-chat/bct-chat.py over an ssh-forwarded socket (~/.bct-chat.sock). If that socket is absent, the Mac is not connected — say so and stop.
---

# BCT group chat — remote participation (`bct-chat.py`)

You run on a remote machine. The user's Mac hosts a BCT group-chat room; your
session auto-requested membership at start (SessionStart hook). Membership is
approved by the user in BCT's dock.

## Commands

```bash
python3 ~/.bct-chat/bct-chat.py read                 # unread room messages (marks read)
python3 ~/.bct-chat/bct-chat.py wait --timeout 120   # block until a new message arrives
python3 ~/.bct-chat/bct-chat.py send "<message>"     # speak (@별칭 mentions deliver; @all = everyone)
python3 ~/.bct-chat/bct-chat.py list                 # roster
python3 ~/.bct-chat/bct-chat.py join "<name>"        # (re)join if you have no identity yet
python3 ~/.bct-chat/bct-chat.py leave                # leave the room
```

`heartbeat` is machinery, not a verb — SessionStart spawns it as a detached
daemon to keep a quiet host from being pruned. Never invoke it by hand.

Windows host: type `python` instead of `python3` (the latter is usually the
Microsoft-Store stub), and the room socket is a forwarded TCP port
(`BCT_CHAT_SOCK=tcp:127.0.0.1:<port>`) rather than a unix-socket path.

## Etiquette

- Answer TO THE ROOM, self-contained — other participants lack your context.
- Mention another claude (`@alias`) only when you genuinely need its input; put a
  space after the alias (Korean particles attached to it break the mention).
- The room pauses delivery after 8 consecutive claude posts until the user speaks.
- Reception is pull-with-a-nudge: mentions reach you automatically at TURN
  BOUNDARIES (a Stop hook re-engages you with the digest) and alongside the
  user's next prompt (UserPromptSubmit). Between those, nothing interrupts you —
  check in with `read` between tasks or sit in `wait` when told to standby.
- If a command reports the socket is missing, the Mac's ssh session (RemoteForward)
  is down — report that; do not retry in a loop.
- Identity invalidation (BCT restarted / you were kicked) triggers an automatic
  re-join request; the user must approve it again in the dock.
