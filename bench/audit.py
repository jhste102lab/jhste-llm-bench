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
    calls: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        if record.get("type") == "custom" and record.get("customType") == "tool_execution_start":
            data = record.get("data") or {}
            call_id = data.get("toolCallId")
            if not call_id:
                continue
            if call_id not in calls:
                calls[call_id] = {"id": call_id, "tool": data.get("toolName"),
                                  "args": data.get("args") or {}, "result": None}
                order.append(call_id)
            else:
                calls[call_id]["args"] = data.get("args") or calls[call_id]["args"]
        elif record.get("type") == "message":
            message = record.get("message") or {}
            if message.get("role") != "toolResult":
                continue
            call_id = message.get("toolCallId")
            if not call_id:
                continue
            if call_id not in calls:
                calls[call_id] = {"id": call_id, "tool": message.get("toolName"),
                                  "args": {}, "result": None}
                order.append(call_id)
            calls[call_id]["result"] = message
    return [calls[cid] for cid in order]


def _blocked(result: dict | None) -> bool:
    if result is None:
        # No result at all: the call never produced evidence of success.
        return True
    if result.get("isError"):
        return True
    text = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text.append(block["text"])
    lowered = "\n".join(text).lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


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
        blocked = _blocked(call.get("result"))
        seen_categories: set[str] = set()
        for kind, value in _targets(call):
            if kind == "url":
                matches = _domain_matches(value, rules)
                target = value
            else:
                resolved = _resolve(value, repo_dir)
                if resolved.startswith(("http://", "https://")):
                    matches = _domain_matches(resolved, rules)
                else:
                    matches = _path_matches(resolved, rules, allowed)
                target = resolved
            for rule in matches:
                category = rule.get("category") or "other"
                if category in seen_categories:
                    continue
                seen_categories.add(category)
                events.append({"tool": call.get("tool"), "category": category,
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
