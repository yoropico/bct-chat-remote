"""Entry point: argv dispatch for the join/send/read/... verbs and hook shims."""
import json, os, re, socket, subprocess, sys, time


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main(argv):
    if not argv:
        die("usage: bct-chat.py <join|send|read|wait|listen|list|leave|session-start|session-end|stop-hook|prompt-submit> …")
    verb, rest = argv[0], argv[1:]
    if verb == "join":
        clear_cooldown()                      # manual intent overrides the cooldown
        do_join(" ".join(rest) or default_name())
    elif verb == "session-start":
        session_start()
    elif verb == "session-end":
        session_end()
    elif verb == "stop-hook":
        stop_hook()
    elif verb == "prompt-submit":
        prompt_submit_hook()
    elif verb == "send":
        msg = " ".join(rest)
        if not msg:
            die('send needs "<message>"')
        r = authed("chat-send", [msg])
        if not r.get("ok"):
            die(r.get("error", "error"))
    elif verb == "read":
        r = authed("chat-read", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "wait":
        timeout = 300
        if "--timeout" in rest:
            i = rest.index("--timeout")
            if i + 1 >= len(rest) or not rest[i + 1].isdigit():
                die("wait --timeout <seconds>")
            timeout = int(rest[i + 1])
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = authed("chat-read", [])
            if not r.get("ok"):
                die(r.get("error", "error"))
            if r.get("text") and r["text"] != NO_NEW:
                print(r["text"])
                return
            time.sleep(2)
        die(f"timeout: no new message within {timeout}s")
    elif verb == "listen":
        # Server-push standby: chat-listen holds the connection until a mention
        # is posted (or ~30s server-side). One call = one turn; the standby loop
        # re-invokes. 40s socket timeout tolerates the ~30s server hold + slack.
        r = authed("chat-listen", [], timeout=40)
        if not r.get("ok"):
            die(r.get("error", "error"))
        txt = r.get("text", "")
        if txt and txt not in (NO_NEW, NO_MENTION):
            print(txt)
    elif verb == "list":
        r = authed("chat-list", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif verb == "leave":
        r = rpc("chat-leave", [], identity())
        for p in (IDENTITY, PENDING):
            forget(p)
        if not r.get("ok") and r.get("error") != NOT_INVITED:
            die(r.get("error", "error"))
    elif verb == "heartbeat":
        interval, max_uptime = HEARTBEAT_INTERVAL, HEARTBEAT_MAX_UPTIME
        if "--interval" in rest:
            i = rest.index("--interval")
            if i + 1 >= len(rest):
                die("heartbeat --interval <seconds>")
            try:
                interval = float(rest[i + 1])
            except ValueError:
                die("heartbeat --interval <seconds>")
        if "--max-uptime" in rest:
            i = rest.index("--max-uptime")
            if i + 1 >= len(rest):
                die("heartbeat --max-uptime <seconds>")
            try:
                max_uptime = float(rest[i + 1])
            except ValueError:
                die("heartbeat --max-uptime <seconds>")
        do_heartbeat(interval, max_uptime)
    else:
        die(f"unknown verb: {verb}")
