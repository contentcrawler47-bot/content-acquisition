#!/usr/bin/env python3
"""
Check a stored acquisition run: was it finished, is it intact, is it whole.

    python3 tools/check_raw.py out/_raw/bian-v14/<run-id>
    python3 tools/check_raw.py out/_raw/bian-v14/<run-id> --require-complete
    python3 tools/check_raw.py out/_raw/bian-v14 --pointers

Three questions, in the order they can be answered:

  FINISHED   RAW.sha256 exists. It is written last, so its absence means the
             run was interrupted -- a job timeout, a killed runner -- and
             run.json's `state` cannot be trusted past "running".
  INTACT     every file the sidecar names is present with its digest, nothing
             is present that the sidecar does not name, and every artifact
             the manifest says was stored is on disk with the manifest's
             digest. Storage rots and syncs mis-fire; this is the check that
             notices (I2.1), and it is the same check an archive should run
             on a schedule.
  WHOLE      run.json says `complete`: every declared artifact was stored.
             `partial` and `failed` are reported with the reason and the
             per-scope counts. --require-complete makes them fail; without
             it a partial run is a warning, because the run itself is still
             evidence worth keeping.

A de-duplicated archive (SAME_AS.json, no payload.zip) is read through the
SIBLING folder it names -- download both folders side by side -- and the
check says so. Its payload files verify against its OWN sidecar, so a target
that is absent or wrong is reported as absent or mismatched files, never as a
pass. When ARCHIVED.json is present, the run digest it recorded is compared
with the one recomputed here; a difference is a finding.

`--pointers` takes an ARCHIVE ROOT instead -- run folders side by side, as
downloaded, or their record files alone -- and sweeps every pointer in it:
target present as a sibling, not itself a pointer, same raw digest as the
pointer recomputes and records (R11). It reads no payload bytes, so the
records of the whole archive suffice; **Check raw archive target** runs it
that way from CI.

Counts carry denominators throughout. Prints paths, statuses and digests only,
never content: logs on a public repo are world-readable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bianlib import acquire as A  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--require-complete", action="store_true",
                    help="fail unless run.json state is 'complete'")
    ap.add_argument("--pointers", action="store_true",
                    help="treat run_dir as an archive ROOT and sweep every "
                         "pointer in it (R11); reads records only")
    args = ap.parse_args()
    run_dir: Path = args.run_dir
    failures, warnings = [], []

    if args.pointers:
        from core.cli import print_pointer_sweep
        print("=" * 70)
        print(f"  Pointer sweep: {run_dir}")
        print("=" * 70 + "\n")
        clean = print_pointer_sweep(A.check_pointers(run_dir))
        print("\n  RESULT: " + ("PASS" if clean else "FAIL"))
        return 0 if clean else 1

    print("=" * 70)
    print(f"  Acquisition run check: {run_dir}")
    print("=" * 70)

    if not run_dir.is_dir():
        print(f"\n  [FAIL] {run_dir} is not a directory")
        return 2

    # -- FINISHED ---------------------------------------------------------
    run_path = run_dir / A.RUN_FILE
    run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
    if not run:
        failures.append("run.json missing: the run never started writing")
    if not (run_dir / A.SIDECAR_FILE).is_file():
        failures.append(
            f"{A.SIDECAR_FILE} missing: the run was not finished "
            f"(run.json state: {run.get('state', '(none)')!r}). Not evidence.")
        _report(failures, warnings)
        return 1
    print(f"\n  FINISHED   {A.SIDECAR_FILE} present; run.json state "
          f"{run.get('state')!r}")

    # -- INTACT -----------------------------------------------------------
    try:
        v = A.verify_run(run_dir)
    except A.RunUnreadable as e:
        failures.append(str(e))
        _report(failures, warnings)
        return 1
    print(f"  INTACT     {v['files_verified']} of {v['files_listed']} listed "
          f"files verified"
          + (f" ({v['files_compressed']} read from the archive form)"
             if v["files_compressed"] else "")
          + f"; {v['artifacts_verified']} of {v['artifacts_stored']} stored "
          f"artifacts match their manifest digest")
    if v["payload_via"]:
        print(f"  POINTER    {A.SAME_AS_FILE} names run {v['payload_via']}; "
              f"payload read from that sibling folder")
    for rel in v["files_absent"]:
        failures.append(f"listed in sidecar but absent: {rel}")
    for rel in v["files_mismatched"]:
        failures.append(f"digest differs from sidecar: {rel}")
    for rel in v["files_stray"]:
        failures.append(f"present but not in sidecar: {rel}")
    for rel in v["artifacts_mismatched"]:
        failures.append(f"stored artifact does not match its manifest "
                        f"record: {rel}")

    # -- WHOLE ------------------------------------------------------------
    state = run.get("state")
    print(f"  WHOLE      state {state!r}"
          + (f" -- {run.get('reason')}" if run.get("reason") else ""))
    for scope, o in (run.get("outcomes") or {}).items():
        print(f"             {scope:<9} declared {o['declared']:>5}  stored "
              f"{o['stored']:>5}  missing {o['missing']:>3}  failed "
              f"{o['failed']:>3}  {o['bytes'] / 1024 / 1024:>7.1f} MB")
    if state != "complete":
        msg = f"run state is {state!r}: {run.get('reason') or 'no reason recorded'}"
        (failures if args.require_complete else warnings).append(msg)

    # The run's identity, over payload lines only: equal for two runs that
    # fetched the same bytes, whatever their timestamps. This is what
    # de-duplication compares; the digest of RAW.sha256 as a file is not.
    digest = A.run_digest(run_dir)
    print(f"\n  raw digest {digest[:16] or '(none)'}  "
          f"(payload lines only; excludes {', '.join(A.RECORD_FILES)})")
    # The archive's record of that digest, checked rather than trusted, and
    # the pointer's, which must equal both.
    marker_path = run_dir / A.MARKER_FILE
    if marker_path.is_file():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        recorded = marker.get("run_digest")
        if recorded and recorded != digest:
            failures.append(f"{A.MARKER_FILE} records run digest "
                            f"{recorded[:16]}, recomputed {digest[:16]}")
        if marker.get("same_as") and marker["same_as"] != v["payload_via"]:
            failures.append(f"{A.MARKER_FILE} says same_as "
                            f"{marker['same_as']} but {A.SAME_AS_FILE} "
                            f"{'names ' + v['payload_via'] if v['payload_via'] else 'is absent'}")
    if v["payload_via"]:
        ptr = A.pointer_of(run_dir) or {}
        if ptr.get("run_digest") and ptr["run_digest"] != digest:
            failures.append(f"{A.SAME_AS_FILE} records run digest "
                            f"{ptr['run_digest'][:16]}, this run's is "
                            f"{digest[:16]}")
        target = run_dir.parent / v["payload_via"]
        if (target / A.SIDECAR_FILE).is_file():
            tdigest = A.run_digest(target)
            if tdigest != digest:
                failures.append(f"pointed-to run {v['payload_via']} has raw "
                                f"digest {tdigest[:16]}, not {digest[:16]}")
            if A.pointer_of(target):
                failures.append(f"pointed-to run {v['payload_via']} is itself "
                                f"a pointer; pointers are one hop")
    prov = run.get("provenance") or {}
    print(f"  provenance where={prov.get('where')} run={prov.get('run_id')} "
          f"sha={(prov.get('commit_sha') or '')[:12] or '(none)'} "
          f"repo_digest={prov.get('repo_digest') or '(none)'}"
          + (f" manifest_digest={prov.get('manifest_digest')}"
             if prov.get("manifest_digest")
             and prov.get("manifest_digest") != prov.get("repo_digest")
             else ""))
    if prov.get("repo_digest") and prov.get("manifest_digest") and \
            prov["repo_digest"] != prov["manifest_digest"]:
        warnings.append("run was made on a tree that does not match its "
                        "MANIFEST.sha256 (repo_digest != manifest_digest)")
    robots = (run.get("policy") or {}).get("robots") or {}
    print(f"  robots     checked={robots.get('checked')} "
          f"rule={robots.get('rule') or 'none'}")

    _report(failures, warnings)
    return 1 if failures else 0


def _report(failures: list, warnings: list) -> None:
    print()
    for w in warnings:
        print(f"  [WARN] {w}")
    for f in failures:
        print(f"  [FAIL] {f}")
    print("\n  RESULT: " + ("FAIL" if failures else
                            "PASS with warnings" if warnings else "PASS"))


if __name__ == "__main__":
    sys.exit(main())
