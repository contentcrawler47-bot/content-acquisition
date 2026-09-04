"""
Archive an acquisition run to Google Drive, immutably, and verify it landed.

    gdrive:raw/<source-id>/<run-id>/
        run.json            plain
        manifest.json       plain
        RAW.sha256          plain -- digests of the DECODED files, as written
        payload.zip         every payload file, one DEFLATE member each,
                            stored under its run-relative path
        ARCHIVED.json       written LAST, after the copy has been verified

Five uploads per run. The first archive layout (changeset 068) uploaded every
payload file separately, gzipped, and Drive's per-file API pacing turned a
723-file, 18 MB copy into an hour: bursts of a dozen files with sixty-second
back-offs between them, on rclone's shared built-in OAuth client. The 20-minute
job budget ran out at roughly a fifth of the way. This layout puts the same
bytes in one member-addressable zip, so the copy takes seconds and no
dedicated client id is needed.

`raw/` is a sibling of `content/`, never inside it. Nothing that publishes
content can reach it: `core/publish.py` is scoped to `content/<source-id>/`
and its verb is `sync`, which deletes. This module's verb is `copy`, which
never deletes.

Immutable, but not brittle: ARCHIVED.json is the completion marker, written
only after `rclone check --checksum` has confirmed every staged file against
Drive's own hashes. A run folder that carries the marker is never touched
again -- a second attempt is refused. A folder WITHOUT the marker is an
interrupted copy, and `rclone copy --checksum` resumes it: files already
present with matching hashes are skipped, the rest are written, and the
marker follows. The first layout refused any folder holding anything, which
turned every interrupted copy into a stranded run id. Same lesson as the
snapshot README and RAW.sha256: the marker is what says "finished", and it
goes last.

The recorded digests stay over the decoded bytes. RAW.sha256 is copied
verbatim, and `bianlib.acquire.read_stored` reads a file from a plain run
directory, a zip member, or (for the one archive made under the first layout)
a per-file `.gz`, so `tools/check_raw.py` verifies a downloaded archive
exactly as it verifies a run on disk.

The zip is built deterministically -- members in sorted order, a fixed
timestamp, fixed permissions, fixed compression level -- so staging the same
run twice yields identical bytes and rclone's checksum comparison means
something across attempts.

Credentials are the publishing credentials, imported from `publish` rather
than copied. Under the `drive.file` scope rclone sees only files rclone
created; a `raw/` folder made by hand in the Drive UI is invisible here.
Deleting a folder rclone made is safe; creating one by hand is not.

Quota is printed from `rclone about` on every archive and every target check.

Reports counts, sizes, paths and digests only, never content.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bianlib import acquire as A
from core.publish import PublishError, check_binary, preflight

#: Same remote name as publishing: one set of credentials, two scoped roots.
REMOTE = os.environ.get("PUBLISH_REMOTE", "gdrive")
ROOT = os.environ.get("RAW_ROOT", "raw")

#: Bumped when the archive layout changes. 1 = per-file .gz (068);
#: 2 = payload.zip + ARCHIVED.json (this module).
ARCHIVE_LAYOUT = 2

PAYLOAD = A.PAYLOAD_FILE
MARKER = A.MARKER_FILE
#: Files copied as they are; everything else goes into the payload zip.
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- staging ----------------------------------------------------------------

def stage(run_dir: Path, staging: Path) -> dict:
    """Build the archive form of `run_dir` in `staging`, which must not exist.

    Deterministic: the same run staged twice produces byte-identical files.
    Returns counts with both byte totals so the compression is on record.
    """
    if staging.exists():
        raise ArchiveError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    summary = {"files": 0, "plain": 0, "members": 0,
               "bytes_decoded": 0, "bytes_stored": 0}
    zip_path = staging / PAYLOAD
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for src in sorted(run_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(run_dir)
            rel_posix = rel.as_posix()
            if rel_posix in (PAYLOAD, MARKER) or rel_posix.endswith(".gz"):
                raise ArchiveError(
                    f"{rel_posix}: a run directory must hold the run as "
                    f"written, not an archive form")
            data = src.read_bytes()
            summary["files"] += 1
            summary["bytes_decoded"] += len(data)
            if src.name in PLAIN_IN_ARCHIVE and len(rel.parts) == 1:
                (staging / rel).write_bytes(data)
                summary["plain"] += 1
                summary["bytes_stored"] += len(data)
                continue
            info = zipfile.ZipInfo(rel_posix, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(info, data, compresslevel=6)
            summary["members"] += 1
    summary["bytes_stored"] += zip_path.stat().st_size
    summary["payload_bytes"] = zip_path.stat().st_size
    return summary


# --- the remote --------------------------------------------------------------

def remote_state(dest: str) -> str:
    """One of: absent, empty, archived, incomplete, legacy.

    absent      no folder (rclone exit 3)
    empty       a folder with nothing in it -- free to use
    archived    ARCHIVED.json present: finished, never touched again
    incomplete  files present, no marker, this layout -- an interrupted copy,
                which `archive` resumes
    legacy      files present, no marker, no payload.zip: the per-file .gz
                layout of changeset 068. Never touched; delete it by hand in
                Drive if it should go (rclone made it, so deleting is safe).
    """
    code, out, err = _run("lsf", "--files-only", dest)
    if code == RCLONE_NOT_FOUND:
        return "absent"
    if code != 0:
        raise ArchiveError(f"rclone lsf failed (exit {code}): {_last_line(err)}")
    names = {ln.strip() for ln in out.splitlines() if ln.strip()}
    code, sub, _ = _run("lsf", "--dirs-only", dest)
    has_dirs = code == 0 and bool(sub.strip())
    if not names and not has_dirs:
        return "empty"
    if MARKER in names:
        return "archived"
    if PAYLOAD in names or not has_dirs:
        return "incomplete"
    return "legacy"


def list_runs(source_id: str | None = None) -> dict[str, dict[str, str]]:
    """{source_id: {run_id: state}} under the raw root. Missing root -> {}."""
    base = f"{REMOTE}:{ROOT}" + (f"/{source_id}" if source_id else "")
    code, out, err = _run("lsjson", "--dirs-only", base)
    if code == RCLONE_NOT_FOUND:
        return {}
    if code != 0:
        raise ArchiveError(f"rclone lsjson failed (exit {code}): {_last_line(err)}")
    names = sorted(e["Name"] for e in json.loads(out or "[]") if e.get("IsDir"))
    if source_id:
        return {source_id: {run: remote_state(destination(source_id, run))
                            for run in names}}
    return {name: list_runs(name).get(name, {}) for name in names}


def quota() -> dict:
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
    parts = [f"{k} {q[k] / gb:.2f} GB" for k in ("used", "free", "total")
             if isinstance(q.get(k), (int, float))]
    return "quota: " + (", ".join(parts) if parts else "not reported")


# --- the operation -----------------------------------------------------------

def archive(run_dir: Path, source_id: str, dry_run: bool = False) -> dict:
    """Archive one finished, intact run. Returns a summary.

    Refuses: an unfinished run (no sidecar), a run that does not verify
    locally, a remote folder already carrying the completion marker, and a
    legacy-layout folder. Resumes an incomplete one. Raises ArchiveError for
    each refusal; the caller decides the exit code.
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
    state = remote_state(dest)
    if state == "archived":
        raise ArchiveError(
            f"{dest} already holds a completed archive ({MARKER} present). "
            f"A run is never rewritten; a new attempt is a new run id.")
    if state == "legacy":
        raise ArchiveError(
            f"{dest} holds a per-file archive from the first layout with no "
            f"completion marker. Not touching it. If it is an interrupted "
            f"copy, delete the folder in Drive by hand and re-run.")
    if state == "incomplete":
        print(f"  {dest} holds an interrupted copy; resuming", flush=True)

    staging = run_dir.parent / f".{run_id}.archive"
    if staging.exists():
        shutil.rmtree(staging)
    summary = stage(run_dir, staging)
    summary.update(destination=dest, run_id=run_id, source_id=source_id,
                   layout=ARCHIVE_LAYOUT, resumed=(state == "incomplete"),
                   dry_run=dry_run)
    print(f"  staged {summary['plain']} plain files + {PAYLOAD} with "
          f"{summary['members']} members; "
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

        # The marker: written locally, then copied, only now.
        marker = {
            "layout": ARCHIVE_LAYOUT, "run_id": run_id, "source_id": source_id,
            "archived_at": _now(), "resumed": summary["resumed"],
            "files": {p.name: A._sha256(p.read_bytes())
                      for p in sorted(staging.iterdir()) if p.is_file()},
            "members": summary["members"],
            "bytes_decoded": summary["bytes_decoded"],
            "bytes_stored": summary["bytes_stored"],
            "check": summary["check"],
        }
        (staging / MARKER).write_text(json.dumps(marker, indent=1,
                                                 sort_keys=True) + "\n",
                                      encoding="utf-8")
        code, _, err = _run("copyto", str(staging / MARKER), f"{dest}/{MARKER}")
        if code != 0:
            raise ArchiveError(
                f"archive verified but the completion marker could not be "
                f"written (exit {code}): {_last_line(err)}. Re-running will "
                f"resume and retry the marker.")
        print(f"  marked complete: {MARKER}", flush=True)
        summary["quota"] = quota()
        print(f"  {quota_line()}", flush=True)
        return summary
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def check_target() -> list[tuple[bool, str]]:
    """The raw-archive half of the Drive check, as (ok, message) lines.

    Mirrors `check-publish` and shares its first two checks. The third is
    the raw root: absent is not a failure -- the first archive run creates
    it -- so it is reported, never failed. Runs are reported with their
    state, so an interrupted copy is visible from here.
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
        out.append((True, f"Drive reachable -- {REMOTE}:{ROOT}/ holds "
                          f"{sum(len(r) for r in runs.values())} run folder(s)"))
        for sid, states in runs.items():
            for run, state in states.items():
                ok = state == "archived"
                out.append((True, f"  {sid}/{run}: {state}"
                            + ("" if ok else "  <- not a completed archive")))
    out.append((True, quota_line()))
    return out
