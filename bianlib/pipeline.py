#!/usr/bin/env python3
"""
The full-landscape pipeline, in three stages that run as separate CI jobs.

    plan       fetch the model once, classify every view, write the work plan
    chunk N    fetch one slice of view pages and convert them
    assemble   merge every part, verify the whole, write the publishable bundle

Why it is split this way:

**The model is fetched once, not once per chunk.** The 47 shards are ~55 MB.
Re-reading them in ten chunk jobs would ask bian.org for half a gigabyte to
produce output identical to reading them once. The plan job reads them, and
everything downstream travels between jobs as GitHub artifacts.

**A chunk needs nothing but the plan.** Diagram titles come from insiteViews and
are baked into the plan, so a chunk job makes exactly one request per view page
and not one more.

**Verification sits between the chunks, not after them.** Each chunk proves
itself before the next starts; the run stops at the first chunk that cannot.
Ten sequential jobs cost a little wall-clock time and buy a failure that names
its own cause and wastes no further requests.

Nothing here publishes. `core.publish` does that, once, from the assembled
bundle — and refuses a bundle marked incomplete.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from bianlib import landscape as L
from bianlib import plan as P
from bianlib import views as V
from bianlib.fetch import Fetcher, SourceUnhappy, robots_disallows
from core.render import write_bundles

#: Diagrams are large. 20 to a file keeps each one comfortably readable
#: through the Drive connector; semantic items are much smaller and group at
#: 250 as they always have.
PER_FILE = {"Sequence diagram": 20, "Class diagram": 20}
PER_FILE_DEFAULT = 250


def _write_items(path: Path, items: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _read_items(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def _cache(path: Path) -> dict:
    """{url: {"etag", "last_modified", "item"}} carried between runs.

    The validators alone would not be enough. A 304 has no body, so a view
    whose page has not changed would convert to nothing and the chunk would
    fail its own count check — the previous run's converted item has to come
    back with it. That is what makes a weekly refresh cheap for bian.org:
    1,231 conditional requests, almost all answered with no content at all.
    """
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _validators_from(cache: dict) -> dict:
    return {url: {"etag": e.get("etag", ""),
                  "last_modified": e.get("last_modified", "")}
            for url, e in cache.items()}


# --- stage 1: plan ---------------------------------------------------------

def do_plan(source, parts: Path, chunk_count: int, delay: float,
            limit: int = 0) -> int:
    """Fetch the model, classify the views, write the plan and the semantic
    half of the bundle. No view pages are fetched here."""
    fetcher = Fetcher(source.base, delay=delay)

    # Politeness before anything else: if the site asks us not to, stop.
    blocked = robots_disallows(fetcher, "/" + source.base.split("/", 3)[-1])
    if blocked:
        print(f"\n  robots.txt disallows {blocked} — not harvesting.",
              flush=True)
        return 2
    print("  robots.txt: no rule against this path", flush=True)

    landscape = L.Landscape(source.base, object_view=source.object_view)
    landscape.load(fetcher)

    items, dropped, skipped = landscape.semantic_items()
    print(f"\n  kept {len(items)} of {len(landscape.objects)} objects "
          f"({sum(dropped.values())} filtered out as non-content)", flush=True)
    for cat, n in sorted(dropped.items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {cat:<32} {n:>7}", flush=True)
    if skipped:
        print(f"  skipped {len(skipped)} malformed objects", flush=True)

    work = P.build(landscape, source.id, chunk_count, limit=limit,
                   expected={"sequence": source.expected_sequence_views,
                             "class": source.expected_class_views})
    print("\n  WORK PLAN\n" + P.describe(work), flush=True)

    _write_items(parts / "model" / "items.json", items)
    P.save(parts / "plan.json", work)
    P.save(parts / "model" / "summary.json", {
        "objects": len(landscape.objects),
        "shards": len(landscape.shards),
        "semantic_items": len(items),
        "relations": len(landscape.relations),
        "views_known": len(landscape.insite_views),
        "malformed_skipped": len(skipped),
        "notes": landscape.notes,
    })

    print(f"\n  fetch: {fetcher.report()}", flush=True)
    fetcher.close()

    # Source-level thresholds first: a model this wrong must not be chunked.
    ok = P.report("MODEL", source.plan_checks(landscape, items))
    if work["problems"]:
        ok = False
        print("\n  VIEW CLASSIFICATION", flush=True)
        for problem in work["problems"]:
            print(f"    [FAIL] {problem}", flush=True)
        print("\n    The view classifier's counts do not match the known "
              "shape of this\n    landscape. Harvesting on this plan would "
              "acquire the wrong set of\n    diagrams. Check "
              "bianlib/plan.py:classify against a sample view.", flush=True)
    return 0 if ok else 1


# --- stage 2: one chunk ----------------------------------------------------

def do_chunk(source, parts: Path, index: int, delay: float,
             cache_in: Path = None) -> int:
    work = P.load(parts / "plan.json")
    chunk = P.chunk_of(work, index)
    views = chunk["views"]
    print(f"  chunk {index} of {work['chunk_count']}: {len(views)} views, "
          f"{chunk['members']} members, plan {work['plan_sha']}", flush=True)

    cache = _cache(cache_in)
    fetcher = Fetcher(work["base"], delay=delay,
                      validators=_validators_from(cache))
    if cache:
        print(f"  {len(cache)} pages cached from a previous run", flush=True)

    items, failed, skipped, reused = [], [], [], 0
    strays: list[tuple[str, int]] = []
    attributes = unassigned = 0
    started = time.time()

    for n, entry in enumerate(views, 1):
        vid = entry["id"]
        url = L.view_url(work["base"], vid)
        try:
            resp = fetcher.get(url)
        except SourceUnhappy as e:
            # The breaker has tripped. Stop the chunk here; the surrounding
            # verification will fail it and no later chunk will start.
            print(f"\n  STOPPING: {e}", flush=True)
            failed.append({"id": vid, "reason": "circuit breaker"})
            break
        except Exception as e:
            failed.append({"id": vid, "reason": f"{type(e).__name__}"})
            continue

        if resp.status == 404:
            skipped.append({"id": vid, "reason": "view page absent (404)"})
            cache.pop(url, None)
            continue

        if resp.from_cache:
            # Unchanged since last time. Reuse what it converted to then,
            # rather than asking for a body we already have.
            known = cache.get(url, {}).get("item")
            if known:
                items.append(known)
                reused += 1
                continue
            # Cached validator without a cached item: ask again, in full.
            fetcher.validators.pop(url, None)
            try:
                resp = fetcher.get(url, conditional=False)
            except Exception as e:
                failed.append({"id": vid, "reason": f"{type(e).__name__}"})
                continue
        try:
            parsed = V.parse_view(resp.text, url, known_title=entry.get("name", ""),
                                  base=work["base"])
            body, kind = V.render(parsed, url)
        except Exception as e:
            failed.append({"id": vid, "reason": f"{type(e).__name__}"})
            continue
        if body is None:
            skipped.append({"id": vid, "reason": "not a sequence or class diagram"})
            continue

        attributes += sum(len(c["attributes"]) for c in parsed["classes"].values())
        attributes += parsed["unassigned_attrs"]
        unassigned += parsed["unassigned_attrs"]
        if parsed["unassigned_attrs"]:
            strays.append((vid, parsed["unassigned_attrs"]))
        item = {
            "id": f"view-{vid}",
            "name": parsed["title"],
            "category": ("Sequence diagram" if kind == "sequence"
                         else "Class diagram"),
            "body": V.diagram_markdown(parsed, body, kind, url),
        }
        items.append(item)
        validator = fetcher.validators.get(url)
        if validator:
            cache[url] = dict(validator, item=item)
        if n % 25 == 0 or n == len(views):
            print(f"    {n:>4}/{len(views)}  {len(items)} converted, "
                  f"{len(skipped)} skipped, {len(failed)} failed", flush=True)

    fetcher.close()
    result = {
        "chunk": index,
        "plan_sha": work["plan_sha"],
        "source": work["source"],
        "attempted": len(items) + len(failed) + len(skipped),
        "converted": len(items),
        "failed": failed,
        "skipped": skipped,
        "reused_from_cache": reused,
        "attributes": attributes,
        "unattached_views": sorted(strays, key=lambda s: -s[1])[:20],
        "unassigned_attrs": unassigned,
        "seconds": round(time.time() - started, 1),
        "fetch": dict(fetcher.stats),
    }

    part = parts / f"chunk-{index:02d}"
    _write_items(part / "items.json", items)
    P.save(part / "result.json", result)
    if cache_in:
        P.save(part / "cache.json",
               {url: e for url, e in cache.items()
                if url in {L.view_url(work["base"], v["id"]) for v in views}})

    print(f"\n  fetch: {fetcher.report()}", flush=True)
    for entry in (failed + skipped)[:10]:
        print(f"    {entry['id']}: {entry['reason']}", flush=True)
    if strays:
        worst = sorted(strays, key=lambda s: -s[1])[:5]
        print(f"  {unassigned} attribute rows across {len(strays)} views could "
              f"not be tied to a box and were", flush=True)
        print(f"  carried through as '(unattached attributes)'. Worst: "
              + ", ".join(f"view {v} ({n})" for v, n in worst), flush=True)

    ok = P.report(f"CHUNK {index} VERIFICATION",
                  P.verify_chunk(result, chunk, work["plan_sha"]))
    if not ok:
        print("\n    This chunk did not harvest what it was asked to, so the "
              "run stops\n    here rather than continuing to request pages. "
              "Re-run failed jobs to\n    retry just this chunk — the plan "
              "artifact is reused.", flush=True)
    return 0 if ok else 1


# --- stage 3: assemble -----------------------------------------------------

def do_assemble(source, parts: Path, outdir: Path) -> int:
    work = P.load(parts / "plan.json")
    model = _read_items(parts / "model" / "items.json")
    summary = P.load(parts / "model" / "summary.json")

    results, diagrams = [], []
    for part in sorted(parts.glob("chunk-*")):
        if (part / "result.json").is_file():
            results.append(P.load(part / "result.json"))
            diagrams += _read_items(part / "items.json")

    print(f"  {len(model)} semantic items + {len(diagrams)} diagrams from "
          f"{len(results)} chunks", flush=True)

    ok = P.report("LANDSCAPE VERIFICATION", P.verify_run(work, results))
    if not ok:
        print("\n    Not writing a bundle: publishing an incomplete landscape "
              "would\n    replace the last good copy on Drive with a thinner "
              "one.", flush=True)
        return 1

    counts = {}
    for item in diagrams:
        counts[item["category"]] = counts.get(item["category"], 0) + 1

    written = write_bundles(
        outdir, source.id, source.name, model + diagrams,
        per_file=PER_FILE, per_file_default=PER_FILE_DEFAULT, complete=True,
        extra_index_lines=[
            f"Landscape version: `{work['landscape']}`.",
            f"Merged from {summary['shards']} data shards "
            f"({summary['objects']} unique objects).",
            f"Semantic content: {len(model)} items.",
            f"Diagrams: {len(diagrams)} — "
            + ", ".join(f"{v} {k.lower()}s"
                        for k, v in sorted(counts.items())) + ".",
            f"Harvested in {work['chunk_count']} verified chunks "
            f"(plan `{work['plan_sha']}`).",
        ])

    # The source's own checks, run here rather than by a second `run.py
    # validate` pass: that would re-probe bian.org for no reason, and give a
    # verified landscape a way to fail on a transient network hiccup.
    results_ok = True
    print("\n  BUNDLE CHECKS", flush=True)
    for check in source.checks(outdir):
        mark = "PASS" if check.ok else ("WARN" if check.warn else "FAIL")
        print(f"    [{mark}] {check.name:<48} {check.detail}", flush=True)
        if not check.ok and not check.warn:
            results_ok = False
            if check.hint:
                print(f"           {check.hint}", flush=True)
    if not results_ok:
        print("\n    The bundle was written but did not pass its own checks, "
              "so it is\n    not marked publishable.", flush=True)
        return 1

    P.save(outdir / "harvest.json", {
        "plan": {k: work[k] for k in
                 ("plan_sha", "landscape", "view_count", "chunk_count",
                  "classification", "generated")},
        "model": summary,
        "chunks": [{k: r[k] for k in
                    ("chunk", "attempted", "converted", "seconds")}
                   | {"failed": len(r["failed"]), "skipped": len(r["skipped"])}
                   for r in sorted(results, key=lambda r: r["chunk"])],
        "assembled": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    merged: dict = {}
    for part in sorted(parts.glob("chunk-*/cache.json")):
        merged.update(P.load(part))
    if merged:
        P.save(parts / "cache.json", merged)
        print(f"  {len(merged)} pages cached for the next run", flush=True)

    print(f"\n  {written['files_written']} files -> {outdir}", flush=True)
    return 0
