"""Aggregation ranking and site export (allowlist + leak gate)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench.aggregate import cohorts
from bench.export import export

PROTOCOL = {"version": "v2", "small_diff_pp": 5}
SOL = "openai-codex/gpt-6.1-sol"
OPUS = "anthropic/claude-opus-5-5"
DS = "cline-pass/deepseek-v4.1-flash-1017"
HAIKU = "anthropic/claude-haiku-5-5"


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(json.dumps(data))


class Home:
    """Synthetic bench home: packs, batches, runs, grades."""

    def __init__(self, root: Path):
        self.root = root

    def pack(self, role: str, pack_id: str, label: str):
        write(self.root / "packs" / role / pack_id / "pack.json",
              {"id": pack_id, "role": role, "label": {"ko": label}})

    def batch(self, batch_id: str, quarantined: bool = False):
        write(self.root / "runs" / batch_id / "batch.json",
              {"batch_id": batch_id, "protocol": "v2", "quarantined": quarantined})

    def run(self, batch: str, run_id: str, role: str, pack: str, model: str,
            effort: str, *, contaminated=False, model_seconds=60.0,
            tool_seconds=40.0, cost=0.1, concurrency=4, web=1, blocked=0,
            access=None, outcome="completed"):
        base = self.root / "runs" / batch / run_id
        write(base / "run.json", {
            "run_id": run_id, "batch_id": batch, "protocol": "v2", "role": role,
            "pack": pack,
            "config": {"key": f"{model}:{effort}", "model": model, "effort": effort},
            "outcome": outcome, "concurrency": concurrency,
            "started_at": "2026-10-20T06:57:15Z", "ended_at": "2026-10-20T07:10:00Z",
        })
        write(base / "summary.json", {"worker": {
            "model_seconds": model_seconds, "tool_seconds": tool_seconds,
            "cost_usd": cost, "web": {"searches": web, "fetches": 0}}})
        write(base / "audit.json", {
            "blocked": blocked, "contaminated": contaminated,
            "accessed": access or []})

    def grade(self, run_id: str, role: str, pack: str, passed: int, total: int,
              *, critical=False, judge=None, score=None, baseline_passed=0):
        if score is None:
            if role == "task":
                target = total - baseline_passed
                fixed = max(passed - baseline_passed, 0)
                score = round(min(fixed / target, 1.0), 3) if target > 0 else None
            else:
                score = round(passed / total, 3)
        record = {
            "run_id": run_id, "batch_id": "b1", "role": role, "pack": pack,
            "kind": "oracle" if role == "task" else "checklist",
            "score": score,
            "passed": passed, "total": total, "critical": critical,
            "baseline_passed": baseline_passed if role == "task" else None,
            "items": [], "judge": judge,
            "graded_at": "2026-10-20T08:00:00Z", "grader_version": "1",
        }
        with (self.root / "grades.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")


class AggregateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        bench = Home(self.home)
        bench.pack("task", "c1", "과제 하나")
        bench.pack("task", "c2", "과제 둘")
        bench.pack("scout", "s1", "탐색 하나")
        bench.batch("b1")
        bench.batch("b2", quarantined=True)

        # sol: 70 (0.8 + 0.6) -> 70.0
        bench.run("b1", "r-sol-c1", "task", "c1", SOL, "high",
                  model_seconds=60, tool_seconds=40, cost=0.2, concurrency=4,
                  web=2, blocked=3, access=[{"category": "answer", "count": 1}])
        bench.run("b1", "r-sol-c2", "task", "c2", SOL, "high",
                  model_seconds=120, tool_seconds=80, cost=0.3, concurrency=2,
                  web=0)
        bench.grade("r-sol-c1", "task", "c1", 8, 10, baseline_passed=1)
        bench.grade("r-sol-c2", "task", "c2", 6, 10, baseline_passed=1)

        # opus: 68.0, within 5pp of sol -> close
        bench.run("b1", "r-opus-c1", "task", "c1", OPUS, "medium", cost=0.5)
        bench.run("b1", "r-opus-c2", "task", "c2", OPUS, "medium", cost=0.5)
        bench.grade("r-opus-c1", "task", "c1", 7, 10, baseline_passed=1)
        bench.grade("r-opus-c2", "task", "c2", 66, 100, baseline_passed=1)

        # ds: 60.0, far from opus -> no close flag
        bench.run("b1", "r-ds-c1", "task", "c1", DS, "max")
        bench.run("b1", "r-ds-c2", "task", "c2", DS, "max")
        bench.grade("r-ds-c1", "task", "c1", 7, 10, baseline_passed=1)
        bench.grade("r-ds-c2", "task", "c2", 5, 10, baseline_passed=1)

        # haiku: 90.0 but contaminated -> bottom, no rank
        bench.run("b1", "r-haiku-c1", "task", "c1", HAIKU, "high",
                  contaminated=True, cost=0.15)
        bench.run("b1", "r-haiku-c2", "task", "c2", HAIKU, "high",
                  contaminated=True, cost=0.15)
        bench.grade("r-haiku-c1", "task", "c1", 9, 10, baseline_passed=1)
        bench.grade("r-haiku-c2", "task", "c2", 9, 10, baseline_passed=1)

        # quarantined batch is excluded entirely
        bench.run("b2", "r-sonnet-c1", "task", "c1",
                  "anthropic/claude-sonnet-5-5", "high")
        bench.grade("r-sonnet-c1", "task", "c1", 10, 10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_ranking_and_flags(self):
        cohort = cohorts(self.home, PROTOCOL)[0]
        self.assertEqual((cohort["protocol"], cohort["role"], cohort["judge"]),
                         ("v2", "task", None))
        rows = cohort["rows"]
        self.assertEqual([r["key"] for r in rows],
                         [f"{SOL}:high", f"{OPUS}:medium", f"{DS}:max", f"{HAIKU}:high"])
        self.assertEqual([r["values"]["score"] for r in rows], [66.7, 66.2, 55.5, 88.9])
        self.assertTrue(rows[3]["flags"]["contaminated"])
        self.assertTrue(rows[1]["flags"]["close"])
        self.assertFalse(rows[2]["flags"]["close"])
        self.assertFalse(rows[0]["flags"]["close"])
        self.assertNotIn("sonnet", json.dumps(rows))

        sol = rows[0]
        self.assertEqual(sol["n"], 1)
        self.assertAlmostEqual(sol["values"]["cost"], 0.25, places=6)
        self.assertAlmostEqual(sol["values"]["time"], 150.0, places=3)
        # Normalised gain cell: fixed over target, not raw pass counts.
        self.assertEqual(sol["tasks"]["c1"]["score"], [7.0, 9.0])
        self.assertEqual(sol["detail"]["modelShare"], 0.6)
        self.assertEqual(sol["detail"]["toolShare"], 0.4)
        self.assertEqual(sol["detail"]["concurrency"], 4)
        self.assertEqual(sol["detail"]["web"], 2.0)
        self.assertEqual(sol["detail"]["blocked"], 3)
        self.assertEqual(sol["detail"]["access"], [{"category": "answer", "count": 1}])

    def test_judge_partition_for_scout(self):
        bench = Home(self.home)
        bench.run("b1", "r-scout", "scout", "s1", SOL, "medium")
        bench.grade("r-scout", "scout", "s1", 3, 4,
                    judge={"model": OPUS, "effort": "medium",
                           "rubric_version": "scout-checklist-1",
                           "prompt_sha256": "0" * 64})
        bench.grade("r-scout", "scout", "s1", 2, 4,
                    judge={"model": "anthropic/claude-sonnet-5-5", "effort": "medium",
                           "rubric_version": "scout-checklist-1",
                           "prompt_sha256": "1" * 64})
        scout = [c for c in cohorts(self.home, PROTOCOL) if c["role"] == "scout"]
        self.assertEqual([c["judge"] for c in scout], [OPUS, "anthropic/claude-sonnet-5-5"])
        self.assertEqual([c["rows"][0]["values"]["score"] for c in scout], [75.0, 50.0])

    def test_export_allowlist_and_empty_state(self):
        docs = Path(self.tmp.name) / "docs"
        target = export(self.home, docs, PROTOCOL)
        text = target.read_text()
        self.assertTrue(text.startswith("// Generated by python3 -m bench export."))
        self.assertIn("window.BENCH_V2 = ", text)
        payload = json.loads(text.split("window.BENCH_V2 = ", 1)[1].rstrip(";\n"))
        suite = payload["suites"]["task"][0]
        self.assertEqual(suite["id"], "v2-task")
        self.assertEqual(suite["label"], {"ko": "코딩 과제"})
        self.assertEqual(suite["date"], "2026.10.20")
        self.assertEqual(suite["tasks"], [
            {"id": "c1", "label": {"ko": "과제 하나"}},
            {"id": "c2", "label": {"ko": "과제 둘"}}])
        self.assertEqual([m["key"] for m in suite["scopes"]["worker"]["metrics"]],
                         ["score", "cost", "time"])
        rows = suite["scopes"]["worker"]["rows"]
        self.assertEqual([r["model"] for r in rows], ["sol", "opus", "ds", "haiku"])
        self.assertEqual(rows[0]["id"], "v2-task:sol:high")
        self.assertEqual(sorted(payload["models"]), ["ds", "haiku", "opus", "sol"])
        for secret in ("run_id", "sessions/", str(self.home), "batch_id"):
            self.assertNotIn(secret, text)
        self.assertEqual(payload["suites"]["scout"], [])

        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        payload = json.loads(export(empty, Path(self.tmp.name) / "docs2", PROTOCOL)
                             .read_text().split("window.BENCH_V2 = ", 1)[1].rstrip(";\n"))
        self.assertEqual(payload["suites"], {"task": [], "scout": []})
        self.assertEqual(payload["models"], {})

    def test_unknown_model_raises(self):
        bench = Home(self.home)
        bench.run("b1", "r-unknown", "task", "c1", "vendor/unknown-model", "high")
        bench.grade("r-unknown", "task", "c1", 1, 1)
        with self.assertRaises(ValueError):
            export(self.home, Path(self.tmp.name) / "docs", PROTOCOL)

    def test_export_leak_gate(self):
        write(self.home / "private.json", {"private_terms": ["secretterm"]})
        write(self.home / "packs" / "task" / "c1" / "pack.json",
              {"id": "c1", "role": "task", "label": {"ko": "secretterm 과제"}})
        docs = Path(self.tmp.name) / "docs"
        target = docs / "data" / "v2.js"
        write(target, "old\n")
        with self.assertRaises(RuntimeError):
            export(self.home, docs, PROTOCOL)
        self.assertEqual(target.read_text(), "old\n")


if __name__ == "__main__":
    unittest.main()
