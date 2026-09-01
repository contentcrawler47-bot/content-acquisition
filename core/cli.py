"""
Command line entry point.

Sources are discovered by scanning sources/<id>/source.py for a class named
`Source`. Adding a directory adds a source; nothing central needs editing, so
sources cannot break each other by being added or removed.

    python run.py list
    python run.py validate bian             # CAN WE EXTRACT? (no Drive involved)
    python run.py harvest bian
    python run.py extract bian             # STAGE 1: store the model as data
    python run.py publish bian [--dry-run]  # CAN WE PUBLISH? (Drive only)
    python run.py check-publish             # Drive credentials/reachability
    python run.py run bian [--publish]      # harvest + validate (+ publish)
    python run.py reindex

`validate` and `publish` are deliberately separate commands with separate
workflows: a red validate means the source cannot be extracted, a red publish
means Drive is the problem. They never share an exit code.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import traceback
from pathlib import Path

from . import checks as checks_mod
from .render import reset_dir
from .source import Source

REPO = Path(__file__).resolve().parent.parent
SOURCES_DIR = REPO / "sources"
OUT = REPO / "out"
#: Stage 1 output. Separate from out/<id>/, which write_bundles empties
#: on every harvest — an extract must not be destroyed by a render.
EXTRACT_OUT = OUT / "_extract"


def discover() -> dict[str, Source]:
    found: dict[str, Source] = {}
    if not SOURCES_DIR.is_dir():
        return found
    for d in sorted(SOURCES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        mod_path = d / "source.py"
        if not mod_path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"sources.{d.name}.source", mod_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, "Source", None)
            if cls is None or not issubclass(cls, Source):
                print(f"  ! {d.name}: no Source class, skipped", file=sys.stderr)
                continue
            inst = cls()
            inst.id = inst.id or d.name
            found[inst.id] = inst
        except Exception as e:
            # One broken source must not stop the others being usable.
            print(f"  ! {d.name}: failed to load ({type(e).__name__}: {e})",
                  file=sys.stderr)
    return found


def outdir_for(source_id: str) -> Path:
    return OUT / source_id


def cmd_list(sources: dict[str, Source], _args) -> int:
    if not sources:
        print("No sources found in sources/")
        return 1
    print(f"{len(sources)} source(s):\n")
    for s in sources.values():
        secrets = ", ".join(s.required_secrets) or "none"
        missing = ", ".join(s.missing_secrets())
        print(f"  {s.id:<12} {s.name}")
        print(f"  {'':<12} {s.description}" if s.description else "", end="")
        print(f"\n  {'':<12} secrets: {secrets}"
              + (f"  [MISSING: {missing}]" if missing else ""))
        print()
    return 0


def cmd_harvest(sources, args) -> int:
    s = sources[args.source]
    missing = s.missing_secrets()
    if missing:
        print(f"Cannot harvest {s.id}: missing {', '.join(missing)}",
              file=sys.stderr)
        return 2
    outdir = reset_dir(outdir_for(s.id))
    print(f"Harvesting {s} -> {outdir}", flush=True)
    result = s.harvest(outdir)
    print(f"  {result.item_count} items, {result.files_written} files",
          flush=True)
    for cat, n in sorted(result.categories.items(), key=lambda kv: -kv[1]):
        print(f"    {cat:<28} {n:>6}", flush=True)
    for note in result.notes:
        print(f"  note: {note}", flush=True)
    return 0


def ci_run() -> dict:
    """Provenance for the CI run, or a marker that there isn't one.

    Read HERE and nowhere deeper. `bianlib.extract.build()` is documented as
    depending on no environment, and that is what lets it be tested without
    reaching bian.org — so the environment is read at the boundary and passed
    down as data.

    Outside CI this returns `{"where": "local"}` rather than an empty dict or
    partly-filled fields. A sandbox replay must never be able to pass for a
    run: recording a rehearsal as a result has already happened here once.
    """
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return {"where": "local"}
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return {
        "where": "github-actions",
        "run_id": run_id,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "run_number": os.environ.get("GITHUB_RUN_NUMBER", ""),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "repository": repo,
        "url": f"{server}/{repo}/actions/runs/{run_id}" if repo else "",
    }


def cmd_extract(sources, args) -> int:
    """Stage 1. Acquire the source's model and store it as structured data.

    Deliberately does not render, filter or publish. The extract is what
    stage 2 reads, so that a renderer or allowlist change costs a re-render
    rather than another pass over someone else's web server.
    """
    s = sources[args.source]
    missing = s.missing_secrets()
    if missing:
        print(f"Cannot extract {s.id}: missing {', '.join(missing)}",
              file=sys.stderr)
        return 2
    outdir = reset_dir(EXTRACT_OUT / s.id)
    print(f"Extracting {s} -> {outdir}", flush=True)
    try:
        s.build_extract(outdir, mode=args.mode, run=ci_run(),
                        gate_options={
                            "max_share": args.gate_max_share,
                            "max_absolute": args.gate_max_absolute,
                            "max_total_share": args.gate_max_total_share,
                            "observe_only": args.gate_observe_only,
                        })
    except NotImplementedError as e:
        print(f"\n  {e}", file=sys.stderr)
        print("  Stage 1 is optional; this source has not adopted it.",
              file=sys.stderr)
        return 2
    return 0


def cmd_render(sources, args) -> int:
    """Stage 2. Select from a stored extract and report what would publish.

    Makes NO network requests: its input is an extract on disk. That is the
    property the two-stage split exists to protect, so there is deliberately
    no fallback to fetching — a missing extract fails and says how to make
    one, rather than quietly producing a correct-looking result by a route
    nobody asked for.

    Selection only, today. Grouping, link resolution and diagram rendering
    are not built; this command does not write a bundle and does not publish.
    """
    from bianlib import extract as extract_mod
    from bianlib.select import allowlist_delta, report, select

    s = sources[args.source]
    outdir = EXTRACT_OUT / s.id
    if not (outdir / extract_mod.INDEX_FILE).is_file():
        print(f"No extract at {outdir}.", file=sys.stderr)
        print(f"  Run: python3 run.py extract {s.id} --mode model-only",
              file=sys.stderr)
        print("  Stage 2 reads stored data and never fetches; this is not a "
              "condition it can recover from.", file=sys.stderr)
        return 2

    print(f"Rendering {s} from {outdir}", flush=True)
    doc = extract_mod.read(outdir)

    # Say WHICH extract this is before saying anything about its contents. A
    # stored extract and a freshly fetched one must never be indistinguishable
    # after the fact, and an extract built outside CI must never pass for a run.
    meta = doc.get("extract", {}) or {}
    run_meta = meta.get("run") or {}
    print(f"  extract   : fetched {meta.get('fetched_at', 'UNKNOWN')}"
          f"  mode={meta.get('mode', 'UNKNOWN')}"
          f"  parser={meta.get('parser_version', 'UNKNOWN')}")
    where = run_meta.get("where")
    if where == "github-actions":
        print(f"  produced by: run {run_meta.get('run_id')} "
              f"attempt {run_meta.get('run_attempt') or '?'} "
              f"({run_meta.get('workflow') or 'unknown workflow'})")
        if run_meta.get("url"):
            print(f"               {run_meta['url']}")
    elif where == "local":
        print("  produced by: NOT A CI RUN — built locally")
    else:
        # Extracts written before changeset 039 carry no run block at all.
        print("  produced by: UNRECORDED — this extract predates run "
              "provenance, so it cannot be traced to a run")

    keep, notes = None, []
    if args.add_category or args.drop_category:
        keep, notes = allowlist_delta(add=args.add_category or (),
                                      drop=args.drop_category or ())

    sel = select(doc.get("objects", []), keep=keep)

    if notes:
        # An experimental selection must never be mistaken for the published
        # one, so it is announced before the numbers it changes and again
        # after them.
        print("\n  *** EXPERIMENTAL SELECTION — NOT THE PUBLISHED SET ***")
        for n in notes:
            print(f"      {n}")
    print()
    print("\n".join(report(sel)))

    if not sel.canary(s.canary_id, s.canary_name):
        print(f"\n  FAIL: canary {s.canary_id} ({s.canary_name}) did not "
              f"survive selection.", file=sys.stderr)
        return 1
    print(f"\n  canary {s.canary_id} ({s.canary_name}): kept")

    if len(sel.kept) < s.min_objects:
        print(f"\n  FAIL: {len(sel.kept)} objects selected, floor is "
              f"{s.min_objects}.", file=sys.stderr)
        return 1

    if notes:
        print("\n  *** the numbers above are EXPERIMENTAL and were NOT "
              "produced by the published allowlist ***")
    return 0


def cmd_validate(sources, args) -> int:
    """Validate that content can be EXTRACTED from this source.

    Never touches Drive. Runs connectivity probes first, then (unless
    --no-harvest) a real harvest into out/<id>/, then the written-output and
    source-specific checks. Stops at the first failing stage.
    """
    s = sources[args.source]
    checks: list = []

    # Stage 0: credentials present? Cheap, and a very common cause.
    missing = s.missing_secrets()
    if s.required_secrets:
        from .source import Check, Stage
        checks.append(Check(
            "required secrets present", not missing,
            f"missing: {', '.join(missing)}" if missing
            else f"{len(s.required_secrets)} set",
            stage=Stage.CONNECT,
            hint=("Set these in the environment locally, or as repo secrets "
                  "referenced by this source's workflow: "
                  + ", ".join(s.required_secrets))))
        if missing:
            return checks_mod.report(s, checks, strict=args.strict,
                                     skipped_from=_stage("PAYLOAD"))

    # Stages 1-2: connectivity and payload shape.
    probe_checks, _ = checks_mod.probe_checks(s, timeout=args.timeout)
    checks += probe_checks
    if any(not c.ok and not c.warn for c in probe_checks):
        return checks_mod.report(s, checks, strict=args.strict,
                                 skipped_from=_stage("PARSE"))

    # Stage 3: parse + extract, by actually harvesting.
    outdir = outdir_for(s.id)
    if not args.no_harvest:
        from .source import Check, Stage
        try:
            outdir = reset_dir(outdir)
            print(f"\n  extracting {s} -> {outdir}", flush=True)
            result = s.harvest(outdir)
            checks.append(Check("harvest completed", True,
                                f"{result.item_count} items, "
                                f"{result.files_written} files",
                                stage=Stage.PARSE))
            for cat, n in sorted(result.categories.items(), key=lambda kv: -kv[1]):
                print(f"    {cat:<28} {n:>6}", flush=True)
        except Exception as e:
            checks.append(Check(
                "harvest completed", False, f"{type(e).__name__}: {e}",
                stage=Stage.PARSE,
                hint="The source was reachable and returned the expected "
                     "payload, so this is a parsing or extraction fault in "
                     "sources/{}/source.py.".format(s.id)))
            return checks_mod.report(s, checks, strict=args.strict,
                                     skipped_from=_stage("EXTRACT"))

    # Stages 4-5: what was actually written.
    checks += checks_mod.output_checks(outdir, s)
    try:
        checks += s.checks(outdir)
    except Exception as e:
        from .source import Check, Stage
        checks.append(Check("source checks ran", False,
                            f"{type(e).__name__}: {e}", stage=Stage.EXTRACT,
                            hint=f"checks() in sources/{s.id}/source.py "
                                 f"raised. Fix the check, then re-run."))

    return checks_mod.report(s, checks, strict=args.strict)


def _stage(name):
    from .source import Stage
    return getattr(Stage, name)


def cmd_check_publish(_sources, _args) -> int:
    """Validate the PUBLISHING half only — credentials and Drive reachability.

    Separate from `validate` so the two failure modes never blur together.
    """
    from . import publish as publish_mod
    print("\n=== Publishing target validation (Google Drive) ===\n", flush=True)
    try:
        publish_mod.preflight()
        print("  [PASS] Drive credentials present in environment", flush=True)
    except publish_mod.PublishError as e:
        print(f"  [FAIL] {e}", flush=True)
        print("\n  This is a PUBLISHING problem, not a source problem.",
              flush=True)
        print("  Set GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_TOKEN "
              "as repo secrets.", flush=True)
        return 1
    try:
        version = publish_mod.check_binary()
        print(f"  [PASS] rclone runs — {version}", flush=True)
    except publish_mod.PublishError as e:
        print(f"  [FAIL] rclone will not run: {e}", flush=True)
        print("\n  Every rclone call would fail the same way. A common cause "
              "is an", flush=True)
        print("  environment variable named RCLONE_<SOMETHING> that rclone "
              "reads as one", flush=True)
        print("  of its own flags.", flush=True)
        return 1
    try:
        folders = publish_mod.list_published()
        print(f"  [PASS] Drive reachable — "
              f"{len(folders)} published source(s): "
              f"{', '.join(folders) or '(none yet)'}", flush=True)
    except publish_mod.PublishError as e:
        print(f"  [FAIL] Drive unreachable: {e}", flush=True)
        print("\n  Credentials are set but Drive rejected them. The token may "
              "have expired or been revoked;", flush=True)
        print("  re-run `rclone config` and update GDRIVE_TOKEN.", flush=True)
        return 1
    print("\n  RESULT: publishing target is healthy.", flush=True)
    return 0


def cmd_publish(sources, args) -> int:
    from . import publish as publish_mod
    s = sources[args.source]
    try:
        dest = publish_mod.publish(s.id, outdir_for(s.id), dry_run=args.dry_run)
    except publish_mod.PublishError as e:
        print(f"Publish failed for {s.id}: {e}", file=sys.stderr)
        return 1
    print(f"  published {s.id} -> {dest}")
    return 0


def cmd_run(sources, args) -> int:
    args.no_harvest = False
    rc = cmd_validate(sources, args)
    if rc:
        print("\nExtraction validation failed — not publishing. "
              "This is a SOURCE problem.", file=sys.stderr)
        return rc
    if args.publish:
        return cmd_publish(sources, args)
    print("\n(not publishing: pass --publish to sync to Drive)")
    return 0


def cmd_reindex(_sources, _args) -> int:
    from . import publish as publish_mod
    try:
        publish_mod.reindex(OUT / "_reindex")
    except publish_mod.PublishError as e:
        print(f"Reindex failed: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    sources = discover()

    ap = argparse.ArgumentParser(prog="run.py", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_source(p):
        p.add_argument("source", choices=sorted(sources) or None,
                       help="source id")
        return p

    sub.add_parser("list", help="show configured sources")
    with_source(sub.add_parser("harvest", help="acquire content"))
    e = with_source(sub.add_parser(
        "extract", help="STAGE 1: store the source model as data"))
    e.add_argument("--mode", choices=["model-only", "full"],
                   default="model-only",
                   help="model-only reads no view pages")
    # Source input gate. The thresholds are passed in rather than fixed in the
    # tool so a reader of the run can see what was demanded of it, exactly as
    # the canary and the floors already are.
    e.add_argument("--gate-max-share", type=float, default=0.5, metavar="PCT",
                   help="fail a gate finding above this share of its "
                        "denominator")
    e.add_argument("--gate-max-absolute", type=int, default=500, metavar="N",
                   help="fail a gate finding above this many affected values, "
                        "whatever its share")
    e.add_argument("--gate-max-total-share", type=float, default=2.0,
                   metavar="PCT",
                   help="fail when sub-threshold findings SUM above this")
    e.add_argument("--gate-observe-only", action="store_true",
                   help="record gate findings without failing on them. "
                        "Fetch, parse and schema findings still fail: you "
                        "cannot threshold your own denominator")
    rn = with_source(sub.add_parser(
        "render", help="STAGE 2: select from a stored extract (no network)"))
    rn.add_argument("--add-category", action="append", metavar="CATEGORY",
                    help="EXPERIMENT: also keep this category. Reports only; "
                         "never publishes. Repeatable.")
    rn.add_argument("--drop-category", action="append", metavar="CATEGORY",
                    help="EXPERIMENT: do not keep this category. Reports "
                         "only; never publishes. Repeatable.")
    v = with_source(sub.add_parser(
        "validate", help="check content can be EXTRACTED (no Drive)"))
    v.add_argument("--strict", action="store_true", help="warnings fail too")
    v.add_argument("--no-harvest", action="store_true",
                   help="validate existing out/<id>/ without re-extracting")
    v.add_argument("--timeout", type=int, default=45,
                   help="per-probe timeout in seconds")
    sub.add_parser("check-publish",
                   help="check Drive credentials and reachability only")
    p = with_source(sub.add_parser("publish", help="sync to Drive"))
    p.add_argument("--dry-run", action="store_true")
    r = with_source(sub.add_parser("run", help="harvest + validate [+ publish]"))
    r.add_argument("--strict", action="store_true")
    r.add_argument("--publish", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--timeout", type=int, default=45)
    sub.add_parser("reindex", help="rebuild the top-level Drive index")

    args = ap.parse_args(argv)
    handler = {
        "list": cmd_list, "harvest": cmd_harvest, "validate": cmd_validate,
        "extract": cmd_extract, "render": cmd_render,
        "publish": cmd_publish, "run": cmd_run, "reindex": cmd_reindex,
        "check-publish": cmd_check_publish,
    }[args.cmd]

    try:
        return handler(sources, args)
    except Exception:
        traceback.print_exc()
        return 1
