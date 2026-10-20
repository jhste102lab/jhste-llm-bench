"""Command line: ``python3 -m bench <command>``."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import home as home_mod
from . import manifest as manifest_mod
from . import pack as pack_mod
from . import run as run_mod


def _print(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _judge(args, protocol) -> dict:
    return run_mod.judge_from_arg(getattr(args, "judge", None), protocol)


def cmd_lock(args) -> int:
    home, protocol = home_mod.bench_home(), home_mod.load_protocol()
    private = home_mod.load_private(home)
    path = manifest_mod.write_lock(home, protocol, private)
    _print({"lock": str(path), "sha256": manifest_mod.sha256_file(path),
            "packs": sorted(manifest_mod.load_lock(home).get("packs") or {}),
            "omp_version": protocol["omp_version"]})
    return 0


def cmd_freeze(args) -> int:
    home, protocol = home_mod.bench_home(), home_mod.load_protocol()
    private = home_mod.load_private(home)
    result = run_mod.freeze(home, protocol, private, args.role, args.pack,
                            force=args.force)
    _print(result)
    return 0


def cmd_isolation_check(args) -> int:
    home = home_mod.bench_home()
    private = home_mod.load_private(home)
    result = run_mod.isolation_check(home, private)
    checks = result.pop("checks", [])
    _print(result)
    for check in checks:
        mark = "ok" if check.get("ok") else "FAIL"
        print(f"  [{mark}] {check.get('kind')} {check.get('category')} "
              f"{check.get('target')}" + (f" — {check.get('detail')}" if check.get("detail") else ""))
    return 0 if result.get("ok") else 2


def cmd_pack_snapshot(args) -> int:
    home = home_mod.bench_home()
    private = home_mod.load_private(home)
    _print(pack_mod.snapshot(home, private, args.role, args.pack, force=args.force))
    return 0


def cmd_run(args) -> int:
    home, protocol = home_mod.bench_home(), home_mod.load_protocol()
    private = home_mod.load_private(home)
    if args.retry:
        judge = (run_mod.judge_from_arg(args.judge, protocol)
                 if args.judge else None)
        result = run_mod.retry(home, protocol, private, args.retry, judge, args.parallel)
        _print(result)
        return 0
    judge = _judge(args, protocol)
    if not args.role:
        raise SystemExit("--role is required unless --retry is used")
    models, efforts = args.model or [], args.effort or []
    if not models and args.no_baseline:
        raise SystemExit("--no-baseline needs at least one --model/--effort pair")
    if len(models) != len(efforts):
        raise SystemExit("each --model needs one --effort")
    if args.attempts < 1:
        raise SystemExit("--attempts must be >= 1")
    packs = [p.strip() for p in (args.packs or "").split(",") if p.strip()]
    result = run_mod.start(
        home, protocol, private, args.role, models, efforts, packs,
        args.attempts, args.parallel, not args.no_baseline, judge)
    _print(result)
    return 0


def cmd_grade(args) -> int:
    home, protocol = home_mod.bench_home(), home_mod.load_protocol()
    judge = _judge(args, protocol)
    run_dir = home_mod.find_run(home, args.run)
    record = run_mod._module("grade").grade_run(home, run_dir, protocol, judge)
    _print(record)
    return 0


def cmd_accept(args) -> int:
    _print(run_mod.accept(home_mod.bench_home(), args.batch))
    return 0


def cmd_export(args) -> int:
    home, protocol = home_mod.bench_home(), home_mod.load_protocol()
    path = run_mod._module("export").export(home, home_mod.REPO_ROOT / "docs", protocol)
    _print({"export": str(path)})
    return 0


def cmd_status(args) -> int:
    _print(run_mod.status(home_mod.bench_home()))
    return 0


def cmd_leak_hashes(args) -> int:
    home = home_mod.bench_home()
    private = home_mod.load_private(home)
    terms = [str(t).strip().lower() for t in private.get("private_terms") or [] if str(t).strip()]
    out = Path(args.out) if args.out else home_mod.REPO_ROOT / "protocol" / "leak-hashes.txt"
    lines = sorted({hashlib.sha256(term.encode()).hexdigest() for term in terms})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(line + "\n" for line in lines))
    _print({"hashes": str(out), "terms": len(lines)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench", description="jhste-llm-bench v2 runner")
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock", help="write manifest.lock.json for packs, protocol, omp and deps")
    lock.set_defaults(func=cmd_lock)

    freeze = sub.add_parser("freeze", help="freeze a pack delegation by running the head once")
    freeze.add_argument("--role", required=True, choices=["task", "scout"])
    freeze.add_argument("--pack", required=True)
    freeze.add_argument("--force", action="store_true", help="replace an existing delegation")
    freeze.set_defaults(func=cmd_freeze)

    iso = sub.add_parser("isolation-check", help="run the sandbox isolation proof")
    iso.set_defaults(func=cmd_isolation_check)

    pack = sub.add_parser("pack", help="pack maintenance")
    pack_sub = pack.add_subparsers(dest="pack_command", required=True)
    snapshot = pack_sub.add_parser("snapshot", help="git archive the pack base_ref into source/")
    snapshot.add_argument("--role", required=True, choices=["task", "scout"])
    snapshot.add_argument("--pack", required=True)
    snapshot.add_argument("--force", action="store_true")
    snapshot.set_defaults(func=cmd_pack_snapshot)

    run = sub.add_parser("run", help="run a batch of (pack, config, attempt) runs")
    run.add_argument("--role", choices=["task", "scout"])
    run.add_argument("--model", action="append", default=[])
    run.add_argument("--effort", action="append", default=[])
    run.add_argument("--packs", help="comma-separated pack ids (default: every pack)")
    run.add_argument("--attempts", type=int, default=1)
    run.add_argument("--parallel", type=int, default=None, help="max concurrent runs (default unlimited)")
    run.add_argument("--no-baseline", action="store_true", help="do not include the role baseline config")
    run.add_argument("--judge", help="judge model:effort for scout grading")
    run.add_argument("--retry", metavar="BATCH", help="retry retryable runs of an existing batch")
    run.set_defaults(func=cmd_run)

    grade = sub.add_parser("grade", help="grade one finished run")
    grade.add_argument("--run", required=True)
    grade.add_argument("--judge", help="judge model:effort for scout packs")
    grade.set_defaults(func=cmd_grade)

    accept = sub.add_parser("accept", help="accept a quarantined batch as a baseline reference")
    accept.add_argument("batch")
    accept.set_defaults(func=cmd_accept)

    export = sub.add_parser("export", help="write docs/data/v2.js")
    export.set_defaults(func=cmd_export)

    status = sub.add_parser("status", help="show packs and batches")
    status.set_defaults(func=cmd_status)

    hashes = sub.add_parser("leak-hashes", help="write sha256 hashes of private terms")
    hashes.add_argument("--out", help="output path (default protocol/leak-hashes.txt)")
    hashes.set_defaults(func=cmd_leak_hashes)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
