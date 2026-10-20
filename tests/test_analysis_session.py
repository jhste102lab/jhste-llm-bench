"""Session summary: timing union, pricing, identity, error classification."""

from __future__ import annotations

import unittest
from pathlib import Path

from bench.session import classify_error, summarize

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
PROTOCOL = {
    "version": "v2",
    "web": {"model": "openai-codex/gpt-6-luna",
            "price_per_mtok": {"input": 0.1, "output": 0.5}},
}


class SessionSummaryTest(unittest.TestCase):
    def test_basic_worker(self):
        summary = summarize(FIXTURES / "basic", PROTOCOL)
        self.assertEqual(summary["head"]["session"],
                         "sessions/2026-10-20T06-57-15-570Z_fixturehead.jsonl")
        self.assertAlmostEqual(summary["head"]["cost_usd"], 0.005, places=8)
        self.assertEqual(summary["head"]["delegations"], [{
            "agent": "task", "context": "Goal: fixture context",
            "task": "Fixture task text"}])
        self.assertIsNone(summary["head"]["error"])

        worker = summary["worker"]
        self.assertEqual(worker["session"],
                         "sessions/2026-10-20T06-57-15-570Z_fixturehead/Alpha.jsonl")
        self.assertEqual(worker["agent"], "task")
        self.assertEqual(worker["resolved_model"], "openai-codex/gpt-6-luna:high")
        self.assertEqual(worker["observed_models"], ["openai-codex/gpt-6-luna"])
        self.assertFalse(worker["fallback"])
        self.assertEqual(worker["thinking_level"], "high")
        self.assertEqual(worker["requests"], 2)
        self.assertAlmostEqual(worker["model_seconds"], 1.5, places=3)
        # Two overlapping reads plus the web call: union 4s, not the 6.5s sum.
        self.assertAlmostEqual(worker["tool_seconds"], 4.0, places=3)
        self.assertAlmostEqual(worker["wall_seconds"], 5.113, places=3)
        self.assertAlmostEqual(worker["cost_breakdown"]["llm"], 0.003, places=8)
        self.assertAlmostEqual(worker["cost_breakdown"]["web"], 0.00035, places=8)
        self.assertAlmostEqual(worker["cost_usd"], 0.00335, places=8)
        self.assertEqual(worker["tokens"],
                         {"input": 150, "output": 15, "cacheRead": 20, "cacheWrite": 5})
        self.assertEqual(worker["web"], {"searches": 1, "fetches": 0,
                                         "input_tokens": 1000, "total_tokens": 1500})
        self.assertEqual(worker["errors"], [])
        self.assertEqual(worker["final_text"], "Fixture report text")
        self.assertTrue(worker["completed"])

    def test_quota_and_fallback(self):
        summary = summarize(FIXTURES / "quota", PROTOCOL)
        worker = summary["worker"]
        self.assertEqual(worker["agent"], "scout")
        self.assertTrue(worker["fallback"])
        self.assertEqual([e["kind"] for e in worker["errors"]],
                         ["quota", "timeout", "provider"])
        self.assertFalse(worker["completed"])
        self.assertEqual(worker["resolved_model"], "anthropic/claude-haiku-5-5:low")

    def test_no_worker_session(self):
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            shutil.copytree(FIXTURES / "basic", run)
            shutil.rmtree(run / "sessions" / "2026-10-20T06-57-15-570Z_fixturehead")
            summary = summarize(run, PROTOCOL)
            self.assertIsNone(summary["worker"])
            self.assertIsNotNone(summary["head"]["session"])

    def test_classify_error(self):
        self.assertEqual(classify_error("Codex error event: usage_limit_reached"), "quota")
        self.assertEqual(classify_error("HTTP 429 Too Many Requests"), "quota")
        self.assertEqual(classify_error("Deadline exceeded"), "timeout")
        self.assertEqual(classify_error("upstream reset"), "provider")


if __name__ == "__main__":
    unittest.main()
