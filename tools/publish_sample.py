#!/usr/bin/env python3
"""
Publish a small sample to Google Drive.

Exercises the whole publishing path — credentials, rclone, folder scoping,
the guard rails in core/publish.py — with a payload of about 26 files rather
than the full landscape. If this works, the only thing left to scale is
volume.

    python3 tools/publish_sample.py --build          generate locally, no Drive
    python3 tools/publish_sample.py --build --publish
    python3 tools/publish_sample.py --publish --dry-run

Output lands in out/bian-savings-sample/ and publishes to
gdrive:content/bian-savings-sample/ — a sample-specific folder, so it can
never disturb a real source's content.

Each diagram becomes a markdown file with the PlantUML in a fenced block:
readable as text, renderable by any PlantUML tool, and directly usable by
Claude through the Drive connector.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCE_ID = "bian-savings-sample"
SOURCE_NAME = "BIAN Savings Account (sample)"
OUTDIR = REPO / "out" / SOURCE_ID
DIAGRAMS = REPO / "out" / "diagrams"


def load_converter():
    spec = importlib.util.spec_from_file_location(
        "view_to_plantuml", REPO / "tools" / "view_to_plantuml.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build() -> int:
    """Generate the Savings Account diagrams, then wrap them for publishing."""
    conv = load_converter()

    if DIAGRAMS.exists():
        for f in DIAGRAMS.glob("*.puml"):
            f.unlink()
    DIAGRAMS.mkdir(parents=True, exist_ok=True)

    print("generating Savings Account diagrams...", flush=True)
    rc = subprocess.run(
        [sys.executable, str(REPO / "tools" / "view_to_plantuml.py"),
         "--savings", "--outdir", str(DIAGRAMS)], cwd=REPO).returncode
    if rc != 0:
        print("  diagram generation failed", flush=True)
        return rc

    pumls = sorted(DIAGRAMS.glob("*.puml"))
    if not pumls:
        print("  no diagrams produced — nothing to publish", flush=True)
        return 1

    if OUTDIR.exists():
        for f in OUTDIR.iterdir():
            if f.is_file():
                f.unlink()
    OUTDIR.mkdir(parents=True, exist_ok=True)

    items, by_kind = {}, {}
    for p in pumls:
        stem = p.stem
        kind = "sequence" if stem.endswith("-sequence") else (
            "class" if stem.endswith("-class") else "other")
        body = p.read_text(encoding="utf-8")

        m = re.search(r"^title (.+)$", body, re.M)
        title = m.group(1).strip() if m else stem
        vid = stem.split("-", 1)[0]
        src = ""
        m = re.search(r"^' Generated from (\S+)", body, re.M)
        if m:
            src = m.group(1)

        lines = [f"# {title}", "",
                 f"- **Kind:** {kind} diagram",
                 f"- **View id:** {vid}"]
        if src:
            lines.append(f"- **Source:** {src}")
        lines += ["",
                  "Generated from the view page's SVG geometry: message order "
                  "from the y coordinate, sender and receiver from the x "
                  "coordinates matched to lifeline columns.",
                  "", "```plantuml", body.rstrip(), "```", ""]
        md = "\n".join(lines)

        out = OUTDIR / f"{stem}.md"
        out.write_text(md, encoding="utf-8")
        items[vid] = {"name": title, "category": kind,
                      "sha256": hashlib.sha256(md.encode()).hexdigest()}
        by_kind.setdefault(kind, []).append((title, out.name))

    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    index = [f"# {SOURCE_NAME}", "",
             f"Sample bundle — {len(items)} diagrams, generated {generated}.",
             "",
             "Every diagram the Savings Account service domain appears on, "
             "converted to PlantUML from the BIAN InSite view pages.",
             "", "| Kind | Count |", "|---|---|"]
    for kind, entries in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        index.append(f"| {kind} | {len(entries)} |")
    for kind, entries in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        index += ["", f"## {kind.title()} diagrams", ""]
        for title, fname in sorted(entries):
            index.append(f"- [{title}]({fname})")
    (OUTDIR / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    manifest = {
        "source": SOURCE_ID, "source_name": SOURCE_NAME,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(items),
        "categories": {k: len(v) for k, v in by_kind.items()},
        "items": items,
    }
    (OUTDIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    total = sum(f.stat().st_size for f in OUTDIR.iterdir() if f.is_file())
    print(f"\n  {len(items)} diagrams -> {OUTDIR}", flush=True)
    for kind, entries in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        print(f"    {kind:<10} {len(entries)}", flush=True)
    print(f"  {total / 1024:.0f} KB total, "
          f"{len(list(OUTDIR.iterdir()))} files", flush=True)
    return 0


def publish(dry_run: bool) -> int:
    from core import publish as pub
    try:
        dest = pub.publish(SOURCE_ID, OUTDIR, dry_run=dry_run)
    except pub.PublishError as e:
        print(f"\n  PUBLISH FAILED: {e}", flush=True)
        print("\n  This is a publishing problem, not a content problem.",
              flush=True)
        print("  Check GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_TOKEN.",
              flush=True)
        return 1
    print(f"\n  published -> {dest}", flush=True)
    if not dry_run:
        print("\n  In Drive, open content/bian-savings-sample/index.md.",
              flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true",
                    help="generate the bundle locally")
    ap.add_argument("--publish", action="store_true", help="sync it to Drive")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --publish, show what would sync")
    args = ap.parse_args()
    if not (args.build or args.publish):
        ap.error("use --build, --publish, or both")

    if args.build:
        rc = build()
        if rc:
            return rc
    if args.publish:
        if not (OUTDIR / "manifest.json").is_file():
            print(f"{OUTDIR} has no manifest — run with --build first")
            return 2
        return publish(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
