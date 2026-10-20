"""Manifest lock: protocol + pack trees + omp version + dependency lockfile."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from . import home as home_mod
from . import omp as omp_mod


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hashes(root: Path, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """Relative path -> sha256 for every file below ``root`` (no dirs)."""
    root = Path(root)
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if any(rel == s or rel.startswith(s.rstrip("/") + "/") for s in skip):
                continue
            if path.is_symlink():
                out[rel] = sha256_bytes(("symlink:" + os.readlink(path)).encode())
            elif path.is_file():
                out[rel] = sha256_file(path)
    return out


def tree_digest(files: dict[str, str]) -> str:
    body = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(files.items()))
    return sha256_bytes(body.encode())


def pack_digest(pack_path: Path) -> dict:
    files = file_hashes(pack_path)
    individual = {rel: digest for rel, digest in files.items()
                  if not rel.startswith("source/")}
    return {"tree": tree_digest(files), "files": individual}


def _packs(home: Path) -> dict[str, dict]:
    packs: dict[str, dict] = {}
    root = Path(home) / "packs"
    if not root.is_dir():
        return packs
    for role_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for pack in sorted(p for p in role_dir.iterdir() if p.is_dir()):
            if not (pack / "pack.json").is_file():
                continue
            packs[f"{role_dir.name}/{pack.name}"] = pack_digest(pack)
    return packs


def _deps(home: Path, private: dict) -> dict[str, dict]:
    deps: dict[str, dict] = {}
    for name in sorted((private.get("deps") or {})):
        lock = home_mod.deps_lock_path(home, private, name)
        deps[name] = {"lock": str(lock.relative_to(home)),
                      "sha256": sha256_file(lock) if lock.is_file() else None}
    return deps


def compute(home: Path, protocol: dict, private: dict) -> dict:
    protocol_file = home_mod.PROTOCOL_DIR / f"{protocol['version']}.json"
    return {
        "version": 1,
        "created_at": utc_now(),
        "protocol": {"version": protocol["version"],
                     "file": f"protocol/{protocol['version']}.json",
                     "sha256": sha256_file(protocol_file)},
        "omp_version": protocol["omp_version"],
        "packs": _packs(home),
        "deps": _deps(home, private),
    }


def write_lock(home: Path, protocol: dict, private: dict) -> Path:
    lock = compute(home, protocol, private)
    path = Path(home) / "manifest.lock.json"
    home_mod.write_json(path, lock)
    return path


def load_lock(home: Path) -> dict:
    path = Path(home) / "manifest.lock.json"
    data = home_mod.read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"missing manifest lock {path}; run `python3 -m bench lock`")
    return data


def manifest_sha256(home: Path) -> str:
    return sha256_file(Path(home) / "manifest.lock.json")


def drift(home: Path, protocol: dict, private: dict, lock: dict | None = None) -> list[str]:
    """Differences between the lock and the live tree; empty means unchanged."""
    lock = lock if lock is not None else load_lock(home)
    problems: list[str] = []
    protocol_file = home_mod.PROTOCOL_DIR / f"{protocol['version']}.json"
    current = sha256_file(protocol_file)
    if (lock.get("protocol") or {}).get("sha256") != current:
        problems.append(f"protocol/{protocol['version']}.json changed")

    installed = omp_mod.version()
    if installed != protocol["omp_version"]:
        problems.append(f"omp version {installed} != pinned {protocol['omp_version']}")
    if lock.get("omp_version") != protocol["omp_version"]:
        problems.append(f"manifest omp version {lock.get('omp_version')} != pinned "
                        f"{protocol['omp_version']}")

    locked_packs = lock.get("packs") or {}
    current_packs = _packs(home)
    for key in sorted(set(locked_packs) | set(current_packs)):
        old = locked_packs.get(key)
        new = current_packs.get(key)
        if old is None:
            problems.append(f"pack {key} added after lock")
        elif new is None:
            problems.append(f"pack {key} missing")
        else:
            if old.get("tree") != new.get("tree"):
                problems.append(f"pack {key} tree changed")
            for rel in sorted(set(old.get("files") or {}) | set(new.get("files") or {})):
                if (old.get("files") or {}).get(rel) != (new.get("files") or {}).get(rel):
                    problems.append(f"pack {key}/{rel} changed")

    locked_deps = lock.get("deps") or {}
    current_deps = _deps(home, private)
    for name in sorted(set(locked_deps) | set(current_deps)):
        old = locked_deps.get(name)
        new = current_deps.get(name)
        if old is None or new is None or old.get("sha256") != new.get("sha256"):
            problems.append(f"deps {name} lockfile changed")
    return problems


def verify(home: Path, protocol: dict, private: dict) -> dict:
    """Refuse on drift; returns the lock."""
    lock = load_lock(home)
    problems = drift(home, protocol, private, lock)
    if problems:
        raise SystemExit("manifest drift (re-run `python3 -m bench lock` after review):\n  "
                         + "\n  ".join(problems))
    return lock
