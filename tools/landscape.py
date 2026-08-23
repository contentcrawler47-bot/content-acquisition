#!/usr/bin/env python3
"""
Harvest a complete BIAN landscape in verified chunks.

    python3 tools/landscape.py plan     bian-v14 --chunks 10
    python3 tools/landscape.py chunk    bian-v14 --index 1
    python3 tools/landscape.py assemble bian-v14
    python3 tools/landscape.py publish  bian-v14 [--dry-run]

Separate from `run.py` on purpose. `run.py validate <source>` answers "can we
still extract from this source?" in about two minutes and is what the weekly
early-warning workflow runs. This is the long job: the model plus ~1,231
diagram view pages, split into chunks that each prove themselves before the
next begins.

Every stage reads and writes `out/<source>/parts/`, which is what moves between
CI jobs as an artifact. `assemble` is the only stage that writes the
publishable bundle, and it writes nothing at all unless the whole landscape
verified.

Pacing is a real setting, not decoration: --delay is the floor on the interval
between requests to bian.org. Lower it only with a reason.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bianlib import pipeline                      # noqa: E402
from core.cli import discover, outdir_for         # noqa: E402


def parts_dir(source_id: str) -> Path:
    """Staging, deliberately OUTSIDE the published directory.

    `rclone sync` copies the output directory recursively, so intermediate
    parts kept under out/<source>/ would be published to Drive alongside the
    content. They live beside it instead.
    """
    return outdir_for(source_id).parent / "_parts" / source_id


def main() -> int:
    ap = argparse.ArgumentParser(prog="landscape.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="stage", required=True)

    def common(p, pacing=True):
        p.add_argument("source", help="source id, e.g. bian-v14")
        if pacing:
            p.add_argument("--delay", type=float, default=1.0,
                           help="minimum seconds between requests (default 1.0)")
        p.add_argument("--cache", default="",
                       help="page cache from a previous run: an unchanged view "
                            "then costs a 304 with no body")
        return p

    p = common(sub.add_parser("plan", help="fetch the model, plan the chunks"))
    p.add_argument("--chunks", type=int, default=10,
                   help="number of chunks to split the view pages into")
    p.add_argument("--limit", type=int, default=0,
                   help="plan only the first N views (for a trial run)")

    p = common(sub.add_parser("chunk", help="harvest one chunk of view pages"))
    p.add_argument("--index", type=int, required=True, help="1-based chunk number")

    common(sub.add_parser("assemble", help="merge, verify and write the bundle"),
           pacing=False)

    p = common(sub.add_parser("publish", help="sync the assembled bundle to Drive"),
               pacing=False)
    p.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    sources = discover()
    source = sources.get(args.source)
    if source is None:
        print(f"No such source: {args.source}. Known: "
              f"{', '.join(sorted(sources)) or '(none)'}", file=sys.stderr)
        return 2
    if not getattr(source, "base", ""):
        print(f"{args.source} is not a landscape source (no base URL).",
              file=sys.stderr)
        return 2

    parts = parts_dir(source.id)
    cache = Path(args.cache) if getattr(args, "cache", "") else None

    print("=" * 70)
    print(f"  {source.name} — {args.stage}")
    print("=" * 70, flush=True)

    if args.stage == "plan":
        return pipeline.do_plan(source, parts, args.chunks, args.delay,
                                limit=args.limit)
    if args.stage == "chunk":
        return pipeline.do_chunk(source, parts, args.index, args.delay,
                                 cache_in=cache)
    if args.stage == "assemble":
        return pipeline.do_assemble(source, parts, outdir_for(source.id))

    # publish
    from core import publish as pub
    try:
        dest = pub.publish(source.id, outdir_for(source.id),
                           dry_run=args.dry_run)
    except pub.PublishError as e:
        print(f"Publish failed for {source.id}: {e}", file=sys.stderr)
        return 1
    print(f"  published {source.id} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
