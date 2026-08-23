#!/usr/bin/env python3
"""
Convert BIAN InSite view pages into PlantUML, one at a time.

The parsing and generation moved to bianlib/views.py when the full-landscape
pipeline needed them too; this is the standalone CLI over the top, unchanged in
behaviour:

    python3 tools/view_to_plantuml.py <file-or-url> [more...]
    python3 tools/view_to_plantuml.py --savings      find Savings Account views

Writes .puml files to out/diagrams/.

For the whole landscape use tools/landscape.py instead — it paces its requests,
splits the work into verified chunks, and does not re-read the shards for every
diagram.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bianlib.fetch import Fetcher                               # noqa: E402
from bianlib.landscape import (data_url, parse_js_assignment,   # noqa: E402
                               parse_js_assignments, shard_numbers,
                               shard_url, view_url)
from bianlib.views import (class_plantuml, fetch, parse_view,   # noqa: E402
                           summarise, to_plantuml)

BASE = "https://bian.org/servicelandscape-14-0-0"
OUTDIR = Path("out/diagrams")


def find_savings_views(base: str = BASE) -> dict:
    """Locate the Savings Account service domain and the diagrams it is on."""
    fetcher = Fetcher(base, delay=0.5)
    print("locating Savings Account...", flush=True)
    mapping = parse_js_assignment(
        fetcher.get(data_url(base, "all_objects_data_mapping.js")).text)
    variables = parse_js_assignments(
        fetcher.get(data_url(base, "all_objects_on_views.js")).text)
    on_views = variables.get("objectsOnViews", {})
    views = variables.get("insiteViews", {})

    hits = []
    for n in shard_numbers(mapping):
        try:
            data = parse_js_assignment(fetcher.get(shard_url(base, n)).text)
        except Exception:
            continue
        for oid, obj in data.items():
            d0 = (obj.get("data") or [{}])[0]
            if isinstance(d0, dict) and d0.get("name") == "Savings Account":
                hits.append((oid, d0.get("type")))
    fetcher.close()
    print(f"  objects named 'Savings Account': {hits}", flush=True)

    view_ids = sorted({str(v) for oid, _t in hits
                       for v in (on_views.get(oid) or [])})
    print(f"  appears on {len(view_ids)} views", flush=True)
    named = {}
    for v in view_ids:
        name = (views.get(v) or {}).get("name", "")
        named[v] = name
        print(f"    view {v}: {name or '?'}", flush=True)
    return named


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", help="view HTML files or URLs")
    ap.add_argument("--savings", action="store_true",
                    help="discover and convert Savings Account diagrams")
    ap.add_argument("--outdir", default=str(OUTDIR))
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    sources = list(args.sources)
    # insiteViews carries every diagram's name. Class diagrams have no
    # UML_Interaction to read a title from, so without this they fall back to
    # "View 36488" — which then becomes the published filename.
    known_titles = {}
    if args.savings:
        named = find_savings_views()
        known_titles = {v: n for v, n in named.items() if n}
        sources += [view_url(BASE, v) for v in named]
    if not sources:
        ap.error("give a file/URL, or --savings")

    written = 0
    for src in sources:
        print(f"\n=== {src}", flush=True)
        try:
            d = parse_view(fetch(src), src, base=BASE)
        except Exception as e:
            print(f"  skipped: {type(e).__name__}: {e}", flush=True)
            continue
        vid = d.get("view_id") or ""
        if known_titles.get(vid) and d["title"] in ("", "diagram",
                                                    f"View {vid}"):
            d["title"] = known_titles[vid]
        print(summarise(d), flush=True)
        if d["messages"]:
            body, kind = to_plantuml(d, src), "sequence"
        elif d["classes"]:
            body, kind = class_plantuml(d, src), "class"
        else:
            print("  neither a sequence nor a class diagram, skipped", flush=True)
            continue
        # The view id is always in the filename. Titles are not unique —
        # three class diagrams all fell back to "diagram" and silently
        # overwrote one another, leaving one file where there should be three.
        slug = re.sub(r"[^a-z0-9]+", "-", d["title"].lower()).strip("-")[:60]
        vid = d.get("view_id") or "x"
        path = outdir / f"{vid}-{slug or 'diagram'}-{kind}.puml"
        path.write_text(body, encoding="utf-8")
        print(f"  wrote {path}", flush=True)
        written += 1

    print(f"\n{written} diagram(s) written to {outdir}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
