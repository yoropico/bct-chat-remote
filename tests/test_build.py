#!/usr/bin/env python3
"""The shipped artifact is generated from src/. A stale artifact is a bug: the
tests, the scp target, the stable copy and the plugin hook all exec the artifact,
so src/ drifting from it means the reviewed code is not the running code."""
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(REPO, "scripts", "build.py")
ARTIFACT = os.path.join(REPO, "scripts", "bct-chat.py")


class BuildTests(unittest.TestCase):
    def test_committed_artifact_matches_src(self):
        r = subprocess.run([sys.executable, BUILD, "--stdout"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(ARTIFACT, encoding="utf-8") as f:
            self.assertEqual(f.read(), r.stdout,
                             "scripts/bct-chat.py is stale — run: python3 scripts/build.py")

    def test_artifact_has_no_intra_package_imports(self):
        with open(ARTIFACT, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("from bctchat", src)
        self.assertNotIn("import bctchat", src)


if __name__ == "__main__":
    unittest.main()
