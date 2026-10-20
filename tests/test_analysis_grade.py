"""Grading: oracle parsing, checklist + blinded judge, append-only records."""

from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from bench import grade as grade_module
from bench import judge as judge_module
from bench.grade import grade_run

FIXTURES = Path(__file__).parent / "fixtures" / "grade" / "home"
PROTOCOL = {
    "version": "v2",
    "worker_timeout_minutes": 1,
    "judge_default": {"model": "anthropic/claude-opus-5-5", "effort": "medium"},
}
JUDGE = {"model": "anthropic/claude-opus-5-5", "effort": "medium"}


def fake_verdicts(report, facts):
    return {"verdicts": [
        {"id": "b1", "verdict": "covered", "evidence": "shared package"},
        {"id": "b2", "verdict": "contradicted", "evidence": "no import-time writes"},
    ]}


class GradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        shutil.copytree(FIXTURES, self.home)
        self._real_link = grade_module._link_deps
        grade_module._link_deps = lambda repo, node_modules: None
        self._real = (grade_module._grading_profile, grade_module._rules,
                      grade_module._wrap)
        self.profiles = []
        grade_module._rules = lambda home, run_dir: [
            {"category": "evaluator", "kind": "path", "value": "/nonexistent"}]
        grade_module._grading_profile = lambda home, run_dir, deps, pack_dir, rules: (
            "(version 1)\n(allow default)\n")
        grade_module._wrap = lambda cmd, profile: self.profiles.append(
            (list(cmd), Path(profile))) or cmd

    def tearDown(self):
        grade_module._link_deps = self._real_link
        (grade_module._grading_profile,
         grade_module._rules, grade_module._wrap) = self._real
        self.tmp.cleanup()

    def run_dir(self, name):
        return self.home / "runs" / "b1" / name

    def test_task_oracle(self):
        run = self.run_dir("b1-task-c1-fixture-a1")
        record = grade_run(self.home, run, PROTOCOL)
        self.assertEqual(record["kind"], "oracle")
        self.assertEqual((record["passed"], record["total"]), (2, 3))
        # baseline_passed=1: (2-1)/(3-1) normalised gain.
        self.assertEqual(record["baseline_passed"], 1)
        self.assertAlmostEqual(record["score"], 0.5)
        self.assertTrue(record["critical"])
        self.assertIsNone(record["setup_error"])
        self.assertIsNone(record["judge"])
        self.assertEqual([i["verdict"] for i in record["items"]],
                         ["passed", "failed", "passed"])
        self.assertEqual(record["run_id"], "b1-task-c1-fixture-a1")
        self.assertEqual(record["batch_id"], "b1")

        grade_run(self.home, run, PROTOCOL)
        lines = (self.home / "grades.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["score"], record["score"])

    def test_oracle_setup_error(self):
        run = self.run_dir("b1-task-c2-fixture-a1")
        record = grade_run(self.home, run, PROTOCOL)
        self.assertIsNone(record["score"])
        self.assertEqual(record["kind"], "oracle")
        self.assertIn("workspace package escapes repo", record["setup_error"])

    def test_oracle_runs_in_grading_sandbox(self):
        run = self.run_dir("b1-task-c1-fixture-a1")
        grade_run(self.home, run, PROTOCOL)
        profile = run / "grade.sb"
        self.assertTrue(profile.is_file())
        self.assertIn("(version 1)", profile.read_text())
        self.assertEqual(len(self.profiles), 1)
        cmd, profile_path = self.profiles[0]
        self.assertEqual(profile_path, profile)
        self.assertEqual(cmd[0], "bash")
        self.assertEqual(Path(cmd[1]), self.home / "packs" / "task" / "c1" / "oracle" / "check.sh")

    def test_malformed_oracle_cases_are_setup_error(self):
        script = self.home / "packs" / "task" / "c1" / "oracle" / "check.sh"
        script.write_text(
            "#!/bin/sh\nprintf '%s' '{\"cases\":[{\"id\":\"ok\",\"passed\":\"yes\"}]}'"
            " > \"$2\"\n")
        record = grade_run(self.home, self.run_dir("b1-task-c1-fixture-a1"), PROTOCOL)
        self.assertIsNone(record["score"])
        self.assertIn("malformed", record["setup_error"])

    def test_malformed_oracle_top_level_is_setup_error(self):
        script = self.home / "packs" / "task" / "c1" / "oracle" / "check.sh"
        script.write_text("#!/bin/sh\nprintf '%s' '{\"cases\":{}}' > \"$2\"\n")
        record = grade_run(self.home, self.run_dir("b1-task-c1-fixture-a1"), PROTOCOL)
        self.assertIsNone(record["score"])
        self.assertIn("unreadable", record["setup_error"])

    def test_file_item_suffix_needs_unique_source_match(self):
        source = self.home / "packs" / "scout" / "s1" / "source"
        for name in ("apps/a/src/index.ts", "apps/b/src/index.ts",
                     "apps/a/src/unique.ts"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        files = grade_module._source_files(source.parent)
        self.assertEqual(len(files), 3)
        self.assertTrue(grade_module._file_covered(
            "apps/a/src/unique.ts", "see src/unique.ts", files))
        self.assertFalse(grade_module._file_covered(
            "apps/a/src/index.ts", "see src/index.ts", files))
        self.assertTrue(grade_module._file_covered(
            "apps/a/src/index.ts", "see apps/a/src/index.ts", files))


        run = self.run_dir("b1-task-c3-fixture-a1")
        with self.assertRaises(ValueError):
            grade_run(self.home, run, PROTOCOL)

    def test_scout_checklist_and_scrub(self):
        seen = {}

        def runner(prompt, model, effort):
            seen["prompt"] = prompt
            seen["model"] = model
            return json.dumps(fake_verdicts(prompt, []))

        run = self.run_dir("b1-scout-s1-fixture-a1")
        # grade_run has no runner hook: patch the judge module's default.
        real = judge_module._run_omp
        judge_module._run_omp = runner
        try:
            record = grade_run(self.home, run, PROTOCOL, judge=JUDGE)
        finally:
            judge_module._run_omp = real

        self.assertEqual(record["kind"], "checklist")
        self.assertEqual((record["passed"], record["total"]), (3, 4))
        self.assertAlmostEqual(record["score"], 0.75)
        self.assertIsNone(record["baseline_passed"])
        self.assertTrue(record["critical"])
        self.assertEqual(record["judge"]["model"], "anthropic/claude-opus-5-5")
        self.assertEqual(record["judge"]["rubric_version"], "scout-checklist-1")
        self.assertEqual(len(record["judge"]["prompt_sha256"]), 64)
        self.assertEqual(seen["model"], "anthropic/claude-opus-5-5")
        self.assertNotIn("Claude", seen["prompt"])
        self.assertNotIn("GPT", seen["prompt"])
        self.assertNotIn("Opus", seen["prompt"])
        verdicts = {i["id"]: i["verdict"] for i in record["items"]}
        self.assertEqual(verdicts, {"f1": "covered", "y1": "covered",
                                    "b1": "covered", "b2": "contradicted"})


class JudgeTest(unittest.TestCase):
    facts = [{"id": "b1", "statement": "one"},
             {"id": "b2", "statement": "two"}]

    def test_retry_then_success(self):
        replies = iter(["not json at all", json.dumps(
            {"verdicts": [{"id": "b1", "verdict": "covered", "evidence": "x"},
                          {"id": "b2", "verdict": "missing", "evidence": ""}]})])
        result = judge_module.judge_facts("report", self.facts,
                                          {"model": "m", "effort": "low"},
                                          runner=lambda p, m, e: next(replies))
        self.assertEqual([(v["id"], v["verdict"]) for v in result],
                         [("b1", "covered"), ("b2", "missing")])

    def test_gives_up_after_retry(self):
        with self.assertRaises(RuntimeError):
            judge_module.judge_facts("report", self.facts,
                                     {"model": "m", "effort": "low"},
                                     runner=lambda p, m, e: "still not json")

    def test_parses_omp_event_stream(self):
        answer = json.dumps({"verdicts": [
            {"id": "b1", "verdict": "covered", "evidence": ""},
            {"id": "b2", "verdict": "contradicted", "evidence": ""}]})

        def runner(prompt, model, effort):
            event = {"type": "message_end",
                     "message": {"role": "assistant",
                                 "content": [{"type": "text", "text": answer}]}}
            return json.dumps(event) + "\n"

        result = judge_module.judge_facts("r", self.facts,
                                          {"model": "m", "effort": "low"},
                                          runner=runner)
        self.assertEqual(result[1]["verdict"], "contradicted")

    def test_prompt_hash_is_stable_and_scrubbed(self):
        first = judge_module.build_prompt("Claude Opus wrote GPT code", self.facts)
        second = judge_module.build_prompt("Claude Opus wrote GPT code", self.facts)
        self.assertEqual(first, second)
        self.assertNotIn("Claude", first)
        self.assertNotIn("Opus", first)
        self.assertNotIn("GPT", first)
        self.assertEqual(len(judge_module.prompt_sha256("x", self.facts)), 64)

    def _run_with_events(self, provider: str, name: str, requested: str):
        seen = {}
        proc = types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "provider": provider, "model": name,
                        "content": [{"type": "text", "text": "{}"}]}}))
        real = judge_module.subprocess

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["overlay"] = Path(cmd[cmd.index("--config") + 1]).read_text()
            return proc

        judge_module.subprocess = types.SimpleNamespace(
            run=fake_run, TimeoutExpired=real.TimeoutExpired)
        try:
            judge_module._run_omp("prompt", requested, "medium")
        finally:
            judge_module.subprocess = real
        return seen

    def test_judge_disables_fallback_and_pins_model(self):
        seen = self._run_with_events("anthropic", "claude-opus-5-5",
                                     "anthropic/claude-opus-5-5")
        self.assertIn("modelFallback: false", seen["overlay"])
        self.assertIn("usageAwareFallback: false", seen["overlay"])
        self.assertIn("fallbackChains: {}", seen["overlay"])
        self.assertIn("--config", seen["cmd"])

    def test_judge_rejects_other_model(self):
        with self.assertRaises(RuntimeError):
            self._run_with_events("anthropic", "claude-sonnet-5-5",
                                  "anthropic/claude-opus-5-5")
        with self.assertRaises(RuntimeError):
            self._run_with_events("unknown", "role", "")

    def test_judge_requires_observed_model(self):
        seen = {}
        proc = types.SimpleNamespace(returncode=0, stderr="", stdout="")
        real = judge_module.subprocess
        judge_module.subprocess = types.SimpleNamespace(
            run=lambda cmd, **kwargs: proc, TimeoutExpired=real.TimeoutExpired)
        try:
            with self.assertRaises(RuntimeError):
                judge_module._run_omp("prompt", "anthropic/claude-opus-5-5", "medium")
        finally:
            judge_module.subprocess = real


if __name__ == "__main__":
    unittest.main()
