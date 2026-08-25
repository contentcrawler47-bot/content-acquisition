# content-acquisition

Acquires reference content from external sources, renders it to clean markdown,
and publishes each source to its own folder in Google Drive — where Claude can
read it across sessions and devices.

```
run.py                     entry point
core/
  source.py                the contract every source implements
  render.py                shared markdown/manifest generation
  checks.py                generic validation + report runner
  publish.py               uniform, per-source-scoped Drive sync
  cli.py                   source discovery and commands
sources/
  _template/               copy this to add a source
  bian-v14/                BIAN Service Landscape v14 — pinned URL, thresholds
bianlib/                   BIAN extraction, shared across landscape versions
  fetch.py                 paced, backing-off, cache-aware HTTP
  landscape.py             the data model: shards, relations, views
  views.py                 view-page SVG geometry -> PlantUML
  plan.py                  view classification, chunking, verification
  pipeline.py              plan -> chunk -> assemble
tools/
  landscape.py             the chunked full-landscape harvest
  check_plantuml.py        hands every diagram to PlantUML to verify it renders
.github/workflows/
  landscape-bian-v14.yml   full landscape, in verified chunks
  validate-bian.yml        can we still extract? (cheap, weekly)
  reindex.yml              rebuilds the top-level Drive index
```

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
python3 run.py publish <source>          sync to gdrive:content/<source>/
python3 run.py run <source> --publish    validate then publish
python3 run.py reindex                   rebuild content/index.md
```

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

## Adding a source

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md).

## Setup

See [SETUP.md](SETUP.md).
