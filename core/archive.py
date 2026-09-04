"""
Archive an acquisition run to Google Drive, immutably, and verify it landed.

    gdrive:raw/<source-id>/<run-id>/
        run.json            plain
        manifest.json       plain
        RAW.sha256          plain -- digests of the DECODED files, as written
        data/....js.gz      every payload file, gzipped
        views/....html.gz

`raw/` is a sibling of `content/`, never inside it. Nothing that publishes
content can reach it: `core/publish.py` is scoped to `content/<source-id>/`
and its verb is `sync`, which deletes. This module has its own verb -- `copy`,
which never deletes -- and refuses a remote run folder that already holds
anything. A run is written once. A second attempt is a second run id.

Why gzip at rest: the first acquisition run measured 188 MB decoded against
18 MB on the wire, a 10x ratio, and every artifact was served gzip. Weekly
full runs stored decoded would cost ~10 GB a year; stored gzip, ~1 GB. The
recorded digests stay over the decoded bytes -- RAW.sha256 is copied verbatim
-- and `bianlib.acquire.verify_run` inflates `.gz` files before hashing, so a
downloaded archive checks exactly as the local run did. run.json,
manifest.json and RAW.sha256 stay plain so an archive can be read about
without inflating anything.

Why staging: rclone copies a directory as it is. The gzipped form is built
into a sibling directory first, deterministically (gzip mtime 0, no embedded
filename), so staging the same run twice yields identical bytes and rclone's
checksum comparison means something.

Verified as written: after `copy`, `rclone check --checksum` compares every
staged file against the remote using Drive's own MD5, without downloading. A
copy that returned 0 is not evidence the bytes arrived; the check is.

Credentials are the publishing credentials -- one token, one scope,
`drive.file`. `preflight` and `check_binary` are imported from `publish`
rather than copied, so there is one place credential handling lives. Under
`drive.file` rclone sees only files rclone created: a `raw/` folder made by
hand in the Drive UI is invisible here and the first archive run will create
its own beside it. Let the first run create the prefix.

Quota is printed from `rclone about` on every archive and every target check,
so the number a retention decision needs is in every run's log rather than
nowhere.

Reports counts, sizes, paths and digests only, never content.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

from bianlib import acquire as A
from core.publish import PublishError, check_binary, preflight

#: Same remote name as publishing: one set of credentials, two scoped roots.
REMOTE = os.environ.get("PUBLISH_REMOTE", "gdrive")
ROOT = os.environ.get("RAW_ROOT", "raw")

#: Files copied as they are. Everything else is gzipped to `<path>.gz`.
PLAIN_IN_ARCHIVE = (A.RUN_FILE, A.MANIFEST_FILE, A.SIDECAR_FILE)

#: rclone's exit code for "directory not found".
RCLONE_NOT_FOUND = 3


class ArchiveError(RuntimeError):
    pass


def destination(source_id: str, run_id: str) -> str:
    return f"{REMOTE}:{ROOT}/{source_id}/{run_id}"


def _run(*args: str) -> tuple[int, str, str]:
    """rclone with its exit code, stdout and stderr. Never echoes the
    environment; rclone's errors do not contain the token."""
    if not shutil.which("rclone"):
        raise ArchiveError("rclone is not installed or not on PATH")
    proc = subprocess.run(["rclone", *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "no detail"


# --- staging ----------------------------------------------------------------

def stage(run_dir: Path, staging: Path) -> dict:
    """Build the archive form of `run_dir` in `staging`, which must not exist.

    Deterministic: the same run staged twice produces byte-identical files.
    Returns counts with both byte totals so the compression is on record.
    """
    if staging.exists():
        raise ArchiveError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    summary = {"files": 0, "plain": 0, "compressed": 0,
               "bytes_decoded": 0, "bytes_stored": 0}
    for src in sorted(run_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(run_dir)
        data = src.read_bytes()
        summary["files"] += 1
        summary["bytes_decoded"] += len(data)
        if src.name in PLAIN_IN_ARCHIVE and len(rel.parts) == 1:
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            summary["plain"] += 1
            summary["bytes_stored"] += len(data)
        else:
            dst = staging / (str(rel) + ".gz")
            dst.parent.mkdir(parents=True, exist_ok=True)
            with open(dst, "wb") as fh:
                with gzip.GzipFile(filename="", mode="wb", fileobj=fh,
                                   mtime=0) as gz:
                    gz.write(data)
            summary["compressed"] += 1
            summary["bytes_stored"] += dst.stat().st_size
    return summary


# --- the remote --------------------------------------------------------------

def remote_holds_anything(dest: str) -> bool:
    """Whether the remote run folder exists with content.

    An absent folder (rclone exit 3) is free. A present but empty folder is
    also treated as free -- a copy that was interrupted before writing a file
    leaves one, and refusing it forever would strand the run id. Anything
    listed means a run was written there, and that is never overwritten.
    """
    code, out, err = _run("lsf", dest)
    if code == RCLONE_NOT_FOUND:
        return False
    if code != 0:
        raise ArchiveError(f"rclone lsf failed (exit {code}): {_last_line(err)}")
    return bool(out.strip())


def list_runs(source_id: str | None = None) -> dict[str, list[str]]:
    """{source_id: [run_id, ...]} under the raw root. Missing root -> {}."""
    base = f"{REMOTE}:{ROOT}" + (f"/{source_id}" if source_id else "")
    code, out, err = _run("lsjson", "--dirs-only", base)
    if code == RCLONE_NOT_FOUND:
        return {}
    if code != 0:
        raise ArchiveError(f"rclone lsjson failed (exit {code}): {_last_line(err)}")
    names = [e["Name"] for e in json.loads(out or "[]") if e.get("IsDir")]
    if source_id:
        return {source_id: sorted(names)}
    return {name: list_runs(name).get(name, []) for name in sorted(names)}


def quota() -> dict:
    """Drive's own account of used / free / total bytes, or {} when the
    remote does not report it."""
    code, out, _ = _run("about", f"{REMOTE}:", "--json")
    if code != 0 or not out.strip():
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {}


def quota_line() -> str:
    q = quota()
    if not q:
        return "quota: not reported by this remote"
    gb = 1024 ** 3
    parts = []
    for key in ("used", "free", "total"):
        if isinstance(q.get(key), (int, float)):
            parts.append(f"{key} {q[key] / gb:.2f} GB")
    return "quota: " + (", ".join(parts) if parts else "not reported")


# --- the operation -----------------------------------------------------------

def archive(run_dir: Path, source_id: str, dry_run: bool = False) -> dict:
    """Archive one finished, intact run. Returns a summary.

    Refuses: an unfinished run (no sidecar), a run that does not verify
    locally, and a remote folder that already holds anything. Raises
    ArchiveError for each; the caller decides the exit code.
    """
    run_id = run_dir.name
    if not (run_dir / A.SIDECAR_FILE).is_file():
        raise ArchiveError(
            f"{run_dir} has no {A.SIDECAR_FILE}: the run was never finished "
            f"and is not evidence. Not archiving.")
    local = A.verify_run(run_dir)
    if not local["ok"]:
        raise ArchiveError(
            f"{run_dir} does not verify locally "
            f"({len(local['files_mismatched'])} mismatched, "
            f"{len(local['files_absent'])} absent, "
            f"{len(local['files_stray'])} stray). Not archiving a run that "
            f"is already wrong.")

    preflight()
    dest = destination(source_id, run_id)
    print(f"  destination: {dest}", flush=True)
    if remote_holds_anything(dest):
        raise ArchiveError(
            f"{dest} already holds a run. A run is never rewritten; a new "
            f"attempt is a new run id.")

    staging = run_dir.parent / f".{run_id}.archive"
    if staging.exists():
        shutil.rmtree(staging)
    summary = stage(run_dir, staging)
    summary.update(destination=dest, run_id=run_id, source_id=source_id,
                   dry_run=dry_run)
    print(f"  staged {summary['files']} files: {summary['plain']} plain, "
          f"{summary['compressed']} gzipped; "
          f"{summary['bytes_decoded'] / 1e6:.1f} MB decoded -> "
          f"{summary['bytes_stored'] / 1e6:.1f} MB stored "
          f"({summary['bytes_decoded'] / max(1, summary['bytes_stored']):.1f}x)",
          flush=True)
    try:
        if dry_run:
            print("  dry run: nothing copied", flush=True)
            return summary

        code, _, err = _run("copy", "--checksum", str(staging), dest)
        if code != 0:
            raise ArchiveError(f"rclone copy failed (exit {code}): {_last_line(err)}")

        # Verified as written: every staged file present remotely with a
        # matching hash, and nothing remote that staging lacks.
        code, _, err = _run("check", "--checksum", str(staging), dest)
        summary["check"] = _last_line(err)
        if code != 0:
            raise ArchiveError(
                f"archive does not match staging after copy (exit {code}): "
                f"{_last_line(err)}")
        print(f"  verified: {summary['check']}", flush=True)
        summary["quota"] = quota()
        print(f"  {quota_line()}", flush=True)
        return summary
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def check_target() -> list[tuple[bool, str]]:
    """The raw-archive half of the Drive check, as (ok, message) lines.

    Mirrors `check-publish` and shares its first two checks. The third is
    the raw root: absent is not a failure -- the first archive run creates
    it -- so it is reported, never failed.
    """
    out: list[tuple[bool, str]] = []
    try:
        preflight()
        out.append((True, "Drive credentials present in environment"))
    except PublishError as e:
        out.append((False, str(e)))
        return out
    try:
        out.append((True, f"rclone runs -- {check_binary()}"))
    except PublishError as e:
        out.append((False, f"rclone will not run: {e}"))
        return out
    try:
        runs = list_runs()
    except ArchiveError as e:
        out.append((False, f"Drive unreachable: {e}"))
        return out
    if not runs:
        out.append((True, f"Drive reachable -- {REMOTE}:{ROOT}/ not yet "
                          f"created; the first archive run creates it"))
    else:
        detail = ", ".join(f"{sid}: {len(r)} run(s)" for sid, r in runs.items())
        out.append((True, f"Drive reachable -- {REMOTE}:{ROOT}/ holds {detail}"))
    out.append((True, quota_line()))
    return out
