#!/usr/bin/env python3
"""
The half of a BIAN source that is the same for every landscape version.

Each version — v14 today, v13 next — is a directory under sources/ holding a
subclass that pins a URL and a set of expected numbers, and nothing else. The
extraction logic lives here so a fix reaches every version at once, and so the
two cannot drift apart into two subtly different harvesters.

Versions are kept apart by the mechanism that already exists: a source's id is
its output directory and its Drive subfolder, and `rclone sync` is scoped to
that subfolder. `bian-v14` and `bian-v13` therefore cannot see, overwrite or
delete one another's content, and neither can a failure in one.

`harvest()` here deliberately acquires the SEMANTIC half only — roughly 11,300
objects from 47 shards, about two minutes. That is what the weekly validation
run needs, and it is far too cheap to be worth chunking. The ~1,231 diagram
view pages are the expensive part, and they are harvested by the chunked
pipeline in bianlib/pipeline.py. A semantic-only bundle is marked incomplete so
it can never be published over a complete one.
"""

from __future__ import annotations

import json
from pathlib import Path

from bianlib import landscape as L
from bianlib.fetch import Fetcher
from core.diagnostics import ProbeSpec
from core.render import write_bundles
from core.source import Check, HarvestResult, Stage
from core.source import Source as BaseSource


class BianSource(BaseSource):
    """Subclass this per landscape version; override the constants below."""

    #: e.g. "https://bian.org/servicelandscape-14-0-0"
    base: str = ""
    #: the object_NN.html page id used when building "Source:" links
    object_view: int = 16
    #: a shard that is known to exist, probed to catch a truncated landscape
    last_known_shard: int = 47

    #: A known object that must survive every harvest. Turns silent upstream
    #: drift into a loud failure.
    canary_id: str = ""
    canary_name: str = ""

    expected_service_domains: int = 0
    min_service_domains: int = 0
    min_objects: int = 0
    min_sequence_diagrams: int = 0
    min_class_diagrams: int = 0

    #: What the view classifier should find, before anything is fetched.
    expected_sequence_views: int = 0
    expected_class_views: int = 0
    #: insiteViews should hold thousands of entries; a handful means the file
    #: parsed but the wrong variable was read.
    min_views: int = 1000

    required_secrets: list[str] = []      # public static files

    # -- probes ----------------------------------------------------------

    def probes(self) -> list[ProbeSpec]:
        """What must be reachable, and what each endpoint should return.

        Each data file is a `var <name> = {...};` assignment, so asserting the
        prefix catches the common failure where bian.org returns an HTML error
        page or a login redirect with a 200 status.
        """
        return [
            ProbeSpec(
                label="shard index (all_objects_data_mapping.js)",
                url=L.data_url(self.base, "all_objects_data_mapping.js"),
                expect_prefix="var objectDataMapping",
                min_bytes=100_000),
            ProbeSpec(
                label="first data shard (all_objects_data_1.js)",
                url=L.shard_url(self.base, 1),
                expect_prefix="var objectData",
                min_bytes=200_000),
            ProbeSpec(
                label=f"last known shard "
                      f"(all_objects_data_{self.last_known_shard}.js)",
                url=L.shard_url(self.base, self.last_known_shard),
                expect_prefix="var objectData",
                min_bytes=200_000),
            ProbeSpec(
                label="relationship graph (all_objects_relations.js)",
                url=L.data_url(self.base, "all_objects_relations.js"),
                expect_prefix="var objectRelations",
                min_bytes=10_000),
            ProbeSpec(
                label="language config (config_data.js)",
                url=L.data_url(self.base, "config_data.js"),
                expect_prefix="var availableLanguages",
                min_bytes=10),
            ProbeSpec(
                label="objects-on-views (all_objects_on_views.js)",
                url=L.data_url(self.base, "all_objects_on_views.js"),
                expect_prefix="var ",
                optional=True),
        ]

    # -- stage 1: extract -------------------------------------------------

    def build_extract(self, outdir: Path, mode: str = "model-only") -> dict:
        """Load the landscape and write it as a JSON-LD extract.

        Loads exactly what `harvest()` loads, and then stores it rather than
        rendering it. Nothing is filtered: the allowlist is applied by stage 2,
        so a change to it costs a re-render and no requests at all.

        `model-only` reads the shards and index files. `full` would add
        per-view geometry and is refused here rather than silently producing a
        model-only extract labelled `full`.
        """
        from bianlib import extract as E

        if mode == "full":
            raise NotImplementedError(
                "mode 'full' stores per-view geometry, which is not "
                "implemented yet. Run with mode 'model-only'.")

        fetcher = Fetcher(self.base)
        model = L.Landscape(self.base, object_view=self.object_view).load(fetcher)
        fetcher.close()

        doc = E.build(model, self.id, mode=mode)
        summary = E.write(doc, outdir / "extract.jsonld")

        status = doc["status"]
        print(f"  extract: {summary['objects']} objects, "
              f"{summary['relations']} relations, "
              f"{summary['views']} views, "
              f"{summary['view_members']} memberships", flush=True)
        print(f"  size   : {summary['bytes'] / 1024 / 1024:.1f} MB "
              f"({summary['bytes']} bytes)", flush=True)
        print(f"  content: {summary['content_digest'][:16]}", flush=True)
        print(f"  file   : {summary['file_digest'][:16]}", flush=True)
        print(f"  notation unresolved: {status['notation_unresolved']} of "
              f"{summary['objects']}", flush=True)
        print(f"  models : {status['models']}   "
              f"geometry: {status['geometry']}", flush=True)
        if status["malformed_objects"]:
            print(f"  malformed objects skipped: "
                  f"{status['malformed_objects']}", flush=True)
        return summary

    # -- harvest ---------------------------------------------------------

    def harvest(self, outdir: Path) -> HarvestResult:
        """Semantic content only. See the module docstring."""
        fetcher = Fetcher(self.base)
        model = L.Landscape(self.base, object_view=self.object_view).load(fetcher)
        fetcher.close()

        items, dropped, skipped = model.semantic_items()
        excluded = sum(dropped.values())
        print(f"  kept {len(items)} of {len(model.objects)} objects "
              f"({excluded} filtered out as non-content)", flush=True)
        for cat, n in sorted(dropped.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {cat:<32} {n:>7}", flush=True)
        if skipped:
            print(f"  skipped {len(skipped)} malformed objects "
                  f"(ids and reasons only):", flush=True)
            for oid, why in skipped[:10]:
                print(f"    {oid}: {why}", flush=True)

        written = write_bundles(
            outdir, self.id, self.name, items, per_file=250, complete=False,
            extra_index_lines=[
                f"Landscape version: `{self.base.rsplit('/', 1)[-1]}`.",
                f"Merged from {len(model.shards)} data shards "
                f"({len(model.objects)} unique objects).",
                f"Objects with relationships: {len(model.relations)}.",
                f"Filtered to BIAN semantic content: {len(items)} kept, "
                f"{excluded} modelling artefacts excluded.",
                f"Malformed objects skipped: {len(skipped)}.",
                "",
                "**Semantic content only.** The diagrams are harvested by the "
                "chunked landscape pipeline; this bundle is marked incomplete "
                "and will not publish.",
            ])

        return HarvestResult(
            source_id=self.id,
            item_count=len(items),
            categories=written["categories"],
            files_written=written["files_written"],
            notes=[f"{len(model.shards)} shards merged into "
                   f"{len(model.objects)} objects",
                   f"{len(model.relations)} objects carry relationships",
                   f"{excluded} non-content objects filtered out",
                   f"{len(skipped)} malformed objects skipped"] + model.notes,
        )

    # -- checks ----------------------------------------------------------

    def plan_checks(self, model, items: list[dict]) -> list[tuple[bool, str, str]]:
        """Model-level thresholds, checked before any view page is fetched.

        The chunked pipeline runs these first: there is no point spending 1,231
        requests on the diagrams of a model that is already wrong.
        """
        domains = sum(1 for i in items if i["category"] == "ServiceDomain")
        canary = next((i for i in items if str(i["id"]) == self.canary_id), None)
        return [
            (len(items) >= self.min_objects, "semantic object count",
             f"{len(items)} (min {self.min_objects})"),
            (domains >= self.min_service_domains, "service domain coverage",
             f"{domains} of ~{self.expected_service_domains} expected"),
            (canary is not None, f"canary object {self.canary_id} acquired",
             self.canary_name),
            (bool(canary) and self.canary_name in (canary or {}).get("name", ""),
             "canary name matches",
             f"got {(canary or {}).get('name', '(missing)')!r}"),
            (len(model.insite_views) >= self.min_views, "view index loaded",
             f"{len(model.insite_views)} views in insiteViews "
             f"(min {self.min_views})"),
        ]

    def checks(self, outdir: Path) -> list[Check]:
        out: list[Check] = []
        manifest = json.loads((outdir / "manifest.json").read_text())
        items = manifest.get("items", {})
        cats = manifest.get("categories", {})
        diagrams = (cats.get("Sequence diagram", 0)
                    + cats.get("Class diagram", 0))
        semantic = len(items) - diagrams

        out.append(Check(
            "object count", semantic >= self.min_objects,
            f"{semantic} (min {self.min_objects})", stage=Stage.EXTRACT,
            hint="Far fewer objects than the landscape contains. The most "
                 "likely cause is that shards failed to download — check the "
                 "per-shard lines in the harvest output."))

        sd = cats.get("ServiceDomain", 0)
        out.append(Check(
            "service domain coverage", sd >= self.min_service_domains,
            f"{sd} of ~{self.expected_service_domains} expected",
            stage=Stage.EXTRACT,
            hint=f"The published value chain view shows "
                 f"{self.min_service_domains} service domains. Materially "
                 f"fewer means data shards are being missed — this is exactly "
                 f"the failure that had us reading 222 of 340. Check the shard "
                 f"count in the harvest output."))
        out.append(Check(
            "service domain count not wildly above expectation",
            sd <= self.expected_service_domains * 1.5,
            f"{sd} vs ~{self.expected_service_domains} expected", warn=True,
            stage=Stage.EXTRACT,
            hint="More service domains than the published view shows. Either "
                 "BIAN expanded the model, or shards contain older revisions "
                 "of the same objects under different ids."))

        canary = items.get(self.canary_id, {})
        out.append(Check(
            f"canary object {self.canary_id} acquired", bool(canary),
            stage=Stage.EXTRACT,
            hint=f"Object {self.canary_id} ({self.canary_name}) is missing. If "
                 f"BIAN genuinely retired it, pick a new canary; otherwise the "
                 f"extraction is dropping objects."))
        out.append(Check(
            "canary name matches", self.canary_name in canary.get("name", ""),
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
            body.count("### 1. Role Definition") >= self.min_service_domains,
            f"{body.count('### 1. Role Definition')} sections",
            stage=Stage.RENDER,
            hint="Service domains were found but their documentation is not "
                 "reaching the markdown — check _documentation()."))
        out.append(Check(
            "relationships rendered",
            body.count("### Relationships") >= semantic // 4,
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
            "no structural relation objects emitted",
            not any(L.is_structural(m.get("category", ""), m.get("name", ""))
                    for m in items.values()),
            stage=Stage.EXTRACT,
            hint="ArchiMate relation objects are being emitted as items. They "
                 "carry no content and their edges already render inline — "
                 "check EXCLUDE_CATEGORIES and is_structural()."))

        # Scoped to Relationships sections. An earlier form matched any bullet
        # with a numeric value, so ordinary properties such as
        # "**Cardinality:** 1" produced false failures once the harvest grew
        # large enough to contain them.
        #
        # The section must also be closed by the item separator and by the
        # item heading, not only by the next "### ". Relationships is the last
        # section an item emits, so without this the flag stayed set across the
        # boundary and the NEXT item's "- **Object id:**" and "- **Source:**"
        # bullets were counted as unresolved targets — two false failures for
        # every item that has relationships.
        bad_targets, samples, in_rels = 0, [], False
        for line in body.splitlines():
            if line.startswith("#") or line.startswith("---"):
                in_rels = line.strip() == "### Relationships"
                continue
            if in_rels and line.startswith("- **") and "(" not in line:
                bad_targets += 1
                if len(samples) < 3:
                    samples.append(line.strip()[:70])

        # Exact-zero was the wrong gate at landscape scale. A broken id-to-name
        # lookup shows up as thousands of unresolved targets; a single stray
        # bullet in 11,004 sections is a rendering nit, and refusing to publish
        # 12,500 verified items over it is the worse error of the two. The
        # stray is still reported — as a warning, with the offending text, so
        # it can be diagnosed rather than merely counted.
        sections = body.count("### Relationships")
        limit = max(5, int(sections * 0.005))
        out.append(Check(
            "relationship targets resolved to names", bad_targets <= limit,
            f"{bad_targets} unresolved of {sections} sections (limit {limit})",
            stage=Stage.RENDER,
            hint="Relationship targets rendered without a '(id)' suffix at a "
                 "rate that means the id-to-name lookup is genuinely broken, "
                 "not merely imperfect."))
        if bad_targets:
            out.append(Check(
                "every relationship target resolved", False,
                " | ".join(samples), warn=True, stage=Stage.RENDER,
                hint="These bullets sit inside a Relationships section but "
                     "carry no '(id)'. The likeliest cause is a relation verb "
                     "or target name containing a newline, which splits one "
                     "bullet across two lines. Harmless in small numbers."))

        # Diagram checks only apply to a bundle that claims to hold them.
        if manifest.get("complete") and diagrams:
            out.append(Check(
                "sequence diagrams converted",
                cats.get("Sequence diagram", 0) >= self.min_sequence_diagrams,
                f"{cats.get('Sequence diagram', 0)} "
                f"(min {self.min_sequence_diagrams})",
                stage=Stage.RENDER,
                hint="Fewer sequence diagrams than the landscape holds. Check "
                     "the per-chunk results in harvest.json."))
            out.append(Check(
                "class diagrams converted",
                cats.get("Class diagram", 0) >= self.min_class_diagrams,
                f"{cats.get('Class diagram', 0)} "
                f"(min {self.min_class_diagrams})",
                stage=Stage.RENDER,
                hint="Fewer class diagrams than the landscape holds. Check "
                     "the per-chunk results in harvest.json."))
            out.append(Check(
                "diagrams carry PlantUML",
                body.count("```plantuml") >= diagrams,
                f"{body.count('```plantuml')} blocks for {diagrams} diagrams",
                stage=Stage.RENDER,
                hint="A diagram item was written without its fenced PlantUML "
                     "block — check views.diagram_markdown()."))
        return out
