# content-acquisition

Acquires reference content from external sources, renders it to clean markdown,
and publishes each source to its own folder in Google Drive — where Claude can
read it across sessions and devices.

```
run.py                     entry point
requirements.txt           one pinned, hash-checked dependency (see below)
core/
  source.py                the contract every source implements
  render.py                shared markdown/manifest generation
  checks.py                generic validation + report runner
  diagnostics.py           connectivity probing with error classification
  publish.py               uniform, per-source-scoped Drive sync
  cli.py                   source discovery and commands
sources/
  _template/               copy this to add a source
  bian-v14/                BIAN Service Landscape v14 — pinned URL, thresholds
  bian-apis-v14/           BIAN Semantic APIs v14 — the release archive
bianlib/                   BIAN extraction, shared across landscape versions
  fetch.py                 paced, backing-off, cache-aware HTTP
  landscape.py             the data model: shards, relations, views
  views.py                 view-page SVG geometry -> PlantUML
  plan.py                  view classification, chunking, verification
  pipeline.py              plan -> chunk -> assemble
  source.py                the BianSource base class
tools/
  landscape.py             the chunked full-landscape harvest
  join_report.py           joins two finished bundles by item name
  check_plantuml.py        hands every diagram to PlantUML to verify it renders
  repo_manifest.py         manifest generation and verification
  apply_changeset.py       applies a revision zip, verifying before commit
  landscape_census.py      landscape counts, with denominators stated
  view_to_plantuml.py      single-view conversion, for investigation
  publish_sample.py        the original small end-to-end proof
  probe_*.py               one-off investigations, kept as evidence
docs/ADDING_A_SOURCE.md
changesets/                upload target for revision zips
.github/workflows/         seventeen; six scheduled — see The scheduled week
```

## Sources

| Source id | What | Items | Drive folder |
|---|---|---|---|
| `bian-v14` | BIAN Service Landscape v14 | 12,521 | `content/bian-v14/` |
| `bian-apis-v14` | BIAN Semantic APIs v14 | 258 | `content/bian-apis-v14/` |

The two are measurably related: `tools/join_report.py` resolves API service
domains to landscape service domains and reports the rate. It is **99.6%** —
257 of 258 — which is the number that says the two bundles refer to the same
things.

## Harvesting a whole landscape

A BIAN landscape is two very different jobs. The semantic model is 47 files and
about two minutes. The diagrams are ~1,231 view pages, and asking someone
else's web server for that in one burst is not reasonable.

```
python3 tools/landscape.py plan     bian-v14 --chunks 10
python3 tools/landscape.py chunk    bian-v14 --index 1
python3 tools/landscape.py assemble bian-v14
```

Every chunk's diagrams are handed to PlantUML itself to confirm they render —
PlantUML draws an error image rather than refusing, so a syntax fault is
invisible to anything that only inspects the markdown, and one such fault once
broke all 1,181 published diagrams while every check passed.

The model is read once and passed downstream, so the shards are never re-read
per chunk. Each chunk verifies its own accounting before the next begins, and
nothing is published until the landscape verifies as a whole. Requests are
paced, gzipped, sent over one connection, and conditional — an unchanged view
answers 304 with no body, so a weekly refresh transfers almost nothing.

## Multiple versions of one source

`bian-v14` and `bian-v13` are separate sources sharing `bianlib/`. Each has its
own output directory, its own workflow and its own Drive folder, and
`rclone sync` is scoped per source — so neither version can see, overwrite or
delete the other. Adding v13 means copying `sources/bian-v14/` and changing the
pinned URL and the verified counts.

## Commands

```
python3 run.py list                      configured sources and their secrets
python3 run.py validate <source>         CAN WE EXTRACT?  (never touches Drive)
python3 run.py check-publish             CAN WE PUBLISH?  (never touches a source)
python3 run.py harvest <source>          acquire -> out/<source>/
python3 run.py extract <source>          STAGE 1: model -> out/_extract/<source>/
python3 run.py publish <source>          sync to gdrive:content/<source>/
python3 run.py run <source> --publish    validate then publish
python3 run.py reindex                   rebuild content/index.md

python3 tools/join_report.py out/bian-v14 out/bian-apis-v14 --min-rate 99
python3 tools/check_extract.py out/_extract/bian-v14
python3 tools/repo_manifest.py --verify
```

## Extraction in two stages

`harvest` acquires and renders in one pass, which means a renderer change or a
change to the category allowlist both cost another full pass over the source.
`extract` is the first half of splitting those apart: it stores the source's
model as JSON-LD in `out/_extract/<source>/` and applies no selection and no
rendering at all.

Storing the model unfiltered is the point. Selection belongs to the render
stage, so adding a category to `INCLUDE_CATEGORIES` becomes a re-render against
a stored extract rather than 47 more shard requests.

`tools/check_extract.py` validates the result in two ways that catch different
faults. `schema/bian-extract.schema.json` checks structure and is the contract
between the two stages. The referential integrity checks are Python, because
they need to report a count against a denominator — "1,900 of 2,285 views
resolve, 385 are not objects in the model" — where a conformance boolean would
not say enough to act on.

The extract is an index plus range partitions of each bulk collection
(`objects`, `relations`, `views`, `view_members`) — 380 files at v14. The index
publishes the boundaries, so finding the partition holding an object is a
lookup against the document rather than a rule a reader has to reimplement, and
`bianlib.extract.locate()` is the one implementation of it. Boundaries are cut
at equal rank, which assumes nothing about how the ids are distributed, and a
boundary never falls inside a key — one view has up to 964 members, and
splitting a key would leave items outside the range their own partition
declares. Each part carries its own count and content
digest, the index carries both again, and `check_extract.py` compares them —
declaring a number twice is only useful if something checks it. Byte digests
live in `EXTRACT.sha256` beside the files, because "did the file change" and
"did the content change" are different questions.

The extract also carries the **model index**: `insite_models` groups views into
named models, and is the only published statement of a view's *purpose* —
`insiteViews` gives only a name, and no ArchiMate viewpoints are declared
anywhere. Its location is not in the documented layout, so `fetch_models()`
tries candidates in order and records which one answered in the extract's
`status`. A run that cannot find it **fails**, naming every path it tried;
`--allow-missing-models` downgrades that to a warning.

Schema validation needs `jsonschema`, pinned in `requirements-extract.txt` and
installed only by the extract workflow. When it is absent the structural check
reports SKIP and names itself; `--require-schema` makes that a failure.

`--mode model-only` reads the shards and index files and no view pages.
`--mode full` additionally fetches the pages whose *arrangement* carries
meaning and stores their geometry as nodes and edges.

Not every view earns a page request. `GEOMETRY_VIEW_TYPES` in
`bianlib/source.py` lists the four types that do. A Total view earns it because
its containment is the value chain — view 54486 holds 341 service-domain boxes
inside 51 nested groupings. A Capability map view does not: 12 nodes, no edges,
every member already kept, so the page adds a layout nobody will render. Class
and sequence diagrams are excluded because the existing harvest already
converts them.

Two things about geometry are easy to get wrong. **The SVG concept is a shape,
not a type** — 339 service domains on view 54486 are drawn as
`StrategyCapability`, so the type comes from the model through the node's
`object`. And **ArchiMate nodes carry no `<rect>`**; they are rounded-rectangle
path outlines, so `bianlib/geometry.py` walks the path command list and falls
back to `<rect>` only where there is one.

A third trap, found by the first full run: **an ArchiMate junction is a
connector node, not a relationship.** A relation block is named
`<Source><Target><RelationType>`, so recognising one by suffix also catches the
bare element `OrJunction`. `is_edge()` therefore requires the concept to be
strictly longer than the suffix, and names the junction elements outright.

## The scheduled week

Everything runs on Monday, and **the order is load-bearing**.

| UTC | Workflow | Secrets |
|---|---|---|
| 02:00 | Validate — BIAN v14 | none |
| 03:00 | Landscape — BIAN v14 (full, chunked) | `GDRIVE_*` |
| 04:00 | Validate — BIAN APIs v14 | none |
| 05:00 | Source — BIAN APIs v14 | `GDRIVE_*` |
| 06:00 | Join — BIAN APIs to landscape | none |
| **07:00** | **Reindex published sources — last** | `GDRIVE_*` |

Each source is validated before it is harvested, the join runs once both
bundles exist, and reindex runs last so `content/index.md` records the dates
of the runs that just happened. Reindex was once at 04:30 and a source was
added after it, which would have written a week-stale date every week.
**Adding or rescheduling a source means checking reindex is still last.**

The remaining workflows are on-demand: **Verify repo contents**, **Apply
changeset**, **Check publishing target (Google Drive)**, **Publish sample to
Drive**, **Sample — Savings Account diagrams**, and four one-off
investigations (**Investigate — BIAN coverage gap**, three **Probe —**
workflows) kept as evidence for the findings they produced.

## Extraction and publishing are separate

The two halves fail for entirely different reasons, so they have separate
commands, separate workflows and separate exit codes.

| Question | Command | Workflow | Touches |
|---|---|---|---|
| Can we get content out of the source? | `validate <source>` | **Validate — \<source\>** | the source only |
| Can we write to Drive? | `check-publish` | **Check publishing target** | Drive only |

A red `Validate` is always a source problem. A red `Check publishing target` is
always a Drive problem. The combined per-source workflow runs extraction first
and labels which half failed.

## Staged validation

`validate` runs five stages in order and stops at the first failure, so the
report names one cause rather than a cascade of consequences.

| Stage | Answers | A failure means |
|---|---|---|
| Connectivity | Can we reach the endpoints? | Upstream down, moved, blocking this IP, or missing credentials |
| Payload | Did they return the expected shape? | New upstream version, or an error page served with HTTP 200 |
| Parse | Does it parse? | The upstream format changed |
| Extract | Did we get the expected items? | Upstream restructured, or our selection logic is wrong |
| Render | Is the written output clean? | A bug in this repo, not upstream |

Each failure prints what was observed, what it probably means, and what to
check. Sources declare their endpoints in `probes()`, which is what makes
onboarding a new source verifiable rather than hopeful.

Python standard library only, with one exception: `sources/bian-apis-v14`
reads YAML, which the standard library cannot parse. `requirements.txt` pins
that parser to a version and its hashes, installed with `--require-hashes` so
no unpinned transitive dependency can appear. Only that source's two workflows
install it; every other workflow runs on the standard library alone.

## How sources stay independent

| Concern | Mechanism |
|---|---|
| Code | `sources/<id>/` — sources never import each other |
| Credentials | Per-source `required_secrets`, namespaced by source id |
| Scheduling | One workflow per source, own cron and concurrency group |
| Output | `out/<id>/`, emptied per run, exclusive to that source |
| Publishing | `rclone sync` scoped to `content/<id>/`, never the root |
| Failure | A broken source is reported and skipped; others still run |

`rclone sync` deletes destination files absent from the source, which is why it
is always scoped to one subfolder. `core/publish.py` additionally refuses to
sync a missing, empty, or manifest-less output directory — so a failed harvest
cannot wipe the published copy.

It also refuses a bundle whose manifest says `"complete": false`. A source that
harvests in stages can produce a real-looking partial bundle; syncing that over
a full one would silently delete the difference.

A question *about two sources* belongs in neither of them.
`tools/join_report.py` works from the two finished `manifest.json` files, with
no network access and no code from either side.

## Adding a source

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md).

## Setup

See [SETUP.md](SETUP.md).
