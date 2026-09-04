"""
Stage 2: acquire the source's artifacts and RETAIN them, with provenance.

Everything before this changeset parsed in flight and kept nothing: a shard
was fetched, read, and gone, and the only durable record of a run was the
extract built from it. This module writes the bytes down first, under a
run-addressed directory that is never rewritten, together with enough
recorded context to answer -- from the stored files alone -- where each
artifact came from, when, by what code, under what policy, and against which
declaration of what should have been fetched.

    out/_raw/<source-id>/<run-id>/
        run.json          provenance, policy, declared scopes, outcomes, state
        manifest.json     one record per artifact: URL, path, status, bytes,
                          sha256, both timestamps, headers, every attempt
        RAW.sha256        sidecar: sha256 of every file above, written LAST
        data/...          payload bytes exactly as the entity was served
        views/...         view pages, in `full` mode

The sidecar is the completeness marker. It is written after everything it
covers, so a run directory without one was never finished: a job that timed
out mid-fetch leaves run.json saying "running" and no RAW.sha256, and
tools/check_raw.py refuses it rather than reading it as a result.

Three declarations are made here and nowhere else, because they are the
interpretation that acquisition cannot avoid:

  WHAT A SCOPE CONTAINS is declared in SCOPES below. The model scope is the
  data files the landscape browser loads; the geometry scope is the view pages
  whose category is in `BianSource.GEOMETRY_VIEW_TYPES`, imported rather than
  copied. Resolving the geometry set needs the model parsed, so acquisition
  calls `Landscape.parse` on the bytes it just stored -- the same parser, on
  the same bytes, that stage 3 will use.

  WHAT THE DIGEST IS OVER: the transfer-decoded entity body. A gzip response
  is stored and hashed inflated; `Content-Encoding` travels in the record so
  the decision is visible. Two responses carrying identical content then get
  identical digests whatever the server did on the wire.

  WHAT A 404 MEANS: recorded, never stored at the artifact's path. A 404 body
  at data/all_objects_data_48.js would read as a shard to anything that
  trusts the tree, so the record carries its status, size and digest and the
  path stays empty.

A run is never rewritten. The run id is the CI run id and attempt, or a
timestamp locally; a directory that already exists is refused, not emptied.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from bianlib import landscape as L
from bianlib.fetch import Fetcher, SourceUnhappy, robots_disallows

#: Bumped when the layout or the meaning of a manifest field changes.
MANIFEST_VERSION = "1"
#: Bumped when this module's behaviour changes what gets fetched or recorded
#: for unchanged upstream data.
ACQUIRER_VERSION = "1"

RUN_FILE = "run.json"
MANIFEST_FILE = "manifest.json"
SIDECAR_FILE = "RAW.sha256"

MODES = ("model-only", "full")

#: The declared acquisition scopes. Each names its cadence and what resolves
#: its artifact set; the resolution itself is in `declare_*` below so that
#: the text and the code cannot say different things.
SCOPES = {
    "model": {
        "cadence": "every acquisition",
        "purpose": "the semantic model: objects, relations, view membership, "
                   "language config, and the model index",
        "declared_by": "all_objects_data_mapping.js is the index; shard "
                       "numbers are its values (bianlib.landscape."
                       "shard_numbers); relations, on_views and config are "
                       "fixed names; the models file is discovered from "
                       "bianlib.landscape.MODELS_CANDIDATES and the "
                       "candidate that answered is recorded",
    },
    "geometry": {
        "cadence": "every acquisition in full mode",
        "purpose": "view pages whose arrangement carries meaning; parsed to "
                   "nodes and edges by stage 3",
        "declared_by": "views in insiteViews whose diagram object's category "
                       "is in BianSource.GEOMETRY_VIEW_TYPES (imported, not "
                       "copied). Three counts are recorded separately: views "
                       "KNOWN (insiteViews), views DECLARED (of those "
                       "types), views STORED (2xx). Views that YIELD geometry "
                       "is a stage 3 measurement, status.views_with_geometry",
    },
}


# --- helpers ----------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v or ""
    return ""


def _rel(base: str, url: str) -> str:
    """The artifact's path relative to the run directory: its URL path
    relative to the source base, so the tree mirrors the source."""
    base = base.rstrip("/") + "/"
    if url.startswith(base):
        return url[len(base):]
    return url.split("://", 1)[-1]


def _json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True,
                               ensure_ascii=False) + "\n", encoding="utf-8")


# --- one artifact -----------------------------------------------------------

def fetch_artifact(fetcher: Fetcher, run_dir: Path, base: str, url: str,
                   key: str, scope: str) -> dict:
    """Fetch one URL and store its entity body. Returns the manifest record.

    Every path out of here produces a record: stored, missing (404), or
    failed (gave up, or the breaker tripped -- in which case the exception
    is re-raised AFTER the record is made, so the manifest still says what
    happened to the request that stopped the run).
    """
    rel = _rel(base, url)
    started = time.monotonic()
    record = {
        "key": key, "scope": scope, "url": url, "path": rel,
        "fetched_at": _now(),                    # capture time (I2.11)
        "status": None, "outcome": None, "bytes": 0, "sha256": None,
        "source_last_modified": None,            # source-valid time (I2.11)
        "etag": None, "content_type": None, "content_encoding": None,
        "elapsed_s": None, "attempts": [], "error": None,
    }
    try:
        resp = fetcher.get(url, conditional=False)
    except SourceUnhappy as e:
        record.update(outcome="failed", error=str(e),
                      attempts=list(e.attempts),
                      elapsed_s=round(time.monotonic() - started, 3))
        raise _Stop(record) from e
    except Exception as e:                                  # noqa: BLE001
        record.update(outcome="failed", error=f"{type(e).__name__}: {e}",
                      attempts=list(getattr(e, "attempts", [])),
                      elapsed_s=round(time.monotonic() - started, 3))
        return record

    record.update(
        status=resp.status, attempts=list(resp.attempts),
        elapsed_s=round(time.monotonic() - started, 3),
        bytes=len(resp.body), sha256=_sha256(resp.body),
        source_last_modified=_header(resp.headers, "Last-Modified") or None,
        etag=_header(resp.headers, "ETag") or None,
        content_type=_header(resp.headers, "Content-Type") or None,
        content_encoding=_header(resp.headers, "Content-Encoding") or None,
    )
    if 200 <= resp.status < 300:
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resp.body)
        record["outcome"] = "stored"
    elif resp.status == 404:
        record["outcome"] = "missing"
    else:
        record["outcome"] = "failed"
        record["error"] = f"HTTP {resp.status}"
    return record


class _Stop(Exception):
    """The breaker tripped. Carries the record of the request that did it."""

    def __init__(self, record: dict):
        super().__init__(record.get("error") or "stopped")
        self.record = record


# --- declaring the sets ----------------------------------------------------

def declare_model(base: str, mapping_text: str) -> list[tuple[str, str]]:
    """(key, url) for the model scope, from the index text.

    The mapping is parsed only for its values; that parse is the one
    interpretation acquisition cannot avoid, and it is the same function the
    model loader uses. An unparseable mapping falls back to the loader's
    probe range, exactly as `Landscape` does, and is recorded as such.
    """
    try:
        mapping = L.parse_js_assignment(mapping_text)
    except Exception:
        mapping = {}
    out = [(f"shard:{n}", L.shard_url(base, n)) for n in L.shard_numbers(mapping)]
    out += [("relations", L.data_url(base, "all_objects_relations.js")),
            ("on_views", L.data_url(base, "all_objects_on_views.js")),
            ("config", L.data_url(base, "config_data.js"))]
    return out


def declare_geometry(model: L.Landscape, view_types) -> list[tuple[str, str]]:
    """(key, url) for every known view whose category is a declared type."""
    return [(f"view:{vid}", L.view_url(model.base, vid))
            for vid in sorted(model.insite_views, key=str)
            if model.categories.get(str(vid)) in view_types]


# --- the run ----------------------------------------------------------------

def acquire(source, run_dir: Path, mode: str, provenance: dict,
            fetcher_factory=Fetcher) -> dict:
    """Acquire one source into `run_dir`. Returns the final run record.

    `run_dir` must not exist: a run is never rewritten (I2.4). `provenance`
    is read at the boundary and passed in as data, like `run` for the
    extract, so this function depends on no environment.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    if run_dir.exists():
        raise FileExistsError(
            f"{run_dir} already exists. A run is never rewritten; a new "
            f"attempt is a new run id.")
    run_dir.mkdir(parents=True)

    base = source.base
    fetcher = fetcher_factory(base)
    run = {
        "manifest_version": MANIFEST_VERSION,
        "acquirer_version": ACQUIRER_VERSION,
        "run_id": run_dir.name,
        "source_id": source.id,
        "source_version": base,            # the pinned landscape URL
        "mode": mode,
        "started_at": _now(),
        "finished_at": None,
        "state": "running",
        "reason": None,
        "provenance": dict(provenance),
        "policy": {
            "user_agent": fetcher.request_headers_template()["User-Agent"],
            "request_headers": fetcher.request_headers_template(),
            "delay_s": fetcher.delay,
            "timeout_s": fetcher.timeout,
            "max_attempts": fetcher.max_attempts,
            "consecutive_failure_limit": fetcher.failure_limit,
            "transport_failure_limit": fetcher.transport_failure_limit,
            "robots": {"checked": False, "url": None, "rule": None},
        },
        "scopes": {},
        "outcomes": {},
        "fetch_stats": None,
    }
    _json(run_dir / RUN_FILE, run)      # a record exists from the first second

    records: list[dict] = []
    stop: _Stop | None = None

    def scope_summary(scope: str) -> dict:
        """Counts over DECLARED artifacts in a scope. The models file is
        discovered rather than declared, so its candidates are counted apart
        and never inflate the declared denominator."""
        mine = [r for r in records if r["scope"] == scope
                and not r["key"].startswith("models:")]
        found = [r for r in records if r["scope"] == scope
                 and r["key"].startswith("models:")]
        return {"declared": len(mine),
                "stored": sum(1 for r in mine if r["outcome"] == "stored"),
                "missing": sum(1 for r in mine if r["outcome"] == "missing"),
                "failed": sum(1 for r in mine if r["outcome"] == "failed"),
                "bytes": sum(r["bytes"] for r in mine + found
                             if r["outcome"] == "stored"),
                "discovered_tried": len(found),
                "discovered_stored": sum(1 for r in found
                                         if r["outcome"] == "stored")}

    try:
        # -- policy: robots, once, before anything else ---------------------
        path = "/" + base.split("/", 3)[-1]
        rule = robots_disallows(fetcher, path)
        run["policy"]["robots"] = {
            "checked": True, "url": f"{fetcher.scheme}://{fetcher.host}/robots.txt",
            "rule": rule or None}
        if rule:
            run["state"], run["reason"] = "failed", f"robots.txt disallows {rule}"
            print(f"  robots.txt disallows {rule} -- not acquiring", flush=True)
            return run
        print("  robots.txt: no rule against this path", flush=True)

        # -- model scope ----------------------------------------------------
        print(f"  scope model: {SCOPES['model']['purpose']}", flush=True)
        index = fetch_artifact(fetcher, run_dir, base,
                               L.data_url(base, "all_objects_data_mapping.js"),
                               "index", "model")
        records.append(index)
        mapping_text = ((run_dir / index["path"]).read_text(encoding="utf-8",
                                                            errors="replace")
                        if index["outcome"] == "stored" else "")
        declared = declare_model(base, mapping_text)
        run["scopes"]["model"] = dict(SCOPES["model"], declared=1 + len(declared),
                                      index_parsed=bool(mapping_text))
        for n, (key, url) in enumerate(declared, 1):
            records.append(fetch_artifact(fetcher, run_dir, base, url, key,
                                          "model"))
            if n % 10 == 0 or n == len(declared):
                s = scope_summary("model")
                print(f"    {n} of {len(declared)} (scope so far incl. index): "
                      f"{s['stored']} stored, "
                      f"{s['missing']} missing, {s['failed']} failed, "
                      f"{s['bytes'] / 1024 / 1024:.1f} MB", flush=True)

        # The models file: discovered, not declared. Try candidates in the
        # loader's order and record every attempt; store the first answer.
        found = None
        for candidate in L.MODELS_CANDIDATES:
            rec = fetch_artifact(fetcher, run_dir, base, f"{base}/{candidate}",
                                 f"models:{candidate}", "model")
            records.append(rec)
            if rec["outcome"] == "stored" and rec["bytes"] > 0:
                found = candidate
                break
        run["scopes"]["model"]["models_file"] = found
        run["scopes"]["model"]["models_tried"] = [
            r["key"].split(":", 1)[1] for r in records
            if r["key"].startswith("models:")]
        print(f"    models file: {found or 'NOT FOUND'}", flush=True)

        # -- geometry scope -------------------------------------------------
        if mode == "full":
            print(f"  scope geometry: {SCOPES['geometry']['purpose']}",
                  flush=True)
            model = _parse_stored_model(run_dir, base, records)
            declared = declare_geometry(model, source.GEOMETRY_VIEW_TYPES)
            run["scopes"]["geometry"] = dict(
                SCOPES["geometry"],
                view_types=list(source.GEOMETRY_VIEW_TYPES),
                views_known=len(model.insite_views),
                declared=len(declared),
                model_complete=(scope_summary("model")["failed"] == 0))
            print(f"    {len(model.insite_views)} views known, "
                  f"{len(declared)} declared across "
                  f"{len(source.GEOMETRY_VIEW_TYPES)} view types", flush=True)
            for n, (key, url) in enumerate(declared, 1):
                records.append(fetch_artifact(fetcher, run_dir, base, url, key,
                                              "geometry"))
                if n % 50 == 0 or n == len(declared):
                    s = scope_summary("geometry")
                    print(f"    {n} of {len(declared)}: {s['stored']} stored, "
                          f"{s['missing']} missing, {s['failed']} failed",
                          flush=True)

        # -- terminal state -------------------------------------------------
        failed = sum(1 for r in records if r["outcome"] != "stored"
                     and not r["key"].startswith("models:"))
        run["state"] = "complete" if failed == 0 else "partial"
        run["reason"] = None if failed == 0 else (
            f"{failed} declared artifact(s) not stored")
    except _Stop as e:
        records.append(e.record)
        stop = e
        run["state"], run["reason"] = "failed", f"stopped: {e.record['error']}"
        print(f"\n  STOPPED: {e.record['error']}", flush=True)
    except Exception as e:                                      # noqa: BLE001
        run["state"], run["reason"] = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        fetcher.close()
        run["finished_at"] = _now()
        run["fetch_stats"] = dict(fetcher.stats)
        run["outcomes"] = {scope: scope_summary(scope)
                           for scope in run["scopes"]}
        _json(run_dir / MANIFEST_FILE, {
            "manifest_version": MANIFEST_VERSION,
            "run_id": run_dir.name, "source_id": source.id,
            "artifacts": records})
        _json(run_dir / RUN_FILE, run)
        write_sidecar(run_dir)
        s = run["outcomes"]
        print(f"\n  run {run_dir.name}: {run['state'].upper()}"
              + (f" ({run['reason']})" if run["reason"] else ""), flush=True)
        for scope, o in s.items():
            print(f"    {scope:<9} declared {o['declared']:>5}  stored "
                  f"{o['stored']:>5}  missing {o['missing']:>3}  failed "
                  f"{o['failed']:>3}  {o['bytes'] / 1024 / 1024:>7.1f} MB",
                  flush=True)
        print(f"    fetch: {fetcher.report()}", flush=True)
    if stop is not None:
        raise SourceUnhappy(stop.record["error"], stop.record["attempts"])
    return run


def _parse_stored_model(run_dir: Path, base: str, records: list) -> L.Landscape:
    """The model from the bytes just stored, via the one parser.

    Builds the `texts` shape `Landscape.parse` takes from the manifest
    records, so a shard the source answered 404 to is still declared as
    requested and reported absent, exactly as the live loader does.
    """
    by_key = {r["key"]: r for r in records}

    def text(key: str) -> str:
        r = by_key.get(key)
        if not r or r["outcome"] != "stored":
            return ""
        return (run_dir / r["path"]).read_text(encoding="utf-8",
                                               errors="replace")

    shards: dict = {}
    for r in records:
        if r["key"].startswith("shard:"):
            n = int(r["key"].split(":", 1)[1])
            shards[n] = text(r["key"]) if r["outcome"] == "stored" else None
    texts = {"mapping": text("index"), "shards": shards,
             "relations": text("relations"), "on_views": text("on_views")}
    return L.Landscape(base).parse(texts)


# --- fixity -----------------------------------------------------------------

def write_sidecar(run_dir: Path) -> int:
    """sha256 of every file in the run except the sidecar itself. Written
    last, so its presence means the run finished writing."""
    lines = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file() and p.name != SIDECAR_FILE:
            rel = str(p.relative_to(run_dir)).replace("\\", "/")
            lines.append(f"{_sha256(p.read_bytes())}  {rel}\n")
    (run_dir / SIDECAR_FILE).write_text("".join(lines), encoding="utf-8")
    return len(lines)


def read_sidecar(run_dir: Path) -> dict:
    out = {}
    path = run_dir / SIDECAR_FILE
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if "  " in line:
            digest, rel = line.split("  ", 1)
            out[rel] = digest
    return out


def verify_run(run_dir: Path) -> dict:
    """Re-verify a stored run against its sidecar and manifest.

    Returns counts with denominators. `ok` is True only when a sidecar
    exists, every file it names is present with the recorded digest, no file
    is present that it does not name, and every artifact the manifest says
    was stored is on disk with the manifest's digest.
    """
    result = {"ok": False, "sidecar": False, "files_listed": 0,
              "files_verified": 0, "files_mismatched": [], "files_absent": [],
              "files_stray": [], "artifacts_stored": 0,
              "artifacts_verified": 0, "artifacts_mismatched": []}
    listed = read_sidecar(run_dir)
    if not listed:
        return result
    result["sidecar"] = True
    result["files_listed"] = len(listed)
    for rel, digest in listed.items():
        p = run_dir / rel
        if not p.is_file():
            result["files_absent"].append(rel)
        elif _sha256(p.read_bytes()) != digest:
            result["files_mismatched"].append(rel)
        else:
            result["files_verified"] += 1
    for p in run_dir.rglob("*"):
        if p.is_file() and p.name != SIDECAR_FILE:
            rel = str(p.relative_to(run_dir)).replace("\\", "/")
            if rel not in listed:
                result["files_stray"].append(rel)

    manifest_path = run_dir / MANIFEST_FILE
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for r in manifest.get("artifacts", []):
            if r.get("outcome") != "stored":
                continue
            result["artifacts_stored"] += 1
            p = run_dir / r["path"]
            if p.is_file() and _sha256(p.read_bytes()) == r["sha256"]:
                result["artifacts_verified"] += 1
            else:
                result["artifacts_mismatched"].append(r["path"])

    result["ok"] = (not result["files_absent"] and not result["files_mismatched"]
                    and not result["files_stray"]
                    and not result["artifacts_mismatched"])
    return result
