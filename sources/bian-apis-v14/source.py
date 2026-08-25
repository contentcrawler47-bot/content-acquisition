#!/usr/bin/env python3
"""
BIAN Semantic APIs, release 14.0.0.

The companion to sources/bian-v14/. The landscape says what a service domain
*is*; this says what it *exposes*. They join on the service domain name, which
is the point of harvesting it at all.

Acquisition is ONE request. The whole repository is 57 MB as a zipball against
774 individual files (258 domains x 3 sets), so the archive is both politer to
GitHub and faster. Nothing here is paced or cached because nothing here makes
a second request.

Three sets are read, and they are NOT three equal inputs:

  semantic-apis/oas3          operations           -- the API surface
  apis-iso20022_ext-ddd/oas3  ISO 20022 types only -- SAME operations, verified
  semantic-apis/asyncapi-3.x  event channels       -- genuinely additive

The ISO set was measured against the semantic set on ConsumerLoan: the same 29
operations at the same 29 paths, differing only in operationId convention
("RetrieveCharge" vs "Charge/Retrieve"). Rendering both would duplicate every
operation in the bundle for no information, so operations come from the
semantic set alone and the ISO set contributes the type vocabulary. That
equivalence is re-asserted at harvest time rather than trusted -- see
`iso_path_agreement` in checks().

Credentials: none. Apache-2.0, served unauthenticated.
"""

from __future__ import annotations

import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

import yaml

from core.diagnostics import ProbeSpec
from core.render import write_bundles
from core.source import Check, HarvestResult, Stage
from core.source import Source as BaseSource

UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
HTTP_VERBS = ("get", "put", "post", "delete", "patch")


class Source(BaseSource):
    id = "bian-apis-v14"
    name = "BIAN Semantic APIs v14"
    description = "BIAN service domain API operations, types and event channels"

    required_secrets: list[str] = []

    # --- version pinning ----------------------------------------------
    # To add release 13, copy this directory, change `release` and re-run the
    # numbers in Sanity below against that release. Counts differ between
    # releases and a canary borrowed from 14 may not resolve.
    repo = "bian-official/public"
    release = "release14.0.0"
    zip_url = "https://codeload.github.com/bian-official/public/zip/refs/heads/main"
    raw_base = "https://raw.githubusercontent.com/bian-official/public/main"

    # NOTE THE TRAILING SPACE after "oas3" in the semantic path. It is real,
    # it is in the upstream git tree, and it is NOT present in the ISO path
    # below. Any code that normalises both paths the same way finds nothing in
    # one of them. Over HTTP it must be encoded %20.
    oas_dir = "semantic-apis/oas3 /yamls"
    iso_dir = "apis-iso20022_ext-ddd/oas3/yamls"
    async_dir = "semantic-apis/asyncapi-3.x/yamls"

    # --- sanity: verified against release 14.0.0, 25 August 2026 -------
    # Canary: a known service domain, its control record, and one operation.
    canary_stem = "ConsumerLoan"
    canary_name = "Consumer Loan"
    canary_control_record = "ConsumerLoanFacility"

    # 258 service domains. NOT 259 -- each set also carries a 1-byte
    # Readme.md placeholder, which is not a domain.
    expected_domains = 258
    min_domains = 250

    # 4,580 operations across the semantic set, in only three verbs.
    expected_operations = 4580
    min_operations = 4000

    # 16 domains legitimately declare `paths: {}` and yield no operations at
    # all. A check that demanded operations everywhere would fail a correct
    # harvest, so the bound is an upper one.
    max_domains_without_operations = 30

    # 19,090 schemas exist but ~18,000 are boilerplate repeated in every file
    # (HTTPError appears in all 258). Only control record and behaviour
    # qualifier schemas are rendered.
    min_structures = 800
    # ------------------------------------------------------------------

    schedule = "0 5 * * 1"        # staggered clear of BIAN v14 at 03:00

    # -- probes ---------------------------------------------------------

    def probes(self) -> list[ProbeSpec]:
        """One cheap file from each of the three sets.

        IncentiveAccount is used rather than the canary because it is the
        smallest file in the set (2.9 KB against 308 KB) and a probe should
        not move a third of a megabyte to prove reachability.

        The first two probes differ only in the trailing space, which is the
        single most likely thing to break silently if BIAN tidies the path.
        """
        return [
            ProbeSpec(
                label="semantic OAS3 (trailing-space path)",
                url=f"{self.raw_base}/{self.release}/"
                    "semantic-apis/oas3%20/yamls/IncentiveAccount.yaml",
                expect_contains="openapi",
                min_bytes=500),
            ProbeSpec(
                label="ISO 20022 OAS3 (no trailing space)",
                url=f"{self.raw_base}/{self.release}/"
                    "apis-iso20022_ext-ddd/oas3/yamls/IncentiveAccount.yaml",
                expect_contains="openapi",
                min_bytes=500),
            ProbeSpec(
                label="semantic AsyncAPI",
                url=f"{self.raw_base}/{self.release}/"
                    "semantic-apis/asyncapi-3.x/yamls/IncentiveAccount.yaml",
                expect_contains="asyncapi",
                min_bytes=500),
        ]

    # -- harvest --------------------------------------------------------

    def harvest(self, outdir: Path) -> HarvestResult:
        archive = self._download()
        sets = self._read_sets(archive)

        stems = sorted(sets["oas"])
        items, stats = [], {
            "operations": 0, "structures": 0, "channels": 0,
            "no_operations": 0, "unresolved_refs": 0, "iso_disagreements": 0,
        }

        for stem in stems:
            item, per = self._render_domain(
                stem,
                sets["oas"].get(stem),
                sets["iso"].get(stem),
                sets["async"].get(stem))
            items.append(item)
            for k, v in per.items():
                stats[k] += v

        notes = [
            f"{stats['operations']} operations, {stats['structures']} "
            f"structures, {stats['channels']} event channels",
            f"{stats['no_operations']} domains declare no operations",
        ]
        if stats["unresolved_refs"]:
            notes.append(f"{stats['unresolved_refs']} schema refs unresolved")
        if stats["iso_disagreements"]:
            notes.append(
                f"{stats['iso_disagreements']} domains where the ISO path set "
                "differs from the semantic set")

        written = write_bundles(
            outdir, self.id, self.name, items,
            per_file=60,
            extra_index_lines=[
                f"BIAN {self.release}, {len(items)} service domain APIs.",
                "",
                "Operations come from the semantic OpenAPI set. The ISO 20022 "
                "set describes the same operations at the same paths, so it "
                "contributes its type vocabulary only. Event channels come "
                "from the AsyncAPI set.",
            ])

        self._stats = stats
        (outdir / "harvest.json").write_text(
            json.dumps({
                "source": self.id,
                "release": self.release,
                "repo": self.repo,
                "domains": len(items),
                **stats,
            }, indent=2), encoding="utf-8")

        return HarvestResult(
            source_id=self.id,
            item_count=len(items),
            categories=written["categories"],
            files_written=written["files_written"] + 1,
            notes=notes,
        )

    # -- acquisition ----------------------------------------------------

    def _download(self) -> zipfile.ZipFile:
        req = urllib.request.Request(self.zip_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
        return zipfile.ZipFile(io.BytesIO(raw))

    def _read_sets(self, z: zipfile.ZipFile) -> dict:
        """Parse the three sets, keyed by filename stem.

        The zip's root directory name depends on the branch, so it is derived
        rather than assumed.
        """
        names = z.namelist()
        root = names[0].split("/")[0] + "/" if names else ""
        out = {}
        for key, sub in (("oas", self.oas_dir),
                         ("iso", self.iso_dir),
                         ("async", self.async_dir)):
            prefix = f"{root}{self.release}/{sub}/"
            docs = {}
            for n in names:
                if not n.startswith(prefix) or not n.endswith(".yaml"):
                    continue
                stem = n[len(prefix):-len(".yaml")]
                if "/" in stem:
                    continue
                try:
                    docs[stem] = yaml.safe_load(
                        z.read(n).decode("utf-8", errors="replace"))
                except yaml.YAMLError:
                    docs[stem] = None
            out[key] = docs
        return out

    # -- rendering ------------------------------------------------------

    @staticmethod
    def _resolve(doc: dict, ref: str):
        """Follow a local $ref. Returns None for anything non-local."""
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return None
        node = doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _schema_for(self, doc: dict, op: dict) -> tuple[str | None, bool]:
        """Find the schema an operation carries, by following refs.

        Matching a schema name against a tag name would be simpler and would
        usually agree, but "usually agree" is how this project has been bitten
        before. Returns (schema_name, unresolved).
        """
        candidates = []
        body = op.get("requestBody")
        if isinstance(body, dict):
            candidates.append(body)
        responses = op.get("responses")
        if isinstance(responses, dict):
            for code in ("200", "201", 200, 201):
                if code in responses:
                    candidates.append(responses[code])
                    break

        for node in candidates:
            if "$ref" in node:
                target = self._resolve(doc, node["$ref"])
                if target is None:
                    return None, True
                node = target
            content = (node or {}).get("content") or {}
            for media in content.values():
                ref = ((media or {}).get("schema") or {}).get("$ref")
                if isinstance(ref, str) and "/schemas/" in ref:
                    return ref.rsplit("/", 1)[-1], False
        return None, bool(candidates)

    @staticmethod
    def _operations(doc: dict) -> list[tuple[str, str, dict]]:
        out = []
        for path, item in ((doc or {}).get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for verb, op in item.items():
                if verb.lower() in HTTP_VERBS and isinstance(op, dict):
                    out.append((path, verb.upper(), op))
        return out

    def _render_domain(self, stem, oas, iso, asy) -> tuple[dict, dict]:
        per = {"operations": 0, "structures": 0, "channels": 0,
               "no_operations": 0, "unresolved_refs": 0, "iso_disagreements": 0}
        info = (oas or {}).get("info") or {}
        title = info.get("title") or _spaced(stem)
        version = info.get("version") or self.release

        groups: dict[str, list] = {}
        schema_of: dict[str, str] = {}
        for path, verb, op in self._operations(oas):
            tag = (op.get("tags") or ["(untagged)"])[0]
            groups.setdefault(tag, []).append(
                (op.get("operationId") or "", verb, path,
                 (op.get("summary") or "").strip()))
            per["operations"] += 1
            if tag not in schema_of:
                name, unresolved = self._schema_for(oas, op)
                if name:
                    schema_of[tag] = name
                elif unresolved:
                    per["unresolved_refs"] += 1

        if not groups:
            per["no_operations"] += 1

        # The ISO set must describe the same paths. Asserted per domain, not
        # assumed from the one domain it was measured on.
        if iso:
            if {p for p, _v, _o in self._operations(iso)} != {
                    p for p, _v, _o in self._operations(oas)}:
                per["iso_disagreements"] += 1

        crs = sorted(g for g in groups if g.startswith("CR "))
        bqs = sorted(g for g in groups if g.startswith("BQ "))
        other = sorted(g for g in groups if g not in crs and g not in bqs)

        body = [f"## {title}", ""]
        desc = (info.get("description") or "").strip()
        if desc:
            body += [_first_paragraph(desc), ""]
        body += [
            f"BIAN {version} — {len(crs)} control record, "
            f"{len(bqs)} behaviour qualifiers, {per['operations']} operations.",
            "",
        ]
        if not groups:
            body += ["This service domain declares no API operations at "
                     "release 14.0.0.", ""]

        schemas = ((oas or {}).get("components") or {}).get("schemas") or {}
        for label, keys in (("Control record", crs),
                            ("Behaviour qualifier", bqs),
                            ("Other", other)):
            for tag in keys:
                name = tag.split(" - ", 1)[-1]
                body += [f"### {label} — {name}", ""]
                body += ["| Operation | Method | Path |", "|---|---|---|"]
                for oid, verb, path, _s in sorted(groups[tag]):
                    body.append(f"| {oid} | {verb} | `{path}` |")
                body.append("")
                rows = _properties(schemas.get(schema_of.get(tag)))
                if rows:
                    per["structures"] += 1
                    body += [f"**{name} structure**", "",
                             "| Attribute | Type |", "|---|---|"]
                    body += [f"| {n} | {t} |" for n, t in rows]
                    body.append("")

        iso_types = _iso_only_types(oas, iso)
        if iso_types:
            body += ["### ISO 20022 types", "",
                     "Types the ISO 20022 variant substitutes for this "
                     "domain's semantic types.", "",
                     ", ".join(f"`{t}`" for t in iso_types), ""]

        channels = sorted((asy or {}).get("channels") or {})
        if channels:
            per["channels"] += len(channels)
            body += [f"### Event channels ({len(channels)})", "",
                     ", ".join(f"`{c}`" for c in channels), ""]

        body.append("---")
        return ({
            "id": stem,
            "name": title,
            "category": "Service domain APIs",
            "body": "\n".join(body) + "\n",
        }, per)

    # -- checks ---------------------------------------------------------

    def checks(self, outdir: Path) -> list[Check]:
        manifest = json.loads((outdir / "manifest.json").read_text())
        items = manifest.get("items", {})
        stats = json.loads((outdir / "harvest.json").read_text())

        n = len(items)
        ops = stats.get("operations", 0)
        out = [
            Check("service domain count", n >= self.min_domains,
                  f"{n} domains (expected {self.expected_domains})",
                  warn=(n != self.expected_domains and n >= self.min_domains),
                  stage=Stage.EXTRACT,
                  hint="Each set holds 258 domains plus a 1-byte Readme.md "
                       "placeholder, which is not a domain. A count of 259 "
                       "means the placeholder is being counted."),
            Check("operation count", ops >= self.min_operations,
                  f"{ops} operations (expected {self.expected_operations})",
                  stage=Stage.EXTRACT,
                  hint="Well below 4,580 usually means only one of the three "
                       "sets was read — check the trailing space in oas_dir."),
            Check("canary domain present", self.canary_stem in items,
                  self.canary_stem, stage=Stage.EXTRACT,
                  hint="ConsumerLoan is missing. If it was legitimately "
                       "renamed upstream, choose a new canary."),
            Check("canary named correctly",
                  items.get(self.canary_stem, {}).get("name") == self.canary_name,
                  items.get(self.canary_stem, {}).get("name", "absent"),
                  stage=Stage.PARSE,
                  hint="info.title is not what it was. The join to the "
                       "landscape is on this name, so it matters."),
            Check("structures rendered",
                  stats.get("structures", 0) >= self.min_structures,
                  f"{stats.get('structures', 0)} structures",
                  stage=Stage.RENDER,
                  hint="Control record structure is resolved by following "
                       "$ref from each operation. A collapse to zero means "
                       "the components layout changed."),
            Check("domains without operations",
                  stats.get("no_operations", 0) <= self.max_domains_without_operations,
                  f"{stats.get('no_operations', 0)} of {n} declare none",
                  stage=Stage.EXTRACT,
                  hint="16 domains legitimately declare `paths: {}` at 14.0.0. "
                       "A large rise means paths are being missed, not that "
                       "BIAN removed them."),
            Check("ISO path agreement",
                  stats.get("iso_disagreements", 0) == 0,
                  f"{stats.get('iso_disagreements', 0)} domains disagree",
                  warn=True, stage=Stage.PARSE,
                  hint="The ISO set is rendered as types only because it was "
                       "measured to describe the SAME paths as the semantic "
                       "set. If that stops holding, its operations are being "
                       "silently dropped and it needs its own category."),
            Check("schema refs resolved",
                  stats.get("unresolved_refs", 0) == 0,
                  f"{stats.get('unresolved_refs', 0)} unresolved",
                  warn=True, stage=Stage.PARSE,
                  hint="Counted rather than assumed zero. A rise means the "
                       "requestBodies/responses indirection changed shape."),
        ]
        return out


# -- helpers ------------------------------------------------------------

def _spaced(stem: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)


def _first_paragraph(text: str) -> str:
    for block in text.split("\n\n"):
        block = block.strip()
        if block:
            return " ".join(block.split())
    return ""


def _properties(schema) -> list[tuple[str, str]]:
    props = (schema or {}).get("properties") or {}
    rows = []
    for name, spec in props.items():
        spec = spec or {}
        kind = spec.get("type")
        if not kind:
            ref = spec.get("$ref") or (
                (spec.get("items") or {}).get("$ref") if spec.get("items") else None)
            kind = ref.rsplit("/", 1)[-1] if isinstance(ref, str) else "object"
        if spec.get("type") == "array":
            inner = (spec.get("items") or {})
            ref = inner.get("$ref")
            kind = (f"array of {ref.rsplit('/', 1)[-1]}" if isinstance(ref, str)
                    else f"array of {inner.get('type', 'object')}")
        rows.append((name, kind))
    return rows


def _iso_only_types(oas, iso) -> list[str]:
    """Schema names present in the ISO variant and absent from the semantic
    one — the type vocabulary that is the ISO set's actual contribution."""
    if not iso:
        return []
    a = set(((oas or {}).get("components") or {}).get("schemas") or {})
    b = set(((iso or {}).get("components") or {}).get("schemas") or {})
    return sorted(b - a)
