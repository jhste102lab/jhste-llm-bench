"""Export ``docs/data/v2.js`` from the private bench home.

Only allowlisted site fields leave HOME. The rendered file is leak-scanned
before it is written; any hit aborts the export and leaves the old file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .aggregate import cohorts
from .leak import check_text, generic_hits

MODELS_PATH = Path(__file__).with_name("models.json")
SUITE_LABELS = {"task": {"ko": "코딩 과제"}, "scout": {"ko": "탐색 과제"}}
METRICS = [
    {"key": "score", "kind": "pct", "better": "high", "bar": True, "primary": True},
    {"key": "cost", "kind": "usd", "better": "low", "bar": True, "perTask": True},
    {"key": "time", "kind": "dur", "better": "low", "bar": True, "perTask": True},
]


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _models() -> dict:
    data = _read_json(MODELS_PATH, {})
    if not isinstance(data, dict):
        raise ValueError(f"cannot read {MODELS_PATH}")
    return data


def _model(key: str, models: dict) -> dict:
    entry = models.get(key)
    if not isinstance(entry, dict) or not entry.get("key"):
        raise ValueError(f"unknown model id {key!r}; add it to bench/models.json")
    return entry


def _pack_labels(home: Path, role: str, wanted: list[str]) -> list[dict]:
    root = Path(home) / "packs" / role
    tasks = []
    for pack_id in wanted:
        pack = _read_json(root / pack_id / "pack.json", {}) if root.is_dir() else {}
        label = (pack or {}).get("label") if isinstance(pack, dict) else None
        tasks.append({"id": pack_id,
                      "label": label if isinstance(label, dict) else {"ko": pack_id}})
    return tasks


def _date(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).date().isoformat().replace("-", ".")
    return value[:10].replace("-", ".")


def _suite_id(version: str, role: str, judge: str | None, many: bool) -> str:
    if not many or role != "scout" or not judge:
        return f"{version}-{role}"
    slug = judge.split("/")[-1].replace(".", "-")
    return f"{version}-{role}-{slug}"


def _rows(cohort: dict, suite_id: str, models: dict) -> list[dict]:
    rows = []
    for row in cohort["rows"]:
        model = _model(row["model"], models)
        rows.append({
            "id": f"{suite_id}:{model['key']}:{row['effort']}",
            "model": model["key"],
            "effort": row["effort"],
            "n": row["n"],
            "values": row["values"],
            "tasks": row["tasks"],
            "flags": row["flags"],
            "detail": row["detail"],
        })
    return rows


def _payload(home: Path, protocol: dict, grouped: list[dict] | None = None) -> dict:
    version = protocol.get("version") or "v2"
    models = _models()
    grouped = cohorts(home, protocol) if grouped is None else grouped
    suites = {"task": [], "scout": []}
    used_models: dict[str, dict] = {}
    by_key = {entry["key"]: entry for entry in models.values() if entry.get("key")}

    for role in ("task", "scout"):
        role_cohorts = [c for c in grouped if c["role"] == role]
        many = len(role_cohorts) > 1
        for cohort in role_cohorts:
            suite_id = _suite_id(version, role, cohort["judge"], many)
            rows = _rows(cohort, suite_id, models)
            for row in rows:
                used_models[row["model"]] = by_key[row["model"]]
            judge_key = None
            if role == "scout" and cohort["judge"]:
                judge_key = _model(cohort["judge"], models)["key"]
                used_models[judge_key] = by_key[judge_key]
            suites[role].append({
                "id": suite_id,
                "label": SUITE_LABELS[role],
                "protocol": version,
                "date": _date(cohort.get("latest_at")),
                "runs": cohort["runs"],
                "judge": judge_key,
                "tasks": _pack_labels(home, role, cohort["packs"]),
                "scopes": {
                    "worker": {
                        "metrics": METRICS,
                        "rank": ["score", "cost"],
                        "scatter": {"x": "cost", "y": "score"},
                        "taskKey": "score",
                        "rows": rows,
                    }
                },
            })

    return {
        "generated": datetime.now(timezone.utc).date().isoformat(),
        "models": used_models,
        "suites": suites,
    }


def _private_terms(home: Path, required: bool = False) -> list[str]:
    path = Path(home) / "private.json"
    private = _read_json(path)
    terms = private.get("private_terms") if isinstance(private, dict) else None
    clean = [str(term) for term in terms or [] if str(term).strip()]
    if required and not clean:
        raise RuntimeError(
            f"refusing to export: {path} is missing, unreadable or has no private_terms")
    return clean


def export(home: Path, repo_docs_dir: Path, protocol: dict) -> Path:
    """Write ``<repo_docs_dir>/data/v2.js``; raise before writing on any leak."""
    grouped = cohorts(Path(home), protocol)
    # Existing runs can only be leak-scanned against configured private terms;
    # an empty site stays exportable without them.
    has_runs = any(cohort["rows"] for cohort in grouped)
    terms = _private_terms(home, required=has_runs)
    payload = _payload(Path(home), protocol, grouped)
    body = "// Generated by python3 -m bench export. Do not edit.\nwindow.BENCH_V2 = " \
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"

    hits = check_text(body, terms) + generic_hits(body)
    if hits:
        raise RuntimeError("refusing to export: leak patterns " + ", ".join(sorted(set(hits))))

    target = Path(repo_docs_dir) / "data" / "v2.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".tmp")
    scratch.write_text(body, encoding="utf-8")
    os.replace(scratch, target)
    return target
