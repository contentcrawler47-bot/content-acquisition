#!/usr/bin/env python3
"""
Check a stored acquisition run: was it finished, is it intact, is it whole.

    python3 tools/check_raw.py out/_raw/bian-v14/<run-id>
    python3 tools/check_raw.py out/_raw/bian-v14/<run-id> --require-complete

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
    args = ap.parse_args()
    run_dir: Path = args.run_dir
    failures, warnings = [], []

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
    v = A.verify_run(run_dir)
    print(f"  INTACT     {v['files_verified']} of {v['files_listed']} listed "
          f"files verified; {v['artifacts_verified']} of "
          f"{v['artifacts_stored']} stored artifacts match their manifest "
          f"digest")
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

    prov = run.get("provenance") or {}
    print(f"\n  provenance where={prov.get('where')} run={prov.get('run_id')} "
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
