"""The verbs. argparse, not hand-rolled scanning: `wait --timeuot 60` used to mean 300s
and `heartbeat --interval -1` reached time.sleep(-1)."""
import argparse, json, os, re, socket, subprocess, sys, time


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def positive(v):
    f = float(v)
    if f <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return f


def do_read():
    """Drain everything the daemon captured, oldest first, THEN chat-read for anything
    newer. Print, THEN ack — one item at a time. A crash mid-drain (Ctrl-C, SIGKILL, a
    wrapper timeout) must leave the not-yet-printed item in processing/, where
    recover_orphans() returns it to the inbox: at-least-once (a rare duplicate) beats
    a silent loss. Claiming the whole run and acking as it goes (the reverse order)
    would delete items the user never saw."""
    while True:
        got = inbox_claim()
        if not got:
            break
        print(got[1].get("text") or "")
        inbox_ack(got[0])
    r = authed("chat-read", [])
    if not r.get("ok"):
        die(r.get("error", "error"))
    text = r.get("text", "")
    if text and text != NO_NEW:
        print(text)


def do_wait(timeout, die_on_timeout=True):
    """Wait on the INBOX. The daemon owns the socket; a second chat-read poller here would
    consume the cursor out from under it (D13). `listen` is the same wait — it just does not
    die() on an empty window: it is a single server-push-shaped turn (the old chat-listen
    semantics), silent and exit-0 when nothing arrived, while `wait` is a human waiting for
    an answer and must say so loudly."""
    got = inbox_wait(timeout, poll=1.0)
    if not got:
        if die_on_timeout:
            die(f"timeout: no new message within {int(timeout)}s")
        return
    print(got[1].get("text") or "")
    inbox_ack(got[0])


HOOK_VERBS = ("session-start", "session-end", "stop-hook", "prompt-submit")


def build_parser():
    p = argparse.ArgumentParser(prog="bct-chat.py", description="BCT group-chat external client")
    sub = p.add_subparsers(dest="verb", required=True)
    j = sub.add_parser("join"); j.add_argument("name", nargs="*")
    sub.add_parser("leave")
    s = sub.add_parser("send"); s.add_argument("message", nargs="*")
    sub.add_parser("read")
    sub.add_parser("list")
    for v in ("wait", "listen"):
        w = sub.add_parser(v)
        w.add_argument("--timeout", type=positive, default=300)
    for v in HOOK_VERBS:
        sub.add_parser(v)
    for v in ("daemon", "heartbeat"):
        d = sub.add_parser(v)
        d.add_argument("--interval", type=positive, default=PRESENCE_INTERVAL)
        d.add_argument("--listen-timeout", type=positive, default=LISTEN_TIMEOUT)
        d.add_argument("--max-uptime", type=positive, default=None,
                       help=argparse.SUPPRESS)     # accepted and ignored: back-compat
    return p


def main(argv):
    if argv and argv[0] in HOOK_VERBS:
        # The hook verbs take no flags and must always exit 0 — hooks.json falls back
        # python3 -> python on ANY nonzero exit, and the re-run sees stdin already
        # drained. Dispatch directly, before argparse ever gets a chance to raise its
        # own SystemExit(2) on some unexpected argv.
        {"session-start": session_start, "session-end": session_end,
         "stop-hook": stop_hook, "prompt-submit": prompt_submit_hook}[argv[0]]()
        return
    a = build_parser().parse_args(argv)
    v = a.verb
    if v == "join":
        clear_join_state()                         # a human at the shell always wins
        do_join(" ".join(a.name) or default_name())
    elif v == "leave":
        do_leave()
    elif v == "send":
        if not a.message:
            die('send needs "<message>"')     # argparse's generic "required: message" is
                                               # useless at a remote shell; say what to type
        r = authed("chat-send", [" ".join(a.message)])
        if not r.get("ok"):
            die(r.get("error", "error"))
    elif v == "read":
        do_read()
    elif v == "list":
        r = authed("chat-list", [])
        if not r.get("ok"):
            die(r.get("error", "error"))
        print(r.get("text", ""))
    elif v == "wait":
        do_wait(a.timeout)
    elif v == "listen":
        do_wait(a.timeout, die_on_timeout=False)
    elif v in ("daemon", "heartbeat"):
        do_daemon(presence_interval=a.interval, listen_timeout=a.listen_timeout)
