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
from core.render import ALNUM_RE, clean_html_v2

#: Bumped when the declaration or the finding codes change, so a gate result
#: says which rules produced it. A result without this is uninterpretable
#: once the rules move.
GATE_VERSION = "5"

#: How many before/after pairs G26 carries in the extract. Bounded: the point
#: is enough evidence to judge the change by, not a second copy of the corpus.
GATE_CLEAN_SAMPLES = 25

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

#: `Landscape.view_name` reads `name`. `id` is the view's own key echoed into
#: the value and is read by nothing, declared for the same reason `id` is
#: declared on the object wrapper: understood and deliberately redundant, not
#: unnoticed. Omitting it made G40 report 2,285 of 2,285 -- a 100% finding that
#: was entirely an error in this declaration, and which on its own supplied 100
#: of the 101.57% sub-threshold aggregate on run 33464689986. A declaration
#: fault does not just raise a false finding; it destroys the aggregate, which
#: is the number meant to catch many small real ones.
HANDLED_INSITE_VIEW_KEYS = {"id", "name"}

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

#: The variables `data/view_<id>_data.js` declares. MEASURED on run
#: 33468063747: all seven present in all twelve sampled views. Nothing on the
#: bulk path reads any of them.
#:
#: Gate v1 saw only `objectData`, because it read the file with
#: parse_js_assignment -- one value, from a file that declares seven. That is
#: how "viewpoints declared: 0" came back as a zero rather than as the NOT
#: MEASURED it was.
#:
#: `viewpointsData` and `vp_legends` are present and EMPTY in all twelve, which
#: is the first real measurement of the ArchiMate viewpoint question. Empty is
#: not absent and neither is a licence to stop probing: a later landscape
#: version can fill them, and only a run that keeps looking would notice.
VIEW_DATA_FIELDS = ("objectData", "objectReferences", "objectRelations",
                    "viewData", "viewReferences", "viewpointsData",
                    "vp_legends")


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
     "why": "Only data[0] is consumed. VALIDATED on run 33464689986: every "
            "one of 128,270 objects carries exactly one data entry, all "
            "lang 'en', and config_data.js declares one available language. "
            "So this drops nothing today. It stays declared because that is "
            "a fact about v14, not about the parser -- a second published "
            "language would be silent content loss, and G20 is what would "
            "say so.",
     "bound": 0},
    {"code": "X-TABLE-TAIL", "what": "second and later `table` categories",
     "where": "landscape._properties, landscape._stereotypes",
     "why": "Both return on the first match. VALIDATED on run 33464689986: "
            "42,861 table categories across 128,270 objects, never more than "
            "one on an object, so the tail is empty.",
     "bound": 0},
    {"code": "X-TABLE-TITLE", "what": "`title` on a table category",
     "where": "landscape._properties",
     "why": "Group names come from the content keys instead. MEASURED on run "
            "33464689986: every table category carries a title, so this is a "
            "real and universal drop rather than a theoretical one. Kept "
            "because the titles have not been shown to carry anything the "
            "group names do not -- which is a claim worth testing before the "
            "bound is raised, not after. Reported every run as G15.",
     "bound": 42861},
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
    {"code": "X-VIEW-DATA", "what": "data/view_<id>_data.js (all seven vars)",
     "where": "never read on the bulk path",
     "why": "MEASURED on run 33468063747 across 12 of 2,285 views, stratified "
            "by diagram category. viewpointsData and vp_legends present and "
            "EMPTY in all twelve; objectReferences non-empty in eleven, in an "
            "id space where 504 of 549 keys resolve to no object and no view "
            "we hold. Kept out on the working belief that it is per-diagram "
            "presentation data -- a BELIEF, not a validated exclusion, and "
            "G63 is what would contradict it.",
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


def _finding(code, what, affected, denominator, cls, detail="",
             sampled=False):
    """One finding, always with its denominator.

    `affected` may be None, which means NOT MEASURED -- the observation could
    not be made. That is never the same as zero, and evaluate() refuses to
    treat it as a pass.

    `sampled` marks a finding whose denominator is a SAMPLE rather than the
    population. Such a finding may still fail on its own, but it is excluded
    from the at-risk aggregate: 1 of 12 sampled views is 8.3%, and adding that
    to a share taken over 128,270 objects produces a number that means
    nothing. A count is only interpretable against the denominator it came
    from, and summing across denominators throws that away.
    """
    return {"code": code, "what": what, "affected": affected,
            "denominator": denominator, "class": cls, "detail": detail,
            "sampled": bool(sampled)}


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
            "G03-SURPLUS", "objects the shards hold that the mapping does "
                           "not name (surplus, not loss)", None, None,
            REGISTERED, "mapping ids not supplied"))
    else:
        have = set(map(str, objects))
        findings.append(_finding(
            "G02-MAPPING", "every id in the mapping has an object",
            len(mapping_ids - have), len(mapping_ids), FAIL_ALWAYS))
        # SURPLUS, NOT LOSS, and the two must not share a class.
        #
        # This counts objects the shards hold that objectDataMapping never
        # names. We have MORE than the mapping declares, so no content is at
        # risk and nothing here belongs in an at-risk aggregate. Measured at
        # 1,359 of 128,270 (1.06%) on run 33464689986, where it sat above the
        # provisional 0.5% loss threshold and would have failed enforcement
        # for being the wrong shape of number, not for being wrong.
        #
        # It is kept, and kept visible, because it says the mapping is not a
        # complete index of the model -- which matters to anything that might
        # later treat it as one. REGISTERED reports it every run and fails on
        # nothing, which is what its being a declared, understood asymmetry
        # means.
        findings.append(_finding(
            "G03-SURPLUS", "objects the shards hold that the mapping does "
                           "not name (surplus, not loss)",
            len(have - mapping_ids), len(have), REGISTERED))

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
    table_titles = 0
    #: G26. What the proposed cleaner would actually change, measured on the
    #: source rather than argued from an example. Split by whether the object
    #: survives the stage 2 allowlist, because "138 values" and "138 PUBLISHED
    #: values" are different claims and only the second is about what anyone
    #: reads.
    clean_changed = 0
    clean_changed_published = 0
    doc_values_published = 0
    boundary_tag_published = 0
    empty_after_clean_published = 0
    #: A bounded before/after sample, so the next changeset can be judged on
    #: what it does rather than on what it is supposed to do. Bounded because
    #: this rides in the extract, and truncated per value for the same reason.
    clean_samples: list = []
    #: G27. Values where the proposed cleaner emits a line carrying no letter
    #: or digit -- punctuation that lost the text it belonged to. Run
    #: 33477632935 produced one (object 141803) out of 25 carried pairs, in
    #: markup that puts the quotes outside the list item. Counted every run so
    #: the NEXT such case arrives as a number rather than as something spotted
    #: by eye in a sample of 25.
    clean_stranded = 0
    clean_stranded_published = 0
    stranded_samples: list = []
    nonstring_name = 0
    colliding_doc_titles = 0
    empty_after_clean = 0
    boundary_tag_values = 0
    doc_values = 0

    for oid, obj in objects.items():
        # Whether stage 2 would publish this object. The allowlist is IMPORTED,
        # never restated: a tool that re-declared it shipped a wrong published
        # count in this project once already.
        category = landscape.categories.get(str(oid), "")
        published = L.is_wanted(category, landscape.names.get(str(oid), ""))
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
                if isinstance(cat.get("title"), str) and cat["title"].strip():
                    table_titles += 1
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
                    if published:
                        doc_values_published += 1
                    tags = _tags(value)
                    for t in tags:
                        _bump(html_tags, t)
                    boundary = tags - HANDLED_HTML_TAGS - INLINE_HTML_TAGS
                    if boundary:
                        boundary_tag_values += 1
                        if published:
                            boundary_tag_published += 1
                    if value.strip() and not L.clean_html(value):
                        empty_after_clean += 1
                        if published:
                            empty_after_clean_published += 1
                    # G26: run both cleaners over the real value and record
                    # what would move. This is the before/after, produced by
                    # the run on the source, rather than by an example chosen
                    # to show the fix working.
                    before = L.clean_html(value)
                    after = clean_html_v2(value)
                    stranded = [ln for ln in after.split("\n")
                                if ln.strip() and not ALNUM_RE.search(ln)]
                    if stranded:
                        clean_stranded += 1
                        if published:
                            clean_stranded_published += 1
                            if len(stranded_samples) < GATE_CLEAN_SAMPLES:
                                stranded_samples.append({
                                    "object": str(oid), "category": category,
                                    "lines": stranded[:6],
                                    "before": before[:400],
                                    "after": after[:400]})
                    if before != after:
                        clean_changed += 1
                        if published:
                            clean_changed_published += 1
                            if len(clean_samples) < GATE_CLEAN_SAMPLES:
                                clean_samples.append({
                                    "object": str(oid),
                                    "category": category,
                                    "section": title,
                                    "tags": sorted(boundary),
                                    "before": before[:400],
                                    "after": after[:400]})
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
    # A key can be declared handled and still be dropped, when it is consumed
    # on one category type and not another. `title` is read on documentation
    # entries and discarded on table entries, so the coarse key-level check
    # above sees it as handled on all 71,844. Run 33464689986 exposed the same
    # defect class in the insiteViews declaration, where a missing `id` made a
    # 100% finding out of nothing. A declaration must be as fine-grained as
    # the parser it describes.
    findings.append(_finding(
        "G15-TABLE-TITLE", "titles on table categories, which nothing reads",
        table_titles, sum(n for k, n in category_types.items()
                          if k == "table") or n_objects, REGISTERED,
        "declared in EXCLUSIONS as X-TABLE-TITLE. REGISTERED, so it reports "
        "every run and enters no at-risk aggregate: a declared exclusion "
        "counted as loss is exactly what made the v1 aggregate 101.57%"))
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
        empty_after_clean, doc_values, THRESHOLDED,
        f"{empty_after_clean_published} of them on published objects"))
    inv["documentation_published"] = doc_values_published
    inv["clean_html_v2_samples"] = clean_samples
    inv["clean_html_v2_stranded_samples"] = stranded_samples

    # G27 is FAIL-ALWAYS on the published population. A stranded delimiter is
    # a value the current cleaner renders readably and the proposed one
    # breaks, so it is a regression rather than a quantity to tolerate, and
    # observe-only must not wave it through: the point of measuring before
    # adopting is lost if the measurement can pass while broken.
    findings.append(_finding(
        "G27-CLEAN-STRANDED",
        "published values where the proposed cleaner strands punctuation on "
        "its own line",
        clean_stranded_published, doc_values_published or 1, FAIL_ALWAYS,
        f"{clean_stranded} across all objects"))

    # G26 is the decision this measurement exists to inform: adopt v2 or not.
    # THRESHOLDED on the PUBLISHED count, because a value nobody reads being
    # reformatted is not a risk to anything.
    findings.append(_finding(
        "G26-CLEAN-DELTA",
        "published documentation values the proposed cleaner would change",
        clean_changed_published, doc_values_published or 1, THRESHOLDED,
        f"{clean_changed} across all objects; "
        f"{len(clean_samples)} before/after pairs carried in the inventory"))

    findings.append(_finding(
        "G24-HTML-TAG", "documentation values carrying a boundary tag "
                        "clean_html deletes without a separator",
        boundary_tag_values, doc_values, THRESHOLDED,
        ", ".join(sorted(set(html_tags) - HANDLED_HTML_TAGS
                         - INLINE_HTML_TAGS))
        + f"; {boundary_tag_published} of them on published objects"))
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
        inv["view_data_variables"] = NOT_MEASURED
        inv["view_data_keys"] = NOT_MEASURED
        inv["viewpoints"] = NOT_MEASURED
        inv["vp_legends"] = NOT_MEASURED
    else:
        variables: dict = {}
        vkeys: dict = {}
        # THREE states, never two. A key that does not appear in the file at
        # all cannot be reported as "zero declared" -- that conflates absence
        # of the field with absence of content, and this project has already
        # recorded a viewpoint count of zero produced entirely by requests
        # that failed. `present_empty` and `absent` must stay distinguishable.
        # The fields probed for content, taken from what run 33468063747
        # OBSERVED this file to declare, not from a reading of the orientation
        # map. `typeIconPath` was in this list and is not in this file at all
        # -- it lives on the object wrapper in the shards, where it is already
        # declared and consumed. So the gate demanded a field from the wrong
        # artefact and correctly reported that it could not measure it: the
        # mechanism was right and the premise was wrong, which failed the run
        # on my error rather than on anything in the source.
        #
        # A probe list copied from documentation tests the documentation. This
        # one is derived from the file.
        probe = {k: {"present_nonempty": 0, "present_empty": 0, "absent": 0}
                 for k in VIEW_DATA_FIELDS}
        for _vid, payload in view_data.items():
            for var, value in payload.items():
                _bump(variables, var)
                for k in _keys_of(value):
                    _bump(vkeys, k)
            # A field may sit at the top level of any variable in the file.
            for field, counter in probe.items():
                found = [value.get(field) for value in payload.values()
                         if isinstance(value, dict) and field in value]
                if field in payload:
                    found.append(payload[field])
                if not found:
                    counter["absent"] += 1
                elif any(v not in (None, "", [], {}) for v in found):
                    counter["present_nonempty"] += 1
                else:
                    counter["present_empty"] += 1

        inv["view_data_variables"] = variables
        inv["view_data_keys"] = vkeys
        inv["view_data_fields"] = probe
        inv["view_data_sample"] = len(view_data)
        # WHICH views were sampled. Run 33468063747 recorded that twelve were
        # drawn and not which twelve, so nothing about that sample could be
        # re-checked or reproduced -- a finding over an unidentifiable sample
        # is a number you have to take on trust.
        inv["view_data_sampled_ids"] = sorted(view_data, key=str)

        # RECONCILE the identifiers these files use against the ones we hold.
        #
        # Run 33468063747's sample carried 549 numeric keys, of which 44 were
        # known object ids, 19 known view ids and 504 matched nothing in an
        # extract holding 128,270 objects and 72,606 view memberships. So this
        # file is either presentation data in a per-diagram id space we have
        # never captured, or an identifier space with model content in it. The
        # difference matters and was established by hand once; a hand
        # measurement that is not a check does not survive the next landscape
        # version.
        known_objects = set(map(str, objects))
        known_views = set(map(str, landscape.insite_views))
        numeric = {k for k in vkeys if str(k).isdigit()}
        unknown = numeric - known_objects - known_views
        inv["view_data_ids"] = {
            "numeric_keys": len(numeric),
            "known_object_id": len(numeric & known_objects),
            "known_view_id": len(numeric & known_views),
            "unresolved": len(unknown),
        }
        findings.append(_finding(
            "G63-VIEWDATA-IDS",
            "identifiers in per-view files that resolve to nothing we hold",
            len(unknown), len(numeric) or 1, THRESHOLDED,
            "SAMPLE. Unresolved ids are not necessarily loss -- they are "
            "most likely per-diagram element ids -- but until that is "
            "established they are an identifier space of unknown content",
            sampled=True))

        # Nothing on the bulk path reads this file, so anything it carries is
        # unconsumed. Reported over the SAMPLE SIZE as its denominator, and
        # labelled a sample: two views were once generalised to 608 in this
        # project and were wrong by a factor of 40.
        findings.append(_finding(
            "G61-VIEWDATA-UNREAD",
            "sampled views whose data file carries content nothing reads",
            max(c["present_nonempty"] for c in probe.values()),
            len(view_data), THRESHOLDED,
            "SAMPLE, not a population. variables: "
            + ", ".join(sorted(variables)), sampled=True))

        # An absent field is NOT a measurement of its content. Raised as its
        # own finding so a run cannot quietly answer "are there viewpoints?"
        # with a number derived from a field it never saw.
        never_seen = sorted(f for f, c in probe.items()
                            if c["absent"] == len(view_data))
        if never_seen:
            findings.append(_finding(
                "G62-VIEWDATA-ABSENT",
                "documented per-view fields not present in any sampled file",
                None, len(view_data), FAIL_ALWAYS,
                ", ".join(never_seen) + " — NOT MEASURED: absent from the "
                "sample is not absent from the landscape, and the sample is "
                f"{len(view_data)} of {len(landscape.insite_views)} views"))

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
            # Sample-denominator findings are carried but never summed into
            # the population aggregate.
            if not f.get("sampled"):
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
