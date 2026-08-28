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
| `tools/repo_manifest.py` | Manifest generation and verification |
| `tools/apply_changeset.py` | Applies a revision zip, verifying before commit |
| `tools/landscape_census.py` | Landscape counts, each with its denominator |
| `tools/view_to_plantuml.py`, `tools/publish_sample.py`, `tools/probe_*.py` | Diagnostics and one-off investigations |
| `core/__init__.py`, `sources/__init__.py`, `sources/*/__init__.py`, `bianlib/__init__.py` | Empty package markers — required |
| `.github/workflows/` | Fifteen workflows; see **The scheduled week** in the README |
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

| Secret | Required | Used by |
|---|---|---|
| `GDRIVE_TOKEN` | yes | every source's publish step, and reindex |
| `GDRIVE_CLIENT_ID` | no | only if using your own Cloud OAuth client |
| `GDRIVE_CLIENT_SECRET` | no | as above |

One Drive identity publishes everything, but each source syncs only to its own
subfolder, so they cannot overwrite one another.

**Omitting the two client values is the recommended path.** It selects rclone's
built-in OAuth client, which is a published app — so refresh tokens do not
expire. Your own client in *Testing* status expires them after seven days,
which breaks a weekly schedule about a week in, long after anyone is still
thinking about OAuth. The trade-off is a shared quota, immaterial for a weekly
sync of a few dozen files.

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

The entire `token = {...}` JSON → `GDRIVE_TOKEN`. If you did supply your own
client id and secret, add those as `GDRIVE_CLIENT_ID` and
`GDRIVE_CLIENT_SECRET` too; otherwise leave both unset.

The workflows rebuild rclone's config from environment variables at runtime, so
no credential file is written to any runner.

To revoke: that account's Google security settings → third-party access →
remove, then re-run `rclone config` and update `GDRIVE_TOKEN`.

#### If you would rather use your own OAuth client

Service accounts have zero Drive storage quota and cannot own files, so this
route still uses an OAuth token for a real account. Signed in as the dedicated
Gmail, in Google Cloud Console: create a project, enable the **Google Drive
API**, set the OAuth consent screen to External and add that Gmail as a test
user, then create an OAuth client ID of type **Desktop app**. Supply the id and
secret at the `rclone config` prompts above.

Publish the app if you take this route, or the seven-day expiry applies.

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
- **Nothing acquired is exposed by CI.** No artifacts are uploaded, no acquired
  text is logged, and `out/` is gitignored. Logs are public because the repo is
  public — when adding diagnostics, print counts, hashes and classifications,
  never harvested text.
- **Hardening.** `permissions: contents: write` only, for the timestamp commit;
  reindex is `contents: read`. rclone is pinned and checksum-verified rather
  than piped into a root shell. `actions/checkout` is the only third-party
  action — pin it with
  `gh api repos/actions/checkout/git/ref/tags/v5 --jq .object.sha`. Never add a
  `pull_request_target` trigger: unlike `pull_request` it runs with full access
  to secrets.
- **Licensing.** Sources are acquired without authentication where possible,
  but that is not a licence to redistribute. This keeps a private working copy
  and publishes nothing. Check each source's terms.
