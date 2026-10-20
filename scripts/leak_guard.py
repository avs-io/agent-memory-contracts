#!/usr/bin/env python3
"""Public-repo leak guard: fail if the diff or tree contains private markers.

Checks: email addresses outside the allowlist, Indian phone numbers, absolute
local home paths (/Users/<name>/, /home/<name>/, C:\\Users\\<name>\\), API-key
shapes (sk-, ghp_/github_pat_, xox*-, AKIA), and private words given as hashed
tokens in .github/leak-guard.json plus the LEAK_GUARD_PRIVATE_TERMS env/secret.

  python scripts/leak_guard.py --tree             # every tracked file
  python scripts/leak_guard.py --diff origin/main # lines added since BASE
  python scripts/leak_guard.py --staged           # pre-commit (index)
  python scripts/leak_guard.py --self-test
  python scripts/leak_guard.py --hash "term"      # sha256 entry for the config

Exit 0 = clean, 1 = findings, 2 = usage/config error. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".github" / "leak-guard.json"

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
PHONE_IN = re.compile(r"(?<![\w+])(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\w)")
HOME_PATH = re.compile(r"(?:/Users/|/home/)[A-Za-z0-9._-]+/|[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+")
API_KEYS = [
    ("openai/anthropic-style key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
]
TOKEN = re.compile(r"[a-z0-9]+")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def load_config(path: Path = CONFIG) -> dict:
    cfg = json.loads(path.read_text())
    priv = [t.strip().lower() for t in os.environ.get("LEAK_GUARD_PRIVATE_TERMS", "").split(",") if t.strip()]
    cfg["_private_terms"] = priv
    cfg["_denied_hashes"] = set(cfg.get("denied_token_sha256", []))
    cfg["_denied_regex"] = [re.compile(r, re.I) for r in cfg.get("denied_regex", [])]
    return cfg


def email_allowed(addr: str, domain: str, cfg: dict) -> bool:
    d = domain.lower()
    return (addr.lower() in {a.lower() for a in cfg.get("email_allow_addresses", [])}
            or d in cfg.get("email_allow_domains", [])
            or any(d.endswith(s) for s in cfg.get("email_allow_suffixes", [])))


def scan_line(line: str, cfg: dict) -> list[tuple[str, str]]:
    hits = []
    for m in EMAIL.finditer(line):
        if not email_allowed(m.group(0), m.group(1), cfg):
            hits.append(("email", m.group(0)))
    for m in PHONE_IN.finditer(line):
        hits.append(("indian-phone", m.group(0)))
    for m in HOME_PATH.finditer(line):
        if not any(m.group(0).startswith(p) for p in cfg.get("path_allow_prefixes", [])):
            hits.append(("local-path", m.group(0)))
    for name, rx in API_KEYS:
        for m in rx.finditer(line):
            hits.append((name, m.group(0)[:12] + "…"))
    low = line.lower()
    toks = TOKEN.findall(low)
    grams = set(toks) | {a + " " + b for a, b in zip(toks, toks[1:])}
    for g in grams:
        if sha(g) in cfg["_denied_hashes"]:
            hits.append(("private-term", "<redacted denylisted term>"))
    for t in cfg["_private_terms"]:
        if t in low:
            hits.append(("private-term", "<redacted secret term>"))
    for rx in cfg["_denied_regex"]:
        if rx.search(line):
            hits.append(("denied-regex", rx.pattern))
    return hits


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def skip(path: str, cfg: dict) -> bool:
    return path in cfg.get("exclude_paths", []) or Path(path).suffix.lower() in cfg.get("binary_extensions", [])


def iter_tree(cfg):
    for path in git("ls-files", "-z").split("\0"):
        if not path or skip(path, cfg):
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            yield path, n, line
        yield path, 0, path  # file names can leak too


def iter_diff(diff_args, cfg):
    out = git("diff", "--unified=0", "--no-color", "--no-ext-diff", *diff_args)
    path, n = None, 0
    for raw in out.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
            if path:
                yield path, 0, path
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            n = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path and not skip(path, cfg):
            yield path, n, raw[1:]
            n += 1


def run(lines, cfg) -> int:
    count = 0
    for path, n, line in lines:
        for kind, sample in scan_line(line, cfg):
            count += 1
            where = f"{path}:{n}" if n else f"{path} (file name)"
            print(f"::error file={path},line={max(n, 1)}::leak-guard {kind}: {sample}" if os.environ.get("GITHUB_ACTIONS")
                  else f"{where}: {kind}: {sample}")
    print(f"leak-guard: {count} finding(s)", file=sys.stderr)
    return 1 if count else 0


def self_test() -> int:
    cfg = load_config()
    at, us = "@", "/Us" + "ers/"
    must_hit = {
        "email": "contact: someone" + at + "gmail.com",
        "indian-phone": "call +91 98" + "765 43210",
        "local-path": "see " + us + "alice/notes.md",
        "openai/anthropic-style key": "key=s" + "k-" + "A" * 24,
        "github token": "gh" + "p_" + "a" * 36,
        "slack token": "xo" + "xb-" + "1234567890-abc",
    }
    ok = True
    for kind, line in must_hit.items():
        if kind not in {k for k, _ in scan_line(line, cfg)}:
            print(f"self-test FAIL: {kind} not detected", file=sys.stderr)
            ok = False
    for line in ["mail user" + at + "example.com", "sha 0123456789abcdef9876543210", "ts 1779544178556",
                 "/home/runner/work/x", "bot" + at + "users.noreply.github.com"]:
        if scan_line(line, cfg):
            print(f"self-test FAIL: false positive on {line!r}: {scan_line(line, cfg)}", file=sys.stderr)
            ok = False
    print("leak-guard self-test:", "ok" if ok else "FAILED", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail if private markers appear in the repo.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tree", action="store_true")
    g.add_argument("--diff", metavar="BASE")
    g.add_argument("--staged", action="store_true")
    g.add_argument("--self-test", action="store_true")
    g.add_argument("--hash", metavar="TERM")
    ap.add_argument("--config", default=str(CONFIG))
    a = ap.parse_args(argv)
    if a.hash:
        print(sha(" ".join(TOKEN.findall(a.hash.lower()))))
        return 0
    if a.self_test:
        return self_test()
    try:
        cfg = load_config(Path(a.config))
    except (OSError, ValueError) as exc:
        print(f"leak-guard: bad config: {exc}", file=sys.stderr)
        return 2
    if a.tree:
        return run(iter_tree(cfg), cfg)
    if a.staged:
        return run(iter_diff(["--cached"], cfg), cfg)
    return run(iter_diff([f"{a.diff}...HEAD"], cfg), cfg)


if __name__ == "__main__":
    sys.exit(main())
