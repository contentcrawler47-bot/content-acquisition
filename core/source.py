"""
The contract every content source implements.

A source knows how to acquire content from one place and turn it into markdown.
It knows nothing about Google Drive, GitHub Actions, or any other source — the
core handles publishing uniformly, so adding a source cannot affect existing
ones.

Validation is staged so a failure localises itself. The stages run in order and
stop at the first that fails, because a parse failure caused by an unreachable
host should not also report fifty missing items.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .diagnostics import ProbeSpec


class Stage(str, Enum):
    """Where a check sits. Everything up to RENDER is extraction; PUBLISH is a
    separate concern handled by core.publish and never mixed in."""
    CONNECT = "Connectivity"     # can we reach the source at all?
    PAYLOAD = "Payload"          # did it return what we expected to receive?
    PARSE = "Parse"              # does the payload parse into a structure?
    EXTRACT = "Extract"          # did we get the items we expected?
    RENDER = "Render"            # is the written output complete and clean?
    PUBLISH = "Publish"          # Drive — checked separately, never here

    @property
    def order(self) -> int:
        return list(Stage).index(self)


@dataclass
class Check:
    """One validation result.

    `hint` is what makes a failure actionable — say what to look at, not just
    what was wrong.
    """
    name: str
    ok: bool
    detail: str = ""
    warn: bool = False
    stage: "Stage" = None
    hint: str = ""

    def __post_init__(self):
        if self.stage is None:
            self.stage = Stage.EXTRACT


@dataclass
class HarvestResult:
    """What a harvest produced. Counts and paths only — never content, because
    this is summarised into logs that may be world-readable."""
    source_id: str
    item_count: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    files_written: int = 0
    notes: list[str] = field(default_factory=list)


class Source:
    """Subclass this in sources/<id>/source.py and name the class `Source`.

    Required: id, name, harvest().
    Optional: description, required_secrets, schedule, checks().
    """

    #: Short slug. Becomes the CLI argument, the output directory, and the
    #: Drive subfolder. Lowercase, no spaces.
    id: str = ""

    #: Human-readable name shown in logs and indexes.
    name: str = ""

    description: str = ""

    #: Environment variable names this source needs, e.g. ["ACME_API_KEY"].
    #: Namespace them with the source id to avoid collisions between sources.
    #: Empty means the source needs no credentials.
    required_secrets: list[str] = []

    #: Cron for the generated workflow. Documentation only — the schedule
    #: actually lives in .github/workflows/source-<id>.yml.
    schedule: str = "0 3 * * 1"

    # -- lifecycle -------------------------------------------------------

    def probes(self) -> list[ProbeSpec]:
        """Endpoints that must be reachable, with the shape expected back.

        Run before any harvest. This is what distinguishes "the source is
        down / moved / now needs a login" from "our parser is wrong", and it
        is how a newly onboarded source proves it can connect at all.
        """
        return []

    def harvest(self, outdir: Path) -> HarvestResult:
        """Acquire content and write markdown plus manifest.json into outdir.

        outdir is exclusive to this source and is emptied before each run, so
        a source can never disturb another's output.
        """
        raise NotImplementedError

    def checks(self, outdir: Path) -> list[Check]:
        """Source-specific validation of what was written.

        Tag each Check with the stage it belongs to. Include a canary: a known
        item that must be present with a known name, so upstream restructuring
        fails loudly instead of quietly thinning the output.
        """
        return []

    def build_extract(self, outdir: Path, mode: str = "model-only",
                      run: dict | None = None) -> dict:
        """Optional. Write this source's data as a structured extract.

        Stage 1 of the two-stage design: acquire and store the model, without
        applying any selection or producing any markdown. Sources that have
        not adopted it keep working exactly as before — this is an optional
        capability rather than a change to the harvest contract, so adding it
        cannot affect a source that does not implement it.

        `run` is CI provenance recorded into the extract, supplied by the
        caller so that nothing below this line reads the environment.

        Returns a summary dict of counts and digests. Never returns content.
        """
        raise NotImplementedError(
            f"{self.id} does not implement build_extract()")


    # -- helpers ---------------------------------------------------------

    def missing_secrets(self) -> list[str]:
        return [s for s in self.required_secrets if not os.environ.get(s)]

    def __str__(self) -> str:
        return f"{self.name or self.id} ({self.id})"
