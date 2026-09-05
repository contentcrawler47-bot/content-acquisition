"""
Command line entry point.

Sources are discovered by scanning sources/<id>/source.py for a class named
`Source`. Adding a directory adds a source; nothing central needs editing, so
sources cannot break each other by being added or removed.

    python run.py list
    python run.py validate bian             # CAN WE EXTRACT? (no Drive involved)
    python run.py harvest bian
    python run.py extract bian             # STAGE 1: store the model as data
    python run.py acquire bian --mode full # retain raw artifacts + provenance
    python run.py archive bian --run-id X  # copy that run to Drive, immutably
    python run.py restore bian [--run-id X] # copy an archived run back (R10)
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
#: Stage 2 output: out/_raw/<source>/<run-id>/. Run-addressed and never
#: emptied by anything -- a run directory is refused if it already exists.
RAW_OUT = OUT / "_raw"


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
    """Stage 3. Parse a retained acquisition run into structured data.

    Reads the run directory `acquire` wrote, or an archive downloaded from
    Drive, and never the source. Deliberately does not render, filter or
    publish. The extract is what the render stage reads, so that a renderer
    or allowlist change costs a re-render rather than another pass over
    someone else's web server; and because the run is retained, a parser
    change costs a re-extract and no requests at all.

    Exit 2 when the run cannot be read at all: never finished, the wrong
    source, or an artifact that no longer matches its own manifest.
    """
    from bianlib.acquire import RunUnreadable

    s = sources[args.source]
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"Cannot extract {s.id}: {run_dir} is not a directory. Give "
              f"the run `acquire` wrote (out/_raw/{s.id}/<run-id>) or a "
              f"downloaded archive folder.", file=sys.stderr)
        return 2
    outdir = reset_dir(EXTRACT_OUT / s.id)
    print(f"Extracting {s} from {run_dir} -> {outdir}", flush=True)
    try:
        s.build_extract(outdir, run_dir, producer=build_provenance(),
                        gate_options={
                            "max_share": args.gate_max_share,
                            "max_absolute": args.gate_max_absolute,
                            "max_total_share": args.gate_max_total_share,
                            "observe_only": args.gate_observe_only,
                        })
    except NotImplementedError as e:
        print(f"\n  {e}", file=sys.stderr)
        print("  Stage 3 is optional; this source has not adopted it.",
              file=sys.stderr)
        return 2
    except RunUnreadable as e:
        print(f"\n  cannot read the run: {e}", file=sys.stderr)
        return 2
    return 0


def build_provenance() -> dict:
    """Provenance for whatever this process builds: `ci_run()` plus the code.

    One builder for the acquisition record's `provenance` and the extract's
    `producer`, so the two blocks have one shape and a reader walks from a
    projection back to the bytes through fields with the same names. It adds
    what a downloaded artifact has always needed to name the code that made
    it -- the commit SHA and the repo digest. The digest is COMPUTED over the
    checked-out tree by the same function `tools/repo_manifest.py --verify`
    uses, and the manifest's declared digest is recorded beside it, so a run
    on a dirty tree says so.
    """
    prov = ci_run()
    prov["commit_sha"] = os.environ.get("GITHUB_SHA", "")
    prov["ref"] = os.environ.get("GITHUB_REF_NAME", "")
    prov["workflow_ref"] = os.environ.get("GITHUB_WORKFLOW_REF", "")
    prov["runner_os"] = os.environ.get("RUNNER_OS", "")
    try:
        spec = importlib.util.spec_from_file_location(
            "repo_manifest", REPO / "tools" / "repo_manifest.py")
        rm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rm)
        prov["repo_digest"] = rm.digest_of(rm.build())
        declared = rm.load()
        prov["manifest_digest"] = rm.digest_of(declared) if declared else ""
    except Exception as e:                                      # noqa: BLE001
        prov["repo_digest"] = ""
        prov["manifest_digest"] = ""
        prov["repo_digest_error"] = f"{type(e).__name__}: {e}"
    return prov


def default_run_id() -> str:
    """The CI run id and attempt, or a timestamp outside CI.

    Deterministic from the environment so a workflow can name the same
    directory this command will write, without the command telling it.
    """
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        return f"{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    from datetime import datetime, timezone
    return "local-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def cmd_acquire(sources, args) -> int:
    """Stage 2. Acquire and RETAIN the source's raw artifacts, with provenance.

    Writes out/_raw/<source>/<run-id>/ and never touches Drive; archiving is a
    separate step with its own credentials and its own failure. Refuses an
    existing run directory: a run is never rewritten.
    """
    from bianlib import acquire as A
    from bianlib.fetch import SourceUnhappy

    s = sources[args.source]
    if not getattr(s, "base", ""):
        print(f"{s.id} has no `base` URL; acquisition is BIAN-shaped today.",
              file=sys.stderr)
        return 2
    run_dir = RAW_OUT / s.id / (args.run_id or default_run_id())
    print(f"Acquiring {s} -> {run_dir}  (mode {args.mode})", flush=True)
    try:
        run = A.acquire(s, run_dir, mode=args.mode,
                        provenance=build_provenance())
    except FileExistsError as e:
        print(f"\n  {e}", file=sys.stderr)
        return 2
    except SourceUnhappy:
        return 3
    return 0 if run["state"] == "complete" else 1


def cmd_check_raw_target(_sources, args) -> int:
    """Validate the RAW-ARCHIVE half only: credentials, rclone, the raw root.

    Separate from `check-publish` as `check-publish` is from `validate`, so
    a red archive is never mistaken for a red acquisition. Prints the Drive
    quota, which a retention decision needs and no other check records.

    `--pointers` adds the archive-wide sweep (R11): every run's RECORD files
    are copied locally -- never payload.zip -- and `bianlib.acquire
    .check_pointers` confirms each pointer names a sibling holder with the
    same raw digest. A pointed-to run that has gone is a finding here and
    nowhere else until the Health check workflow (075).
    """
    from . import archive as archive_mod
    from bianlib import acquire as A
    print("\n=== Raw archive target validation (Google Drive) ===\n", flush=True)
    lines = archive_mod.check_target()
    for ok, msg in lines:
        print(f"  [{'PASS' if ok else 'FAIL'}] {msg}", flush=True)
    healthy = all(ok for ok, _ in lines)
    if healthy and getattr(args, "pointers", False):
        print("\n=== Pointer sweep over the archive records ===\n", flush=True)
        try:
            runs = archive_mod.list_runs()
        except archive_mod.ArchiveError as e:
            print(f"  [FAIL] Drive unreachable: {e}", flush=True)
            return 1
        for sid in sorted(runs):
            root = RAW_OUT / "_records" / sid
            try:
                n = archive_mod.fetch_records(sid, root)
            except archive_mod.ArchiveError as e:
                print(f"  [FAIL] {sid}: {e}", flush=True)
                healthy = False
                continue
            print(f"  {sid}: records of {n} run folder(s) copied "
                  f"(no {A.PAYLOAD_FILE})", flush=True)
            healthy = print_pointer_sweep(A.check_pointers(root)) and healthy
    if healthy:
        print("\n  RESULT: raw archive target is healthy.", flush=True)
        return 0
    print("\n  This is an ARCHIVE problem, not a source problem. Acquisition "
          "itself references no Drive credentials.", flush=True)
    return 1


def print_pointer_sweep(sweep: dict) -> bool:
    """Print a `check_pointers` result as PASS/FAIL lines; True when clean.
    Shared with tools/check_raw.py --pointers so the two read alike."""
    print(f"  runs {sweep['runs']}: holders {sweep['holders']}, pointers "
          f"{sweep['pointers']}, distinct targets {len(sweep['pointed_to'])}")
    for target in sorted(sweep["pointed_to"]):
        print(f"    target {target}: must not be deleted")
    for f in sweep["findings"]:
        print(f"  [FAIL] {f}")
    if not sweep["findings"]:
        print(f"  [PASS] every pointer names a sibling holder with the same "
              f"raw digest ({sweep['pointers']} of {sweep['pointers']})")
    return not sweep["findings"]


def cmd_restore(sources, args) -> int:
    """Copy an archived run from Drive into out/_raw/<source>/ (R10, P.2).

    The consumer half of the archive: Extract and Regenerate read a run by
    id and never fetch from the source. `--run-id` empty means the newest
    archived run. A de-duplicated run is restored together with the run it
    points to, as siblings, so every reader sees the payload through the
    pointer. `--resolve-only` prints the resolution and copies nothing, so a
    workflow can try the Actions cache under the resolved id first.

    Prints `raw_run_id=` and `payload_run_id=` lines and, when GITHUB_OUTPUT
    is set, writes the same two names there. Exit 2 when the run cannot be
    resolved or is refused; 1 for any other Drive failure.
    """
    from . import archive as archive_mod
    s = sources[args.source]
    root = RAW_OUT / s.id
    try:
        archive_mod.preflight()
        if args.resolve_only:
            r = archive_mod.resolve_run(s.id, args.run_id or None)
            print(f"Resolved {s} run {r['run_id']}"
                  + (f" -> pointer to {r['payload_run_id']}"
                     if r["pointer"] else " (holds its payload)"), flush=True)
        else:
            r = archive_mod.restore_run(s.id, args.run_id or None, root)
            print(f"Restored {s} run {r['run_id']} -> {root / r['run_id']}"
                  + (f"  with its target {r['payload_run_id']} as a sibling"
                     if r["pointer"] else ""), flush=True)
            print(f"  verified  : {r['files_verified']} sidecar lines across "
                  f"{len(r['folders'])} folder(s)"
                  + (f"; payload read via {r['payload_via']}"
                     if r["payload_via"] else ""), flush=True)
    except archive_mod.PublishError as e:
        print(f"\n  {e}", file=sys.stderr)
        return 1
    except archive_mod.ArchiveError as e:
        msg = str(e)
        print(f"\n  {msg}", file=sys.stderr)
        refused = ("not an archived run" in msg or "no archived run" in msg
                   or "already exists" in msg or "points to" in msg)
        return 2 if refused else 1
    print(f"  raw_run_id={r['run_id']}")
    print(f"  payload_run_id={r['payload_run_id']}")
    out_path = os.environ.get("GITHUB_OUTPUT")
    if out_path:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(f"raw_run_id={r['run_id']}\n")
            fh.write(f"payload_run_id={r['payload_run_id']}\n")
    return 0


def cmd_archive(sources, args) -> int:
    """Archive one acquisition run to Drive, immutably, and verify it landed.

    Exit 0 archived and verified -- as a pointer when an archived run already
    holds the identical payload bytes (R11); 2 refused (unfinished run, run
    that does not verify locally, or a remote folder that already holds a
    run); 1 any other archive failure. A refusal is not a fault -- it is the guard doing
    its job -- but it is still red, because the run was not archived.
    """
    from . import archive as archive_mod
    s = sources[args.source]
    run_dir = RAW_OUT / s.id / args.run_id
    print(f"Archiving {run_dir} -> Drive"
          + ("  [dry run]" if args.dry_run else ""), flush=True)
    try:
        summary = archive_mod.archive(run_dir, s.id, dry_run=args.dry_run)
    except archive_mod.ArchiveError as e:
        msg = str(e)
        print(f"\n  {msg}", file=sys.stderr)
        refused = ("never finished" in msg or "does not verify" in msg
                   or "already holds" in msg)
        return 2 if refused else 1
    except Exception as e:                                      # noqa: BLE001
        print(f"\n  {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"\n  archived {summary['run_id']} to {summary['destination']}"
          + (f"  as a pointer to {summary['same_as']}"
             if summary.get("same_as") else "")
          + ("  (dry run)" if summary.get("dry_run") else ""), flush=True)
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
        print(f"  Locally: python3 run.py extract {s.id} --run-dir "
              f"out/_raw/{s.id}/<run-id>", file=sys.stderr)
        print("  In CI: the Render workflow restores the extract from the "
              "Actions cache; a miss names the Regenerate dispatch (S.8).",
              file=sys.stderr)
        print("  Stage 2 reads stored data and never fetches; this is not a "
              "condition it can recover from.", file=sys.stderr)
        return 2

    print(f"Rendering {s} from {outdir}", flush=True)
    # Verified INSIDE the stage, before anything is read (S.6): every file
    # against the sidecar, and the sidecar present at all. This was a
    # `sha256sum -c` step in the workflow, which a second consumer could
    # forget; here it cannot be skipped. A mismatch is a failure of this
    # stage, not a condition to read through.
    try:
        verified = extract_mod.verify(
            outdir, expect_raw_run_id=args.expect_raw_run_id or None)
    except extract_mod.ExtractUnreadable as e:
        print(f"\n  cannot read the extract: {e}", file=sys.stderr)
        return 1
    print(f"  verified  : {verified['files']} files match "
          f"{extract_mod.SIDECAR_FILE}"
          + (f"; built from run {verified['raw_run_id']} as asked"
             if args.expect_raw_run_id else ""))
    doc = extract_mod.read(outdir, verify_first=False)

    # Say WHICH extract this is before saying anything about its contents. A
    # stored extract and a freshly fetched one must never be indistinguishable
    # after the fact, and an extract built outside CI must never pass for a
    # run. Since schema 1.10.0 `producer` is the build and `run` the
    # acquisition; before it, `run` was both at once, and is all there is.
    meta = doc.get("extract", {}) or {}
    run_meta = meta.get("run") or {}
    maker = meta.get("producer") or run_meta
    built = meta.get("built_at") or meta.get("fetched_at", "UNKNOWN")
    print(f"  extract   : built {built}"
          f"  captured {meta.get('captured_at') or 'UNRECORDED'}"
          f"  mode={meta.get('mode', 'UNKNOWN')}"
          f"  parser={meta.get('parser_version', 'UNKNOWN')}"
          f"  schema={meta.get('schema_version', 'UNKNOWN')}")
    where = maker.get("where")
    if where == "github-actions":
        print(f"  produced by: run {maker.get('run_id')} "
              f"attempt {maker.get('run_attempt') or '?'} "
              f"({maker.get('workflow') or 'unknown workflow'}) at commit "
              f"{(maker.get('commit_sha') or '')[:12] or 'UNRECORDED'}")
        if maker.get("url"):
            print(f"               {maker['url']}")
    elif where == "local":
        print("  produced by: NOT A CI RUN — built locally")
    else:
        # Extracts written before changeset 039 carry no run block at all.
        print("  produced by: UNRECORDED — this extract predates run "
              "provenance, so it cannot be traced to a run")
    if run_meta.get("raw_run_id"):
        print(f"  built from : run {run_meta['raw_run_id']} "
              f"({run_meta.get('raw_run_state') or '?'}), acquired at commit "
              f"{(run_meta.get('commit_sha') or '')[:12] or 'UNRECORDED'}")

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
        "extract", help="STAGE 3: parse a retained run into stored data "
                        "(never fetches)"))
    e.add_argument("--run-dir", required=True, metavar="DIR",
                   help="the acquisition run to read: out/_raw/<source>/"
                        "<run-id> as `acquire` wrote it, or a folder "
                        "downloaded from raw/<source>/<run-id>/ on Drive. "
                        "The extract's mode is the run's")
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
    a = with_source(sub.add_parser(
        "acquire", help="acquire and RETAIN raw artifacts with provenance "
                        "(no Drive)"))
    a.add_argument("--mode", choices=["model-only", "full"],
                   default="model-only",
                   help="model-only fetches the data files; full adds the "
                        "declared view pages")
    a.add_argument("--run-id", default=None, metavar="ID",
                   help="run directory name; defaults to the CI run id and "
                        "attempt, or a timestamp outside CI")
    rn = with_source(sub.add_parser(
        "render", help="STAGE 2: select from a stored extract (no network)"))
    rn.add_argument("--add-category", action="append", metavar="CATEGORY",
                    help="EXPERIMENT: also keep this category. Reports only; "
                         "never publishes. Repeatable.")
    rn.add_argument("--expect-raw-run-id", default=None, metavar="ID",
                    help="fail unless the extract records this acquisition "
                         "run as its source (S.6: the identity asked for)")
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
    crt = sub.add_parser("check-raw-target",
                         help="check Drive credentials and the raw archive root only")
    crt.add_argument("--pointers", action="store_true",
                     help="also sweep every run's records for pointer "
                          "integrity (R11); copies no payload bytes")
    rs = with_source(sub.add_parser(
        "restore", help="copy an archived run from Drive into out/_raw/ "
                        "(with its pointer target, as siblings)"))
    rs.add_argument("--run-id", default=None, metavar="ID",
                    help="the archived run; empty means the newest one")
    rs.add_argument("--resolve-only", action="store_true",
                    help="print which run and which payload folder; copy "
                         "nothing")
    ar = with_source(sub.add_parser(
        "archive", help="copy a finished acquisition run to Drive, immutably"))
    ar.add_argument("--run-id", required=True, metavar="ID",
                    help="the run directory name under out/_raw/<source>/")
    ar.add_argument("--dry-run", action="store_true",
                    help="stage and report, copy nothing")
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
        "acquire": cmd_acquire,
        "publish": cmd_publish, "run": cmd_run, "reindex": cmd_reindex,
        "check-publish": cmd_check_publish,
        "check-raw-target": cmd_check_raw_target, "archive": cmd_archive,
        "restore": cmd_restore,
    }[args.cmd]

    try:
        return handler(sources, args)
    except Exception:
        traceback.print_exc()
        return 1
