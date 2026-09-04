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
      "base_digest": "6cec645aaffc6a4f",
      "operations": [
        {"op": "add",    "path": "tools/new.py"},
        {"op": "update", "path": "core/existing.py"},
        {"op": "delete", "path": "tools/obsolete.py"},
        {"op": "rename", "from": "tools/old.py", "to": "tools/new_name.py"}
      ]
    }

`base_digest` is the repo state this changeset expects to start FROM. It is
checked before anything is written, so applying changesets out of order is
rejected up front with a clear message rather than surfacing later as a
confusing list of missing files.

Verification runs BEFORE any commit. If the resulting tree does not match
MANIFEST.sha256 the changes are left uncommitted and the run fails, so a bad
changeset cannot land.

Workflow conformance runs BEFORE anything is written, on the workflow set the
changeset WOULD produce rather than the one on disk — checking the repo as it
stands would pass a changeset whose unpinned action is still inside the zip.
A changeset that unpins an action, adds a `pull_request` trigger or uploads
harvested bytes as an artifact is therefore refused at `--dry-run`. When the
changeset updates `tools/check_workflows.py` itself, the shipped copy does the
checking, so a rule added in a changeset applies to that same changeset.

    python3 tools/apply_changeset.py changesets/pending.zip [--dry-run]

`skill_impact` is REQUIRED. It declares what this change teaches, so a repo
change and the instructions for operating the repo cannot move apart:

    "skill_impact": [
      {"skill": "content-acquisition", "change": "add read-the-artefact rule"}
    ]

An EMPTY LIST is a valid answer and means "asked, and nothing". An absent key
is an unanswered question, which is what rots. The declaration is cross-checked
against the operations in both directions: a skill named here must be touched,
and a file touched under skills/ must be named here. A changeset therefore
cannot alter a skill quietly, and a declared impact cannot fail to materialise.

--reconcile makes application idempotent. It ignores base_digest, writes every
payload file whether or not it already matches, treats an already-performed
delete or rename as success, removes anything the manifest does not list, and
then verifies. Applying once, twice, or after a half-finished run converges on
the same state, so a partial or lost application is repaired by re-running the
same zip. It repairs a repo that was on the correct base; it cannot rebuild
from an arbitrary state, because the zip carries content only for the files it
changes. For that, cut a changeset containing every file with a null
base_digest.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALID_OPS = {"add", "update", "delete", "rename"}
SKILLS_DIR = "skills"
# <!-- skill: content-acquisition v8 | repo: changeset 023 -->
SKILL_MARKER = re.compile(r"<!--\s*skill:\s*(\S+)\s+v(\d+)")


def current_digest() -> str:
    """The repo's digest right now, via the same code repo_manifest uses."""
    spec = importlib.util.spec_from_file_location(
        "repo_manifest", REPO / "tools" / "repo_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.digest_of(mod.build())


def manifest_paths(manifest_file: Path) -> set[str]:
    """Every path the manifest lists. Format: exact  normalised  bytes  path."""
    paths = set()
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 3)
        if len(parts) == 4:
            paths.add(parts[3])
    return paths


def skill_of(path: str) -> str | None:
    """The skill a repo path belongs to, or None. 'skills/x/SKILL.md' -> 'x'."""
    parts = Path(path).parts
    if len(parts) >= 2 and parts[0] == SKILLS_DIR:
        return parts[1]
    return None


def skill_version(name: str) -> str:
    """The version in a skill's marker, for the reinstall notice."""
    f = REPO / SKILLS_DIR / name / "SKILL.md"
    if not f.is_file():
        return "(no SKILL.md)"
    m = SKILL_MARKER.search(f.read_text(encoding="utf-8", errors="replace"))
    return f"v{m.group(2)}" if m else "(unversioned)"


WORKFLOWS = ".github/workflows"


def check_workflows_after(planned, workdir: Path) -> bool:
    """Run tools/check_workflows.py over the workflow set this changeset would
    leave behind: the repo's current files, with the changeset's additions,
    updates, renames and deletions applied in a temp directory.

    Returns True when conformant or when there is nothing to check. A
    changeset that unpins an action or adds a `pull_request` trigger is
    therefore refused at dry-run, before a single file is written.
    """
    checker = REPO / "tools" / "check_workflows.py"
    if not checker.exists():
        return True                       # predates 071; nothing to enforce

    staged = workdir / "_workflow_check"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(parents=True)

    src = REPO / WORKFLOWS
    if src.is_dir():
        for f in src.iterdir():
            if f.is_file():
                shutil.copyfile(f, staged / f.name)

    touched = False
    for kind, a, b, target, source in planned:
        for rel in (a, b):
            if rel and str(rel).startswith(WORKFLOWS + "/"):
                touched = True
        if kind == "delete" and str(a).startswith(WORKFLOWS + "/"):
            (staged / Path(a).name).unlink(missing_ok=True)
        elif kind == "rename":
            if str(a).startswith(WORKFLOWS + "/"):
                (staged / Path(a).name).unlink(missing_ok=True)
            if b and str(b).startswith(WORKFLOWS + "/"):
                shutil.copyfile(target, staged / Path(b).name)
        elif str(a).startswith(WORKFLOWS + "/"):
            shutil.copyfile(source, staged / Path(a).name)

    # The checker itself may be what this changeset updates; run the version
    # the changeset ships, not the one on disk, or a rule added in the same
    # changeset would not apply to it.
    for kind, a, _b, _t, source in planned:
        if a == "tools/check_workflows.py" and kind in ("add", "update"):
            checker = source
            touched = True
            break

    if not touched:
        shutil.rmtree(staged, ignore_errors=True)
        return True

    print("\n" + "=" * 70, flush=True)
    print("  WORKFLOW CONFORMANCE of the resulting tree", flush=True)
    print("=" * 70, flush=True)
    rc = subprocess.run(
        [sys.executable, str(checker), "--dir", str(staged)],
        cwd=REPO).returncode
    shutil.rmtree(staged, ignore_errors=True)
    return rc == 0


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
    ap.add_argument("--reconcile", action="store_true",
                    help="idempotent repair: ignore base_digest, rewrite every "
                         "payload file, tolerate already-done operations, and "
                         "remove anything absent from the manifest")
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

    # ---- does this changeset belong on the current state? -----------
    expected_base = cs.get("base_digest")
    if args.reconcile:
        print("\n  RECONCILE MODE — base_digest is not checked.", flush=True)
        print("  The manifest is the target; the repo is made to match it.",
              flush=True)
        expected_base = None
    if expected_base:
        try:
            actual_base = current_digest()
        except Exception as e:
            actual_base = None
            print(f"\n  could not compute the current digest: "
                  f"{type(e).__name__}: {e}", flush=True)
        print(f"\n  base digest expected : {expected_base}", flush=True)
        print(f"  base digest actual   : {actual_base or '(unknown)'}",
              flush=True)
        if actual_base and actual_base != expected_base:
            shutil.rmtree(workdir, ignore_errors=True)
            print("\n  The repo is not in the state this changeset expects.",
                  flush=True)
            print("  Either an earlier changeset has not been applied, or one "
                  "has been", flush=True)
            print("  applied that this changeset does not know about. Nothing "
                  "was changed.", flush=True)
            print("\n  Run 'Verify repo contents' to see the current state, "
                  "then apply", flush=True)
            print("  changesets in the order they were issued.", flush=True)
            return fail(f"base digest mismatch: expected {expected_base}, "
                        f"found {actual_base}")
    elif not args.reconcile:
        print("\n  NOTE: this changeset declares no base_digest, so the "
              "starting state", flush=True)
        print("  is not checked. Ordering errors will only surface at "
              "verification.", flush=True)
    print(f"\n  {len(ops)} operation(s)"
          + ("   [DRY RUN — nothing will be written]" if args.dry_run else ""),
          flush=True)

    # ---- the change and its skill consequences are one unit ---------
    # Declared BEFORE the operations are planned, because a missing
    # declaration should stop the run whatever else is wrong with it.
    if "skill_impact" not in cs:
        shutil.rmtree(workdir, ignore_errors=True)
        print("\n  CHANGESET.json has no 'skill_impact' key.", flush=True)
        print("  Every changeset must declare what it teaches. If it teaches "
              "nothing,", flush=True)
        print('  say so explicitly with "skill_impact": [] — an absent key is '
              "an", flush=True)
        print("  unanswered question, not an answer.", flush=True)
        return fail("skill_impact is required")

    impact = cs.get("skill_impact")
    if not isinstance(impact, list):
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("skill_impact must be a list (use [] for none)")

    impact_skills = set()
    impact_problems = []
    for i, entry in enumerate(impact, 1):
        if not isinstance(entry, dict) or not entry.get("skill"):
            impact_problems.append(f"skill_impact {i}: needs a 'skill' name")
            continue
        if not entry.get("change"):
            impact_problems.append(
                f"skill_impact {i}: needs a 'change' saying what it teaches")
            continue
        impact_skills.add(entry["skill"])

    print("\n  SKILL IMPACT:", flush=True)
    if impact:
        for entry in impact:
            if isinstance(entry, dict):
                print(f"    {entry.get('skill', '?')} — "
                      f"{entry.get('change', '?')}", flush=True)
    else:
        print("    none declared", flush=True)

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
                    if args.reconcile and d.is_file():
                        print(f"    note: {src} -> {dst} already done",
                              flush=True)
                        continue
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

    # ---- cross-check the declaration against the operations ---------
    # Both directions. Direction 2 is the one that matters: it makes it
    # impossible to change a skill without saying what it teaches.
    touched_skills = set()
    for kind, a, b, _t, _s in planned:
        for path in (a, b):
            if path:
                name = skill_of(path)
                if name:
                    touched_skills.add(name)

    for name in sorted(impact_skills - touched_skills):
        impact_problems.append(
            f"skill_impact names '{name}' but no operation touches "
            f"{SKILLS_DIR}/{name}/ — the declared change did not materialise")
    for name in sorted(touched_skills - impact_skills):
        impact_problems.append(
            f"operations modify {SKILLS_DIR}/{name}/ but skill_impact does not "
            f"mention '{name}' — a skill cannot be changed silently")

    problems = impact_problems + problems

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

    # ---- workflow conformance, on the tree this WOULD produce -------
    # Checking the repo as it stands would pass a changeset that unpins an
    # action, because the unpinning is in the zip and not yet on disk. So
    # compose the post-application set of workflow files in a temp directory
    # and check that. Nothing is written to the repo either way, which is
    # what lets this run in a dry run and refuse before any damage.
    if not check_workflows_after(planned, workdir):
        shutil.rmtree(workdir, ignore_errors=True)
        return fail("the workflows this changeset would produce are not "
                    "conformant; nothing was changed")

    # ---- reconcile: the manifest is the whole target ----------------
    # Verify only warns about a file the manifest does not list. Here it is an
    # error to correct, or reconcile could not converge on the declared state.
    strays = []
    if args.reconcile:
        try:
            listed = manifest_paths(manifest_file)
        except Exception as e:
            shutil.rmtree(workdir, ignore_errors=True)
            return fail(f"could not read the changeset's manifest: {e}")
        renamed_away = {a for k, a, _b, _t, _s in planned if k == "rename"}
        deleted = {a for k, a, _b, _t, _s in planned if k == "delete"}
        for f in sorted(REPO.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(REPO))
            parts = Path(rel).parts
            if parts[0] in (".git", ".changeset_tmp", "out", "__pycache__",
                            ".venv", ".runs", ".idea", ".vscode"):
                continue
            if f.suffix in (".pyc", ".pyo", ".zip"):
                continue
            if f.name in ("MANIFEST.sha256", "NEXT_STEPS.md", ".DS_Store"):
                continue
            if rel not in listed and rel not in renamed_away \
                    and rel not in deleted:
                strays.append(rel)
        if strays:
            print("\n  Not in the manifest, will be removed:", flush=True)
            for s in strays:
                print(f"    {s}", flush=True)

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

    for rel in strays:
        (REPO / rel).unlink()
        print(f"    removed  {rel}  (not in manifest)", flush=True)

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

    # ---- if a skill moved, the installed copy is now stale ----------
    # Claude cannot write to the skills store and cannot see it change, so the
    # reinstall is the user's step and this is the only reliable prompt for it.
    if touched_skills:
        print("=" * 70, flush=True)
        print("  REINSTALL REQUIRED", flush=True)
        print("=" * 70, flush=True)
        for name in sorted(touched_skills):
            print(f"    {name}  {skill_version(name)}", flush=True)
        print("\n  Run 'Package skills' to build the upload archives, then "
              "install", flush=True)
        print("  them in Customize > Skills. Until then the installed copies "
              "are", flush=True)
        print("  behind this repo state.\n", flush=True)

    print("\n  Changeset applied and verified.\n", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
