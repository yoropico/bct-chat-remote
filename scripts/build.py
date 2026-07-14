#!/usr/bin/env python3
"""Concatenate src/bctchat/*.py into the single-file artifact scripts/bct-chat.py.

The artifact is ONE namespace, not a package: the tests, and any operator with a
python REPL, monkeypatch module globals (mod.rpc, mod.sock_available). A real
`from bctchat.wire import rpc` would bind those names at import time and silently
defeat every such patch — so intra-package imports are stripped, never emitted.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src", "bctchat")
ARTIFACT = os.path.join(REPO, "scripts", "bct-chat.py")

# Concatenation order = dependency order. config first (it defines ARTIFACT/paths),
# cli last (it defines main()).
MODULES = ["config", "wire", "state", "inbox", "membership", "presence", "delivery", "cli"]

HEADER = "#!/usr/bin/env python3\n"
FOOTER = '\n\nif __name__ == "__main__":\n    main(sys.argv[1:])\n'


def is_intra_import(line):
    s = line.strip()
    return s.startswith("from bctchat") or s.startswith("import bctchat")


def build_artifact():
    out = [HEADER]
    for name in MODULES:
        path = os.path.join(SRC, name + ".py")
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.readlines() if not is_intra_import(l)]
        body = "".join(lines).strip("\n")
        out.append(f"\n\n# ---- {name} " + "-" * (66 - len(name)) + "\n" + body + "\n")
    return "".join(out).rstrip("\n") + FOOTER


def main(argv):
    text = build_artifact()
    if "--stdout" in argv:
        # The artifact holds Korean strings; never trust the locale (same rule
        # bctchat/config.py documents for the client itself — Windows defaults a text-mode
        # stdout to cp1252/cp949, and encoding a Korean character there raises
        # UnicodeEncodeError). Write raw UTF-8 bytes straight to the binary buffer: it
        # sidesteps the locale entirely AND, being binary, never applies Windows'
        # \n -> \r\n translation — so this path stays byte-identical to the file write
        # below, whose open() already pins newline="\n".
        sys.stdout.buffer.write(text.encode("utf-8"))
        return
    tmp = ARTIFACT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, ARTIFACT)
    os.chmod(ARTIFACT, 0o755)
    print(f"wrote {ARTIFACT} ({len(text)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
