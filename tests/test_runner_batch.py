"""End-to-end batch runner tests with a fake omp and stand-in Analysis modules."""

from __future__ import annotations

import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_bench  # noqa: E402
from bench import home as home_mod
from bench import manifest, run as run_mod

MODEL = "test/model-a"
EFFORT = "high"


class BatchRunnerTest(unittest.TestCase):
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
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)
        self.protocol = home_mod.load_protocol()
        self.private = home_mod.load_private(self.home)
        manifest.write_lock(self.home, self.protocol, self.private)

    def tearDown(self) -> None:
        self._env.stop()
        fake_bench.remove_modules()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def start(self, attempts: int = 1, packs: list[str] | None = None,
              use_baseline: bool = True) -> dict:
        return run_mod.start(self.home, self.protocol, self.private, "task",
                             [MODEL], [EFFORT], packs or ["c1"], attempts, None,
                             use_baseline, dict(self.protocol["judge_default"]))

    def outcomes(self, result: dict) -> dict[str, str]:
        return {r["run_id"]: r["outcome"] for r in result["runs"]}

    def batch_runs(self, batch_id: str) -> list[dict]:
        batch_dir = self.home / "runs" / batch_id
        return [json.loads((d / "run.json").read_text())
                for d in sorted(batch_dir.iterdir()) if (d / "run.json").is_file()]

    # ------------------------------------------------------------------ tests

    def test_completed_batch_writes_run_dir_and_artifacts(self) -> None:
        result = self.start()
        self.assertEqual(len(result["runs"]), 2, "user config plus auto baseline")
        self.assertEqual(set(self.outcomes(result).values()), {"completed"})

        runs = {r["config"]["key"]: r for r in result["runs"]}
        self.assertIn(f"{MODEL}:{EFFORT}", runs)
        baseline_key = f"{self.protocol['baseline']['task']['model']}:" \
                       f"{self.protocol['baseline']['task']['effort']}"
        self.assertTrue(runs[baseline_key]["baseline"])
        self.assertFalse(runs[f"{MODEL}:{EFFORT}"]["baseline"])

        run_dir = self.home / "runs" / result["batch"]["batch_id"] / \
            runs[f"{MODEL}:{EFFORT}"]["run_id"]
        for name in ("run.json", "summary.json", "audit.json", "overlay.yml",
                     "sandbox.sb", "prompt.md", "events.jsonl", "stderr.log",
                     "pristine-tree.sha256"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertTrue((run_dir / "repo" / "src" / "app.js").is_file())
        self.assertFalse((run_dir / "repo" / ".git").exists())
        self.assertFalse((run_dir / "repo" / "node_modules" / ".git").exists())
        self.assertEqual(run_dir.joinpath("repo", "node_modules", "lodash").resolve(),
                         (self.home / "deps" / "demo" / "node_modules" / "lodash").resolve())
        self.assertEqual(run_dir.joinpath("repo", "node_modules", "@acme", "lib").resolve(),
                         (run_dir / "repo" / "packages" / "lib").resolve())

        record = runs[f"{MODEL}:{EFFORT}"]
        self.assertTrue(record["isolation_proof"]["ok"])
        self.assertGreater(record["isolation_proof"]["checked"], 0)
        self.assertEqual(record["head"]["model"], self.protocol["head"]["model"])
        self.assertGreaterEqual(record["concurrency"], 1)

        # Every run's profile denies the sibling run of the same batch.
        run_dirs = sorted(d for d in (run_dir.parent).iterdir() if d.is_dir())
        self.assertEqual(len(run_dirs), 2)
        for current in run_dirs:
            profile = (current / "sandbox.sb").read_text()
            sibling = [d for d in run_dirs if d != current][0]
            self.assertIn(str(sibling.resolve()), profile)
            self.assertIn(f"(deny file-read-data (subpath \"{sibling.resolve()}\"))", profile)

        prompt = (run_dir / "prompt.md").read_text()
        self.assertIn(self.fixture["frozen"]["task"], prompt)
        overlay = (run_dir / "overlay.yml").read_text()
        self.assertIn(f'{MODEL}:{EFFORT}', overlay)
        self.assertIn("disabledAgents: [scout, sonic", overlay)
        self.assertNotIn("disabledAgents: [task", overlay)
        self.assertIn(f"maxRuntimeMs: {1 * 60 * 1000}", overlay)

        grades = [json.loads(line) for line in
                  (self.home / "grades.jsonl").read_text().splitlines()]
        self.assertEqual(len(grades), 2)
        self.assertEqual(grades[0]["kind"], "oracle")

        batch = json.loads((self.home / "runs" / result["batch"]["batch_id"]
                            / "batch.json").read_text())
        self.assertEqual(batch["packs"], ["c1"])
        self.assertFalse(batch["quarantined"])

    def test_timeout_is_scored(self) -> None:
        os.environ["FAKE_BENCH_SCENARIO"] = "timeout"
        result = self.start()
        self.assertEqual(set(self.outcomes(result).values()), {"timeout"})
        grades = (self.home / "grades.jsonl").read_text().splitlines()
        self.assertEqual(len(grades), 2)

    def test_quota_paused_is_not_scored_and_retry_adds_attempt(self) -> None:
        os.environ["FAKE_BENCH_SCENARIO"] = "quota"
        result = self.start()
        batch_id = result["batch"]["batch_id"]
        self.assertEqual(set(self.outcomes(result).values()), {"quota-paused"})
        self.assertFalse((self.home / "grades.jsonl").exists())

        os.environ["FAKE_BENCH_SCENARIO"] = "completed"
        retried = run_mod.retry(self.home, self.protocol, self.private,
                                batch_id, None, None)
        self.assertEqual(len(retried["runs"]), 2)
        self.assertEqual({r["attempt"] for r in retried["runs"]}, {2})
        self.assertEqual(set(self.outcomes(retried).values()), {"completed"})

        runs = self.batch_runs(batch_id)
        self.assertEqual(len(runs), 4, "attempts are kept")
        for config in {r["config"]["key"] for r in runs}:
            attempts = [r["attempt"] for r in runs if r["config"]["key"] == config]
            self.assertEqual(attempts, [1, 2])

        again = run_mod.retry(self.home, self.protocol, self.private,
                              batch_id, None, None)
        self.assertEqual(again["runs"], [])
        self.assertIn("skipped", again)

    def test_delegation_mismatch_is_invalid(self) -> None:
        os.environ["FAKE_BENCH_SCENARIO"] = "mismatch"
        result = self.start()
        self.assertEqual(set(self.outcomes(result).values()), {"invalid"})
        detail = result["runs"][0]["outcome_detail"]
        self.assertIn("context", detail)

    def test_fallback_is_invalid(self) -> None:
        os.environ["FAKE_BENCH_SCENARIO"] = "fallback"
        result = self.start()
        self.assertEqual(set(self.outcomes(result).values()), {"invalid"})
        self.assertIn("fallback", result["runs"][0]["outcome_detail"])

    def test_classify_timeout_from_wall_clock_and_quota_from_head(self) -> None:
        frozen = self.fixture["frozen"]
        summary = {"head": {"session": "x", "delegations": [
            {f: frozen[f] for f in ("agent", "context", "task")}], "error": None},
            "worker": {"completed": True, "fallback": False,
                       "observed_models": [MODEL], "resolved_model": MODEL + ":high",
                       "errors": []}}
        outcome, _ = run_mod.classify(summary, {"timed_out": True}, MODEL, frozen)
        self.assertEqual(outcome, "timeout")

        quota = json.loads(json.dumps(summary))
        quota["worker"] = None
        quota["head"]["error"] = "usage limit reached for this account"
        outcome, _ = run_mod.classify(quota, {"timed_out": False}, MODEL, frozen)
        self.assertEqual(outcome, "quota-paused")

        missing = {"head": {"session": "x", "delegations": [], "error": None},
                   "worker": None}
        outcome, detail = run_mod.classify(missing, {"timed_out": False}, MODEL, frozen)
        self.assertEqual(outcome, "invalid")
        self.assertIn("delegation", detail)

    def test_no_worker_session_is_setup_error(self) -> None:
        summary = {"head": {"session": "x", "error": None, "delegations": [
            {f: self.fixture["frozen"][f] for f in ("agent", "context", "task")}]},
            "worker": None}
        outcome, detail = run_mod.classify(summary, {"timed_out": False}, MODEL,
                                           self.fixture["frozen"])
        self.assertEqual(outcome, "setup-error")
        self.assertIn("worker session", detail)

    def test_baseline_drift_quarantines_and_accept_clears(self) -> None:
        first = self.start()
        run_mod.accept(self.home, first["batch"]["batch_id"])

        os.environ["FAKE_BENCH_SCORE"] = "0.95"
        second = self.start(use_baseline=True)
        batch = second["batch"]
        self.assertTrue(batch["quarantined"])
        self.assertIn("baseline moved", batch["quarantine_reason"])

        accepted = run_mod.accept(self.home, batch["batch_id"])
        self.assertFalse(accepted["quarantined"])
        self.assertTrue(accepted["accepted"])
        self.assertIsNone(accepted["quarantine_reason"])

    def test_resume_skips_existing_final_runs(self) -> None:
        result = self.start()
        batch_id = result["batch"]["batch_id"]
        # Re-running every spec of the same batch must not duplicate run dirs.
        specs = [("c1", f"{MODEL}:{EFFORT}", 1)]
        again = run_mod._run_specs(self.home, self.protocol, self.private,
                                   result["batch"], specs,
                                   dict(self.protocol["judge_default"]), None,
                                   manifest.manifest_sha256(self.home))
        self.assertEqual(again["runs"], [])
        self.assertEqual(len(self.batch_runs(batch_id)), 2)

    def test_manifest_drift_refuses_before_creating_a_batch(self) -> None:
        (self.fixture["pack_dir"] / "request.md").write_text("changed request\n")
        batches_before = sorted((self.home / "runs").iterdir())
        with self.assertRaises(SystemExit) as caught:
            self.start()
        self.assertIn("drift", str(caught.exception))
        self.assertEqual(sorted((self.home / "runs").iterdir()), batches_before)

    def test_reinvoking_completes_a_partial_batch(self) -> None:
        judge = dict(self.protocol["judge_default"])
        batch = run_mod.create_batch(self.home, self.protocol, "task",
                                     [f"{MODEL}:{EFFORT}"], None, judge, 1, ["c1"])
        result = run_mod.start(self.home, self.protocol, self.private, "task",
                               [MODEL], [EFFORT], ["c1"], 1, None, False, judge)
        self.assertEqual(result["batch"]["batch_id"], batch["batch_id"])
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["outcome"], "completed")

    def test_parallel_one(self) -> None:
        result = run_mod.start(self.home, self.protocol, self.private, "task",
                               [MODEL], [EFFORT], ["c1"], 1, 1, True,
                               dict(self.protocol["judge_default"]))
        self.assertEqual(len(result["runs"]), 2)
        self.assertEqual(set(self.outcomes(result).values()), {"completed"})

    def test_status_and_isolation_check(self) -> None:
        self.start()
        status = run_mod.status(self.home)
        self.assertEqual(len(status["batches"]), 1)
        self.assertEqual(status["batches"][0]["runs"], 2)
        self.assertEqual(status["packs"]["task"], ["c1"])

        proof = run_mod.isolation_check(self.home, self.private)
        self.assertTrue(proof["ok"], proof)
        denied = [c for c in proof["checks"] if c["kind"] == "deny"]
        self.assertTrue(any(c["category"] == "answer" for c in denied))
        self.assertTrue(all(c["ok"] for c in denied))
        allowed = {c["category"] for c in proof["checks"] if c["kind"] == "allow"}
        self.assertIn("repo", allowed)
        self.assertIn("deps", allowed)
        self.assertFalse((self.home / "_isolation-check").exists())


if __name__ == "__main__":
    unittest.main()
