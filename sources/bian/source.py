Thought for 46s

That's the right question to ask now, before there's a second source to untangle. The current layout has BIAN-specific logic, generic rendering, and publishing all fused together — fine for one source, wrong for several.

Let me restructure it properly.
Ran 10 commands, created 6 files, read a file · 4 notes

Restructured around a plugin model: core/ holds everything shared, sources/<id>/ holds everything specific, and adding a source touches only new files.

How each outcome is met:

Independent management. Sources are discovered by scanning sources/*/source.py for a class named Source — no central registry to edit, so adding or removing one can't disturb another. I tested this by dropping in a source that raises on harvest and another containing invalid Python: discovery reported both problems and continued, BIAN still listed and validated cleanly.

Different credentials. Each source declares required_secrets, namespaced by source id. The CLI refuses to harvest when any are missing rather than producing partial output, and each workflow injects only its own source's secrets.

Uniform publishing. Every source calls the same core/publish.py. It reads RCLONE_CONFIG_GDRIVE_* from the environment, so no credential file is ever written to a runner.

Publishing isolation — the one I'd draw your attention to. rclone sync deletes destination files absent from the source, which makes it genuinely dangerous if pointed at a shared root. So the sync is scoped to content/<source-id>/, never above it, and publish() refuses outright to sync a directory that's missing, empty, or has no manifest.json. I tested all three refusals: a failed BIAN harvest cannot wipe another source's published folder, or even its own.

Two design points worth knowing. The top-level content/index.md is rebuilt by a separate reindex workflow rather than by each source, because concurrent sources rewriting one shared file would race. And each source has one workflow with the publish step gated on an input — running with publish: false injects no Drive credentials at all, which is how you validate a harvester change safely.

Migration is in SETUP.md. The old flat files all go; the three GDRIVE_* secrets carry over unchanged. One Drive-side change: content moves from BIAN/ to content/bian/, so let the first run create the new location, then delete the old folder by hand.

sources/_template/ is a working example to copy — underscore-prefixed so discovery skips it. docs/ADDING_A_SOURCE.md walks through the six steps and the design rules that keep sources isolated.
Readme
Document · MD 
Setup
Document · MD 
Adding a source
Document · MD 
Run
PY 
Source
PY 
Render
PY 
Checks
PY 
Publish
PY 
Cli
PY 
Source
PY 
Source
PY 
Source bian
YML 
Reindex
YML 
.gitignore
GITIGNORE 

Update so that each source has a validate action that confirms that the content is able to be extracted. We want to be able to differentiate between issues with extracting content from individual source and publishing this to google drive.

When a new content source is onboarded, the validate script should be able to ensure that is can be connected to with the expected content extracted. The validation script should be structured so that is provides informative feedback in the event of failure that helps diagnose the cause
Thought for 22s

Good distinction to enforce structurally. Right now a failure tells you that something broke, not where. Let me add staged validation with a separate workflow, so "can't reach the source" and "can't write to Drive" are never confused.
Ran 16 commands, created 2 files, read a file · 8 notes

The two concerns are now structurally separated — a red run can't be ambiguous, because the two halves never share a workflow or an exit code.

Validate — BIAN Service Landscape references no Drive secrets at all. Check publishing target touches no source. The combined scheduled workflow runs extraction first and emits a distinct annotation depending on which half failed, so the Actions summary itself tells you where to look.

Staged validation is the new mechanism. Sources declare their endpoints in probes(), which runs before any parsing. Five stages execute in order and stop at the first failure, so you get one cause rather than a cascade.

I tested each failure class rather than assuming. core/diagnostics.py correctly distinguishes DNS failure, TLS failure, timeout, 401/403/404/429/5xx, redirect-to-login-gate, truncated body, and — importantly — an HTML error page served with HTTP 200, which is the failure that would otherwise look like a parser bug. Then I ran the full validator against a deliberately broken source in four states:

    Missing credentials → fails at Connectivity, naming the exact variables
    Host unreachable → fails at Connectivity with the DNS classification
    Reachable but parser throws → fails at Parse, and the hint says "the source was reachable and returned the expected payload, so this is a fault in sources/demo/source.py" — pointing at your code, not upstream
    Parses but yields a stub dataset → fails at Extract, with the canary check

Each failure prints what was observed, what the stage means, the per-check hint, and concrete next steps.

For onboarding, probes() is the thing to write before harvest(). Run validate and if Connectivity and Payload pass, you know the endpoint is reachable and correctly shaped, so you can write the parser knowing any further failure is yours. docs/ADDING_A_SOURCE.md leads with this now.

One nudge built in: a source with no probes() gets a warning saying connectivity is unverified — so skipping it is visible rather than silent.
Readme
Document · MD 
Setup
Document · MD 
Adding a source
Document · MD 
Diagnostics
PY 
Checks
PY 
Source
PY 
Cli
PY 
Publish
PY 
Render
PY 
Source
PY 
Source
PY 
Validate bian
YML 
Check publish
YML 
Source bian
YML 
Reindex
YML 
Run
PY 
Now using credits • Your plan limit resets Aug 22 at 9:00 PM.


Claude is AI and can make mistakes. Please double-check responses.
Source · PY
#!/usr/bin/env python3
"""
BIAN Service Landscape.
 
The landscape browser is a Backbone.js client that loads its entire dataset
from static JavaScript files — no API, no login, no rendering step:
 
    data/all_objects_data_16.js   -> var objectData     = {id: {...}}
    data/all_objects_relations.js -> var objectRelations = {id: [{via, to}]}
 
Keys are the same object ids used in object_16.html?object=NNNNN, so this is a
download-and-parse job rather than a crawl.
 
Credentials: none. The files are served unauthenticated.
"""
 
from __future__ import annotations
 
import json
import re
import urllib.request
from pathlib import Path
 
from core.diagnostics import ProbeSpec
from core.render import clean_html, write_bundles
from core.source import Check, HarvestResult, Stage
from core.source import Source as BaseSource
 
# --- version pinning -------------------------------------------------------
# When BIAN publishes a new landscape you receive a fresh link by email.
# Update these two, then re-run validation.
BASE = "https://bian.org/servicelandscape-14-0-0"
VIEW = 16
# ---------------------------------------------------------------------------
 
FILES = {
    "objects":   f"{BASE}/data/all_objects_data_{VIEW}.js",
    "relations": f"{BASE}/data/all_objects_relations.js",
    "mapping":   f"{BASE}/data/all_objects_data_mapping.js",
    "on_views":  f"{BASE}/data/all_objects_on_views.js",
    "config":    f"{BASE}/data/config_data.js",
}
 
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
TIMEOUT = 120
 
# Canary: a known service domain. If BIAN restructures or the parser drifts,
# validation fails loudly instead of quietly producing thinner output.
CANARY_ID = "34300"
CANARY_NAME = "Consumer Loan"
MIN_OBJECTS = 1000
MIN_SERVICE_DOMAINS = 100
 
SKIP_RELATION_VERBS = {"", "<unknown role>"}
SKIP_RELATION_NAMES = {"Realization relation"}
 
 
def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")
 
 
def _parse_js_assignment(text: str):
    """`var name = <json>;` -> Python object."""
    m = re.match(r"\s*var\s+\w+\s*=\s*", text)
    if not m:
        raise ValueError("unexpected file format — no var assignment")
    return json.loads(re.sub(r";\s*$", "", text[m.end():].strip()))
 
 
def _stereotypes(entry: dict) -> list[str]:
    for cat in entry.get("categories", []):
        if cat.get("type") == "table":
            st = cat.get("content", {}).get("Stereotypes", {}).get("stereotype", {})
            return list(st.get("value", []))
    return []
 
 
def _properties(entry: dict) -> dict:
    for cat in entry.get("categories", []):
        if cat.get("type") == "table":
            return cat.get("content", {})
    return {}
 
 
def _documentation(entry: dict) -> dict:
    out = {}
    for cat in entry.get("categories", []):
        if cat.get("type") != "documentation":
            continue
        text = clean_html(cat.get("content", {}).get("value", ""))
        if text:
            out[cat.get("title") or "documentation"] = text
    return out
 
 
def _flatten(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "link":
            v = value.get("value", {})
            return f"{v.get('title', '')} — {v.get('location', '')}".strip(" —")
        if kind == "object":
            return value.get("value", {}).get("name", "")
        if kind == "collection":
            return [x for x in (_flatten(i) for i in value.get("value", [])) if x]
    return ""
 
 
def _relations_block(oid, relations, names) -> list[str]:
    rels = relations.get(str(oid)) or []
    if not rels:
        return []
    lines = ["### Relationships"]
    for rel in sorted(rels, key=lambda r: r.get("via", "")):
        via = (rel.get("via") or "").strip()
        if via in SKIP_RELATION_VERBS:
            continue
        targets = sorted(
            f"{names[str(t)]} ({t})" for t in rel.get("to", [])
            if names.get(str(t)) and names[str(t)] not in SKIP_RELATION_NAMES)
        if targets:
            lines.append(f"- **{via}:** " + "; ".join(targets))
    return lines + [""] if len(lines) > 1 else []
 
 
def _render(oid, entry, relations, names) -> tuple[str, str]:
    sts = _stereotypes(entry)
    otype = entry.get("type", "")
    lines = [
        f"## {entry.get('name', f'Object {oid}')}", "",
        f"- **Object id:** {oid}",
        f"- **Type:** {otype}" + (f" ({', '.join(sts)})" if sts else ""),
        f"- **Source:** {BASE}/object_{VIEW}.html?object={oid}", "",
    ]
 
    for title, text in _documentation(entry).items():
        lines += [f"### {'Description' if title == 'documentation' else title}",
                  text, ""]
 
    for group, fields in _properties(entry).items():
        if group == "Stereotypes" or not isinstance(fields, dict):
            continue
        rows = []
        for key, raw in fields.items():
            val = _flatten(raw)
            if isinstance(val, list):
                if val:
                    rows.append(f"- **{key}:** ({len(val)})")
                    rows += [f"  - {v}" for v in val]
            elif val:
                rows.append(f"- **{key}:** "
                            + " / ".join(v.strip() for v in val.split("\n") if v.strip()))
        if rows:
            lines += [f"### {group}", *rows, ""]
 
    lines += _relations_block(oid, relations, names)
    lines += ["---", ""]
    return "\n".join(lines), (sts[0] if sts else otype or "Other")
 
 
class Source(BaseSource):
    id = "bian"
    name = "BIAN Service Landscape"
    description = "Banking Industry Architecture Network service domain model"
    required_secrets: list[str] = []      # public static files
    schedule = "0 3 * * 1"
 
    def probes(self) -> list[ProbeSpec]:
        """What must be reachable, and what each endpoint should return.
 
        Each data file is a `var <name> = {...};` assignment, so asserting the
        prefix catches the common failure where bian.org returns an HTML error
        page or a login redirect with a 200 status.
        """
        return [
            ProbeSpec(
                label="landscape data (all_objects_data_%d.js)" % VIEW,
                url=FILES["objects"],
                expect_prefix="var objectData",
                min_bytes=200_000),
            ProbeSpec(
                label="relationship graph (all_objects_relations.js)",
                url=FILES["relations"],
                expect_prefix="var objectRelations",
                min_bytes=10_000),
            ProbeSpec(
                label="language config (config_data.js)",
                url=FILES["config"],
                expect_prefix="var availableLanguages",
                min_bytes=10),
            ProbeSpec(
                label="view mapping (all_objects_data_mapping.js)",
                url=FILES["mapping"],
                expect_prefix="var ",
                optional=True),
            ProbeSpec(
                label="objects-on-views (all_objects_on_views.js)",
                url=FILES["on_views"],
                expect_prefix="var ",
                optional=True),
        ]
 
    def harvest(self, outdir: Path) -> HarvestResult:
        raw = {}
        for key, url in FILES.items():
            text = _download(url)
            print(f"  {key:<10} {len(text) / 1024:>8.0f} KB", flush=True)
            raw[key] = text
 
        objects = _parse_js_assignment(raw["objects"])
        try:
            relations = _parse_js_assignment(raw["relations"])
        except Exception:
            relations = {}
 
        names = {oid: (o.get("data") or [{}])[0].get("name", "")
                 for oid, o in objects.items()}
 
        items = []
        for oid, obj in objects.items():
            data = obj.get("data") or []
            if not data:
                continue
            body, category = _render(oid, data[0], relations, names)
            items.append({"id": oid, "name": data[0].get("name", ""),
                          "category": category, "body": body})
 
        written = write_bundles(
            outdir, self.id, self.name, items,
            extra_index_lines=[
                f"Landscape version: `{BASE.rsplit('/', 1)[-1]}`, view {VIEW}.",
                f"Objects with relationships: {len(relations)}.",
            ])
 
        return HarvestResult(
            source_id=self.id,
            item_count=len(items),
            categories=written["categories"],
            files_written=written["files_written"],
            notes=[f"{len(relations)} objects carry relationships"],
        )
 
    def checks(self, outdir: Path) -> list[Check]:
        out: list[Check] = []
        manifest = json.loads((outdir / "manifest.json").read_text())
        items = manifest.get("items", {})
        cats = manifest.get("categories", {})
 
        out.append(Check(
            "object count", len(items) >= MIN_OBJECTS,
            f"{len(items)} (min {MIN_OBJECTS})", stage=Stage.EXTRACT,
            hint="Far fewer objects than the landscape contains. Either the "
                 "data file was truncated, or objectData is now nested "
                 "differently and only part of it is being read."))
        out.append(Check(
            "service domains found",
            cats.get("ServiceDomain", 0) >= MIN_SERVICE_DOMAINS,
            f"{cats.get('ServiceDomain', 0)} (min {MIN_SERVICE_DOMAINS})",
            stage=Stage.EXTRACT,
            hint="Objects were extracted but few classified as ServiceDomain. "
                 "BIAN has probably renamed the stereotype — check the "
                 "Stereotypes block in the raw data."))
 
        canary = items.get(CANARY_ID, {})
        out.append(Check(
            f"canary object {CANARY_ID} acquired", bool(canary),
            stage=Stage.EXTRACT,
            hint=f"Object {CANARY_ID} ({CANARY_NAME}) is missing. If BIAN "
                 f"genuinely retired it, pick a new canary; otherwise the "
                 f"extraction is dropping objects."))
        out.append(Check(
            "canary name matches", CANARY_NAME in canary.get("name", ""),
            f"got {canary.get('name', '(missing)')!r}", stage=Stage.EXTRACT,
            hint="The id resolved but to a different object — ids may have "
                 "been reassigned in a new landscape version."))
        out.append(Check(
            "canary is a service domain",
            canary.get("category") == "ServiceDomain",
            f"got {canary.get('category')!r}", stage=Stage.EXTRACT,
            hint="Categorisation is reading the wrong field; check "
                 "_stereotypes()."))
 
        body = "\n".join(f.read_text(encoding="utf-8")
                         for f in outdir.glob("*.md"))
        out.append(Check(
            "role definitions rendered",
            body.count("### 1. Role Definition") >= MIN_SERVICE_DOMAINS,
            f"{body.count('### 1. Role Definition')} sections",
            stage=Stage.RENDER,
            hint="Service domains were found but their documentation is not "
                 "reaching the markdown — check _documentation()."))
        out.append(Check(
            "relationships rendered",
            body.count("### Relationships") >= len(items) // 4,
            f"{body.count('### Relationships')} sections", stage=Stage.RENDER,
            hint="The relations file parsed but few edges resolved. Ids in "
                 "all_objects_relations.js may not match objectData keys."))
        out.append(Check(
            "portal API links preserved",
            body.count("portal.bian.org/service-domain-api/") >= 50,
            f"{body.count('portal.bian.org/service-domain-api/')} links",
            warn=True, stage=Stage.RENDER,
            hint="Expected in service domain property tables. A legitimate "
                 "drop can happen between BIAN versions."))
        out.append(Check(
            "no translate placeholders", body.count("__is_translate") == 0,
            f"{body.count('__is_translate')} found", stage=Stage.RENDER,
            hint="Internal BIAN placeholder keys reached the output — the "
                 "property-table title filter needs updating."))
        out.append(Check(
            "no unresolved relation ids",
            len(re.findall(r"^- \*\*(?!Object id)[^*]+:\*\* [\d; ]+$",
                           body, re.M)) == 0,
            stage=Stage.RENDER,
            hint="Relationship targets rendered as bare numbers, meaning the "
                 "id-to-name lookup missed them."))
        return out
 
