"""Forbidden-access rules: single source of truth for sandbox and audit.

``Rule = {"category": str, "kind": "path" | "domain", "value": str}``.
Path values are absolute (``~`` expanded). Other benchmarks' runs are expanded
one rule per run directory so every rule stays a plain prefix/literal match.
"""

from __future__ import annotations

import os
from pathlib import Path

CATEGORIES = (
    "answer", "history", "credential", "production", "private-repo",
    "other-run", "evaluator",
)


def _expand(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def _variants(path: Path) -> list[Path]:
    """macOS aliases: /tmp and /private/tmp, /var and /private/var."""
    out = [path]
    text = str(path)
    if text == "/tmp":
        out.append(Path("/private/tmp"))
    elif text.startswith("/tmp/"):
        out.append(Path("/private" + text))
    elif text == "/private/tmp":
        out.append(Path("/tmp"))
    elif text.startswith("/private/tmp/"):
        out.append(Path("/tmp/" + text[len("/private/tmp/"):]))
    for candidate in list(out):
        real = Path(os.path.realpath(candidate))
        if real not in out:
            out.append(real)
    return out


def _other_runs(home: Path, run_dir: Path) -> list[Path]:
    runs_root = home / "runs"
    if not runs_root.is_dir():
        return []
    mine = run_dir.resolve()
    found: list[Path] = []
    for batch in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run in sorted(p for p in batch.iterdir() if p.is_dir()):
            if run.resolve() == mine:
                continue
            if not ((run / "run.json").is_file() or (run / "repo").is_dir()):
                continue
            found.append(run)
    return found


def forbidden_rules(home: Path, run_dir: Path, private: dict) -> list[dict]:
    home = Path(home)
    run_dir = Path(run_dir)
    rules: list[dict] = []

    def add(category: str, kind: str, value: str) -> None:
        rule = {"category": category, "kind": kind, "value": value}
        if rule not in rules:
            rules.append(rule)

    for entry in private.get("forbidden_paths") or []:
        category = entry.get("category") or "other"
        path = _expand(entry.get("path") or "")
        for variant in _variants(path):
            add(category, "path", str(variant))

    for entry in private.get("forbidden_domains") or []:
        add(entry.get("category") or "other", "domain", str(entry.get("pattern") or ""))

    for category, path in (("evaluator", home / "packs"),
                           ("evaluator", home / "grades.jsonl")):
        if path.resolve() == run_dir.resolve():
            continue
        add(category, "path", str(path.resolve()))

    for run in _other_runs(home, run_dir):
        add("other-run", "path", str(run.resolve()))

    return rules


def path_rules(rules: list[dict]) -> list[dict]:
    return [r for r in rules if r.get("kind") == "path"]


def domain_rules(rules: list[dict]) -> list[dict]:
    return [r for r in rules if r.get("kind") == "domain"]
