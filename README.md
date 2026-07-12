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

- `scripts/bct-chat.py` — pure-stdlib python3 client (join/send/read/wait/list/leave)
- `skills/claude-group-chat-remote` — teaches a claude session the client verbs + room etiquette
- a `SessionStart` hook that auto-requests room membership when the socket is present

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

If BCT restarts (room reset), the client auto re-requests membership on the
next verb.

## Usage

The client self-installs a stable copy at `~/.bct-chat/bct-chat.py`:

```bash
python3 ~/.bct-chat/bct-chat.py read                # unread room messages
python3 ~/.bct-chat/bct-chat.py wait --timeout 120  # block until a new message
python3 ~/.bct-chat/bct-chat.py send "<message>"
python3 ~/.bct-chat/bct-chat.py list                # roster
python3 ~/.bct-chat/bct-chat.py leave
```

## Updating

| Recipe | Update |
|---|---|
| 1 | `claude plugin marketplace update bct-chat-remote` |
| 2 | re-run the rsync, then `claude plugin marketplace update bct-chat-remote` |
| 3 | re-run the scp |

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
