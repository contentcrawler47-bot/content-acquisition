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
  bian/                    BIAN Service Landscape
.github/workflows/
  source-bian.yml          one workflow per source
  reindex.yml              rebuilds the top-level Drive index
```

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

Python standard library only. No dependencies to install.

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

## Adding a source

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md).

## Setup

See [SETUP.md](SETUP.md).
