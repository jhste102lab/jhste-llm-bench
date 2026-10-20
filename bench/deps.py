"""Thin per-run ``node_modules``: shared install symlinked, workspaces local.

Ported from the v1 trial linker, parameterised by the shared deps directory
(``HOME/deps/<name>/node_modules`` from private.json). Ordinary packages become
symlinks into the shared install; workspace packages become symlinks to the
run repository's own source, so a run never resolves another checkout.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
from pathlib import Path

MARKER = ".bench-node-modules.json"


def workspace_packages(repo: Path) -> dict[str, Path]:
    package_json = Path(repo) / "package.json"
    if not package_json.is_file():
        raise RuntimeError(f"link_deps: {package_json} missing")
    try:
        data = json.loads(package_json.read_text())
    except ValueError as exc:
        raise RuntimeError(f"link_deps: cannot parse {package_json}: {exc}") from exc
    found: dict[str, Path] = {}
    for pattern in data.get("workspaces") or []:
        for rel in sorted(glob.glob(str(Path(repo) / pattern))):
            manifest = Path(rel) / "package.json"
            if not manifest.is_file():
                continue
            try:
                name = json.loads(manifest.read_text()).get("name")
            except ValueError:
                continue
            if name:
                found[name] = Path(rel).absolute()
    return found


def _is_generated(nm: Path) -> bool:
    return (nm / MARKER).is_file()


def _clear_generated(path: Path, force: bool) -> None:
    """Remove a bench-generated tree; refuse to delete a foreign install."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if not _is_generated(path) and not force:
        raise RuntimeError(f"link_deps: {path} exists and is not bench-generated")
    shutil.rmtree(path)


def _link_tree(shared_nm: Path, target_nm: Path,
               workspace: dict[str, Path], warnings: list[str]) -> None:
    target_nm.mkdir(parents=True, exist_ok=True)
    for entry in sorted(shared_nm.iterdir(), key=lambda p: p.name):
        dest = target_nm / entry.name
        if entry.is_dir() and entry.name.startswith("@"):
            dest.mkdir(exist_ok=True)
            for pkg in sorted(entry.iterdir(), key=lambda p: p.name):
                if pkg.name.startswith("."):
                    continue
                src = workspace.get(f"{entry.name}/{pkg.name}")
                if src is None:
                    src = pkg.resolve()
                (dest / pkg.name).symlink_to(src, target_is_directory=src.is_dir())
            continue
        if entry.name in workspace:
            (target_nm / entry.name).symlink_to(workspace[entry.name],
                                                target_is_directory=True)
            continue
        dest.symlink_to(entry.resolve(), target_is_directory=entry.is_dir())
    (target_nm / MARKER).write_text(json.dumps(
        {"shared": str(shared_nm), "generated": True}, indent=2) + "\n")


def link_deps(repo: Path, shared_node_modules: Path, *, force: bool = False) -> dict:
    """Prepare ``<repo>/node_modules``; raises RuntimeError on unsafe state."""
    repo = Path(repo).resolve()
    shared_nm = Path(shared_node_modules).resolve()
    if not (repo / "package.json").is_file():
        raise RuntimeError(f"link_deps: {repo}/package.json missing")
    if not shared_nm.is_dir():
        raise RuntimeError(f"link_deps: shared install {shared_nm} missing")

    node_modules = repo / "node_modules"
    if node_modules.exists() or node_modules.is_symlink():
        _clear_generated(node_modules, force)

    workspace = workspace_packages(repo)
    warnings: list[str] = []
    for name, src in workspace.items():
        real = Path(os.path.realpath(src))
        if not (real == repo or str(real).startswith(str(repo) + os.sep)):
            raise RuntimeError(
                f"link_deps: workspace package {name} resolves outside {repo}: {real}")
    _link_tree(shared_nm, node_modules, workspace, warnings)

    nested: list[str] = []
    for pattern in _workspace_patterns(repo):
        for rel in sorted(glob.glob(str(repo / pattern))):
            rel_path = Path(rel).relative_to(repo)
            shared_sub = shared_nm.parent / rel_path / "node_modules"
            if shared_sub.is_dir():
                nested_nm = repo / rel_path / "node_modules"
                if nested_nm.exists() or nested_nm.is_symlink():
                    _clear_generated(nested_nm, force)
                _link_tree(shared_sub, nested_nm, workspace, warnings)
                nested.append(str(rel_path / "node_modules"))

    resolved: dict[str, str] = {}
    for name, src in workspace.items():
        link = node_modules / name
        if link.exists() or link.is_symlink():
            real = Path(os.path.realpath(link))
            resolved[name] = str(real)
            if not (real == src or str(real).startswith(str(repo) + os.sep)):
                raise RuntimeError(
                    f"link_deps: workspace package {name} resolves outside {repo}: {real}")

    (node_modules / MARKER).write_text(json.dumps(
        {"shared": str(shared_nm), "repo": str(repo), "nested": nested}, indent=2) + "\n")
    return {"node_modules": str(node_modules), "shared": str(shared_nm),
            "workspace": resolved, "nested": nested, "warnings": warnings}


def _workspace_patterns(repo: Path) -> list[str]:
    try:
        data = json.loads((repo / "package.json").read_text())
    except (OSError, ValueError):
        return []
    patterns = [p for p in (data.get("workspaces") or []) if isinstance(p, str)]
    return patterns or ["apps/*", "packages/*"]
