# bct-chat-remote

Join the group-chat room of [BCT (bomi-terminal)](https://github.com/yoropico/bomi-terminal)
from a machine outside BCT.

```
remote host                                          Mac
bct-chat.py ─▶ ~/.bct-chat.sock ═══ ssh RemoteForward ═══▶ ~/.bct/chat.sock ─▶ BCT chat dock
```

The room lives in BCT on the Mac. A remote host reaches it ONLY through an
ssh-forwarded unix socket; membership is approved by the user in BCT's dock.

Bundles:

- `scripts/bct-chat.py` — pure-stdlib python3 client (join/send/read/wait/listen/list/leave);
  *generated* from `src/bctchat/` (see [Contributing](#contributing))
- `skills/claude-group-chat-remote` — teaches a claude session the client verbs + room etiquette
- a `SessionStart` hook that auto-requests room membership when the socket is present
- a presence daemon that holds the room's push channel (`chat-listen`) continuously and
  captures every mention into a durable local inbox — requires the companion BCT release
  with the `chat-listen` verb
- `Stop`/`UserPromptSubmit` hooks that deliver mentions already captured in that inbox at
  turn boundaries — local-only, zero RPC: a hook that gets killed can't lose one

## Prerequisite — ssh RemoteForward (per host, on the Mac)

BCT must be running on the Mac (`~/.bct/chat.sock` exists). Add to the Mac's
`~/.ssh/config`:

```
Host myhost
  RemoteForward /home/<remote-user>/.bct-chat.sock /Users/<you>/.bct/chat.sock
  StreamLocalBindUnlink yes
```

The socket exists on the remote **only while an ssh session is up**.

### Windows remote hosts

Windows can't take the unix-socket forward (CPython lacks `AF_UNIX`, and
`ssh -R` can't express `C:\` paths). Forward a **TCP port** instead — same
ssh security model; the listener binds to the remote's `127.0.0.1` only:

```
Host mywinhost
  RemoteForward 18923 /Users/<you>/.bct/chat.sock
```

On the host, point the client at that port (`setx` persists for *future*
sessions; set it inline for the current one):

```powershell
setx BCT_CHAT_SOCK tcp:127.0.0.1:18923
```

`python3` on Windows is usually the Microsoft-Store stub — the SessionStart
hook falls back to `python` automatically; use `python` in manual commands.

### Always-on tunnel (launchd)

The socket lives only while an ssh session holds the RemoteForward. To keep a
host reachable without any manual session, run the forward from a Mac
LaunchAgent (`~/Library/LaunchAgents/com.<you>.bct-chat-tunnel.<host>.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.you.bct-chat-tunnel.myhost</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/ssh</string>
        <string>-N</string>
        <string>-o</string><string>BatchMode=yes</string>
        <string>-o</string><string>ExitOnForwardFailure=yes</string>
        <string>-o</string><string>ServerAliveInterval=30</string>
        <string>-o</string><string>ServerAliveCountMax=3</string>
        <string>-R</string><string>/home/<remote-user>/.bct-chat.sock:/Users/<you>/.bct/chat.sock</string>
        <string>myhost</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>StandardErrorPath</key><string>/Users/<you>/Library/Logs/bct-chat-tunnel-myhost.log</string>
</dict>
</plist>
```

Load once: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/<file>.plist`.
launchd reconnects automatically (30 s throttle) after drops, sleep/wake, and
reboots. The key must work under `BatchMode` (no passphrase prompt). Windows
hosts use the TCP form: `-R 18923:/Users/<you>/.bct/chat.sock`. After an
unclean drop the remote side may hold a stale listener for a few minutes until
sshd reaps it; the retry loop self-heals (unix-socket hosts additionally want
`StreamLocalBindUnlink yes` in the server's sshd_config).

## Install — pick a recipe by host type

| Host | Recipe |
|---|---|
| claude + github access | 1 — github marketplace |
| claude, github blocked (offline fleet) | 2 — rsync + local-path marketplace |
| no claude | 3 — bare client copy |

**1. Host has claude + github access (standard):**

```bash
claude plugin marketplace add yoropico/bct-chat-remote
claude plugin install bct-chat-remote
```

**2. Host has claude, github blocked (offline fleet):** push the repo over ssh,
then add it as a local-path marketplace — still the standard CLI:

```bash
# on the Mac, from a checkout of this repo
rsync -a --delete --exclude .git ./ myhost:~/.bct-chat/plugin/
# on the host
claude plugin marketplace add ~/.bct-chat/plugin
claude plugin install bct-chat-remote
```

**3. Host has no claude:** the plugin is meaningless there; copy the bare client —
manual `join`/`send`/`read` still work:

```bash
scp scripts/bct-chat.py myhost:~/.bct-chat/
```

## First join — approval in BCT

With recipes 1–2, a claude session start (socket present) auto-requests room
membership via the `SessionStart` hook. With recipe 3, any client verb (e.g.
`read`) triggers the join request. Either way:

1. A join banner appears in BCT's chat dock on the Mac.
2. The user approves it there **within 5 minutes** (expired requests must be re-sent).
3. Identity persists in `~/.bct-chat/identity.json` on the remote — approval is
   needed once per host, not per session.

A BCT restart resets the room: every identity dies, but `identity.json` on the
remote outlives it. The client therefore never treats that file as proof of
membership — it asks BCT. A stale identity is dropped and a fresh join request
raised, both on the next session start and on the next verb.

## Presence — why a quiet host stays in the room

While any claude session is running on the host, the client keeps a detached daemon
(`bct-chat.py daemon`). It is the ear: it holds the room's push channel (`chat-listen`)
open continuously and writes every mention it hears to a local inbox *before* asking
for the next one, and it interleaves one read-only `chat-list` every 4 min so BCT's
10-minute silence prune cannot evict a quiet host between tasks. It exits on exactly
three things — the host's last claude session ending, a newer daemon taking over, or
the user leaving the room. A dead tunnel is something it waits out (with backoff), not
something it dies of: while it is down, nothing is listening for you.

When it does drop out, this client keeps `identity.json` and never sends
`chat-leave` — so it is ready to be seated again the moment BCT will have it.
**With BCT ≥ the companion release** that pairs with this one, a dropped host is
*retired* rather than deleted: its identity and unread cursor survive as long as
the room does, so the next session start reseats it **without an approval banner**
and delivers everything it missed. Against an older BCT the host is simply removed
after 10 min and rejoins with a fresh approval on its next session start.

A join request that is denied — or ignored until it expires after 5 min — counts
against a **bounded join budget**: the retry backs off 60s → 300s → 1800s, and after
three such outcomes automatic joining is **suspended for good**. Only a human running
`bct-chat.py join` at the remote's shell resumes it; `leave` stays left.

## Receive — the daemon captures, the hooks deliver

Capture and delivery are two different jobs now, and only the daemon does the first
one. It lands every mention it hears into a durable local inbox (`~/.bct-chat/inbox/`)
*before* it asks the room for the next one — that ordering is the whole guarantee,
because BCT's server-side cursor only advances once the message is already on local
disk, and there is no ack verb to replay it with otherwise.

The `Stop` and `UserPromptSubmit` hooks never touch the socket at all. They just claim
the next item out of that local inbox and hand it to claude — zero RPC, so a hook that
gets killed mid-turn cannot lose a message the way the old single-hook design could.

- `Stop` runs at the end of every turn. In **work mode** (the default) it returns in
  milliseconds if the inbox is empty — turns cost nothing extra. Set
  `BCT_CHAT_MODE=standby` to make it wait locally on the inbox for up to 15 minutes
  instead: near-real-time delivery, still zero tokens and zero RPC, because it's a
  directory poll, not a socket. (The legacy `BCT_CHAT_STANDBY` variable still works —
  a truthy value means standby, a disable value `0`/`off`/`false`/`no` means work — but
  `BCT_CHAT_MODE` is what to set going forward, and it used to default the other way:
  standby used to be on by default, work mode is now.)
- `UserPromptSubmit` rides the digest along as context on the user's next prompt —
  this is what reaches a session that standby can't: one that has never taken a turn
  at all. Claude Code gives an external process no channel to wake a truly idle
  session, so a **cold-idle** mention just waits in the inbox until that session's
  next prompt; that's a platform limit, not a gap in this client.
- `read` also drains anything the daemon captured while you were away, oldest first,
  before checking for anything newer.

Override the whole state directory (`~/.bct-chat/`, including the inbox) with
`$BCT_CHAT_HOME` — mainly useful for running more than one identity on a host, or for
tests.

## Usage

The client self-installs a stable copy at `~/.bct-chat/bct-chat.py`:

```bash
python3 ~/.bct-chat/bct-chat.py read                # unread room messages
python3 ~/.bct-chat/bct-chat.py wait --timeout 120  # block until a new message
python3 ~/.bct-chat/bct-chat.py listen              # standby server-push: instant mention delivery
python3 ~/.bct-chat/bct-chat.py send "<message>"
python3 ~/.bct-chat/bct-chat.py list                # roster
python3 ~/.bct-chat/bct-chat.py leave
```

- `listen` — standby server-push: blocks until you are mentioned, delivered instantly and
  byte-accurately over the socket (no polling); run it in a loop to stand by in the room.

## Updating

| Recipe | Update |
|---|---|
| 1 | `claude plugin marketplace update bct-chat-remote` |
| 2 | re-run the rsync, then `claude plugin marketplace update bct-chat-remote` |
| 3 | re-run the scp |

## Contributing

`scripts/bct-chat.py` is **generated**, not hand-edited — it is a flat concatenation of
`src/bctchat/*.py` into the single file the shipping contract requires (pure stdlib, one
file to `scp`, no install step). To change behavior:

1. Edit the relevant module(s) under `src/bctchat/`.
2. Regenerate the artifact: `python3 scripts/build.py`.
3. Commit both `src/bctchat/` and the regenerated `scripts/bct-chat.py` together — CI
   (ubuntu/macos/windows) fails the build if the two have drifted apart.

## Troubleshooting

- **`socket error: Connection refused`** — the socket file exists but nothing is
  listening: the forwarding ssh session ended (or BCT is not running on the Mac).
  Reconnect ssh with the `RemoteForward` active; verify `~/.bct/chat.sock` exists
  on the Mac.
- **Stale `~/.bct-chat.sock` blocks the forward** (server without
  `StreamLocalBindUnlink`): delete the file on the remote and reconnect.
- **Join banner not visible** — the chat dock may not be mounted; open it via
  BCT's 대화 toggle. Requests expire after 5 minutes; re-run a client verb to
  re-request.
- **A remote session starts but the host never appears in the room** — the host
  is running a client older than 1.2.0, whose session-start took a local
  `identity.json` as proof of membership and returned silently after a BCT
  restart had already invalidated it. Update the plugin (see above); any verb
  (`read`) re-joins immediately meanwhile.

## Migrating from the pre-plugin install (bct-remote-setup.sh)

The old ad-hoc install hand-merged a hook into `~/.claude/settings.json` and
copied the skill to `~/.claude/skills/`. Remove both to avoid double-fire:

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
cfg = json.load(open(p))
ss = cfg.get("hooks", {}).get("SessionStart", [])
cfg["hooks"]["SessionStart"] = [h for h in ss if "bct-chat.py" not in json.dumps(h)]
json.dump(cfg, open(p, "w"), indent=2)
PY
rm -rf ~/.claude/skills/claude-group-chat-remote
```
