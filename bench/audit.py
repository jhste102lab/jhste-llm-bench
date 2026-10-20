"""Post-run access audit over every head and worker tool call.

Blocked attempts (sandbox EPERM) are counted but never contaminate a run.
A forbidden path or domain that was actually reached marks the run
``contaminated``: it keeps its score, loses its rank.

Rules come from :func:`bench.rules.forbidden_rules`; this module only matches.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .session import read_records

PATH_TOOLS = ("read", "grep", "glob", "edit", "write", "ast_edit")
BLOCK_MARKERS = ("operation not permitted", "eperm", "not permitted")

_URL_RE = re.compile(r"https?://[^\s'\"`<>()\[\]{}]+", re.IGNORECASE)
_TILDE_RE = re.compile(r"(?:^|[\s'\"`=(;|&])~[\w.@%+/-]*")
_ABS_RE = re.compile(r"(?:^|[\s'\"`=(;|&])/(?:[\w.@%+-]+/)*[\w.@%+-]+")
_SELECTOR_RE = re.compile(r":(?:\d[\d,+-]*|raw|img)\b.*$")
_PUNCT = ".,;:!?)]}\"'`"


# --------------------------------------------------------------------------
# call collection
# --------------------------------------------------------------------------

def _calls(records: list[dict]) -> list[dict]:
    """One entry per tool call, joining assistant arguments, start and result.

    The harness records the authoritative arguments on the assistant
    ``toolCall`` block; ``tool_execution_start`` is only a fallback for
    sessions whose custom record happens to carry them.
    """
    calls: dict[str, dict] = {}
    order: list[str] = []

    def ensure(call_id: str, tool: str | None) -> dict:
        if call_id not in calls:
            calls[call_id] = {"id": call_id, "tool": tool, "args": {}, "result": None}
            order.append(call_id)
        elif tool and not calls[call_id]["tool"]:
            calls[call_id]["tool"] = tool
        return calls[call_id]

    for record in records:
        kind = record.get("type")
        if kind == "message":
            message = record.get("message") or {}
            role = message.get("role")
            if role == "assistant":
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    call_id = block.get("id") or block.get("toolCallId")
                    if not call_id:
                        continue
                    call = ensure(call_id, block.get("name"))
                    args = block.get("arguments") or block.get("args")
                    if isinstance(args, dict) and args:
                        call["args"] = args
                continue
            if role != "toolResult":
                continue
            call_id = message.get("toolCallId")
            if not call_id:
                continue
            ensure(call_id, message.get("toolName"))["result"] = message
        elif kind == "custom" and record.get("customType") == "tool_execution_start":
            data = record.get("data") or {}
            call_id = data.get("toolCallId")
            if not call_id:
                continue
            call = ensure(call_id, data.get("toolName"))
            args = data.get("args")
            if isinstance(args, dict) and args and not call["args"]:
                call["args"] = args
    return [calls[cid] for cid in order]


def _result_text(result: dict | None) -> str:
    text = []
    for block in (result or {}).get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text.append(block["text"])
    return "\n".join(text)


def _denied_line(text: str, targets: list[str]) -> bool:
    """A denial marker on a line that names this specific target."""
    for line in text.splitlines():
        lowered = line.lower()
        if not any(marker in lowered for marker in BLOCK_MARKERS):
            continue
        if any(target and target in line for target in targets):
            return True
    return False


def _blocked(tool: str, kind: str, raw: str, target: str,
             result: dict | None) -> bool:
    """Whether one specific access was stopped by the sandbox.

    Path tools report failure structurally (``isError``). Free-text tools only
    count when the denial line names the target, so a worker cannot print
    ``EPERM`` next to a successful access. Domains are never sandboxed (the
    network is open), so a domain hit is blocked only when a read-tool URL
    fetch failed.
    """
    if result is None:
        # No result at all: the call never produced evidence of success.
        return True
    if kind == "url":
        return bool(result.get("isError")) and tool in PATH_TOOLS
    if tool in PATH_TOOLS:
        return bool(result.get("isError"))
    expanded = os.path.expanduser(raw) if isinstance(raw, str) else raw
    return _denied_line(_result_text(result), [target, raw, expanded])


# --------------------------------------------------------------------------
# target extraction
# --------------------------------------------------------------------------

def _clean(raw: str) -> str:
    value = raw.strip()
    while value and value[-1] in _PUNCT:
        value = value[:-1]
    return value


def _path_value(raw: str) -> str:
    value = _clean(raw)
    value = _SELECTOR_RE.sub("", value)
    # A glob target under a forbidden tree is still access to that tree; drop
    # glob segments so the prefix match sees the concrete ancestors.
    segments = value.split("/")
    parts = []
    for part in segments:
        if "*" in part or "?" in part or "[" in part:
            break
        parts.append(part)
    return "/".join(parts) or "/" if len(parts) != len(segments) else value


def _args_paths(tool: str, args: dict) -> list[str]:
    values: list[str] = []
    for key in ("path", "paths", "file", "files"):
        raw = args.get(key)
        if isinstance(raw, str):
            values.append(raw)
        elif isinstance(raw, list):
            values.extend(v for v in raw if isinstance(v, str))
    out: list[str] = []
    for value in values:
        for piece in value.split(";"):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _text_targets(text: str) -> tuple[list[str], list[str]]:
    """Absolute/``~`` paths and URLs mentioned in free text (bash/eval)."""
    urls = [_clean(u) for u in _URL_RE.findall(text)]
    stripped = _URL_RE.sub(" ", text)
    paths = []
    for pattern in (_TILDE_RE, _ABS_RE):
        for match in pattern.finditer(stripped):
            raw = match.group(0)
            # Drop the separator character the pattern consumed.
            raw = raw.lstrip(" \t'\"`=(;|&")
            if raw:
                paths.append(_clean(raw))
    return paths, urls


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def _expand(raw: str) -> str:
    return os.path.normpath(os.path.expanduser(raw))


def _allowed_roots(run_dir: Path) -> list[str]:
    # Reading inside the run repo or the shared deps install is never access.
    roots = [_expand(str(Path(run_dir) / "repo"))]
    # HOME/runs/<batch>/<run> -> HOME; deps are shared ordinary dependencies.
    parts = Path(run_dir).parts
    if len(parts) >= 5 and parts[-3] == "runs":
        roots.append(_expand(str(Path(run_dir).parents[2] / "deps")))
    return roots


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _resolve(raw: str, repo_dir: Path) -> str:
    value = os.path.expanduser(raw.strip())
    if value.startswith(("http://", "https://")):
        return value
    if not os.path.isabs(value):
        value = str(Path(repo_dir) / value)
    return _expand(value)


def _domain_target(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    return (parts.netloc + (parts.path or "")).lower()


def _path_matches(path: str, rules: list[dict], allowed: list[str]) -> list[dict]:
    if any(_under(path, root) for root in allowed):
        return []
    hits = []
    for rule in rules:
        if rule.get("kind") != "path":
            continue
        value = _expand(str(rule.get("value") or ""))
        if not value:
            continue
        if path == value or path.startswith(value + os.sep):
            hits.append(rule)
    return hits


def _domain_matches(url: str, rules: list[dict]) -> list[dict]:
    target = _domain_target(url)
    if not target:
        return []
    hits = []
    for rule in rules:
        if rule.get("kind") != "domain":
            continue
        value = str(rule.get("value") or "").lower()
        if value and value in target:
            hits.append(rule)
    return hits


def _targets(call: dict) -> list[tuple[str, str]]:
    """``(kind, value)`` targets of one tool call."""
    tool = call.get("tool") or ""
    args = call.get("args") or {}
    out: list[tuple[str, str]] = []
    if tool in PATH_TOOLS:
        for raw in _args_paths(tool, args):
            if raw.lower().startswith(("http://", "https://")):
                out.append(("url", _clean(raw)))
            else:
                out.append(("path", _path_value(raw)))
    elif tool == "bash":
        paths, urls = _text_targets(str(args.get("command") or ""))
        out.extend(("path", p) for p in paths)
        out.extend(("url", u) for u in urls)
    elif tool == "eval":
        paths, urls = _text_targets(str(args.get("code") or ""))
        out.extend(("path", p) for p in paths)
        out.extend(("url", u) for u in urls)
    return out


def audit(run_dir: Path, rules: list[dict]) -> dict:
    """Audit every tool call of the head and worker sessions of a run."""
    run_dir = Path(run_dir)
    sessions = run_dir / "sessions"
    records: list[dict] = []
    if sessions.is_dir():
        for path in sorted(sessions.rglob("*.jsonl")):
            records.extend(read_records(path))

    repo_dir = run_dir / "repo"
    allowed = _allowed_roots(run_dir)
    events: list[dict] = []
    accessed: dict[str, int] = {}

    for call in _calls(records):
        tool = call.get("tool") or ""
        seen_categories: set[str] = set()
        for kind, value in _targets(call):
            resolved = value
            if kind == "url":
                matches = _domain_matches(value, rules)
                target = value
            else:
                resolved = _resolve(value, repo_dir)
                if resolved.startswith(("http://", "https://")):
                    kind = "url"
                    matches = _domain_matches(resolved, rules)
                else:
                    matches = _path_matches(resolved, rules, allowed)
                target = resolved
            if not matches:
                continue
            blocked = _blocked(tool, kind, value, target, call.get("result"))
            for rule in matches:
                category = rule.get("category") or "other"
                if category in seen_categories:
                    continue
                seen_categories.add(category)
                events.append({"tool": tool, "category": category,
                               "blocked": blocked, "target": target})
                if not blocked:
                    accessed[category] = accessed.get(category, 0) + 1

    events.sort(key=lambda e: (str(e["tool"]), e["category"], e["target"]))
    return {
        "blocked": sum(1 for e in events if e["blocked"]),
        "accessed": [{"category": c, "count": n}
                     for c, n in sorted(accessed.items())],
        "contaminated": any(not e["blocked"] for e in events),
        "events": events,
    }
