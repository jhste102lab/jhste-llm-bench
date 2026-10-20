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


def build_profile(home: Path, run_dir: Path, deps_dir: Path | None,
                  rules: list[dict]) -> str:
    """SBPL text: allow everything, deny the forbidden paths (current run excepted).

    Only file *contents* and directory listings are denied (`file-read-data`).
    Metadata stays readable so realpath/lstat of ancestor directories works;
    with `file-read*` node module resolution and vitest fail inside the run.
    """
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

    # Last match wins: traversal into the excepted subtrees stays readable.
    for allowed in (run_s, deps_s):
        if allowed:
            lines.append(f"(allow file-read* (subpath {_quote(allowed)}))")

    return "\n".join(lines) + "\n"


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
          env: dict | None = None) -> dict:
    """Run the sandbox probes once per batch.

    Every forbidden path that exists must be denied, the run repository and the
    deps install must be readable, and GitHub tokens must be absent.
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
