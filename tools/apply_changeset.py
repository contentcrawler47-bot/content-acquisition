#!/usr/bin/env python3
"""
Apply a changeset zip to the repository, then verify the result.

A changeset bundles the files to write, the operations to perform, and the
expected end state, so applying a revision is one upload instead of a dozen
manual edits.

    changeset.zip
    ├── CHANGESET.json      what to do
    ├── MANIFEST.sha256     what the repo must look like afterwards
    └── files/              new and updated content, mirroring repo paths
        ├── tools/thing.py
        └── core/other.py

CHANGESET.json:

    {
      "description": "Fix duplicate participant aliases",
      "operations": [
        {"op": "add",    "path": "tools/new.py"},
        {"op": "update", "path": "core/existing.py"},
        {"op": "delete", "path": "tools/obsolete.py"},
        {"op": "rename", "from": "tools/old.py", "to": "tools/new_name.py"}
      ]
    }

Verification runs BEFORE any commit. If the resulting tree does not match
MANIFEST.sha256 the changes are left uncommitted and the run fails, so a bad
changeset cannot land.

    python3 tools/apply_changeset.py changesets/pending.zip [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALID_OPS = {"add", "update", "delete", "rename"}


def fail(msg: str) -> int:
    print(f"\n  FAILED: {msg}\n", flush=True)
    return 1


def safe_path(rel: str) -> Path:
    """Resolve a changeset path inside the repo, refusing escapes.

    A zip can contain '../' or absolute members (zip-slip). Since this writes
    to a real repository and then commits, path containment is checked rather
    than assumed.
    """
    rel = rel.replace("\\", "/").strip()
    if not rel:
        raise ValueError("empty path")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise ValueError(f"absolute path not allowed: {rel!r}")
    if ".." in Path(rel).parts:
        raise ValueError(f"parent traversal not allowed: {rel!r}")
    target = (REPO / rel).resolve()
    if not str(target).startswith(str(REPO) + "/") and target != REPO:
        raise ValueError(f"path escapes the repository: {rel!r}")
    # Guard the .git directory itself — matching on prefix would also catch
    # .github, which changesets legitimately need to write to.
    if Path(rel).parts and Path(rel).parts[0] == ".git":
        raise ValueError(f"writing inside .git is not allowed: {rel!r}")
    return target


def load(zip_path: Path, workdir: Path):
    if not zip_path.is_file():
        raise FileNotFoundError(f"{zip_path} not found")
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.startswith("/") or ".." in Path(member).parts:
                raise ValueError(f"unsafe zip member: {member!r}")
        z.extractall(workdir)

    # Tolerate a single wrapper directory, which is what most zip tools produce.
    entries = [p for p in workdir.iterdir() if not p.name.startswith("__")]
    if len(entries) == 1 and entries[0].is_dir() and \
            not (workdir / "CHANGESET.json").is_file():
        return entries[0]
    return workdir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", help="path to the changeset zip")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    workdir = REPO / ".changeset_tmp"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir()

    try:
        root = load(Path(args.zip), workdir)
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail(f"could not read the changeset: {e}")

    cs_file = root / "CHANGESET.json"
    manifest_file = root / "MANIFEST.sha256"
    files_dir = root / "files"

    if not cs_file.is_file():
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("CHANGESET.json missing from the zip")
    if not manifest_file.is_file():
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("MANIFEST.sha256 missing from the zip — the end state "
                    "must be declared so the result can be verified")

    try:
        cs = json.loads(cs_file.read_text(encoding="utf-8"))
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return fail(f"CHANGESET.json is not valid JSON: {e}")

    ops = cs.get("operations") or []
    print("=" * 70)
    print(f"  CHANGESET: {cs.get('description', '(no description)')}")
    print("=" * 70)
    print(f"\n  {len(ops)} operation(s)"
          + ("   [DRY RUN — nothing will be written]" if args.dry_run else ""),
          flush=True)

    # ---- validate every operation before touching anything ----------
    planned, problems = [], []
    for i, op in enumerate(ops, 1):
        kind = (op.get("op") or "").lower()
        if kind not in VALID_OPS:
            problems.append(f"operation {i}: unknown op {kind!r}")
            continue
        try:
            if kind == "rename":
                src, dst = op.get("from"), op.get("to")
                if not src or not dst:
                    problems.append(f"operation {i}: rename needs 'from' and 'to'")
                    continue
                s, d = safe_path(src), safe_path(dst)
                if not s.is_file():
                    problems.append(f"rename source missing in repo: {src}")
                    continue
                planned.append(("rename", src, dst, s, d))
            elif kind == "delete":
                path = op.get("path")
                t = safe_path(path)
                if not t.is_file():
                    print(f"    note: {path} already absent, delete is a no-op",
                          flush=True)
                    continue
                planned.append(("delete", path, None, t, None))
            else:  # add / update
                path = op.get("path")
                t = safe_path(path)
                src = files_dir / path
                if not src.is_file():
                    problems.append(f"{kind} {path}: not present under files/ "
                                    f"in the zip")
                    continue
                if kind == "add" and t.is_file():
                    print(f"    note: {path} already exists, treating add "
                          f"as update", flush=True)
                if kind == "update" and not t.is_file():
                    print(f"    note: {path} absent, treating update as add",
                          flush=True)
                planned.append((kind, path, None, t, src))
        except ValueError as e:
            problems.append(f"operation {i}: {e}")

    # every payload file must be accounted for by an operation
    if files_dir.is_dir():
        declared = {p for k, p, _o, _t, _s in planned if k in ("add", "update")}
        for f in sorted(files_dir.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(files_dir))
                if rel not in declared:
                    problems.append(f"files/{rel} is in the zip but no "
                                    f"operation refers to it")

    if problems:
        print("\n  Problems:", flush=True)
        for p in problems:
            print(f"    - {p}", flush=True)
        shutil.rmtree(workdir, ignore_errors=True)
        return fail(f"{len(problems)} problem(s); nothing was changed")

    print("\n  Plan:", flush=True)
    for kind, a, b, _t, _s in planned:
        print(f"    {kind:<7} {a}" + (f"  ->  {b}" if b else ""), flush=True)

    touches_workflows = any(
        str(x).startswith(".github/workflows/")
        for k, a, b, _t, _s in planned for x in (a, b) if x)
    if touches_workflows:
        print("\n  NOTE: this changeset modifies files under "
              ".github/workflows/.", flush=True)
        print("  GITHUB_TOKEN cannot push workflow changes; the workflow needs "
              "a PAT", flush=True)
        print("  with 'workflow' scope in CHANGESET_TOKEN, or the push will be "
              "rejected.", flush=True)

    if args.dry_run:
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n  Dry run complete, nothing written.\n", flush=True)
        return 0

    # ---- apply ------------------------------------------------------
    print("\n  Applying:", flush=True)
    for kind, a, b, target, src in planned:
        if kind == "delete":
            target.unlink()
            print(f"    deleted  {a}", flush=True)
        elif kind == "rename":
            src_path, dst_path = target, b and safe_path(b)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src_path), str(dst_path))
            print(f"    renamed  {a} -> {b}", flush=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            print(f"    wrote    {a}  ({target.stat().st_size} bytes)",
                  flush=True)

    shutil.copyfile(manifest_file, REPO / "MANIFEST.sha256")
    print(f"    wrote    MANIFEST.sha256", flush=True)
    shutil.rmtree(workdir, ignore_errors=True)

    # ---- verify BEFORE anything is committed ------------------------
    print("\n" + "=" * 70)
    print("  VERIFYING the resulting tree")
    print("=" * 70, flush=True)
    rc = subprocess.run(
        [sys.executable, str(REPO / "tools" / "repo_manifest.py"),
         "--verify", "--exact"],
        cwd=REPO).returncode
    if rc != 0:
        return fail("the repo does not match the changeset's MANIFEST.sha256. "
                    "Changes are left uncommitted — nothing will be pushed.")

    print("\n  Changeset applied and verified.\n", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
