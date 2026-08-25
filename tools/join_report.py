#!/usr/bin/env python3
"""
Report how the BIAN API set joins to the BIAN Service Landscape.

The APIs source covers 258 service domains; the landscape covers 367. Whether
those 258 actually resolve to landscape domains is the number that decides
whether the two bundles are usable together, and until it is measured it is an
assumption.

This is a TOOL and not a check inside either source, deliberately:

  - A source never imports another source. Putting this in bian-apis-v14 would
    couple it to bian-v14's internals.
  - Making the APIs source fetch the landscape would turn a one-request
    harvest into a forty-eight-request one, against someone else's web server,
    to answer a question that is not about the APIs at all.

Both bundles already record every item's name in manifest.json, so the join
needs no network access and no source code from either side. Point it at two
output directories:

    python3 tools/join_report.py out/bian-v14 out/bian-apis-v14

The landscape half is cheap to produce -- `run.py validate bian-v14` writes the
semantic half in about two minutes, which is all this needs.

Names are compared with punctuation and case removed, because the landscape
says "Consumer Loan" and the API filename says "ConsumerLoan". Both the API
item's id and its title are tried, so a mismatch in one does not hide a match
in the other.

Exit status is 0 unless --min-rate is given and the join falls below it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NORM_RE = re.compile(r"[^a-z0-9]+")


def norm(text: str) -> str:
    return NORM_RE.sub("", (text or "").lower())


def load_manifest(bundle: Path) -> dict:
    path = bundle / "manifest.json"
    if not path.is_file():
        sys.exit(f"  no manifest.json in {bundle} — has it been harvested?")
    return json.loads(path.read_text(encoding="utf-8"))


def service_domain_category(manifest: dict) -> str:
    """Find the landscape category holding service domains.

    Guessing the exact string and hard-coding it is how a silent mismatch
    starts, so it is discovered and the alternatives are printed on failure.
    """
    categories = manifest.get("categories") or {}
    for name in categories:
        if "service domain" in name.lower():
            return name
    sys.exit(
        "  could not find a service domain category in the landscape "
        "bundle.\n  categories present: "
        + ", ".join(sorted(categories)) or "(none)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("landscape", type=Path, help="out/bian-v14")
    ap.add_argument("apis", type=Path, help="out/bian-apis-v14")
    ap.add_argument("--min-rate", type=float, default=None,
                    help="fail below this percentage of APIs matched")
    ap.add_argument("--list", action="store_true",
                    help="list every unmatched name")
    args = ap.parse_args()

    land = load_manifest(args.landscape)
    apis = load_manifest(args.apis)
    category = service_domain_category(land)

    domains = {
        item.get("name", ""): item_id
        for item_id, item in (land.get("items") or {}).items()
        if item.get("category") == category
    }
    by_norm = {norm(n): n for n in domains if n}

    matched, unmatched = [], []
    for item_id, item in (apis.get("items") or {}).items():
        title = item.get("name", "")
        hit = by_norm.get(norm(title)) or by_norm.get(norm(item_id))
        (matched if hit else unmatched).append((item_id, title, hit))

    total = len(matched) + len(unmatched)
    rate = (100.0 * len(matched) / total) if total else 0.0
    covered = {m[2] for m in matched}
    no_api = sorted(n for n in domains if n not in covered)

    print("=" * 70)
    print("  BIAN APIs to Service Landscape join")
    print("=" * 70)
    print()
    print(f"  landscape bundle : {args.landscape}")
    print(f"  landscape source : {land.get('source')} "
          f"({'complete' if land.get('complete') else 'INCOMPLETE'})")
    print(f"  apis bundle      : {args.apis}")
    print(f"  apis source      : {apis.get('source')}")
    print()
    print(f"  landscape service domains : {len(domains)}")
    print(f"  api service domains       : {total}")
    print()
    print(f"  matched      {len(matched):>4}  ({rate:.1f}% of APIs)")
    print(f"  unmatched    {len(unmatched):>4}")
    print(f"  no API       {len(no_api):>4}  "
          f"(landscape domains this release exposes no API for)")
    print()

    if unmatched:
        print("  Unmatched APIs — these have no landscape domain, which means")
        print("  either a naming drift or a domain the landscape omits:")
        shown = unmatched if args.list else unmatched[:15]
        for item_id, title, _ in sorted(shown):
            print(f"    {item_id:<40} {title}")
        if not args.list and len(unmatched) > len(shown):
            print(f"    ... and {len(unmatched) - len(shown)} more (--list)")
        print()

    if args.list and no_api:
        print("  Landscape domains with no API at this release:")
        for name in no_api:
            print(f"    {name}")
        print()

    if args.min_rate is not None and rate < args.min_rate:
        print(f"  RESULT: join rate {rate:.1f}% is below the required "
              f"{args.min_rate:.1f}%.")
        print("  Check naming drift before trusting the two bundles together.")
        return 1

    print("  RESULT: join measured. This number is the one to watch when "
          "either")
    print("  side is re-harvested — a sudden drop means a naming change, not "
          "a")
    print("  content change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
