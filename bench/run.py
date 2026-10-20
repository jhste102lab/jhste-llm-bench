"""Batch orchestration: prepare, prove isolation, run, classify, audit, grade."""

from __future__ import annotations

import importlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import deps as deps_mod
from . import home as home_mod
from . import manifest as manifest_mod
from . import omp as omp_mod
from . import pack as pack_mod
from . import rules as rules_mod
from . import sandbox as sandbox_mod

RETRYABLE_OUTCOMES = {"quota-paused", "setup-error", "invalid"}
SCORED_OUTCOMES = {"completed", "timeout"}
QUOTA_HINTS = ("quota", "usage limit", "rate limit", "insufficient", "429")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _module(name: str):
    """Import a sibling module at call time so tests can substitute it."""
    return importlib.import_module(f".{name}", __package__)


def config_key(model: str, effort: str) -> str:
    return f"{model}:{effort}"


def parse_config(key: str) -> tuple[str, str]:
    if ":" not in key:
        raise SystemExit(f"config must be model:effort, got {key!r}")
    model, effort = key.rsplit(":", 1)
    return model, effort


def cfgslug(model: str, effort: str) -> str:
    raw = f"{model}-{effort}"
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw).strip("-")


def run_id_for(batch: dict, pack_id: str, key: str, attempt: int) -> str:
    model, effort = parse_config(key)
    return (f"{batch['batch_id']}-{batch['role']}-{pack_id}-"
            f"{cfgslug(model, effort)}-a{attempt}")


def _unique_batch_id(home: Path, role: str) -> str:
    base = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{role}"
    candidate = base
    suffix = 1
    while (Path(home) / "runs" / candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def batch_configs(role: str, models: list[str], efforts: list[str],
                  protocol: dict, use_baseline: bool) -> tuple[list[str], str | None]:
    if len(models) != len(efforts):
        raise SystemExit("each --model needs exactly one --effort")
    configs = [config_key(m, e) for m, e in zip(models, efforts)]
    baseline = protocol["baseline"][role]
    baseline_key = config_key(baseline["model"], baseline["effort"])
    if use_baseline and baseline_key not in configs:
        configs.append(baseline_key)
    return configs, (baseline_key if baseline_key in configs else None)


def create_batch(home: Path, protocol: dict, role: str, configs: list[str],
                 baseline_key: str | None, judge: dict, attempts: int,
                 packs: list[str]) -> dict:
    batch_id = _unique_batch_id(home, role)
    batch = {
        "batch_id": batch_id,
        "protocol": protocol["version"],
        "role": role,
        "configs": configs,
        "baseline_key": baseline_key,
        "created_at": utc_now(),
        "quarantined": False,
        "quarantine_reason": None,
        "accepted": False,
        "judge": judge,
        "attempts": attempts,
        "packs": list(packs),
    }
    home_mod.write_json(Path(home) / "runs" / batch_id / "batch.json", batch)
    return batch


def judge_from_arg(raw: str | None, protocol: dict) -> dict:
    if not raw:
        return dict(protocol["judge_default"])
    model, effort = parse_config(raw)
    return {"model": model, "effort": effort}


# ---------------------------------------------------------------- preparation

def prepare_run(home: Path, protocol: dict, private: dict, batch: dict,
                pack: dict, pack_id: str, key: str, attempt: int,
                manifest_sha: str) -> dict:
    model, effort = parse_config(key)
    role = batch["role"]
    run_id = run_id_for(batch, pack_id, key, attempt)
    run_dir = Path(home) / "runs" / batch["batch_id"] / run_id
    if run_dir.exists():
        raise SystemExit(f"run dir already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    source = pack_mod.require_source(home, role, pack_id)
    shutil.copytree(source, run_dir / "repo", symlinks=True)
    if (run_dir / "repo" / ".git").exists():
        raise SystemExit("run repo must not carry git history")

    pristine = manifest_mod.file_hashes(run_dir / "repo")
    (run_dir / "pristine-tree.sha256").write_text(
        "".join(f"{digest}  {rel}\n" for rel, digest in sorted(pristine.items())))

    deps_nm = None
    if pack.get("deps"):
        deps_nm = home_mod.deps_dir(home, private, pack["deps"])
        deps_mod.link_deps(run_dir / "repo", deps_nm)

    frozen = pack_mod.frozen_delegation(home, role, pack_id)
    (run_dir / "overlay.yml").write_text(omp_mod.overlay_text(
        role, model, effort, protocol, pack.get("timeout_minutes")))
    (run_dir / "prompt.md").write_text(omp_mod.replay_prompt(role, frozen))

    return {"home": Path(home), "protocol": protocol, "batch": batch,
            "pack": pack, "pack_id": pack_id, "key": key, "model": model,
            "effort": effort, "attempt": attempt, "run_id": run_id,
            "run_dir": run_dir, "deps_nm": deps_nm, "rules": [],
            "frozen": frozen, "manifest_sha256": manifest_sha,
            "baseline": key == batch.get("baseline_key")}


def _setup_error_record(ctx: dict, detail: str) -> dict:
    return {
        "run_id": ctx["run_id"], "batch_id": ctx["batch"]["batch_id"],
        "protocol": ctx["protocol"]["version"],
        "manifest_sha256": ctx["manifest_sha256"], "role": ctx["batch"]["role"],
        "pack": ctx["pack_id"],
        "config": {"key": ctx["key"], "model": ctx["model"], "effort": ctx["effort"]},
        "baseline": ctx["baseline"], "attempt": ctx["attempt"],
        "head": {"model": ctx["protocol"]["head"]["model"],
                 "effort": ctx["protocol"]["head"]["effort"]},
        "started_at": utc_now(), "ended_at": utc_now(),
        "outcome": "setup-error", "outcome_detail": detail,
        "concurrency": 0, "isolation_proof": {"ok": True, "checked": 0},
    }


# ---------------------------------------------------------------- classification

def _quota_text(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in QUOTA_HINTS)


def _delegation_check(delegations: list[dict], frozen: dict) -> tuple[bool, str]:
    if len(delegations) != 1:
        return False, f"head made {len(delegations)} task delegation(s), expected exactly 1"
    found = delegations[0]
    for field in pack_mod.DELEGATION_FIELDS:
        if found.get(field) != frozen.get(field):
            return False, f"delegation {field} differs from the frozen arguments"
    return True, ""


def classify(summary: dict, launched: dict, model: str,
             frozen: dict) -> tuple[str, str]:
    if launched.get("timed_out"):
        return "timeout", "runner wall-clock timeout"
    summary = summary or {}
    head = summary.get("head") or {}
    worker = summary.get("worker")
    delegations = head.get("delegations") or []

    if worker is None:
        if _quota_text(head.get("error")):
            return "quota-paused", f"head hit quota: {head.get('error')}"
        if not head.get("session"):
            return "setup-error", f"no head session ({launched.get('rc')})"
        ok, why = _delegation_check(delegations, frozen)
        if not ok:
            return "invalid", why
        return "setup-error", "worker session missing"

    errors = worker.get("errors") or []
    kinds = {e.get("kind") for e in errors}
    if "quota" in kinds:
        return "quota-paused", _first_message(errors, "quota")
    if "timeout" in kinds:
        return "timeout", _first_message(errors, "timeout")

    ok, why = _delegation_check(delegations, frozen)
    if not ok:
        return "invalid", why

    if worker.get("fallback"):
        return "invalid", "model fallback was used"
    observed = worker.get("observed_models") or []
    if any(m != model for m in observed):
        return "invalid", f"observed models {observed} != requested {model}"
    resolved = worker.get("resolved_model") or ""
    if not resolved:
        return "invalid", "worker resolved model missing"
    if resolved.rsplit(":", 1)[0] != model:
        return "invalid", f"resolved model {resolved} != requested {model}"

    if "provider" in kinds:
        return "setup-error", _first_message(errors, "provider")
    if worker.get("completed"):
        return "completed", ""
    if errors:
        return "setup-error", errors[0].get("message") or "worker error"
    return "invalid", "worker did not complete"


def _first_message(errors: list[dict], kind: str) -> str:
    for error in errors:
        if error.get("kind") == kind:
            return str(error.get("message") or kind)
    return kind


# ---------------------------------------------------------------- one run

def execute_run(ctx: dict, judge: dict, isolation: dict) -> dict:
    run_dir: Path = ctx["run_dir"]
    protocol = ctx["protocol"]
    started = utc_now()
    # Written before omp starts: session.summarize reads role/agent from it, and a
    # killed runner leaves a retryable setup-error record instead of no record.
    record = {
        "run_id": ctx["run_id"], "batch_id": ctx["batch"]["batch_id"],
        "protocol": protocol["version"],
        "manifest_sha256": ctx["manifest_sha256"], "role": ctx["batch"]["role"],
        "pack": ctx["pack_id"],
        "config": {"key": ctx["key"], "model": ctx["model"], "effort": ctx["effort"]},
        "baseline": ctx["baseline"], "attempt": ctx["attempt"],
        "head": {"model": protocol["head"]["model"], "effort": protocol["head"]["effort"]},
        "started_at": started, "ended_at": started,
        "outcome": "setup-error", "outcome_detail": "run did not finish",
        "concurrency": 0,
        "isolation_proof": sandbox_mod.summary(isolation),
    }
    home_mod.write_json(run_dir / "run.json", record)

    launched: dict = {}
    summary: dict = {}
    try:
        launched = omp_mod.launch(run_dir, protocol, protocol["head"])
        summary = _module("session").summarize(run_dir, protocol) or {}
        home_mod.write_json(run_dir / "summary.json", summary)
        outcome, detail = classify(summary, launched, ctx["model"], ctx["frozen"])
    except Exception as exc:  # noqa: BLE001 - any setup failure becomes an outcome
        outcome, detail = "setup-error", f"{type(exc).__name__}: {exc}"

    audit_result = {}
    try:
        audit_result = _module("audit").audit(run_dir, ctx["rules"]) or {}
        home_mod.write_json(run_dir / "audit.json", audit_result)
    except Exception as exc:  # noqa: BLE001 - auditing never changes the outcome
        detail = (detail + f"; audit failed: {type(exc).__name__}: {exc}").strip("; ")

    record["ended_at"] = utc_now()
    record["outcome"] = outcome
    record["outcome_detail"] = detail
    home_mod.write_json(run_dir / "run.json", record)

    if outcome in SCORED_OUTCOMES:
        try:
            _module("grade").grade_run(
                ctx["home"], run_dir, protocol,
                judge if ctx["batch"]["role"] == "scout" else None)
        except Exception as exc:  # noqa: BLE001 - grading is separate from the run outcome
            record["grade_error"] = f"{type(exc).__name__}: {exc}"
            home_mod.write_json(run_dir / "run.json", record)
    return record


# ---------------------------------------------------------------- batch driver

def _run_specs(home: Path, protocol: dict, private: dict, batch: dict,
               specs: list[tuple[str, str, int]], judge: dict,
               parallel: int | None, manifest_sha: str) -> dict:
    batch_dir = Path(home) / "runs" / batch["batch_id"]
    contexts: list[dict] = []
    prepared_records: list[dict] = []

    for pack_id, key, attempt in specs:
        run_id = run_id_for(batch, pack_id, key, attempt)
        run_dir = batch_dir / run_id
        if (run_dir / "run.json").is_file():
            continue
        try:
            pack = pack_mod.load(home, batch["role"], pack_id)
            contexts.append(prepare_run(home, protocol, private, batch, pack,
                                        pack_id, key, attempt, manifest_sha))
        except Exception as exc:  # noqa: BLE001 - recorded as setup-error
            run_dir.mkdir(parents=True, exist_ok=True)
            model, effort = parse_config(key)
            fake_ctx = {
                "run_id": run_id, "batch": batch, "protocol": protocol,
                "manifest_sha256": manifest_sha, "pack_id": pack_id, "key": key,
                "model": model, "effort": effort,
                "baseline": key == batch.get("baseline_key"),
                "attempt": attempt, "run_dir": run_dir,
            }
            record = _setup_error_record(fake_ctx, f"{type(exc).__name__}: {exc}")
            home_mod.write_json(run_dir / "run.json", record)
            prepared_records.append(record)

    # All run dirs exist now, so every profile can deny every sibling run.
    for ctx in contexts:
        ctx["rules"] = rules_mod.forbidden_rules(home, ctx["run_dir"], private)
        sandbox_mod.write_profile(ctx["run_dir"], sandbox_mod.build_profile(
            home, ctx["run_dir"], ctx["deps_nm"], ctx["rules"]))

    isolation = {"ok": True, "checked": 0, "checks": []}
    if contexts:
        first = contexts[0]
        isolation = sandbox_mod.proof(first["run_dir"], first["deps_nm"], first["rules"])
        home_mod.write_json(batch_dir / "isolation.json", isolation)
        if not isolation.get("ok"):
            failures = [c for c in isolation.get("checks", []) if not c.get("ok")]
            for ctx in contexts:
                record = _setup_error_record(ctx, "isolation proof failed")
                record["isolation_proof"] = sandbox_mod.summary(isolation)
                home_mod.write_json(ctx["run_dir"] / "run.json", record)
            raise SystemExit(
                "isolation proof failed; batch aborted:\n  "
                + "\n  ".join(f"{c['kind']} {c['category']} {c['target']}: {c.get('detail')}"
                              for c in failures))

    records: list[dict] = list(prepared_records)
    if contexts:
        worker_count = parallel if parallel else len(contexts)
        worker_count = max(1, min(worker_count, len(contexts)))
        lock = threading.Lock()

        def work(ctx: dict) -> dict:
            record = execute_run(ctx, judge, isolation)
            with lock:
                records.append(record)
            return record

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            list(pool.map(work, contexts))

    update_concurrency(batch_dir)
    for record in records:
        fresh = home_mod.read_json(batch_dir / record["run_id"] / "run.json")
        if isinstance(fresh, dict):
            record.update(fresh)
    batch = update_batch(home, protocol, batch)
    return {"batch": batch, "runs": sorted(records, key=lambda r: r["run_id"])}


def _parse_time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def update_concurrency(batch_dir: Path) -> None:
    """Max overlapping runs per run of this batch, counting runs of every batch in HOME."""
    batch_dir = Path(batch_dir)
    intervals: dict[Path, tuple] = {}
    for run_file in sorted(batch_dir.parent.glob("*/*/run.json")):
        record = home_mod.read_json(run_file) or {}
        started = _parse_time(record.get("started_at"))
        # A still-running run in another batch has no end yet; count it as open.
        ended = _parse_time(record.get("ended_at")) or (
            datetime.max.replace(tzinfo=timezone.utc) if started else None)
        if started and ended:
            intervals[run_file.parent] = (started, ended)
    for run_dir, (start, end) in intervals.items():
        if run_dir.parent != batch_dir:
            continue
        count = sum(1 for other_start, other_end in intervals.values()
                    if other_start <= end and start <= other_end)
        run_file = run_dir / "run.json"
        record = home_mod.read_json(run_file) or {}
        if record.get("concurrency") != count:
            record["concurrency"] = count
            home_mod.write_json(run_file, record)


def latest_grades(home: Path) -> dict[str, dict]:
    path = Path(home) / "grades.jsonl"
    grades: dict[str, dict] = {}
    if not path.is_file():
        return grades
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("run_id"):
            grades[record["run_id"]] = record
    return grades


def _batch_runs(batch_dir: Path) -> list[dict]:
    runs = []
    for run_dir in sorted(Path(batch_dir).iterdir()):
        if run_dir.is_dir() and (run_dir / "run.json").is_file():
            runs.append(home_mod.read_json(run_dir / "run.json") or {})
    return runs


def update_batch(home: Path, protocol: dict, batch: dict) -> dict:
    """Recompute baseline drift for the batch and persist batch.json."""
    batch_dir = Path(home) / "runs" / batch["batch_id"]
    grades = latest_grades(home)
    runs = _batch_runs(batch_dir)

    baseline = [grades[r["run_id"]] for r in runs
                if r.get("baseline") and r["run_id"] in grades
                and r.get("outcome") in SCORED_OUTCOMES]
    scores = [g["score"] for g in baseline if isinstance(g.get("score"), (int, float))]

    quarantined, reason = False, None
    if batch.get("accepted"):
        scores = []
    if scores:
        current = sum(scores) / len(scores)
        reference = _accepted_baseline_scores(home, protocol, batch, grades)
        if reference:
            previous = sum(reference) / len(reference)
            moved_pp = abs(current - previous) * 100
            if moved_pp > protocol["baseline_drift_pp"]:
                quarantined = True
                reason = (f"baseline moved {moved_pp:.1f}pp "
                          f"({previous:.3f} -> {current:.3f}); "
                          f"limit {protocol['baseline_drift_pp']}pp")
        batch["baseline_score"] = round(current, 4)

    batch["quarantined"] = quarantined
    batch["quarantine_reason"] = reason
    home_mod.write_json(batch_dir / "batch.json", batch)
    return batch


def _accepted_baseline_scores(home: Path, protocol: dict, batch: dict,
                              grades: dict[str, dict]) -> list[float]:
    runs_root = Path(home) / "runs"
    scores: list[float] = []
    if not runs_root.is_dir():
        return scores
    for other_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if other_dir.name == batch["batch_id"]:
            continue
        other = home_mod.read_json(other_dir / "batch.json") or {}
        if not other.get("accepted") or other.get("quarantined"):
            continue
        if other.get("role") != batch["role"] or other.get("protocol") != batch["protocol"]:
            continue
        for run in _batch_runs(other_dir):
            record = grades.get(run.get("run_id"))
            if not run.get("baseline") or not record:
                continue
            if run.get("outcome") not in SCORED_OUTCOMES:
                continue
            if isinstance(record.get("score"), (int, float)):
                scores.append(record["score"])
    return scores


def start(home: Path, protocol: dict, private: dict, role: str,
          models: list[str], efforts: list[str], packs: list[str] | None,
          attempts: int, parallel: int | None, use_baseline: bool,
          judge: dict) -> dict:
    manifest_mod.verify(home, protocol, private)
    manifest_sha = manifest_mod.manifest_sha256(home)

    if packs:
        for pack_id in packs:
            pack_mod.load(home, role, pack_id)
    else:
        packs = home_mod.iter_packs(home, role)
        if not packs:
            raise SystemExit(f"no packs under {Path(home) / 'packs' / role}")

    configs, baseline_key = batch_configs(role, models, efforts, protocol, use_baseline)
    batch = _resumable_batch(home, protocol, role, configs, baseline_key, judge,
                             attempts, packs) or create_batch(
        home, protocol, role, configs, baseline_key, judge, attempts, packs)

    specs = [(pack_id, key, attempt)
             for pack_id in packs for key in configs
             for attempt in range(1, max(1, attempts) + 1)]
    return _run_specs(home, protocol, private, batch, specs, judge, parallel, manifest_sha)


def _resumable_batch(home: Path, protocol: dict, role: str, configs: list[str],
                     baseline_key: str | None, judge: dict, attempts: int,
                     packs: list[str]) -> dict | None:
    """An existing batch with the same declaration but missing runs is continued."""
    runs_root = Path(home) / "runs"
    if not runs_root.is_dir():
        return None
    for batch_dir in sorted((p for p in runs_root.iterdir() if p.is_dir()),
                            key=lambda p: p.name, reverse=True):
        batch = home_mod.read_json(batch_dir / "batch.json")
        if not isinstance(batch, dict) or batch.get("accepted"):
            continue
        if (batch.get("protocol") != protocol["version"] or batch.get("role") != role
                or batch.get("baseline_key") != baseline_key
                or int(batch.get("attempts") or 0) != attempts
                or sorted(batch.get("configs") or []) != sorted(configs)
                or sorted(batch.get("packs") or []) != sorted(packs)
                or (batch.get("judge") or {}) != judge):
            continue
        for pack_id in packs:
            for key in configs:
                for attempt in range(1, max(1, attempts) + 1):
                    run_dir = batch_dir / run_id_for(batch, pack_id, key, attempt)
                    if not (run_dir / "run.json").is_file():
                        return batch
    return None


def retry(home: Path, protocol: dict, private: dict, batch_id: str,
          judge: dict | None, parallel: int | None) -> dict:
    batch = home_mod.read_json(Path(home) / "runs" / batch_id / "batch.json")
    if not isinstance(batch, dict):
        raise SystemExit(f"no batch {batch_id}")
    manifest_mod.verify(home, protocol, private)
    manifest_sha = manifest_mod.manifest_sha256(home)

    judge = judge or batch.get("judge") or dict(protocol["judge_default"])
    latest: dict[tuple[str, str], dict] = {}
    for run in _batch_runs(Path(home) / "runs" / batch_id):
        key = (run.get("pack"), (run.get("config") or {}).get("key"))
        current = latest.get(key)
        if current is None or int(run.get("attempt") or 0) > int(current.get("attempt") or 0):
            latest[key] = run

    packs = batch.get("packs") or sorted({pack for pack, _ in latest})
    specs: list[tuple[str, str, int]] = []
    for pack_id in packs:
        for key in batch.get("configs") or []:
            record = latest.get((pack_id, key))
            if record is None:
                specs.append((pack_id, key, 1))
            elif record.get("outcome") in RETRYABLE_OUTCOMES:
                specs.append((pack_id, key, int(record.get("attempt") or 0) + 1))
    if not specs:
        return {"batch": batch, "runs": [], "skipped": "no retryable runs"}
    return _run_specs(home, protocol, private, batch, specs, judge, parallel, manifest_sha)


# ---------------------------------------------------------------- admin

def accept(home: Path, batch_id: str) -> dict:
    path = Path(home) / "runs" / batch_id / "batch.json"
    batch = home_mod.read_json(path)
    if not isinstance(batch, dict):
        raise SystemExit(f"no batch {batch_id}")
    batch["quarantined"] = False
    batch["quarantine_reason"] = None
    batch["accepted"] = True
    home_mod.write_json(path, batch)
    return batch


def status(home: Path) -> dict:
    home = Path(home)
    protocol = home_mod.load_protocol()
    batches = []
    runs_root = home / "runs"
    if runs_root.is_dir():
        for batch_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
            meta = home_mod.read_json(batch_dir / "batch.json") or {}
            runs = _batch_runs(batch_dir)
            outcomes: dict[str, int] = {}
            for run in runs:
                outcomes[run.get("outcome") or "unknown"] = \
                    outcomes.get(run.get("outcome") or "unknown", 0) + 1
            batches.append({
                "batch_id": batch_dir.name, "role": meta.get("role"),
                "protocol": meta.get("protocol"), "configs": meta.get("configs"),
                "runs": len(runs), "outcomes": outcomes,
                "baseline_score": meta.get("baseline_score"),
                "quarantined": bool(meta.get("quarantined")),
                "quarantine_reason": meta.get("quarantine_reason"),
                "accepted": bool(meta.get("accepted")),
            })
    return {
        "home": str(home),
        "protocol": protocol["version"],
        "omp_version": protocol["omp_version"],
        "packs": {role: home_mod.iter_packs(home, role) for role in ("task", "scout")},
        "batches": batches,
    }


def isolation_check(home: Path, private: dict) -> dict:
    """Standalone isolation proof in a throwaway run-shaped directory."""
    probe = Path(home) / "_isolation-check"
    if probe.exists():
        shutil.rmtree(probe)
    repo = probe / "repo"
    repo.mkdir(parents=True)
    (repo / "probe.txt").write_text("isolation probe\n")

    deps_nm = None
    for name in sorted(private.get("deps") or {}):
        candidate = Path(home) / private["deps"][name]
        if candidate.is_dir():
            deps_nm = candidate
            break

    rules = rules_mod.forbidden_rules(home, probe, private)
    sandbox_mod.write_profile(probe, sandbox_mod.build_profile(
        home, probe, deps_nm, rules))
    result = sandbox_mod.proof(probe, deps_nm, rules)
    result["run_dir"] = str(probe)
    shutil.rmtree(probe, ignore_errors=True)
    return result


def freeze(home: Path, protocol: dict, private: dict, role: str, pack_id: str,
           force: bool = False) -> dict:
    """Run the head once on the pack request and freeze its task arguments."""
    home = Path(home)
    pack = pack_mod.load(home, role, pack_id)
    pack_path = home_mod.pack_dir(home, role, pack_id)
    delegation_path = pack_path / "delegation.json"
    if delegation_path.exists() and not force:
        raise SystemExit(f"{delegation_path} exists; use --force to replace it")

    request_path = pack_path / "request.md"
    if not request_path.is_file():
        raise SystemExit(f"pack {role}/{pack_id} has no request.md")
    installed = omp_mod.version()
    if installed != protocol["omp_version"]:
        raise SystemExit(f"omp {installed} != pinned {protocol['omp_version']}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    freeze_dir = home / "runs" / f"freeze-{stamp}-{role}-{pack_id}"
    suffix = 1
    while freeze_dir.exists():
        suffix += 1
        freeze_dir = home / "runs" / f"freeze-{stamp}-{role}-{pack_id}-{suffix}"
    (freeze_dir / "repo").mkdir(parents=True)
    (freeze_dir / "sessions").mkdir()

    # The worker is only spawned to let the head's task call complete; stop it at once.
    baseline = protocol["baseline"][role]
    (freeze_dir / "overlay.yml").write_text(omp_mod.overlay_text(
        role, baseline["model"], baseline["effort"], protocol, max_runtime_ms=1000))
    (freeze_dir / "prompt.md").write_text(
        omp_mod.freeze_prompt(role, request_path.read_text()))
    rules = rules_mod.forbidden_rules(home, freeze_dir, private)
    sandbox_mod.write_profile(freeze_dir, sandbox_mod.build_profile(
        home, freeze_dir, None, rules))

    launched = omp_mod.launch(freeze_dir, protocol, protocol["head"])
    summary = _module("session").summarize(freeze_dir, protocol) or {}
    home_mod.write_json(freeze_dir / "summary.json", summary)
    head = summary.get("head") or {}
    delegations = head.get("delegations") or []
    if launched.get("timed_out") or not delegations:
        raise SystemExit(
            f"freeze failed (rc={launched.get('rc')}, timed_out={launched.get('timed_out')}); "
            f"see {freeze_dir}")
    found = delegations[0]
    if found.get("agent") != role:
        raise SystemExit(f"head delegated to {found.get('agent')!r}, expected {role!r}")
    payload = {field: found[field] for field in pack_mod.DELEGATION_FIELDS}
    record = dict(payload)
    record.update({
        "head": {"model": protocol["head"]["model"], "effort": protocol["head"]["effort"]},
        "frozen_at": utc_now(),
        "sha256": pack_mod.delegation_sha256(payload),
    })
    home_mod.write_json(delegation_path, record)
    return {"pack": f"{role}/{pack_id}", "run_dir": str(freeze_dir),
            "delegation": str(delegation_path), "sha256": record["sha256"],
            "hint": "run `python3 -m bench lock` to pin the new delegation"}
