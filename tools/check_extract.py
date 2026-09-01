#!/usr/bin/env python3
"""
Check a stage 1 extract: structure, then referential integrity.

    python3 tools/check_extract.py out/_extract/bian-v14
    python3 tools/check_extract.py out/_extract/bian-v14 --require-schema
    python3 tools/check_extract.py out/_extract/bian-v14 --canary-id 34300 \
        --canary-name "Consumer Loan" --min-objects 100000

Takes the extract DIRECTORY. The extract is an index plus one file per bulk
collection, and they have to be read together — pointing this at one part
file would validate a quarter of the graph and say nothing about the rest.

Two kinds of check, because they catch different things.

**Structure** is JSON Schema, against schema/bian-extract.schema.json. JSON-LD
is JSON, so the schema validates the document directly with no RDF processor
involved. This is the contract between stage 1 and stage 2: it catches a field
renamed on one side of the boundary at the boundary, rather than three steps
later as an empty diagram.

**Referential integrity** is Python, and it is the half that catches what has
actually gone wrong on this project. A schema can say `diagram_object` is a
string matching a pattern; it cannot say the object it names is present. Each
of these reports a count against its denominator rather than a boolean,
because a bare "conforms: false" would not have caught a 0% join that looked
like upstream drift, and "1,900 of 2,285, expected 1,900" does.

`jsonschema` is not in the standard library and is not yet installed by any
workflow. When it is absent the structural check is reported as a SKIP and
named in the summary — never silently passed. Pass --require-schema to make
its absence a failure, which is what CI should do once the dependency lands.

Exit codes: 0 all checks pass, 1 a check failed, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bianlib import landscape as L  # noqa: E402

SCHEMA_PATH = REPO / "schema" / "bian-extract.schema.json"

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Result:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = ""):
        self.rows.append((status, name, detail))

    def count(self, resolved: int, total: int, name: str,
              expected: int | None = None, allow_unresolved: int = 0):
        """A check that states its denominator.

        `expected` is the number that should resolve when the number that
        cannot is itself known and legitimate — 385 views are not objects in
        the model, so 1,900 of 2,285 is the correct answer, not a shortfall.
        """
        unresolved = total - resolved
        detail = f"{resolved} of {total} resolved"
        if unresolved:
            detail += f", {unresolved} unresolved"
        if total == 0:
            self.add(WARN, name, "nothing to check (denominator is 0)")
        elif expected is not None and resolved != expected:
            self.add(FAIL, name, detail + f", expected {expected}")
        elif expected is None and unresolved > allow_unresolved:
            self.add(FAIL, name, detail
                     + f", allowed {allow_unresolved}")
        else:
            self.add(PASS, name, detail)

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)


def load(outdir: Path) -> tuple[dict, dict]:
    """The merged document, and the raw file contents keyed by filename.

    Reading goes through bianlib.extract.read so this tool and stage 2 cannot
    grow two different ideas of the layout.
    """
    from bianlib import extract as E
    doc = E.read(outdir)
    raw = {}
    index = json.loads(
        (outdir / E.INDEX_FILE).read_text(encoding="utf-8"))
    raw[E.INDEX_FILE] = index
    for meta in index.get("parts", []):
        for p in meta.get("partitions", []):
            raw[p["file"]] = json.loads(
                (outdir / p["file"]).read_text(encoding="utf-8"))
    return doc, raw


def check_parts(outdir: Path, raw: dict, result: Result) -> None:
    """The index, the partitions and the files on disk must all agree.

    Counts and digests are declared in three places by design - the index says
    what each collection and each partition should hold, and each partition
    says what it holds. That is only useful if something compares them.
    """
    from bianlib import extract as E

    index = raw.get(E.INDEX_FILE, {})
    declared = {m["part"]: m for m in index.get("parts", [])}
    missing = [p for p in E.PARTS if p not in declared]
    if missing:
        result.add(FAIL, "index declares every part",
                   f"missing: {', '.join(missing)}")
    else:
        result.add(PASS, "index declares every part",
                   f"{len(declared)} of {len(E.PARTS)}")

    agree = 0
    total_parts = 0
    for name, meta in sorted(declared.items()):
        parts = meta.get("partitions", [])
        total_parts += len(parts)
        seen = []
        for p in parts:
            payload = raw.get(p["file"], {})
            items = payload.get(name, [])
            seen.extend(items)
            if not (len(items) == p["count"] == payload.get("count")):
                result.add(FAIL, f"partition {p['file']} count agrees",
                           f"index {p['count']}, header "
                           f"{payload.get('count')}, actual {len(items)}")
                continue
            if E.part_digest(items) != p["content_digest"]:
                result.add(FAIL, f"partition {p['file']} digest reproduces",
                           f"recomputed {E.part_digest(items)[:16]} != "
                           f"{p['content_digest'][:16]}")
                continue
            agree += 1
        if len(seen) != meta["count"]:
            result.add(FAIL, f"{name} partitions hold the whole collection",
                       f"{len(seen)} across partitions, index says "
                       f"{meta['count']}")
    result.count(agree, total_parts,
                 "partitions agree with the index", expected=total_parts)

    # Boundaries must tile the key space: sorted, contiguous, no overlap, and
    # every item inside the range its own partition declares. A key falling in
    # a gap would be unreachable through the partition function even though it
    # is present in the file.
    ok_bounds = ok_keys = total_keys = 0
    for name, meta in sorted(declared.items()):
        parts = sorted(meta.get("partitions", []), key=lambda p: p["partition"])
        contiguous = all(a["max_key"] + 1 == b["min_key"]
                         for a, b in zip(parts, parts[1:]))
        ordered = all(p["min_key"] <= p["max_key"] for p in parts)
        if contiguous and ordered:
            ok_bounds += 1
        else:
            result.add(FAIL, f"{name} boundaries tile the key space",
                       "partitions overlap or leave a gap")
        for p in parts:
            for item in raw.get(p["file"], {}).get(name, []):
                total_keys += 1
                if p["min_key"] <= E._key(name, item) <= p["max_key"]:
                    ok_keys += 1
    result.count(ok_bounds, len(declared),
                 "boundaries tile the key space", expected=len(declared))
    result.count(ok_keys, total_keys, "every item sits in its own partition",
                 expected=total_keys)

    # The partition function has to actually find things. Resolving a sample
    # through locate() tests the published boundaries the way a reader would,
    # rather than trusting that they were written correctly.
    found = tried = 0
    for name, meta in sorted(declared.items()):
        for p in meta.get("partitions", [])[:5]:
            items = raw.get(p["file"], {}).get(name, [])
            if not items:
                continue
            tried += 1
            if E.locate(index, name, E._key(name, items[0])) == p["file"]:
                found += 1
    result.count(found, tried, "partition function resolves to the right file",
                 expected=tried)

    sidecar = outdir / E.SIDECAR_FILE
    if not sidecar.is_file():
        result.add(FAIL, "sidecar digests present", f"{E.SIDECAR_FILE} missing")
        return
    import hashlib
    lines = [l.split("  ", 1) for l in
             sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = 0
    for want, fname in lines:
        path = outdir / fname
        if not path.is_file():
            result.add(FAIL, "sidecar digests match", f"{fname} missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() == want:
            ok += 1
        else:
            result.add(FAIL, "sidecar digests match", f"{fname} differs")
    result.count(ok, len(lines), "sidecar file digests match",
                 expected=len(lines))


def check_schema(raw: dict, result: Result, require: bool) -> None:
    """Validate the index and every part file against the same schema.

    The schema branches on `type`, so one file covers both shapes and a part
    cannot be validated as an index by accident.
    """
    try:
        import jsonschema
    except ImportError:
        status = FAIL if require else SKIP
        result.add(status, "structure matches JSON Schema",
                   "jsonschema is not installed"
                   + ("" if require else " (pass --require-schema to enforce)"))
        return
    if not SCHEMA_PATH.is_file():
        result.add(FAIL, "structure matches JSON Schema",
                   f"schema not found at {SCHEMA_PATH}")
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    clean, shown = 0, 0
    for fname, payload in sorted(raw.items()):
        errors = sorted(validator.iter_errors(payload),
                        key=lambda e: list(e.path))
        if not errors:
            clean += 1
            continue
        # Name the first few precisely; a hundred identical errors from one
        # bad field is one problem, not a hundred.
        for err in errors[:3]:
            where = "/".join(str(p) for p in err.path) or "(root)"
            result.add(FAIL, "structure matches JSON Schema",
                       f"{fname} {where}: {err.message[:140]}")
            shown += 1
        if len(errors) > 3:
            result.add(FAIL, "structure matches JSON Schema",
                       f"{fname}: and {len(errors) - 3} further errors")
    if clean == len(raw):
        result.count(clean, len(raw), "structure matches JSON Schema",
                     expected=len(raw))


def check_integrity(doc: dict, result: Result, args) -> None:
    objects = doc.get("objects", [])
    views = doc.get("views", [])
    members = doc.get("view_members", [])
    relations = doc.get("relations", [])

    object_ids = {o.get("id") for o in objects}
    view_ids = {v.get("id") for v in views}
    category_names = {c.get("name") for c in doc.get("categories", [])}
    notation_names = {n.get("name") for n in doc.get("notations", [])}
    model_names = {m.get("name") for m in doc.get("models", [])}

    # Identity. Anything keyed by name collapses duplicates silently, so
    # objects and distinct names are reported separately.
    result.count(len(object_ids), len(objects), "object ids are unique",
                 expected=len(objects))
    distinct_names = len({o.get("name") for o in objects})
    result.add(PASS, "objects and distinct names",
               f"{len(objects)} objects, {distinct_names} distinct names")

    # Views to diagram objects. The views that do not resolve are the views
    # that are not objects in the model, and that number is known.
    named = [v for v in views if v.get("diagram_object")]
    resolved = sum(1 for v in named if v["diagram_object"] in object_ids)
    result.count(resolved, len(named),
                 "every named view resolves to an object",
                 expected=len(named))
    unnamed = len(views) - len(named)
    result.add(PASS, "views without a diagram object",
               f"{unnamed} of {len(views)} are not objects in the model")

    # Membership. Split by what the target is, because objectsOnViews holds
    # two different relationships and one assertion over both could only ever
    # be answered with an allowance. A view drawn on another view is a
    # legitimate diagram-to-diagram reference, not a dangling object.
    ok = sum(1 for m in members if m.get("view") in view_ids)
    result.count(ok, len(members), "every membership names a known view",
                 expected=len(members))

    by_kind = {}
    for m in members:
        by_kind.setdefault(m.get("target_type"), []).append(m)

    to_obj = by_kind.get("object", [])
    ok = sum(1 for m in to_obj if m.get("target") in object_ids)
    result.count(ok, len(to_obj), "object memberships resolve to an object",
                 expected=len(to_obj))

    to_view = by_kind.get("view", [])
    ok = sum(1 for m in to_view if m.get("target") in view_ids)
    result.count(ok, len(to_view), "view references resolve to a view",
                 expected=len(to_view))

    # What is left is genuinely dangling: ids objectsOnViews names that exist
    # neither as an object nor as a view. Measured at 15 of 127,588 on
    # 29 August 2026 — the same class of upstream inconsistency as the shard
    # mapping listing fewer ids than the shards hold. Bounded rather than
    # demanded to be zero, and the bound is there to catch the number moving,
    # not to hide it.
    dangling = len(by_kind.get("unresolved", []))
    status = PASS if dangling <= args.allow_unresolved_members else FAIL
    result.add(status, "memberships resolving to nothing",
               f"{dangling} of {len(members)}, allowed "
               f"{args.allow_unresolved_members}")

    # Relations, both ends. One unresolved target is known and stable here —
    # a relation whose target name contains a newline — so a small allowance
    # is expressed as a number rather than by weakening the check.
    ok = sum(1 for r in relations
             if r.get("source") in object_ids and r.get("target") in object_ids)
    result.count(ok, len(relations), "every relation resolves at both ends",
                 allow_unresolved=args.allow_unresolved_relations)

    # Vocabularies close over the objects that use them.
    ok = sum(1 for o in objects if o.get("category") in category_names)
    result.count(ok, len(objects), "every object category is declared",
                 expected=len(objects))
    with_notation = [o for o in objects if o.get("notation")]
    ok = sum(1 for o in with_notation if o["notation"] in notation_names)
    result.count(ok, len(with_notation), "every object notation is declared",
                 expected=len(with_notation))

    # Notation coverage. A zero here is a failed measurement, not a
    # measurement: it means the icon field was not found, and the notation
    # column would be meaningless rather than empty.
    if not objects:
        result.add(WARN, "notation resolved for objects", "no objects")
    elif not with_notation:
        result.add(FAIL, "notation resolved for objects",
                   f"0 of {len(objects)} — typeIconPath was not found on any "
                   f"object, so notation is NOT MEASURED rather than absent")
    else:
        share = 100.0 * len(with_notation) / len(objects)
        status = PASS if share >= args.min_notation_share else FAIL
        result.add(status, "notation resolved for objects",
                   f"{len(with_notation)} of {len(objects)} ({share:.1f}%), "
                   f"floor {args.min_notation_share:.0f}%")

    # Models. The index is the only published statement of a view's purpose,
    # so its absence is a failure rather than a footnote: a run that silently
    # stopped fetching it would still look green everywhere else.
    status = doc.get("status", {})
    tried = ", ".join(status.get("models_tried") or []) or "nothing"
    if status.get("models") != "present":
        result.add(WARN if args.allow_missing_models else FAIL,
                   "the model index was fetched",
                   f"not fetched after trying {tried}")
    else:
        result.add(PASS, "the model index was fetched",
                   f"{len(doc.get('models', []))} models from "
                   f"{status.get('models_url')}")

        with_model = [v for v in views if v.get("model")]
        bad = [v for v in with_model if v["model"] not in model_names]
        result.count(len(with_model) - len(bad), len(with_model),
                     "every view model is declared", expected=len(with_model))

        # Views carrying a model, against every view. The number that cannot
        # is known and is the same 385 that are not objects in the model, so
        # this is an equality rather than a floor.
        unnamed = sum(1 for v in views if not v.get("diagram_object"))
        result.count(len(with_model), len(views), "views carry a model",
                     expected=len(views) - unnamed)

        # A model's declared view_count must match the views pointing at it.
        counted = {}
        for v in with_model:
            counted[v["model"]] = counted.get(v["model"], 0) + 1
        agree = sum(1 for m in doc.get("models", [])
                    if counted.get(m["name"], 0) == m["view_count"])
        result.count(agree, len(doc.get("models", [])),
                     "model view counts agree",
                     expected=len(doc.get("models", [])))

    # Geometry. Only meaningful when pages were fetched; when they were not,
    # say so with the denominator rather than passing silently.
    status = doc.get("status", {})
    gnodes = doc.get("geometry_nodes", [])
    gedges = doc.get("geometry_edges", [])
    if status.get("geometry") != "present":
        result.add(PASS, "view geometry",
                   f"not fetched (mode {doc.get('extract', {}).get('mode')})")
    else:
        geo_views = {v["id"] for v in views if v.get("has_geometry")}
        result.count(len(geo_views), status.get("views_with_geometry", 0),
                     "views flagged as carrying geometry",
                     expected=status.get("views_with_geometry", 0))

        # Every node and edge must name a view that exists.
        ok = sum(1 for n in gnodes if n.get("view") in view_ids)
        result.count(ok, len(gnodes), "geometry nodes name a known view",
                     expected=len(gnodes))
        ok = sum(1 for e in gedges if e.get("view") in view_ids)
        result.count(ok, len(gedges), "geometry edges name a known view",
                     expected=len(gedges))

        # A node's object must resolve. This is the join that makes geometry
        # useful: without it a box has a shape and no type.
        # Bounded, not an equality. A page draws blocks the model does not
        # contain: view 54486 carries two `CommandDefinition` controls whose
        # ids are among the 15 memberships that already resolve to nothing.
        # Those are the page's own furniture, not missing content.
        with_obj = [n for n in gnodes if n.get("object")]
        ok = sum(1 for n in with_obj if n["object"] in object_ids)
        dangling = len(with_obj) - ok
        result.add(PASS if dangling <= args.allow_unresolved_members else FAIL,
                   "geometry nodes resolve to an object",
                   f"{ok} of {len(with_obj)}, {dangling} to nothing, "
                   f"allowed {args.allow_unresolved_members}")
        if gnodes:
            share = 100.0 * len(with_obj) / len(gnodes)
            result.add(PASS if share >= 90 else FAIL,
                       "geometry nodes carry an object",
                       f"{len(with_obj)} of {len(gnodes)} ({share:.1f}%)")

        # parent_id and edge endpoints reference node ids within the same view.
        by_view = {}
        for n in gnodes:
            by_view.setdefault(n["view"], set()).add(n["node_id"])
        parented = [n for n in gnodes if n.get("parent_id")]
        ok = sum(1 for n in parented
                 if n["parent_id"] in by_view.get(n["view"], ()))
        result.count(ok, len(parented),
                     "containment resolves within its view",
                     expected=len(parented))
        # Every edge must have both endpoints. Checking only the edges that
        # already had one hid 13 misclassified junction elements sitting in
        # the edge collection with neither -- a check that skips the empty
        # cases cannot see the case where everything is empty.
        both = sum(1 for e in gedges
                   if e.get("from_node") and e.get("to_node"))
        result.count(both, len(gedges), "every geometry edge has endpoints",
                     expected=len(gedges))

        ends = [e for e in gedges if e.get("from_node") or e.get("to_node")]
        ok = sum(1 for e in ends
                 if e.get("from_node") in by_view.get(e["view"], ())
                 and e.get("to_node") in by_view.get(e["view"], ()))
        result.count(ok, len(ends), "edge endpoints resolve to nodes",
                     expected=len(ends))

        # A page that parsed into nothing must not look like an empty page.
        #
        # This compares against an EXPECTED value rather than demanding zero.
        # Zero is not achievable and never was: junctions are connector nodes
        # drawn without a box, so they legitimately yield none. Demanding zero
        # made a check that could only ever warn, and a permanently warning
        # check is noise that stops being read.
        #
        # The direction of a mismatch is the signal. Changeset 035
        # reclassified OrJunction from edge to node; edges fell 3,680 -> 3,667
        # and unboxed rose 16 -> 29 in the same step, the same 13 blocks moving
        # between two counters. So a count BELOW the expectation suggests that
        # reclassification has regressed and junctions are back in the edge
        # collection with no endpoints.
        unboxed = status.get("geometry_unboxed", 0)
        want_unboxed = args.expect_geometry_unboxed
        by_concept = status.get("geometry_unboxed_concepts") or {}
        detail = (f"{unboxed} across "
                  f"{status.get('views_with_geometry', 0)} views, "
                  f"expected {want_unboxed}")
        if unboxed == want_unboxed:
            result.add(PASS, "blocks without a box", detail)
        else:
            # The direction alone does NOT identify the cause, and an earlier
            # version of this check asserted that it did. A fall can mean a
            # junction reclassification regressed, or that box_of gained a
            # strategy -- changeset 049 did the latter, recovered 39 blocks,
            # and this check reported a suspected regression. So it now
            # reports the direction and hands over the per-concept counts
            # instead of naming a cause.
            way = "FEWER" if unboxed < want_unboxed else "more"
            result.add(WARN, "blocks without a box",
                       f"{detail} - {way} than expected; compare the concepts "
                       f"below against the previous run rather than assuming "
                       f"a cause")
        endless = status.get("geometry_endless_edges", 0)
        if endless:
            result.add(WARN, "edges dropped for having no endpoints",
                       f"{endless} - "
                       + ", ".join(f"{k} {v}" for k, v in sorted(
                           (status.get("geometry_endless_edge_concepts")
                            or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:6]))
        else:
            result.add(PASS, "edges dropped for having no endpoints", "0")

        if by_concept:
            # Sorted here rather than upstream: the extract is written with
            # sort_keys=True so the content digest is stable, which discards
            # any order build() might have chosen.
            top = sorted(by_concept.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
            result.add(PASS, "blocks without a box, by concept",
                       ", ".join(f"{k} {v}" for k, v in top)
                       + ("" if len(by_concept) <= 6
                          else f", +{len(by_concept) - 6} more"))

    # Relation verbs, reported WITH the population each count is over.
    #
    # Two figures in this project's reference data were recorded as a bare
    # number against a verb when they were actually counts of one endpoint
    # pair: `message end` was written as 9,564 (ExecSpec->ExecSpec) when the
    # verb holds 10,292 edges in total, and `member end` as 6,605 (Class->
    # Class) against 6,975. Both labels were right and both numbers were
    # unusable, because nothing said which population they covered. Printing
    # the dominant endpoint pair beside the total means a figure copied out of
    # this log carries its own denominator.
    if relations:
        cat_of = {o.get("id"): o.get("category", "?") for o in objects}
        per_verb = {}
        for r in relations:
            v = r.get("verb", "")
            e = per_verb.setdefault(v, [0, {}])
            e[0] += 1
            pair = (cat_of.get(r.get("source"), "?"),
                    cat_of.get(r.get("target"), "?"))
            e[1][pair] = e[1].get(pair, 0) + 1
        top = sorted(per_verb.items(), key=lambda kv: -kv[1][0])[:5]
        for verb, (total, pairs) in top:
            (a, b), n = max(pairs.items(), key=lambda kv: kv[1])
            result.add(PASS, f"verb {verb!r}",
                       f"{total} edges; largest pair {a} -> {b} = {n} "
                       f"({n / total:.0%})")

    # The allowlist is imported, never restated. This reports what stage 2
    # would select from this extract without running stage 2.
    selected = sum(1 for o in objects
                   if L.is_wanted(o.get("category", ""), o.get("name", "")))
    result.add(PASS, "objects the current allowlist would select",
               f"{selected} of {len(objects)}")

    # Service domains counted across every spelling. A substring test against
    # one spelling found only part of the set here twice; normalise, then
    # union every match.
    def norm(text: str) -> str:
        return "".join(ch for ch in (text or "").lower() if ch.isalnum())

    sd_categories = sorted({o.get("category") for o in objects
                            if norm(o.get("category")) == "servicedomain"})
    sd_objects = [o for o in objects
                  if norm(o.get("category")) == "servicedomain"]
    sd_names = len({o.get("name") for o in sd_objects})
    if args.min_service_domains:
        status = PASS if len(sd_objects) >= args.min_service_domains else FAIL
        result.add(status, "service domain objects",
                   f"{len(sd_objects)} objects across "
                   f"{len(sd_categories)} spellings "
                   f"({', '.join(sd_categories) or 'none'}), "
                   f"{sd_names} distinct names, floor "
                   f"{args.min_service_domains}")
    else:
        result.add(PASS, "service domain objects",
                   f"{len(sd_objects)} objects across "
                   f"{len(sd_categories)} spellings, "
                   f"{sd_names} distinct names")

    # Canary.
    if args.canary_id:
        want = f"urn:bian:{doc['extract']['source_id']}:object:{args.canary_id}"
        found = next((o for o in objects if o.get("id") == want), None)
        if found is None:
            result.add(FAIL, "canary object present",
                       f"{args.canary_id} not in the extract")
        elif args.canary_name and found.get("name") != args.canary_name:
            result.add(FAIL, "canary object present",
                       f"{args.canary_id} is named {found.get('name')!r}, "
                       f"expected {args.canary_name!r}")
        else:
            result.add(PASS, "canary object present",
                       f"{args.canary_id} = {found.get('name')!r} "
                       f"({found.get('category')})")

    # Floors. Bounds, not equalities: a gate that catches a broken mechanism
    # rather than one demanding perfection.
    for value, floor, label in (
        (len(objects), args.min_objects, "objects"),
        (len(views), args.min_views, "views"),
    ):
        if floor:
            status = PASS if value >= floor else FAIL
            result.add(status, f"{label} above floor",
                       f"{value}, floor {floor}")

    # The source input gate. Everything else in this file runs DOWNSTREAM of
    # the extractor and so cannot see what the extractor filtered out; this is
    # the one check that looks the other way. Reported here rather than
    # recomputed: the gate observes the SOURCE, and by the time an extract
    # exists the source is no longer in hand.
    gate = (doc.get("status", {}) or {}).get("gate") or {}
    if gate.get("ok") is None and gate.get("detail") == "not run":
        result.add(FAIL if args.require_gate else WARN,
                   "the source input gate ran",
                   "not run — an extract with no gate result has NOT been "
                   "checked against its source"
                   + ("" if args.require_gate else
                      " (pass --require-gate to enforce)"))
    elif not gate:
        result.add(FAIL if args.require_gate else WARN,
                   "the source input gate ran",
                   "no gate block; this extract predates the gate")
    else:
        for f in gate.get("not_measured", []):
            result.add(FAIL, f"gate {f['code']}",
                       f"NOT MEASURED — {f.get('detail') or f['what']}")
        for f in gate.get("failed", []):
            result.add(FAIL, f"gate {f['code']}",
                       f"{f['affected']} of {f['denominator']} "
                       f"({f.get('share', 0):.3f}%) — {f['what']}"
                       + (f" [{f['detail']}]" if f.get("detail") else ""))
        # Sub-threshold findings are carried, not hidden. Printed with their
        # denominators so a figure copied out of this log arrives with one.
        for f in sorted(gate.get("under_threshold", []),
                        key=lambda x: -(x.get("share") or 0)):
            result.add(PASS, f"gate {f['code']} (under threshold)",
                       f"{f['affected']} of {f['denominator']} "
                       f"({f.get('share', 0):.3f}%) — {f['what']}")
        total = gate.get("under_threshold_total_share", 0)
        cap = (gate.get("thresholds") or {}).get("max_total_share")
        result.add(FAIL if gate.get("aggregate_breached") else PASS,
                   "gate sub-threshold aggregate",
                   f"{total}% across {len(gate.get('under_threshold', []))} "
                   f"findings, cap {cap}%")
        if gate.get("observe_only"):
            result.add(WARN, "gate mode",
                       "OBSERVE-ONLY — threshold findings did not fail this "
                       "run. Set the thresholds from these numbers, then "
                       "enforce.")

    # G70: every key the extractor emits into `status` must be DECLARED in
    # the schema. `status.additionalProperties` is true and stays true -- it
    # is the one extension point that lets a new counter land without a schema
    # bump -- but an undeclared counter is one that could stop being emitted
    # without failing anything, which is how six of them survived for months.
    # Checked here rather than in the gate library because it needs the schema
    # file, and the gate is pure.
    try:
        from bianlib import gate as G
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for f in G.schema_reachability(doc.get("status", {}) or {}, schema):
            if f["affected"] is None:
                result.add(FAIL, f"gate {f['code']}",
                           f"NOT MEASURED — {f['detail']}")
            else:
                result.add(FAIL if f["affected"] else PASS,
                           f"gate {f['code']}",
                           f"{f['affected']} of {f['denominator']} status "
                           f"keys undeclared"
                           + (f" [{f['detail']}]" if f["detail"] else ""))
    except Exception as e:                                  # noqa: BLE001
        result.add(FAIL, "gate G70-SCHEMA", f"{type(e).__name__}: {e}")

    # Digest is reproducible from the content it describes.
    try:
        from bianlib import extract as E
        recomputed = E.content_digest(doc)
        ok = recomputed == doc.get("content_digest")
        result.add(PASS if ok else FAIL, "content digest reproduces",
                   recomputed[:16] + ("" if ok else
                                      f" != {str(doc.get('content_digest'))[:16]}"))
    except Exception as e:
        result.add(FAIL, "content digest reproduces",
                   f"{type(e).__name__}: {e}")


def report(result: Result, outdir: Path, doc: dict) -> int:
    line = "=" * 70
    print(f"\n{line}\n  Extract check: {outdir}\n{line}\n")
    meta = doc.get("extract", {})
    print(f"  source      : {meta.get('source_id')}")
    print(f"  mode        : {meta.get('mode')}")
    print(f"  fetched at  : {meta.get('fetched_at')}")
    print(f"  schema      : {meta.get('schema_version')}   "
          f"parser: {meta.get('parser_version')}")
    total = sum(f.stat().st_size for f in outdir.glob("*.jsonld"))
    print(f"  size        : {total / 1024 / 1024:.1f} MB across "
          f"{len(list(outdir.glob('*.jsonld')))} files")
    print(f"  content     : {str(doc.get('content_digest'))[:16]}\n")
    for status, name, detail in result.rows:
        print(f"  [{status}] {name:<44} {detail}")
    counts = {}
    for status, _, _ in result.rows:
        counts[status] = counts.get(status, 0) + 1
    print(f"\n{line}")
    summary = "  " + "   ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(summary)
    if result.failed:
        print("  RESULT: EXTRACT FAILED VALIDATION")
        print(f"{line}\n")
        return 1
    print("  RESULT: EXTRACT IS VALID")
    print(f"{line}\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path,
                    help="the extract directory, e.g. out/_extract/bian-v14")
    ap.add_argument("--require-schema", action="store_true",
                    help="fail if jsonschema is not installed")
    ap.add_argument("--require-gate", action="store_true",
                    help="fail if the extract carries no gate result. An "
                         "extract that was never checked against its source "
                         "must not read as one that passed")
    ap.add_argument("--canary-id", default="")
    ap.add_argument("--canary-name", default="")
    ap.add_argument("--min-objects", type=int, default=0)
    ap.add_argument("--min-views", type=int, default=0)
    ap.add_argument("--min-service-domains", type=int, default=0)
    ap.add_argument("--min-notation-share", type=float, default=95.0,
                    help="percent of objects that must resolve a notation")
    ap.add_argument("--allow-unresolved-relations", type=int, default=5,
                    help="known-unresolvable relation targets")
    ap.add_argument("--allow-missing-models", action="store_true",
                    help="downgrade a missing model index to a warning")
    ap.add_argument("--expect-geometry-unboxed", type=int, default=14,
                    help="blocks that legitimately have no box. Measured 14 on "
                         "extract run 33443477519, and the by-concept line on "
                         "that run says exactly what they are: OrJunction 9, "
                         "Junction 4, MotivationValue 1. The 13 junctions are "
                         "connector nodes drawn without a box by design and "
                         "are the same 13 changeset 035 moved out of the edge "
                         "collection. The single MotivationValue is "
                         "unexplained. This figure has moved four times for "
                         "four different reasons -- read the by-concept line, "
                         "never the total alone")
    ap.add_argument("--allow-unresolved-members", type=int, default=25,
                    help="memberships naming neither an object nor a view "
                         "(measured 15 of 127,588 on 29 August 2026)")
    args = ap.parse_args(argv)

    outdir = args.path
    if outdir.is_file():
        # Pointed at the index rather than the directory holding it.
        outdir = outdir.parent
    if not outdir.is_dir():
        print(f"No such extract directory: {args.path}", file=sys.stderr)
        return 2
    try:
        doc, raw = load(outdir)
    except Exception as e:
        print(f"Could not read the extract in {outdir}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    result = Result()
    check_schema(raw, result, args.require_schema)
    try:
        check_parts(outdir, raw, result)
        check_integrity(doc, result, args)
    except Exception as e:
        result.add(FAIL, "integrity checks ran", f"{type(e).__name__}: {e}")
    return report(result, outdir, doc)


if __name__ == "__main__":
    raise SystemExit(main())
