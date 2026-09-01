#!/usr/bin/env python3
"""
The source input gate: what does BIAN publish that we do not consume?

Every other check in this project runs DOWNSTREAM of the extractor and is
therefore blind in exactly one direction. `check_extract.py` asks whether the
extract is internally consistent, and it stays perfectly consistent when a
whole shard is missing: every object still resolves, every category is still
declared. `select.py` reports what the allowlist dropped, but it can only
report categories that reached it. A filter cannot see the population it
excludes, and neither can anything placed after the filter.

This module looks the other way. It observes what is actually present in the
source, compares it against a DECLARATION of what the parser handles, and
reports what is present and unconsumed.

THE DECLARATION IS AN ALLOWLIST, NOT AN EXCLUSION LIST. Anything observed and
undeclared is a finding. That is what makes this a gate rather than an audit:
a field that appears in v15 fails a run instead of being quietly absent from
the output.

WHAT A KEY CENSUS WOULD MISS, AND WHY THIS ONE COUNTS MORE THAN KEYS

Three of the known losses are not key-shaped at all:

  - Only `data[0]` is ever read. `data` is present and consumed; entries 1..n
    vanish. The loss is a LIST INDEX.
  - Only the first `table` category is read. Same shape.
  - `clean_html` deletes structural tags with no separator, so
    `<td>A</td><td>B</td>` becomes `AB`. Every key consumed, content corrupted.

So this records key paths, list cardinality, value-type discriminators and the
HTML tag inventory. A finding carries the number of VALUES behind it and the
denominator that number is over, because a key on three objects and a key on
ninety thousand are not the same problem and must not cost the same.

THREE CLASSES OF FINDING, AND ONLY ONE IS THRESHOLDABLE

`FAIL_ALWAYS`   Fetch and parse integrity, and schema reachability. You cannot
                threshold your own denominator: if a shard was never read,
                every share computed below it is over a population that does
                not exist, and the sub-threshold bucket fills with numbers that
                mean nothing.
`THRESHOLDED`   Undeclared shapes and the non-key losses. Failing above the
                threshold, recorded below it.
`REGISTERED`    Declared in EXCLUSIONS and within its bound. Reported, never
                failing. That is what declaring it meant.

SUB-THRESHOLD FINDINGS ARE CARRIED, NOT PRINTED. They go into the extract's
`status.gate`, so they travel with the artifact, survive the run log, and diff
between runs. A bucket that empties into a world-readable log nobody reads is
the silent-drop mechanism this module exists to remove, moved up one level.

This module makes NO requests. `observe()` takes an already-loaded Landscape
plus whatever `source.py` fetched for it, exactly as `extract.build()` takes
`geometry`. That keeps it testable without reaching bian.org, which is the same
property `build()` protects and for the same reason.
"""

from __future__ import annotations

import re

from bianlib import landscape as L

#: Bumped when the declaration or the finding codes change, so a gate result
#: says which rules produced it. A result without this is uninterpretable
#: once the rules move.
GATE_VERSION = "1"

# --- the declaration -------------------------------------------------------
#
# What the parser handles. Derived by READING the parser, not by asserting what
# it ought to do; each entry names the function that consumes it, so a reader
# can check the claim rather than trust it.

#: `Landscape._index` and `extract._icon_path` read the object wrapper.
#: `id` is read by nothing -- the shard's own key is used -- but it is declared
#: because it is understood and deliberately redundant, not unnoticed.
HANDLED_WRAPPER_KEYS = {"id", "data", "typeIconPath"}

#: `Landscape._index` reads `name` and `type`; `_categories` reads `categories`.
#: `lang` is read by nothing: only data[0] is consumed. Declared, and paired
#: with the ONLY_FIRST_DATA_ENTRY finding below so the drop is visible.
HANDLED_ENTRY_KEYS = {"lang", "name", "type", "categories"}

#: `_documentation` branches on "documentation"; `_properties` and
#: `_stereotypes` branch on "table". There is no third branch and no else.
HANDLED_CATEGORY_TYPES = {"documentation", "table"}

#: A `categories[]` entry. `title` is consumed on documentation entries and
#: dropped on table entries -- see EXCLUSIONS.
HANDLED_CATEGORY_KEYS = {"type", "title", "content"}

#: The `type` discriminators `_flatten` recognises on a property value. Bare
#: strings, bools, ints and floats are handled by isinstance and carry no
#: discriminator, so they are not in this set.
HANDLED_PROPERTY_TYPES = {"link", "object", "rtf", "collection", "structure"}

#: `_relations_block` and `extract.build` read `via` and `to`.
HANDLED_RELATION_KEYS = {"via", "to"}

#: `Landscape.view_name` reads `name`. Nothing else in insiteViews is consumed.
HANDLED_INSITE_VIEW_KEYS = {"name"}

#: `_models_index` reads `name` and `views`; within a view entry it reads `id`.
HANDLED_MODEL_KEYS = {"name", "views"}
HANDLED_MODEL_VIEW_KEYS = {"id", "title"}

#: Tags `clean_html` turns into a paragraph break. Every OTHER tag is deleted
#: with no separator, so a tag outside this set can silently join two words.
HANDLED_HTML_TAGS = {"p", "br"}

#: Tags that are deleted but cannot join words, because they are inline and
#: their content is continuous prose. Declared so the finding below reports
#: only tags that carry a boundary.
INLINE_HTML_TAGS = {"span", "b", "i", "em", "strong", "u", "a", "font", "sup",
                    "sub", "small", "big", "code", "tt"}


# --- the exclusions register -----------------------------------------------
#
# Every deliberate exclusion, declared. A comment saying "we skip this" and an
# unnoticed drop read identically from the outside; an entry here does not.
#
# `bound` is the largest affected count that is still the decision rather than
# a change in the source. None means unmeasured -- the first run fills it in.
# A bound is a prompt to re-decide, not a licence to stop looking.

EXCLUSIONS = [
    {"code": "X-DATA-TAIL", "what": "data[1:] (non-first language entries)",
     "where": "Landscape._index, extract._first_entry",
     "why": "Only data[0] is consumed. NOT a validated exclusion: no run has "
            "established how many entries exist, because config_data.js is "
            "probed and never parsed. Declared so it stops looking handled.",
     "bound": 0},
    {"code": "X-TABLE-TAIL", "what": "second and later `table` categories",
     "where": "landscape._properties, landscape._stereotypes",
     "why": "Both return on the first match. Unvalidated for the same reason.",
     "bound": 0},
    {"code": "X-TABLE-TITLE", "what": "`title` on a table category",
     "where": "landscape._properties",
     "why": "Group names come from the content keys instead. The title has "
            "never been observed to carry anything the groups do not.",
     "bound": None},
    {"code": "X-UNKNOWN-ROLE", "what": "relation verb `<unknown role>`",
     "where": "landscape.SKIP_RELATION_VERBS",
     "why": "Documented noise. The empty verb is in the same set and is NOT "
            "noise -- it is a shape failure, so it is reported separately.",
     "bound": None},
    {"code": "X-RELATION-OBJECTS", "what": "ArchiMate relation objects",
     "where": "landscape.is_structural (stage 2 only)",
     "why": "They carry no documentation and their edges render inline on "
            "each real object. Stage 1 keeps them; only the bundle drops them.",
     "bound": None},
    {"code": "X-OBJECT-PAGE", "what": "object_16.html",
     "where": "never fetched",
     "why": "JS-rendered shell, returns nothing. See references/dead-ends.md.",
     "bound": 0},
]


# --- findings --------------------------------------------------------------

FAIL_ALWAYS = "fail-always"
THRESHOLDED = "thresholded"
REGISTERED = "registered"

NOT_MEASURED = "NOT MEASURED"


def _finding(code, what, affected, denominator, cls, detail=""):
    """One finding, always with its denominator.

    `affected` may be None, which means NOT MEASURED -- the observation could
    not be made. That is never the same as zero, and evaluate() refuses to
    treat it as a pass.
    """
    return {"code": code, "what": what, "affected": affected,
            "denominator": denominator, "class": cls, "detail": detail}


def _keys_of(value) -> set:
    return set(value.keys()) if isinstance(value, dict) else set()


TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)")


def _tags(html: str) -> set:
    return {m.lower() for m in TAG_RE.findall(html or "")}


def _bump(counter: dict, key, n: int = 1):
    counter[str(key)] = counter.get(str(key), 0) + n


# --- observation -----------------------------------------------------------

def observe(landscape, config=None, view_data=None, shard_results=None) -> dict:
    """Inventory the source and compare it against the declaration.

    Pure. `config` is the parsed config_data.js, `view_data` a mapping of
    sampled view id to parsed per-view data, `shard_results` the loader's
    record of which shards were requested and which were read. All are passed
    in rather than fetched, so this runs against a stored model.

    Any of them may be None, which produces NOT MEASURED findings rather than
    zeros. A zero from an observation that did not happen is the failure mode
    this whole module exists to prevent, and it would be absurd to reproduce
    it here.
    """
    inv: dict = {}
    findings: list = []

    objects = landscape.objects
    n_objects = len(objects)

    # -- 1. fetch and parse integrity -------------------------------------
    requested = list(shard_results.get("requested", [])) if shard_results else []
    read = list(shard_results.get("read", [])) if shard_results else []
    if shard_results is None:
        findings.append(_finding(
            "G01-SHARDS", "every requested shard was read", None, None,
            FAIL_ALWAYS, "shard results not supplied"))
    else:
        missing = [n for n in requested if n not in set(read)]
        findings.append(_finding(
            "G01-SHARDS", "every requested shard was read",
            len(missing), len(requested), FAIL_ALWAYS,
            f"unread: {missing}" if missing else ""))
    inv["shards_requested"] = len(requested)
    inv["shards_read"] = len(read)

    mapping_ids = set(shard_results.get("mapping_ids", [])) if shard_results else None
    if mapping_ids is None:
        findings.append(_finding(
            "G02-MAPPING", "every id in the mapping has an object", None,
            None, FAIL_ALWAYS, "mapping ids not supplied"))
        findings.append(_finding(
            "G03-UNMAPPED", "every object is named by the mapping", None,
            None, THRESHOLDED, "mapping ids not supplied"))
    else:
        have = set(map(str, objects))
        findings.append(_finding(
            "G02-MAPPING", "every id in the mapping has an object",
            len(mapping_ids - have), len(mapping_ids), FAIL_ALWAYS))
        findings.append(_finding(
            "G03-UNMAPPED", "every object is named by the mapping",
            len(have - mapping_ids), len(have), THRESHOLDED))

    # -- 2. object wrapper and entry --------------------------------------
    wrapper_keys: dict = {}
    entry_keys: dict = {}
    data_lengths: dict = {}
    langs: dict = {}
    category_types: dict = {}
    doc_titles: dict = {}
    category_keys: dict = {}
    property_types: dict = {}
    html_tags: dict = {}

    multi_data = 0
    multi_table = 0
    nonstring_name = 0
    colliding_doc_titles = 0
    empty_after_clean = 0
    boundary_tag_values = 0
    doc_values = 0

    for oid, obj in objects.items():
        for k in _keys_of(obj):
            _bump(wrapper_keys, k)
        data = L._l(L._d(obj).get("data"))
        _bump(data_lengths, len(data))
        if len(data) > 1:
            multi_data += 1
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for k in entry:
                _bump(entry_keys, k)
            lang = entry.get("lang")
            if isinstance(lang, str):
                _bump(langs, lang)

        first = L._d(data[0]) if data and isinstance(data[0], dict) else {}
        if data and isinstance(data[0], dict):
            name = first.get("name")
            if name is not None and not isinstance(name, str):
                nonstring_name += 1

        tables = 0
        seen_titles: set = set()
        for cat in L._categories(first):
            ctype = cat.get("type")
            _bump(category_types, ctype)
            for k in cat:
                _bump(category_keys, k)
            if ctype == "table":
                tables += 1
                content = L._d(cat.get("content"))
                for _group, fields in content.items():
                    if not isinstance(fields, dict):
                        continue
                    for _key, raw in fields.items():
                        if isinstance(raw, dict):
                            _bump(property_types, raw.get("type"))
                        elif isinstance(raw, list):
                            _bump(property_types, "(bare list)")
            elif ctype == "documentation":
                title = cat.get("title") or "documentation"
                _bump(doc_titles, title)
                if title in seen_titles:
                    colliding_doc_titles += 1
                seen_titles.add(title)
                content = cat.get("content")
                value = (content if isinstance(content, str)
                         else L._d(content).get("value", ""))
                if isinstance(value, str):
                    doc_values += 1
                    tags = _tags(value)
                    for t in tags:
                        _bump(html_tags, t)
                    boundary = tags - HANDLED_HTML_TAGS - INLINE_HTML_TAGS
                    if boundary:
                        boundary_tag_values += 1
                    if value.strip() and not L.clean_html(value):
                        empty_after_clean += 1
        if tables > 1:
            multi_table += 1

    inv["wrapper_keys"] = wrapper_keys
    inv["entry_keys"] = entry_keys
    inv["data_lengths"] = data_lengths
    inv["langs"] = langs
    inv["category_types"] = category_types
    inv["category_keys"] = category_keys
    inv["documentation_titles"] = len(doc_titles)
    inv["property_value_types"] = property_types
    inv["html_tags"] = html_tags

    findings.append(_finding(
        "G10-WRAPPER-KEY", "object wrapper keys are all handled",
        sum(n for k, n in wrapper_keys.items()
            if k not in HANDLED_WRAPPER_KEYS), n_objects, THRESHOLDED,
        ", ".join(sorted(set(wrapper_keys) - HANDLED_WRAPPER_KEYS))))
    findings.append(_finding(
        "G11-ENTRY-KEY", "object entry keys are all handled",
        sum(n for k, n in entry_keys.items() if k not in HANDLED_ENTRY_KEYS),
        n_objects, THRESHOLDED,
        ", ".join(sorted(set(entry_keys) - HANDLED_ENTRY_KEYS))))
    findings.append(_finding(
        "G12-CATEGORY-TYPE", "categories[] types are all handled",
        sum(n for k, n in category_types.items()
            if k not in HANDLED_CATEGORY_TYPES), n_objects, THRESHOLDED,
        ", ".join(sorted(str(k) for k in
                         set(category_types) - HANDLED_CATEGORY_TYPES))))
    findings.append(_finding(
        "G13-CATEGORY-KEY", "categories[] entry keys are all handled",
        sum(n for k, n in category_keys.items()
            if k not in HANDLED_CATEGORY_KEYS), n_objects, THRESHOLDED,
        ", ".join(sorted(set(category_keys) - HANDLED_CATEGORY_KEYS))))
    findings.append(_finding(
        "G14-PROPERTY-TYPE", "property value types are all handled",
        sum(n for k, n in property_types.items()
            if k not in HANDLED_PROPERTY_TYPES and k != "None"),
        sum(property_types.values()), THRESHOLDED,
        ", ".join(sorted(set(property_types) - HANDLED_PROPERTY_TYPES))))

    # Non-key losses. None of these would be visible to a key census.
    findings.append(_finding(
        "G20-DATA-TAIL", "objects carrying more than one data entry",
        multi_data, n_objects, THRESHOLDED))
    findings.append(_finding(
        "G21-TABLE-TAIL", "objects carrying more than one table category",
        multi_table, n_objects, THRESHOLDED))
    findings.append(_finding(
        "G22-DOC-TITLE", "documentation titles colliding within one object",
        colliding_doc_titles, n_objects, THRESHOLDED))
    findings.append(_finding(
        "G23-DOC-EMPTY", "documentation values that clean to nothing",
        empty_after_clean, doc_values, THRESHOLDED))
    findings.append(_finding(
        "G24-HTML-TAG", "documentation values carrying a boundary tag "
                        "clean_html deletes without a separator",
        boundary_tag_values, doc_values, THRESHOLDED,
        ", ".join(sorted(set(html_tags) - HANDLED_HTML_TAGS
                         - INLINE_HTML_TAGS))))
    findings.append(_finding(
        "G25-NAME-TYPE", "names that are not strings",
        nonstring_name, n_objects, THRESHOLDED))

    # -- 3. relations ------------------------------------------------------
    edge_keys: dict = {}
    target_types: dict = {}
    verbs: dict = {}
    empty_verb = 0
    bad_target = 0
    n_edges = 0
    for _src, edges in landscape.relations.items():
        for edge in L._l(edges):
            if not isinstance(edge, dict):
                continue
            n_edges += 1
            for k in edge:
                _bump(edge_keys, k)
            via = (edge.get("via") or "").strip()
            _bump(verbs, via or "(empty)")
            if not via:
                empty_verb += 1
            for target in L._l(edge.get("to")):
                _bump(target_types, type(target).__name__)
                if not isinstance(target, (str, int)):
                    bad_target += 1

    inv["relation_edge_keys"] = edge_keys
    inv["relation_target_types"] = target_types
    inv["relation_verbs"] = len(verbs)

    findings.append(_finding(
        "G30-EDGE-KEY", "relation edge keys are all handled",
        sum(n for k, n in edge_keys.items() if k not in HANDLED_RELATION_KEYS),
        n_edges, THRESHOLDED,
        ", ".join(sorted(set(edge_keys) - HANDLED_RELATION_KEYS))))
    findings.append(_finding(
        "G31-EDGE-TARGET", "relation targets that are neither str nor int",
        bad_target, sum(target_types.values()), THRESHOLDED))
    findings.append(_finding(
        "G32-EDGE-VERB", "relation edges carrying an empty verb",
        empty_verb, n_edges, THRESHOLDED,
        "the empty verb is a shape failure, not documented noise"))

    # -- 4. views and models ----------------------------------------------
    view_keys: dict = {}
    for _vid, meta in landscape.insite_views.items():
        for k in _keys_of(meta):
            _bump(view_keys, k)
    inv["insite_view_keys"] = view_keys
    findings.append(_finding(
        "G40-VIEW-KEY", "insiteViews keys are all handled",
        sum(n for k, n in view_keys.items()
            if k not in HANDLED_INSITE_VIEW_KEYS),
        len(landscape.insite_views), THRESHOLDED,
        ", ".join(sorted(set(view_keys) - HANDLED_INSITE_VIEW_KEYS))))

    # -- 5. artefacts that are fetched but never parsed --------------------
    if config is None:
        findings.append(_finding(
            "G50-CONFIG", "config_data.js was parsed", None, None,
            FAIL_ALWAYS, "not supplied"))
        inv["available_languages"] = NOT_MEASURED
    else:
        languages = []
        for value in (config or {}).values():
            if isinstance(value, list):
                languages = [v for v in value if isinstance(v, (str, dict))]
                break
        inv["available_languages"] = len(languages)
        # The denominator for G20. More than one published language with only
        # data[0] read is content loss, not a curiosity.
        findings.append(_finding(
            "G51-LANGUAGES", "languages published beyond the one consumed",
            max(0, len(languages) - 1), len(languages) or 1, THRESHOLDED))

    if not view_data:
        findings.append(_finding(
            "G60-VIEWDATA", "per-view data files were sampled", None, None,
            FAIL_ALWAYS, "no view sampled"))
        inv["view_data_keys"] = NOT_MEASURED
        inv["viewpoints_declared"] = NOT_MEASURED
    else:
        vkeys: dict = {}
        viewpoints = 0
        legends = 0
        for _vid, payload in view_data.items():
            for k in _keys_of(payload):
                _bump(vkeys, k)
            if L._l(payload.get("viewpointsData")) or \
                    L._d(payload.get("viewpointsData")):
                viewpoints += 1
            if L._l(payload.get("vp_legends")) or \
                    L._d(payload.get("vp_legends")):
                legends += 1
        inv["view_data_keys"] = vkeys
        inv["viewpoints_declared"] = viewpoints
        inv["vp_legends_declared"] = legends
        # Nothing in the bulk path reads this file at all, so EVERY key in it
        # is unconsumed. The finding is the content behind those keys, and the
        # sample size is the denominator -- this is a sample, and says so.
        findings.append(_finding(
            "G61-VIEWDATA-UNREAD", "sampled views whose data file carries "
                                   "viewpoints or legends nothing reads",
            max(viewpoints, legends), len(view_data), THRESHOLDED,
            "sampled, not a population: " + ", ".join(sorted(vkeys))))

    return {"gate_version": GATE_VERSION, "inventory": inv,
            "findings": findings,
            "exclusions": [dict(x) for x in EXCLUSIONS]}


# --- evaluation ------------------------------------------------------------

def evaluate(observation: dict, max_share: float = 0.5,
             max_absolute: int = 500, max_total_share: float = 2.0,
             observe_only: bool = False) -> dict:
    """Classify findings against the thresholds. Returns a verdict dict.

    Two thresholds, failing on either: the share catches broad drift, and the
    absolute catches a rare-but-large loss that a six-figure denominator would
    otherwise dilute below any share worth setting.

    `max_total_share` caps the SUM of everything sub-threshold. Without it the
    bucket leaks: fifty findings at 0.9% each pass individually while a third
    of the content walks out.

    `observe_only` suppresses threshold failures but NOT fail-always ones. A
    first run has no population to set thresholds from -- a threshold taken
    from a sample mis-fires at scale, and there is no sample here that is not
    the whole landscape -- but a shard that did not load is not a threshold
    question and must fail on the first run as on every other.
    """
    failed, under, registered, unmeasured = [], [], [], []
    total_share = 0.0

    for f in observation.get("findings", []):
        affected, denom, cls = f["affected"], f["denominator"], f["class"]
        if affected is None:
            unmeasured.append(f)
            continue
        share = (100.0 * affected / denom) if denom else 0.0
        entry = dict(f, share=round(share, 4))
        if affected == 0:
            continue
        if cls == REGISTERED:
            registered.append(entry)
            continue
        if cls == FAIL_ALWAYS:
            failed.append(entry)
            continue
        over = share > max_share or affected > max_absolute
        if over and not observe_only:
            failed.append(entry)
        else:
            under.append(entry)
            total_share += share

    aggregate_breached = (total_share > max_total_share and not observe_only)
    ok = not failed and not unmeasured and not aggregate_breached

    return {
        "gate_version": observation.get("gate_version"),
        "observe_only": observe_only,
        "thresholds": {"max_share": max_share, "max_absolute": max_absolute,
                       "max_total_share": max_total_share},
        "ok": ok,
        "failed": failed,
        "under_threshold": under,
        "registered": registered,
        "not_measured": unmeasured,
        "under_threshold_total_share": round(total_share, 4),
        "aggregate_breached": aggregate_breached,
    }


def schema_reachability(status_keys, schema: dict) -> list:
    """Findings for status keys the schema does not declare.

    `status.additionalProperties` is true, which is the one open extension
    point in the schema and is worth keeping -- but it is also why six emitted
    counters currently validate while being unknown to the contract. A counter
    that lands there and is never declared is a counter that can vanish
    without failing anything.

    FAIL_ALWAYS: an undeclared key is a certainty, not a measurement.
    """
    try:
        block = schema["allOf"][0]["then"]["properties"]["status"]
        declared = set(block.get("properties", {}))
    except Exception:                                       # noqa: BLE001
        return [_finding("G70-SCHEMA", "status keys are declared in the "
                         "schema", None, None, FAIL_ALWAYS,
                         "status block not found in the schema")]
    undeclared = sorted(set(map(str, status_keys)) - declared)
    return [_finding("G70-SCHEMA", "status keys are declared in the schema",
                     len(undeclared), len(set(status_keys)), FAIL_ALWAYS,
                     ", ".join(undeclared))]


def report(verdict: dict) -> list[str]:
    """Human-readable evidence: codes, counts and denominators only.

    Never source text. Actions logs on a public repo are world-readable, and
    key names and tag names are structure while the values behind them are
    content.
    """
    def line(f):
        denom = f["denominator"]
        share = f"{f['share']:.3f}%" if denom else "no denominator"
        detail = f"  [{f['detail']}]" if f.get("detail") else ""
        return (f"    {f['code']:<22} {f['affected']:>8} of "
                f"{denom if denom is not None else '?':>8}  {share}{detail}")

    out = [f"  gate v{verdict.get('gate_version')}"
           + ("  OBSERVE-ONLY" if verdict.get("observe_only") else "")]
    t = verdict["thresholds"]
    out.append(f"  thresholds: share > {t['max_share']}% or count > "
               f"{t['max_absolute']}; aggregate > {t['max_total_share']}%")

    for label, key in (("NOT MEASURED", "not_measured"),
                       ("FAILED", "failed"),
                       ("under threshold (carried)", "under_threshold"),
                       ("registered exclusions", "registered")):
        items = verdict.get(key) or []
        out.append(f"\n  {label}: {len(items)}")
        for f in items:
            out.append(line(f) if "share" in f else
                       f"    {f['code']:<22} {NOT_MEASURED}  "
                       f"[{f.get('detail', '')}]")

    out.append(f"\n  sub-threshold total: "
               f"{verdict['under_threshold_total_share']}% "
               f"(cap {t['max_total_share']}%)")
    if verdict.get("aggregate_breached"):
        out.append("  AGGREGATE CAP BREACHED — individually small findings "
                   "sum to more than the cap allows")
    return out
