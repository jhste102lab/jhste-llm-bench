"""Bench home, protocol and pack locations.

The public repository never stores private paths: everything specific lives in
``$BENCH_HOME/private.json`` and is read at runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIR = REPO_ROOT / "protocol"
DEFAULT_PROTOCOL = "v2"


def bench_home() -> Path:
    """Private bench home; ``BENCH_HOME`` overrides the machine default."""
    override = os.environ.get("BENCH_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home().joinpath(".omp", "experiments", "jhste-llm-bench")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: object) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def load_protocol(version: str = DEFAULT_PROTOCOL) -> dict:
    path = PROTOCOL_DIR / f"{version}.json"
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"cannot read protocol {path}")
    return data


def load_private(home: Path | None = None) -> dict:
    home = Path(home) if home else bench_home()
    path = home / "private.json"
    if not path.is_file():
        raise SystemExit(
            f"missing {path.name} in the bench home (set BENCH_HOME or create it)")
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"cannot parse {path}")
    return data


def pack_dir(home: Path, role: str, pack_id: str) -> Path:
    return Path(home) / "packs" / role / pack_id


def iter_packs(home: Path, role: str) -> list[str]:
    root = Path(home) / "packs" / role
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / "pack.json").is_file())


def iter_runs(home: Path) -> Iterator[Path]:
    """Run directories that contain a run.json, in stable order."""
    runs_root = Path(home) / "runs"
    if not runs_root.is_dir():
        return
    for batch in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run in sorted(p for p in batch.iterdir() if p.is_dir()):
            if (run / "run.json").is_file():
                yield run


def find_run(home: Path, run_id: str) -> Path:
    for run in iter_runs(home):
        if run.name == run_id:
            return run
    raise SystemExit(f"no run {run_id!r} under {Path(home) / 'runs'}")


def deps_dir(home: Path, private: dict, name: str) -> Path:
    """Resolve a pack's ``deps`` name to the shared ``node_modules`` dir."""
    rel = (private.get("deps") or {}).get(name)
    if not rel:
        raise SystemExit(f"private.json has no deps entry {name!r}")
    path = Path(home) / rel
    if not path.is_dir():
        raise SystemExit(f"deps dir missing: {path}")
    return path


def deps_lock_path(home: Path, private: dict, name: str) -> Path:
    rel = (private.get("deps") or {}).get(name)
    if not rel:
        raise SystemExit(f"private.json has no deps entry {name!r}")
    return Path(home) / Path(rel).parent / "package-lock.json"
