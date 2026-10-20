"""CLI tests: every subcommand parses, and run/freeze/grade/accept are wired."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_bench  # noqa: E402
from bench import cli, home as home_mod, manifest, run as run_mod

MODEL = "test/model-a"
COMMANDS = [
    ["--help"], ["lock", "--help"], ["freeze", "--help"], ["isolation-check", "--help"],
    ["pack", "--help"], ["pack", "snapshot", "--help"], ["run", "--help"],
    ["grade", "--help"], ["accept", "--help"], ["export", "--help"],
    ["status", "--help"], ["leak-hashes", "--help"],
]


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        fake_bench.install_modules()
        self.fixture = fake_bench.build_home(self.root)
        self.home = self.fixture["home"]
        self.omp = fake_bench.install_fake_omp(self.root)
        self._env = mock.patch.dict(os.environ, {
            "BENCH_HOME": str(self.home),
            "BENCH_OMP": str(self.omp),
            "FAKE_BENCH_SCENARIO": "completed",
        }, clear=False)
        self._env.start()
        self.protocol = home_mod.load_protocol()
        self.private = home_mod.load_private(self.home)
        manifest.write_lock(self.home, self.protocol, self.private)

    def tearDown(self) -> None:
        self._env.stop()
        fake_bench.remove_modules()
        sys.modules.pop("bench.export", None)
        self._tmp.cleanup()

    def call(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
                if not isinstance(exc.code, int) and exc.code:
                    out.write(str(exc.code))
        return code, out.getvalue()

    def test_help_for_every_command(self) -> None:
        for argv in COMMANDS:
            code, output = self.call(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("usage:", output, argv)

    def test_status_and_lock(self) -> None:
        code, output = self.call(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["batches"], [])
        (self.home / "manifest.lock.json").unlink()
        code, output = self.call(["lock"])
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "manifest.lock.json").is_file())

    def test_leak_hashes(self) -> None:
        out = self.root / "hashes.txt"
        code, _ = self.call(["leak-hashes", "--out", str(out)])
        self.assertEqual(code, 0)
        lines = out.read_text().splitlines()
        expected = sorted(hashlib.sha256(term.encode()).hexdigest()
                          for term in self.private["private_terms"])
        self.assertEqual(lines, expected)

    def test_run_grade_accept(self) -> None:
        code, output = self.call(["run", "--role", "task", "--model", MODEL,
                                  "--effort", "high", "--packs", "c1", "--attempts", "1"])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual({r["outcome"] for r in result["runs"]}, {"completed"})
        run_id = result["runs"][0]["run_id"]

        code, output = self.call(["grade", "--run", run_id])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["run_id"], run_id)

        code, output = self.call(["accept", result["batch"]["batch_id"]])
        self.assertEqual(code, 0)
        batch = json.loads(output)
        self.assertTrue(batch["accepted"])
        self.assertFalse(batch["quarantined"])

        code, output = self.call(["run", "--retry", result["batch"]["batch_id"]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["runs"], [])

    def test_freeze_refuses_then_writes_delegation(self) -> None:
        os.environ["FAKE_BENCH_SCENARIO"] = "freeze"
        code, output = self.call(["freeze", "--role", "task", "--pack", "c1"])
        self.assertEqual(code, 2)
        self.assertIn("--force", output)
        frozen_before = json.loads(
            (self.fixture["pack_dir"] / "delegation.json").read_text())

        code, output = self.call(["freeze", "--role", "task", "--pack", "c1",
                                  "--force"])
        self.assertEqual(code, 0)
        result = json.loads(output)
        frozen = json.loads((self.fixture["pack_dir"] / "delegation.json").read_text())
        self.assertEqual(frozen["agent"], "task")
        self.assertEqual(frozen["task"], "freeze assignment")
        self.assertEqual(frozen["sha256"], result["sha256"])
        self.assertEqual(frozen["head"], {"model": self.protocol["head"]["model"],
                                          "effort": self.protocol["head"]["effort"]})
        self.assertNotEqual(frozen["sha256"], frozen_before["sha256"])
        self.assertTrue((Path(result["run_dir"]) / "summary.json").is_file())
        drift = manifest.drift(self.home, self.protocol, self.private)
        self.assertTrue(any("delegation.json changed" in d for d in drift), drift)

    def test_isolation_check_cli(self) -> None:
        code, output = self.call(["isolation-check"])
        self.assertEqual(code, 0, output)
        self.assertIn("ok", output)

    def test_export_wiring(self) -> None:
        module = types.ModuleType("bench.export")

        def export(home, docs_dir, protocol):
            return Path(docs_dir) / "data" / "v2.js"

        module.export = export
        sys.modules["bench.export"] = module
        code, output = self.call(["export"])
        self.assertEqual(code, 0)
        self.assertIn("v2.js", json.loads(output)["export"])

    def test_retry_cli_without_role(self) -> None:
        code, output = self.call(["run", "--retry", "nosuchbatch"])
        self.assertEqual(code, 2)
        self.assertIn("nosuchbatch", output)


if __name__ == "__main__":
    unittest.main()
