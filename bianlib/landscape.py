#!/usr/bin/env python3
"""
The BIAN Service Landscape data model, independent of any one version.

The landscape browser is a Backbone.js client that loads its dataset from
static JavaScript files — no API, no login, no rendering step:

    data/all_objects_data_mapping.js -> var objectDataMapping = {id: shard}
    data/all_objects_data_N.js       -> var objectData        = {id: {...}}
    data/all_objects_relations.js    -> var objectRelations   = {id: [{via,to}]}
    data/all_objects_on_views.js     -> var objectsOnViews    = {id: [viewIds]}
                                        var insiteViews       = {viewId: {...}}

The model is SHARDED across numbered data files capped around 5,000 objects
each. objectDataMapping says which shard holds each object, so the set of shard
numbers is simply set(mapping.values()). Reading only one shard — as this
source originally did — yields about 5% of the landscape.

Everything here is version-agnostic: the base URL is passed in, so v14 and v13
use identical code and differ only in their pinned URL and thresholds.

Credentials: none. The files are served unauthenticated.
"""

from __future__ import annotations

import json
import re

from core.render import clean_html

#: Safety net if the mapping cannot be parsed. Shards are 1-indexed; 0 is 404.
#: 47 shards existed at v14.0, so the fallback allows generous headroom.
FALLBACK_SHARDS = range(1, 81)
MAX_SHARDS = 200

SKIP_RELATION_VERBS = {"", "<unknown role>"}

# The full landscape is 128,000 objects, but only about a tenth is BIAN
# semantic content. The rest is UML and ArchiMate modelling furniture:
# Attribute (14,983), Execution specification (7,248), Enumeration literal
# (5,171), Message (5,149), Line (3,922), Graphical shape (3,313) and so on,
# across 140+ categories.
#
# An allowlist is the only maintainable approach at that scale — a new junk
# category upstream is then ignored by default rather than silently bloating
# the output.
#
# MEASURED, not argued. Membership is decided against a stored extract, which
# is what the two-stage split bought: `run.py render --add-category X` reports
# what X would add without a single request to bian.org. Two results are
# recorded here because a later reader will otherwise re-open them.
#
#   `Work package` (added)     355 objects, +3.1% on the bundle. 59.4% carry
#                              real documentation at a median of 500 chars —
#                              denser prose than every other kept category
#                              except ServiceDomain.
#   `Business function` (out)  328 objects, +2.9%, but only 27.1% carry text
#                              at a median of 70 chars. Rejected on density,
#                              not on size. Re-measurable if that changes.
#
# Five kept categories carry NO documentation key at all — ServiceGroup
# (1,485), AnalyticsObject (339), SDServiceGroup (325), Service Domain (2),
# Business Scenario (1): 2,152 objects, 19% of the bundle, name and category
# only. Deliberately KEPT. They are real BIAN structure, and an object with a
# name and a place in the model is still worth publishing; the alternative was
# a bundle that silently omits a fifth of the service model. Do not remove
# them on the grounds that they look empty — that question has been asked.
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
    "Business Scenario", "Work package",
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

# Categories of the *diagram* objects, used to classify view pages without
# fetching them. See plan.py.
SEQUENCE_MEMBER_CATEGORIES = {
    "Message", "Execution specification", "Interaction", "Lifeline",
    "Interaction operand", "Fragment", "Combined fragment",
}
CLASS_MEMBER_CATEGORIES = {
    "Class", "Attribute", "Generalization", "Enumeration",
    "Enumeration literal", "Operation", "Association",
}
ARCHIMATE_MEMBER_CATEGORIES = {
    "Business function", "Capability", "Flow relation", "Triggering relation",
    "Realization relation", "Serving relation", "Grouping", "Work package",
}


def data_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/data/{name}"


def shard_url(base: str, n: int) -> str:
    return data_url(base, f"all_objects_data_{n}.js")


def view_url(base: str, view_id) -> str:
    return f"{base.rstrip('/')}/views/view_{view_id}.html"


def object_url(base: str, view: int, oid) -> str:
    return f"{base.rstrip('/')}/object_{view}.html?object={oid}"


# --- parsing ---------------------------------------------------------------

def parse_js_assignments(text: str) -> dict:
    """Extract every `var name = <json>;` assignment in a file.

    Not one variable per file: all_objects_on_views.js defines several.
    raw_decode consumes a single JSON value and reports where it ended, so the
    scan resumes from there rather than failing on trailing content.
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


def parse_js_assignment(text: str):
    """The first assignment in a file, which is the payload in every data file
    consumed as a single variable."""
    return next(iter(parse_js_assignments(text).values()))


#: `data/models_data.js` defines `insite_models`, the only published statement
#: of a view's PURPOSE. `insiteViews` gives a view a name; nothing else says
#: what it is for, and zero ArchiMate viewpoints are declared anywhere in the
#: landscape.
#:
#: The file is not in the documented layout, so the location is discovered by
#: trying candidates rather than asserted. That is deliberate: the probe found
#: it this way, and no run had recorded which candidate answered, so hardcoding
#: one would be quoting a path as known without the run that proves it. The
#: order is most-likely first; the one that answers is recorded in the extract.
MODELS_CANDIDATES = ("data/models_data.js", "models_data.js",
                     "data/all_models_data.js")


def fetch_models(fetcher) -> tuple[list, str, list[str]]:
    """The `insite_models` entries, the URL that answered, and what was tried.

    Returns ([], "", tried) when none answered, so a caller can carry on and
    report the absence rather than losing the run to one missing file. Each
    candidate's outcome is printed, so a failure names which paths were tried
    and how each one failed — an empty result and a wrong URL must not look
    alike.

    Promoted from the ArchiMate probe, which is where this file was found and
    which was deleted once it had been. Promoted rather than reimplemented: the
    candidate list and the order it tries them in are what an actual run
    exercised, and rewriting them would have reproduced the guess instead.
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
            entries = _l(next(iter(parse_js_assignments(resp.text).values())))
        except Exception as e:                              # noqa: BLE001
            print(f"    {candidate:<28} unparseable ({type(e).__name__})",
                  flush=True)
            continue
        views = sum(len(_l(_d(e).get("views"))) for e in entries)
        print(f"    {candidate:<28} {len(entries)} models, {views} views",
              flush=True)
        return entries, candidate, tried
    return [], "", tried


def shard_numbers(mapping: dict) -> list[int]:
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
            # A blank stereotype value must not become a blank category:
            # the object's UML type is the right answer in that case.
            return [v for v in _l(st.get("value"))
                    if isinstance(v, str) and v.strip()]
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


#: How deep a property value may nest before flattening gives up. Structures
#: were measured at depth 1 (probe run 90418066705); the cap exists so a
#: cyclic value cannot hang a harvest, not because 8 is known to be needed.
FLATTEN_MAX_DEPTH = 8


def _flatten(value, depth: int = 0):
    """One property value as text, or a list of texts for a collection.

    Every shape the landscape actually uses is handled here, because anything
    that falls through to "" is dropped by render() WITHOUT A TRACE -- the row
    is skipped and nothing says a value was there. That is how 50,868
    `structure` values and 987 booleans stayed out of the published bundle
    unnoticed since the first harvest: parsed, found, discarded silently.

    Shapes and counts are from probe run 90418066705 over all 128,270 objects:
    string 45,192, structure 50,868, object 31,528, collection 17,117,
    bool 987, link 953, rtf 363, int 4, float 3.

    `rtf` is HTML despite its name, and is cleaned with the same `clean_html`
    that `_documentation` uses. Measured from extract run 33373471167 rather
    than guessed: all 1,794 values are shaped {"type": "rtf", "value": <str>}
    with no variants, 1,422 of them empty, and the 372 carrying text hold
    only <p>, <span>, &nbsp; and &quot; -- exactly what clean_html reduces.
    """
    if depth > FLATTEN_MAX_DEPTH:
        return ""
    if isinstance(value, str):
        return value.strip()
    # bool before int: bool is a subclass of int, so the order matters.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "link":
            v = _d(value.get("value"))
            return f"{v.get('title', '')} — {v.get('location', '')}".strip(" —")
        if kind == "object":
            return _d(value.get("value")).get("name", "")
        if kind == "rtf":
            # Named rtf, but the markup is HTML: <p>, <span>, &nbsp;, &quot;
            # and nothing else across all 1,794 values. Cleaned rather than
            # passed through, because markup reaching the output is what
            # core/checks.py flags as clean_html() not having been applied.
            text = value.get("value")
            return clean_html(text) if isinstance(text, str) else ""
        if kind == "collection":
            return [x for x in (_flatten(i, depth + 1)
                                for i in _l(value.get("value"))) if x]
        if kind == "structure":
            # A record of named fields, rendered inline so that a service
            # operation with 54 parameters stays readable. Source key order is
            # preserved: for SO parameters it is the parameter signature, and
            # reordering it would make the output harder to scan, not easier.
            parts = []
            for key, item in _d(value.get("value")).items():
                text = _flatten(item, depth + 1)
                if isinstance(text, list):
                    text = ", ".join(text)
                if text:
                    parts.append(f"{key}: {text}")
            return "; ".join(parts)
    return ""


def is_structural(category: str, name: str) -> bool:
    """Structural graph artefacts, not content."""
    return (category in EXCLUDE_CATEGORIES
            or category.endswith(" relation")
            or (name or "").endswith(" relation"))


def is_wanted(category: str, name: str) -> bool:
    """Keep only BIAN semantic content."""
    return category in INCLUDE_CATEGORIES and not is_structural(category, name)


# --- the model -------------------------------------------------------------

class Landscape:
    """One version of the landscape, loaded from its static data files.

    Holds the merged object model, the relation graph and the view membership
    map. Deliberately separate from anything that writes output, so the same
    model serves the semantic harvest, the view classifier and the diagram
    chunks without being fetched more than once.
    """

    def __init__(self, base: str, object_view: int = 16):
        self.base = base.rstrip("/")
        self.object_view = object_view
        self.objects: dict = {}
        self.relations: dict = {}
        self.on_views: dict = {}
        self.insite_views: dict = {}
        self.names: dict = {}
        self.categories: dict = {}
        self.shards: list[int] = []
        self.notes: list[str] = []
        #: What the loader ASKED FOR against what it actually got. `shards`
        #: alone is the intent, computed from the mapping before a single
        #: request is made, so an extract carrying it can declare it read a
        #: shard that 404'd. The gate needs the difference, and the notes list
        #: never left the harvest path.
        self.shard_results: dict = {"requested": [], "read": [],
                                    "mapping_ids": []}

    # -- loading ------------------------------------------------------

    def load(self, fetcher) -> "Landscape":
        # conditional=False throughout: the model has to be materialised
        # every run, so a 304 here would be an empty body and nothing to parse.
        mapping_text = fetcher.get(
            data_url(self.base, "all_objects_data_mapping.js"),
            conditional=False).text
        try:
            mapping = parse_js_assignment(mapping_text)
        except Exception:
            mapping = {}
        self.shards = shard_numbers(mapping)
        self.shard_results["requested"] = list(self.shards)
        self.shard_results["mapping_ids"] = [str(k) for k in mapping]
        print(f"  {len(self.shards)} shards to fetch: "
              f"{self.shards[0]}-{self.shards[-1]}", flush=True)

        missing = []
        for n in self.shards:
            resp = fetcher.get(shard_url(self.base, n),
                               conditional=False)
            if resp.status == 404:
                missing.append(n)
                continue
            try:
                data = parse_js_assignment(resp.text)
            except Exception as e:
                self.notes.append(f"shard {n} unparseable ({type(e).__name__})")
                continue
            before = len(self.objects)
            self.shard_results["read"].append(n)
            for oid, obj in data.items():
                self.objects.setdefault(oid, obj)
            print(f"  shard {n:<3} {len(resp.text) / 1024:>8.0f} KB  "
                  f"{len(data):>6} objects  "
                  f"(+{len(self.objects) - before} new)", flush=True)
        if missing:
            self.notes.append(f"shards absent: {', '.join(map(str, missing))}")

        try:
            self.relations = parse_js_assignment(
                fetcher.get(data_url(self.base, "all_objects_relations.js"),
                            conditional=False).text)
        except Exception:
            self.relations = {}

        try:
            variables = parse_js_assignments(
                fetcher.get(data_url(self.base, "all_objects_on_views.js"),
                            conditional=False).text)
            self.on_views = variables.get("objectsOnViews", {})
            self.insite_views = variables.get("insiteViews", {})
        except Exception:
            self.on_views, self.insite_views = {}, {}

        self._index()
        print(f"  merged {len(self.objects)} unique objects from "
              f"{len(self.shards)} shards; {len(self.relations)} carry "
              f"relations; {len(self.insite_views)} views known", flush=True)
        return self

    def _index(self):
        """Name and category for every object, used everywhere downstream."""
        for oid, obj in self.objects.items():
            entry = _l(_d(obj).get("data"))
            first = _d(entry[0]) if entry else {}
            name = first.get("name")
            self.names[oid] = name if isinstance(name, str) else ""
            sts = _stereotypes(first)
            otype = first.get("type") or ""
            if not isinstance(otype, str):
                otype = str(otype)
            self.categories[oid] = sts[0] if sts else (otype or "Other")

    # -- rendering ----------------------------------------------------

    def _relations_block(self, oid) -> list[str]:
        rels = [r for r in _l(self.relations.get(str(oid))) if isinstance(r, dict)]
        if not rels:
            return []
        lines = ["### Relationships"]
        for rel in sorted(rels, key=lambda r: r.get("via") or ""):
            via = (rel.get("via") or "").strip()
            if via in SKIP_RELATION_VERBS:
                continue
            targets = sorted(
                f"{self.names[str(t)]} ({t})" for t in _l(rel.get("to"))
                if isinstance(t, (str, int)) and self.names.get(str(t))
                and not (self.names[str(t)] or "").endswith(" relation"))
            if targets:
                lines.append(f"- **{via}:** " + "; ".join(targets))
        return lines + [""] if len(lines) > 1 else []

    def render(self, oid, entry) -> tuple[str, str]:
        entry = _d(entry)
        sts = _stereotypes(entry)
        otype = entry.get("type") or ""
        if not isinstance(otype, str):
            otype = str(otype)
        lines = [
            f"## {entry.get('name') or f'Object {oid}'}", "",
            f"- **Object id:** {oid}",
            f"- **Type:** {otype}" + (f" ({', '.join(sts)})" if sts else ""),
            f"- **Source:** {object_url(self.base, self.object_view, oid)}", "",
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
                                + " / ".join(v.strip()
                                             for v in val.split("\n") if v.strip()))
            if rows:
                lines += [f"### {group}", *rows, ""]

        lines += self._relations_block(oid)
        lines += ["---", ""]
        return "\n".join(lines), (sts[0] if sts else otype or "Other")

    def semantic_items(self) -> tuple[list[dict], dict, list]:
        """Every object the allowlist keeps, rendered to markdown.

        Returns (items, dropped_by_category, skipped) so the caller can report
        what was filtered and what was malformed without this deciding how any
        of it should be printed.
        """
        items, skipped, dropped = [], [], {}
        for oid, obj in self.objects.items():
            data = _l(_d(obj).get("data"))
            if not data or not isinstance(data[0], dict):
                skipped.append((oid, type(data[0]).__name__ if data else "empty"))
                continue
            try:
                body, category = self.render(oid, data[0])
            except Exception as e:
                # One malformed object must not abort a 128,000-object harvest.
                skipped.append((oid, f"{type(e).__name__}"))
                continue
            name = self.names.get(oid, "")
            if not is_wanted(category, name):
                dropped[category] = dropped.get(category, 0) + 1
                continue
            items.append({"id": oid, "name": name,
                          "category": category, "body": body})
        return items, dropped, skipped

    # -- views --------------------------------------------------------

    def views_to_members(self) -> dict[str, list[str]]:
        """Invert objectsOnViews: {viewId: [objectId, ...]}.

        This is what makes it possible to classify a diagram — and to size it —
        without fetching the page first.
        """
        out: dict[str, list[str]] = {}
        for oid, views in self.on_views.items():
            for vid in _l(views):
                out.setdefault(str(vid), []).append(str(oid))
        return out

    def view_name(self, vid) -> str:
        return (_d(self.insite_views.get(str(vid))).get("name") or "").strip()
