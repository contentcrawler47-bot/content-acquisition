#!/usr/bin/env python3
"""
Check that the PlantUML in a content bundle actually renders.

    python3 tools/check_plantuml.py out/bian-v14 --jar plantuml.jar
    python3 tools/check_plantuml.py out/_parts/bian-v14/chunk-01/items.json \
        --jar plantuml.jar

Takes either an assembled bundle directory or one chunk's items.json, so the
same check runs per chunk — where it stops the run at chunk 1 instead of after
all ten — and again on the whole bundle before it is published.

This exists because a whole published landscape once failed to draw while
every check passed. The bundle check counted fenced ```plantuml blocks, which
proves a block is present, not that it is valid — and PlantUML's response to
invalid input is to render an error image rather than to complain, so nothing
downstream noticed either. An apostrophe comment placed at the end of a line
instead of the start was enough to break all 1,181 diagrams.

Only PlantUML can settle whether PlantUML will render something, so this hands
the diagrams to PlantUML.

Two stages, because the fast invocation is not the informative one:

  1. `-checkonly -failfast2` over every extracted diagram at once. About 30
     diagrams a second, and a single exit code for the lot: 0 clean, 200 if
     anything is wrong. That is the whole run in a few seconds.
  2. Only if stage 1 fails, each diagram again on its own with `-tsvg`, which
     does report "Error line 10 in file: ...". Slow per diagram, but it runs
     only when something is already broken, and it names what.

Exit codes: 0 all valid, 1 something invalid, 2 could not run the checker.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BLOCK_RE = re.compile(r"```plantuml\n(.*?)```", re.S)
#: The name is on the "## " heading immediately above each diagram's block.
HEADING_RE = re.compile(r"^## (.+)$", re.M)

JAVA_FLAGS = ["-Djava.awt.headless=true"]
BATCH_TIMEOUT = 900
SINGLE_TIMEOUT = 120


def extract(outdir: Path) -> list[tuple[str, str]]:
    """Every fenced PlantUML block in the bundle, as (label, source).

    Labelled by the markdown heading it sits under, so a failure names the
    diagram a human can find rather than an index into a file.
    """
    found = []
    for path in sorted(outdir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        headings = [(m.start(), m.group(1).strip())
                    for m in HEADING_RE.finditer(text)]
        for n, block in enumerate(BLOCK_RE.finditer(text)):
            name = next((h for pos, h in reversed(headings)
                         if pos < block.start()), f"{path.stem}#{n}")
            found.append((f"{path.name}: {name}", block.group(1)))
    return found


def extract_items(path: Path) -> list[tuple[str, str]]:
    """Every PlantUML block in one chunk's items.json."""
    found = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        for block in BLOCK_RE.finditer(item.get("body", "")):
            found.append((f"{item.get('id', '?')}: {item.get('name', '')}",
                          block.group(1)))
    return found


def write_all(diagrams: list[tuple[str, str]], workdir: Path) -> dict[Path, str]:
    """One file per diagram. Names are indices, not titles: titles are not
    unique and not filesystem-safe."""
    mapping = {}
    for i, (label, source) in enumerate(diagrams):
        path = workdir / f"d{i:05d}.puml"
        path.write_text(source, encoding="utf-8")
        mapping[path] = label
    return mapping


def run(jar: Path, args: list[str], timeout: int):
    return subprocess.run(
        ["java", *JAVA_FLAGS, "-jar", str(jar), *args],
        capture_output=True, text=True, timeout=timeout)


def batch_ok(jar: Path, paths: list[Path]) -> bool:
    """Stage 1. True if PlantUML is happy with all of them."""
    proc = run(jar, ["-checkonly", "-failfast2", *[str(p) for p in paths]],
               BATCH_TIMEOUT)
    return proc.returncode == 0


def blame(jar: Path, paths: list[Path], mapping: dict[Path, str],
          limit: int) -> list[tuple[str, str]]:
    """Stage 2. Which diagrams are broken, and what PlantUML says about them."""
    problems = []
    with tempfile.TemporaryDirectory() as svgdir:
        for path in paths:
            proc = run(jar, ["-tsvg", "-o", svgdir, str(path)], SINGLE_TIMEOUT)
            output = (proc.stdout + proc.stderr).strip()
            if proc.returncode != 0 or "contains errors" in output:
                detail = " / ".join(
                    re.sub(r" in file: \S+", "", line).strip()
                    for line in output.splitlines()
                    if line.strip() and "consider upgrading" not in line)
                problems.append((mapping[path], detail or "rejected"))
                if len(problems) >= limit:
                    break
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target",
                    help="content bundle directory, or a chunk's items.json")
    ap.add_argument("--jar", required=True, help="path to plantuml.jar")
    ap.add_argument("--sample", type=int, default=0,
                    help="check only every Nth diagram (0 = all). Checking all "
                         "of them takes seconds, so a sample is rarely worth it")
    ap.add_argument("--max-report", type=int, default=10,
                    help="stop naming diagrams after this many failures")
    args = ap.parse_args()

    target, jar = Path(args.target), Path(args.jar)
    if not target.exists():
        print(f"No such bundle or items file: {target}", file=sys.stderr)
        return 2
    if not jar.is_file():
        print(f"No PlantUML jar at {jar}", file=sys.stderr)
        return 2
    if not shutil.which("java"):
        # Deliberately fatal rather than a skip. A validation step that
        # silently does nothing is worse than no validation step, because it
        # reports success.
        print("java is not on PATH — cannot validate PlantUML.", file=sys.stderr)
        return 2

    diagrams = (extract_items(target) if target.suffix == ".json"
                else extract(target))
    if not diagrams:
        print(f"  no PlantUML blocks found in {target}")
        # Not an error: a bundle may legitimately carry no diagrams.
        return 0

    if args.sample > 1:
        diagrams = diagrams[::args.sample]
    print(f"  checking {len(diagrams)} PlantUML diagrams from {target}",
          flush=True)

    with tempfile.TemporaryDirectory() as workdir:
        mapping = write_all(diagrams, Path(workdir))
        paths = sorted(mapping)

        try:
            if batch_ok(jar, paths):
                print(f"  [PASS] all {len(paths)} diagrams render")
                return 0
        except subprocess.TimeoutExpired:
            print("  PlantUML did not finish in time", file=sys.stderr)
            return 2

        print("  [FAIL] PlantUML rejected at least one diagram; "
              "identifying which", flush=True)
        problems = blame(jar, paths, mapping, args.max_report)

    for label, detail in problems:
        print(f"    {label}\n      {detail}")
    print(f"\n  {len(problems)} diagram(s) named"
          + (f" (stopped at {args.max_report})"
             if len(problems) >= args.max_report else ""))
    print("\n  PlantUML renders an error image rather than failing loudly, so "
          "these\n  would have published as broken diagrams inside "
          "correct-looking markdown.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
