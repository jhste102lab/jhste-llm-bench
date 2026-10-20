"""Leak detection: private terms, joined n-grams, hashed CLI, generic patterns."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bench.leak import check_hashes, check_text, generic_hits

REPO = Path(__file__).resolve().parents[1]


class LeakTest(unittest.TestCase):
    def test_private_terms_join_variants(self):
        terms = ["sample-visits", "fixture_catalog", "fixtureterm"]
        text = ("The SAMPLE_VISITS table and Fixture-Catalog join. "
                "See fixtureterm for details.")
        self.assertEqual(check_text(text, terms),
                         ["fixture_catalog", "fixtureterm", "sample-visits"])

    def test_private_terms_do_not_match_substrings(self):
        self.assertEqual(check_text("superfixtureterminal", ["fixtureterm"]), [])

    def test_generic_patterns(self):
        self.assertIn("home-path", generic_hits("see /Users/someone/code/x"))
        self.assertIn("omp-state", generic_hits("path /Users/someone/.omp/agent/sessions"))
        self.assertIn("uuid", generic_hits("id 0f8fad5b-d9cb-469f-a165-70867728950e"))
        self.assertIn("session-id", generic_hits("id 01a15819a3ea72a8ac35e76efcd6002b"))
        self.assertIn("email", generic_hits("mail dev@example.com"))
        self.assertIn("openai-token", generic_hits("sk-abcdefghijklmnopqrst"))
        self.assertIn("github-token", generic_hits("ghp_" + "a" * 30))
        self.assertIn("github-pat", generic_hits("github_pat_" + "a" * 30))
        self.assertEqual(generic_hits("window.BENCH_V2 = {rows: []};"), [])

    def test_hashes_only(self):
        hashes = {hashlib.sha256(b"secretterm").hexdigest()}
        self.assertEqual(len(check_hashes("Secret_Term and secretterm", hashes)), 1)
        self.assertEqual(check_hashes("nothing here", hashes), [])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashes = root / "hashes.txt"
            hashes.write_text(hashlib.sha256(b"secretterm").hexdigest() + "\n")
            leak = root / "leak.txt"
            leak.write_text("this mentions Secret_Term once\n")
            clean = root / "clean.txt"
            clean.write_text("nothing private here\n")
            generic = root / "generic.txt"
            generic.write_text("see /Users/someone/code\n")

            env = dict(os.environ, PYTHONPATH=str(REPO))
            run = lambda *args: subprocess.run(
                [sys.executable, "-m", "bench.leak", "--hashes", str(hashes), *args],
                cwd=REPO, env=env, capture_output=True, text=True)

            hit = run(str(leak))
            self.assertEqual(hit.returncode, 1, hit.stderr)
            self.assertIn("hashed-term", hit.stdout)
            self.assertNotIn("Secret_Term", hit.stdout)

            self.assertEqual(run(str(clean)).returncode, 0)
            self.assertEqual(run(str(generic)).returncode, 1)

            # Site files carry no generic leak patterns.
            docs = REPO / "docs"
            if docs.is_dir():
                site = run(str(docs))
                self.assertEqual(site.returncode, 0, site.stdout)


if __name__ == "__main__":
    unittest.main()
