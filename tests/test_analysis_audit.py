"""Audit: blocked attempts vs successful forbidden access, paths and domains."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bench.audit import audit

FIXTURES = Path(__file__).parent / "fixtures" / "sessions" / "audit"


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "runs" / "batch" / "run"
        shutil.copytree(FIXTURES, self.run_dir)
        self.rules = [
            {"category": "answer", "kind": "path",
             "value": os.path.expanduser("~/code")},
            {"category": "answer", "kind": "path",
             "value": "/tmp/bench-audit-forbidden"},
            {"category": "credential", "kind": "path",
             "value": os.path.expanduser("~/.ssh")},
            {"category": "production", "kind": "domain", "value": "example-catalog.test"},
            {"category": "other-run", "kind": "path",
             "value": str(self.run_dir / "outside")},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocked_and_contamination(self):
        result = audit(self.run_dir, self.rules)
        # c1 ~/code denied, c4 bash cat denied, c9 ~/.ssh denied.
        self.assertEqual(result["blocked"], 3)
        self.assertTrue(result["contaminated"])
        self.assertEqual(result["accessed"],
                         [{"category": "answer", "count": 1},
                          {"category": "other-run", "count": 1},
                          {"category": "production", "count": 1}])

        by_target = {(e["tool"], e["category"], e["blocked"]) for e in result["events"]}
        self.assertIn(("read", "answer", True), by_target)
        self.assertIn(("bash", "answer", True), by_target)
        self.assertIn(("read", "credential", True), by_target)
        self.assertIn(("read", "production", False), by_target)
        self.assertIn(("write", "other-run", False), by_target)
        # A web search query is not an access.
        self.assertNotIn("web_search", {e["tool"] for e in result["events"]})

    def test_repo_reads_are_never_violations(self):
        rules = [{"category": "answer", "kind": "path", "value": str(self.run_dir)}]
        result = audit(self.run_dir, rules)
        targets = [e["target"] for e in result["events"]]
        self.assertFalse(any(str(self.run_dir / "repo") in target for target in targets))
        # The sibling directory outside the repo is still a violation.
        self.assertIn(str(self.run_dir / "outside" / "notes.txt"), targets)

    def test_clean_run(self):
        rules = [{"category": "answer", "kind": "path", "value": "/nonexistent/path"}]
        result = audit(self.run_dir, rules)
        self.assertEqual(result, {"blocked": 0, "accessed": [],
                                   "contaminated": False, "events": []})

    def test_domain_matching_uses_host_and_path(self):
        rules = [{"category": "private-repo", "kind": "domain",
                  "value": "example-catalog.test/data"}]
        result = audit(self.run_dir, rules)
        self.assertTrue(result["contaminated"])
        self.assertEqual(result["accessed"], [{"category": "private-repo", "count": 1}])


if __name__ == "__main__":
    unittest.main()
