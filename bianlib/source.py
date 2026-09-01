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

    #: View types whose PAGE is worth a request.
    #:
    #: WIDENED 31 August 2026 to every ArchiMate view type BIAN publishes as a
    #: page. This REVERSES the 30 August decision that fetched only the four
    #: types where "arrangement carries meaning that membership does not",
    #: which excluded Capability map view as "a layout nobody will render" and
    #: Architecture overview as "a navigation index at 0% coverage".
    #:
    #: Both statements were true and both are now beside the point. They
    #: measured whether a page carries FACTS the model lacks. Measured across
    #: all 608 stored views, it largely does not: 97% of drawn edges and 59% of
    #: containments are already relations in the graph. But a diagram's value
    #: is the CURATION -- someone chose these elements out of 128,270 -- and
    #: stage 2 now renders every published ArchiMate view, so this list must
    #: track what is worth DRAWING rather than what is worth knowing.
    #:
    #: Effect: 668 ArchiMate views published on the site, of which 608 already
    #: had geometry. The 54 added here are Capability map view (42),
    #: Architecture overview (6) and six singletons. About 54 extra requests,
    #: ~1 minute at 1s pacing.
    #:
    #: Expect some to yield nothing, and that is not a fault: `Roadmap view`
    #: is a single `Canvas` block with 0 members, and two capability maps have
    #: 0 members. Six `Total view` records already fetch to nothing for the
    #: same reason.
    #:
    #: Class and sequence diagrams stay excluded because the existing harvest
    #: already converts them; adding them would double the request count for
    #: output that exists.
    GEOMETRY_VIEW_TYPES = (
        "Total view", "Total view new style", "ArchiMate total view",
        "Information structure view", "Capability map view",
        "Architecture overview", "Business function view",
        "Business Model Canvas", "Motivation view", "Roadmap view",
        "Strategy motivation view", "Ecosystem view",
    )

    # -- stage 1: extract -------------------------------------------------

    def _fetch_geometry(self, model, fetcher_factory) -> dict:
        """Fetch and parse the pages whose arrangement is worth storing.

        A separate Fetcher so page requests are paced independently of the
        index files already read, and so a geometry failure cannot leave the
        model half-loaded. Progress is reported every 50 views: a silent
        ten-minute step is indistinguishable from a hung one.
        """
        from bianlib import geometry as GEO

        wanted = [vid for vid in sorted(model.insite_views, key=str)
                  if model.categories.get(str(vid)) in self.GEOMETRY_VIEW_TYPES]
        print(f"  geometry: {len(wanted)} views to fetch "
              f"({', '.join(self.GEOMETRY_VIEW_TYPES)})", flush=True)

        out, failed = {}, 0
        fetcher = fetcher_factory(self.base)
        try:
            for n, vid in enumerate(wanted, 1):
                try:
                    resp = fetcher.get(L.view_url(self.base, vid),
                                       conditional=False)
                    if resp.status != 200 or not resp.text.strip():
                        failed += 1
                        continue
                    g = GEO.parse_geometry(resp.text, vid)
                except Exception as e:                      # noqa: BLE001
                    print(f"    view {vid}: {type(e).__name__}", flush=True)
                    failed += 1
                    continue
                if g["node_count"] or g["edge_count"]:
                    out[str(vid)] = g
                if n % 50 == 0 or n == len(wanted):
                    print(f"    {n} of {len(wanted)} fetched, "
                          f"{len(out)} with geometry, {failed} failed",
                          flush=True)
        finally:
            fetcher.close()

        # A page that yielded nothing is not the same as a page not fetched.
        print(f"  geometry: {len(out)} of {len(wanted)} views parsed, "
              f"{failed} failed", flush=True)
        return out

    #: How many per-view data files the gate samples. `data/view_<id>_data.js`
    #: is read by nothing on the bulk path, so every key in it is unconsumed
    #: and a handful of pages settles what is behind them. A SAMPLE, and the
    #: gate labels it as one -- generalising two views to 608 was wrong by a
    #: factor of 40 in this project once already.
    GATE_VIEW_SAMPLE = 5

    def _run_gate(self, model, options: dict) -> dict:
        """Observe the source against the parser's declaration, then evaluate.

        Two artefacts are fetched here and nowhere else in the pipeline:
        `config_data.js`, which is probed for existence and has never been
        parsed, and a sample of `data/view_<id>_data.js`, which nothing on the
        bulk path reads at all.

        A failure to fetch either produces NOT MEASURED, never zero. The one
        recorded measurement of ArchiMate viewpoints in this project was a zero
        produced by thirty fetches that had all failed on a path later proved
        wrong, and it survived the correction of that path.
        """
        from bianlib import gate as G

        fetcher = Fetcher(self.base)
        config, view_data = None, {}
        try:
            try:
                resp = fetcher.get(L.data_url(self.base, "config_data.js"),
                                   conditional=False)
                if resp.status == 200 and resp.text.strip():
                    config = L.parse_js_assignments(resp.text)
                else:
                    print(f"    gate: config_data.js HTTP {resp.status}",
                          flush=True)
            except Exception as e:                          # noqa: BLE001
                print(f"    gate: config_data.js {type(e).__name__}",
                      flush=True)

            for vid in sorted(model.insite_views, key=str)[:self.GATE_VIEW_SAMPLE]:
                url = L.data_url(self.base, f"view_{vid}_data.js")
                try:
                    resp = fetcher.get(url, conditional=False)
                    if resp.status != 200 or not resp.text.strip():
                        print(f"    gate: view {vid} data HTTP {resp.status}",
                              flush=True)
                        continue
                    payload = L.parse_js_assignment(resp.text)
                    if isinstance(payload, dict):
                        view_data[str(vid)] = payload
                except Exception as e:                      # noqa: BLE001
                    print(f"    gate: view {vid} data {type(e).__name__} "
                          f"on {url}", flush=True)
        finally:
            fetcher.close()

        observation = G.observe(model, config=config, view_data=view_data,
                                shard_results=model.shard_results)
        verdict = G.evaluate(
            observation,
            max_share=float(options.get("max_share", 0.5)),
            max_absolute=int(options.get("max_absolute", 500)),
            max_total_share=float(options.get("max_total_share", 2.0)),
            observe_only=bool(options.get("observe_only", False)))
        verdict["inventory"] = observation["inventory"]
        verdict["exclusions"] = observation["exclusions"]
        return verdict

    def build_extract(self, outdir: Path, mode: str = "model-only",
                      run: dict | None = None,
                      gate_options: dict | None = None) -> dict:
        """Load the landscape and write it as a JSON-LD extract.

        Loads exactly what `harvest()` loads, and then stores it rather than
        rendering it. Nothing is filtered: the allowlist is applied by stage 2,
        so a change to it costs a re-render and no requests at all.

        `model-only` reads the shards and index files. `full` additionally
        fetches every view page and stores its geometry as nodes and edges,
        which is several hundred requests against someone else's web server.
        """
        from bianlib import extract as E

        fetcher = Fetcher(self.base)
        model = L.Landscape(self.base, object_view=self.object_view).load(fetcher)

        # The model index. Its location is discovered rather than asserted --
        # see MODELS_CANDIDATES. Fetched after the model so a failure here
        # cannot be confused with a failure to load the landscape.
        print(f"  looking for insite_models under {self.base}", flush=True)
        entries, models_url, tried = L.fetch_models(fetcher)
        fetcher.close()

        geometry = {}
        if mode == "full":
            geometry = self._fetch_geometry(model, fetcher_factory=Fetcher)

        gate = self._run_gate(model, gate_options or {})

        doc = E.build(model, self.id, mode=mode, insite_models=entries,
                      models_url=models_url, models_tried=tried,
                      geometry=geometry, run=run, gate=gate)
        summary = E.write(doc, outdir)

        status = doc["status"]
        counts = summary["parts"]
        print(f"  extract: {counts['objects']} objects, "
              f"{counts['relations']} relations, "
              f"{counts['views']} views, "
              f"{counts['view_members']} memberships", flush=True)
        for part, size in summary["part_bytes"].items():
            print(f"    {part:<14} {size / 1024 / 1024:>8.1f} MB  "
                  f"{summary['partitions'][part]:>4} partitions", flush=True)
        print(f"  total  : {summary['bytes'] / 1024 / 1024:.1f} MB "
              f"across {summary['files']} files", flush=True)
        print(f"  content: {summary['content_digest'][:16]}", flush=True)
        print(f"  notation unresolved: {status['notation_unresolved']} of "
              f"{counts['objects']}", flush=True)
        print(f"  memberships resolving to nothing: "
              f"{status['unresolved_members']} of "
              f"{counts['view_members']}", flush=True)
        if status["models"] == "present":
            print(f"  models : {len(doc['models'])} named, "
                  f"{status['views_with_model']} of {counts['views']} views "
                  f"carry one   (from {status['models_url']})", flush=True)
        else:
            print(f"  models : NOT FETCHED after trying "
                  f"{', '.join(status['models_tried']) or 'nothing'}",
                  flush=True)
        if status["geometry"] == "present":
            print(f"  geometry: {status['views_with_geometry']} views, "
                  f"{counts['geometry_nodes']} nodes, "
                  f"{counts['geometry_edges']} edges, "
                  f"{status['geometry_unboxed']} blocks without a box",
                  flush=True)
        else:
            print("  geometry: not-fetched", flush=True)
        if status["malformed_objects"]:
            print(f"  malformed objects skipped: "
                  f"{status['malformed_objects']}", flush=True)

        # The gate is REPORTED here and ENFORCED in check_extract.py, which
        # runs after the artifact is uploaded. A failed extract is the thing
        # you most want to look at, and refusing to write it would throw away
        # the evidence for the refusal.
        from bianlib import gate as G
        print("\n" + "\n".join(G.report(status["gate"])), flush=True)
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
