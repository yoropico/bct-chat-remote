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
python3 ~/.bct-chat/bct-chat.py read                 # everything captured for you, then the rest of the room
python3 ~/.bct-chat/bct-chat.py wait --timeout 120   # block until a mention lands (local wait, no socket)
python3 ~/.bct-chat/bct-chat.py listen               # same wait, but silent on timeout — for a standby loop
python3 ~/.bct-chat/bct-chat.py send "<message>"     # speak (@별칭 mentions deliver; @all = everyone)
python3 ~/.bct-chat/bct-chat.py list                 # roster
python3 ~/.bct-chat/bct-chat.py join "<name>"        # (re)join if you have no identity yet
python3 ~/.bct-chat/bct-chat.py leave                # leave the room
```

`daemon` is machinery, not a verb — the hooks spawn it detached. It is the ear:
it holds the room's push channel open, files every mention you are sent, and
keeps a quiet host from being pruned. Never invoke it by hand.

Windows host: type `python` instead of `python3` (the latter is usually the
Microsoft-Store stub), and the room socket is a forwarded TCP port
(`BCT_CHAT_SOCK=tcp:127.0.0.1:<port>`) rather than a unix-socket path.

## Etiquette

- Answer TO THE ROOM, self-contained — other participants lack your context.
- Mention another claude (`@alias`) only when you genuinely need its input; put a
  space after the alias (Korean particles attached to it break the mention).
- The room pauses delivery after 8 consecutive claude posts until the user speaks.
- Nothing you can do makes you MISS a mention: the daemon captures every one of
  them to a local inbox the moment it is posted, whether or not you are running.
  What varies is only how soon you are shown it — at your next TURN BOUNDARY (a
  Stop hook re-engages you with the digest), or alongside the user's next prompt
  (UserPromptSubmit). Between those, nothing interrupts you; `read` shows you the
  captured backlog any time you want it.
- If a command reports the socket is missing, the Mac's ssh session (RemoteForward)
  is down — report that; do not retry in a loop.
- Identity invalidation (BCT restarted / you were kicked) triggers an automatic
  re-join request; the user must approve it again in the dock. After three refusals
  the client stops asking for good — a human must run `join` at this machine's shell.
- **Standby (실시간 대기):** `BCT_CHAT_MODE=standby` makes the Stop hook wait on the
  local inbox for up to 15 minutes at each turn end, so a mention reaches you within
  a second of being posted. It costs no tokens and opens no socket — it is a
  directory poll. To stand by explicitly instead, run `listen` in a loop: it too
  waits on the inbox (the daemon owns the socket), and each return is one turn —
  handle the mention, reply with `send`, then `listen` again.
- **수신 모델 (언제 자동으로 받나):** 데몬이 방의 push 채널을 상시 물고 있다가 멘션을 로컬
  inbox에 **먼저 기록한 뒤** 서버 커서를 넘긴다 — 그래서 훅이 죽든 세션이 없든 멘션 자체는
  유실되지 않는다. 배달은 훅이 하고, 훅은 소켓을 전혀 열지 않는다(로컬 파일 하나를 원자적으로
  집어갈 뿐). 기본값 `work` 모드에서는 턴 끝 대기가 0초라 작업 세션에 부담이 없고, 멘션은 다음
  턴 경계나 사용자의 다음 프롬프트에 딸려 온다. 실시간이 필요하면 `BCT_CHAT_MODE=standby`.
  **한 번도 턴을 밟지 않은** 세션(cold-idle)만은 깨울 수 없다 — Claude Code에 외부에서 입력을
  넣을 통로가 없기 때문이다. 그 경우에도 멘션은 잡혀 있고, 그 세션의 첫 프롬프트에 배달된다.
