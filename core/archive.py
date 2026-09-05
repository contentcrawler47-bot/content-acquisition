"""
Archive an acquisition run to Google Drive, immutably, and verify it landed.

    gdrive:raw/<source-id>/<run-id>/
        run.json            plain
        manifest.json       plain
        RAW.sha256          plain -- digests of the DECODED files, as written
        payload.zip         every payload file, one DEFLATE member each,
                            stored under its run-relative path
        ARCHIVED.json       written LAST, after the copy has been verified

Five uploads per run -- or four (R11): when the run's payload bytes are
identical to a run already archived, `SAME_AS.json` naming that run is written
in place of `payload.zip`. The run is still an attributable record that the
source was checked and served the same bytes; the bytes are stored once.
Identity is `bianlib.acquire.run_digest`, over the sidecar's payload lines
only, so two acquisitions of an unchanged landscape agree whatever their
timestamps. Pointers are one hop: a pointer always names a run that HOLDS its
payload. A pointed-to run must not be deleted while a pointer names it;
nothing here deletes, and `tools/check_raw.py` reads a pointer folder through
its sibling.

The first archive layout (changeset 068) uploaded every
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

The consumer side (073c) lives here too, because it is the only other Drive
code the raw archive has: `resolve_run` names the run a consumer means (the
newest archived run by default, P.2), `restore_run` copies it -- and a
pointer's target as a sibling -- into a local root and verifies each folder,
and `fetch_records` copies every run's record files without payload bytes so
the pointer sweep can cover the whole archive cheaply.

Reports counts, sizes, paths and digests only, never content.
"""

from __future__ import annotations

import json
import os
import re
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
SAME_AS = A.SAME_AS_FILE
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

def stage(run_dir: Path, staging: Path, same_as: str | None = None,
          run_digest: str = "") -> dict:
    """Build the archive form of `run_dir` in `staging`, which must not exist.

    With `same_as`, the pointer form: the plain files and SAME_AS.json naming
    that run, no payload zip. Deterministic either way -- the same run staged
    twice produces byte-identical files, which is why the pointer carries no
    timestamp (the marker does). Returns counts with both byte totals so the
    compression, or its absence, is on record.
    """
    if staging.exists():
        raise ArchiveError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    summary = {"files": 0, "plain": 0, "members": 0,
               "bytes_decoded": 0, "bytes_stored": 0, "payload_bytes": 0,
               "same_as": same_as, "run_digest": run_digest}
    zip_path = staging / PAYLOAD
    zf = None
    if same_as is None:
        zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6)
    try:
        for src in sorted(run_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(run_dir)
            rel_posix = rel.as_posix()
            if rel_posix in A.ARCHIVE_ONLY or rel_posix.endswith(".gz"):
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
            if zf is None:
                continue                      # a pointer stores no payload
            info = zipfile.ZipInfo(rel_posix, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            zf.writestr(info, data, compresslevel=6)
            summary["members"] += 1
    finally:
        if zf is not None:
            zf.close()
    if same_as is None:
        summary["bytes_stored"] += zip_path.stat().st_size
        summary["payload_bytes"] = zip_path.stat().st_size
    else:
        pointer = {"layout": ARCHIVE_LAYOUT, "run_id": same_as,
                   "run_digest": run_digest}
        text = json.dumps(pointer, indent=1, sort_keys=True) + "\n"
        (staging / SAME_AS).write_text(text, encoding="utf-8")
        summary["plain"] += 1
        summary["bytes_stored"] += len(text.encode("utf-8"))
    return summary


# --- the remote --------------------------------------------------------------

def remote_names(dest: str) -> tuple[set[str], bool] | None:
    """(top-level file names, has subfolders) of a remote folder; None when
    the folder is absent (rclone exit 3)."""
    code, out, err = _run("lsf", "--files-only", dest)
    if code == RCLONE_NOT_FOUND:
        return None
    if code != 0:
        raise ArchiveError(f"rclone lsf failed (exit {code}): {_last_line(err)}")
    names = {ln.strip() for ln in out.splitlines() if ln.strip()}
    code, sub, _ = _run("lsf", "--dirs-only", dest)
    return names, (code == 0 and bool(sub.strip()))


def remote_state(dest: str) -> str:
    """One of: absent, empty, archived, incomplete, legacy.

    absent      no folder (rclone exit 3)
    empty       a folder with nothing in it -- free to use
    archived    ARCHIVED.json present: finished, never touched again. A
                pointer folder (SAME_AS.json, no payload.zip) is archived
                like any other; `remote_pointer` says which it is.
    incomplete  files present, no marker, this layout -- an interrupted copy,
                which `archive` resumes
    legacy      files present, no marker, no payload.zip: the per-file .gz
                layout of changeset 068. Never touched; delete it by hand in
                Drive if it should go (rclone made it, so deleting is safe).
    """
    listing = remote_names(dest)
    if listing is None:
        return "absent"
    names, has_dirs = listing
    if not names and not has_dirs:
        return "empty"
    if MARKER in names:
        return "archived"
    if PAYLOAD in names or SAME_AS in names or not has_dirs:
        return "incomplete"
    return "legacy"


def _cat(remote_path: str) -> str:
    code, out, err = _run("cat", remote_path)
    if code != 0:
        raise ArchiveError(
            f"rclone cat {remote_path} failed (exit {code}): {_last_line(err)}")
    return out


def remote_pointer(dest: str, names: set[str] | None = None) -> str | None:
    """The run id an archived pointer folder names, or None for a holder."""
    if names is None:
        listing = remote_names(dest)
        names = listing[0] if listing else set()
    if SAME_AS not in names:
        return None
    try:
        return str(json.loads(_cat(f"{dest}/{SAME_AS}"))["run_id"])
    except (ValueError, KeyError, TypeError) as e:
        raise ArchiveError(f"{dest}/{SAME_AS} is not a readable pointer: {e}")


def _run_order(run_id: str) -> tuple:
    """Newest first when sorted descending: CI ids are `<run>-<attempt>`,
    both numeric; anything else sorts after them, by name."""
    m = re.fullmatch(r"(\d+)-(\d+)", run_id)
    return (1, int(m.group(1)), int(m.group(2))) if m else (0, 0, 0, run_id)


def find_duplicate(source_id: str, digest: str,
                   exclude: str | None = None) -> str | None:
    """The newest archived run HOLDING payload bytes whose run digest equals
    `digest`, or None. Pointers are skipped, so a match is always one hop.

    Compares `run_digest` recomputed from each candidate's RAW.sha256 -- the
    definition -- never ARCHIVED.json's `files.RAW.sha256`, which is the
    sidecar's file digest and differs between byte-identical runs. Newest
    first and stops at the first match: normally one small read.
    """
    if not digest:
        return None
    states = list_runs(source_id).get(source_id, {})
    archived = sorted((r for r, st in states.items()
                       if st == "archived" and r != exclude),
                      key=_run_order, reverse=True)
    for run in archived:
        dest = destination(source_id, run)
        listing = remote_names(dest)
        if listing is None or SAME_AS in listing[0]:
            continue
        candidate = A.run_digest_of(A.parse_sidecar(_cat(f"{dest}/{A.SIDECAR_FILE}")))
        if candidate == digest:
            return run
    return None


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

    # De-duplication (R11): the same payload bytes are stored once. The
    # comparison is the run digest recomputed from each holder's sidecar.
    digest = A.run_digest(run_dir)
    same_as = find_duplicate(source_id, digest, exclude=run_id)
    print(f"  raw digest {digest[:16]}"
          + (f": identical to archived run {same_as}; archiving as a "
             f"pointer, no {PAYLOAD}" if same_as else
             ": no archived run holds these bytes"), flush=True)

    staging = run_dir.parent / f".{run_id}.archive"
    if staging.exists():
        shutil.rmtree(staging)
    summary = stage(run_dir, staging, same_as=same_as, run_digest=digest)
    summary.update(destination=dest, run_id=run_id, source_id=source_id,
                   layout=ARCHIVE_LAYOUT, resumed=(state == "incomplete"),
                   dry_run=dry_run)
    if same_as:
        print(f"  staged {summary['plain']} plain files including {SAME_AS} "
              f"-> {summary['bytes_stored'] / 1e3:.1f} KB stored", flush=True)
    else:
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
            "run_digest": digest,
            "same_as": same_as,
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


# --- the consumer side (073c; R10, P.2) -------------------------------------

def resolve_run(source_id: str, run_id: str | None = None) -> dict:
    """Which archived run a consumer means, and where its payload bytes are.

    `run_id` empty means the newest archived run (P.2's default; from Drive
    until `.runs/INDEX.jsonl` exists at 074). Returns
    {run_id, state, payload_run_id, pointer} where `payload_run_id` is the
    run holding the bytes -- the run itself, or the one its SAME_AS.json
    names. Raises ArchiveError when the run is absent, incomplete, legacy, or
    a pointer whose target is not an archived holder: a consumer never reads
    through a broken pointer.
    """
    states = list_runs(source_id).get(source_id, {})
    if not run_id:
        archived = sorted((r for r, st in states.items() if st == "archived"),
                          key=_run_order, reverse=True)
        if not archived:
            raise ArchiveError(
                f"no archived run under {REMOTE}:{ROOT}/{source_id}/ -- "
                f"run Acquire first")
        run_id = archived[0]
    state = states.get(run_id, "absent")
    if state != "archived":
        raise ArchiveError(
            f"{destination(source_id, run_id)} is {state}, not an archived "
            f"run" + (" (no ARCHIVED.json: an interrupted copy)"
                      if state == "incomplete" else ""))
    dest = destination(source_id, run_id)
    via = remote_pointer(dest)
    payload = via or run_id
    if via:
        tstate = states.get(via, "absent")
        if tstate != "archived":
            raise ArchiveError(
                f"{run_id} points to {via}, which is {tstate}, not archived")
        if remote_pointer(destination(source_id, via)):
            raise ArchiveError(
                f"{run_id} points to {via}, which is itself a pointer; "
                f"pointers are one hop")
    return {"run_id": run_id, "state": state, "payload_run_id": payload,
            "pointer": bool(via)}


def restore_run(source_id: str, run_id: str, dest_root: Path) -> dict:
    """Copy one archived run -- and, for a pointer, its target as a SIBLING
    -- from Drive into `dest_root/<run-id>/`, then verify each folder against
    its own sidecar. The folders must not already exist: a restore never
    overwrites a run directory, cached or written.

    Returns the resolved run plus {folders, files_verified, payload_via}.
    Raises ArchiveError on a folder that does not verify; nothing partial is
    left for a later step to trust, because the caller caches what it gets.
    """
    resolved = resolve_run(source_id, run_id)
    folders = [resolved["run_id"]]
    if resolved["pointer"]:
        folders.append(resolved["payload_run_id"])
    for name in folders:
        if (dest_root / name).exists():
            raise ArchiveError(
                f"{dest_root / name} already exists; a run directory is "
                f"never overwritten. Restore into an empty root.")
    dest_root.mkdir(parents=True, exist_ok=True)
    verified = 0
    try:
        for name in folders:
            code, _, err = _run("copy", "--checksum",
                                destination(source_id, name),
                                str(dest_root / name))
            if code != 0:
                raise ArchiveError(
                    f"rclone copy of {name} failed (exit {code}): "
                    f"{_last_line(err)}")
        for name in reversed(folders):          # the target first, so the
            try:                                # pointer reads through it
                v = A.verify_run(dest_root / name)
            except A.RunUnreadable as e:
                raise ArchiveError(f"restored {name} is unreadable: {e}") from e
            if not v["ok"]:
                raise ArchiveError(
                    f"restored {name} does not verify against its own "
                    f"{A.SIDECAR_FILE}: {len(v['files_mismatched'])} "
                    f"mismatched, {len(v['files_absent'])} absent, "
                    f"{len(v['files_stray'])} stray")
            verified += v["files_verified"]
    except Exception:
        for name in folders:
            shutil.rmtree(dest_root / name, ignore_errors=True)
        raise
    via = A.verify_run(dest_root / resolved["run_id"])["payload_via"]
    resolved.update(folders=folders, files_verified=verified, payload_via=via)
    return resolved


#: The archive files a pointer sweep needs -- every record, never the bytes.
RECORD_NAMES = (A.RUN_FILE, A.MANIFEST_FILE, A.SIDECAR_FILE, MARKER, SAME_AS)


def fetch_records(source_id: str, dest_root: Path) -> int:
    """Copy every run's RECORD files (no payload.zip) under the source's
    archive root into `dest_root/<run-id>/`, so `bianlib.acquire
    .check_pointers` can sweep the whole archive from a few hundred
    kilobytes. Returns the number of run folders copied."""
    dest_root.mkdir(parents=True, exist_ok=True)
    args = ["copy", "--checksum", f"{REMOTE}:{ROOT}/{source_id}",
            str(dest_root)]
    for name in RECORD_NAMES:
        args += ["--include", f"/*/{name}"]
    code, _, err = _run(*args)
    if code == RCLONE_NOT_FOUND:
        return 0
    if code != 0:
        raise ArchiveError(
            f"rclone copy of archive records failed (exit {code}): "
            f"{_last_line(err)}")
    return sum(1 for p in dest_root.iterdir() if p.is_dir())


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
                via = remote_pointer(destination(sid, run)) if ok else None
                out.append((True, f"  {sid}/{run}: {state}"
                            + (f"  -> same as {via}" if via else "")
                            + ("" if ok else "  <- not a completed archive")))
    out.append((True, quota_line()))
    return out
