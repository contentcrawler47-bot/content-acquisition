#!/usr/bin/env python3
"""
Stage 1: the landscape as data, before anything decides how it should read.

The pipeline has always parsed and rendered in one pass. `views.parse_view`
returns a structure and `views.render` turns it into PlantUML, but nothing
keeps the structure: `pipeline` caches the rendered item and `write_bundles`
takes a body that is already markdown. The consequence is that a renderer
change or an allowlist change both cost a full re-fetch, and a second renderer
has nothing to render from.

This module writes the model down instead. It answers "what does BIAN
publish"; stage 2 answers "what do we publish, and how does it read".

Two properties matter and are worth stating because they are easy to erode:

**Nothing here applies the allowlist.** `is_wanted` is a stage 2 concern. An
extract that stored only the objects the current allowlist keeps would freeze
that allowlist into the data and defeat the point — adding a category would be
another 47-shard fetch rather than a re-render. Selection stays downstream.

**Nothing here renders.** No markdown, no PlantUML, no headings. If a field
exists only to make output look a certain way, it does not belong in the
extract.

Output is JSON-LD. The `@context` is embedded rather than referenced by URL so
the document is self-describing without a network fetch, and so there is no
second copy of the vocabulary to drift. Identifiers are URNs: stable across
runs, independent of `object_view` and of any page layout, and reusable as the
anchor targets stage 2 links to.

The document is validated against schema/bian-extract.schema.json by
tools/check_extract.py, which also runs the referential integrity checks that
a structural schema cannot express.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from bianlib import landscape as L
from bianlib import plan as P

#: Bumped when the shape of the document changes. Paired with the schema's
#: own version; stage 2 refuses an extract it does not understand. 1.9.0:
#: `extract.run` carries lineage -- commit_sha, repo_digest, raw_run_id,
#: raw_run_state -- naming the acquisition the extract was built from.
#: 1.10.0: `run` is the acquisition's provenance and nothing else; `producer`
#: names the run and commit that BUILT the extract; `fetched_at` is retitled
#: `built_at`, which is what it always measured; `captured_at` is the run's
#: capture time, taken from the run record and never from the clock.
SCHEMA_VERSION = "1.10.0"

#: The provenance fields a run or a build may carry into the extract, as the
#: schema lists them. `core.cli` reads the environment into a dict with these
#: keys plus diagnostics (`repo_digest_error`) that do not belong in the
#: artifact; both blocks are filtered through this set.
PROVENANCE_KEYS = ("where", "run_id", "run_attempt", "run_number", "workflow",
                   "repository", "url", "ref", "workflow_ref", "runner_os",
                   "commit_sha", "repo_digest", "manifest_digest")

#: Where `captured_at` comes from, stated in the artifact so a reader does
#: not have to know (I2.11: downstream timestamps derive from a named axis).
#: The run record's `finished_at`: the moment the capture was complete and the
#: bytes were what the extract describes. Per-artifact `fetched_at` values in
#: the run's manifest bound it from below.
CAPTURED_AT_DERIVATION = "run.json finished_at of the raw run"


def _provenance(block: dict | None, extra: tuple = ()) -> dict:
    """A provenance block as the schema allows it: known keys, and `where`.

    `core.cli` builds provenance dicts with diagnostic fields beside the
    recorded ones (`repo_digest_error`); those are for the log, not the
    artifact. Absent or empty means built outside CI and says so.
    """
    keep = set(PROVENANCE_KEYS) | set(extra)
    out = {k: v for k, v in (block or {}).items() if k in keep}
    out.setdefault("where", "local")
    return out

#: Bumped when parsing changes in a way that alters values for unchanged
#: upstream data. The render cache carries a renderer version for the same
#: reason: derived output must say what derived it.
PARSER_VERSION = "4"

#: Scopes. `model-only` reads the shards and index files and no view pages,
#: which is the whole landscape model in about a minute. `full` additionally
#: stores per-view geometry and is not implemented yet — see the module note
#: at the bottom of build().
MODES = ("model-only", "full")

VOCAB = "urn:bian:vocab:"


def _urn(source_id: str, kind: str, ident) -> str:
    return f"urn:bian:{source_id}:{kind}:{ident}"


def context() -> dict:
    """The JSON-LD context, embedded in every extract.

    Deliberately small and local. Mapping BIAN onto an external ontology is a
    separate question from making this document addressable, and answering the
    second does not require answering the first.
    """
    return {
        "@vocab": VOCAB,
        "id": "@id",
        "type": "@type",
        "name": f"{VOCAB}name",
        "category": f"{VOCAB}category",
        "notation": f"{VOCAB}notation",
        "documentation": f"{VOCAB}documentation",
        "objects": {"@id": f"{VOCAB}object", "@container": "@set"},
        "relations": {"@id": f"{VOCAB}relation", "@container": "@set"},
        "views": {"@id": f"{VOCAB}view", "@container": "@set"},
        "view_members": {"@id": f"{VOCAB}viewMember", "@container": "@set"},
        "models": {"@id": f"{VOCAB}model", "@container": "@set"},
        "categories": {"@id": f"{VOCAB}categoryDef", "@container": "@set"},
        "notations": {"@id": f"{VOCAB}notationDef", "@container": "@set"},
        "source": {"@id": f"{VOCAB}source", "@type": "@id"},
        "target": {"@id": f"{VOCAB}target", "@type": "@id"},
        "diagram_object": {"@id": f"{VOCAB}diagramObject", "@type": "@id"},
        "view": {"@id": f"{VOCAB}onView", "@type": "@id"},
        "object": {"@id": f"{VOCAB}ofObject", "@type": "@id"},
    }


# --- notation --------------------------------------------------------------

#: `typeIconPath` names the notation an object is drawn in. It sits on the
#: object WRAPPER, beside "data", not inside the first data entry. The first
#: version of this module read it one level too deep and resolved notation for
#: 0 of 128,270 objects on 29 August 2026 — which the checker reported as NOT
#: MEASURED rather than as an absent column, and that is how it was found.
#:
#: The shape is data/icons/<Notation>/<Type>.png, so the notation is the path
#: segment after "icons". Taken structurally rather than by matching substrings
#: of the filename: the probe run that measured the split (68,626 UML, 52,354
#: ArchiMate, 7,290 MM_ModelPackage, summing to every object) read it this way,
#: and a substring test would have produced roughly the right answer for the
#: wrong reason.
ICON_SEGMENT = "icons"


def _icon_path(obj: dict) -> str:
    """The icon path from an object wrapper, or "" if it is not there."""
    if not isinstance(obj, dict):
        return ""
    value = obj.get("typeIconPath")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def notation_of(icon_path: str) -> str:
    """Notation from an icon path, or "" when it cannot be decided.

    Returning "" rather than a default is deliberate: an unrecognised path and
    a known notation must not produce the same value, or the count of resolved
    notations stops meaning anything.
    """
    parts = [p for p in str(icon_path).split("/") if p]
    if ICON_SEGMENT in parts:
        i = parts.index(ICON_SEGMENT)
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


# --- building --------------------------------------------------------------

def _first_entry(obj) -> dict:
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def _merge_counts(dicts) -> dict:
    """Sum per-view counters into one.

    Deliberately NOT ordered by count: write() serialises with sort_keys=True
    because the content digest depends on stable ordering, so any order chosen
    here is discarded. Ordering for display is the reader's job -- see
    check_extract.py, which sorts by count when it prints.
    """
    total: dict = {}
    for d in dicts:
        for k, v in (d or {}).items():
            total[str(k)] = total.get(str(k), 0) + int(v)
    return dict(sorted(total.items(), key=lambda kv: (-kv[1], kv[0])))


def build(landscape: L.Landscape, source_id: str, mode: str = "model-only",
          insite_models=None, models_url: str = "",
          models_tried: list | None = None, geometry: dict | None = None,
          run: dict | None = None, gate: dict | None = None,
          producer: dict | None = None,
          captured_at: str | None = None) -> dict:
    """The extract document for a loaded landscape.

    Pure: takes a materialised model and returns a dict. No network, no disk,
    no environment. That is what makes it testable without reaching bian.org,
    and it is why loading stays in Landscape.load where it already was.

    `run` is the provenance of the ACQUISITION this extract was built from --
    the run that fetched the bytes, and the code that fetched them -- plus
    `raw_run_id` and `raw_run_state` naming it. `producer` is the provenance
    of THIS build: the run and commit that normalised those bytes. Since
    changeset 070 the two are different runs, days apart, and a reader of the
    artifact needs both to walk the lineage back. `captured_at` is the run's
    capture time, read from its record by the caller. All three are passed in
    rather than read from the environment or the clock -- reading them here
    would make this function environment-dependent and untestable, which is
    the property the paragraph above exists to protect -- and all three sit
    outside `content_digest` by construction, so two builds over identical
    content still agree.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    objects, categories, notations = [], {}, {}
    notation_missing = 0
    malformed = []
    with_properties = 0
    property_groups_skipped = 0

    for oid, obj in landscape.objects.items():
        entry = _first_entry(obj)
        if not entry:
            malformed.append(str(oid))
            continue
        category = landscape.categories.get(oid, "") or "Other"
        icon = _icon_path(obj)
        notation = notation_of(icon)
        if not notation:
            notation_missing += 1

        record = {
            "id": _urn(source_id, "object", oid),
            "type": "Object",
            "object_id": str(oid),
            "name": landscape.names.get(oid, ""),
            "category": category,
            "notation": notation or None,
        }
        if icon:
            record["type_icon_path"] = icon
        docs = L._documentation(entry)
        if docs:
            record["documentation"] = docs

        # Property tables, stored RAW and unfiltered.
        #
        # Raw because the shapes carry meaning that flattening destroys: a
        # `structure` is a record of named fields, and `/ 6. SO parameters` is
        # 45,263 of them holding the full parameter signature of every BIAN
        # service operation -- name, direction, type, multiplicity. Flattened
        # to text you can no longer tell which type belongs to which
        # parameter. Measured over 128,270 objects by probe run 90418066705:
        # 50,868 structure, 45,192 string, 31,528 object reference, 17,117
        # collection, 987 bool, 953 link, 363 rtf.
        #
        # Unfiltered because the extract is the unfiltered stage 1 record and
        # `select.py` is the only place the allowlist acts. Filtering here
        # would put a second filter in a second place, and the useful content
        # is owned by categories the allowlist excludes -- SO parameters hang
        # off UML `Operation` objects, which are not published.
        #
        # A group whose value is not a mapping is skipped and COUNTED, never
        # dropped in silence: render() has always skipped them, so if any
        # exist we would rather see the number than assume it is zero.
        props = {}
        for group, fields in L._properties(entry).items():
            if isinstance(fields, dict):
                props[str(group)] = fields
            else:
                property_groups_skipped += 1
        if props:
            record["properties"] = props
            with_properties += 1

        objects.append(record)

        categories[category] = categories.get(category, 0) + 1
        if notation:
            notations[notation] = notations.get(notation, 0) + 1

    # Relations. ArchiMate models these as objects too, but the edge list is
    # the authority for the graph; the objects representing edges are left in
    # `objects` unfiltered and dropped by stage 2 like anything else.
    relations = []
    for src, edges in landscape.relations.items():
        if not isinstance(edges, list):
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            via = (edge.get("via") or "").strip()
            if via in L.SKIP_RELATION_VERBS:
                continue
            for target in edge.get("to") or []:
                if not isinstance(target, (str, int)):
                    continue
                relations.append({
                    "id": _urn(source_id, "relation",
                               f"{src}-{via.replace(' ', '_')}-{target}"),
                    "type": "Relation",
                    "source": _urn(source_id, "object", src),
                    "target": _urn(source_id, "object", target),
                    "verb": via,
                })

    # Views. The kind is read from the diagram object's own category, never
    # inferred from what the view contains — inferring was tried and produced
    # over a thousand class views against a known 802.
    members = landscape.views_to_members()
    all_views = set(members) | {str(v) for v in landscape.insite_views}
    models_by_view = _models_index(insite_models)

    views, view_members = [], []
    unresolved_members = 0
    for vid in sorted(all_views):
        oids = members.get(vid, [])
        category = landscape.categories.get(vid, "")
        named = vid in landscape.categories
        record = {
            "id": _urn(source_id, "view", vid),
            "type": "View",
            "view_id": str(vid),
            "title": landscape.view_name(vid),
            "diagram_object": _urn(source_id, "object", vid) if named else None,
            "diagram_category": category or None,
            "kind": P.MODEL_KIND.get(category) or ("other" if named else "unnamed"),
            "model": models_by_view.get(str(vid)),
            "member_count": len(oids),
            "has_geometry": False,
        }
        views.append(record)
        for oid in oids:
            # A membership does not always point at an object. objectsOnViews
            # also carries diagram-to-diagram references: measured on 29 August
            # 2026, 4,956 of 127,588 memberships name a view. 4,571 of those
            # resolve as objects too, because a named view IS an object in this
            # model, and only the 385 views that are not objects showed up as
            # dangling. Classifying the target says what the data actually
            # holds, rather than tolerating a count of unresolved references.
            if oid in landscape.objects:
                target, kind = _urn(source_id, "object", oid), "object"
            elif oid in all_views:
                target, kind = _urn(source_id, "view", oid), "view"
            else:
                target, kind = _urn(source_id, "object", oid), "unresolved"
                unresolved_members += 1
            view_members.append({
                "type": "ViewMember",
                "view": _urn(source_id, "view", vid),
                "target": target,
                "target_type": kind,
            })

    models = _models_list(insite_models)
    geo_nodes, geo_edges = _geometry_records(source_id, geometry or {})
    with_geometry = set(geometry or {})
    for record in views:
        if record["view_id"] in with_geometry:
            record["has_geometry"] = True

    doc = {
        "@context": context(),
        "id": _urn(source_id, "extract", PARSER_VERSION),
        "type": "Extract",
        "extract": {
            "source_id": source_id,
            "base_url": landscape.base,
            # Two time axes, never conflated (I2.11). `built_at` is this
            # build's wall clock and describes the processing, not the
            # source. `captured_at` is when the run fetched the bytes, taken
            # from the run record; it is the time a projection may derive its
            # timestamps from, and it is null -- never the clock -- when the
            # run does not say. Until 1.10.0 the first was named `fetched_at`
            # and a design rested on it as capture time, which since 070 it
            # had not been.
            "built_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "captured_at": captured_at,
            "captured_at_derivation": CAPTURED_AT_DERIVATION,
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "mode": mode,
            "shards": list(landscape.shards),
            # Which acquisition this was built from: `raw_run_id` and
            # `raw_run_state` name the retained run, and the rest is that
            # run's own provenance -- the CI run and commit that FETCHED the
            # bytes. Without it there is no way back from a downloaded
            # artifact to the bytes that made it.
            "run": _provenance(run, extra=("raw_run_id", "raw_run_state")),
            # Which run and commit BUILT this extract from those bytes. The
            # cache key names the normalising commit, and until 1.10.0 the
            # extract could not confirm which one that was. "local" when
            # built outside CI, so a sandbox replay can never be mistaken for
            # a run -- a rehearsal has been recorded as a result here once
            # already.
            "producer": _provenance(producer),
        },
        "status": {
            # Named states, so "absent" and "not asked for" never look alike.
            "models": "present" if models_by_view else "not-fetched",
            "models_url": models_url,
            "models_tried": list(models_tried or []),
            "views_with_model": sum(1 for v in views if v["model"]),
            "geometry": "present" if geometry else "not-fetched",
            "views_with_geometry": len(geometry or {}),
            "geometry_unboxed": sum(g.get("unboxed", 0)
                                    for g in (geometry or {}).values()),
            "geometry_unboxed_concepts": _merge_counts(
                g.get("unboxed_concepts") for g in (geometry or {}).values()),
            "geometry_endless_edges": sum(g.get("endless_edges", 0)
                                          for g in (geometry or {}).values()),
            "geometry_endless_edge_concepts": _merge_counts(
                g.get("endless_edge_concepts")
                for g in (geometry or {}).values()),
            "notation_unresolved": notation_missing,
            "malformed_objects": len(malformed),
            "objects_with_properties": with_properties,
            "property_groups_skipped": property_groups_skipped,
            "unresolved_members": unresolved_members,
            # The source input gate. Carried in the extract rather than left
            # in the run log: a sub-threshold finding printed to a
            # world-readable log nobody reads is the same silent drop the gate
            # exists to remove, moved up one level. Here it travels with the
            # artifact and diffs between runs.
            #
            # Passed in, like `geometry` and `run`, because observing the
            # source needs requests and build() is pure. It sits under
            # `status` and so is outside `content_digest` by construction --
            # correct, because a gate finding is an observation about the run,
            # not a change in what BIAN published.
            "gate": dict(gate) if gate else {"ok": None,
                                             "detail": "not run"},
        },
        "notations": [{"type": "Notation", "name": n, "object_count": c}
                      for n, c in sorted(notations.items())],
        "categories": [{"type": "Category", "name": n, "object_count": c}
                       for n, c in sorted(categories.items())],
        "objects": objects,
        "relations": relations,
        "views": views,
        "view_members": view_members,
        "geometry_nodes": geo_nodes,
        "geometry_edges": geo_edges,
        "models": models,
    }
    if malformed:
        doc["status"]["malformed_object_ids"] = sorted(malformed)[:50]
    doc["content_digest"] = content_digest(doc)
    return doc


def _geometry_records(source_id: str, geometry: dict) -> tuple[list, list]:
    """Flatten per-view geometry into two addressable collections.

    Node and edge ids are the SVG's own `bizzid`, unique within a view but not
    across views, so each record carries its view and the pair is the key.
    `object_id` is what resolves into the model; `concept` is the shape that
    was drawn and is deliberately NOT a type -- 339 service domains on view
    54486 are drawn as `StrategyCapability`.
    """
    nodes, edges = [], []
    for vid, g in sorted(geometry.items()):
        view_urn = _urn(source_id, "view", vid)
        for n in g.get("nodes", []):
            nodes.append({
                "type": "GeometryNode",
                "view": view_urn,
                "node_id": n["node_id"],
                "object": (_urn(source_id, "object", n["object_id"])
                           if n.get("object_id") else None),
                "concept": n["concept"],
                "label": n.get("label") or "",
                "x": round(float(n["x"]), 2), "y": round(float(n["y"]), 2),
                "w": round(float(n["w"]), 2), "h": round(float(n["h"]), 2),
                "parent_id": n.get("parent_id"),
            })
        for e in g.get("edges", []):
            edges.append({
                "type": "GeometryEdge",
                "view": view_urn,
                "edge_id": e["edge_id"],
                "object": (_urn(source_id, "object", e["object_id"])
                           if e.get("object_id") else None),
                "concept": e["concept"],
                "label": e.get("label") or "",
                "from_node": e.get("from_node"),
                "to_node": e.get("to_node"),
            })
    return nodes, edges


def _models_index(entries) -> dict:
    """{viewId: modelName} from the insite_models entries.

    The shape is a LIST of model entries, each carrying `name` and a `views`
    list of objects with an `id` — not a mapping of name to view ids. An
    earlier version of this module assumed the latter and was never exercised,
    because models were not fetched until changeset 033. Read from the probe
    run that actually parsed the file.
    """
    out = {}
    for entry in L._l(entries):
        name = L._d(entry).get("name") or "(unnamed model)"
        for view in L._l(L._d(entry).get("views")):
            vid = L._d(view).get("id")
            if vid is not None:
                out[str(vid)] = name
    return out


def _models_list(entries) -> list:
    """One record per named model, with the number of views it groups."""
    out = []
    for entry in L._l(entries):
        name = L._d(entry).get("name") or "(unnamed model)"
        out.append({
            "type": "Model",
            "name": name,
            "view_count": len(L._l(L._d(entry).get("views"))),
        })
    return sorted(out, key=lambda m: m["name"])


# --- partitioning and serialising ------------------------------------------

#: Each bulk collection is written as a set of partitions rather than one file.
#: Measured on 29 August 2026 the extract is 67.3 MB, of which objects alone is
#: 32.3 MB — a size nothing can read and stage 2 would have to load whole.
#:
#: Partitioning is by RANGE over an integer key, with boundaries cut at equal
#: RANK. Cutting by rank rather than by value means no assumption is made about
#: how the ids are distributed: on this data they span 28,702 to 745,473 at
#: 17.9% density, and equal-value slices would be badly uneven. Equal rank is
#: even by construction whatever the distribution.
#:
#: Boundaries are published in the index, so the partition function is a lookup
#: against the document rather than a computation a reader has to reimplement.
#: That is also what keeps repartitioning local: splitting one boundary rewrites
#: one file, where changing a modulus would remap everything.
#:
#: Boundaries are recomputed each run rather than frozen. Freezing would need
#: them stored either in the repo, which is public and must hold no harvested
#: data, or carried from a prior extract, which makes a run depend on an earlier
#: one. Recomputing is safe because identity is location-free: nothing anywhere
#: references a partition, so an object landing elsewhere next run breaks
#: nothing. The cost is that partition digests are not comparable across runs.
PARTS = ("objects", "relations", "views", "view_members",
         "geometry_nodes", "geometry_edges")

#: The integer each part is partitioned on. Objects and their relations share
#: the object id, so a relation sits in the partition matching its source.
PART_KEY = {
    "objects": lambda i: i["object_id"],
    "relations": lambda i: i["source"].rsplit(":", 1)[1],
    "views": lambda i: i["view_id"],
    "view_members": lambda i: i["view"].rsplit(":", 1)[1],
    "geometry_nodes": lambda i: i["view"].rsplit(":", 1)[1],
    "geometry_edges": lambda i: i["view"].rsplit(":", 1)[1],
}

PART_SLUG = {
    "objects": "objects",
    "relations": "relations",
    "views": "views",
    "view_members": "view-members",
    "geometry_nodes": "geometry-nodes",
    "geometry_edges": "geometry-edges",
}

#: Items per partition. At the measured mean sizes this puts objects near
#: 264 KB and the others lower — small enough to load one at a time, large
#: enough that the index stays a few hundred rows rather than thousands.
PARTITION_ITEMS = 1000

INDEX_FILE = "extract.jsonld"

#: Sidecar listing the sha256 of every file written, including the index.
#: Byte digests live here and content digests live in the index, because they
#: answer different questions: "did the file change" and "did the content
#: change". A digest written into the file it describes changes the thing it
#: describes, so this stays outside.
SIDECAR_FILE = "EXTRACT.sha256"

#: Small enough to keep inline in the index, and wanted by anything reading it
#: for orientation rather than for bulk.
INLINE = ("categories", "notations", "models")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def part_digest(items: list) -> str:
    """Content digest of one collection, independent of how it is laid out.

    Items are canonicalised and sorted before hashing, so the digest identifies
    the SET of items and not the order they happened to be built in.
    Partitioning reorders by key, so an order-sensitive digest would change on
    a round trip through write() and read() even though nothing about the
    content had changed - which is precisely the question this digest exists
    to answer.
    """
    return _sha256(_canonical(sorted(_canonical(i) for i in items)))


def _key(name: str, item: dict) -> int:
    """The partition key of an item, as an integer.

    Non-numeric keys sort to the end rather than raising: an id that is not a
    number is a fact about the source, not a reason to lose the run, and it
    surfaces as an out-of-range key in check_extract.
    """
    raw = PART_KEY[name](item)
    return int(raw) if str(raw).isdigit() else -1


def partition(name: str, items: list, per: int = PARTITION_ITEMS) -> list:
    """Split items into range partitions with boundaries cut at equal rank.

    A partition boundary never falls inside a key. The key is not unique for
    every collection - one object has many relations, and one view has up to
    964 members - so cutting purely by rank would put items sharing a key on
    both sides of a boundary, and each would then sit outside the range its own
    partition declares. Grouping by key first and packing whole groups keeps
    every lookup resolving to exactly one file, at the cost of partitions
    varying in size when a single key is large.

    Returns [{index, min_key, max_key, items}], contiguous and non-overlapping.
    """
    if not items:
        return [{"index": 0, "min_key": 0, "max_key": 0, "items": []}]

    groups: dict = {}
    for item in items:
        groups.setdefault(_key(name, item), []).append(item)
    ordered = sorted(groups.items())

    packs, current = [], []
    for key, members in ordered:
        if current and len(current) + len(members) > per:
            packs.append(current)
            current = []
        current.extend(sorted(members, key=_canonical))
    if current or not packs:
        packs.append(current)

    out = []
    for n, chunk in enumerate(packs):
        keys = [_key(name, i) for i in chunk] or [0]
        out.append({"index": n, "min_key": min(keys), "max_key": max(keys),
                    "items": chunk})
    # Make the ranges contiguous so a lookup can never fall down a gap. Safe
    # because no key spans two partitions.
    for a, b in zip(out, out[1:]):
        a["max_key"] = b["min_key"] - 1
    out[-1]["max_key"] = max(_key(name, i) for i in items)
    return out


def partition_file(name: str, index: int) -> str:
    return f"{PART_SLUG[name]}-{index:04d}.jsonld"


def locate(index_doc: dict, name: str, key) -> str:
    """The partition function: object reference -> partition file.

    A lookup against the boundaries the index publishes, not a computation.
    Exported so tools and stage 2 resolve placement one way rather than each
    growing its own copy of the rule.
    """
    k = int(key) if str(key).isdigit() else -1
    for meta in index_doc.get("parts", []):
        if meta.get("part") != name:
            continue
        for p in meta.get("partitions", []):
            if p["min_key"] <= k <= p["max_key"]:
                return p["file"]
    return ""


def content_digest(doc: dict) -> str:
    """Digest over the content, excluding run metadata.

    Taken over each collection whole rather than over its partitions, so it is
    stable against a change in PARTITION_ITEMS: refiling the same objects into
    different partitions is not a change in content.
    """
    payload = {"parts": {name: part_digest(doc.get(name, []) or [])
                         for name in PARTS}}
    for name in INLINE:
        payload[name] = doc.get(name, [])
    return _sha256(_canonical(payload))


def _part_text(doc_id: str, name: str, part: dict) -> str:
    """One partition file: a JSON-LD document with one item per line.

    Not pretty-printed — at this scale indentation is megabytes — but not a
    single line either, because a file nobody can read or grep is a file nobody
    will check.
    """
    items = part["items"]
    header = {
        "@context": context(),
        "id": f"{doc_id}:part:{name}:{part['index']:04d}",
        "type": "ExtractPart",
        "part": name,
        "partition": part["index"],
        "min_key": part["min_key"],
        "max_key": part["max_key"],
        "extract": doc_id,
        "count": len(items),
        "content_digest": part_digest(items),
    }
    fields = ",\n ".join(
        f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False, sort_keys=True)}"
        for k, v in sorted(header.items()))
    body = ("[\n  " + ",\n  ".join(_canonical(i) for i in items) + "\n ]"
            if items else "[]")
    return "{\n " + fields + ",\n " + json.dumps(name) + ": " + body + "\n}\n"


def write(doc: dict, outdir, per: int = PARTITION_ITEMS) -> dict:
    """Write the index and every partition. Returns a summary."""
    outdir.mkdir(parents=True, exist_ok=True)
    doc_id = doc.get("id", "")
    written, parts_meta, total_bytes = {}, [], 0

    for name in PARTS:
        items = doc.get(name, []) or []
        partitions = partition(name, items, per)
        pmeta = []
        for p in partitions:
            fname = partition_file(name, p["index"])
            path = outdir / fname
            text = _part_text(doc_id, name, p)
            path.write_text(text, encoding="utf-8")
            nbytes = len(path.read_bytes())
            written[fname] = _sha256(text)
            total_bytes += nbytes
            pmeta.append({
                "file": fname,
                "partition": p["index"],
                "min_key": p["min_key"],
                "max_key": p["max_key"],
                "count": len(p["items"]),
                "content_digest": part_digest(p["items"]),
                "bytes": nbytes,
            })
        parts_meta.append({
            "part": name,
            "count": len(items),
            "content_digest": part_digest(items),
            "partitions": pmeta,
        })

    index = {k: v for k, v in doc.items() if k not in PARTS}
    index["parts"] = parts_meta
    index_text = json.dumps(index, ensure_ascii=False, indent=1,
                            sort_keys=True) + "\n"
    (outdir / INDEX_FILE).write_text(index_text, encoding="utf-8")
    written[INDEX_FILE] = _sha256(index_text)
    total_bytes += len((outdir / INDEX_FILE).read_bytes())

    (outdir / SIDECAR_FILE).write_text(
        "".join(f"{written[f]}  {f}\n" for f in sorted(written)),
        encoding="utf-8")

    return {
        "dir": str(outdir),
        "bytes": total_bytes,
        "content_digest": doc.get("content_digest", ""),
        "files": len(written),
        "parts": {m["part"]: m["count"] for m in parts_meta},
        "partitions": {m["part"]: len(m["partitions"]) for m in parts_meta},
        "part_bytes": {m["part"]: sum(p["bytes"] for p in m["partitions"])
                       for m in parts_meta},
    }


class ExtractUnreadable(Exception):
    """The extract cannot be consumed: never finished writing, a file does
    not match its sidecar line, or it is not the extract the caller asked
    for. The stage-3 counterpart of `acquire.RunUnreadable`."""


def verify(outdir, expect_digest: str | None = None,
           expect_raw_run_id: str | None = None) -> dict:
    """Verify a stored extract before anything reads it. Raises on failure.

    Three checks, in the order a consumer needs them (S.6): the sidecar is
    present -- it is written last, so its absence means the write never
    finished; every file it lists is present with the recorded digest, and no
    `.jsonld` is present that it does not list; and, when the caller says
    which extract it wants, the index declares that identity -- its
    `content_digest` (Regenerate proving an equality) or its
    `extract.run.raw_run_id` (Render asked for the extract of one run; the
    cache key named it, and the restored object must agree). The content
    digest is NOT recomputed from the parts here: that is the producer's
    checker's job before the extract is handed on, and repeating it on every
    restore would re-read every part to learn what the sidecar plus the
    declared digest already establish.

    Returns counts, so a caller can print what was verified. This used to be
    a `sha256sum -c` step in the Render workflow, where a second consumer
    could forget it; here every consumer gets it by reading.
    """
    outdir = Path(outdir)
    sidecar = outdir / SIDECAR_FILE
    if not sidecar.is_file():
        raise ExtractUnreadable(
            f"{outdir} has no {SIDECAR_FILE}: the extract never finished "
            f"writing and is not evidence")
    listed = {}
    for line in sidecar.read_text(encoding="utf-8").splitlines():
        if "  " in line:
            digest, fname = line.split("  ", 1)
            listed[fname] = digest
    if INDEX_FILE not in listed:
        raise ExtractUnreadable(f"{SIDECAR_FILE} does not list {INDEX_FILE}")
    absent, differs = [], []
    for fname, want in listed.items():
        path = outdir / fname
        if not path.is_file():
            absent.append(fname)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            differs.append(fname)
    stray = sorted(p.name for p in outdir.glob("*.jsonld")
                   if p.name not in listed)
    if absent or differs or stray:
        raise ExtractUnreadable(
            f"{outdir} does not match its {SIDECAR_FILE}: "
            f"{len(absent)} listed but absent, {len(differs)} differ, "
            f"{len(stray)} present but unlisted"
            + (f" (first: {(absent + differs + stray)[0]})"))
    declared, raw_run_id = "", ""
    if expect_digest or expect_raw_run_id:
        index = json.loads((outdir / INDEX_FILE).read_text(encoding="utf-8"))
        declared = str(index.get("content_digest", ""))
        raw_run_id = str(((index.get("extract") or {}).get("run") or {})
                         .get("raw_run_id") or "")
    if expect_digest and not declared.startswith(expect_digest):
        raise ExtractUnreadable(
            f"{outdir} is extract {declared[:16] or '(undeclared)'}, "
            f"not the {expect_digest[:16]} that was asked for")
    if expect_raw_run_id and raw_run_id != expect_raw_run_id:
        raise ExtractUnreadable(
            f"{outdir} was built from run {raw_run_id or '(unrecorded)'}, "
            f"not the {expect_raw_run_id} that was asked for")
    return {"files": len(listed), "content_digest": declared,
            "raw_run_id": raw_run_id}


def read(outdir, verify_first: bool = True,
         expect_digest: str | None = None,
         expect_raw_run_id: str | None = None) -> dict:
    """Load a partitioned extract back into one document.

    The inverse of write(), used by stage 2 and by tools/check_extract.py so
    that both read the extract the same way rather than each growing its own
    idea of the layout. Verifies against the sidecar first (see `verify`)
    unless told not to -- the checker turns that off so it can REPORT a bad
    sidecar as a finding rather than stop at it; a consumer never should.
    """
    outdir = Path(outdir)
    if verify_first:
        verify(outdir, expect_digest, expect_raw_run_id)
    index = json.loads((outdir / INDEX_FILE).read_text(encoding="utf-8"))
    doc = {k: v for k, v in index.items() if k != "parts"}
    for meta in index.get("parts", []):
        items = []
        for p in meta.get("partitions", []):
            payload = json.loads(
                (outdir / p["file"]).read_text(encoding="utf-8"))
            items.extend(payload.get(meta["part"], []))
        doc[meta["part"]] = items
    return doc
