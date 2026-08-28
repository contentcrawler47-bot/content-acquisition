#!/usr/bin/env python3
"""
Census the BIAN Service Landscape: how many of each thing, and of what.

Every number this prints carries its denominator and its definition. That is
the whole point of the tool. Three separate bugs in this project came from a
count that was correct but labelled as something it was not:

  018  a substring test for the service domain category could never match the
       landscape's spelling, and locked onto a 2-object stray instead
  019  a dict keyed by name collapsed duplicates, so a line labelled
       "landscape service domains" reported distinct names, not objects
  ---  a workflow comment recorded the same bug as "222 of 340" where the
       reference says "222 of 367" — model total and view membership swapped

So: counts are reported as OBJECTS or as DISTINCT NAMES, never as a bare
"service domains", and every ratio names what it is over.

    python3 tools/landscape_census.py
    python3 tools/landscape_census.py --json census.json

Read-only. No credentials. About 48 requests, paced, gzip-encoded — roughly
12 MB and two minutes. Safe to run anywhere with network access, but prefer
the workflow so the pacing and the User-Agent stay consistent.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict

BASE = "https://bian.org/servicelandscape-14-0-0"
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
PACE = 1.0

# The allowlist of wanted categories. Matched NORMALISED (see normalise), so
# both spellings of the service domain category are covered by one entry --
# they are listed separately anyway, because the pair is the exact thing that
# catches people out and a reader should see it here.
ALLOWLIST = [
    "ServiceDomain", "Service Domain",
    "ServiceOperation", "ServiceOperationType",
    "ServiceGroup", "SDServiceGroup", "BusinessService", "ControlRecord",
    "AssetType", "AnalyticsObject", "Business object",
    "BehaviorQualifier", "BehaviorQualifierType",
    "ReferenceInformation", "BusinessArea", "BusinessDomain",
    "FunctionalPattern", "Capability", "Grouping", "GenericArtifact",
    "ActionTerm",
]

SERVICE_DOMAIN_SPELLINGS = ["ServiceDomain", "Service Domain"]


def normalise(s: str) -> str:
    """Case, punctuation and whitespace removed. 'Service Domain' ==
    'ServiceDomain' == 'service-domain'. Unicode is folded to NFKD first so a
    non-breaking space or a curly apostrophe cannot survive as a difference."""
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def get(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw.decode("utf-8", errors="replace")


def parse_js_vars(text: str) -> dict:
    """A .js file may define more than one variable -- all_objects_on_views.js
    defines two. raw_decode in a loop, not json.loads on the whole body."""
    out, dec, pos = {}, json.JSONDecoder(), 0
    pat = re.compile(r"var\s+(\w+)\s*=\s*")
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        try:
            value, end = dec.raw_decode(text, m.end())
        except ValueError:
            pos = m.end()
            continue
        out[m.group(1)] = value
        pos = end
    return out


def first_var(text: str):
    v = parse_js_vars(text)
    if not v:
        raise ValueError("no parseable var assignment")
    return next(iter(v.values()))


def entry_of(obj):
    """The English data entry, defensively. Across 47 shards the shape is not
    uniform: `data` is occasionally not a list, and fields that are dicts for
    most objects are sometimes bare strings. One unguarded access aborts a
    128,000-object pass, so coerce and let the caller skip."""
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def stereotypes(entry) -> list:
    cats = entry.get("categories")
    if not isinstance(cats, list):
        return []
    for cat in cats:
        if not isinstance(cat, dict) or cat.get("type") != "table":
            continue
        content = cat.get("content")
        if not isinstance(content, dict):
            continue
        st = content.get("Stereotypes")
        if not isinstance(st, dict):
            continue
        inner = st.get("stereotype")
        if not isinstance(inner, dict):
            continue
        val = inner.get("value")
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            return [v for v in val if isinstance(v, str)]
    return []


def category_of(entry) -> str:
    """First stereotype, falling back to UML type. A BLANK stereotype counts as
    absent -- taking it literally yields an empty category and the object then
    matches no allowlist and vanishes."""
    for s in stereotypes(entry):
        if s and s.strip():
            return s.strip()
    t = entry.get("type")
    return t.strip() if isinstance(t, str) else ""


def name_of(entry) -> str:
    n = entry.get("name")
    if not isinstance(n, str):
        return ""
    return re.sub(r"\s+", " ", n).strip()   # names can contain newlines


def fetch_objects(verbose=True) -> tuple:
    mapping = first_var(get(f"{BASE}/data/all_objects_data_mapping.js"))
    shards = sorted({int(v) for v in mapping.values()})
    if verbose:
        print(f"  mapping lists {len(shards)} shards "
              f"over {len(mapping)} object ids", flush=True)

    objects, malformed = {}, 0
    for i, n in enumerate(shards):
        time.sleep(PACE if i else 0)
        try:
            data = first_var(get(f"{BASE}/data/all_objects_data_{n}.js"))
        except Exception as e:
            print(f"  shard {n}: {type(e).__name__} -- ABORTING, a partial "
                  f"pass would produce plausible wrong totals", flush=True)
            raise
        if not isinstance(data, dict):
            malformed += 1
            continue
        for oid, obj in data.items():
            if oid not in objects:          # first occurrence wins
                objects[oid] = obj
        if verbose:
            print(f"  shard {n:<3} {len(data):>6} objects  "
                  f"running total {len(objects):>7}", flush=True)
    return objects, shards, malformed


def main() -> int:
    ap = argparse.ArgumentParser(description="Census the BIAN landscape.")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the counts as JSON")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-shard progress")
    args = ap.parse_args()
    verbose = not args.quiet

    print("=" * 70)
    print("  BIAN Service Landscape census")
    print("=" * 70)
    print(f"\n  base {BASE}\n")

    objects, shards, malformed = fetch_objects(verbose)

    by_category = Counter()
    skipped = 0
    sd_ids, sd_names_by_spelling = set(), defaultdict(list)
    names_by_norm = defaultdict(list)

    allow_norm = {normalise(a) for a in ALLOWLIST}
    sd_norm = {normalise(s) for s in SERVICE_DOMAIN_SPELLINGS}

    for oid, obj in objects.items():
        entry = entry_of(obj)
        if entry is None:
            skipped += 1
            continue
        cat = category_of(entry)
        by_category[cat] += 1
        if normalise(cat) in sd_norm:
            sd_ids.add(oid)
            sd_names_by_spelling[cat].append(name_of(entry))
            names_by_norm[normalise(name_of(entry))].append(oid)

    allowed = sum(n for c, n in by_category.items()
                  if normalise(c) in allow_norm)

    # ---- relations -------------------------------------------------
    time.sleep(PACE)
    relations = first_var(get(f"{BASE}/data/all_objects_relations.js"))
    eq_edges, eq_sources = 0, set()
    if isinstance(relations, dict):
        for oid, rels in relations.items():
            if oid not in sd_ids or not isinstance(rels, list):
                continue
            for rel in rels:
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
    sd_objects = len(sd_ids)
    sd_distinct = len(names_by_norm)

    print()
    print("=" * 70)
    print("  RESULTS -- every figure names what it counts")
    print("=" * 70)
    print(f"""
  Shards fetched                                  {len(shards)}
  Unique objects (union of all shards)            {len(objects)}
  Objects skipped, unreadable payload             {skipped}
  Malformed shards                                {malformed}

  Objects whose CATEGORY is in the allowlist      {allowed}
      matched normalised, over {len(objects)} objects

  SERVICE DOMAINS""")
    for sp in SERVICE_DOMAIN_SPELLINGS:
        print(f"      objects spelled {sp!r:<18} "
              f"{len(sd_names_by_spelling.get(sp, []))}")
    other = {c: n for c, n in by_category.items()
             if normalise(c) in sd_norm and c not in SERVICE_DOMAIN_SPELLINGS}
    for c, n in sorted(other.items()):
        print(f"      objects spelled {c!r:<18} {n}   <-- NEW SPELLING")
    print(f"""      ------------------------------------------
      OBJECTS, all spellings                    {sd_objects}
      DISTINCT NAMES, normalised                {sd_distinct}
      names shared by more than one object      {len(dupes)}

  'is equal to' FROM a service domain
      edges             {eq_edges:<6} over {sd_objects} service domain objects
      distinct sources  {len(eq_sources):<6} of {sd_objects} objects""")

    if len(eq_sources) == sd_distinct:
        print("\n  NOTE: distinct sources equals the DISTINCT NAME count.")
        print("  Check this is real and not a name-keyed collapse (bug 019).")

    print("\n  Largest categories:")
    for c, n in by_category.most_common(12):
        print(f"    {n:>7}  {c or '(empty)'}")

    if dupes:
        print(f"\n  Shared service domain names ({len(dupes)}), first 10:")
        for norm, ids in list(sorted(dupes.items()))[:10]:
            entry = entry_of(objects[ids[0]])
            print(f"    {name_of(entry)!r} -- {len(ids)} objects: "
                  f"{', '.join(sorted(ids))}")

    if args.json:
        payload = {
            "base": BASE,
            "shards": len(shards),
            "unique_objects": len(objects),
            "objects_skipped": skipped,
            "allowlist_objects": allowed,
            "service_domain_objects": sd_objects,
            "service_domain_distinct_names": sd_distinct,
            "service_domain_by_spelling": {
                c: n for c, n in by_category.items() if normalise(c) in sd_norm},
            "shared_names": len(dupes),
            "is_equal_to_edges": eq_edges,
            "is_equal_to_distinct_sources": len(eq_sources),
            "categories": dict(by_category),
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        print(f"\n  wrote {args.json}")

    print("\n  These are MEASUREMENTS. They belong in REFERENCE-DATA.md on")
    print("  Drive, not in a skill. Record them with their denominators.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
