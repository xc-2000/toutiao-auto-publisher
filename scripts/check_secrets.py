#!/usr/bin/env python3
"""Fail when publish candidates contain credentials or local-only files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "covers",
    "drafts",
    "node_modules",
    "state",
    "videos",
    "work",
}
SKIP_FILES = {"scripts/check_secrets.py"}
FORBIDDEN_EXACT = {"config.toml", ".env"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key"}
FORBIDDEN_FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}

PATTERNS = {
    "private key": re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
    "GitHub token": re.compile(r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "OpenAI-style token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "local user path": re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/][^\\/\s]+|/Users/[^/\s]+)"),
}


def candidate_files() -> list[Path]:
    if (ROOT / ".git").is_dir():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


def main() -> int:
    violations: list[str] = []
    for path in candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        suffix = path.suffix.lower()
        if relative in FORBIDDEN_EXACT or suffix in FORBIDDEN_SUFFIXES:
            violations.append(f"forbidden file: {relative}")
            continue
        if relative.startswith("assets/fonts/") and suffix in FORBIDDEN_FONT_SUFFIXES:
            violations.append(f"local font file: {relative}")
            continue
        if relative in SKIP_FILES or suffix == ".traineddata" or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                violations.append(f"{name}: {relative}:{line}")
    if violations:
        print("Sensitive-data check failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Sensitive-data check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
