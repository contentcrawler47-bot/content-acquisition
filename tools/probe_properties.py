#!/usr/bin/env python3
"""
What is in the per-object PROPERTY TABLES, and is the extract right to drop them?

`bianlib.landscape._properties()` parses a property table off every object.
`Landscape.render()` emits it, so the CURRENTLY PUBLISHED bundle contains it.
`bianlib.extract.build()` does not, so a stage 2 body renderer reading the
extract cannot reproduce it. Nothing would fail if that happened: the object
counts would be unchanged and only the `### <group>` sections would vanish.

Probe run 33364405091 established WHICH fields exist -- 120 names across 11
groups -- but printed no values. This one establishes what the values ARE, so
that carrying them can be designed rather than guessed at.

  T5a  What SHAPE is each value?
       `_flatten` recognises str, and dicts of type link / object / collection.
       A collection of object references is EDGES we are discarding, which is
       a much stronger claim than "properties we are discarding". A string is
       documentation. The two need different treatment in the extract.

  T5b  Do collection members RESOLVE to known object ids?
       If they do, the property is graph data.

  T5c  Is it REDUNDANT with the relation graph?
       For every field holding object references, how many of those references
       are ALREADY reachable from the same object by some relation verb. This
       measures duplication directly instead of arguing about it.

  T5d  What would carrying it COST?
       Serialised bytes per group, so the decision is not taken blind.

    python3 tools/probe_properties.py
    python3 tools/probe_properties.py --json properties.json

Read-only, no credentials. About 50 paced requests.

PRINTS NO VERDICT. The previous probe computed a summary line with the same
code that gathered its evidence, and the line was wrong while the evidence
under it was right. This one prints distributions and stops.

WHAT IT DELIBERATELY DOES NOT PRINT: full URLs (hosts only) and more than one
short sample per field. Actions logs on a public repo are world-readable and
the values are BIAN's content.

Delete once the answers are recorded on Drive.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bianlib import landscape as L                          # noqa: E402
from bianlib.fetch import Fetcher                           # noqa: E402
from bianlib.landscape import Landscape                     # noqa: E402
from core.cli import discover                               # noqa: E402

SAMPLE = 60

#: Groups that are the modelling tool's own furniture rather than BIAN
#: content. Reported separately so the signal is not buried, never silently
#: dropped -- a filter cannot see the population it excludes.
TOOL_GROUPS = {"Stereotypes", "Multiplicity element"}


def hdr(t):
    print("\n" + "=" * 72)
    print(f"  {t}")
    print("=" * 72, flush=True)


def first_entry(obj) -> dict:
    data = L._l(L._d(obj).get("data"))
    return L._d(data[0]) if data else {}


def classify(raw):
    """Shape of a RAW property value, before _flatten collapses it.

    Returns (kind, detail) where detail carries what that kind needs:
    object/link -> the keys present, so we can see whether an id travels with
    the reference; collection -> its members for recursion.
    """
    if isinstance(raw, str):
        return "string", None
    if isinstance(raw, list):
        return "bare-list", raw
    if isinstance(raw, dict):
        kind = raw.get("type")
        if kind in ("link", "object"):
            return kind, L._d(raw.get("value"))
        if kind == "collection":
            return "collection", L._l(raw.get("value"))
        return f"dict:{kind}", raw
    return type(raw).__name__, None


def ref_id(value) -> str:
    """An object id out of an `object` reference, if one travels with it."""
    for key in ("id", "objectId", "object_id", "oid"):
        v = L._d(value).get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


def short(s) -> str:
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:SAMPLE] + ("..." if len(s) > SAMPLE else "")


def stats(ns) -> str:
    if not ns:
        return "n=0"
    ns = sorted(ns)
    return (f"n={len(ns)} min={ns[0]} median={ns[len(ns) // 2]} "
            f"max={ns[-1]}")


def probe(land: Landscape) -> dict:
    # ---- gather -----------------------------------------------------
    # field -> counters. Everything is keyed "group / field" so a field name
    # appearing under two groups is never merged.
    shapes = defaultdict(Counter)
    str_lens = defaultdict(list)
    coll_lens = defaultdict(list)
    ref_keys = defaultdict(Counter)
    refs_by_field = defaultdict(list)     # (owner_oid, referenced_id)
    hosts = defaultdict(Counter)
    samples = {}
    group_bytes = Counter()
    group_objects = Counter()
    parsed = documented = 0

    for oid, obj in land.objects.items():
        entry = first_entry(obj)
        if not entry:
            continue
        parsed += 1
        if L._documentation(entry):
            documented += 1
        for group, fields in L._properties(entry).items():
            group = str(group)
            group_objects[group] += 1
            if not isinstance(fields, dict):
                continue
            try:
                group_bytes[group] += len(json.dumps(fields, default=str))
            except (TypeError, ValueError):
                pass
            for key, raw in fields.items():
                label = f"{group} / {key}"
                kind, detail = classify(raw)
                shapes[label][kind] += 1

                if kind == "string":
                    str_lens[label].append(len(raw))
                    samples.setdefault(label, ("string", short(raw)))
                elif kind == "link":
                    loc = L._d(detail).get("location", "")
                    ref_keys[label].update(sorted(L._d(detail)))
                    if isinstance(loc, str) and loc:
                        host = urllib.parse.urlsplit(loc).netloc or "(relative)"
                        hosts[label][host] += 1
                    samples.setdefault(
                        label, ("link", short(L._d(detail).get("title", ""))))
                elif kind == "object":
                    ref_keys[label].update(sorted(L._d(detail)))
                    rid = ref_id(detail)
                    if rid:
                        refs_by_field[label].append((str(oid), rid))
                    samples.setdefault(
                        label, ("object", short(L._d(detail).get("name", ""))))
                elif kind in ("collection", "bare-list"):
                    members = detail or []
                    coll_lens[label].append(len(members))
                    for m in members:
                        mkind, mdetail = classify(m)
                        shapes[label][f"  member:{mkind}"] += 1
                        if mkind == "object":
                            ref_keys[label].update(sorted(L._d(mdetail)))
                            rid = ref_id(mdetail)
                            if rid:
                                refs_by_field[label].append((str(oid), rid))
                            samples.setdefault(
                                label, ("collection of object",
                                        short(L._d(mdetail).get("name", ""))))
                        elif mkind == "link":
                            loc = L._d(mdetail).get("location", "")
                            if isinstance(loc, str) and loc:
                                host = (urllib.parse.urlsplit(loc).netloc
                                        or "(relative)")
                                hosts[label][host] += 1
                            samples.setdefault(
                                label, ("collection of link",
                                        short(L._d(mdetail).get("title", ""))))
                        elif mkind == "string":
                            samples.setdefault(
                                label, ("collection of string", short(m)))

    # ---- report -----------------------------------------------------
    hdr("DENOMINATORS")
    print(f"\n  objects in model               {len(land.objects):>8}")
    print(f"  entries that parsed            {parsed:>8}")
    print(f"  entries carrying documentation {documented:>8}   <- control")
    print(f"  distinct group/field names     {len(shapes):>8}", flush=True)

    if not parsed or not shapes:
        # A group reporting nothing of every kind means the parse failed, not
        # that the tables are empty. Say so rather than reporting zeros.
        print("\n  INCONCLUSIVE: no property table parsed. Every distribution")
        print("  below would be a zero that means nothing. Do not conclude"
              " from this run.", flush=True)
        return {"parsed": parsed, "documented": documented,
                "verdict": "INCONCLUSIVE"}

    hdr("T5d  BYTES AND OBJECTS PER GROUP")
    print(f"\n  {'group':<34} {'objects':>9} {'bytes':>12}", flush=True)
    for g, n in group_objects.most_common():
        tag = "  (tool furniture)" if g in TOOL_GROUPS else ""
        print(f"  {g or '(ungrouped)':<34} {n:>9} {group_bytes[g]:>12,}{tag}",
              flush=True)
    print(f"\n  TOTAL bytes across all groups: {sum(group_bytes.values()):,}",
          flush=True)

    hdr("T5a  VALUE SHAPE PER FIELD")
    print("\n  `member:` rows are the shapes found INSIDE a collection.\n",
          flush=True)
    for label in sorted(shapes, key=lambda k: -sum(shapes[k].values())):
        group = label.split(" / ")[0]
        tag = "  (tool furniture)" if group in TOOL_GROUPS else ""
        total = sum(v for k, v in shapes[label].items()
                    if not k.startswith("  member:"))
        print(f"\n  {label}   ({total} objects){tag}", flush=True)
        for kind, n in shapes[label].most_common():
            print(f"      {n:>7}  {kind}", flush=True)
        if str_lens[label]:
            print(f"      string length   {stats(str_lens[label])}", flush=True)
        if coll_lens[label]:
            print(f"      collection size {stats(coll_lens[label])}", flush=True)
        if ref_keys[label]:
            print(f"      reference keys  "
                  f"{dict(ref_keys[label].most_common(8))}", flush=True)
        if hosts[label]:
            print(f"      link hosts      {dict(hosts[label].most_common(5))}",
                  flush=True)
        if label in samples:
            kind, text = samples[label]
            print(f"      sample ({kind})  {text!r}", flush=True)

    hdr("T5b  DO REFERENCES RESOLVE TO KNOWN OBJECTS?")
    resolution = {}
    if not refs_by_field:
        print("\n  No field carried an object reference with an id attached.")
        print("  Every value is prose or an unresolvable label, so the property")
        print("  tables are DOCUMENTATION rather than graph data.", flush=True)
    else:
        print(f"\n  {'field':<52} {'refs':>7} {'resolve':>8}", flush=True)
        for label, pairs in sorted(refs_by_field.items(),
                                   key=lambda kv: -len(kv[1])):
            ok = sum(1 for _, rid in pairs if rid in land.categories)
            resolution[label] = {"refs": len(pairs), "resolve": ok}
            print(f"  {label:<52} {len(pairs):>7} {ok:>8}", flush=True)
            cats = Counter(land.categories.get(rid, "(unresolved)")
                           for _, rid in pairs)
            print(f"      target categories: {dict(cats.most_common(6))}",
                  flush=True)

    hdr("T5c  ARE THOSE REFERENCES ALREADY IN THE RELATION GRAPH?")
    overlap = {}
    if not refs_by_field:
        print("\n  Not applicable: no object references found.", flush=True)
    else:
        # Everything reachable from an object by ANY verb, in one pass.
        reachable = {}
        for src, rels in land.relations.items():
            targets = set()
            for edge in L._l(rels):
                if not isinstance(edge, dict):
                    continue
                for t in edge.get("to") or []:
                    if isinstance(t, (str, int)):
                        targets.add(str(t))
            reachable[str(src)] = targets

        print(f"\n  {'field':<52} {'refs':>7} {'already an edge':>16}",
              flush=True)
        for label, pairs in sorted(refs_by_field.items(),
                                   key=lambda kv: -len(kv[1])):
            hit = sum(1 for owner, rid in pairs
                      if rid in reachable.get(owner, ()))
            overlap[label] = {"refs": len(pairs), "already_edge": hit}
            pct = f"{hit / len(pairs):.0%}" if pairs else "-"
            print(f"  {label:<52} {len(pairs):>7} {hit:>10} ({pct})",
                  flush=True)
        print("\n  A LOW percentage means the property carries links the graph")
        print("  does not have, and dropping it loses them. A HIGH percentage")
        print("  means it restates edges the extract already keeps.", flush=True)

    return {
        "parsed": parsed,
        "documented": documented,
        "group_objects": dict(group_objects),
        "group_bytes": dict(group_bytes),
        "shapes": {k: dict(v) for k, v in shapes.items()},
        "resolution": resolution,
        "overlap": overlap,
        "link_hosts": {k: dict(v) for k, v in hosts.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="bian-v14",
                    help="source id whose base URL to probe")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--json", metavar="PATH",
                    help="also write the machine-readable summary here")
    args = ap.parse_args()

    # The base URL belongs to the source, not to this tool.
    sources = discover()
    src = sources.get(args.source)
    if src is None:
        print(f"unknown source {args.source!r}; known: {sorted(sources)}",
              file=sys.stderr)
        return 2

    print(f"probing {src.id} at {src.base}", flush=True)
    fetcher = Fetcher(src.base, delay=args.delay)
    try:
        land = Landscape(src.base, object_view=src.object_view).load(fetcher)
        if not land.objects:
            print("\nNOT MEASURED: the model did not load.", file=sys.stderr)
            return 1
        summary = {"source": src.id, "base": src.base,
                   "objects": len(land.objects)}
        summary.update(probe(land))
    finally:
        print("\n" + fetcher.report(), flush=True)
        fetcher.close()

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1),
                                   encoding="utf-8")
        print(f"wrote {args.json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
