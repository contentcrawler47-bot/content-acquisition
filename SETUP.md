# Setup

## What this repo is

A harvester for reference content from multiple external sources. Each source
is independent; publishing to Google Drive is uniform across all of them.

Drive ends up laid out like this:

```
content/
  index.md          all sources, item counts, last acquired
  bian-v14/
    index.md        categories, counts, which file holds what
    servicedomain_01.md ...
    harvest.json
    manifest.json
  bian-apis-v14/
    ...
  <next source>/
    ...
```

Point Claude at `content/index.md` and it can navigate to whatever you ask
about without reading everything.

---

## Files in the repo

54 files at the time of writing. `python3 tools/repo_manifest.py --verify`
lists them all and confirms the repo matches its shipped state; this table is
the map, not the inventory.

| Path | Purpose |
|---|---|
| `run.py` | Entry point |
| `requirements.txt` | The single pinned, hash-checked dependency — see below |
| `core/source.py` | The contract every source implements |
| `core/render.py` | Shared markdown and manifest generation |
| `core/checks.py` | Generic validation and the report runner |
| `core/diagnostics.py` | Connectivity probing with error classification |
| `core/publish.py` | Per-source-scoped Drive sync |
| `core/cli.py` | Source discovery and commands |
| `sources/bian-v14/source.py` | BIAN Service Landscape v14 — pinned URL and thresholds only |
| `sources/bian-apis-v14/source.py` | BIAN Semantic APIs v14 — the release archive |
| `sources/_template/source.py` | Copy to add a source |
| `bianlib/` | BIAN extraction shared across landscape versions — `fetch`, `landscape`, `views`, `plan`, `pipeline`, `source` |
| `tools/landscape.py` | The chunked full-landscape harvest |
| `tools/join_report.py` | Joins two finished bundles by item name |
| `tools/check_plantuml.py` | Hands every diagram to PlantUML |
| `tools/check_workflows.py` | Workflow conformance — pins, permissions, triggers, artifact paths |
| `tools/repo_manifest.py` | Manifest generation and verification |
| `tools/apply_changeset.py` | Applies a revision zip, verifying before commit |
| `tools/landscape_census.py` | Landscape counts, each with its denominator |
| `tools/view_to_plantuml.py`, `tools/publish_sample.py` | Diagnostics and samples |
| `core/__init__.py`, `sources/__init__.py`, `sources/*/__init__.py`, `bianlib/__init__.py` | Empty package markers — required |
| `.github/workflows/` | The workflows; see **The scheduled week** in the README |
| `MANIFEST.sha256` | The shipped state, checked by **Verify repo contents** |
| `changesets/` | Upload target for revision zips |
| `.gitignore`, `README.md`, `SETUP.md`, `docs/ADDING_A_SOURCE.md` | |

Everything must be on the **default branch** (`main`) — workflows are invisible
to the Actions tab until they are there. The empty `__init__.py` files matter:
without them the `core` imports fail.

The repo can be **public**: only code lives here. Acquired content goes to a
private Drive folder and is never committed.

### Dependencies

The repo is Python standard library only, with one exception:
`sources/bian-apis-v14` reads OpenAPI and AsyncAPI documents, and there is no
YAML parser in the standard library. `requirements.txt` pins one to a version
and its hashes, installed with `--require-hashes` so no unpinned transitive
dependency can appear. **Only that source's two workflows install it**; every
other workflow runs on the standard library alone.

CI additionally downloads two pinned, checksum-verified binaries per run —
`rclone` for the Drive sync and `plantuml.jar` for diagram validation. Neither
is committed, and nothing needs installing on your own machine.

---

## Secrets

### Shared — the Drive publisher identity

| Secret | Required | Scope | Used by |
|---|---|---|---|
| `GDRIVE_TOKEN` | yes | `drive.file` | every source's publish step, reindex, and the mirror's one write |
| `GDRIVE_MIRROR_TOKEN` | for the context mirror | `drive.readonly` | **Mirror project context** only (step 3a) |

One Drive identity publishes everything, but each source syncs only to its own
subfolder, so they cannot overwrite one another.

Both tokens come from **rclone's built-in OAuth client**, which is a published
app, so refresh tokens do not expire. There is no `GDRIVE_CLIENT_ID` or
`GDRIVE_CLIENT_SECRET`: a project-owned OAuth client was attempted early on and
abandoned over free-tier configuration problems, and would in any case have
expired its tokens after seven days while in *Testing* status. Six workflows
still carry `RCLONE_CONFIG_GDRIVE_CLIENT_*` lines referencing those two secrets;
the secrets have never existed, the lines expand to empty strings and select the
built-in client, and they are removed as each workflow is next touched. The
trade-off of the built-in client is a shared quota, immaterial at this volume.

Revoking rclone's access in the account's Google security settings revokes
**both** tokens at once, since they share the app.

### Changesets

| Secret | Required | Notes |
|---|---|---|
| `CHANGESET_TOKEN` | for workflow edits | PAT with Contents + Workflows write |

`GITHUB_TOKEN` cannot modify anything under `.github/workflows/`, so a
changeset touching a workflow needs this. **The current token expires
21 November 2026.**

### Per source

Declared in each source's `required_secrets` and injected only into that
source's workflow. Namespace them with the source id — `ACME_API_TOKEN`, not
`API_TOKEN` — so sources never collide.

**Both BIAN sources need none.** Their files are served unauthenticated.

Run `python3 run.py list` to see what each source requires and what is missing
from the current environment.

---

## Setup steps

### 1. Add the files, then check discovery

```
python3 run.py list
```

Expect `bian-v14` and `bian-apis-v14` listed with `secrets: none`.

### 2. Validate extraction locally — no credentials needed

```
python3 run.py validate bian-v14
```

This probes bian.org, extracts, and checks the result. It never touches Drive,
so it proves the source half works before you spend time on OAuth.

Five stages run in order and stop at the first failure, so a red run names one
cause: **Connectivity** (unreachable, blocked, or missing credentials),
**Payload** (reachable but returning the wrong thing — usually a new upstream
version), **Parse** (format changed), **Extract** (wrong items selected), or
**Render** (a bug in this repo). Each failure prints what was observed, what it
means, and what to check.

If you only want a local copy, stop here.

### 3. Authorise rclone

The only step needing a machine where you can install software.

```
rclone config
# n) new remote
# name> gdrive
# storage> drive
# client_id> <blank — uses rclone's built-in client>
# client_secret> <blank>
# scope> 3          (drive.file — only files rclone itself creates)
# service_account_file> blank
# advanced config> n
# use web browser to authenticate> y   -> sign in as the dedicated Gmail
# configure as team drive> n
```

`drive.file` is deliberate: a leaked token can reach only files this repo
created. **Because of that scope, let rclone create the folders** — ones made
by hand in the Drive web UI are invisible to it:

```
rclone mkdir gdrive:content
rclone lsd gdrive:
```

Then extract the token and add it as a repo secret:

```
grep -A5 '\[gdrive\]' ~/.config/rclone/rclone.conf
```

The entire `token = {...}` JSON → `GDRIVE_TOKEN`, as a repository secret in
the `drive` Environment.

The workflows rebuild rclone's config from environment variables at runtime, so
no credential file is written to any runner.

To revoke: that account's Google security settings → third-party access →
remove, then re-run `rclone config` and update `GDRIVE_TOKEN`.

A service account is not an alternative: it needs a Google Cloud project, has
zero Drive storage quota and cannot own files.

### 3a. Authorise the read-only mirror remote

Needed only for **Mirror project context**, which copies the project-context
snapshot out of Drive so a Claude session can read it from the sandbox
(`skills/content-acquisition/references/context-mirror.md`). The `drive.file`
token cannot see files the Drive connector wrote, so this is a second token at
scope `drive.readonly` — read everything, write nothing. Its marginal exposure
over `GDRIVE_TOKEN`, which can already read, overwrite and delete everything
rclone created, is read access to hand-made files: in a dedicated account, the
design documents the mirror publishes as ciphertext anyway.

Same machine and procedure as step 3, different name and scope:

```
rclone config
# n) new remote
# name> mirror
# storage> drive
# client_id> <blank>
# client_secret> <blank>
# scope> 2          (drive.readonly)
# service_account_file> blank
# advanced config> n
# use web browser to authenticate> y   -> sign in as the dedicated Gmail
# configure as team drive> n
```

Before extracting anything, prove the scope is what it should be:

```
rclone lsd mirror:content/_project-context                     # must list the snapshot folders
rclone touch mirror:content/_project-context/SHOULD-FAIL.txt   # must be refused
```

An empty listing means scope 3 was chosen; a created file means the scope is
not read-only. Delete the remote and redo either way. Then the `token = {...}`
JSON from `rclone config show mirror` → `GDRIVE_MIRROR_TOKEN`, as an
**Environment** secret under `drive` — `tools/check_workflows.py` refuses a
`GDRIVE_*` reference anywhere else — and delete the remote from that machine.

First run: Actions → **Mirror project context** → `check_only: true`. The log
lists the folder or names which of the above went wrong. A step-by-step version
for Windows and PowerShell is in the project context on Drive under
`playbooks/`.

### 4. Check the two halves separately in Actions

Actions → **Check publishing target (Google Drive)** → Run workflow. This tests
credentials and Drive reachability and touches no source, so a failure here is
unambiguously a Drive problem.

Actions → **Validate — BIAN v14** → Run workflow. This tests extraction and
references no Drive secrets, so a failure here is unambiguously a source
problem. It also runs weekly at 02:00 UTC as an early warning, an hour before
the harvest.

Running them separately is the point: you never have to guess which half broke.

### 5. First publish

Actions → **Landscape — BIAN v14 (full, chunked)** → Run workflow. It plans,
harvests in verified chunks, and publishes only once the whole landscape
verifies. Confirm `content/bian-v14/` appears in Drive.

Then Actions → **Source — BIAN APIs v14** for the second source, and confirm
`content/bian-apis-v14/`.

Then run **Reindex published sources** once to create `content/index.md`.

After this everything runs on the Monday schedule in the README — and reindex
must stay last on it.

### 6. Give Claude access

Connect the Google Drive connector signed in as the dedicated Gmail. Ask Claude
to read `content/index.md` first, then the relevant source's `index.md`.

---

## Operating notes

- **Version pinning.** Upstream versions live in the source that uses them —
  for the landscape, `base` and `object_view` in `sources/bian-v14/source.py`;
  for the APIs, `release` and `release_label`. Update them, then run validation
  with publish off.
- **Structural objects are excluded.** BIAN's model is ArchiMate, which
  represents relationships as first-class objects (Flow relation, Triggering
  relation, and so on). These carry no documentation and their edges already
  render inline under each real object's Relationships section, so they are
  filtered out — about a third of the raw object count. See
  `EXCLUDE_CATEGORIES` in `bianlib/landscape.py`.
- **Canaries.** Each source asserts a known item is present with a known name,
  so upstream restructuring fails loudly rather than silently thinning the
  output.
- **Diagnosing a red run.** Read the failing *stage* first. Anything up to
  Render is the source; Drive problems never appear there. If extraction passed
  and the run still failed, it is publishing — run **Check publishing target**.
  In a chunked landscape run, read which *job* failed: `plan` made no page
  requests at all, `chunk N` localises it to one slice, `assemble` means
  nothing was published. If validation passes locally but fails in CI, suspect
  IP blocking or a secret missing from the workflow rather than the source.
- **A red join is not a broken source.** **Join — BIAN APIs to landscape**
  compares two finished bundles. If it fails, either a name genuinely drifted
  upstream or the matching is wrong — check the tool before the sources, and
  never raise the threshold to make the run go green.
- **Publishing is guarded.** `core/publish.py` refuses to sync a missing, empty
  or manifest-less directory, because `rclone sync` deletes destination files
  absent from the source. A failed harvest cannot wipe the published copy. It
  also refuses a bundle whose manifest says `"complete": false`.
- **Stagger cron times** when adding sources — independent workflows, one
  shared Drive account and its rate limits — and confirm reindex is still last.
- **Schedule expiry.** GitHub disables scheduled workflows after 60 days of
  repo inactivity; the keep-alive step commits a timestamp under `.runs/`.
- **Acquired text is never logged**, and `out/` is gitignored. Logs are public
  because the repo is public — when adding diagnostics, print counts, hashes
  and classifications, never harvested text.
- **Artifacts are as public as logs**, and two of them still carry payload
  bytes: the run directory and the extract uploaded by **Extract**. Anyone
  with a GitHub account can download them. **Acquire** stopped at 073b: its
  run travels to the archive job in the Actions cache, which has no public
  read path. The two that remain go at 073c, when Extract consumes an
  archived run by id and Render restores the extract from the cache. Until
  then, do not add to them — `check_workflows.py` refuses any new Class-B
  artifact and lists the two exceptions by name.
- **Hardening.** Workflow-level `permissions:` everywhere, minimal;
  `contents: write` only in **Apply changeset** and in the landscape
  keep-alive commit. Every third-party action is pinned to a full commit SHA
  with a `# vX.Y.Z` comment, and the runner image is pinned too; a tag can be
  force-pushed, a SHA cannot. rclone and actionlint are pinned and
  checksum-verified rather than piped into a root shell. Drive secrets live in
  the `drive` GitHub Environment, so a job that talks to a source cannot see
  them. Every job starts with harden-runner in audit mode, which records
  outbound connections. Never add a `pull_request` or `pull_request_target`
  trigger: the second runs with full access to secrets, and both would let a
  fork's code reach the Actions cache.
- **These are checked, not merely intended.** `tools/check_workflows.py` runs
  in **Verify repo contents** and again on the tree a changeset would produce,
  so a change that unpins an action or adds a `pull_request` trigger is
  refused before it is written. Where it permits an exception it names the
  changeset that removes it, and a stale exception is itself a failure. Read
  that file for the current rules rather than this paragraph.
- **Licensing.** Sources are acquired without authentication where possible,
  but that is not a licence to redistribute. This keeps a private working copy
  and publishes nothing. Check each source's terms.
