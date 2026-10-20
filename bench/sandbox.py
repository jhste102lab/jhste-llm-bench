"""sandbox-exec profile rendering and the isolation proof."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .rules import path_rules

PROBE_TIMEOUT = 30


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and not os.path.isdir(path)
    except OSError:
        return False


def _inside(child: str, parent: str) -> bool:
    child = os.path.normpath(child)
    parent = os.path.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _protected_write_trees(home_s: str, run_s: str, deps_s: str | None) -> list[str]:
    """Trees the worker may not write: the bench home (minus the run), the
    shared dependency install and the public checkout root."""
    code = str(Path("~/code").expanduser().resolve())
    deny: list[str] = []
    if _inside(run_s, home_s):
        deny.append(f"(deny file-write* (require-all (subpath {_quote(home_s)}) "
                    f"(require-not (subpath {_quote(run_s)}))))")
    else:
        deny.append(f"(deny file-write* (subpath {_quote(home_s)}))")
    for tree in (deps_s, code):
        if tree:
            deny.append(f"(deny file-write* (subpath {_quote(tree)}))")
    return deny


def _render(home: Path, run_dir: Path, deps_dir: Path | None,
            rules: list[dict], readable: list[Path] | None = None) -> str:
    home_s = str(Path(home).resolve())
    run_s = str(Path(run_dir).resolve())
    deps_s = str(Path(deps_dir).resolve()) if deps_dir else None
    lines = ["(version 1)", "(allow default)"]

    def deny_path(value: str) -> None:
        exceptions = [p for p in (run_s, deps_s) if p and _inside(p, value)]
        if _is_file(value):
            form = f"(literal {_quote(value)})"
        else:
            form = f"(subpath {_quote(value)})"
            for exc in exceptions:
                form = f"(require-all {form} (require-not (subpath {_quote(exc)})))"
        lines.append(f"(deny file-read-data {form})")

    # Home stays private except this run (and the shared dependency install).
    deny_path(home_s)
    for rule in path_rules(rules):
        deny_path(rule["value"])

    # Re-allow the excepted subtrees. The allow must name the same operation as
    # the denies: SBPL prefers the more specific `file-read-data` over `file-read*`
    # regardless of order, so an `allow file-read*` would not override them.
    for allowed in [run_s, deps_s] + [str(Path(p).resolve()) for p in (readable or [])]:
        if allowed:
            lines.append(f"(allow file-read-data (subpath {_quote(allowed)}))")

    # Writes: only the run dir (repo, tmp, grading output) stays open. The
    # sandbox matches resolved paths, so repo/node_modules symlinks into the
    # shared install are covered by the dependency deny.
    lines.extend(_protected_write_trees(home_s, run_s, deps_s))
    return "\n".join(lines) + "\n"


def build_profile(home: Path, run_dir: Path, deps_dir: Path | None,
                  rules: list[dict]) -> str:
    """Worker SBPL profile: read-denies everywhere forbidden, write-denies the
    protected trees while keeping the run dir writable for omp sessions/tmp."""
    return _render(home, run_dir, deps_dir, rules)


def grading_profile(home: Path, run_dir: Path, deps_dir: Path | None,
                    pack_dir: Path, rules: list[dict]) -> str:
    """Grading SBPL profile: the worker read rules plus its own pack dir.

    Writes stay confined to the run dir (repo, tmp, grading output) and the
    system temp/cache paths node and vitest need; the bench home outside the
    run, the shared install and the public checkout stay write-denied.
    """
    return _render(home, run_dir, deps_dir, rules, readable=[Path(pack_dir)])


def wrap(cmd: list[str], profile_path: Path) -> list[str]:
    """Run ``cmd`` under a sandbox profile with GitHub tokens removed."""
    return ["sandbox-exec", "-f", str(profile_path),
            "env", "-u", "GITHUB_TOKEN", "-u", "GH_TOKEN", *cmd]


def write_profile(run_dir: Path, profile: str) -> Path:
    path = Path(run_dir) / "sandbox.sb"
    path.write_text(profile)
    return path


def launch_env(base: dict | None = None) -> dict:
    """Environment for the sandboxed omp process: GitHub tokens removed."""
    env = dict(os.environ if base is None else base)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env.setdefault("NO_COLOR", "1")
    return env


def _run(cmd: list[str], env: dict) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=PROBE_TIMEOUT)
        return {"rc": proc.returncode, "out": proc.stdout, "err": proc.stderr}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"rc": -1, "out": "", "err": str(exc)}


def _denial(res: dict) -> tuple[bool, str]:
    text = (res["err"] + res["out"]).strip()
    denied = res["rc"] != 0 and ("not permitted" in text.lower()
                                 or "permission denied" in text.lower()
                                 or "denied" in text.lower())
    return denied, text.splitlines()[-1] if text else ""


def proof(run_dir: Path, deps_dir: Path | None, rules: list[dict],
          env: dict | None = None, home: Path | None = None) -> dict:
    """Run the sandbox probes once per batch.

    Every forbidden path that exists must be denied, the run repository and
    deps install must be readable, writes to the protected trees must fail
    while the run repo stays writable, and GitHub tokens must be absent.
    """
    run_dir = Path(run_dir)
    profile = run_dir / "sandbox.sb"
    if not profile.is_file():
        return {"ok": False, "checked": 0,
                "error": f"missing sandbox profile {profile}", "checks": []}
    if not shutil.which("sandbox-exec"):
        return {"ok": False, "checked": 0, "error": "sandbox-exec not available",
                "checks": []}

    env = launch_env(env)
    checks: list[dict] = []
    base = ["sandbox-exec", "-f", str(profile)]

    seen: set[str] = set()
    for rule in path_rules(rules):
        target = os.path.realpath(rule["value"])
        if target in seen or not os.path.exists(target):
            continue
        seen.add(target)
        cmd = base + (["/bin/cat", target] if _is_file(target)
                      else ["/bin/ls", target])
        denied, detail = _denial(_run(cmd, env))
        checks.append({"kind": "deny", "category": rule["category"],
                       "target": target, "ok": denied, "detail": detail})

    readable = [("repo", run_dir / "repo")]
    if deps_dir:
        readable.append(("deps", Path(deps_dir)))
    for label, path in readable:
        if not Path(path).is_dir():
            checks.append({"kind": "allow", "category": label, "target": str(path),
                           "ok": False, "detail": "missing"})
            continue
        res = _run(base + ["/bin/ls", str(path)], env)
        checks.append({"kind": "allow", "category": label, "target": str(path),
                       "ok": res["rc"] == 0,
                       "detail": "" if res["rc"] == 0 else res["err"].strip()[-300:]})

    # Ancestors of the run must stay stat-able (node realpath walks every component).
    for ancestor in run_dir.resolve().parents:
        if str(ancestor) == "/":
            continue
        res = _run(base + ["/usr/bin/stat", "-f", "%N", str(ancestor)], env)
        if res["rc"] != 0:
            checks.append({"kind": "allow", "category": "traversal", "target": str(ancestor),
                           "ok": False, "detail": res["err"].strip()[-300:]})
            break
    else:
        checks.append({"kind": "allow", "category": "traversal",
                       "target": str(run_dir.resolve()), "ok": True, "detail": ""})

    # Writes: the protected trees must reject, the run repository must accept.
    probe_name = f".bench-write-probe-{os.getpid()}"
    targets: list[tuple[str, Path]] = []
    if home is not None:
        targets.append(("home", Path(home) / probe_name))
    if deps_dir:
        targets.append(("deps", Path(deps_dir) / probe_name))
    code = Path("~/code").expanduser()
    if code.is_dir():
        targets.append(("code", code / probe_name))
    for label, target in targets:
        if not target.parent.is_dir():
            continue
        res = _run(base + ["/usr/bin/touch", str(target)], env)
        denied, detail = _denial(res)
        try:
            target.unlink()
        except OSError:
            pass
        checks.append({"kind": "write-deny", "category": label,
                       "target": str(target), "ok": denied, "detail": detail})

    repo = run_dir / "repo"
    if repo.is_dir():
        target = repo / probe_name
        res = _run(base + ["/usr/bin/touch", str(target)], env)
        try:
            target.unlink()
        except OSError:
            pass
        checks.append({"kind": "write-allow", "category": "repo",
                       "target": str(target), "ok": res["rc"] == 0,
                       "detail": "" if res["rc"] == 0 else res["err"].strip()[-300:]})

    res = _run(base + ["/usr/bin/env"], env)
    leaked = [line for line in res["out"].splitlines()
              if line.startswith("GITHUB_TOKEN=") or line.startswith("GH_TOKEN=")]
    checks.append({"kind": "env", "category": "credential", "target": "tokens",
                   "ok": res["rc"] == 0 and not leaked,
                   "detail": "present" if leaked else ""})

    ok = all(c["ok"] for c in checks) and bool(checks)
    return {"ok": ok, "checked": len(checks), "checks": checks}


def summary(proof_result: dict) -> dict:
    return {"ok": bool(proof_result.get("ok")), "checked": int(proof_result.get("checked") or 0)}
