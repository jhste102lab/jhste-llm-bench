"""Grading: oracle parsing, checklist + blinded judge, append-only records."""

from __future__ import annotations

import json
import shutil
import tempfile
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

    def tearDown(self):
        grade_module._link_deps = self._real_link
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

    def test_no_headroom_above_baseline_raises(self):
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


if __name__ == "__main__":
    unittest.main()
