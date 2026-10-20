"""Synthetic bench home, fake omp and stand-in runner modules for offline tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import types
from pathlib import Path

HEAD_MODEL = "openai-codex/gpt-6.1-sol"
HEAD_EFFORT = "high"
FROZEN = {
    "agent": "task",
    "context": "# Goal\nImplement the change described in the request.",
    "task": "Implement the requested change in the repository, then report what changed.",
}

FAKE_OMP = r'''#!/usr/bin/env python3
"""Fake omp used by the offline runner tests."""
import json, os, re, sys, uuid

if "--version" in sys.argv:
    print("omp/18.12.3")
    sys.exit(0)

def arg(name):
    return sys.argv[sys.argv.index(name) + 1]

session_dir = None
for i, a in enumerate(sys.argv):
    if a.startswith("--session-dir"):
        session_dir = sys.argv[i + 1]
    if a.startswith("--session-dir="):
        session_dir = a.split("=", 1)[1]
if not session_dir:
    sys.exit(3)
sessions = os.path.abspath(session_dir)
run_dir = os.path.dirname(sessions)
scenario = os.environ.get("FAKE_BENCH_SCENARIO", "completed")
prompt = ""
prompt_path = os.path.join(run_dir, "prompt.md")
with open(prompt_path, encoding="utf-8") as handle:
    prompt = handle.read()

delegation = dict(agent="task", context="freeze context", task="freeze assignment")
match = re.search(r"```json\n(.*?)\n```", prompt, re.S)
if match:
    payload = json.loads(match.group(1))
    entry = payload["tasks"][0]
    delegation = {"agent": entry["agent"], "context": payload["context"], "task": entry["task"]}

overlay = open(os.path.join(run_dir, "overlay.yml"), encoding="utf-8").read()
mo = re.search(r"agentModelOverrides:\n\s+(\w+): \"(.+):(\w+)\"", overlay)
role, model, effort = mo.group(1), mo.group(2), mo.group(3)

head_model, head_effort = "openai-codex/gpt-6.1-sol", "high"
for i, a in enumerate(sys.argv):
    if a == "--model":
        head_model = sys.argv[i + 1]
    if a == "--thinking":
        head_effort = sys.argv[i + 1]

fallback = scenario == "fallback"
worker_model = "anthropic/claude-opus-5-5" if fallback else model
prov, mdl = worker_model.split("/", 1)
if scenario == "mismatch":
    delegation["context"] = delegation["context"] + " (tampered)"
errors, completed = [], True
terminal_error, runtime_aborted = None, False
if scenario == "timeout":
    errors = [{"kind": "timeout", "message": "worker exceeded 45m"}]
    terminal_error = errors[0]
    completed = False
elif scenario == "quota":
    errors = [{"kind": "quota", "message": "usage limit reached"}]
    terminal_error = errors[0]
    completed = False
elif scenario == "runtime-abort":
    errors = [{"kind": "provider", "message": "Request was aborted"}]
    terminal_error = {"kind": "timeout",
                      "message": "Subagent runtime limit exceeded (task.maxRuntimeMs=1000)"}
    runtime_aborted = True
    completed = False

stamp = "2026-10-20T00-00-00-000Z_" + uuid.uuid4().hex
head_path = os.path.join(sessions, stamp + ".jsonl")
worker_dir = os.path.join(sessions, stamp)
os.makedirs(worker_dir, exist_ok=True)
cwd = os.path.join(run_dir, "repo")

def write(path, entries):
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")

task_args = {"i": "Delegating", "context": delegation["context"],
             "tasks": [{"agent": delegation["agent"], "task": delegation["task"]}]}
if scenario == "two-tasks":
    task_args["tasks"].append(dict(task_args["tasks"][0]))
if scenario == "task-extra":
    task_args["tasks"][0]["model"] = "annex/override:low"

head_entries = [
    {"type": "session", "version": 3, "id": uuid.uuid4().hex, "timestamp": stamp, "cwd": cwd},
    {"type": "model_change", "model": head_model, "resolvedModelIsFallback": False},
    {"type": "thinking_level_change", "thinkingLevel": head_effort, "configured": None},
]
for extra in (["bash"] if scenario == "head-bash" else
              ["todo"] if scenario == "head-todo" else []):
    head_entries += [
        {"type": "message", "message": {"role": "assistant", "provider": "openai-codex",
         "model": head_model.split("/", 1)[1], "content": [{"type": "toolCall",
         "id": "call_extra", "name": extra, "arguments": {"i": "extra"}}],
         "usage": {"cost": {"total": 0.001}}}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "call_extra",
         "toolName": extra, "content": [{"type": "text", "text": "ok"}]}},
    ]
head_entries += [
    {"type": "message", "message": {"role": "assistant", "provider": "openai-codex",
     "model": head_model.split("/", 1)[1], "content": [{"type": "toolCall", "id": "call_1",
     "name": "task", "arguments": task_args}],
     "usage": {"cost": {"total": 0.02}}}},
    {"type": "message", "message": {"role": "toolResult", "toolCallId": "call_1",
     "toolName": "task", "content": [{"type": "text", "text": "worker finished"}],
     "details": {"results": [{"id": "Worker", "agent": delegation["agent"],
                              "aborted": runtime_aborted,
                              "abortReason": (terminal_error or {}).get("message")}]}}},
    {"type": "message", "message": {"role": "assistant", "provider": "openai-codex",
     "model": head_model.split("/", 1)[1], "content": [{"type": "text", "text": "done"}],
     "usage": {"cost": {"total": 0.01}}}},
    {"type": "session_exit", "data": {"reason": "dispose", "kind": "normal"}},
]
write(head_path, head_entries)

worker_entries = [
    {"type": "session", "version": 3, "id": uuid.uuid4().hex, "timestamp": stamp,
     "cwd": cwd, "parentSession": head_path},
    {"type": "model_change", "model": worker_model, "resolvedModelIsFallback": fallback},
    {"type": "thinking_level_change", "thinkingLevel": effort, "configured": None},
    {"type": "session_init", "resolvedModel": model + ":" + effort},
    {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": delegation["task"]}]}},
    {"type": "message", "message": {"role": "assistant", "provider": prov, "model": mdl,
     "content": [{"type": "text", "text": "implemented the change"}],
     "usage": {"input": 100, "output": 50, "totalTokens": 150,
               "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.003}}}},
    {"type": "custom", "customType": "test_worker_state",
     "data": {"errors": errors, "completed": completed,
              "terminal_error": terminal_error, "runtime_aborted": runtime_aborted}},
    {"type": "custom", "customType": "session_exit", "data": {"reason": "dispose"}},
]
write(os.path.join(worker_dir, "Worker.jsonl"), worker_entries)
'''


def install_fake_omp(root: Path) -> Path:
    path = Path(root) / "fake-omp"
    path.write_text(FAKE_OMP)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _entries(path: Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def summarize(run_dir, protocol):  # noqa: ARG001 - contract signature
    run_dir = Path(run_dir)
    sessions = run_dir / "sessions"
    head_files = sorted(p for p in sessions.glob("*.jsonl") if p.is_file())
    if not head_files:
        return {"head": {"session": None, "cost_usd": 0.0, "tool_calls": [],
                         "delegations": [], "error": None},
                "worker": None}
    head_path = head_files[0]
    head_entries = _entries(head_path)
    cost = 0.0
    delegations = []
    tool_calls = []
    error = None
    for entry in head_entries:
        if entry.get("type") == "custom" and entry.get("customType") == "test_head_error":
            error = entry.get("data", {}).get("message")
        message = entry.get("message") or {}
        for part in message.get("content") or []:
            if part.get("type") != "toolCall":
                continue
            tool_calls.append({"name": part.get("name")})
            if part.get("name") != "task":
                continue
            args = part.get("arguments") or {}
            tasks = args.get("tasks")
            if isinstance(tasks, list):
                entry_task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
                allowed = {"agent", "task", "name", "i"}
                count = len(tasks)
            else:
                entry_task = args
                allowed = {"agent", "task", "name", "i", "context"}
                count = 1
            delegations.append({
                "agent": entry_task.get("agent"),
                "context": args.get("context"),
                "task": entry_task.get("task"),
                "tasks_count": count,
                "extra_fields": sorted(set(entry_task) - allowed),
            })
        usage = message.get("usage") or {}
        cost += ((usage.get("cost") or {}).get("total") or 0)

    worker = None
    worker_files = sorted((sessions / head_path.stem).glob("*.jsonl"))
    if worker_files:
        entries = _entries(worker_files[0])
        resolved = observed = thinking = None
        fallback = False
        state = {"errors": [], "completed": True, "terminal_error": None,
                 "runtime_aborted": False}
        text = ""
        worker_cost = 0.0
        tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        for entry in entries:
            kind = entry.get("type")
            if kind == "session_init":
                resolved = entry.get("resolvedModel")
            elif kind == "model_change":
                fallback = fallback or bool(entry.get("resolvedModelIsFallback"))
            elif kind == "thinking_level_change":
                thinking = entry.get("thinkingLevel")
            elif kind == "custom" and entry.get("customType") == "test_worker_state":
                state = entry.get("data") or state
            message = entry.get("message") or {}
            if message.get("role") == "assistant":
                observed = f"{message.get('provider')}/{message.get('model')}"
                usage = message.get("usage") or {}
                worker_cost += ((usage.get("cost") or {}).get("total") or 0)
                for key in tokens:
                    tokens[key] += int(usage.get(key) or 0)
                for part in message.get("content") or []:
                    if part.get("type") == "text":
                        text = part.get("text") or text
        worker = {
            "session": str(worker_files[0].relative_to(run_dir)),
            "agent": "task", "resolved_model": resolved,
            "observed_models": [observed] if observed else [],
            "fallback": fallback, "thinking_level": thinking,
            "requests": 1, "model_seconds": 1.0, "tool_seconds": 0.5,
            "wall_seconds": 2.0, "cost_usd": worker_cost,
            "cost_breakdown": {"llm": worker_cost, "web": 0.0}, "tokens": tokens,
            "web": {"searches": 0, "fetches": 0, "input_tokens": 0, "total_tokens": 0},
            "errors": state.get("errors") or [],
            "terminal_error": state.get("terminal_error"),
            "runtime_aborted": bool(state.get("runtime_aborted")),
            "final_text": text,
            "completed": bool(state.get("completed")),
        }

    return {"head": {"session": str(head_path.relative_to(run_dir)),
                     "cost_usd": round(cost, 6), "tool_calls": tool_calls,
                     "delegations": delegations, "error": error},
            "worker": worker}


def audit(run_dir, rules):  # noqa: ARG001
    return {"blocked": 0, "accessed": [], "contaminated": False, "events": []}


def grade_run(home, run_dir, protocol, judge=None):  # noqa: ARG001
    from pathlib import Path as _Path
    run_dir = _Path(run_dir)
    record = json.loads((run_dir / "run.json").read_text())
    score = float(os.environ.get("FAKE_BENCH_SCORE", "0.5"))
    grade = {"run_id": record["run_id"], "batch_id": record["batch_id"],
             "role": record["role"], "pack": record["pack"], "kind": "oracle",
             "score": score, "passed": int(score * 2), "total": 2, "critical": False,
             "items": [], "judge": judge, "graded_at": "2026-10-20T00:00:00Z",
             "grader_version": "1"}
    with (_Path(home) / "grades.jsonl").open("a") as handle:
        handle.write(json.dumps(grade) + "\n")
    return grade


def install_modules() -> None:
    """Register stand-ins for the Analysis-owned modules."""
    for name, funcs in (("session", {"summarize": summarize}),
                        ("audit", {"audit": audit}),
                        ("grade", {"grade_run": grade_run})):
        module = types.ModuleType(f"bench.{name}")
        for attr, value in funcs.items():
            setattr(module, attr, value)
        sys.modules[f"bench.{name}"] = module


def remove_modules() -> None:
    for name in ("session", "audit", "grade"):
        sys.modules.pop(f"bench.{name}", None)


def build_home(root: Path, *, delegation: dict | None = None,
               workspace: bool = True) -> dict:
    """A minimal HOME: one task pack, one shared deps install, private.json."""
    root = Path(root)
    home = root / "home"
    secret = root / "answer-zone"
    secret.mkdir(parents=True)
    (secret / "solution.js").write_text("// forbidden\n")

    nm = home / "deps" / "demo" / "node_modules"
    (nm / "lodash").mkdir(parents=True)
    (nm / "lodash" / "index.js").write_text("module.exports = {};\n")
    (nm / "@acme" / "lib").mkdir(parents=True)
    (nm / "@acme" / "lib" / "index.js").write_text("// shared copy\n")
    (home / "deps" / "demo" / "package-lock.json").write_text('{"lockfileVersion": 3}\n')

    source = home / "packs" / "task" / "c1" / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.js").write_text("export const value = 1;\n")
    packages = {"name": "demo-app", "productive": True}
    if workspace:
        (source / "packages" / "lib" / "src").mkdir(parents=True)
        (source / "packages" / "lib" / "package.json").write_text('{"name": "@acme/lib"}\n')
        (source / "packages" / "lib" / "src" / "index.js").write_text("export const x = 1;\n")
        packages["workspaces"] = ["packages/*"]
    (source / "package.json").write_text(json.dumps(packages) + "\n")

    frozen = dict(delegation or FROZEN)
    frozen["sha256"] = delegation_sha256(frozen)
    frozen["head"] = {"model": HEAD_MODEL, "effort": HEAD_EFFORT}
    frozen["frozen_at"] = "2026-10-20T00:00:00Z"
    pack = {
        "id": "c1", "role": "task", "label": {"ko": "테스트 팩"}, "deps": "demo",
        "base_ref": "0" * 40, "source_repo": "demo", "reference_ref": "1" * 40,
        "timeout_minutes": 1, "cases": 2, "critical": [],
    }
    pack_dir = home / "packs" / "task" / "c1"
    (pack_dir / "pack.json").write_text(json.dumps(pack) + "\n")
    (pack_dir / "request.md").write_text(
        "Change the aggregation policy and keep the public API stable.\n")
    (pack_dir / "delegation.json").write_text(json.dumps(frozen) + "\n")
    (home / "runs").mkdir(parents=True)

    private = {
        "deps": {"demo": "deps/demo/node_modules"},
        "source_repos": {"demo": str(root / "source-repo")},
        "forbidden_paths": [{"category": "answer", "path": str(secret)},
                            {"category": "credential", "path": str(root / "missing-creds")}],
        "forbidden_domains": [{"category": "production", "pattern": "example-production"}],
        "private_terms": ["secretterm", "example-production"],
    }
    (home / "private.json").write_text(json.dumps(private) + "\n")
    return {"home": home, "secret": secret, "private": private,
            "pack_dir": pack_dir, "frozen": frozen, "source": source}


def delegation_sha256(delegation: dict) -> str:
    import hashlib
    body = json.dumps({f: delegation[f] for f in ("agent", "context", "task")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()
