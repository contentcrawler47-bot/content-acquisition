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

from bianlib import landscape as L
from bianlib import plan as P

#: Bumped when the shape of the document changes. Paired with the schema's
#: own version; stage 2 refuses an extract it does not understand.
SCHEMA_VERSION = "1.0.0"

#: Bumped when parsing changes in a way that alters values for unchanged
#: upstream data. The render cache carries a renderer version for the same
#: reason: derived output must say what derived it.
PARSER_VERSION = "1"

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

#: `typeIconPath` names the notation an object is drawn in — UML, ArchiMate or
#: a model package. It is read defensively: the field has been measured as
#: present on every object, but this code has never itself run against a live
#: shard, so a miss is counted and reported rather than guessed at. A run that
#: resolves notation for zero objects is a loud failure in check_extract, not
#: a silent column of nulls.
ICON_HINTS = (
    ("archimate", "ArchiMate"),
    ("uml_", "UML"),
    ("uml/", "UML"),
    ("mm_modelpackage", "MM_ModelPackage"),
    ("modelpackage", "MM_ModelPackage"),
)


def _icon_path(entry: dict) -> str:
    for key in ("typeIconPath", "typeIcon", "iconPath"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def notation_of(icon_path: str) -> str:
    """Notation from an icon path, or "" when it cannot be decided.

    Returning "" rather than a default is deliberate: an unrecognised icon and
    a known notation must not produce the same value, or the count of resolved
    notations stops meaning anything.
    """
    low = icon_path.lower()
    for needle, name in ICON_HINTS:
        if needle in low:
            return name
    return ""


# --- building --------------------------------------------------------------

def _first_entry(obj) -> dict:
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def build(landscape: L.Landscape, source_id: str, mode: str = "model-only",
          insite_models: dict | None = None) -> dict:
    """The extract document for a loaded landscape.

    Pure: takes a materialised model and returns a dict. No network, no disk,
    no environment. That is what makes it testable without reaching bian.org,
    and it is why loading stays in Landscape.load where it already was.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    objects, categories, notations = [], {}, {}
    notation_missing = 0
    malformed = []

    for oid, obj in landscape.objects.items():
        entry = _first_entry(obj)
        if not entry:
            malformed.append(str(oid))
            continue
        category = landscape.categories.get(oid, "") or "Other"
        icon = _icon_path(entry)
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
            view_members.append({
                "type": "ViewMember",
                "view": _urn(source_id, "view", vid),
                "object": _urn(source_id, "object", oid),
            })

    models = _models_list(insite_models)

    doc = {
        "@context": context(),
        "id": _urn(source_id, "extract", PARSER_VERSION),
        "type": "Extract",
        "extract": {
            "source_id": source_id,
            "base_url": landscape.base,
            "fetched_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "mode": mode,
            "shards": list(landscape.shards),
        },
        "status": {
            # Named states, so "absent" and "not asked for" never look alike.
            "models": "present" if insite_models else "not-fetched",
            "geometry": "not-fetched",
            "notation_unresolved": notation_missing,
            "malformed_objects": len(malformed),
        },
        "notations": [{"type": "Notation", "name": n, "object_count": c}
                      for n, c in sorted(notations.items())],
        "categories": [{"type": "Category", "name": n, "object_count": c}
                       for n, c in sorted(categories.items())],
        "objects": objects,
        "relations": relations,
        "views": views,
        "view_members": view_members,
        "models": models,
    }
    if malformed:
        doc["status"]["malformed_object_ids"] = sorted(malformed)[:50]
    doc["content_digest"] = content_digest(doc)
    return doc


def _models_index(insite_models) -> dict:
    """{viewId: modelName} from models_data.js, when it has been fetched.

    Not wired into the fetch path yet. The probe located this file by trying
    several candidate paths, and no run recorded here has exercised the one
    that answered, so hardcoding a URL would be quoting a path as known
    without the run that proves it. build() accepts the parsed structure so
    that wiring it later is one call, not a change to this module.
    """
    if not isinstance(insite_models, dict):
        return {}
    out = {}
    for name, entry in insite_models.items():
        if not isinstance(entry, dict):
            continue
        for vid in entry.get("views") or []:
            out[str(vid)] = name
    return out


def _models_list(insite_models) -> list:
    if not isinstance(insite_models, dict):
        return []
    out = []
    for name, entry in sorted(insite_models.items()):
        views = entry.get("views") or [] if isinstance(entry, dict) else []
        out.append({"type": "Model", "name": name, "view_count": len(views)})
    return out


# --- serialising -----------------------------------------------------------

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def content_digest(doc: dict) -> str:
    """Digest over the content, excluding run metadata.

    `fetched_at` changes every run, so a digest over the whole document could
    never answer "did anything actually change". This one can, and it is what
    makes the determinism check in stage 2 meaningful.
    """
    payload = {k: v for k, v in doc.items()
               if k in ("objects", "relations", "views", "view_members",
                        "categories", "notations", "models")}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def write(doc: dict, path) -> dict:
    """Write the extract and its sidecar digest. Returns a summary.

    The digest lives beside the file, never inside it: a digest written into
    the document it describes changes the thing it describes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    raw = path.read_bytes()
    file_digest = hashlib.sha256(raw).hexdigest()
    (path.parent / (path.name + ".sha256")).write_text(
        f"{file_digest}  {path.name}\n", encoding="utf-8")
    return {
        "path": str(path),
        "bytes": len(raw),
        "file_digest": file_digest,
        "content_digest": doc.get("content_digest", ""),
        "objects": len(doc.get("objects", [])),
        "relations": len(doc.get("relations", [])),
        "views": len(doc.get("views", [])),
        "view_members": len(doc.get("view_members", [])),
    }
