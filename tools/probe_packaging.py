#!/usr/bin/env python3
"""
Is the BIAN model's PACKAGE STRUCTURE recoverable from the published landscape?

The extract records which model each VIEW belongs to, and nothing at all about
where an ELEMENT sits. A session spent inferring package structure from object
ids concluded that ids are creation order, not tree position -- so four
questions were left open that only the source can answer.

  T1  Does any object carry a package / folder / path / location property?
      The extract stores name, category, notation, icon and documentation and
      discards the per-object property table that landscape._properties()
      already parses. If a packaging property exists there, element grouping
      is recoverable and the inference was unnecessary. Measured over the
      WHOLE population, not a sample: every property group and field name is
      inventoried with its count.

  T2  Are Folder names stripped by the source, or by us?
      All 309 Folder objects reach the extract named "Folder". Either the
      source publishes them that way, or our parser is dropping a name. This
      dumps the complete raw entry for the container-like objects so the two
      cases can be told apart.

  T3  Is there an object -> model mapping we have missed?
      models_data.js is read for views only. 247 ArchiMate diagram objects
      are published in no view, referenced by no view and touched by no
      relation, so their package is currently assigned by id proximity alone.
      This looks for any variable, key or field that assigns a model or owner
      to an OBJECT.

  T4  What are the relation verb counts, with denominators?
      REFERENCE-DATA gives `realized by` as roughly 5,400; the extract holds
      14,532 of 105,831 edges. The older figure's method is not recorded.
      This recomputes the census from source using the pipeline's own edge
      expansion, so the number carries its denominator and its definition.

    python3 tools/probe_packaging.py
    python3 tools/probe_packaging.py --json packaging.json

Read-only, no credentials. About 50 paced requests. Prints field names,
counts and values truncated to 80 characters -- never documentation prose,
because Actions logs on a public repo are world-readable.

Delete once T1-T4 are settled and the answers are recorded on Drive.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bianlib import landscape as L                          # noqa: E402
from bianlib.fetch import Fetcher                           # noqa: E402
from bianlib.landscape import Landscape                     # noqa: E402
from core.cli import discover                               # noqa: E402

TRUNC = 80

#: Property names that would answer T1. Matched against a normalised form, so
#: "Model Package", "model_package" and "modelpackage" all hit.
PACKAGE_HINTS = ("package", "folder", "path", "location", "model", "owner",
                 "parent", "container", "namespace", "breadcrumb")

#: Categories that look like containers. Their raw entries are dumped for T2.
CONTAINER_CATEGORIES = ("Folder", "Model package", "ArchiMate model",
                        "Model", "Package")


def normalise(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def short(v):
    """One line per value, never enough to reproduce documentation prose."""
    if isinstance(v, str):
        s = re.sub(r"\s+", " ", v).strip()
        return f'"{s[:TRUNC]}{"..." if len(s) > TRUNC else ""}"'
    if isinstance(v, (int, float, bool)) or v is None:
        return repr(v)
    if isinstance(v, list):
        return f"[{len(v)} items]" + (" " + short(v[0]) if v else "")
    if isinstance(v, dict):
        return "{" + ", ".join(sorted(map(str, v))[:6]) + \
               ("..." if len(v) > 6 else "") + "}"
    return type(v).__name__


def hdr(t):
    print("\n" + "=" * 72)
    print(f"  {t}")
    print("=" * 72, flush=True)


def first_entry(obj) -> dict:
    data = L._l(L._d(obj).get("data"))
    return L._d(data[0]) if data else {}


# --------------------------------------------------------------- T1
def t1_property_inventory(land: Landscape) -> dict:
    """Every property group/field name across every object, with counts.

    Reported as OBJECTS CARRYING the field, not occurrences, and against a
    denominator of objects whose entry parsed -- an object with no entry is
    not evidence of an absent property.
    """
    hdr("T1  DOES ANY OBJECT CARRY A PACKAGING PROPERTY?")

    fields = Counter()          # "group / field" -> objects carrying it
    groups = Counter()
    parsed = 0
    documented = 0              # control: see below
    by_field_cat = defaultdict(Counter)

    for oid, obj in land.objects.items():
        entry = first_entry(obj)
        if not entry:
            continue
        parsed += 1
        if L._documentation(entry):
            documented += 1
        cat = land.categories.get(oid, "?")
        props = L._properties(entry)
        for group, group_fields in props.items():
            groups[str(group)] += 1
            if not isinstance(group_fields, dict):
                continue
            for key in group_fields:
                label = f"{group} / {key}"
                fields[label] += 1
                by_field_cat[label][cat] += 1

    print(f"\n  objects in model              {len(land.objects):>8}")
    print(f"  entries that parsed           {parsed:>8}   <- denominator")
    print(f"  entries carrying documentation{documented:>8}   <- control")
    print(f"  distinct property groups      {len(groups):>8}")
    print(f"  distinct group/field names    {len(fields):>8}", flush=True)

    if not parsed:
        print("\n  NOT MEASURED: no object entry parsed. Every count below "
              "would be a zero that means nothing.", flush=True)
        return {"parsed": 0, "documented": 0, "fields": {}, "hits": {},
                "verdict": "NOT MEASURED"}

    # A negative answer is only worth anything against a real population and
    # a working parser. `documentation` and `properties` are read from the
    # same `categories` block by the same code, so documentation is the
    # control: if it comes back rich and properties come back empty, the
    # property table genuinely is not published. If BOTH are empty the parse
    # is broken, or this is not the full model, and the answer is INCONCLUSIVE
    # rather than "no packaging property exists".
    if not groups:
        verdict = ("INCONCLUSIVE" if not documented else
                   "NO PROPERTY TABLES PUBLISHED")
        print(f"\n  no property table on ANY of the {parsed} parsed entries.",
              flush=True)
        if not documented:
            print("  ...and no documentation either, from the same block and "
                  "the same parser.", flush=True)
            print("  VERDICT: INCONCLUSIVE. This is a parse or coverage "
                  "failure, not evidence", flush=True)
            print("  that packaging properties are absent. Do not conclude "
                  "T1 from this run.", flush=True)
        else:
            print(f"  ...but {documented} DO carry documentation, read from "
                  "the same block by", flush=True)
            print("  the same parser. The control passes, so the property "
                  "table genuinely is", flush=True)
            print("  not published. VERDICT: element packaging is NOT "
                  "recoverable.", flush=True)
        return {"parsed": parsed, "documented": documented, "fields": {},
                "hits": {}, "verdict": verdict}

    print(f"\n  property GROUPS (objects carrying each):\n", flush=True)
    for g, n in groups.most_common(30):
        print(f"    {n:>8}  {g}", flush=True)

    print(f"\n  every group/field name (objects carrying each):\n", flush=True)
    for f, n in fields.most_common():
        print(f"    {n:>8}  {f}", flush=True)

    hits = {f: n for f, n in fields.items()
            if any(h in normalise(f) for h in PACKAGE_HINTS)}
    print(f"\n  --- fields matching a packaging hint {PACKAGE_HINTS} ---",
          flush=True)
    if not hits:
        print(f"\n  NONE, across {len(fields)} distinct field names on "
              f"{parsed} parsed entries.", flush=True)
        print("  Property tables ARE being read, so this is a real negative: "
              "the source", flush=True)
        print("  publishes no packaging property. Element packaging is NOT "
              "recoverable,", flush=True)
        print("  and grouping must come from the relation graph.", flush=True)
    else:
        for f, n in sorted(hits.items(), key=lambda kv: -kv[1]):
            print(f"\n    {f}   ({n} objects)", flush=True)
            print(f"      categories: "
                  f"{dict(by_field_cat[f].most_common(6))}", flush=True)
        print("\n  -> T1 ANSWERED YES. Re-derive element grouping from these",
              flush=True)
        print("     fields rather than from the relation graph.", flush=True)

    return {"parsed": parsed, "documented": documented,
            "fields": dict(fields), "hits": hits,
            "verdict": "PACKAGING FOUND" if hits else "NO PACKAGING PROPERTY"}


# --------------------------------------------------------------- T2
def t2_container_entries(land: Landscape) -> dict:
    """Complete raw entry for each container-like category.

    Answers whether Folder objects arrive from the source unnamed, or whether
    a name is present and the parser is dropping it. Both cases look identical
    downstream, which is why this dumps the entry rather than the parse.
    """
    hdr("T2  WHAT DOES A CONTAINER OBJECT ACTUALLY LOOK LIKE?")

    by_cat = defaultdict(list)
    for oid, cat in land.categories.items():
        by_cat[cat].append(oid)

    out = {}
    for cat in CONTAINER_CATEGORIES:
        ids = sorted(by_cat.get(cat, []), key=lambda x: int(x) if str(x).isdigit() else 0)
        print(f"\n--- {cat}: {len(ids)} objects ---", flush=True)
        if not ids:
            print(f"      none, of {len(land.categories)} categorised objects",
                  flush=True)
            out[cat] = {"count": 0}
            continue

        names = Counter(land.names.get(i, "") for i in ids)
        print(f"      distinct names: {len(names)}   "
              f"most common: {dict(names.most_common(3))}", flush=True)

        # Prefer an object that carries documentation -- if any container
        # survives with prose attached, it is the one most likely to still
        # carry a name too.
        pick = next((i for i in ids
                     if L._documentation(first_entry(land.objects[i]))), ids[0])
        entry = first_entry(land.objects[pick])
        print(f"\n      sample id={pick} "
              f"name={land.names.get(pick, '')!r}", flush=True)
        print(f"      TOP-LEVEL KEYS: {sorted(entry)}", flush=True)
        for k, v in entry.items():
            if k == "categories":
                continue
            print(f"        {k}: {short(v)}", flush=True)
        for c in L._l(entry.get("categories")):
            if not isinstance(c, dict):
                print(f"        category: (non-dict {type(c).__name__})",
                      flush=True)
                continue
            ctype, title = c.get("type"), c.get("title")
            if ctype == "documentation":
                content = c.get("content")
                n = len(L._d(content).get("value", "") or "") \
                    if isinstance(content, dict) else len(str(content))
                print(f"        [doc] {title!r}: {n} chars", flush=True)
                continue
            print(f"        [{ctype}] {title!r}:", flush=True)
            for group, gfields in L._d(c.get("content")).items():
                if isinstance(gfields, dict):
                    for fk, fv in gfields.items():
                        print(f"            {group} / {fk}: {short(fv)}",
                              flush=True)
                else:
                    print(f"            {group}: {short(gfields)}", flush=True)

        rels = L._l(land.relations.get(str(pick)))
        print(f"        relations: {len(rels)}", flush=True)
        views = L._l(land.on_views.get(str(pick)))
        print(f"        appears on views: {len(views)}", flush=True)
        out[cat] = {"count": len(ids), "distinct_names": len(names),
                    "sample": str(pick), "keys": sorted(entry)}
    return out


# --------------------------------------------------------------- T3
def t3_object_model_mapping(land: Landscape, fetcher: Fetcher) -> dict:
    """Look for any object -> model assignment, and for orphan diagrams.

    models_data.js is currently read for views only. If it, or any other
    variable in the data files, assigns a model to an OBJECT, the 247 orphan
    diagram objects stop being guesswork.
    """
    hdr("T3  IS THERE AN OBJECT -> MODEL MAPPING WE HAVE MISSED?")

    models, url, notes = L.fetch_models(fetcher)
    print(f"\n  models_data.js: {url or 'NOT FOUND'}", flush=True)
    for n in notes:
        print(f"    note: {n}", flush=True)
    if not models:
        print("  NOT MEASURED: models file did not load.", flush=True)
    else:
        print(f"  {len(models)} models declared", flush=True)
        shapes = Counter()
        empty = []
        for m in models:
            if not isinstance(m, dict):
                shapes[type(m).__name__] += 1
                continue
            shapes[",".join(sorted(m))] += 1
            views = m.get("views")
            if isinstance(views, list) and not views:
                empty.append(m.get("name"))
        print(f"\n  model record shapes (keys -> count):", flush=True)
        for s, n in shapes.most_common(10):
            print(f"    {n:>5}  {{{s}}}", flush=True)
        print(f"\n  models declaring ZERO views: {len(empty)}", flush=True)
        for name in empty[:20]:
            print(f"    {name}", flush=True)

    # Any variable in the data files beyond the two we consume is
    # undiscovered structure, and is exactly where an object->model map
    # would live.
    print("\n  --- variables present in each data file ---", flush=True)
    seen = {}
    for fname in ("all_objects_on_views.js", "all_objects_relations.js",
                  "all_objects_data_mapping.js"):
        try:
            text = fetcher.get(L.data_url(land.base, fname),
                               conditional=False).text
            variables = L.parse_js_assignments(text)
        except Exception as e:
            print(f"    {fname}: FAILED ({type(e).__name__})", flush=True)
            continue
        seen[fname] = sorted(variables)
        for k, v in variables.items():
            kind = (f"{len(v)} keys" if isinstance(v, dict)
                    else f"{len(v)} items" if isinstance(v, list)
                    else type(v).__name__)
            # Everything the pipeline already reads. Anything else in these
            # files is undiscovered structure and is flagged.
            known = k in ("objectsOnViews", "insiteViews", "objectDataMapping",
                          "objectRelations", "objectData", "insite_models")
            print(f"    {fname}: var {k} = {kind}"
                  f"{'' if known else '   <== NOT CONSUMED BY THE PIPELINE'}",
                  flush=True)

    # insiteViews is the only containment statement we have. What is in it?
    if land.insite_views:
        sample_key = next(iter(land.insite_views))
        sample = land.insite_views[sample_key]
        print(f"\n  insiteViews sample: key={sample_key!r}", flush=True)
        if isinstance(sample, dict):
            for k, v in sample.items():
                print(f"    {k}: {short(v)}", flush=True)
        else:
            print(f"    {short(sample)}", flush=True)

    # Orphan diagram objects: published in no view, on no view, no relations.
    view_ids = {str(v) for v in land.insite_views}
    members = land.views_to_members()
    referenced = {m for oids in members.values() for m in oids}
    orphans = []
    for oid, cat in land.categories.items():
        if not normalise(cat).endswith("view") and "diagram" not in normalise(cat):
            continue
        if str(oid) in view_ids:
            continue
        if str(oid) in referenced:
            continue
        if L._l(land.relations.get(str(oid))):
            continue
        orphans.append(oid)
    print(f"\n  diagram-ish objects that are published in no view, appear on")
    print(f"  no view and carry no relation: {len(orphans)}", flush=True)
    print(f"    by category: "
          f"{dict(Counter(land.categories.get(o, '?') for o in orphans).most_common(8))}",
          flush=True)
    print("  -> if this is non-zero, their package assignment rests on id",
          flush=True)
    print("     proximity alone and should be reported as UNVERIFIED.",
          flush=True)

    return {"models": len(models), "variables": seen, "orphans": len(orphans)}


# --------------------------------------------------------------- T4
def t4_verb_census(land: Landscape) -> dict:
    """Relation verb counts, expanded exactly as bianlib.extract does.

    Every count carries its denominator. `to` is a list, so an edge record is
    not an edge: the extract emits one relation per target, and a census that
    counts records instead of targets reports a different, smaller number --
    which is the most likely explanation of the figure on record.
    """
    hdr("T4  RELATION VERB CENSUS, WITH DENOMINATORS")

    edges = Counter()       # verb -> expanded edges (what the extract emits)
    records = Counter()     # verb -> edge records (what `to` sits on)
    total_edges = total_records = 0
    skipped = 0

    for src, rels in land.relations.items():
        for edge in L._l(rels):
            if not isinstance(edge, dict):
                continue
            via = (edge.get("via") or "").strip()
            if via in L.SKIP_RELATION_VERBS:
                skipped += 1
                continue
            targets = [t for t in (edge.get("to") or [])
                       if isinstance(t, (str, int))]
            records[via] += 1
            edges[via] += len(targets)
            total_records += 1
            total_edges += len(targets)

    print(f"\n  objects carrying relations    {len(land.relations):>8}")
    print(f"  edge RECORDS                  {total_records:>8}")
    print(f"  expanded EDGES                {total_edges:>8}   "
          f"<- what the extract emits")
    print(f"  records skipped (blank verb)  {skipped:>8}")
    print(f"  distinct verbs                {len(edges):>8}", flush=True)

    if not total_edges:
        print("\n  NOT MEASURED: no relation parsed.", flush=True)
        return {"edges": 0}

    print(f"\n  {'verb':<28} {'edges':>9} {'records':>9}  ratio", flush=True)
    for via, n in edges.most_common():
        r = records[via]
        print(f"  {via:<28} {n:>9} {r:>9}  {n / r if r else 0:>5.2f}",
              flush=True)

    return {"edge_records": total_records, "edges": total_edges,
            "by_verb_edges": dict(edges), "by_verb_records": dict(records)}


# --------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="bian-v14",
                    help="source id whose base URL to probe")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--json", metavar="PATH",
                    help="also write the machine-readable summary here")
    args = ap.parse_args()

    # The base URL belongs to the source, not to this tool. A second copy is
    # a second thing to keep right.
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
    finally:
        pass

    if not land.objects:
        print("\nNOT MEASURED: the model did not load; every question below "
              "would report a zero that means nothing.", file=sys.stderr)
        fetcher.close()
        return 1

    summary = {"source": src.id, "base": src.base,
               "objects": len(land.objects)}
    try:
        summary["t1"] = t1_property_inventory(land)
        summary["t2"] = t2_container_entries(land)
        summary["t3"] = t3_object_model_mapping(land, fetcher)
        summary["t4"] = t4_verb_census(land)
    finally:
        print("\n" + fetcher.report(), flush=True)
        fetcher.close()

    hdr("WHAT TO DO WITH THIS")
    t1_hits = summary["t1"].get("hits") or {}
    print(f"""
  T1 packaging property   {'FOUND -> rebuild element grouping from it'
                           if t1_hits else 'NOT PRESENT -> grouping stays graph-derived'}
  T2 container entries    read the TOP-LEVEL KEYS above; a `name` we are not
                          reading is a parser fix, an absent one closes it
  T3 orphan diagrams      {summary['t3'].get('orphans', 0)} still unassignable
                          from anything but id proximity
  T4 verb census          compare `edges` against REFERENCE-DATA and replace
                          that table rather than reconciling it

  Record the answers on Drive, then delete this probe and its workflow.
""", flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1),
                                   encoding="utf-8")
        print(f"wrote {args.json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
