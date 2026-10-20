"""Packs: loading, source snapshot (git archive) and frozen delegations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from . import home as home_mod

DELEGATION_FIELDS = ("agent", "context", "task")


def load(home: Path, role: str, pack_id: str) -> dict:
    path = home_mod.pack_dir(home, role, pack_id)
    data = home_mod.read_json(path / "pack.json")
    if not isinstance(data, dict):
        raise SystemExit(f"missing or broken pack {role}/{pack_id}: {path / 'pack.json'}")
    if data.get("role") not in (None, role) or data.get("id") not in (None, pack_id):
        raise SystemExit(f"pack {path} does not declare role {role!r} id {pack_id!r}")
    data.setdefault("role", role)
    data.setdefault("id", pack_id)
    return data


def require_source(home: Path, role: str, pack_id: str) -> Path:
    source = home_mod.pack_dir(home, role, pack_id) / "source"
    if not source.is_dir() or not any(source.iterdir()):
        raise SystemExit(f"pack {role}/{pack_id} has no source/; run `bench pack snapshot`")
    if (source / ".git").exists():
        raise SystemExit(f"pack {role}/{pack_id} source contains .git; snapshots stay history-free")
    return source


def delegation_sha256(delegation: dict) -> str:
    body = json.dumps({f: delegation[f] for f in DELEGATION_FIELDS},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def frozen_delegation(home: Path, role: str, pack_id: str) -> dict:
    path = home_mod.pack_dir(home, role, pack_id) / "delegation.json"
    data = home_mod.read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"pack {role}/{pack_id} has no frozen delegation; run `bench freeze`")
    missing = [f for f in DELEGATION_FIELDS if not isinstance(data.get(f), str)]
    if missing:
        raise SystemExit(f"{path} lacks {', '.join(missing)}")
    if data.get("sha256") and data["sha256"] != delegation_sha256(data):
        raise SystemExit(f"{path} content does not match its sha256")
    return data


def snapshot(home: Path, private: dict, role: str, pack_id: str,
             force: bool = False) -> dict:
    """``git archive`` the pack's base_ref into ``source/`` (no history)."""
    pack = load(home, role, pack_id)
    repo_name = pack.get("source_repo")
    repo = (private.get("source_repos") or {}).get(repo_name)
    if not repo:
        raise SystemExit(f"private.json has no source_repos entry {repo_name!r}")
    base_ref = pack.get("base_ref")
    if not base_ref:
        raise SystemExit(f"pack {role}/{pack_id} has no base_ref")
    repo_path = Path(repo)
    if not (repo_path / ".git").exists():
        raise SystemExit(f"source repo {repo_name!r} is not a git repository")

    resolved = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        capture_output=True, text=True)
    if resolved.returncode != 0:
        raise SystemExit(f"cannot resolve {base_ref} in {repo_name!r}: "
                         f"{resolved.stderr.strip()}")

    dest = home_mod.pack_dir(home, role, pack_id) / "source"
    if dest.exists() and any(dest.iterdir()):
        if not force:
            raise SystemExit(f"{dest} already has content; use --force to replace")
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    archive = subprocess.run(["git", "-C", str(repo_path), "archive", "--format=tar",
                              resolved.stdout.strip()], capture_output=True)
    if archive.returncode != 0:
        raise SystemExit(f"git archive failed: {archive.stderr.decode()[-500:]}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")

    return {"pack": f"{role}/{pack_id}", "source": str(dest),
            "base_ref": base_ref, "commit": resolved.stdout.strip(),
            "files": sum(len(files) for _, _, files in os.walk(dest))}
