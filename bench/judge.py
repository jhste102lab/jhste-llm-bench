"""Blinded judge for scout checklist facts.

The report is scrubbed of model/vendor names before it reaches the judge, the
judge sees only ``fact id + statement``, and it must answer strict JSON. The
sha256 of the exact prompt is recorded with the grade.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

RUBRIC_VERSION = "scout-judge-1"

SCRUB_WORDS = ("Claude", "Anthropic", "GPT", "OpenAI", "Codex", "DeepSeek",
               "Sol", "Luna", "Opus", "Sonnet", "Haiku", "Fable", "Gemini")
_SCRUB_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in SCRUB_WORDS) + r")\b",
                       re.IGNORECASE)

VERDICTS = ("covered", "missing", "contradicted")

INSTRUCTIONS = """You are grading a read-only repository exploration report against checklist facts.

For every fact decide exactly one verdict:
- "covered": the report states the fact (paraphrase is fine).
- "missing": the report does not state it.
- "contradicted": the report states something incompatible with it.

Judge only what the report says. Do not use outside knowledge about the repository.
Answer with strict JSON only, no prose and no code fence:
{"verdicts": [{"id": "<fact id>", "verdict": "covered|missing|contradicted", "evidence": "<short quote from the report, or empty>"}]}
Every fact id must appear exactly once."""


def scrub(text: str) -> str:
    """Remove model and vendor names so the judge cannot infer the worker."""
    return _SCRUB_RE.sub("model", text or "")


def build_prompt(report: str, facts: list[dict]) -> str:
    """Deterministic judge prompt; its sha256 is what a grade records."""
    payload = [{"id": f.get("id"), "statement": f.get("statement") or f.get("why") or ""}
               for f in facts]
    return (
        INSTRUCTIONS
        + "\n\nFacts (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n<report>\n"
        + scrub(report)
        + "\n</report>\n"
    )


def prompt_sha256(report: str, facts: list[dict]) -> str:
    return hashlib.sha256(build_prompt(report, facts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# omp process
# --------------------------------------------------------------------------

def _binary() -> str:
    try:
        from .omp import binary
        return binary()
    except Exception:
        return shutil.which("omp") or "omp"


def _observed_models(stdout: str) -> set[str]:
    """``provider/model`` of every assistant event in an ``omp --mode json`` stream."""
    seen: set[str] = set()
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        provider, name = message.get("provider"), message.get("model")
        if provider and name:
            seen.add(f"{provider}/{name}")
    return seen


FALLBACK_OFF = ("retry: {modelFallback: false, usageAwareFallback: false, "
                "fallbackChains: {}}\n")


def _run_omp(prompt: str, model: str, effort: str) -> str:
    with tempfile.TemporaryDirectory(prefix="bench-judge-") as scratch:
        overlay = Path(scratch) / "overlay.yml"
        overlay.write_text(FALLBACK_OFF, encoding="utf-8")
        cmd = [_binary(), "-p", "--mode", "json", "--no-tools", "--no-session",
               "--no-extensions", "--no-skills", "--no-rules",
               "--config", str(overlay),
               "--model", model, "--thinking", effort]
        try:
            proc = subprocess.run(cmd, input=prompt, text=True,
                                  capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"judge process failed: {error}") from error
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-800:]
        raise RuntimeError(f"judge exited {proc.returncode}: {tail}")
    observed = _observed_models(proc.stdout)
    if observed != {model}:
        raise RuntimeError(
            f"judge model mismatch: requested {model}, observed {sorted(observed)}")
    return proc.stdout


def _assistant_text(stdout: str) -> str:
    """Last assistant text from an ``omp --mode json`` event stream."""
    found = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = "\n".join(block.get("text", "")
                         for block in message.get("content") or []
                         if isinstance(block, dict) and block.get("type") == "text")
        if text.strip():
            found = text
    return found or (stdout or "")


def _payload(text: str):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in judge output")
    return json.loads(text[start:end + 1])


def _verdicts(payload, facts: list[dict]) -> list[dict]:
    expected = [f.get("id") for f in facts]
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and isinstance(payload.get("verdicts"), list):
        entries = payload["verdicts"]
    elif isinstance(payload, dict):
        entries = [{"id": key, "verdict": value} for key, value in payload.items()]
    else:
        raise ValueError("judge output is not a verdict list")

    by_id: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("judge verdict entry is not an object")
        fact_id = entry.get("id")
        verdict = entry.get("verdict")
        if fact_id not in expected:
            raise ValueError(f"judge returned unknown fact id {fact_id!r}")
        if verdict not in VERDICTS:
            raise ValueError(f"judge returned invalid verdict {verdict!r}")
        by_id[fact_id] = {"id": fact_id, "verdict": verdict,
                          "evidence": str(entry.get("evidence") or "")}
    if set(by_id) != set(expected):
        raise ValueError("judge did not answer every fact")
    return [by_id[fact_id] for fact_id in expected]


def judge_facts(report: str, facts: list[dict], judge: dict,
                runner=None) -> list[dict]:
    """Grade ``facts`` against ``report``; one retry on unparsable output."""
    if not facts:
        return []
    model = (judge or {}).get("model")
    effort = (judge or {}).get("effort") or "medium"
    if not model:
        raise ValueError("judge model is required")
    run = runner or _run_omp
    prompt = build_prompt(report, facts)
    attempts = [prompt, prompt + "\nYour previous reply was not the required JSON. Reply again with only the JSON object.\n"]
    last_error = None
    for text in attempts:
        try:
            return _verdicts(_payload(_assistant_text(run(text, model, effort))), facts)
        except (ValueError, json.JSONDecodeError) as error:
            last_error = error
    raise RuntimeError(f"judge output unparsable after retry: {last_error}")
