#!/usr/bin/env python3
"""
How is ArchiMate used in the BIAN landscape, and which parts are worth taking?

The pipeline treats ArchiMate as furniture. Relation objects are dropped by
is_structural(), and every view whose diagram object is not a Class or
Sequence diagram is planned as "other" and never fetched -- 1,055 of 2,285
views at v14. That is a defensible default that has never been measured, and
before anything is extracted selectively there has to be a taxonomy of what is
actually there.

This probe builds that taxonomy on four axes, three of which need no view page
at all:

  A  MODEL      data/models_data.js holds `insite_models`: named models, each
                listing its views. The model name is a purpose label --
                "BIAN Reference Model", "BIAN Business Scenario Model",
                "<Domain> Control Record Model". The pipeline has never read
                this file and the bian-extraction skill's orientation map does
                not list it.
  B  VIEW TYPE  the diagram object's own category, which the planner already
                reads: Total view, Capability map view, and the rest.
  C  NOTATION   `typeIconPath` on an object, observed as
                "data/icons/ArchiMate/AllView.png" -- the first path segment
                names the notation. If it is present across the shards it is a
                per-object ArchiMate/UML discriminator the model does not
                otherwise expose. Measured here, not assumed.
  D  COVERAGE   what fraction of a view's members is_wanted() already keeps.
                A view whose members are entirely harvested objects adds
                STRUCTURE, not elements -- which is a different extraction
                job, and a cheaper one.

Q4 samples pages for the concept vocabulary, because that is the one thing the
data files do not hold. Observed on view 53590: StrategyCapability,
BusinessEvent, CompositeGrouping, Canvas, ViewGraphic, and connectors named
<Source><Target><RelationType> such as
StrategyCapabilityStrategyCapabilityTriggering. No UML_ prefix anywhere, so
view_to_plantuml cannot be extended to these -- they need their own renderer.
Each sampled view also gets its small per-view data file, which carries
typeIconPath, viewpointsData and vp_legends -- ArchiMate viewpoints being a
statement of a view's purpose by the modeller. Its path is NOT established:
the documented `views/view_<id>_data.js` returned a non-200 for all 30 views
on the first run, and views.title_from_view_data uses the same path but
swallows every exception, so nothing has ever confirmed it. The candidates in
VIEW_DATA_CANDIDATES are tried in order and the winner is reported.

Prints counts, categories, concept names and icon paths. Never harvested
prose: this log is public, so documentation is reported as a length.

    python3 tools/probe_archimate.py
    python3 tools/probe_archimate.py --no-pages --json probe.json

Read-only. No credentials. Roughly 50 data requests plus two files per sampled
view, paced at one second. Delete once the decision it informs has been made.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bianlib import views as V                             # noqa: E402
from bianlib.fetch import Fetcher                          # noqa: E402
from bianlib.landscape import (INCLUDE_CATEGORIES,         # noqa: E402
                               Landscape, _d, _documentation, _l,
                               is_wanted, parse_js_assignments, view_url)
from bianlib.plan import MODEL_KIND                        # noqa: E402

BASE = "https://bian.org/servicelandscape-14-0-0"
DELAY = 1.0

#: The wireframe example that prompted this probe. At v14 it is object 53590,
#: category "Total view", named "Customer Services", in the BIAN Reference
#: Model -- so "wireframe" describes how the page is drawn, not a model
#: category. Confirmed from a saved copy of the page; re-confirmed each run.
EXAMPLE_VIEW = "53590"

#: models_data.js is not in the documented layout, so its location is a
#: hypothesis. Try the candidates in order and report which answered; a probe
#: that silently falls back has not established anything.
MODELS_CANDIDATES = ("data/models_data.js", "models_data.js",
                     "data/all_models_data.js")

#: Decorations, not concepts. They carry the text and the glyphs.
DECORATIONS = {"label", "icon"}

#: ArchiMate relation blocks are named <Source><Target><RelationType>. The
#: relation types are a closed set in ArchiMate 3, so the suffix is
#: recoverable without guessing where the source name ends.
RELATION_SUFFIXES = (
    "Triggering", "Association", "Composition", "Aggregation", "Assignment",
    "Realization", "Serving", "Access", "Influence", "Specialization",
    "Flow", "Junction",
)

DEFAULT_PER_CELL = 1
MAX_PAGES = 30
W = 58


def hdr(title: str):
    print(f"\n{'=' * 74}\n  {title}\n{'=' * 74}", flush=True)


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def convertible(category: str) -> bool:
    """The planner's own test, imported rather than restated."""
    return category in MODEL_KIND


def split_relation(concept: str) -> tuple[str, str]:
    """('StrategyCapabilityStrategyCapabilityTriggering') -> (ends, 'Triggering')."""
    for suffix in RELATION_SUFFIXES:
        if concept.endswith(suffix) and len(concept) > len(suffix):
            return concept[: -len(suffix)], suffix
    return concept, ""


# --- axis A: models ---------------------------------------------------------

def fetch_models(fetcher: Fetcher) -> tuple[dict, str, list[str]]:
    """{viewId: modelName} from insite_models, plus which URL answered.

    Returns ({}, "", tried) when none did, so the caller can carry on with the
    other three axes rather than losing the whole run to one missing file.
    """
    tried = []
    for candidate in MODELS_CANDIDATES:
        url = f"{fetcher.base}/{candidate}"
        tried.append(candidate)
        try:
            resp = fetcher.get(url, conditional=False)
        except Exception as e:                              # noqa: BLE001
            print(f"    {candidate:<28} {type(e).__name__}", flush=True)
            continue
        if resp.status != 200 or not resp.text.strip():
            print(f"    {candidate:<28} HTTP {resp.status}", flush=True)
            continue
        try:
            models = next(iter(parse_js_assignments(resp.text).values()))
        except Exception as e:                              # noqa: BLE001
            print(f"    {candidate:<28} unparseable ({type(e).__name__})",
                  flush=True)
            continue
        out = {}
        for entry in _l(models):
            name = _d(entry).get("name") or "(unnamed model)"
            for view in _l(_d(entry).get("views")):
                out[str(_d(view).get("id"))] = name
        print(f"    {candidate:<28} {len(_l(models))} models, "
              f"{len(out)} views", flush=True)
        return out, candidate, tried
    return {}, "", tried


# --- the view table ---------------------------------------------------------

def view_table(land: Landscape, view_model: dict) -> dict:
    """One row per view, carrying all four axes.

    Built from the same `set(members) | set(insiteViews)` union the planner
    uses, so the denominator is the planner's and not a second reading.
    """
    members = land.views_to_members()
    all_views = set(members) | {str(v) for v in land.insite_views}

    rows = {}
    for vid in all_views:
        oids = members.get(vid, [])
        kept = sum(1 for o in oids
                   if is_wanted(land.categories.get(o, ""), land.names.get(o, "")))
        category = land.categories.get(vid, "")
        rows[vid] = {
            "category": category or "(not an object in the model)",
            "named": vid in land.categories,
            "convertible": convertible(category),
            "model": view_model.get(vid, "(not in any model)"),
            "members": len(oids),
            "kept_members": kept,
            "name": land.view_name(vid),
        }
    return rows


def report_axes(rows: dict) -> dict:
    """A. Model against view type -- the taxonomy the rest hangs off."""
    hdr("A  Model x view type: what each group of views is for")

    per_model = Counter(r["model"] for r in rows.values())
    print(f"  {len(rows)} views; {sum(1 for r in rows.values() if r['model'] != '(not in any model)')}"
          f" belong to a named model\n", flush=True)

    print(f"  {'model':<44}{'views':>7}{'planned':>9}{'members':>9}", flush=True)
    print(f"  {'-' * 70}", flush=True)
    grouped = []
    for model, n in per_model.most_common(20):
        vs = [r for r in rows.values() if r["model"] == model]
        planned = sum(1 for r in vs if r["convertible"])
        print(f"  {model[:43]:<44}{n:>7}{planned:>9}"
              f"{sum(r['members'] for r in vs):>9}", flush=True)
        grouped.append({"model": model, "views": n, "planned": planned,
                        "members": sum(r["members"] for r in vs)})
    if len(per_model) > 20:
        print(f"  ... and {len(per_model) - 20} more, mostly per-domain models",
              flush=True)

    # The cross-tab is the actual taxonomy. Suppress single-view cells in the
    # long tail so the shape stays readable.
    print(f"\n  {'model':<34}{'view type':<26}{'views':>6}{'med':>6}"
          f"{'covered':>9}", flush=True)
    print(f"  {'-' * 82}", flush=True)
    cells = defaultdict(list)
    for r in rows.values():
        cells[(r["model"], r["category"])].append(r)
    table = []
    for (model, category), vs in sorted(cells.items(), key=lambda kv: -len(kv[1])):
        sizes = sorted(v["members"] for v in vs)
        tot = sum(v["members"] for v in vs)
        kept = sum(v["kept_members"] for v in vs)
        row = {"model": model, "view_type": category, "views": len(vs),
               "median_members": sizes[len(sizes) // 2] if sizes else 0,
               "members": tot, "kept_members": kept,
               "convertible": convertible(category)}
        table.append(row)
        if len(vs) < 3 and len(table) > 24:
            continue
        mark = "" if row["convertible"] else " ."
        print(f"  {model[:33]:<34}{category[:25]:<26}{len(vs):>6}"
              f"{row['median_members']:>6}{pct(kept, tot):>9}{mark}", flush=True)

    print("\n  covered = share of member objects is_wanted() already keeps.\n"
          "  A high figure means the view adds STRUCTURE over objects that are\n"
          "  already harvested; a low one means it holds elements the semantic\n"
          "  pass never sees. Those are different extraction jobs.\n"
          "  ( . = not fetched today )", flush=True)
    return {"per_model": grouped, "cells": table}


def report_view_types(rows: dict, land: Landscape) -> list[dict]:
    """B. Views per category beside OBJECTS per category, with the gap."""
    per_view = Counter(r["category"] for r in rows.values())
    per_object = Counter(land.categories.values())

    hdr("B  View types: view count against diagram-object count")
    print(f"  {'model category':<32}{'views':>7}{'objects':>9}{'gap':>6}"
          f"{'median':>8}{'max':>7}  planned", flush=True)
    print(f"  {'-' * 76}", flush=True)
    out = []
    for category, n in per_view.most_common():
        sizes = sorted(r["members"] for r in rows.values()
                       if r["category"] == category)
        objects = per_object.get(category, 0)
        row = {"category": category, "views": n, "objects": objects,
               "gap": objects - n if objects else 0,
               "median_members": sizes[len(sizes) // 2] if sizes else 0,
               "max_members": max(sizes or [0]),
               "convertible": convertible(category)}
        out.append(row)
        print(f"  {category[:31]:<32}{n:>7}{objects:>9}{row['gap']:>6}"
              f"{row['median_members']:>8}{row['max_members']:>7}"
              f"  {'yes' if row['convertible'] else '.'}", flush=True)

    print("\n  gap = diagram objects of that category minus views resolving to\n"
          "  it. Positive means the model holds diagram objects insiteViews\n"
          "  does not publish. Two denominators; do not reconcile them.",
          flush=True)
    return out


def report_membership(rows: dict, land: Landscape, top: int = 10) -> dict:
    """What object categories sit on each non-convertible view type."""
    members = land.views_to_members()
    cross: dict[str, Counter] = defaultdict(Counter)
    for vid, oids in members.items():
        vcat = rows.get(vid, {}).get("category", "(unknown)")
        for oid in oids:
            cross[vcat][land.categories.get(oid, "Other")] += 1

    hdr("B2  What sits on each non-convertible view type")
    print("  [+] already in INCLUDE_CATEGORIES; [ ] dropped by is_wanted().\n",
          flush=True)
    out = {}
    for vcat, counter in sorted(cross.items(), key=lambda kv: -sum(kv[1].values())):
        if convertible(vcat) or not counter:
            continue
        total = sum(counter.values())
        print(f"  {vcat}  --  {total} placements", flush=True)
        for cat, n in counter.most_common(top):
            flag = "+" if cat in INCLUDE_CATEGORIES else " "
            print(f"    [{flag}] {cat[:37]:<38}{n:>8}{pct(n, total):>8}",
                  flush=True)
        print("", flush=True)
        out[vcat] = counter.most_common(top)
    return out


def report_exclusive(rows: dict, land: Landscape, floor: int = 20) -> list[dict]:
    """Where each category is placed: convertible views against the rest.

    The first version printed a literal 0 in the "on convertible" column. It
    was never wrong, because the filter only admitted categories with a zero
    there -- but a constant formatted as a measurement is precisely the
    failure this project keeps making. The count is the measured one now, and
    near-exclusive categories are admitted too, so the column can be non-zero
    and the reader can see it is being measured.
    """
    members = land.views_to_members()
    conv, other = Counter(), Counter()
    for vid, oids in members.items():
        bucket = conv if rows.get(vid, {}).get("convertible") else other
        for oid in oids:
            bucket[land.categories.get(oid, "Other")] += 1

    hdr("B3  Where each category is placed")
    print("  Categories placed wholly or almost wholly on views the pipeline\n"
          "  never fetches. 'convertible' counts placements on class and\n"
          f"  sequence diagrams. Floor: {floor} placements elsewhere, and at\n"
          "  least 90% of placements off convertible views.\n", flush=True)
    print(f"  {'category':<38}{'other':>10}{'convertible':>13}"
          f"{'other share':>13}  wanted", flush=True)
    print(f"  {'-' * 82}", flush=True)
    out = []
    for cat, n in other.most_common():
        c = conv.get(cat, 0)
        if n < floor or n / (n + c) < 0.9:
            continue
        wanted = cat in INCLUDE_CATEGORIES
        print(f"  {cat[:37]:<38}{n:>10}{c:>13}{pct(n, n + c):>13}"
              f"  {'yes' if wanted else 'no'}", flush=True)
        out.append({"category": cat, "other": n, "convertible": c,
                    "other_share": round(n / (n + c), 4), "wanted": wanted})
    if not out:
        print(f"  (nothing above the {floor}-placement floor)", flush=True)
    return out


# --- axis C: notation -------------------------------------------------------

def report_notation(land: Landscape) -> dict:
    """C. typeIconPath as an ArchiMate/UML discriminator, if it is there.

    Observed once, on view 53590: "data/icons/ArchiMate/AllView.png". Whether
    the shards carry it for every object is the question -- so the payload
    keys are inventoried first rather than the field being assumed.
    """
    hdr("C  Notation from typeIconPath")

    keys = Counter()
    for obj in land.objects.values():
        for k in _d(obj):
            keys[k] += 1
    print(f"  object payload keys across {len(land.objects)} objects:",
          flush=True)
    for k, n in keys.most_common(12):
        print(f"    {k:<24}{n:>9}{pct(n, len(land.objects)):>9}", flush=True)

    if "typeIconPath" not in keys:
        print("\n  typeIconPath is NOT on the shard payloads -- it exists only\n"
              "  on the per-view data files. Axis C then costs one request per\n"
              "  view and cannot classify objects in bulk. Q4 still reads it\n"
              "  for the sampled views.", flush=True)
        return {"present": False, "keys": keys.most_common(12)}

    notation, by_cat = Counter(), defaultdict(Counter)
    for oid, obj in land.objects.items():
        path = _d(obj).get("typeIconPath") or ""
        parts = [p for p in str(path).split("/") if p]
        # data/icons/<Notation>/<Type>.png -- take the segment after "icons".
        note = ""
        if "icons" in parts:
            i = parts.index("icons")
            note = parts[i + 1] if i + 1 < len(parts) else ""
        note = note or "(none)"
        notation[note] += 1
        by_cat[note][land.categories.get(oid, "Other")] += 1

    print(f"\n  {'notation':<24}{'objects':>10}{'share':>9}", flush=True)
    print(f"  {'-' * 44}", flush=True)
    for note, n in notation.most_common():
        print(f"  {note[:23]:<24}{n:>10}{pct(n, len(land.objects)):>9}",
              flush=True)

    print("\n  Top categories per notation, with [+] for already-wanted:",
          flush=True)
    out_cats = {}
    for note, counter in notation.most_common(6):
        print(f"\n  {note}", flush=True)
        rows = by_cat[note].most_common(10)
        out_cats[note] = rows
        for cat, n in rows:
            flag = "+" if cat in INCLUDE_CATEGORIES else " "
            print(f"    [{flag}] {cat[:39]:<40}{n:>9}", flush=True)

    print("\n  This is the selective-extraction lever: if the notation split is\n"
          "  clean, an ArchiMate allowlist can be expressed against it rather\n"
          "  than against 140-odd category names.", flush=True)
    return {"present": True, "notation": notation.most_common(),
            "by_category": out_cats}


# --- axis D and the example -------------------------------------------------

def report_example(rows: dict, land: Landscape, vid: str) -> dict:
    """Settle what the model calls the wireframe example, from this run."""
    hdr(f"View {vid}, the wireframe example")
    row = rows.get(vid)
    if row is None:
        print(f"  {vid} is in neither objectsOnViews nor insiteViews.",
              flush=True)
        return {"id": vid, "known": False}

    for k in ("category", "name", "model", "members", "kept_members"):
        print(f"  {k:<16}{row[k]!r}", flush=True)
    print(f"  {'planned':<16}{'yes' if row['convertible'] else 'no'}",
          flush=True)
    print(f"  {'covered':<16}{pct(row['kept_members'], row['members'])}",
          flush=True)

    breakdown = Counter(land.categories.get(o, "Other")
                        for o in land.views_to_members().get(vid, []))
    print("\n  member categories:", flush=True)
    for cat, n in breakdown.most_common(15):
        flag = "+" if cat in INCLUDE_CATEGORIES else " "
        print(f"    [{flag}] {cat[:39]:<40}{n:>6}", flush=True)
    return {"id": vid, "known": True, **row,
            "members_by_category": breakdown.most_common(15)}


# --- Q4: sampled pages ------------------------------------------------------

def choose_samples(rows: dict, per_cell: int, extra: str) -> list[str]:
    """Exemplars per (model, view type) cell, round-robin across cells.

    Stratified by cell rather than by view type, because the same view type
    serves different purposes in different models.

    Round-robin, and this matters: taking per_cell from each cell in size
    order and then truncating to MAX_PAGES defeats the stratification it was
    written for. The first run did exactly that -- --per-cell 10 spent all 30
    pages on the three largest cells and sampled no Capability map view, no
    ArchiMate total view, no Ecosystem view and no Business Model Canvas, the
    view types the probe exists to characterise. Every cell now gets its first
    exemplar before any cell gets a second.
    """
    cells: dict[tuple, list[str]] = defaultdict(list)
    for vid, r in rows.items():
        if not r["convertible"]:
            cells[(r["model"], r["category"])].append(vid)

    ordered = {cell: sorted(vids, key=lambda v: -rows[v]["members"])
               for cell, vids in cells.items()}
    order = sorted(ordered, key=lambda c: -len(ordered[c]))

    picked: list[str] = []
    for rank in range(per_cell):
        for cell in order:
            if rank < len(ordered[cell]):
                picked.append(ordered[cell][rank])
    if extra in rows and extra in picked:
        picked.remove(extra)
    if extra in rows:
        picked.insert(0, extra)
    return picked[:MAX_PAGES]


#: Where the per-view data file lives. `views/view_<id>_data.js` is what the
#: skill's orientation map and views.title_from_view_data both say -- and the
#: first probe run got a non-200 for all 30 sampled views, so the documented
#: path is unconfirmed rather than known. title_from_view_data swallows every
#: exception and returns "", and the bulk pipeline never calls it, so nothing
#: would have noticed. Try the candidates, lock on to whichever answers, and
#: say which one it was.
VIEW_DATA_CANDIDATES = (
    "views/view_{vid}_data.js",
    "data/view_{vid}_data.js",
    "view_{vid}_data.js",
    "data/views/view_{vid}_data.js",
)


def probe_view_data(fetcher: Fetcher, vid: str, pattern: str = "") -> dict:
    """The 2 KB per-view data file: icon path, viewpoints, legends.

    Returns {"error": ...} on failure -- never a silently empty result, and
    never a zero that could be read as a measurement.
    """
    patterns = [pattern] if pattern else list(VIEW_DATA_CANDIDATES)
    problems = []
    for candidate in patterns:
        url = f"{fetcher.base}/{candidate.format(vid=vid)}"
        try:
            resp = fetcher.get(url, conditional=False)
        except Exception as e:                              # noqa: BLE001
            problems.append(f"{candidate}: {type(e).__name__}")
            continue
        if resp.status != 200 or not resp.text.strip():
            problems.append(f"{candidate}: HTTP {resp.status}")
            continue
        try:
            parsed = parse_js_assignments(resp.text)
        except Exception as e:                              # noqa: BLE001
            problems.append(f"{candidate}: unparseable ({type(e).__name__})")
            continue
        objdata = _d(parsed.get("objectData")).get(str(vid)) or {}
        entry = _l(_d(objdata).get("data"))
        first = _d(entry[0]) if entry else {}
        return {
            "pattern": candidate,
            "typeIconPath": _d(objdata).get("typeIconPath") or "",
            "type": first.get("type") or "",
            "viewpoints": len(_l(parsed.get("viewpointsData"))),
            "legends": len(_d(parsed.get("vp_legends"))),
            "objectReferences": len(_d(parsed.get("objectReferences"))),
            "viewReferences": len(_d(parsed.get("viewReferences"))),
            "documentation_chars": sum(len(t)
                                       for t in _documentation(first).values()),
        }
    return {"error": "; ".join(problems)}


def report_pages(rows: dict, fetcher: Fetcher, sample: list[str]) -> dict:
    """Q4. Concept vocabulary and viewpoints, from a stratified sample."""
    hdr(f"D  Sampled pages: concept vocabulary and viewpoints ({len(sample)})")

    per_type: dict[str, Counter] = defaultdict(Counter)
    relations: Counter = Counter()
    icons: Counter = Counter()
    navigation: Counter = Counter()
    per_view, failed = [], []
    meta_ok, meta_problems = 0, Counter()
    pattern = ""

    for vid in sample:
        r = rows[vid]
        try:
            meta = probe_view_data(fetcher, vid, pattern)
        except Exception as e:                              # noqa: BLE001
            meta = {"error": type(e).__name__}
        if meta.get("error"):
            meta_problems[meta["error"]] += 1
        else:
            meta_ok += 1
            # Lock on after the first success so the dead candidates are not
            # re-requested 29 more times.
            pattern = pattern or meta.get("pattern", "")
        if meta.get("typeIconPath"):
            icons[meta["typeIconPath"]] += 1

        try:
            resp = fetcher.get(view_url(fetcher.base, vid), conditional=False)
            if resp.status != 200:
                failed.append((vid, f"HTTP {resp.status}"))
                continue
            blocks = V.blocks(V.extract_svg(resp.text))
        except Exception as e:                              # noqa: BLE001
            failed.append((vid, type(e).__name__))
            continue

        concepts = Counter(b.get("concept") or "(none)" for b in blocks.values())
        real = Counter({c: n for c, n in concepts.items()
                        if c not in DECORATIONS})
        per_type[r["category"]] += real
        for concept, n in real.items():
            _, kind = split_relation(concept)
            if kind:
                relations[kind] += n

        semantic = {b["semantic"] for b in blocks.values() if b.get("semantic")}
        nav = sum(1 for s in semantic
                  if rows.get(s, {}).get("named") or s in rows)
        if nav:
            navigation[r["category"]] += nav
        print(f"  {vid:<8}{r['category'][:22]:<23}{r['model'][:26]:<27}"
              f"{len(blocks):>5} blocks{len(real):>4} concepts"
              f"{len(semantic):>5} semantic{nav:>5} nav", flush=True)
        per_view.append({"id": vid, "category": r["category"],
                         "model": r["model"], "blocks": len(blocks),
                         "concepts": len(real), "semantic": len(semantic),
                         "navigation_targets": nav,
                         "members": r["members"], **meta})

    print(f"\n  pages fetched {len(per_view)} of {len(sample)}; "
          f"{len(failed)} failed", flush=True)
    for vid, why in failed:
        print(f"    {vid}  {why}", flush=True)
    print(f"  view data files read {meta_ok} of {len(sample)}"
          + (f", pattern {pattern}" if pattern else ""), flush=True)
    for why, n in meta_problems.most_common(4):
        print(f"    {n:>4}  {why[:100]}", flush=True)

    print("\n  Concept vocabulary per view type. A UML_ prefix means the\n"
          "  existing converter understands the page; anything else needs its\n"
          "  own renderer.\n", flush=True)
    out_concepts = {}
    for cat, counter in sorted(per_type.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"  {cat}", flush=True)
        out_concepts[cat] = counter.most_common(15)
        for concept, n in counter.most_common(15):
            base, kind = split_relation(concept)
            note = f"  <- relation: {kind}" if kind else ""
            print(f"    {concept[:W - 1]:<{W}}{n:>6}{note}", flush=True)
        print("", flush=True)

    if relations:
        print("  Relation types seen across the sample:", flush=True)
        for kind, n in relations.most_common():
            print(f"    {kind:<24}{n:>7}", flush=True)

    if icons:
        print("\n  typeIconPath on the sampled views:", flush=True)
        for path, n in icons.most_common():
            print(f"    {path[:W - 1]:<{W}}{n:>5}", flush=True)

    if navigation:
        print("\n  Navigation targets -- members that are themselves views:",
              flush=True)
        for cat, n in navigation.most_common():
            print(f"    {cat[:39]:<40}{n:>7}", flush=True)
        print("  A view built mostly from other views is an index, not\n"
              "  content. Architecture overview was 51% Class diagram members\n"
              "  and 28% Total view members on the first run.", flush=True)

    # The denominator here is view data files successfully READ, not views
    # sampled. The first run printed "viewpoints: 0" and a conclusion drawn
    # from it while every one of the 30 fetches had failed -- a zero that was
    # an absence of measurement wearing the label of a measurement.
    if not meta_ok:
        print("\n  viewpoints: NOT MEASURED -- no view data file was read.\n"
              "  Nothing follows about viewpoints from this run. Find the real\n"
              "  path from a saved page's script tags before concluding.",
              flush=True)
        vps = None
    else:
        vps = sum(v.get("viewpoints") or 0 for v in per_view)
        print(f"\n  viewpoints declared, across {meta_ok} view data files read:"
              f" {vps}", flush=True)
        if not vps:
            print("  None declared. ArchiMate viewpoints would have stated a\n"
                  "  view's purpose outright; without them, purpose comes from\n"
                  "  axis A -- the model a view belongs to.", flush=True)

    return {"views": per_view, "concepts": out_concepts,
            "navigation": navigation.most_common(),
            "view_data_pattern": pattern, "view_data_read": meta_ok,
            "view_data_problems": meta_problems.most_common(4),
            "viewpoints": vps,
            "relations": relations.most_common(), "icons": icons.most_common(),
            "failed": failed}


# --- documentation ----------------------------------------------------------

def report_documentation(land: Landscape, categories: list[str]) -> list[dict]:
    """Is there anything to harvest, or is it furniture?"""
    hdr("E  Documentation coverage on candidate categories")
    print("  Lengths only: this log is public.\n", flush=True)
    print(f"  {'category':<34}{'objects':>9}{'with docs':>11}{'share':>8}"
          f"{'median len':>12}  allowlist", flush=True)
    print(f"  {'-' * 78}", flush=True)

    by_cat: dict[str, list[str]] = defaultdict(list)
    for oid, cat in land.categories.items():
        by_cat[cat].append(oid)

    out = []
    for cat in categories:
        oids = by_cat.get(cat, [])
        lengths = []
        for oid in oids:
            # Unwrapped exactly as Landscape._index does it.
            entry = _l(_d(land.objects.get(oid)).get("data"))
            docs = _documentation(_d(entry[0]) if entry else {})
            total = sum(len(t) for t in docs.values())
            if total:
                lengths.append(total)
        lengths.sort()
        median = lengths[len(lengths) // 2] if lengths else 0
        print(f"  {cat[:33]:<34}{len(oids):>9}{len(lengths):>11}"
              f"{pct(len(lengths), len(oids)):>8}{median:>12}"
              f"  {'yes' if cat in INCLUDE_CATEGORIES else 'no'}", flush=True)
        out.append({"category": cat, "objects": len(oids),
                    "with_documentation": len(lengths),
                    "median_length": median,
                    "in_allowlist": cat in INCLUDE_CATEGORIES})

    print("\n  Near-zero coverage settles it: furniture, whatever view it sits\n"
          "  on. High coverage outside the allowlist is the case for changing\n"
          "  it -- as a decision, with a before-and-after count.", flush=True)
    return out


# --- the verdict table ------------------------------------------------------

def report_verdicts(axes: dict, min_views: int = 5) -> list[dict]:
    """Turn the cross-tab into candidate extraction verdicts.

    Deliberately mechanical, and deliberately not a decision: it applies two
    stated rules to measured numbers so the cells needing a human judgement
    are the short list rather than all 300.
    """
    hdr("F  Candidate verdicts per (model, view type)")
    print("  Rules applied, both from measurements above:\n"
          "    covered >= 80%  -> members already harvested; the view would\n"
          "                       add STRUCTURE (relations, grouping) only\n"
          "    covered <  40%  -> the view carries elements the semantic pass\n"
          "                       never sees; extracting elements is the job\n"
          "  Anything between is mixed and needs reading.\n", flush=True)
    print(f"  {'model':<32}{'view type':<24}{'views':>6}{'covered':>9}"
          f"  verdict", flush=True)
    print(f"  {'-' * 86}", flush=True)

    out = []
    for cell in sorted(axes["cells"], key=lambda c: -c["views"]):
        if cell["views"] < min_views or cell["convertible"]:
            continue
        share = (cell["kept_members"] / cell["members"]) if cell["members"] else 0
        if share >= 0.8:
            verdict = "structure only"
        elif share < 0.4:
            verdict = "elements missing"
        else:
            verdict = "mixed - read it"
        print(f"  {cell['model'][:31]:<32}{cell['view_type'][:23]:<24}"
              f"{cell['views']:>6}{pct(cell['kept_members'], cell['members']):>9}"
              f"  {verdict}", flush=True)
        out.append({**cell, "covered": round(share, 4), "verdict": verdict})
    if not out:
        print(f"  (no non-convertible cell has {min_views} or more views)",
              flush=True)
    return out


# --- main -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--delay", type=float, default=DELAY)
    ap.add_argument("--per-cell", type=int, default=DEFAULT_PER_CELL,
                    help="view pages to sample per (model, view type) cell")
    ap.add_argument("--no-pages", action="store_true",
                    help="axes A, B, C and E from the data files only")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    fetcher = Fetcher(args.base, delay=args.delay)

    print(f"  looking for insite_models under {args.base}", flush=True)
    view_model, models_url, tried = fetch_models(fetcher)
    if not view_model:
        print(f"  NOT FOUND. Tried: {', '.join(tried)}. Axis A is unavailable;\n"
              "  the other three still work. Find the real path in the page\n"
              "  source before concluding the file does not exist.", flush=True)

    print(f"\n  loading the model from {args.base}", flush=True)
    land = Landscape(args.base).load(fetcher)
    wanted = sum(1 for oid, cat in land.categories.items()
                 if is_wanted(cat, land.names.get(oid, "")))
    print(f"  {len(land.objects)} objects, {wanted} kept by is_wanted()",
          flush=True)

    rows = view_table(land, view_model)
    result = {
        "base": args.base,
        "models_url": models_url,
        "objects": len(land.objects),
        "wanted": wanted,
        "views": len(rows),
        "axis_a": report_axes(rows),
        "axis_b": report_view_types(rows, land),
        "membership": report_membership(rows, land),
        "exclusive": report_exclusive(rows, land),
        "axis_c": report_notation(land),
        "example": report_example(rows, land, EXAMPLE_VIEW),
    }

    if not args.no_pages:
        sample = choose_samples(rows, args.per_cell, EXAMPLE_VIEW)
        result["axis_d"] = report_pages(rows, fetcher, sample)

    candidates = ["Business function", "Work package", "Capability", "Grouping"]
    candidates += [r["category"] for r in result["exclusive"]
                   if r["category"] not in candidates]
    result["documentation"] = report_documentation(land, candidates[:15])
    result["verdicts"] = report_verdicts(result["axis_a"])

    hdr("REQUESTS")
    for k, v in sorted(fetcher.stats.items()):
        print(f"    {k:<16}{v}", flush=True)

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  wrote {args.json}", flush=True)

    hdr("READING THIS")
    print("""
  A  which models the views belong to. The model name is the closest thing
     to a statement of purpose the landscape publishes, and views outside
     every model are worth checking against the count of views that are not
     objects -- if the two sets coincide, one file explains the other.
  B  view type against diagram-object count. Quote both denominators.
  C  whether typeIconPath gives a clean notation split. If it does, a
     selective ArchiMate extraction can be expressed against it.
  D  the concept vocabulary a renderer would have to understand, and
     whether any viewpoint states a view's purpose outright.
  E  whether the candidate categories carry documentation at all.
  F  mechanical verdicts. They are a short list to read, not a decision.

  Record the numbers in REFERENCE-DATA.md with the digest they were measured
  at. Log a decision only if alternatives existed.
""", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
