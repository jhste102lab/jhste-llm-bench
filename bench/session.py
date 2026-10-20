"""Session summarizer: head delegation, worker timing/cost/identity.

Reads the omp JSONL session files a run leaves in ``<run>/sessions``:

* the head session is a file directly under ``sessions/``;
* the worker subagent session lives in ``sessions/<head-stem>/<Name>.jsonl``
  and is identified by ``session_init.agent``.

Only worker metrics are published; the head block exists so a run can be
diagnosed (delegation text, head cost, head error).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MESSAGE_TYPES = ("message",)


# --------------------------------------------------------------------------
# record helpers
# --------------------------------------------------------------------------

def read_records(path: Path) -> list[dict]:
    """Parse a JSONL session file, skipping unparsable lines."""
    records: list[dict] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _ms_from_iso(value) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _ms_from_value(value) -> int | None:
    """Epoch milliseconds from a numeric field or an ISO timestamp."""
    if isinstance(value, (int, float)):
        return int(value)
    return _ms_from_iso(value)


def _record_ms(record: dict) -> int | None:
    message = record.get("message")
    if isinstance(message, dict):
        stamp = _ms_from_value(message.get("completedAt"))
        if stamp is not None:
            return stamp
        stamp = _ms_from_value(message.get("timestamp"))
        if stamp is not None:
            return stamp
    return _ms_from_iso(record.get("timestamp"))


def text_of(message: dict) -> str:
    """Concatenated text blocks of an omp message."""
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _tool_calls(records: list[dict]) -> list[dict]:
    """Assistant tool calls with their arguments, in session order."""
    calls = []
    for record in records:
        if record.get("type") not in MESSAGE_TYPES:
            continue
        message = record.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "toolCall":
                calls.append({
                    "name": block.get("name"),
                    "arguments": block.get("arguments") or {},
                })
    return calls


# --------------------------------------------------------------------------
# session discovery
# --------------------------------------------------------------------------

def _sessions_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "sessions"


def _head_candidates(sessions: Path) -> list[Path]:
    if not sessions.is_dir():
        return []
    return [p for p in sessions.iterdir()
            if p.is_file() and p.suffix == ".jsonl" and not p.name.startswith(".")]


def _head_session(sessions: Path, records_by_path: dict[Path, list[dict]]) -> Path | None:
    candidates = _head_candidates(sessions)
    if not candidates:
        return None

    def has_parent(path: Path) -> bool:
        for record in records_by_path.get(path, []):
            if record.get("type") == "session" and record.get("parentSession"):
                return True
        return False

    roots = [p for p in candidates if not has_parent(p)] or candidates
    # A rerun can leave more than one head file; the task tool call is the
    # decisive marker, then mtime.
    with_task = [p for p in roots if any(c["name"] == "task" for c in _tool_calls(records_by_path.get(p, [])))]
    pool = with_task or roots
    return max(pool, key=lambda p: p.stat().st_mtime)


def _worker_sessions(sessions: Path, head: Path | None) -> list[Path]:
    """Subagent session files, preferring the head session's own directory."""
    if head is not None:
        own = sessions / head.stem
        if own.is_dir():
            found = sorted(own.glob("*.jsonl"))
            if found:
                return found
    found: list[Path] = []
    if sessions.is_dir():
        for child in sorted(p for p in sessions.iterdir() if p.is_dir()):
            found.extend(sorted(child.glob("*.jsonl")))
    return found


def _session_init(records: list[dict]) -> dict:
    for record in records:
        if record.get("type") == "session_init":
            return record
    return {}


# --------------------------------------------------------------------------
# head
# --------------------------------------------------------------------------

def _delegations(calls: list[dict]) -> list[dict]:
    delegations = []
    for call in calls:
        if call["name"] != "task":
            continue
        arguments = call["arguments"] if isinstance(call["arguments"], dict) else {}
        agent = arguments.get("agent")
        task = arguments.get("task")
        tasks = arguments.get("tasks")
        if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
            agent = tasks[0].get("agent", agent)
            task = tasks[0].get("task", task)
        delegations.append({
            "agent": agent,
            "context": arguments.get("context"),
            "task": task,
        })
    return delegations


def _assistant_messages(records: list[dict]) -> list[dict]:
    out = []
    for record in records:
        if record.get("type") not in MESSAGE_TYPES:
            continue
        message = record.get("message") or {}
        if message.get("role") == "assistant":
            out.append(message)
    return out


def _head_block(sessions: Path, head: Path | None, records: list[dict]) -> dict:
    if head is None:
        return {"session": None, "cost_usd": 0.0, "delegations": [], "error": None}
    cost = 0.0
    error = None
    for message in _assistant_messages(records):
        usage = message.get("usage") or {}
        cost += float((usage.get("cost") or {}).get("total") or 0.0)
        if message.get("errorMessage") or message.get("stopReason") in ("error", "aborted"):
            error = message.get("errorMessage") or message.get("stopReason")
    return {
        "session": str(head.relative_to(sessions.parent)),
        "cost_usd": round(cost, 8),
        "delegations": _delegations(_tool_calls(records)),
        "error": error,
    }


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------

QUOTA_MARKERS = ("usage limit", "usage_limit", "429", "rate limit", "ratelimit",
                 "quota", "too many requests", "capacity")
TIMEOUT_MARKERS = ("deadline exceeded", "timed out", "timeout")


def classify_error(message: str) -> str:
    lowered = (message or "").lower()
    if any(marker in lowered for marker in TIMEOUT_MARKERS):
        return "timeout"
    if any(marker in lowered for marker in QUOTA_MARKERS):
        return "quota"
    return "provider"


def _union_seconds(intervals: list[tuple[int, int]]) -> float:
    if not intervals:
        return 0.0
    total = 0
    current_start, current_end = None, None
    for start, end in sorted(intervals):
        if end < start:
            start, end = end, start
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return total / 1000.0


def _tool_intervals(records: list[dict]) -> list[tuple[int, int]]:
    starts: dict[str, int] = {}
    results: dict[str, int] = {}
    order: list[str] = []
    session_end = None
    for record in records:
        stamp = _record_ms(record)
        if stamp is not None:
            session_end = stamp if session_end is None else max(session_end, stamp)
        if record.get("type") == "custom" and record.get("customType") == "tool_execution_start":
            data = record.get("data") or {}
            call_id = data.get("toolCallId")
            started = _ms_from_iso(data.get("startedAt")) or _ms_from_iso(record.get("timestamp"))
            if call_id and started is not None:
                starts[call_id] = started
                order.append(call_id)
        elif record.get("type") == "message":
            message = record.get("message") or {}
            if message.get("role") == "toolResult":
                call_id = message.get("toolCallId")
                stamp = _ms_from_value(message.get("timestamp")) or _ms_from_iso(record.get("timestamp"))
                if call_id and stamp is not None and call_id not in results:
                    results[call_id] = stamp
    intervals = []
    for call_id in order:
        start = starts[call_id]
        end = results.get(call_id)
        if end is None:
            end = session_end if session_end is not None else start
        intervals.append((start, end))
    return intervals


def _web_usage(records: list[dict]) -> dict:
    searches = 0
    fetches = 0
    input_tokens = 0
    total_tokens = 0
    for record in records:
        if record.get("type") == "custom" and record.get("customType") == "tool_execution_start":
            data = record.get("data") or {}
            if data.get("toolName") == "web_search":
                searches += 1
            elif data.get("toolName") == "read":
                target = (data.get("args") or {}).get("path")
                if isinstance(target, str) and target.lower().startswith(("http://", "https://")):
                    fetches += 1
        elif record.get("type") == "message":
            message = record.get("message") or {}
            if message.get("role") == "toolResult" and message.get("toolName") == "web_search":
                usage = ((message.get("details") or {}).get("response") or {}).get("usage") or {}
                input_tokens += int(usage.get("inputTokens") or 0)
                total_tokens += int(usage.get("totalTokens") or 0)
    return {"searches": searches, "fetches": fetches,
            "input_tokens": input_tokens, "total_tokens": total_tokens}


def web_cost(usage: dict, protocol: dict) -> float:
    """Price web tokens: ``input`` at the input rate, the rest at output."""
    price = ((protocol or {}).get("web") or {}).get("price_per_mtok") or {}
    input_rate = float(price.get("input") or 0.0)
    output_rate = float(price.get("output") or 0.0)
    input_tokens = int(usage.get("input_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    output_tokens = max(total_tokens - input_tokens, 0)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def _worker_records(path: Path) -> list[dict]:
    return read_records(path)


def _nested_records(worker_path: Path) -> list[dict]:
    """Sessions nested under a worker session, if any (cost only)."""
    nested = worker_path.parent / worker_path.stem
    if not nested.is_dir():
        return []
    records: list[dict] = []
    for child in sorted(nested.rglob("*.jsonl")):
        records.extend(read_records(child))
    return records


def _worker_block(run_dir: Path, path: Path, protocol: dict) -> dict:
    records = _worker_records(path)
    init = _session_init(records)
    assistants = _assistant_messages(records)

    resolved_model = init.get("resolvedModel")
    agent = init.get("agent")
    fallback = False
    thinking_level = None
    for record in records:
        if record.get("type") == "model_change":
            if record.get("resolvedModelIsFallback"):
                fallback = True
            if not resolved_model and record.get("model"):
                resolved_model = record.get("model")
        elif record.get("type") == "thinking_level_change":
            level = record.get("thinkingLevel")
            if level is not None:
                thinking_level = level

    model_seconds = 0.0
    llm_cost = 0.0
    tokens = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    observed: set[str] = set()
    errors: list[dict] = []
    final_text = ""
    last_stop = None

    for message in assistants:
        duration = message.get("duration")
        if isinstance(duration, (int, float)):
            model_seconds += float(duration) / 1000.0
        usage = message.get("usage") or {}
        llm_cost += float((usage.get("cost") or {}).get("total") or 0.0)
        for key in tokens:
            tokens[key] += int(usage.get(key) or 0)
        provider = message.get("provider")
        model = message.get("model")
        if model:
            observed.add(f"{provider}/{model}" if provider else model)
        text = text_of(message)
        if text.strip():
            final_text = text
        last_stop = message.get("stopReason")
        if message.get("errorMessage") or last_stop in ("error", "aborted"):
            detail = message.get("errorMessage") or str(last_stop)
            errors.append({"kind": classify_error(detail), "message": detail})

    nested = _nested_records(path)
    if nested:
        for message in _assistant_messages(nested):
            usage = message.get("usage") or {}
            llm_cost += float((usage.get("cost") or {}).get("total") or 0.0)
            for key in tokens:
                tokens[key] += int(usage.get(key) or 0)

    web = _web_usage(records)
    web_spend = web_cost(web, protocol)

    exit_kind = None
    for record in records:
        if record.get("type") == "custom" and record.get("customType") == "session_exit":
            exit_kind = (record.get("data") or {}).get("kind")

    wall_seconds = 0.0
    stamps = [ms for ms in (_record_ms(r) for r in records) if ms is not None]
    if stamps:
        wall_seconds = (max(stamps) - min(stamps)) / 1000.0

    completed = bool(exit_kind is None or exit_kind == "normal") and not errors and last_stop in (None, "stop")

    return {
        "session": str(path.relative_to(run_dir)),
        "agent": agent,
        "resolved_model": resolved_model,
        "observed_models": sorted(observed),
        "fallback": fallback,
        "thinking_level": thinking_level,
        "requests": len(assistants),
        "model_seconds": round(model_seconds, 3),
        "tool_seconds": round(_union_seconds(_tool_intervals(records)), 3),
        "wall_seconds": round(wall_seconds, 3),
        "cost_usd": round(llm_cost + web_spend, 8),
        "cost_breakdown": {"llm": round(llm_cost, 8), "web": round(web_spend, 8)},
        "tokens": tokens,
        "web": web,
        "errors": errors,
        "final_text": final_text,
        "completed": completed,
    }


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

def _run_role(run_dir: Path) -> str | None:
    try:
        data = json.loads((Path(run_dir) / "run.json").read_text())
    except (OSError, ValueError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return role if isinstance(role, str) else None


def summarize(run_dir: Path, protocol: dict) -> dict:
    """Summarize one run's head and worker sessions."""
    run_dir = Path(run_dir)
    sessions = _sessions_dir(run_dir)
    records_by_path = {p: read_records(p) for p in _head_candidates(sessions)}
    head = _head_session(sessions, records_by_path)
    head_records = records_by_path.get(head, []) if head else []
    head_block = _head_block(sessions, head, head_records)

    role = _run_role(run_dir)
    delegation_agent = next(
        (d.get("agent") for d in head_block["delegations"] if d.get("agent")),
        None,
    )
    expected = role or delegation_agent
    workers = _worker_sessions(sessions, head)

    worker = None
    if workers:
        chosen = workers[0]
        if expected:
            matched = [p for p in workers if _session_init(read_records(p)).get("agent") == expected]
            if matched:
                chosen = matched[0]
        worker = _worker_block(run_dir, chosen, protocol or {})

    return {"head": head_block, "worker": worker}
