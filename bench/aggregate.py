"""Cohort aggregation: scored attempts -> per-config leaderboard rows.

A cohort is ``(protocol, role, judge model for scout)``. Rows are per config
(``provider/model:effort``), ranked by score desc then cost asc; contaminated
rows sort below every clean row and rank nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path

SCORED_OUTCOMES = ("completed", "timeout")


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _iter_runs(home: Path):
    try:
        from .home import iter_runs
        return list(iter_runs(Path(home)))
    except BaseException:
        root = Path(home) / "runs"
        if not root.is_dir():
            return []
        return [p for p in sorted(root.glob("*/*")) if (p / "run.json").is_file()]


def _judge_key(record: dict):
    judge = record.get("judge")
    if isinstance(judge, dict) and judge.get("model"):
        return judge["model"]
    return None


def _grades(home: Path) -> dict[str, dict]:
    """Latest grade per run and judge: ``{run_id: {judge_key: record}}``."""
    index: dict[str, dict] = {}
    path = Path(home) / "grades.jsonl"
    if not path.is_file():
        return index
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            run_id = record.get("run_id")
            if not run_id:
                continue
            index.setdefault(run_id, {})[_judge_key(record)] = record
    return index


def _audited(run_dir: Path, run: dict) -> bool:
    """A run is publishable only when its audit completed successfully.

    Absent or unreadable ``audit.json`` is not evidence of a clean run, and an
    audit failure leaves a marker in ``outcome_detail`` (the result is kept,
    but the row must not be ranked).
    """
    if "audit failed" in str(run.get("outcome_detail") or "").lower():
        return False
    audit = _read_json(Path(run_dir) / "audit.json")
    return (isinstance(audit, dict)
            and isinstance(audit.get("contaminated"), bool)
            and isinstance(audit.get("accessed"), list))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _attempt_payload(run_dir: Path, grade: dict) -> dict:
    summary = _read_json(run_dir / "summary.json") or {}
    audit = _read_json(run_dir / "audit.json") or {}
    worker = summary.get("worker") or {}
    return {
        "grade": grade,
        "cost": float(worker.get("cost_usd") or 0.0),
        "seconds": float(worker.get("model_seconds") or 0.0)
        + float(worker.get("tool_seconds") or 0.0),
        "model_seconds": float(worker.get("model_seconds") or 0.0),
        "tool_seconds": float(worker.get("tool_seconds") or 0.0),
        "web": int((worker.get("web") or {}).get("searches") or 0)
        + int((worker.get("web") or {}).get("fetches") or 0),
        "blocked": int(audit.get("blocked") or 0),
        "contaminated": bool(audit.get("contaminated")),
        "access": audit.get("accessed") or [],
    }


def _row(key: str, model: str, effort: str, attempts: list[dict],
         small_diff_pp: float, cohort_packs: set[str] | None = None) -> dict:
    by_pack: dict[str, list[dict]] = {}
    for attempt in attempts:
        by_pack.setdefault(attempt["grade"]["pack"], []).append(attempt)

    tasks = {}
    pack_scores = []
    pack_costs = []
    pack_times = []
    for pack_id, entries in sorted(by_pack.items()):
        scores = [_num_score(e["grade"]) for e in entries]
        scores = [s for s in scores if s is not None]
        if not scores:
            continue
        fixed = _mean([max((e["grade"].get("passed") or 0)
                           - (e["grade"].get("baseline_passed") or 0), 0)
                      for e in entries])
        target = _mean([(e["grade"].get("total") or 0)
                        - (e["grade"].get("baseline_passed") or 0)
                        for e in entries])
        cost = _mean([e["cost"] for e in entries])
        seconds = _mean([e["seconds"] for e in entries])
        tasks[pack_id] = {
            # Normalised gain: cases fixed above the untouched baseline.
            "score": [round(fixed, 1), round(target, 1)],
            "cost": round(cost, 6),
            "time": round(seconds, 1),
            "critical": any(e["grade"].get("critical") for e in entries),
        }
        pack_scores.append(_mean(scores))
        pack_costs.append(cost)
        pack_times.append(seconds)

    if not pack_scores:
        return {}

    missing = sorted(p for p in (cohort_packs or set()) if p not in by_pack)

    scored = [e for e in attempts if e["grade"].get("score") is not None]
    counts = [len([e for e in entries if e["grade"].get("score") is not None])
              for entries in by_pack.values()]
    n = min(counts) if counts else 0

    model_seconds = sum(e["model_seconds"] for e in scored)
    tool_seconds = sum(e["tool_seconds"] for e in scored)
    total_seconds = model_seconds + tool_seconds
    access: dict[str, int] = {}
    for attempt in scored:
        for entry in attempt["access"]:
            category = entry.get("category")
            access[category] = access.get(category, 0) + int(entry.get("count") or 0)

    concurrency = max((e["concurrency"] for e in scored), default=0)
    detail = {
        "modelShare": round(model_seconds / total_seconds, 3) if total_seconds else 0.0,
        "toolShare": round(tool_seconds / total_seconds, 3) if total_seconds else 0.0,
        "concurrency": concurrency,
        "web": round(sum(e["web"] for e in scored) / n, 2) if n else 0,
        "blocked": sum(e["blocked"] for e in scored),
        "access": [{"category": c, "count": count} for c, count in sorted(access.items())],
    }

    return {
        "key": key,
        "model": model,
        "effort": effort,
        "n": n,
        "values": {
            "score": round(100 * _mean(pack_scores), 1),
            "cost": round(_mean(pack_costs), 6),
            "time": round(_mean(pack_times), 1),
        },
        "tasks": tasks,
        "flags": {
            "contaminated": any(e["contaminated"] for e in scored),
            "critical": any(e["grade"].get("critical") for e in scored),
            "incomplete": bool(missing),
            "close": False,
        },
        "detail": detail,
        "_pack_scores": pack_scores,
    }


def _num_score(grade: dict):
    score = grade.get("score")
    return None if score is None else float(score)


def _ranked(row: dict) -> bool:
    return not (row["flags"]["contaminated"] or row["flags"]["incomplete"])


def _mark_close(rows: list[dict], small_diff_pp: float) -> None:
    previous = None
    for row in rows:
        if not _ranked(row):
            continue
        if previous is not None:
            row["flags"]["close"] = (
                row["values"]["score"] >= previous["values"]["score"] - small_diff_pp)
        previous = row


def cohorts(home: Path, protocol: dict) -> list[dict]:
    """Group scored attempts into cohorts and build per-config rows."""
    home = Path(home)
    version = protocol.get("version") or "v2"
    small_diff = float(protocol.get("small_diff_pp") or 5)
    grades = _grades(home)

    grouped: dict[tuple, dict] = {}
    for run_dir in _iter_runs(home):
        run = _read_json(run_dir / "run.json")
        if not isinstance(run, dict):
            continue
        if run.get("protocol") not in (None, version):
            continue
        if run.get("outcome") not in SCORED_OUTCOMES:
            continue
        batch = _read_json(run_dir.parent / "batch.json") or {}
        if batch.get("quarantined"):
            continue
        if not _audited(run_dir, run):
            continue
        run_grades = grades.get(run.get("run_id") or run_dir.name)
        if not run_grades:
            continue
        role = run.get("role")
        config = run.get("config") or {}
        key = config.get("key") or f"{config.get('model')}:{config.get('effort')}"
        for judge_key, grade in run_grades.items():
            if grade.get("role") not in (None, role):
                continue
            if grade.get("score") is None:
                continue
            cohort_key = (version, role, judge_key if role == "scout" else None)
            cohort = grouped.setdefault(cohort_key, {
                "protocol": version,
                "role": role,
                "judge": judge_key if role == "scout" else None,
                "small_diff_pp": small_diff,
                "packs": set(),
                "rows": {},
                "runs": 0,
                "latest_at": None,
            })
            attempt = _attempt_payload(run_dir, grade)
            attempt["concurrency"] = int(run.get("concurrency") or 0)
            attempt["config"] = config
            attempt["key"] = key
            cohort["packs"].add(grade.get("pack"))
            cohort["runs"] += 1
            stamp = run.get("ended_at") or run.get("started_at")
            if isinstance(stamp, str) and (cohort["latest_at"] is None or stamp > cohort["latest_at"]):
                cohort["latest_at"] = stamp
            cohort["rows"].setdefault(key, []).append(attempt)

    result = []
    for cohort in grouped.values():
        packs = {p for p in cohort["packs"] if p}
        rows = []
        for key, attempts in cohort["rows"].items():
            config = attempts[0]["config"]
            row = _row(key, config.get("model") or key, config.get("effort") or "",
                       attempts, small_diff, packs)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: (not _ranked(r),
                                 -r["values"]["score"], r["values"]["cost"]))
        _mark_close(rows, small_diff)
        for row in rows:
            row.pop("_pack_scores", None)
        cohort["packs"] = sorted(packs)
        cohort["rows"] = rows
        result.append(cohort)

    result.sort(key=lambda c: (c["protocol"], c["role"], c["judge"] or ""))
    return result
