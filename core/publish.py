"""
Uniform publishing to Google Drive.

Every source publishes the same way, through rclone, into its own subfolder:

    gdrive:content/<source-id>/

Isolation is the point. `rclone sync` deletes destination files absent from the
source, so it is scoped to one subfolder per source and never to the root. A
broken or empty harvest for one source therefore cannot delete another's
content — and a source failing mid-run leaves every other source untouched.

Credentials: rclone reads RCLONE_CONFIG_GDRIVE_* from the environment, so no
config file is ever written to disk. The Drive identity is shared across all
sources; per-source credentials belong to the source, not here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REMOTE = os.environ.get("PUBLISH_REMOTE", "gdrive")
ROOT = os.environ.get("PUBLISH_ROOT", "content")

# Only the token is required. rclone ships with a built-in OAuth client, so a
# Google Cloud project is optional: leave client_id and client_secret blank
# during `rclone config` and rclone uses its own.
#
#   own client     needs a Cloud project; a dedicated quota; an unpublished
#                  app expires refresh tokens after 7 days, so it must be set
#                  to "In production"
#   built-in       no Cloud project at all, and no 7-day expiry since rclone's
#                  app is already published; the quota is shared with other
#                  rclone users, so heavy use can be rate limited
#
# For a weekly sync of a few dozen files the built-in client is ample.
REQUIRED_ENV = ["RCLONE_CONFIG_GDRIVE_TOKEN"]
OPTIONAL_ENV = [
    "RCLONE_CONFIG_GDRIVE_CLIENT_ID",
    "RCLONE_CONFIG_GDRIVE_CLIENT_SECRET",
]


class PublishError(RuntimeError):
    pass


def _rclone(*args: str, capture: bool = False) -> str:
    if not shutil.which("rclone"):
        raise PublishError("rclone is not installed or not on PATH")
    cmd = ["rclone", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Never echo the command environment; rclone errors can be verbose but
        # do not contain the token.
        raise PublishError(
            f"rclone {args[0]} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'no detail'}")
    return proc.stdout if capture else ""


def preflight() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise PublishError(
            "missing Drive credentials in environment: " + ", ".join(missing)
            + ". GDRIVE_TOKEN is required; GDRIVE_CLIENT_ID and "
              "GDRIVE_CLIENT_SECRET are optional and only needed when using "
              "your own Google Cloud OAuth client.")

    # A workflow passes every secret, so an unset one arrives as an empty
    # string. rclone treats an empty client_id as configured-but-blank rather
    # than absent, which breaks the fallback — so remove them.
    using_own = False
    for key in OPTIONAL_ENV:
        if os.environ.get(key):
            using_own = True
        else:
            os.environ.pop(key, None)
    print(f"  auth: {'own OAuth client' if using_own else 'rclone built-in client'}",
          flush=True)

    os.environ.setdefault("RCLONE_CONFIG_GDRIVE_TYPE", "drive")
    os.environ.setdefault("RCLONE_CONFIG_GDRIVE_SCOPE", "drive.file")


def destination(source_id: str) -> str:
    return f"{REMOTE}:{ROOT}/{source_id}"


def publish(source_id: str, outdir: Path, dry_run: bool = False) -> str:
    """Sync one source's output to its own Drive subfolder."""
    preflight()

    if not outdir.is_dir():
        raise PublishError(f"{outdir} does not exist — nothing to publish")
    files = [f for f in outdir.iterdir() if f.is_file()]
    if not files:
        raise PublishError(
            f"{outdir} is empty — refusing to sync, that would delete the "
            f"published copy of '{source_id}'")
    if not (outdir / "manifest.json").is_file():
        raise PublishError(
            f"{outdir}/manifest.json missing — refusing to publish an "
            f"incomplete harvest")

    dest = destination(source_id)
    args = ["sync", str(outdir), dest, "--create-empty-src-dirs=false",
            "--stats-log-level", "NOTICE"]
    if dry_run:
        args.append("--dry-run")

    print(f"  publishing {len(files)} files -> {dest}"
          + (" (dry run)" if dry_run else ""), flush=True)
    _rclone(*args)
    return dest


def check_binary() -> str:
    """Confirm rclone actually runs.

    Worth its own call: an environment problem — such as an env var rclone
    misreads as one of its own flags — breaks every invocation, and without
    this the failure surfaces as an empty result that looks like success.
    """
    return _rclone("version", capture=True).splitlines()[0].strip()


def list_published() -> list[str]:
    """Source folders currently under the publish root.

    A missing root is normal before the first publish and returns []. Any
    other failure is raised: previously everything was swallowed, so a broken
    rclone reported "0 published sources" and the check passed when it should
    have failed.
    """
    preflight()
    try:
        raw = _rclone("lsjson", f"{REMOTE}:{ROOT}", "--dirs-only", capture=True)
    except PublishError as e:
        msg = str(e).lower()
        if "directory not found" in msg or "not found" in msg:
            return []
        raise
    return sorted(e["Name"] for e in json.loads(raw or "[]"))


def reindex(workdir: Path) -> str:
    """Regenerate the top-level index listing every published source.

    Run occasionally and on its own — deliberately not part of a source's
    workflow, so two sources publishing at once cannot race over this file.
    """
    preflight()
    workdir.mkdir(parents=True, exist_ok=True)
    lines = ["# Acquired content", "",
             "Each folder below holds one source. Open its `index.md` first.",
             "", "| Source | Items | Last acquired |", "|---|---|---|"]

    for sid in list_published():
        tmp = workdir / f"{sid}.json"
        try:
            _rclone("copyto", f"{destination(sid)}/manifest.json", str(tmp))
            m = json.loads(tmp.read_text())
            lines.append(
                f"| [{m.get('source_name', sid)}]({sid}/index.md) "
                f"| {m.get('count', '?')} | {m.get('generated', '?')} |")
        except Exception:
            lines.append(f"| {sid} | ? | (no manifest) |")
        finally:
            tmp.unlink(missing_ok=True)

    index = workdir / "index.md"
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _rclone("copyto", str(index), f"{REMOTE}:{ROOT}/index.md")
    print(f"  reindexed {len(lines) - 6} source(s)", flush=True)
    return f"{REMOTE}:{ROOT}/index.md"


if __name__ == "__main__":
    print("Use: python run.py publish <source>", file=sys.stderr)
    sys.exit(2)
