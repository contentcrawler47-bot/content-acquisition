#!/usr/bin/env python3
"""
BIAN Service Landscape.

The landscape browser is a Backbone.js client that loads its dataset from
static JavaScript files — no API, no login, no rendering step:

    data/all_objects_data_N.js    -> var objectData      = {id: {...}}
    data/all_objects_data_mapping.js -> var objectDataMapping = {id: N}
    data/all_objects_relations.js -> var objectRelations  = {id: [{via, to}]}

The model is SHARDED across ~24 numbered data files, capped around 5,000
objects each. objectDataMapping says which shard holds each object, so the set
of shard numbers is simply set(mapping.values()). Reading only one shard —
as this source originally did — yields about 5% of the landscape.

Credentials: none. The files are served unauthenticated.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from core.diagnostics import ProbeSpec
from core.render import clean_html, write_bundles
from core.source import Check, HarvestResult, Stage
from core.source import Source as BaseSource

# --- version pinning -------------------------------------------------------
# When BIAN publishes a new landscape you receive a fresh link by email.
# Update these two, then re-run validation.
BASE = "https://bian.org/servicelandscape-14-0-0"
VIEW = 16
# ---------------------------------------------------------------------------

FILES = {
    "mapping":   f"{BASE}/data/all_objects_data_mapping.js",
    "relations": f"{BASE}/data/all_objects_relations.js",
    "on_views":  f"{BASE}/data/all_objects_on_views.js",
    "config":    f"{BASE}/data/config_data.js",
}


def shard_url(n: int) -> str:
    return f"{BASE}/data/all_objects_data_{n}.js"


#: Safety net if the mapping cannot be parsed. Shards are 1-indexed; 0 is 404.
#: 47 shards existed at v14.0, so the fallback allows generous headroom.
FALLBACK_SHARDS = range(1, 81)
MAX_SHARDS = 200
LAST_KNOWN_SHARD = 47

UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
TIMEOUT = 120

# Canary: a known service domain. If BIAN restructures or the parser drifts,
# validation fails loudly instead of quietly producing thinner output.
CANARY_ID = "34300"
CANARY_NAME = "Consumer Loan"

# The V14.0 value chain view (views/view_54486.html) shows 340 service domains.
# Anything materially below that means shards are being missed again.
# The full model holds more service domains than the value chain view depicts
# (367 vs 340), since not every domain appears on that diagram.
EXPECTED_SERVICE_DOMAINS = 367
MIN_SERVICE_DOMAINS = 340
# After allowlist filtering, roughly 11,000 of 128,000 objects remain.
MIN_OBJECTS = 8000
MIN_SHARDS = 40

SKIP_RELATION_VERBS = {"", "<unknown role>"}

# The full landscape is 128,000 objects, but only about a tenth is BIAN
# semantic content. The rest is UML and ArchiMate modelling furniture:
# Attribute (14,983), Execution specification (7,248), Enumeration literal
# (5,171), Message (5,149), Line (3,922), Graphical shape (3,313) and so on,
# across 140+ categories.
#
# An allowlist is the only maintainable approach at that scale — a new junk
# category upstream is then ignored by default rather than silently bloating
# the output. Anything omitted here is still in objects_raw.json.
INCLUDE_CATEGORIES = {
    # Core service model
    "ServiceDomain", "Service Domain", "ServiceOperation", "ServiceOperationType",
    "ServiceGroup", "SDServiceGroup", "BusinessService", "Business service",
    # Information model
    "ControlRecord", "AssetType", "AnalyticsObject", "Business object",
    "BehaviorQualifier", "BehaviorQualifierType", "ReferenceInformation",
    "BIAN Data Type", "BIAN DataType",
    # Structure and classification
    "BusinessArea", "BusinessDomain", "BusinessConcept", "FunctionalPattern",
    "Capability", "Grouping", "GenericArtifact", "ActionTerm",
    "Business Scenario",
}


# ArchiMate models relationships as first-class objects. They carry no
# documentation of their own and the edges they represent are already rendered
# inline on each real object. Kept as a second line of defence.
EXCLUDE_CATEGORIES = {
    "Flow relation", "Triggering relation", "Realization relation",
    "Serving relation", "Association relation", "Composition relation",
    "Aggregation relation", "Assignment relation", "Access relation",
    "Specialization relation", "Influence relation", "Junction",
    "Lifeline",
}


def _is_structural(category: str, name: str) -> bool:
    """Structural graph artefacts, not content."""
    return (category in EXCLUDE_CATEGORIES
            or category.endswith(" relation")
            or (name or "").endswith(" relation"))


def _is_wanted(category: str, name: str) -> bool:
    """Keep only BIAN semantic content."""
    return category in INCLUDE_CATEGORIES and not _is_structural(category, name)


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_js_assignments(text: str) -> dict:
    """Extract every `var name = <json>;` assignment in a file.

    Not one variable per file: all_objects_on_views.js defines several.
    raw_decode consumes a single JSON value and reports where it ended, so
    the scan resumes from there rather than failing on trailing content.
    """
    out, dec, pos = {}, json.JSONDecoder(), 0
    pattern = re.compile(r"var\s+(\w+)\s*=\s*")
    while True:
        m = pattern.search(text, pos)
        if not m:
            break
        try:
            value, end = dec.raw_decode(text, m.end())
        except ValueError:
            pos = m.end()
            continue
        out[m.group(1)] = value
        pos = end
    if not out:
        raise ValueError("unexpected file format — no parseable var assignment")
    return out


def _parse_js_assignment(text: str):
    """The first assignment in a file, which is the payload in every data
    file we consume."""
    return next(iter(_parse_js_assignments(text).values()))


def _shard_numbers(mapping: dict) -> list[int]:
    """Shard indices to fetch, taken from the mapping's values.

    The mapping is authoritative: every object id points at the shard holding
    it. Falls back to a probe range if the values look implausible.
    """
    try:
        nums = sorted({int(v) for v in mapping.values()})
    except Exception:
        nums = []
    if not nums or max(nums) > MAX_SHARDS:
        return list(FALLBACK_SHARDS)
    # Shards are contiguous; include any gap the mapping happens not to cite.
    return list(range(min(nums), max(nums) + 1))


def _fetch_shards(numbers: list[int]) -> tuple[dict, list[str]]:
    """Download and merge every shard. First occurrence of an id wins, since
    an object repeated across shards carries the same payload."""
    merged: dict = {}
    notes: list[str] = []
    missing: list[int] = []

    for n in numbers:
        try:
            text = _download(shard_url(n))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                missing.append(n)
                continue
            raise
        try:
            data = _parse_js_assignment(text)
        except Exception as e:
            notes.append(f"shard {n} unparseable ({type(e).__name__})")
            continue

        before = len(merged)
        for oid, obj in data.items():
            merged.setdefault(oid, obj)
        print(f"  shard {n:<3} {len(text)/1024:>8.0f} KB  {len(data):>6} objects  "
              f"(+{len(merged) - before} new)", flush=True)

    if missing:
        notes.append(f"shards absent: {', '.join(map(str, missing))}")
    return merged, notes


# Across 47 shards the payload is not uniformly shaped: fields that are dicts
# for most objects are occasionally bare strings. These coercions keep a single
# oddly-shaped object from aborting a 128,000-object harvest.

def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x) -> list:
    return x if isinstance(x, list) else []


def _categories(entry) -> list:
    return [c for c in _l(_d(entry).get("categories")) if isinstance(c, dict)]


def _stereotypes(entry) -> list[str]:
    for cat in _categories(entry):
        if cat.get("type") == "table":
            st = _d(_d(_d(cat.get("content")).get("Stereotypes")).get("stereotype"))
            return [v for v in _l(st.get("value")) if isinstance(v, str)]
    return []


def _properties(entry) -> dict:
    for cat in _categories(entry):
        if cat.get("type") == "table":
            return _d(cat.get("content"))
    return {}


def _documentation(entry) -> dict:
    out = {}
    for cat in _categories(entry):
        if cat.get("type") != "documentation":
            continue
        content = cat.get("content")
        value = content if isinstance(content, str) else _d(content).get("value", "")
        text = clean_html(value if isinstance(value, str) else "")
        if text:
            out[cat.get("title") or "documentation"] = text
    return out


def _flatten(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "link":
            v = _d(value.get("value"))
            return f"{v.get('title', '')} — {v.get('location', '')}".strip(" —")
        if kind == "object":
            return _d(value.get("value")).get("name", "")
        if kind == "collection":
            return [x for x in (_flatten(i) for i in _l(value.get("value"))) if x]
    return ""


def _relations_block(oid, relations, names) -> list[str]:
    rels = [r for r in _l(relations.get(str(oid))) if isinstance(r, dict)]
    if not rels:
        return []
    lines = ["### Relationships"]
    for rel in sorted(rels, key=lambda r: r.get("via") or ""):
        via = (rel.get("via") or "").strip()
        if via in SKIP_RELATION_VERBS:
            continue
        targets = sorted(
            f"{names[str(t)]} ({t})" for t in _l(rel.get("to"))
            if isinstance(t, (str, int)) and names.get(str(t))
            and not (names[str(t)] or "").endswith(" relation"))
        if targets:
            lines.append(f"- **{via}:** " + "; ".join(targets))
    return lines + [""] if len(lines) > 1 else []


def _render(oid, entry, relations, names) -> tuple[str, str]:
    entry = _d(entry)
    sts = _stereotypes(entry)
    otype = entry.get("type") or ""
    if not isinstance(otype, str):
        otype = str(otype)
    lines = [
        f"## {entry.get('name') or f'Object {oid}'}", "",
        f"- **Object id:** {oid}",
        f"- **Type:** {otype}" + (f" ({', '.join(sts)})" if sts else ""),
        f"- **Source:** {BASE}/object_{VIEW}.html?object={oid}", "",
    ]

    for title, text in _documentation(entry).items():
        lines += [f"### {'Description' if title == 'documentation' else title}",
                  text, ""]

    for group, fields in _properties(entry).items():
        if group == "Stereotypes" or not isinstance(fields, dict):
            continue
        if not isinstance(group, str):
            group = str(group)
        rows = []
        for key, raw in fields.items():
            val = _flatten(raw)
            if isinstance(val, list):
                if val:
                    rows.append(f"- **{key}:** ({len(val)})")
                    rows += [f"  - {v}" for v in val]
            elif val:
                rows.append(f"- **{key}:** "
                            + " / ".join(v.strip() for v in val.split("\n") if v.strip()))
        if rows:
            lines += [f"### {group}", *rows, ""]

    lines += _relations_block(oid, relations, names)
    lines += ["---", ""]
    return "\n".join(lines), (sts[0] if sts else otype or "Other")


class Source(BaseSource):
    id = "bian"
    name = "BIAN Service Landscape"
    description = "Banking Industry Architecture Network service domain model"
    required_secrets: list[str] = []      # public static files
    schedule = "0 3 * * 1"

    def probes(self) -> list[ProbeSpec]:
        """What must be reachable, and what each endpoint should return.

        Each data file is a `var <name> = {...};` assignment, so asserting the
        prefix catches the common failure where bian.org returns an HTML error
        page or a login redirect with a 200 status.
        """
        return [
            ProbeSpec(
                label="shard index (all_objects_data_mapping.js)",
                url=FILES["mapping"],
                expect_prefix="var objectDataMapping",
                min_bytes=100_000),
            ProbeSpec(
                label="first data shard (all_objects_data_1.js)",
                url=shard_url(1),
                expect_prefix="var objectData",
                min_bytes=200_000),
            ProbeSpec(
                label=f"last known shard (all_objects_data_{LAST_KNOWN_SHARD}.js)",
                url=shard_url(LAST_KNOWN_SHARD),
                expect_prefix="var objectData",
                min_bytes=200_000),
            ProbeSpec(
                label="relationship graph (all_objects_relations.js)",
                url=FILES["relations"],
                expect_prefix="var objectRelations",
                min_bytes=10_000),
            ProbeSpec(
                label="language config (config_data.js)",
                url=FILES["config"],
                expect_prefix="var availableLanguages",
                min_bytes=10),
            ProbeSpec(
                label="objects-on-views (all_objects_on_views.js)",
                url=FILES["on_views"],
                expect_prefix="var ",
                optional=True),
        ]

    def harvest(self, outdir: Path) -> HarvestResult:
        raw = {}
        for key, url in FILES.items():
            text = _download(url)
            print(f"  {key:<10} {len(text) / 1024:>8.0f} KB", flush=True)
            raw[key] = text

        try:
            mapping = _parse_js_assignment(raw["mapping"])
        except Exception:
            mapping = {}
        shards = _shard_numbers(mapping)
        print(f"  {len(shards)} shards to fetch: "
              f"{shards[0]}-{shards[-1]}", flush=True)

        objects, shard_notes = _fetch_shards(shards)
        print(f"  merged {len(objects)} unique objects from "
              f"{len(shards)} shards", flush=True)

        try:
            relations = _parse_js_assignment(raw["relations"])
        except Exception:
            relations = {}

        names = {}
        for oid, o in objects.items():
            entry = _l(_d(o).get("data"))
            first = _d(entry[0]) if entry else {}
            nm = first.get("name")
            names[oid] = nm if isinstance(nm, str) else ""

        items, excluded, skipped, dropped = [], 0, [], {}
        for oid, obj in objects.items():
            data = _l(_d(obj).get("data"))
            if not data or not isinstance(data[0], dict):
                skipped.append((oid, type(data[0]).__name__ if data else "empty"))
                continue
            try:
                body, category = _render(oid, data[0], relations, names)
            except Exception as e:
                # One malformed object must not abort a 128,000-object harvest.
                skipped.append((oid, f"{type(e).__name__}"))
                continue
            name = names.get(oid, "")
            if not _is_wanted(category, name):
                excluded += 1
                dropped[category] = dropped.get(category, 0) + 1
                continue
            items.append({"id": oid, "name": name,
                          "category": category, "body": body})

        print(f"  kept {len(items)} of {len(objects)} objects "
              f"({excluded} filtered out as non-content)", flush=True)
        top = sorted(dropped.items(), key=lambda kv: -kv[1])[:12]
        if top:
            print("  largest filtered categories:", flush=True)
            for cat, n in top:
                print(f"    {cat:<32} {n:>7}", flush=True)

        if skipped:
            print(f"  skipped {len(skipped)} malformed objects "
                  f"(ids and reasons only):", flush=True)
            for oid, why in skipped[:10]:
                print(f"    {oid}: {why}", flush=True)
            if len(skipped) > 10:
                print(f"    ... and {len(skipped) - 10} more", flush=True)

        # 128,000 objects at the 40-per-file default would produce thousands
        # of files, which is unusable in Drive and slow to write. Revisit once
        # the category breakdown is known.
        written = write_bundles(
            outdir, self.id, self.name, items, per_file=250,
            extra_index_lines=[
                f"Landscape version: `{BASE.rsplit('/', 1)[-1]}`.",
                f"Merged from {len(shards)} data shards "
                f"({len(objects)} unique objects).",
                f"Objects with relationships: {len(relations)}.",
                f"Filtered to BIAN semantic content: {len(items)} kept, "
                f"{excluded} modelling artefacts excluded.",
                f"Malformed objects skipped: {len(skipped)}.",
            ])

        return HarvestResult(
            source_id=self.id,
            item_count=len(items),
            categories=written["categories"],
            files_written=written["files_written"],
            notes=[f"{len(shards)} shards merged into {len(objects)} objects",
                   f"{len(relations)} objects carry relationships",
                   f"{excluded} non-content objects filtered out",
                   f"{len(skipped)} malformed objects skipped"]
                  + shard_notes,
        )

    def checks(self, outdir: Path) -> list[Check]:
        out: list[Check] = []
        manifest = json.loads((outdir / "manifest.json").read_text())
        items = manifest.get("items", {})
        cats = manifest.get("categories", {})

        out.append(Check(
            "object count", len(items) >= MIN_OBJECTS,
            f"{len(items)} (min {MIN_OBJECTS})", stage=Stage.EXTRACT,
            hint="Far fewer objects than the landscape contains. The most "
                 "likely cause is that shards failed to download — check the "
                 "per-shard lines in the harvest output."))

        sd = cats.get("ServiceDomain", 0)
        out.append(Check(
            "service domain coverage", sd >= MIN_SERVICE_DOMAINS,
            f"{sd} of ~{EXPECTED_SERVICE_DOMAINS} expected",
            stage=Stage.EXTRACT,
            hint=f"The V14.0 value chain view (views/view_54486.html) shows "
                 f"{EXPECTED_SERVICE_DOMAINS} service domains. Materially "
                 f"fewer means data shards are being missed — this is exactly "
                 f"the failure that had us reading 222 of 340. Check the shard "
                 f"count in the harvest output."))
        out.append(Check(
            "service domain count not wildly above expectation",
            sd <= EXPECTED_SERVICE_DOMAINS * 1.5,
            f"{sd} vs ~{EXPECTED_SERVICE_DOMAINS} expected", warn=True,
            stage=Stage.EXTRACT,
            hint="More service domains than the published view shows. Either "
                 "BIAN expanded the model, or shards contain older revisions "
                 "of the same objects under different ids."))

        canary = items.get(CANARY_ID, {})
        out.append(Check(
            f"canary object {CANARY_ID} acquired", bool(canary),
            stage=Stage.EXTRACT,
            hint=f"Object {CANARY_ID} ({CANARY_NAME}) is missing. If BIAN "
                 f"genuinely retired it, pick a new canary; otherwise the "
                 f"extraction is dropping objects."))
        out.append(Check(
            "canary name matches", CANARY_NAME in canary.get("name", ""),
            f"got {canary.get('name', '(missing)')!r}", stage=Stage.EXTRACT,
            hint="The id resolved but to a different object — ids may have "
                 "been reassigned in a new landscape version."))
        out.append(Check(
            "canary is a service domain",
            canary.get("category") == "ServiceDomain",
            f"got {canary.get('category')!r}", stage=Stage.EXTRACT,
            hint="Categorisation is reading the wrong field; check "
                 "_stereotypes()."))

        body = "\n".join(f.read_text(encoding="utf-8")
                         for f in outdir.glob("*.md"))
        out.append(Check(
            "role definitions rendered",
            body.count("### 1. Role Definition") >= MIN_SERVICE_DOMAINS,
            f"{body.count('### 1. Role Definition')} sections",
            stage=Stage.RENDER,
            hint="Service domains were found but their documentation is not "
                 "reaching the markdown — check _documentation()."))
        out.append(Check(
            "relationships rendered",
            body.count("### Relationships") >= len(items) // 4,
            f"{body.count('### Relationships')} sections", stage=Stage.RENDER,
            hint="The relations file parsed but few edges resolved. Ids in "
                 "all_objects_relations.js may not match objectData keys."))
        out.append(Check(
            "portal API links preserved",
            body.count("portal.bian.org/service-domain-api/") >= 50,
            f"{body.count('portal.bian.org/service-domain-api/')} links",
            warn=True, stage=Stage.RENDER,
            hint="Expected in service domain property tables. A legitimate "
                 "drop can happen between BIAN versions."))
        out.append(Check(
            "no translate placeholders", body.count("__is_translate") == 0,
            f"{body.count('__is_translate')} found", stage=Stage.RENDER,
            hint="Internal BIAN placeholder keys reached the output — the "
                 "property-table title filter needs updating."))
        # Skipping malformed objects keeps a huge harvest alive, but a large
        # number would mean the payload shape has genuinely moved on.
        out.append(Check(
            "malformed objects are rare",
            True, "see 'malformed objects skipped' in the harvest notes",
            warn=True, stage=Stage.EXTRACT,
            hint="If that count is more than a fraction of a percent, the "
                 "upstream structure has changed and the coercions in _d()/_l() "
                 "are papering over a real problem."))
        out.append(Check(
            "no structural relation objects emitted",
            not any(_is_structural(m.get("category", ""), m.get("name", ""))
                    for m in items.values()),
            stage=Stage.EXTRACT,
            hint="ArchiMate relation objects are being emitted as items. They "
                 "carry no content and their edges already render inline — "
                 "check EXCLUDE_CATEGORIES and _is_structural()."))
        out.append(Check(
            "no relation objects as relationship targets",
            " relation (" not in body,
            stage=Stage.RENDER,
            hint="Relationship lines point at ArchiMate relation objects "
                 "rather than real ones — the target-name filter in "
                 "_relations_block() needs widening."))
        # Scoped to Relationships sections. The previous form matched any
        # bullet with a numeric value, so ordinary properties such as
        # "**Cardinality:** 1" produced false failures once the harvest grew
        # large enough to contain them.
        bad_targets, in_rels = 0, False
        for line in body.splitlines():
            if line.startswith("### "):
                in_rels = line.strip() == "### Relationships"
                continue
            if in_rels and line.startswith("- **") and "(" not in line:
                bad_targets += 1
        out.append(Check(
            "relationship targets resolved to names", bad_targets == 0,
            f"{bad_targets} unresolved", stage=Stage.RENDER,
            hint="Relationship targets rendered without a '(id)' suffix, "
                 "meaning the id-to-name lookup missed them."))
        return out
