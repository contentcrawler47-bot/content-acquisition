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

    # Models, when they were fetched at all.
    status_models = doc.get("status", {}).get("models")
    if status_models == "not-fetched":
        result.add(WARN, "views carry a model",
                   f"0 of {len(views)} — models not fetched in this run")
    else:
        with_model = [v for v in views if v.get("model")]
        bad = [v for v in with_model if v["model"] not in model_names]
        result.count(len(with_model) - len(bad), len(with_model),
                     "every view model is declared",
                     expected=len(with_model))

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
    ap.add_argument("--canary-id", default="")
    ap.add_argument("--canary-name", default="")
    ap.add_argument("--min-objects", type=int, default=0)
    ap.add_argument("--min-views", type=int, default=0)
    ap.add_argument("--min-service-domains", type=int, default=0)
    ap.add_argument("--min-notation-share", type=float, default=95.0,
                    help="percent of objects that must resolve a notation")
    ap.add_argument("--allow-unresolved-relations", type=int, default=5,
                    help="known-unresolvable relation targets")
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
