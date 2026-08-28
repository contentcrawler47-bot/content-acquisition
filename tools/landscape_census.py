#!/usr/bin/env python3
"""
Census the BIAN Service Landscape: how many of each thing, and of what.

Every number this prints carries its denominator and its definition. That is
the whole point of the tool. Four separate bugs in this project came from a
count that was correct but labelled as something it was not:

  018  a substring test for the service domain category could never match the
       landscape's spelling, and locked onto a 2-object stray instead
  019  a dict keyed by name collapsed duplicates, so a line labelled
       "landscape service domains" reported distinct names, not objects
  ---  a workflow comment recorded the same bug as "222 of 340" where the
       reference said "222 of 367" -- model total and view membership swapped
  023  THIS TOOL shipped with its own hardcoded copy of the category
       allowlist, transcribed from a skill's prose rather than read from the
       code. The prose was six categories short, so the tool reported 11,336
       wanted objects where the pipeline finds 11,340 -- a wrong number
       produced by the tool built to stop wrong numbers.

So: counts are reported as OBJECTS or as DISTINCT NAMES, never as a bare
"service domains"; every ratio names what it is over; and **the filter is
imported from the pipeline, never restated here.** A second copy of a constant
is a second thing to keep right, and the copy nobody runs is the one that rots.

    python3 tools/landscape_census.py
    python3 tools/landscape_census.py --json census.json

Read-only. No credentials. About 50 requests, paced, roughly two minutes.
Prefer the workflow so the pacing and User-Agent stay consistent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bianlib.fetch import Fetcher                          # noqa: E402
from bianlib.landscape import (INCLUDE_CATEGORIES,         # noqa: E402
                               Landscape, is_wanted)

BASE = "https://bian.org/servicelandscape-14-0-0"
DELAY = 1.0

# Which categories name a service domain. Derived from the pipeline's own
# allowlist rather than typed out: any spelling it accepts, this counts.
SERVICE_DOMAIN_SPELLINGS = sorted(
    c for c in INCLUDE_CATEGORIES
    if re.fullmatch(r"service ?domain", c.strip(), re.I))


def normalise(s: str) -> str:
    """Case, punctuation and whitespace removed, NFKD-folded first so a
    non-breaking space or curly apostrophe cannot survive as a difference."""
    return re.sub(r"[^a-z0-9]+", "",
                  unicodedata.normalize("NFKD", s or "").lower())


def census(land: Landscape) -> dict:
    """Every count in one pass over an already-loaded model."""
    by_category = Counter(land.categories.values())

    sd_norm = {normalise(s) for s in SERVICE_DOMAIN_SPELLINGS}
    sd_ids = {oid for oid, cat in land.categories.items()
              if normalise(cat) in sd_norm}

    names_by_norm: dict[str, list[str]] = defaultdict(list)
    for oid in sd_ids:
        names_by_norm[normalise(land.names.get(oid, ""))].append(oid)

    # The pipeline's filter, not a reimplementation of it. is_wanted() also
    # drops anything whose category or name ends in " relation", which a flat
    # category count would miss.
    wanted = sum(1 for oid, cat in land.categories.items()
                 if is_wanted(cat, land.names.get(oid, "")))

    eq_edges, eq_sources = 0, set()
    for oid in sd_ids:
        rels = land.relations.get(str(oid))
        for rel in rels if isinstance(rels, list) else []:
            if not isinstance(rel, dict):
                continue
            if normalise(rel.get("via", "")) != normalise("is equal to"):
                continue
            targets = rel.get("to")
            targets = targets if isinstance(targets, list) else []
            if targets:
                eq_edges += len(targets)
                eq_sources.add(oid)

    dupes = {k: v for k, v in names_by_norm.items() if len(v) > 1}
    return {
        "base": land.base,
        "shards": len(land.shards),
        "unique_objects": len(land.objects),
        "wanted_objects": wanted,
        "service_domain_objects": len(sd_ids),
        "service_domain_distinct_names": len(names_by_norm),
        "service_domain_by_spelling": {
            c: n for c, n in by_category.items() if normalise(c) in sd_norm},
        "shared_names": len(dupes),
        "objects_with_a_shared_name": sum(len(v) for v in dupes.values()),
        "is_equal_to_edges": eq_edges,
        "is_equal_to_distinct_sources": len(eq_sources),
        "categories": dict(by_category),
        "_dupes": {k: sorted(v) for k, v in dupes.items()},
        "_names": {oid: land.names.get(oid, "") for oid in sd_ids},
        "notes": list(land.notes),
    }


def report(c: dict) -> None:
    sd = c["service_domain_objects"]
    print("\n" + "=" * 70)
    print("  RESULTS -- every figure names what it counts")
    print("=" * 70)
    print(f"""
  Shards fetched                                  {c['shards']}
  Unique objects (union of all shards)            {c['unique_objects']}

  Objects kept by the pipeline filter             {c['wanted_objects']}
      bianlib.landscape.is_wanted(), over {c['unique_objects']} objects
      INCLUDE_CATEGORIES holds {len(INCLUDE_CATEGORIES)} categories

  SERVICE DOMAINS""")
    for spelling, n in sorted(c["service_domain_by_spelling"].items()):
        print(f"      objects spelled {spelling!r:<18} {n}")
    print(f"""      ------------------------------------------
      OBJECTS, all spellings                    {sd}
      DISTINCT NAMES, normalised                {c['service_domain_distinct_names']}
      names shared by more than one object      {c['shared_names']}
      objects carrying a shared name            {c['objects_with_a_shared_name']}
      loss if deduplicated by name              {sd - c['service_domain_distinct_names']}

  'is equal to' FROM a service domain
      edges             {c['is_equal_to_edges']:<6} over {sd} service domain objects
      distinct sources  {c['is_equal_to_distinct_sources']:<6} of {sd} objects""")

    if c["is_equal_to_distinct_sources"] == c["service_domain_distinct_names"]:
        print("\n  NOTE: distinct sources equals the DISTINCT NAME count.")
        print("  Check this is real and not a name-keyed collapse (bug 019).")

    print("\n  Largest categories:")
    for cat, n in Counter(c["categories"]).most_common(12):
        print(f"    {n:>7}  {cat or '(empty)'}")

    if c["_dupes"]:
        print(f"\n  Shared service domain names ({c['shared_names']}):")
        for norm, ids in sorted(c["_dupes"].items()):
            print(f"    {c['_names'][ids[0]]!r} -- {len(ids)} objects: "
                  f"{', '.join(ids)}")

    for note in c["notes"]:
        print(f"\n  NOTE from the loader: {note}")

    print("\n  These are MEASUREMENTS. They belong in REFERENCE-DATA.md on")
    print("  Drive, not in a skill. Record them with their denominators.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Census the BIAN landscape.")
    ap.add_argument("--json", metavar="PATH", help="also write counts as JSON")
    ap.add_argument("--base", default=BASE, help="landscape base URL")
    ap.add_argument("--delay", type=float, default=DELAY,
                    help="seconds between requests (default 1.0)")
    args = ap.parse_args()

    print("=" * 70)
    print("  BIAN Service Landscape census")
    print("=" * 70)
    print(f"\n  base {args.base}\n")

    land = Landscape(args.base).load(Fetcher(args.base, delay=args.delay))
    if not land.objects:
        print("\n  No objects loaded. Refusing to report zero as a count.")
        return 1

    c = census(land)
    report(c)

    if args.json:
        payload = {k: v for k, v in c.items() if not k.startswith("_")}
        payload["shared_name_ids"] = c["_dupes"]
        Path(args.json).write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"  wrote {args.json}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
