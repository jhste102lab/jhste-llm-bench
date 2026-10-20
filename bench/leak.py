"""Leak detection for the public site and CI.

``check_text`` matches private terms on token boundaries (plus hyphen/underscore
joined n-grams, so ``sample-visits`` matches ``Sample_VISITS``).
``--hashes`` compares sha256 hashes only: CI never sees the private terms.

Matched text is never printed; only the file and a hit kind.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

MAX_GRAM = 6
SEPARATORS = ("", "-", "_", ".")
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

GENERIC_PATTERNS = (
    ("home-path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/")),
    ("omp-state", re.compile(r"(?<!~)/(?:[^\s/]+/)*\.omp/")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)),
    ("session-id", re.compile(r"\b01[0-9a-z]{22,32}\b", re.IGNORECASE)),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("openai-token", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
)


def generic_hits(text: str) -> list[str]:
    """Structural leak patterns that never depend on private.json."""
    return sorted({name for name, pattern in GENERIC_PATTERNS if pattern.search(text or "")})


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall((text or "").lower())


def _candidates(tokens: list[str]) -> set[str]:
    found = set(tokens)
    for size in range(2, MAX_GRAM + 1):
        if size > len(tokens):
            break
        for index in range(len(tokens) - size + 1):
            window = tokens[index:index + size]
            for separator in SEPARATORS:
                found.add(separator.join(window))
    return found


def check_text(text: str, private_terms: list[str]) -> list[str]:
    """Private terms present in ``text`` (lowercased term names)."""
    terms = [str(term).strip().lower() for term in private_terms or [] if str(term).strip()]
    if not terms:
        return []
    candidates = _candidates(_tokens(text))
    return sorted({term for term in terms if term in candidates})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_hashes(text: str, hashes: set[str]) -> list[str]:
    """Number of hashed terms present in ``text`` (values stay private)."""
    if not hashes:
        return []
    hits = sum(1 for candidate in _candidates(_tokens(text)) if _hash(candidate) in hashes)
    return ["hashed-term"] * hits


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="ignore")


def _paths(targets: list[str]):
    for target in targets:
        path = Path(target)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and ".git" not in child.parts:
                    yield child
        elif path.is_file():
            yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.leak",
                                     description="Scan files for private data leaks.")
    parser.add_argument("--hashes", help="file of sha256 hashes of lowercase private terms")
    parser.add_argument("paths", nargs="+", help="files or directories to scan")
    args = parser.parse_args(argv)

    hashes: set[str] = set()
    if args.hashes:
        try:
            for line in Path(args.hashes).read_text().splitlines():
                value = line.strip().lower()
                if value and not value.startswith("#"):
                    hashes.add(value)
        except OSError as error:
            print(f"bench.leak: cannot read hashes: {error}", file=sys.stderr)
            return 2

    failed = False
    for path in _paths(args.paths):
        text = _read_text(path)
        if text is None:
            continue
        kinds = generic_hits(text) + check_hashes(text, hashes)
        for kind in sorted(set(kinds)):
            print(f"{path}: {kind}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
