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

REQUIRED_ENV = [
    "RCLONE_CONFIG_GDRIVE_CLIENT_ID",
    "RCLONE_CONFIG_GDRIVE_CLIENT_SECRET",
    "RCLONE_CONFIG_GDRIVE_TOKEN",
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
            "missing Drive credentials in environment: " + ", ".join(missing))
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


def list_published() -> list[str]:
    """Source folders currently under the publish root."""
    preflight()
    try:
        raw = _rclone("lsjson", f"{REMOTE}:{ROOT}", "--dirs-only", capture=True)
    except PublishError:
        return []
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
