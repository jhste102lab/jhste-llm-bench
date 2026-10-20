"""Run grading: oracle for task packs, checklist + blinded judge for scout.

Grades are append-only: one JSON line per grading in ``HOME/grades.jsonl``.
Re-grading appends a newer record, never overwrites.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import judge as judge_module
from . import session as session_module

GRADER_VERSION = "1"


# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------

def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _pack_dir(home: Path, role: str, pack_id: str) -> Path:
    try:
        from .home import pack_dir
        return Path(pack_dir(home, role, pack_id))
    except Exception:
        return Path(home) / "packs" / role / pack_id


def _deps_node_modules(home: Path, pack: dict) -> Path | None:
    name = pack.get("deps")
    if not name:
        return None
    try:
        from .home import deps_dir, load_private
        return Path(deps_dir(home, load_private(home), name))
    except BaseException:
        return Path(home) / "deps" / str(name) / "node_modules"


def _link_deps(repo: Path, node_modules: Path | None) -> None:
    """Indirection so tests need no real dependency install."""
    if node_modules is None:
        return
    from .deps import link_deps
    link_deps(repo, node_modules)


def _deps_dir(home: Path, pack: dict) -> Path | None:
    name = pack.get("deps")
    if not name:
        return None
    return Path(home) / "deps" / str(name)


def _rules(home: Path, run_dir: Path) -> list[dict]:
    """Indirection: tests grade without reading a private home."""
    from .home import load_private
    from .rules import forbidden_rules
    return forbidden_rules(home, run_dir, load_private(home))


def _grading_profile(home: Path, run_dir: Path, deps_dir: Path | None,
                     pack_dir: Path, rules: list[dict]) -> str:
    from .sandbox import grading_profile
    return grading_profile(home, run_dir, deps_dir, pack_dir, rules)


def _wrap(cmd: list[str], profile_path: Path) -> list[str]:
    from .sandbox import wrap
    return wrap(cmd, profile_path)


def _sandboxed_oracle_cmd(home: Path, run_dir: Path, pack_dir: Path, pack: dict,
                          cmd: list[str]) -> list[str]:
    """Write ``run_dir/grade.sb`` and wrap the oracle command in it."""
    profile_path = Path(run_dir) / "grade.sb"
    profile = _grading_profile(home, run_dir, _deps_dir(home, pack), pack_dir,
                               _rules(home, run_dir))
    profile_path.write_text(profile, encoding="utf-8")
    return _wrap(cmd, profile_path)


def _append(home: Path, record: dict) -> None:
    path = Path(home) / "grades.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _score(passed: int, total: int):
    if total <= 0:
        return None
    return round(passed / total, 3)


# --------------------------------------------------------------------------
# task: oracle
# --------------------------------------------------------------------------

def _oracle(home: Path, run_dir: Path, pack_dir: Path, pack: dict,
            protocol: dict, node_modules: Path | None) -> dict:
    repo = run_dir / "repo"
    setup_error = None
    if not repo.is_dir():
        return {"items": [], "setup_error": f"run repo missing: {repo}"}
    script = pack_dir / "oracle" / "check.sh"
    if not script.is_file():
        return {"items": [], "setup_error": f"oracle missing: {script}"}
    _link_deps(repo, node_modules)

    timeout = 60 * int(pack.get("timeout_minutes")
                       or protocol.get("worker_timeout_minutes") or 45)
    with tempfile.TemporaryDirectory(prefix="bench-oracle-") as scratch:
        out = Path(scratch) / "oracle.json"
        try:
            cmd = _sandboxed_oracle_cmd(
                home, run_dir, pack_dir, pack,
                ["bash", str(script), str(repo), str(out)])
        except Exception as error:  # noqa: BLE001 - no profile means no safe grade
            return {"items": [], "setup_error": f"grading sandbox failed: {error}"}
        try:
            proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"items": [], "setup_error": f"oracle timeout after {timeout}s"}
        except OSError as error:
            return {"items": [], "setup_error": f"oracle failed to start: {error}"}
        payload = _read_json(out)

    if proc.returncode == 2:
        error = (payload or {}).get("setup_error") if isinstance(payload, dict) else None
        return {"items": [], "setup_error": str(error or (proc.stderr or "").strip()[-500:]
                                                or "oracle setup error")}
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return {"items": [], "setup_error": f"oracle output unreadable (exit {proc.returncode}): {tail}"}
    if payload.get("setup_error"):
        return {"items": [], "setup_error": str(payload["setup_error"])}

    critical_ids = set(pack.get("critical") or [])
    items = []
    for case in payload["cases"]:
        # The oracle runs worker-controlled code: its JSON is untrusted.
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not case["id"] or not isinstance(case.get("passed"), bool)
                or ("critical" in case and not isinstance(case["critical"], bool))):
            return {"items": [], "setup_error": "oracle output malformed: bad case entry"}
        case_id = case["id"]
        passed = case["passed"]
        critical = bool(case.get("critical")) or case_id in critical_ids
        items.append({"id": case_id, "passed": passed, "critical": critical,
                      "verdict": "passed" if passed else "failed"})
    if not items:
        return {"items": [], "setup_error": "oracle produced no cases"}
    return {"items": items, "setup_error": None}


# --------------------------------------------------------------------------
# scout: checklist
# --------------------------------------------------------------------------

def _source_files(pack_dir: Path) -> list[str]:
    root = Path(pack_dir) / "source"
    if not root.is_dir():
        return []
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


def _file_covered(path: str, report: str, source_files: list[str] = ()) -> bool:
    segments = [s for s in path.strip("/").split("/") if s]
    if not segments:
        return False
    if "/".join(segments) in report:
        return True
    # Shorter suffixes only count when they identify exactly one source file.
    for size in range(min(2, len(segments)), len(segments)):
        suffix = "/".join(segments[-size:])
        matches = [name for name in source_files
                   if name == suffix or name.endswith("/" + suffix)]
        if len(matches) == 1 and suffix in report:
            return True
    return False


def _symbol_covered(name: str, report: str) -> bool:
    if not name:
        return False
    pattern = r"(?<![\w$])" + re.escape(name) + r"(?![\w$])"
    return re.search(pattern, report) is not None


def _checklist(report: str, checklist: dict, judge_cfg: dict,
               source_files: list[str] = ()) -> dict:
    items = []
    facts = []
    for item in checklist.get("items") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind == "file":
            covered = _file_covered(str(item.get("path") or ""), report,
                                    source_files)
            items.append({"id": item.get("id"), "passed": covered,
                          "critical": False,
                          "verdict": "covered" if covered else "missing"})
        elif kind == "symbol":
            covered = _symbol_covered(str(item.get("name") or ""), report)
            items.append({"id": item.get("id"), "passed": covered,
                          "critical": False,
                          "verdict": "covered" if covered else "missing"})
        elif kind == "fact":
            facts.append(item)

    judge_note = None
    if facts:
        verdicts = judge_module.judge_facts(report, facts, judge_cfg)
        for fact, verdict in zip(facts, verdicts):
            items.append({"id": fact.get("id"),
                          "passed": verdict["verdict"] == "covered",
                          "critical": bool(fact.get("critical"))
                          and verdict["verdict"] == "contradicted",
                          "verdict": verdict["verdict"]})
        judge_note = {
            "model": judge_cfg.get("model"),
            "effort": judge_cfg.get("effort") or "medium",
            "rubric_version": checklist.get("rubric_version") or judge_module.RUBRIC_VERSION,
            "prompt_sha256": judge_module.prompt_sha256(report, facts),
        }
    return {"items": items, "judge": judge_note}


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

def grade_run(home: Path, run_dir: Path, protocol: dict,
              judge: dict | None = None) -> dict:
    """Grade one run and append the record to ``HOME/grades.jsonl``."""
    home = Path(home)
    run_dir = Path(run_dir)
    run = _read_json(run_dir / "run.json") or {}
    role = run.get("role")
    pack_id = run.get("pack")
    if not role or not pack_id:
        raise ValueError(f"run.json in {run_dir} lacks role/pack")

    pack_dir = _pack_dir(home, role, pack_id)
    pack = _read_json(pack_dir / "pack.json") or {}
    summary = _read_json(run_dir / "summary.json")
    if not isinstance(summary, dict):
        summary = session_module.summarize(run_dir, protocol)

    setup_error = None
    judge_note = None
    baseline = None
    if role == "task":
        kind = "oracle"
        if "baseline_passed" not in pack:
            raise ValueError(f"pack {pack_id} lacks baseline_passed")
        baseline = int(pack["baseline_passed"])
        result = _oracle(home, run_dir, pack_dir, pack, protocol,
                         _deps_node_modules(home, pack))
        items = result["items"]
        setup_error = result["setup_error"]
        critical = any(item["critical"] and not item["passed"] for item in items)
    else:
        kind = "checklist"
        checklist_path = pack_dir / "checklist.json"
        checklist = _read_json(checklist_path)
        if not isinstance(checklist, dict):
            items = []
            setup_error = f"checklist missing: {checklist_path}"
            critical = False
        else:
            worker = summary.get("worker") or {}
            report = worker.get("final_text") or ""
            judge_cfg = judge or protocol.get("judge_default") or {}
            judged = _checklist(report, checklist, judge_cfg,
                                _source_files(pack_dir))
            items = judged["items"]
            judge_note = judged["judge"]
            critical = any(item["critical"] and item["verdict"] == "contradicted"
                           for item in items)

    passed = sum(1 for item in items if item["passed"])
    total = len(items)
    if setup_error:
        score = None
    elif role == "task":
        target = total - baseline
        if target <= 0:
            raise ValueError(f"pack {pack_id} has no cases above the baseline")
        score = round(min(max((passed - baseline) / target, 0.0), 1.0), 3)
    else:
        score = _score(passed, total)

    record = {
        "run_id": run.get("run_id") or run_dir.name,
        "batch_id": run.get("batch_id") or run_dir.parent.name,
        "role": role,
        "pack": pack_id,
        "kind": kind,
        "score": score,
        "passed": passed,
        "total": total,
        "critical": critical,
        "baseline_passed": baseline,
        "items": items,
        "judge": judge_note,
        "graded_at": _now(),
        "grader_version": GRADER_VERSION,
        "setup_error": setup_error,
    }
    _append(home, record)
    return record
