"""Unit tests: rules, deps, manifest, pack snapshot, omp command, sandbox profile."""

from __future__ import annotations

import json
import sys
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_bench  # noqa: E402
from bench import deps, home as home_mod, manifest, omp, pack, rules, sandbox


class RulesTest(unittest.TestCase):
    def test_private_paths_domains_and_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            run_dir = home / "runs" / "b1" / "r1"
            run_dir.mkdir(parents=True)
            other = home / "runs" / "b1" / "r2"
            (other / "repo").mkdir(parents=True)
            (home / "packs").mkdir()
            (home / "grades.jsonl").write_text("")
            private = {
                "forbidden_paths": [{"category": "answer", "path": "/tmp/answer-zone"}],
                "forbidden_domains": [{"category": "production", "pattern": "example.com"}],
            }
            found = rules.forbidden_rules(home, run_dir, private)
            paths = [r for r in found if r["kind"] == "path"]
            values = {r["value"] for r in paths}
            self.assertIn("/tmp/answer-zone", values)
            self.assertIn("/private/tmp/answer-zone", values)
            self.assertIn(str((home / "packs").resolve()), values)
            self.assertIn(str((home / "grades.jsonl").resolve()), values)
            self.assertIn(str(other.resolve()), values)
            self.assertNotIn(str(run_dir.resolve()), values)
            self.assertEqual([r for r in found if r["kind"] == "domain"],
                             [{"category": "production", "kind": "domain",
                               "value": "example.com"}])
            categories = {r["category"] for r in found}
            self.assertTrue({"answer", "evaluator", "other-run"} <= categories)

    def test_missing_home_has_no_other_run_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            found = rules.forbidden_rules(home, home / "runs" / "b" / "r",
                                          {"forbidden_paths": []})
            self.assertEqual([r for r in found if r["category"] == "other-run"], [])


class DepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        fixture = fake_bench.build_home(self.root)
        self.shared = fixture["home"] / "deps" / "demo" / "node_modules"
        self.repo = self.root / "repo"
        (self.repo / "packages" / "lib" / "src").mkdir(parents=True)
        (self.repo / "package.json").write_text(
            json.dumps({"name": "app", "workspaces": ["packages/*"]}))
        (self.repo / "packages" / "lib" / "package.json").write_text(
            '{"name": "@acme/lib"}')
        (self.repo / "packages" / "lib" / "src" / "index.js").write_text("x\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shared_packages_symlink_workspaces_stay_local(self) -> None:
        result = deps.link_deps(self.repo, self.shared)
        node_modules = self.repo / "node_modules"
        self.assertEqual((node_modules / "lodash").resolve(),
                         (self.shared / "lodash").resolve())
        self.assertEqual((node_modules / "@acme" / "lib").resolve(),
                         (self.repo / "packages" / "lib").resolve())
        self.assertTrue((node_modules / deps.MARKER).is_file())
        self.assertEqual(result["workspace"]["@acme/lib"],
                         str((self.repo / "packages" / "lib").resolve()))
        self.assertEqual(result["warnings"], [])

    def test_foreign_node_modules_is_refused_then_replaced(self) -> None:
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules" / "prebuilt.txt").write_text("x")
        with self.assertRaises(RuntimeError):
            deps.link_deps(self.repo, self.shared)
        deps.link_deps(self.repo, self.shared, force=True)
        self.assertFalse((self.repo / "node_modules" / "prebuilt.txt").exists())
        self.assertTrue((self.repo / "node_modules" / deps.MARKER).is_file())

    def test_workspace_outside_repo_is_refused(self) -> None:
        outside = self.root / "outside" / "lib"
        outside.mkdir(parents=True)
        (outside / "package.json").write_text('{"name": "@acme/lib"}')
        shutil.rmtree(self.repo / "packages" / "lib")
        (self.repo / "packages" / "lib").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            deps.link_deps(self.repo, self.shared)

    def test_missing_shared_install_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            deps.link_deps(self.repo, self.root / "nowhere")


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fixture = fake_bench.build_home(self.root)
        self.home = self.fixture["home"]
        self.protocol = home_mod.load_protocol()
        self.private = home_mod.load_private(self.home)
        self.fake_omp = fake_bench.install_fake_omp(self.root)
        self._env = mock.patch.dict(os.environ, {"BENCH_OMP": str(self.fake_omp)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_lock_is_stable_and_detects_drift(self) -> None:
        first = manifest.compute(self.home, self.protocol, self.private)
        second = manifest.compute(self.home, self.protocol, self.private)
        self.assertEqual(first["packs"], second["packs"])
        self.assertEqual(set(first["packs"]), {"task/c1"})
        self.assertIn("pack.json", first["packs"]["task/c1"]["files"])
        self.assertNotIn("source/package.json", first["packs"]["task/c1"]["files"])

        manifest.write_lock(self.home, self.protocol, self.private)
        self.assertEqual(manifest.drift(self.home, self.protocol, self.private), [])

        (self.fixture["pack_dir"] / "request.md").write_text("edited\n")
        problems = manifest.drift(self.home, self.protocol, self.private)
        self.assertTrue(any("request.md" in p for p in problems), problems)

        (self.fixture["pack_dir"] / "source" / "src" / "app.js").write_text("edited\n")
        problems = manifest.drift(self.home, self.protocol, self.private)
        self.assertTrue(any("tree changed" in p for p in problems), problems)

    def test_new_pack_and_deps_lock_drift(self) -> None:
        manifest.write_lock(self.home, self.protocol, self.private)
        extra = self.home / "packs" / "task" / "c2"
        extra.mkdir()
        (extra / "pack.json").write_text('{"id": "c2", "role": "task"}\n')
        problems = manifest.drift(self.home, self.protocol, self.private)
        self.assertTrue(any("task/c2 added" in p for p in problems), problems)

        (self.home / "deps" / "demo" / "package-lock.json").write_text('{"x": 1}\n')
        problems = manifest.drift(self.home, self.protocol, self.private)
        self.assertTrue(any("deps demo" in p for p in problems), problems)

    def test_missing_lock_refuses(self) -> None:
        with self.assertRaises(SystemExit):
            manifest.load_lock(self.home)

    def test_version_mismatch_is_drift(self) -> None:
        manifest.write_lock(self.home, self.protocol, self.private)
        broken = dict(self.protocol)
        broken["omp_version"] = "0.0.1"
        problems = manifest.drift(self.home, broken, self.private)
        self.assertTrue(any("omp version" in p for p in problems), problems)


class PackTest(unittest.TestCase):
    def test_snapshot_extracts_historyless_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = fake_bench.build_home(root)
            home = fixture["home"]
            private = dict(fixture["private"])
            repo = root / "src-repo"
            repo.mkdir()
            (repo / "README.md").write_text("hello\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com",
                            "-c", "user.name=t", "commit", "-qm", "base"], check=True)
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            private["source_repos"] = {"demo": str(repo)}
            pack_file = fixture["pack_dir"] / "pack.json"
            meta = json.loads(pack_file.read_text())
            meta["base_ref"] = sha
            pack_file.write_text(json.dumps(meta) + "\n")

            result = pack.snapshot(home, private, "task", "c1", force=True)
            source = fixture["pack_dir"] / "source"
            self.assertEqual(result["commit"], sha)
            self.assertTrue((source / "README.md").is_file())
            self.assertFalse((source / ".git").exists())
            self.assertFalse((source / "src" / "app.js").exists())
            with self.assertRaises(SystemExit):
                pack.snapshot(home, private, "task", "c1")

    def test_delegation_digest_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = fake_bench.build_home(Path(tmp))
            frozen = pack.frozen_delegation(fixture["home"], "task", "c1")
            self.assertEqual(frozen["sha256"], pack.delegation_sha256(frozen))
            broken = dict(frozen)
            broken["task"] = "tampered"
            (fixture["pack_dir"] / "delegation.json").write_text(json.dumps(broken))
            with self.assertRaises(SystemExit):
                pack.frozen_delegation(fixture["home"], "task", "c1")


class OmpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = home_mod.load_protocol()

    def test_command_matches_verified_invocation(self) -> None:
        run_dir = Path("/tmp/bench-run")
        cmd = omp.build_command(run_dir, self.protocol, self.protocol["head"])
        self.assertEqual(cmd[:5], ["sandbox-exec", "-f", "/tmp/bench-run/sandbox.sb",
                                   "env", "-u"])
        self.assertEqual(cmd[5:7], ["GITHUB_TOKEN", "-u"])
        self.assertIn("--session-dir", cmd)
        self.assertEqual(cmd[cmd.index("--session-dir") + 1], "/tmp/bench-run/sessions")
        self.assertEqual(cmd[cmd.index("--cwd") + 1], "/tmp/bench-run/repo")
        self.assertEqual(cmd[cmd.index("--config") + 1], "/tmp/bench-run/overlay.yml")
        self.assertEqual(cmd[cmd.index("--model") + 1], self.protocol["head"]["model"])
        self.assertEqual(cmd[cmd.index("--thinking") + 1], self.protocol["head"]["effort"])
        self.assertEqual(cmd[cmd.index("--max-time") + 1],
                         f"{self.protocol['head_timeout_minutes']}m")
        self.assertEqual(cmd[-1], "@/tmp/bench-run/prompt.md")
        for flag in ("--no-title", "--no-prewalk", "--auto-approve", "--no-extensions",
                     "--no-skills", "--no-rules", "--no-lsp", "--no-pty", "-p"):
            self.assertIn(flag, cmd)

    def test_overlay_pins_worker_only(self) -> None:
        text = omp.overlay_text("scout", "provider/model", "low", self.protocol, 7)
        self.assertIn('scout: "provider/model:low"', text)
        self.assertIn("maxRuntimeMs: 420000", text)
        self.assertIn("maxRecursionDepth: 1", text)
        disabled = [line for line in text.splitlines() if "disabledAgents" in line][0]
        self.assertNotIn("scout", disabled)
        self.assertIn("task", disabled)
        self.assertIn('web: "' + self.protocol["web"]["model"] + '"', text)
        self.assertIn("modelFallback: false", text)
        self.assertIn("github: {enabled: false}", text)

    def test_prompts_replay_frozen_text(self) -> None:
        frozen = {"agent": "task", "context": "ctx", "task": "do the thing"}
        prompt = omp.replay_prompt("task", frozen)
        payload = json.loads(prompt.split("```json")[1].split("```")[0])
        self.assertEqual(payload["context"], "ctx")
        self.assertEqual(payload["tasks"], [{"agent": "task", "task": "do the thing"}])
        freeze = omp.freeze_prompt("scout", "explore the repository")
        self.assertIn("explore the repository", freeze)
        self.assertIn('agent "scout"', freeze)

    def test_launch_env_strips_github_tokens(self) -> None:
        env = sandbox.launch_env({"GITHUB_TOKEN": "a", "GH_TOKEN": "b", "PATH": "/bin"})
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_version_from_fake_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = fake_bench.install_fake_omp(Path(tmp))
            self.assertEqual(omp.version(str(fake)), "18.12.3")


class SandboxProfileTest(unittest.TestCase):
    def test_ancestor_rule_gets_exceptions_and_allows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            run_dir = home / "runs" / "b1" / "r1"
            run_dir.mkdir(parents=True)
            deps_dir = home / "deps" / "demo" / "node_modules"
            deps_dir.mkdir(parents=True)
            secret = Path(tmp) / "secret"
            secret.mkdir()
            found = [
                {"category": "history", "kind": "path", "value": str(home)},
                {"category": "answer", "kind": "path", "value": str(secret)},
                {"category": "production", "kind": "domain", "value": "example.com"},
            ]
            text = sandbox.build_profile(home, run_dir, deps_dir, found)
            lines = text.splitlines()
            self.assertEqual(lines[0], "(version 1)")
            self.assertEqual(lines[1], "(allow default)")
            home_rule = [line for line in lines if str(home) in line
                         and "require-all" in line][0]
            self.assertIn(f"(require-not (subpath \"{run_dir.resolve()}\"))", home_rule)
            self.assertTrue(any(f"(allow file-read* (subpath \"{run_dir.resolve()}\"))"
                                == line for line in lines))
            self.assertTrue(any("require-not" in line and str(deps_dir.resolve()) in line
                                for line in lines))
            self.assertNotIn("example.com", text)
            self.assertIn(f'(deny file-read-data (subpath "{secret}"))', text)

    def test_missing_profile_fails_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = sandbox.proof(Path(tmp), None, [])
            self.assertFalse(result["ok"])
            self.assertEqual(result["checked"], 0)


class ConcurrencyTest(unittest.TestCase):
    def test_overlap_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp) / "b1"
            spans = {"a": ("2026-10-20T00:00:00Z", "2026-10-20T00:10:00Z"),
                     "b": ("2026-10-20T00:05:00Z", "2026-10-20T00:15:00Z"),
                     "c": ("2026-10-20T00:20:00Z", "2026-10-20T00:25:00Z")}
            for name, (start, end) in spans.items():
                run_dir = batch_dir / name
                run_dir.mkdir(parents=True)
                (run_dir / "run.json").write_text(json.dumps({
                    "run_id": name, "started_at": start, "ended_at": end,
                    "concurrency": 0}))
            from bench import run as run_mod
            run_mod.update_concurrency(batch_dir)
            counts = {name: json.loads((batch_dir / name / "run.json").read_text())
                      ["concurrency"] for name in spans}
            self.assertEqual(counts, {"a": 2, "b": 2, "c": 1})


if __name__ == "__main__":
    unittest.main()
