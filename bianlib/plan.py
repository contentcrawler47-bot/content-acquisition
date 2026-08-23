#!/usr/bin/env python3
"""
Deciding what to fetch, splitting it into chunks, and proving each chunk landed.

Two problems are solved here.

**Which view pages are worth fetching.** The landscape holds ~2,285 views, of
which only sequence and class diagrams convert to PlantUML; the rest are
ArchiMate total views and capability maps using a different concept set
entirely. Fetching all 2,285 to discover that would nearly double the demand on
bian.org for no output. The type is derivable without fetching anything: invert
objectsOnViews to get each view's members, then look up each member's category
in the model. A view carrying Messages and Execution specifications is a
sequence diagram; one carrying Classes and Attributes is a class diagram. The
classifier's output is asserted against known counts before a single page is
requested, so a misfire stops the run rather than silently harvesting the wrong
half of the landscape.

**Splitting the work so a failure is cheap.** The plan is computed once, written
down, and passed to every chunk as an artifact. Chunks do not re-derive it —
two jobs deriving "the same" partition from a source that changed underneath
them is a class of bug not worth having. Each chunk records the plan's hash,
and the assembly step refuses to combine chunks whose hashes disagree.

Chunks are contiguous blocks of the id-ordered view list, balanced by member
count rather than by view count, because a 964-member class diagram costs
considerably more than a 12-member one.
"""

from __future__ import annotations

import hashlib
import json
import time

from bianlib import landscape as L

#: Kinds the pipeline can convert. Everything else is planned as "other" and
#: never fetched.
CONVERTIBLE = ("sequence", "class")

#: Verified against v14.0. A classifier that lands far outside these has
#: misfired, and it is better to stop than to harvest a fraction of the model.
#: See content/_project-context/REFERENCE-DATA.md on Drive.
EXPECTED = {"sequence": 429, "class": 802}
TOLERANCE = 0.25          # ±25% before the plan is rejected

#: Per-chunk acceptance. A handful of view pages can legitimately fail — a
#: transient 5xx, a page that is not a diagram at all — but a chunk that has
#: lost a tenth of its pages has not "succeeded" in any useful sense.
MAX_FAILED_FRACTION = 0.02
MAX_FAILED_ABSOLUTE = 5
#: ...and the absolute allowance must not swamp a small chunk: five failures
#: out of seven views is not a chunk that succeeded.
MAX_FAILED_SHARE = 0.25


def failure_limit(planned: int) -> int:
    return min(max(MAX_FAILED_ABSOLUTE, int(planned * MAX_FAILED_FRACTION)),
               max(1, int(planned * MAX_FAILED_SHARE)))


#: The diagram objects' own categories, which is what the model calls them.
#: A view id appears to BE the id of the diagram object it renders, so this is
#: a direct reading rather than an inference from what the view contains.
MODEL_KIND = {
    "Sequence diagram": "sequence",
    "Class diagram": "class",
    "Total view": "archimate",
    "Capability map view": "archimate",
    "Business process diagram": "archimate",
}


def _by_members(landscape: L.Landscape, oids: list[str]) -> str:
    """Kind inferred from what the view contains.

    Scoring rather than first-match: a sequence diagram contains a handful of
    Class objects too, and vice versa. Used only where the model does not name
    the diagram itself — it over-counts class views, because several ArchiMate
    view types are full of objects this cannot tell from UML classes.
    """
    seq = cls = arch = 0
    for oid in oids:
        cat = landscape.categories.get(oid, "")
        if cat in L.SEQUENCE_MEMBER_CATEGORIES:
            seq += 1
        elif cat in L.CLASS_MEMBER_CATEGORIES:
            cls += 1
        elif cat in L.ARCHIMATE_MEMBER_CATEGORIES:
            arch += 1
    best = max((seq, "sequence"), (cls, "class"), (arch, "archimate"))
    return best[1] if best[0] else "unknown"


def classify(landscape: L.Landscape) -> tuple[dict, dict, dict]:
    """{viewId: kind}, the member counts, and how each kind was decided.

    Two independent readings, in order of trust:

    1. **The model names the diagram.** Every diagram is itself an object, and
       its category says what it is — "Class diagram", "Sequence diagram",
       "Total view", "Capability map view". Where the view id resolves to such
       an object, that is the answer and there is nothing to infer.
    2. **What the view contains.** Only for views the model does not name.

    The first reading was added after the second put 1,043 views in the class
    bucket against a known 802 — it cannot separate a UML class diagram from
    the several ArchiMate view types built out of similar-looking objects, and
    240 extra pages is 240 requests of somebody else's server for nothing.
    """
    members = landscape.views_to_members()
    all_views = set(members) | {str(v) for v in landscape.insite_views}

    kinds, sizes, how = {}, {}, {}
    for vid in all_views:
        oids = members.get(vid, [])
        sizes[vid] = len(oids)
        from_model = MODEL_KIND.get(landscape.categories.get(vid, ""), "")
        from_members = _by_members(landscape, oids)
        kinds[vid] = from_model or from_members
        how[vid] = (landscape.categories.get(vid, ""), from_model, from_members)
    return kinds, sizes, how


def evidence(how: dict) -> str:
    """Cross-tabulate the two readings, so a disagreement is visible.

    Printed every run. If the model ever stops naming its diagrams, this is
    what shows it — the fallback column would carry the whole classification.
    """
    named = {k: v for k, v in how.items() if v[1]}
    rows: dict[tuple, int] = {}
    for _vid, (category, from_model, from_members) in how.items():
        key = (category or "(not an object in the model)",
               from_model or "-", from_members)
        rows[key] = rows.get(key, 0) + 1

    out = [f"  views named by the model : {len(named)} of {len(how)}",
           "",
           f"  {'model category':<32}{'kind':<12}"
           f"{'fallback would say':<20}count",
           f"  {'-' * 70}"]
    for (category, from_model, from_members), n in sorted(
            rows.items(), key=lambda kv: -kv[1]):
        out.append(f"  {category:<32}{from_model:<12}{from_members:<20}{n}")
    return "\n".join(out)


def counts(kinds: dict) -> dict:
    out: dict[str, int] = {}
    for kind in kinds.values():
        out[kind] = out.get(kind, 0) + 1
    return out


def plausible(kind_counts: dict, expected: dict = None,
              tolerance: float = TOLERANCE) -> list[str]:
    """Complaints about a classification, empty if it looks right."""
    expected = expected or EXPECTED
    problems = []
    for kind, want in expected.items():
        got = kind_counts.get(kind, 0)
        if not (want * (1 - tolerance) <= got <= want * (1 + tolerance)):
            problems.append(
                f"{kind} views: {got}, expected about {want} "
                f"(±{int(tolerance * 100)}%)")
    return problems


def partition(entries: list[dict], chunk_count: int) -> list[list[dict]]:
    """Contiguous, deterministic, balanced by weight.

    The target is recomputed from what is left rather than fixed up front, so
    a few very large diagrams early on cannot push everything else into the
    final chunk.
    """
    chunk_count = max(1, int(chunk_count))
    chunks: list[list[dict]] = []
    remaining = list(entries)
    weight_left = sum(e["members"] for e in remaining) or len(remaining)
    for slot in range(chunk_count, 0, -1):
        if slot == 1:
            chunks.append(remaining)
            break
        target = weight_left / slot
        taken, weight = [], 0
        # Always leave at least one entry per remaining slot.
        while remaining and len(remaining) > slot - 1 and (
                weight < target or not taken):
            item = remaining.pop(0)
            taken.append(item)
            weight += item["members"] or 1
        chunks.append(taken)
        weight_left -= weight
    while len(chunks) < chunk_count:
        chunks.append([])
    return chunks


def plan_sha(view_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(view_ids).encode("utf-8")).hexdigest()[:16]


def build(landscape: L.Landscape, source_id: str, chunk_count: int,
          kinds_wanted=CONVERTIBLE, limit: int = 0,
          expected: dict = None) -> dict:
    """The work plan: which views, in what order, split how."""
    kinds, sizes, how = classify(landscape)
    kind_counts = counts(kinds)

    wanted = sorted(
        (vid for vid, k in kinds.items() if k in kinds_wanted),
        key=lambda v: (int(v) if v.isdigit() else 0, v))
    if limit:
        wanted = wanted[:limit]

    entries = [{"id": vid, "kind": kinds[vid], "members": sizes.get(vid, 0),
                "name": landscape.view_name(vid)} for vid in wanted]

    chunks = partition(entries, chunk_count)
    return {
        "source": source_id,
        "base": landscape.base,
        "landscape": landscape.base.rsplit("/", 1)[-1],
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "chunk_count": len(chunks),
        "plan_sha": plan_sha(wanted),
        "view_count": len(wanted),
        "classification": kind_counts,
        "evidence": evidence(how),
        "problems": plausible(kind_counts, expected),
        "chunks": [
            {"index": i + 1, "views": c,
             "members": sum(e["members"] for e in c)}
            for i, c in enumerate(chunks)
        ],
    }


def describe(plan: dict) -> str:
    lines = [
        plan.get("evidence", ""), "",
        f"  landscape      {plan['landscape']}",
        f"  views planned  {plan['view_count']} of "
        f"{sum(plan['classification'].values())} known",
        f"  plan sha       {plan['plan_sha']}",
        "  classification " + ", ".join(
            f"{k} {v}" for k, v in sorted(plan["classification"].items(),
                                          key=lambda kv: -kv[1])),
        f"  chunks         {plan['chunk_count']}",
    ]
    for c in plan["chunks"]:
        ids = [e["id"] for e in c["views"]]
        span = f"{ids[0]}-{ids[-1]}" if ids else "(empty)"
        lines.append(f"    chunk {c['index']:>2}  {len(ids):>4} views  "
                     f"{c['members']:>6} members  {span}")
    return "\n".join(lines)


def chunk_of(plan: dict, index: int) -> dict:
    for c in plan["chunks"]:
        if c["index"] == index:
            return c
    raise KeyError(f"chunk {index} is not in this plan "
                   f"(it has {plan['chunk_count']})")


# --- verification ----------------------------------------------------------

def verify_chunk(result: dict, chunk: dict,
                 plan_sha: str = "") -> list[tuple[bool, str, str]]:
    """Did this chunk actually harvest what it was asked to?

    Returns (ok, name, detail) triples. Run before the next chunk starts, so a
    chunk that half-worked stops the run instead of contributing a quiet hole
    in the middle of the landscape.
    """
    planned = len(chunk["views"])
    attempted = result.get("attempted", 0)
    converted = result.get("converted", 0)
    failed = len(result.get("failed", []))
    skipped = len(result.get("skipped", []))

    checks = [
        (attempted == planned, "every planned view was attempted",
         f"{attempted} of {planned}"),
        (converted + failed + skipped == attempted,
         "every attempted view is accounted for",
         f"{converted} converted + {failed} failed + {skipped} skipped "
         f"= {converted + failed + skipped}, attempted {attempted}"),
        (failed <= failure_limit(planned), "failures within tolerance",
         f"{failed} failed (limit {failure_limit(planned)})"),
        (converted > 0 or planned == 0, "the chunk produced diagrams",
         f"{converted} diagrams"),
        (not plan_sha or result.get("plan_sha") == plan_sha,
         "chunk was harvested against this plan",
         f"{result.get('plan_sha')} vs {plan_sha}"),
    ]
    # Attribute ownership is the class-diagram quality metric: 147 of 147 were
    # assignable in the sample. A few strays at scale are tolerable; a lot
    # means the containment test has stopped working.
    attrs = result.get("attributes", 0)
    unassigned = result.get("unassigned_attrs", 0)
    checks.append(
        (unassigned <= max(5, attrs * 0.01),
         "class attributes resolved to an owning class",
         f"{unassigned} unassigned of {attrs}"))
    return checks


def verify_run(plan: dict, results: list[dict]) -> list[tuple[bool, str, str]]:
    """Are all the chunks present, from the same plan, and complete?"""
    seen = {r.get("chunk") for r in results}
    expected = {c["index"] for c in plan["chunks"]}
    shas = {r.get("plan_sha") for r in results}
    converted = sum(r.get("converted", 0) for r in results)
    failed = sum(len(r.get("failed", [])) for r in results)
    planned = plan["view_count"]

    return [
        (seen == expected, "every chunk reported",
         f"have {sorted(seen)}, expected {sorted(expected)}"),
        (len(shas) <= 1 and (not shas or plan["plan_sha"] in shas),
         "all chunks harvested against the same plan",
         f"{sorted(s for s in shas if s)} vs plan {plan['plan_sha']}"),
        (converted >= planned * 0.95, "the landscape is substantially complete",
         f"{converted} diagrams from {planned} planned views"),
        (failed <= max(MAX_FAILED_ABSOLUTE * 2,
                       int(planned * MAX_FAILED_FRACTION)),
         "total failures within tolerance", f"{failed} failed"),
    ]


def report(title: str, checks: list[tuple[bool, str, str]]) -> bool:
    print(f"\n  {title}", flush=True)
    ok = True
    for passed, name, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"    [{mark}] {name:<48} {detail}", flush=True)
        ok = ok and passed
    return ok


def load(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
