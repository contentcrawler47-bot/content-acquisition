#!/usr/bin/env python3
"""
Check every workflow against the action-level observability contract.

Clauses A.3 to A.6 of the observability design are structural properties of
the workflow files, so they can be checked before a change is applied rather
than discovered after it has run. This is that check.

    python3 tools/check_workflows.py [--dir DIR]

Exit 0 conformant, 1 with findings. Run by **Verify repo contents** and by
the changeset dry-run, which composes the tree a changeset WOULD produce and
checks that, so a changeset that unpins an action or adds a `pull_request`
trigger is refused before anything is written.

What is checked
---------------

A.3  Workflow-level `permissions:` present. `contents: write` only where
     declared below. Drive secrets referenced only by jobs that declare
     `environment: drive`. `persist-credentials: false` on every checkout
     except the jobs that push, declared below.
A.4  Triggers drawn only from workflow_dispatch, schedule, workflow_call and
     workflow_run; a `# TRIGGERS:` line in the header states it.
A.5  Every `uses:` a 40-hex commit SHA with a `# vX.Y.Z` comment; runner
     image pinned, never `-latest`. First step of every job is harden-runner.
A.6  `timeout-minutes` on every job; workflow-level `concurrency`.
A.12 No `upload-artifact` whose path is Class B (run directories, extracts,
     diagnostics), except where declared below.
A.7  No `set -x`, which would echo Class-B values into a public log.

Exceptions are DECLARED, and the declarations are checked in both directions.
An exception naming something that no longer exists is itself a finding: a
carried exception is how a rule quietly stops applying to the thing it was
written for. Each one names the changeset that removes it.

This check does not read `SETUP.md` or any prose description of what CI does.
Prose said no artifacts were uploaded while three workflows uploaded payload
bytes for months. The workflow files are the artefact; this reads those.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    print("ERROR: PyYAML is required. Install it with:")
    print("    python3 -m pip install --require-hashes -r requirements.txt")
    sys.exit(1)

# --- declared exceptions -------------------------------------------------
# Each is a measurement with an expiry, not a rule. Removing the thing an
# exception covers must also remove the exception, and the cross-check below
# makes that mandatory rather than hoped for.

# Workflows permitted `contents: write`, with why.
CONTENTS_WRITE_OK = {
    "apply-changeset.yml": "commits the applied changeset; the whole point",
    # Moves to the Report workflow at 074, which becomes the sole committer
    # to .runs/ (D-8). Until then the keep-alive lives here.
    "landscape-bian-v14.yml": "keep-alive timestamp commit; moves to Report at 074",
}

# job key "<file>:<job>" -> why that job's checkout keeps its credentials.
PERSIST_CREDENTIALS_OK = {
    "apply-changeset.yml:apply": "pushes the applied changeset",
    "landscape-bian-v14.yml:assemble": "pushes the keep-alive stamp",
}

# Artifact uploads of Class-B content still in the repo. All three go at 072,
# when the Actions cache replaces the artifact as inter-job transport (D-3).
CLASS_B_ARTIFACTS_OK = {
    "acquire-bian-v14.yml:raw-${{ env.SOURCE_ID }}-${{ env.RUN_ID }}":
        "transport between acquire and archive; becomes a cache key at 072",
    "extract-bian-v14.yml:raw-${{ env.SOURCE_ID }}-${{ env.RUN_ID }}":
        "this workflow stops acquiring at 072 (R10)",
    "extract-bian-v14.yml:extract-${{ env.SOURCE_ID }}":
        "read by Render source: stored; becomes a cache restore at 072",
}

ALLOWED_TRIGGERS = {"workflow_dispatch", "schedule", "workflow_call", "workflow_run"}

# Paths whose contents are Class B: response bodies, parsed text, models.
CLASS_B_PATH = re.compile(r"RUN_DIR|out/_raw|out/_extract|(^|/)diag(/|$)")

SHA_USES = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"#\s*v\d+\.\d+\.\d+")
HARDEN_RUNNER = "step-security/harden-runner@"


def load(path: Path):
    """Parse a workflow. `on:` is YAML's boolean True, which is why this
    exists rather than a plain safe_load at each call site."""
    doc = yaml.safe_load(path.read_text())
    triggers = doc.get(True, doc.get("on"))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {t: None for t in triggers}
    return doc, triggers or {}


def check_file(path: Path, findings: list[str], used: set[str]) -> None:
    name = path.name
    text = path.read_text()
    doc, triggers = load(path)

    def bad(msg: str) -> None:
        findings.append(f"{name}: {msg}")

    # --- A.4 triggers ----------------------------------------------------
    for t in triggers:
        if str(t).startswith("pull_request"):
            bad(f"trigger {t} is never permitted (D-14: cache privacy)")
        elif t not in ALLOWED_TRIGGERS:
            bad(f"trigger {t} is not in the allowed set "
                f"({', '.join(sorted(ALLOWED_TRIGGERS))})")
    if not re.search(r"^# TRIGGERS:", text, re.M):
        bad("header has no '# TRIGGERS:' line stating the trigger rule (A.4)")

    # --- A.3 permissions -------------------------------------------------
    perms = doc.get("permissions")
    if perms is None:
        bad("no workflow-level permissions: block (A.3)")
    elif isinstance(perms, dict) and perms.get("contents") == "write":
        if name in CONTENTS_WRITE_OK:
            used.add(f"cw:{name}")
        else:
            bad("contents: write is not declared for this workflow (A.3)")

    # --- A.6 concurrency -------------------------------------------------
    if "concurrency" not in doc:
        bad("no workflow-level concurrency: group (A.6)")

    for job_name, job in (doc.get("jobs") or {}).items():
        jk = f"{name}:{job_name}"

        def jbad(msg: str) -> None:
            findings.append(f"{jk}: {msg}")

        # --- A.6 budgets -------------------------------------------------
        if "timeout-minutes" not in job:
            jbad("no timeout-minutes (A.6)")

        # --- A.5 pinned runner -------------------------------------------
        runner = job.get("runs-on")
        if isinstance(runner, str) and runner.endswith("-latest"):
            jbad(f"runner {runner} is not pinned; use an explicit image (A.5)")

        steps = job.get("steps") or []

        # --- A.5 harden-runner first -------------------------------------
        if not steps:
            jbad("no steps")
        elif HARDEN_RUNNER not in str(steps[0].get("uses", "")):
            jbad("first step is not harden-runner (A.15)")
        else:
            policy = (steps[0].get("with") or {}).get("egress-policy")
            if policy not in ("audit", "block"):
                jbad(f"harden-runner egress-policy is {policy!r}, "
                     "expected audit or block (A.15)")

        # --- A.3 Drive secrets are environment-scoped ---------------------
        job_text = yaml.safe_dump(job)
        if "secrets.GDRIVE_" in job_text and job.get("environment") != "drive":
            jbad("references GDRIVE_* secrets without environment: drive "
                 "(A.3, D-11)")
        if job.get("environment") == "drive" and "secrets.GDRIVE_" not in job_text:
            jbad("declares environment: drive but references no Drive secret")

        for step in steps:
            uses = str(step.get("uses", "")).strip()
            if uses:
                # --- A.5 SHA pinning ---------------------------------------
                # Local composite actions (./.github/actions/...) are in-repo
                # and manifest-covered, so they need no pin. The version
                # comment beside each pin is checked textually further down,
                # because YAML parsing discards comments.
                if not uses.startswith("./") and not SHA_USES.match(uses):
                    jbad(f"uses: {uses} is not a 40-hex commit SHA (A.5)")

            # --- A.3 checkout credentials ----------------------------------
            if uses.startswith("actions/checkout@"):
                pc = (step.get("with") or {}).get("persist-credentials")
                if pc is not False:
                    if jk in PERSIST_CREDENTIALS_OK:
                        used.add(f"pc:{jk}")
                    else:
                        jbad("checkout without persist-credentials: false (A.3)")

            # --- A.12 no Class-B artifact ----------------------------------
            if uses.startswith("actions/upload-artifact@"):
                with_ = step.get("with") or {}
                art = f"{name}:{with_.get('name', '?')}"
                if CLASS_B_PATH.search(str(with_.get("path", ""))):
                    if art in CLASS_B_ARTIFACTS_OK:
                        used.add(f"ab:{art}")
                    else:
                        jbad(f"uploads Class-B content as artifact "
                             f"{with_.get('name')} (A.12); artifacts on a "
                             "public repo are downloadable by any account")

            # --- A.7 log discipline ----------------------------------------
            if re.search(r"^\s*set -x", str(step.get("run", "")), re.M):
                jbad("set -x echoes values into a public log (A.7)")

    # --- A.5 version comment beside every pin, checked textually ----------
    # The comment is what makes a pin readable; YAML drops comments, so this
    # is a line check rather than a document one.
    for i, line in enumerate(text.split("\n"), 1):
        m = re.search(r"uses:\s*(\S+@[0-9a-f]{40})", line)
        if m and not VERSION_COMMENT.search(line):
            bad(f"line {i}: {m.group(1)} has no '# vX.Y.Z' comment (A.5)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dir", default=".github/workflows",
                    help="directory of workflow files to check")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory")
        return 1
    files = sorted(list(root.glob("*.yml")) + list(root.glob("*.yaml")))
    if not files:
        print(f"ERROR: no workflow files in {root}")
        return 1

    print("=" * 70)
    print("  Workflow conformance (A.3-A.6, A.7, A.12, A.15)")
    print("=" * 70)
    print(f"\n  checking {len(files)} workflows in {root}\n")

    findings: list[str] = []
    used: set[str] = set()
    for f in files:
        try:
            check_file(f, findings, used)
        except Exception as e:                       # noqa: BLE001
            findings.append(f"{f.name}: could not be checked: {e}")

    # --- exceptions must still describe something ------------------------
    # An exception for a thing that is gone silently widens the rule the next
    # time something similar appears. Fail on the stale declaration itself.
    for k, why in CONTENTS_WRITE_OK.items():
        if f"cw:{k}" not in used:
            findings.append(
                f"stale exception: {k} no longer takes contents: write "
                f"({why}) -- remove it from CONTENTS_WRITE_OK")
    for k, why in PERSIST_CREDENTIALS_OK.items():
        if f"pc:{k}" not in used:
            findings.append(
                f"stale exception: {k} no longer keeps checkout credentials "
                f"({why}) -- remove it from PERSIST_CREDENTIALS_OK")
    for k, why in CLASS_B_ARTIFACTS_OK.items():
        if f"ab:{k}" not in used:
            findings.append(
                f"stale exception: {k} is no longer uploaded ({why}) "
                "-- remove it from CLASS_B_ARTIFACTS_OK")

    if findings:
        print(f"  [FAIL] {len(findings)} finding(s)\n")
        for f_ in findings:
            print(f"    {f_}")
        print("\n" + "=" * 70)
        print("  RESULT: NOT CONFORMANT")
        print("=" * 70 + "\n")
        return 1

    print(f"  [PASS] {len(files)} workflows conformant")
    print(f"  {len(CONTENTS_WRITE_OK)} contents:write, "
          f"{len(PERSIST_CREDENTIALS_OK)} credentialed checkout, "
          f"{len(CLASS_B_ARTIFACTS_OK)} Class-B artifact exception(s), "
          "each still in use")
    print("\n" + "=" * 70)
    print("  RESULT: CONFORMANT")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
