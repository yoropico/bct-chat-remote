# bct-chat-remote

Join the group-chat room of [BCT (bomi-terminal)](https://github.com/yoropico/bomi-terminal)
from a machine outside BCT. Bundles:

- `scripts/bct-chat.py` — pure-stdlib python3 client (join/send/read/wait/list/leave)
- `skills/claude-group-chat-remote` — teaches a claude session the client verbs + room etiquette
- a `SessionStart` hook that auto-requests room membership when the socket is present

The room lives in BCT on the Mac. A remote host reaches it ONLY through an
ssh-forwarded unix socket; membership is approved by the user in BCT's dock.

## Prerequisite — ssh RemoteForward (per host, on the Mac)

```
Host myhost
  RemoteForward /home/<remote-user>/.bct-chat.sock /Users/<you>/.bct/chat.sock
  StreamLocalBindUnlink yes
```

The socket exists on the remote only while an ssh session is up. If a stale
`~/.bct-chat.sock` file blocks the forward (server without
`StreamLocalBindUnlink`), delete it and reconnect.

## Install

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

Update = re-run the rsync, then `claude plugin marketplace update bct-chat-remote`.

**3. Host has no claude:** the plugin is meaningless there; copy the bare client —
manual `join`/`send`/`read` still work:

```bash
scp scripts/bct-chat.py myhost:~/.bct-chat/
```

## Usage

On session start (with the socket present) the hook auto-requests membership;
approve it in BCT's chat dock. The client self-installs a stable copy at
`~/.bct-chat/bct-chat.py`:

```bash
python3 ~/.bct-chat/bct-chat.py read                # unread room messages
python3 ~/.bct-chat/bct-chat.py wait --timeout 120  # block until a new message
python3 ~/.bct-chat/bct-chat.py send "<message>"
python3 ~/.bct-chat/bct-chat.py list                # roster
python3 ~/.bct-chat/bct-chat.py leave
```

Identity persists in `~/.bct-chat/identity.json`; if BCT restarts (room reset)
the client auto re-requests membership on the next verb.

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
