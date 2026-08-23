#!/usr/bin/env python3
"""
Verify the repo contents match the version that was shipped.

Detects files that were missed, half-pasted, edited, or left behind from an
earlier revision — the failure mode where the repo drifts from what you were
given and nobody notices until something behaves oddly.

    python3 tools/repo_manifest.py --write     regenerate MANIFEST.sha256
    python3 tools/repo_manifest.py --verify    check the repo against it
    python3 tools/repo_manifest.py --print     fingerprint only, for pasting

Two hashes per file:

  exact       sha256 of the bytes on disk
  normalised  sha256 after converting CRLF/CR to LF and stripping trailing
              blank lines

The GitHub web editor can silently change line endings and trailing newlines,
so an exact mismatch with a matching normalised hash means the content is
right and only whitespace differs. That is reported as a warning, not a
failure.
"""

import argparse
import hashlib
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "MANIFEST.sha256"

# Directories that never belong in the manifest.
SKIP_DIRS = {".git", "__pycache__", "out", ".venv", ".runs", ".idea", ".vscode"}
SKIP_SUFFIX = {".pyc", ".pyo"}
# NEXT_STEPS.md is instructions handed over with a revision, not repo content.
# Excluded from both sides of the comparison, so committing it or not makes no
# difference to the result.
SKIP_NAMES = {"MANIFEST.sha256", "NEXT_STEPS.md", ".DS_Store"}


def tracked_files():
    for p in sorted(REPO.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix in SKIP_SUFFIX or p.name in SKIP_NAMES:
            continue
        yield rel, p


def hashes(path: Path) -> tuple[str, str, int]:
    raw = path.read_bytes()
    exact = hashlib.sha256(raw).hexdigest()
    norm = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").rstrip(b"\n")
    return exact, hashlib.sha256(norm).hexdigest(), len(raw)


def build() -> dict[str, tuple[str, str, int]]:
    return {str(rel).replace("\\", "/"): hashes(p) for rel, p in tracked_files()}


def digest_of(entries: dict) -> str:
    """One short string summarising the whole repo, using normalised hashes so
    it survives the web editor."""
    joined = "\n".join(f"{n} {path}" for path, (_e, n, _s) in sorted(entries.items()))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def load() -> dict[str, tuple[str, str, int]]:
    if not MANIFEST.is_file():
        return {}
    out = {}
    for line in MANIFEST.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        exact, norm, size, path = parts[0], parts[1], parts[2], " ".join(parts[3:])
        out[path] = (exact, norm, int(size))
    return out


def cmd_write() -> int:
    entries = build()
    lines = [
        "# content-acquisition repo manifest",
        f"# generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"# files: {len(entries)}",
        f"# digest: {digest_of(entries)}",
        "#",
        "# exact_sha256  normalised_sha256  bytes  path",
    ]
    for path, (exact, norm, size) in sorted(entries.items()):
        lines.append(f"{exact}  {norm}  {size}  {path}")
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST.name}: {len(entries)} files, "
          f"digest {digest_of(entries)}")
    return 0


def cmd_print() -> int:
    entries = build()
    print(f"\nREPO FINGERPRINT   digest={digest_of(entries)}  "
          f"files={len(entries)}\n")
    print(f"  {'normalised sha256':<18}  {'bytes':>8}  path")
    print(f"  {'-' * 18}  {'-' * 8}  {'-' * 40}")
    for path, (_e, norm, size) in sorted(entries.items()):
        print(f"  {norm[:16]:<18}  {size:>8}  {path}")
    print()
    return 0


def cmd_verify(strict: bool, exact: bool = False) -> int:
    expected = load()
    if not expected:
        print("MANIFEST.sha256 is missing or empty — cannot verify.")
        print("It ships with the repo; add it, or run --write to create one.")
        return 2

    actual = build()
    exp_digest = ""
    for line in MANIFEST.read_text().splitlines():
        if line.startswith("# digest:"):
            exp_digest = line.split(":", 1)[1].strip()
    act_digest = digest_of(actual)

    print("=" * 70)
    print("  Repo content verification")
    print("=" * 70)
    print(f"\n  expected digest : {exp_digest or '(not recorded)'}")
    print(f"  actual digest   : {act_digest}")
    print(f"  expected files  : {len(expected)}")
    print(f"  actual files    : {len(actual)}\n")

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed, whitespace = [], []
    for path in sorted(set(expected) & set(actual)):
        e_exact, e_norm, e_size = expected[path]
        a_exact, a_norm, a_size = actual[path]
        if a_exact == e_exact:
            continue
        if a_norm == e_norm:
            whitespace.append((path, e_size, a_size))
        else:
            changed.append((path, e_size, a_size))

    ok = sorted(set(expected) & set(actual))
    ok = [p for p in ok if expected[p][0] == actual[p][0]]
    print(f"  [PASS] identical            {len(ok)}")
    print(f"  [WARN] whitespace only      {len(whitespace)}")
    print(f"  [FAIL] content differs      {len(changed)}")
    print(f"  [FAIL] missing from repo    {len(missing)}")
    print(f"  [WARN] not in manifest      {len(extra)}")

    if missing:
        print("\n  --- MISSING (add these) ---")
        for p in missing:
            print(f"    - {p}   ({expected[p][2]} bytes expected)")

    if changed:
        print("\n  --- CONTENT DIFFERS (replace these) ---")
        for p, es, a_s in changed:
            delta = a_s - es
            print(f"    - {p}   expected {es} bytes, found {a_s} "
                  f"({delta:+d})")
        print("\n    A large negative delta usually means a truncated paste.")

    if whitespace:
        print("\n  --- WHITESPACE ONLY (content correct, safe to ignore) ---")
        for p, es, a_s in whitespace:
            print(f"    - {p}   {es} -> {a_s} bytes")

    if extra:
        print("\n  --- NOT IN MANIFEST ---")
        for p in extra:
            print(f"    + {p}   ({actual[p][2]} bytes)")
        print("\n    Files you added deliberately, or leftovers from an "
              "earlier revision that should be deleted.")

    print("\n" + "=" * 70)
    if changed or missing:
        print("  RESULT: REPO IS OUT OF DATE")
        print("=" * 70)
        print("\n  Fix the files listed above, then re-run this workflow.\n")
        return 1
    if extra and exact:
        # Changeset verification: the end state must be exactly what the
        # changeset declared. An unexpected file means an operation was
        # missed, so it must not be treated as a note.
        print("  RESULT: UNEXPECTED FILES PRESENT")
        print("=" * 70)
        print("\n  The tree contains files the manifest does not declare.")
        print("  Under --exact this is a failure: a changeset should leave "
              "the repo")
        print("  in precisely the declared state.\n")
        return 1
    if extra or whitespace:
        print("  RESULT: CONTENT CORRECT, with notes")
        print("=" * 70)
        if whitespace:
            print("\n  Whitespace differences come from the GitHub web editor "
                  "and are harmless.")
        if extra:
            print("\n  Review the unexpected files above.")
        print()
        return 1 if strict else 0
    print("  RESULT: REPO MATCHES THE SHIPPED VERSION EXACTLY")
    print("=" * 70)
    print(f"\n  Digest {act_digest} — quote this to confirm the state.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate manifest")
    g.add_argument("--verify", action="store_true", help="check against it")
    g.add_argument("--print", dest="show", action="store_true",
                   help="print a fingerprint for pasting")
    ap.add_argument("--strict", action="store_true",
                    help="unexpected or whitespace-only differences also fail")
    ap.add_argument("--exact", action="store_true",
                    help="unexpected files fail, whitespace-only does not "
                         "(used when verifying an applied changeset)")
    args = ap.parse_args()

    if args.write:
        return cmd_write()
    if args.show:
        return cmd_print()
    return cmd_verify(args.strict, args.exact)


if __name__ == "__main__":
    sys.exit(main())
