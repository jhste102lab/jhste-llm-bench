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
        if (run_dir / "run.json").is_file():
            raise SystemExit(f"run dir already exists: {run_dir}")
        # Interrupted preparation: no record was written, so rebuild in place.
        shutil.rmtree(run_dir)
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


def _delegation_check(head: dict, frozen: dict) -> tuple[bool, str]:
    calls = head.get("tool_calls")
    if calls is None:
        return False, "head tool calls are missing from the summary"
    names = [c.get("name") for c in calls if isinstance(c, dict)]
    unexpected = [name for name in names if name not in ("task", "todo")]
    if unexpected:
        return False, f"head made non-delegation tool call(s): {unexpected}"
    task_calls = [name for name in names if name == "task"]
    if len(task_calls) != 1:
        return False, f"head made {len(task_calls)} task call(s), expected exactly 1"

    delegations = head.get("delegations") or []
    if len(delegations) != 1:
        return False, f"head made {len(delegations)} task delegation(s), expected exactly 1"
    found = delegations[0]
    if int(found.get("tasks_count") or 0) != 1:
        return False, (f"task call carries {found.get('tasks_count')} tasks, "
                       "expected exactly 1")
    extra = found.get("extra_fields") or []
    if extra:
        return False, f"task call has unexpected field(s): {', '.join(map(str, extra))}"
    for field in pack_mod.DELEGATION_FIELDS:
        if found.get(field) != frozen.get(field):
            return False, f"delegation {field} differs from the frozen arguments"
    # Omitted is tolerated (omp accepts it); any other value would differ per head.
    if found.get("solution_space") not in (None, omp_mod.SOLUTION_SPACE):
        return False, "delegation solutionSpace differs from the replay value"
    return True, ""


def _identity_check(worker: dict, model: str) -> tuple[bool, str]:
    if worker.get("fallback"):
        return False, "model fallback was used"
    observed = worker.get("observed_models") or []
    if any(m != model for m in observed):
        return False, f"observed models {observed} != requested {model}"
    resolved = worker.get("resolved_model") or ""
    if not resolved:
        return False, "worker resolved model missing"
    if resolved.rsplit(":", 1)[0] != model:
        return False, f"resolved model {resolved} != requested {model}"
    return True, ""


def classify(summary: dict, launched: dict, model: str,
             frozen: dict) -> tuple[str, str]:
    summary = summary or {}
    head = summary.get("head") or {}
    worker = summary.get("worker")

    if worker is None:
        if _quota_text(head.get("error")):
            return "quota-paused", f"head hit quota: {head.get('error')}"
        if not head.get("session"):
            return "setup-error", f"no head session ({launched.get('rc')})"
        ok, why = _delegation_check(head, frozen)
        if not ok:
            return "invalid", why
        if launched.get("timed_out"):
            return "setup-error", "head wall-clock timeout without a worker session"
        return "setup-error", "worker session missing"

    # Identity and assignment are validated before any timeout is accepted:
    # a wrong or reassigned worker is invalid even if it ran out of time.
    ok, why = _delegation_check(head, frozen)
    if not ok:
        return "invalid", why
    ok, why = _identity_check(worker, model)
    if not ok:
        return "invalid", why

    terminal = worker.get("terminal_error") or {}
    kind = terminal.get("kind")
    message = str(terminal.get("message") or kind or "")
    if worker.get("runtime_aborted"):
        return "timeout", message or "worker exceeded task.maxRuntimeMs"
    if kind == "quota":
        return "quota-paused", message
    if kind == "timeout":
        return "timeout", message or "worker timeout"
    if launched.get("timed_out"):
        return "timeout", "runner wall-clock timeout"
    if kind == "provider":
        return "setup-error", message
    if worker.get("completed"):
        return "completed", ""
    errors = worker.get("errors") or []
    if errors:
        return "setup-error", str(errors[-1].get("message") or "worker error")
    return "invalid", "worker did not complete"


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
        isolation = sandbox_mod.proof(first["run_dir"], first["deps_nm"],
                                      first["rules"], home=home)
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
    """Peak simultaneous runs during each run's interval (all batches in HOME)."""
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

    # Count at each change point with closed intervals: starts before ends.
    per_time: dict[datetime, list[int]] = {}
    for start, end in intervals.values():
        per_time.setdefault(start, [0, 0])[0] += 1
        per_time.setdefault(end, [0, 0])[1] += 1
    running = 0
    active_at: dict[datetime, int] = {}
    for moment in sorted(per_time):
        starts, ends = per_time[moment]
        running += starts
        active_at[moment] = running
        running -= ends

    for run_dir, (start, end) in intervals.items():
        if run_dir.parent != batch_dir:
            continue
        count = max((value for moment, value in active_at.items()
                     if start <= moment <= end), default=1)
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

    current = _baseline_scores(home, batch["batch_id"], grades)
    quarantined, reason = False, None
    if batch.get("accepted"):
        current = {}
    if current:
        reference = _baseline_reference(home, batch, grades)
        if reference:
            common = sorted(set(reference) & set(current))
        else:
            common = []
        if common:
            now = sum(current[p] for p in common) / len(common)
            previous = sum(reference[p] for p in common) / len(common)
            moved_pp = abs(now - previous) * 100
            if moved_pp > protocol["baseline_drift_pp"]:
                quarantined = True
                reason = (f"baseline moved {moved_pp:.1f}pp "
                          f"({previous:.3f} -> {now:.3f}); "
                          f"limit {protocol['baseline_drift_pp']}pp")
            batch["baseline_score"] = round(now, 4)
        else:
            batch["baseline_score"] = round(
                sum(current.values()) / len(current), 4)

    batch["quarantined"] = quarantined
    batch["quarantine_reason"] = reason
    home_mod.write_json(batch_dir / "batch.json", batch)
    return batch


def _baseline_scores(home: Path, batch_id: str,
                     grades: dict[str, dict]) -> dict[str, float]:
    """Equal-weight baseline score per pack for one batch."""
    by_pack: dict[str, list[float]] = {}
    for run in _batch_runs(Path(home) / "runs" / batch_id):
        if not run.get("baseline") or run.get("outcome") not in SCORED_OUTCOMES:
            continue
        record = grades.get(run.get("run_id"))
        if not record or not isinstance(record.get("score"), (int, float)):
            continue
        by_pack.setdefault(run.get("pack"), []).append(float(record["score"]))
    return {pack: sum(scores) / len(scores) for pack, scores in by_pack.items()}


def _baseline_reference(home: Path, batch: dict,
                        grades: dict[str, dict]) -> dict[str, float]:
    """Baseline scores of the latest earlier accepted comparable batch.

    Comparable = same role and protocol, and for scout the same judge. Scores
    are returned per pack so drift compares the common pack set with equal
    weight per pack.
    """
    runs_root = Path(home) / "runs"
    if not runs_root.is_dir():
        return {}
    candidates = []
    current_order = (batch.get("created_at") or "", batch["batch_id"])
    for other_dir in (p for p in runs_root.iterdir() if p.is_dir()):
        if other_dir.name == batch["batch_id"]:
            continue
        other = home_mod.read_json(other_dir / "batch.json") or {}
        if not other.get("accepted") or other.get("quarantined"):
            continue
        if other.get("role") != batch.get("role"):
            continue
        if other.get("protocol") != batch.get("protocol"):
            continue
        if batch.get("role") == "scout" and (other.get("judge") or {}) != (batch.get("judge") or {}):
            continue
        if (other.get("created_at") or "", other_dir.name) >= current_order:
            continue
        candidates.append((other.get("created_at") or "", other_dir.name, other_dir))
    if not candidates:
        return {}
    _, _, reference_dir = max(candidates)
    return _baseline_scores(home, reference_dir.name, grades)


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
    runs = _batch_runs(Path(home) / "runs" / batch_id)
    by_slot: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        key = (run.get("pack"), (run.get("config") or {}).get("key"))
        by_slot.setdefault(key, []).append(run)

    packs = batch.get("packs") or sorted({pack for pack, _ in by_slot})
    wanted = max(1, int(batch.get("attempts") or 1))
    specs: list[tuple[str, str, int]] = []
    for pack_id in packs:
        for key in batch.get("configs") or []:
            existing = by_slot.get((pack_id, key), [])
            scored = sum(1 for r in existing
                         if r.get("outcome") in SCORED_OUTCOMES)
            attempt = max((int(r.get("attempt") or 0) for r in existing), default=0) + 1
            for _ in range(max(0, wanted - scored)):
                specs.append((pack_id, key, attempt))
                attempt += 1
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
    result = sandbox_mod.proof(probe, deps_nm, rules, home=home)
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
