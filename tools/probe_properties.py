#!/usr/bin/env python3
"""
What is in the per-object PROPERTY TABLES, and what would carrying them cost?

`bianlib.landscape._properties()` parses a property table off every object.
`Landscape.render()` emits it, so the CURRENTLY PUBLISHED bundle contains part
of it. `bianlib.extract.build()` does not, so a stage 2 body renderer reading
the extract cannot reproduce any of it. Nothing would fail if that shipped:
the object counts would be unchanged and only the `### <group>` sections would
vanish.

DECIDED: property references are carried as PROPERTIES, not promoted to
relations. The relation graph keeps its single provenance, its symmetry and
its 38 real verbs. So the open question is no longer "relations or
properties" but "what exactly has to be stored", which is what this measures.

PASS 1 (run 90413375983) established value shapes and that the references are
100% disjoint from the relation graph. It left three gaps, which are the
reason for this pass:

  T5e  WHAT IS INSIDE A `structure`?
       50,868 values -- the single largest class, 44% of all property values --
       were classified as `dict:structure` and never opened. `/ 6. SO
       parameters` alone holds 45,263 of them across 4,817 objects, median 8
       each. Storage cannot be designed while blind to them. This walks in:
       key signatures, per-key value shapes, nesting depth, and any object
       references they carry.

  T5f  BYTES PER FIELD, not just per group.
       Pass 1 reported 34.4 MB in one ungrouped bucket, mixing SO parameters
       with layout junk like `Show borders` and `Sort order x-axis`. Without a
       per-field split no exclusion list can be sized.

  T5g  WHICH CATEGORY OWNS EACH FIELD?
       Pass 1 recorded the reference TARGET but never the OWNER, so "`/ 1.
       Service Operation` is owned by Message" is inference from a count
       match. It decides whether that field is new information or a duplicate
       of the existing `is refined in` edges.

Carried forward from pass 1: value shape per field (T5a), reference
resolution (T5b), overlap with the relation graph (T5c).

    python3 tools/probe_properties.py
    python3 tools/probe_properties.py --json properties.json

Read-only, no credentials. About 50 paced requests.

PRINTS NO VERDICT. An earlier probe computed a summary line with the same code
that gathered its evidence; the line was wrong while the evidence under it was
right.

WHAT IT DELIBERATELY DOES NOT PRINT: full URLs (hosts only), and at most one
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
MAX_DEPTH = 8          # structures could nest; a cap stops a cycle hanging CI

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

    `structure` is returned with its value dict so the walker can descend.
    _flatten() does NOT handle structure -- it falls through to "" -- which is
    why these values never reach the published bundle.
    """
    if isinstance(raw, str):
        return "string", None
    if isinstance(raw, bool):
        return "bool", None
    if isinstance(raw, (int, float)):
        return type(raw).__name__, None
    if isinstance(raw, list):
        return "bare-list", raw
    if isinstance(raw, dict):
        kind = raw.get("type")
        if kind in ("link", "object", "structure", "rtf"):
            return kind, L._d(raw.get("value"))
        if kind == "collection":
            return "collection", L._l(raw.get("value"))
        return f"dict:{kind}", raw
    return type(raw).__name__, None


def ref_id(value) -> str:
    """An object id out of a reference, if one travels with it."""
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
    return f"n={len(ns)} min={ns[0]} median={ns[len(ns) // 2]} max={ns[-1]}"


class Acc:
    """Everything accumulated per `group / field`, in one place.

    Keyed by the full label so a field name appearing under two groups is
    never merged -- `1. User name` exists both grouped and ungrouped.
    """

    def __init__(self):
        self.shapes = defaultdict(Counter)
        self.str_lens = defaultdict(list)
        self.coll_lens = defaultdict(list)
        self.ref_keys = defaultdict(Counter)
        self.refs = defaultdict(list)          # (owner_oid, referenced_id)
        self.hosts = defaultdict(Counter)
        self.samples = {}
        self.owner_cats = defaultdict(Counter)     # T5g
        self.field_bytes = Counter()               # T5f
        self.field_objects = Counter()
        self.group_bytes = Counter()
        self.group_objects = Counter()
        # T5e
        self.struct_sigs = defaultdict(Counter)    # label -> "k1|k2" -> n
        self.struct_keys = defaultdict(lambda: defaultdict(Counter))
        self.struct_depth = defaultdict(list)
        self.struct_count = Counter()


def walk(acc: Acc, label: str, owner: str, raw, depth: int, prefix: str = ""):
    """Classify one value, descending into collections and structures.

    `prefix` names where inside a structure we are, so a key nested two deep
    is reported as `structure.outer.inner` rather than merged with the top.
    """
    if depth > MAX_DEPTH:
        acc.shapes[label]["  (depth cap reached)"] += 1
        return

    kind, detail = classify(raw)
    tag = f"{prefix}{kind}" if not prefix else f"  {prefix}{kind}"
    acc.shapes[label][tag] += 1

    if kind == "string":
        acc.str_lens[label].append(len(raw))
        acc.samples.setdefault(label, ("string", short(raw)))

    elif kind in ("link", "object"):
        acc.ref_keys[label].update(sorted(L._d(detail)))
        if kind == "link":
            loc = L._d(detail).get("location", "")
            if isinstance(loc, str) and loc:
                acc.hosts[label][urllib.parse.urlsplit(loc).netloc
                                 or "(relative)"] += 1
            acc.samples.setdefault(label,
                                   ("link", short(L._d(detail).get("title", ""))))
        else:
            rid = ref_id(detail)
            if rid:
                acc.refs[label].append((owner, rid))
            acc.samples.setdefault(label,
                                   ("object", short(L._d(detail).get("name", ""))))

    elif kind in ("collection", "bare-list"):
        members = detail or []
        acc.coll_lens[label].append(len(members))
        for m in members:
            walk(acc, label, owner, m, depth + 1, prefix + "member:")

    elif kind == "structure":
        # T5e: the whole point of this pass.
        acc.struct_count[label] += 1
        fields = L._d(detail)
        acc.struct_sigs[label]["|".join(sorted(map(str, fields))) or "(empty)"] += 1
        acc.struct_depth[label].append(depth)
        for key, value in fields.items():
            key = str(key)
            kkind, kdetail = classify(value)
            acc.struct_keys[label][key][kkind] += 1
            # A structure may itself carry object references; those belong in
            # the resolution and overlap tables like any other reference.
            if kkind == "object":
                rid = ref_id(kdetail)
                if rid:
                    acc.refs[label].append((owner, rid))
                acc.ref_keys[label].update(sorted(L._d(kdetail)))
            elif kkind in ("structure", "collection", "bare-list"):
                walk(acc, label, owner, value, depth + 1,
                     prefix + f"struct.{key}:")
            elif kkind == "string":
                acc.samples.setdefault(
                    label, (f"structure.{key}", short(value)))


def probe(land: Landscape) -> dict:
    acc = Acc()
    parsed = documented = 0

    for oid, obj in land.objects.items():
        entry = first_entry(obj)
        if not entry:
            continue
        parsed += 1
        if L._documentation(entry):
            documented += 1
        cat = land.categories.get(oid, "?")
        for group, fields in L._properties(entry).items():
            group = str(group)
            acc.group_objects[group] += 1
            if not isinstance(fields, dict):
                continue
            try:
                acc.group_bytes[group] += len(json.dumps(fields, default=str))
            except (TypeError, ValueError):
                pass
            for key, raw in fields.items():
                label = f"{group} / {key}"
                acc.field_objects[label] += 1
                acc.owner_cats[label][cat] += 1           # T5g
                try:
                    acc.field_bytes[label] += len(json.dumps(raw, default=str))
                except (TypeError, ValueError):
                    pass
                walk(acc, label, str(oid), raw, 0)

    # ---- denominators ------------------------------------------------
    hdr("DENOMINATORS")
    print(f"\n  objects in model               {len(land.objects):>8}")
    print(f"  entries that parsed            {parsed:>8}")
    print(f"  entries carrying documentation {documented:>8}   <- control")
    print(f"  distinct group/field names     {len(acc.shapes):>8}", flush=True)

    if not parsed or not acc.shapes:
        # Nothing of every kind means the parse failed, not that the tables
        # are empty. Say so rather than reporting zeros.
        print("\n  INCONCLUSIVE: no property table parsed. Every distribution")
        print("  below would be a zero that means nothing. Do not conclude"
              " from this run.", flush=True)
        return {"parsed": parsed, "documented": documented,
                "verdict": "INCONCLUSIVE"}

    # ---- T5f ---------------------------------------------------------
    hdr("T5f  BYTES PER FIELD  (and per group)")
    print(f"\n  {'group':<34} {'objects':>9} {'bytes':>13}", flush=True)
    for g, n in acc.group_objects.most_common():
        tag = "  (tool furniture)" if g in TOOL_GROUPS else ""
        print(f"  {g or '(ungrouped)':<34} {n:>9} {acc.group_bytes[g]:>13,}{tag}",
              flush=True)
    total = sum(acc.group_bytes.values())
    print(f"\n  TOTAL {total:,} bytes", flush=True)

    print(f"\n  every field by bytes:\n", flush=True)
    print(f"  {'field':<52} {'objects':>8} {'bytes':>13} {'cum%':>6}",
          flush=True)
    running = 0
    field_total = sum(acc.field_bytes.values()) or 1
    for f, b in acc.field_bytes.most_common():
        running += b
        print(f"  {f or '(ungrouped)':<52} {acc.field_objects[f]:>8} "
              f"{b:>13,} {running / field_total:>5.1%}", flush=True)

    # ---- T5g ---------------------------------------------------------
    hdr("T5g  WHICH CATEGORY OWNS EACH FIELD?")
    print("\n  Ordered by objects. This is what decides whether a field is new")
    print("  information or a restatement of an edge the extract already has.\n",
          flush=True)
    for f, n in acc.field_objects.most_common():
        group = f.split(" / ")[0]
        tag = "  (tool furniture)" if group in TOOL_GROUPS else ""
        print(f"  {f or '(ungrouped)':<52} {n:>7}{tag}", flush=True)
        print(f"      owners: {dict(acc.owner_cats[f].most_common(6))}",
              flush=True)

    # ---- T5e ---------------------------------------------------------
    hdr("T5e  WHAT IS INSIDE A `structure`?")
    if not acc.struct_count:
        print("\n  No structure-shaped value was reached. Pass 1 counted 50,868,")
        print("  so a zero here means this walker did not descend -- treat as")
        print("  INCONCLUSIVE rather than as an absence.", flush=True)
    else:
        print(f"\n  {sum(acc.struct_count.values()):,} structures reached "
              f"across {len(acc.struct_count)} fields.\n", flush=True)
        for f, n in acc.struct_count.most_common():
            print(f"\n  {f or '(ungrouped)':<52} {n:>8,} structures",
                  flush=True)
            print(f"      nesting depth  {stats(acc.struct_depth[f])}",
                  flush=True)
            print(f"      key signatures ({len(acc.struct_sigs[f])} distinct):",
                  flush=True)
            for sig, c in acc.struct_sigs[f].most_common(5):
                print(f"        {c:>7,}  {sig[:100]}", flush=True)
            print(f"      per-key value shapes:", flush=True)
            for key, shapes in sorted(acc.struct_keys[f].items(),
                                      key=lambda kv: -sum(kv[1].values()))[:12]:
                print(f"        {key:<28} {dict(shapes.most_common(4))}",
                      flush=True)
            if f in acc.samples:
                kind, text = acc.samples[f]
                print(f"      sample ({kind})  {text!r}", flush=True)

    # ---- T5a ---------------------------------------------------------
    hdr("T5a  VALUE SHAPE PER FIELD")
    print("\n  Indented rows are shapes found INSIDE a collection or structure.\n",
          flush=True)
    for label in sorted(acc.shapes, key=lambda k: -acc.field_objects[k]):
        group = label.split(" / ")[0]
        tag = "  (tool furniture)" if group in TOOL_GROUPS else ""
        print(f"\n  {label or '(ungrouped)'}   "
              f"({acc.field_objects[label]} objects){tag}", flush=True)
        for kind, n in acc.shapes[label].most_common(10):
            print(f"      {n:>8,}  {kind}", flush=True)
        if acc.str_lens[label]:
            print(f"      string length   {stats(acc.str_lens[label])}",
                  flush=True)
        if acc.coll_lens[label]:
            print(f"      collection size {stats(acc.coll_lens[label])}",
                  flush=True)
        if acc.ref_keys[label]:
            print(f"      reference keys  "
                  f"{dict(acc.ref_keys[label].most_common(8))}", flush=True)
        if acc.hosts[label]:
            print(f"      link hosts      "
                  f"{dict(acc.hosts[label].most_common(5))}", flush=True)
        if label in acc.samples:
            kind, text = acc.samples[label]
            print(f"      sample ({kind})  {text!r}", flush=True)

    # ---- T5b ---------------------------------------------------------
    hdr("T5b  DO REFERENCES RESOLVE TO KNOWN OBJECTS?")
    resolution = {}
    if not acc.refs:
        print("\n  No field carried an object reference with an id attached.",
              flush=True)
    else:
        print(f"\n  {'field':<52} {'refs':>8} {'resolve':>8}", flush=True)
        for f, pairs in sorted(acc.refs.items(), key=lambda kv: -len(kv[1])):
            ok = sum(1 for _, rid in pairs if rid in land.categories)
            resolution[f] = {"refs": len(pairs), "resolve": ok}
            print(f"  {f or '(ungrouped)':<52} {len(pairs):>8} {ok:>8}",
                  flush=True)
            cats = Counter(land.categories.get(rid, "(unresolved)")
                           for _, rid in pairs)
            print(f"      targets: {dict(cats.most_common(6))}", flush=True)

    # ---- T5c ---------------------------------------------------------
    hdr("T5c  ARE THOSE REFERENCES ALREADY IN THE RELATION GRAPH?")
    overlap = {}
    if not acc.refs:
        print("\n  Not applicable: no object references found.", flush=True)
    else:
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

        print(f"\n  {'field':<52} {'refs':>8} {'already an edge':>16}",
              flush=True)
        for f, pairs in sorted(acc.refs.items(), key=lambda kv: -len(kv[1])):
            hit = sum(1 for owner, rid in pairs
                      if rid in reachable.get(owner, ()))
            overlap[f] = {"refs": len(pairs), "already_edge": hit}
            pct = f"{hit / len(pairs):.0%}" if pairs else "-"
            print(f"  {f or '(ungrouped)':<52} {len(pairs):>8} "
                  f"{hit:>10} ({pct})", flush=True)
        print("\n  LOW means the property carries links the graph does not have.")
        print("  HIGH means it restates edges the extract already keeps.",
              flush=True)

    return {
        "parsed": parsed,
        "documented": documented,
        "group_objects": dict(acc.group_objects),
        "group_bytes": dict(acc.group_bytes),
        "field_objects": dict(acc.field_objects),
        "field_bytes": dict(acc.field_bytes),
        "owner_categories": {k: dict(v) for k, v in acc.owner_cats.items()},
        "shapes": {k: dict(v) for k, v in acc.shapes.items()},
        "structures": {
            f: {"count": n,
                "signatures": dict(acc.struct_sigs[f].most_common(20)),
                "keys": {k: dict(v) for k, v in acc.struct_keys[f].items()},
                "depth_max": max(acc.struct_depth[f]) if acc.struct_depth[f] else 0}
            for f, n in acc.struct_count.items()},
        "resolution": resolution,
        "overlap": overlap,
        "link_hosts": {k: dict(v) for k, v in acc.hosts.items()},
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
