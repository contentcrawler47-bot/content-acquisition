#!/usr/bin/env python3
"""
Template for a new content source. Copy this directory to sources/<your-id>/,
rename, and implement harvest(). Nothing central needs editing — the CLI
discovers sources by scanning this folder.

Directories starting with _ are ignored, so this template never runs.
"""

from __future__ import annotations

from pathlib import Path

from core.diagnostics import ProbeSpec
from core.render import clean_html, write_bundles
from core.source import Check, HarvestResult, Stage
from core.source import Source as BaseSource


class Source(BaseSource):
    id = "template"
    name = "Example Source"
    description = "One line describing what this acquires"

    # Namespace secrets with the source id so sources never collide.
    # Leave empty if the source needs no credentials.
    required_secrets: list[str] = []          # e.g. ["TEMPLATE_API_TOKEN"]

    schedule = "0 4 * * 1"                    # documentation only

    def probes(self) -> list[ProbeSpec]:
        """Declare what must be reachable BEFORE any parsing is attempted.

        This is the single most useful thing to get right when onboarding a
        source: it separates "cannot reach it" from "cannot parse it", so a
        failure names its own cause.

        Assert the payload's shape, not just an HTTP 200 — many sites return
        200 with an error page or a login redirect.
        """
        return [
            ProbeSpec(
                label="items endpoint",
                url="https://example.invalid/api/items",
                expect_prefix="{",              # or expect_contains="\"items\""
                min_bytes=100),
        ]

    def harvest(self, outdir: Path) -> HarvestResult:
        # 1. Acquire. Read credentials from os.environ, never from a file.
        # 2. Build one dict per item; body is markdown ending with "---".
        items = [{
            "id": "1",
            "name": "Example item",
            "category": "Example",
            "body": "## Example item\n\n"
                    + clean_html("<p>Some <b>content</b>.</p>")
                    + "\n\n---\n",
        }]

        # 3. Hand off. write_bundles produces index.md, manifest.json and
        #    grouped markdown in the shape every source shares.
        written = write_bundles(outdir, self.id, self.name, items)

        return HarvestResult(
            source_id=self.id,
            item_count=len(items),
            categories=written["categories"],
            files_written=written["files_written"],
        )

    def checks(self, outdir: Path) -> list[Check]:
        """Validate what was written.

        Tag each check with the stage it belongs to, and give a `hint` saying
        what to look at — that is what turns a red run into a fix.

        Always include a canary: a known item that must be present with a known
        name, so upstream restructuring fails loudly rather than quietly
        thinning the output.
        """
        import json
        manifest = json.loads((outdir / "manifest.json").read_text())
        items = manifest.get("items", {})
        return [
            Check("item count", len(items) >= 1, f"{len(items)} items",
                  stage=Stage.EXTRACT,
                  hint="Fewer items than expected — compare against the live "
                       "endpoint before lowering this threshold."),
            Check("canary item present", "1" in items,
                  stage=Stage.EXTRACT,
                  hint="A known item is missing. If it was legitimately "
                       "retired upstream, choose a new canary."),
        ]
