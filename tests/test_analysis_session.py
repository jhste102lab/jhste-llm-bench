"""Session summary: timing union, pricing, identity, error classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bench.session import classify_error, summarize

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
PROTOCOL = {
    "version": "v2",
    "web": {"model": "openai-codex/gpt-6-luna",
            "price_per_mtok": {"input": 0.1, "output": 0.5}},
}


def make_run(root: Path, worker_messages: list[dict],
             result: dict | None = None) -> Path:
    """A run dir with the pinned harness's head/worker record shape."""
    run = Path(root) / "run"
    sessions = run / "sessions"
    head_dir = sessions / "2026-10-20T00-00-00-000Z_head"
    head_dir.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"role": "task"}))
    task = "Fixture task text"
    frozen = {"agent": "task", "context": "Goal: fixture context", "task": task}
    head_records = [
        {"type": "session", "id": "head", "timestamp": "2026-10-20T00-00:01.000Z"},
        {"type": "message", "timestamp": "2026-10-20T00:00:02.000Z", "message": {
            "role": "assistant", "stopReason": "toolUse", "content": [
                {"type": "toolCall", "id": "call_1", "name": "task",
                 "arguments": {"i": "Delegating", "context": frozen["context"],
                               "tasks": [{"agent": "task", "name": "Alpha",
                                          "task": task}]}}]}},
        {"type": "custom", "customType": "tool_execution_start",
         "timestamp": "2026-10-20T00:00:02.001Z",
         "data": {"toolCallId": "call_1", "toolName": "task",
                  "startedAt": "2026-10-20T00:00:02.001Z"}},
        {"type": "message", "timestamp": "2026-10-20T00:00:03.000Z", "message": {
            "role": "toolResult", "toolCallId": "call_1", "toolName": "task",
            "content": [{"type": "text", "text": "<task-result/>"}],
            "details": {"results": [result or {"id": "Alpha", "agent": "task"}]}}},
        {"type": "message", "timestamp": "2026-10-20T00:00:04.000Z", "message": {
            "role": "assistant", "provider": "openai-codex", "model": "gpt-6.1-sol",
            "stopReason": "stop", "content": [{"type": "text", "text": "done"}]}},
    ]
    (sessions / "2026-10-20T00-00-00-000Z_head.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in head_records))
    worker_records = [
        {"type": "session", "id": "worker", "parentSession": "head",
         "timestamp": "2026-10-20T00:00:02.000Z"},
        {"type": "model_change", "model": "provider/model",
         "resolvedModelIsFallback": False,
         "timestamp": "2026-10-20T00:00:02.000Z"},
        {"type": "thinking_level_change", "thinkingLevel": "high",
         "timestamp": "2026-10-20T00:00:02.000Z"},
        {"type": "session_init", "agent": "task",
         "resolvedModel": "provider/model:high",
         "timestamp": "2026-10-20T00:00:02.000Z"},
        {"type": "message", "timestamp": "2026-10-20T00:00:02.000Z", "message": {
            "role": "user", "content": [{"type": "text", "text": task}]}},
    ]
    worker_records.extend(worker_messages)
    (head_dir / "Alpha.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in worker_records))
    return run


class SessionSummaryTest(unittest.TestCase):
    def test_basic_worker(self):
        summary = summarize(FIXTURES / "basic", PROTOCOL)
        self.assertEqual(summary["head"]["session"],
                         "sessions/2026-10-20T06-57-15-570Z_fixturehead.jsonl")
        self.assertAlmostEqual(summary["head"]["cost_usd"], 0.005, places=8)
        self.assertEqual(summary["head"]["delegations"], [{
            "agent": "task", "context": "Goal: fixture context",
            "task": "Fixture task text", "solution_space": None, "tasks_count": 1,
            "extra_fields": []}])
        self.assertEqual(summary["head"]["tool_calls"], [{"name": "task"}])
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
        self.assertIsNone(worker["terminal_error"])
        self.assertFalse(worker["runtime_aborted"])
        self.assertEqual(worker["final_text"], "Fixture report text")
        self.assertTrue(worker["completed"])

    def test_runtime_limit_abort_from_task_result(self) -> None:
        abort = ("Subagent runtime limit exceeded (task.maxRuntimeMs=1000) — the agent "
                 "is still live with its full context; message it to resume.")
        worker_message = {
            "type": "message", "timestamp": "2026-10-20T00:00:03.000Z",
            "message": {"role": "assistant", "provider": "provider", "model": "model",
                        "stopReason": "aborted", "errorMessage": "Request was aborted",
                        "duration": 900, "content": []}}
        result = {"id": "Alpha", "agent": "task", "aborted": True,
                  "abortReason": abort, "exitCode": 1,
                  "resolvedModel": "provider/model:high"}
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), [worker_message,
                                       {"type": "custom", "customType": "session_exit",
                                        "data": {"kind": "normal"}}], result)
            summary = summarize(run, PROTOCOL)
        worker = summary["worker"]
        self.assertTrue(worker["runtime_aborted"])
        self.assertEqual(worker["terminal_error"], {"kind": "timeout", "message": abort})
        self.assertEqual([e["kind"] for e in worker["errors"]], ["provider"])
        self.assertFalse(worker["completed"])

    def test_recovered_error_is_not_terminal(self) -> None:
        messages = [
            {"type": "message", "timestamp": "2026-10-20T00:00:03.000Z", "message": {
                "role": "assistant", "provider": "provider", "model": "model",
                "stopReason": "error", "errorMessage": "usage_limit_reached",
                "content": []}},
            {"type": "message", "timestamp": "2026-10-20T00:00:04.000Z", "message": {
                "role": "assistant", "provider": "provider", "model": "model",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "recovered report"}]}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run = make_run(Path(tmp), messages)
            summary = summarize(run, PROTOCOL)
        worker = summary["worker"]
        self.assertEqual([e["kind"] for e in worker["errors"]], ["quota"])
        self.assertIsNone(worker["terminal_error"])
        self.assertTrue(worker["completed"])
        self.assertEqual(worker["final_text"], "recovered report")

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
