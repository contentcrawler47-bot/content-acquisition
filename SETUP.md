# Setup

## What this repo is

A harvester for reference content from multiple external sources. Each source
is independent; publishing to Google Drive is uniform across all of them.

Drive ends up laid out like this:

```
content/
  index.md          all sources, item counts, last acquired
  bian/
    index.md        categories, counts, which file holds what
    servicedomain_01.md ...
    manifest.json
  <next source>/
    ...
```

Point Claude at `content/index.md` and it can navigate to whatever you ask
about without reading everything.

---

## Migrating from the single-source layout

The flat BIAN-only repo is superseded. **Delete these:**

| Path | Replaced by |
|---|---|
| `harvest.py` / `harvest_bian.py` | `sources/bian/source.py` + `core/` |
| `validate.py` / `validate_bian.py` | `core/checks.py` + `Source.checks()` |
| `crawl.py`, `object_ids.txt` | (obsolete since the Playwright version) |
| `.github/workflows/harvest.yml`, `harvest-bian.yml` | `.github/workflows/source-bian.yml` |
| `.github/workflows/test.yml`, `validate-bian.yml`, `probe.yml` | folded into `source-bian.yml` (`publish: false`) |

**Delete these secrets** if still present: `BIAN_NAME`, `BIAN_EMAIL` — BIAN's
data files are public.

**Keep** the three `GDRIVE_*` secrets. They are unchanged and now shared by
every source.

**One Drive change:** content moves from `BIAN/` to `content/bian/`. Let the
first run create it, then delete the old `BIAN/` folder by hand.

---

## Files in the repo

| Path | Purpose |
|---|---|
| `run.py` | Entry point |
| `core/source.py` | The contract every source implements |
| `core/render.py` | Shared markdown and manifest generation |
| `core/checks.py` | Generic validation and the report runner |
| `core/publish.py` | Per-source-scoped Drive sync |
| `core/cli.py` | Source discovery and commands |
| `core/__init__.py`, `sources/__init__.py`, `sources/bian/__init__.py` | Empty package markers — required |
| `sources/bian/source.py` | BIAN Service Landscape |
| `sources/_template/source.py` | Copy to add a source |
| `core/diagnostics.py` | Connectivity probing with error classification |
| `.github/workflows/validate-bian.yml` | **Validate — BIAN**: extraction only, no Drive secrets |
| `.github/workflows/source-bian.yml` | **Source — BIAN**: scheduled harvest and publish |
| `.github/workflows/check-publish.yml` | **Check publishing target**: Drive only, no source |
| `.github/workflows/reindex.yml` | Rebuilds `content/index.md` |
| `.gitignore`, `README.md`, `SETUP.md`, `docs/ADDING_A_SOURCE.md` | |

Everything must be on the **default branch** (`main`) — workflows are invisible
to the Actions tab until they are there. The empty `__init__.py` files matter:
without them the `core` imports fail.

The repo can be **public**: only code lives here. Acquired content goes to a
private Drive folder and is never committed.

---

## Secrets

### Shared — the Drive publisher identity

| Secret | Used by |
|---|---|
| `GDRIVE_CLIENT_ID` | every source's publish step, and reindex |
| `GDRIVE_CLIENT_SECRET` | " |
| `GDRIVE_TOKEN` | " |

One Drive identity publishes everything, but each source syncs only to its own
subfolder, so they cannot overwrite one another.

### Per source

Declared in each source's `required_secrets` and injected only into that
source's workflow. Namespace them with the source id — `ACME_API_TOKEN`, not
`API_TOKEN` — so sources never collide.

**BIAN needs none.** Its data files are served unauthenticated.

Run `python3 run.py list` to see what each source requires and what is missing
from the current environment.

---

## Setup steps

### 1. Add the files, then check discovery

```
python3 run.py list
```

Expect BIAN listed with `secrets: none`.

### 2. Validate extraction locally — no credentials needed

```
python3 run.py validate bian
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

### 3. Create a Google OAuth client

Service accounts have zero Drive storage quota and cannot own files, so this
uses an OAuth token for your own account. Signed in as the dedicated Gmail, in
Google Cloud Console:

1. Create a project.
2. Enable the **Google Drive API**.
3. OAuth consent screen → External → add that Gmail as a test user.
4. Credentials → Create credentials → OAuth client ID → **Desktop app**.
5. Note the client id and secret.

### 4. Authorise rclone

The only step needing a machine where you can install software.

```
rclone config
# n) new remote
# name> gdrive
# storage> drive
# client_id> <from step 3>
# client_secret> <from step 3>
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

Then extract the three values and add them as repo secrets:

```
grep -A5 '\[gdrive\]' ~/.config/rclone/rclone.conf
```

`client_id` → `GDRIVE_CLIENT_ID`, `client_secret` → `GDRIVE_CLIENT_SECRET`, and
the entire `token = {...}` JSON → `GDRIVE_TOKEN`.

The workflows rebuild rclone's config from environment variables at runtime, so
no credential file is written to any runner.

To revoke: that account's Google security settings → third-party access →
remove, then re-run `rclone config` and update `GDRIVE_TOKEN`.

### 5. Check the two halves separately in Actions

Actions → **Check publishing target (Google Drive)** → Run workflow. This tests
credentials and Drive reachability and touches no source, so a failure here is
unambiguously a Drive problem.

Actions → **Validate — BIAN Service Landscape** → Run workflow. This tests
extraction and references no Drive secrets, so a failure here is unambiguously
a source problem. It also runs weekly at 02:00 UTC as an early warning, an hour
before the harvest.

Running them separately is the point: you never have to guess which half broke.

### 6. First publish

Actions → **Source — BIAN Service Landscape** → Run workflow → **publish:
true**. It validates extraction first and only then contacts Drive, labelling
which half failed if either does. Confirm `content/bian/` appears in Drive.

Then run **Reindex published sources** once to create `content/index.md`.

After this, each source runs on its own schedule — BIAN at 03:00 UTC Mondays
(15:00 NZST / 16:00 NZDT), reindex half an hour later.

### 7. Give Claude access

Connect the Google Drive connector signed in as the dedicated Gmail. Ask Claude
to read `content/index.md` first, then the relevant source's `index.md`.

---

## Operating notes

- **Version pinning.** Upstream versions live in the source that uses them —
  for BIAN, `BASE` and `VIEW` at the top of `sources/bian/source.py`. Update
  them, then run validation with publish off.
- **Canaries.** Each source asserts a known item is present with a known name,
  so upstream restructuring fails loudly rather than silently thinning the
  output.
- **Diagnosing a red run.** Read the failing *stage* first. Anything up to
  Render is the source; Drive problems never appear there. If extraction passed
  and the run still failed, it is publishing — run **Check publishing target**.
  If validation passes locally but fails in CI, suspect IP blocking or a secret
  missing from the workflow rather than the source itself.
- **Publishing is guarded.** `core/publish.py` refuses to sync a missing, empty
  or manifest-less directory, because `rclone sync` deletes destination files
  absent from the source. A failed harvest cannot wipe the published copy.
- **Stagger cron times** when adding sources — independent workflows, one
  shared Drive account and its rate limits.
- **Schedule expiry.** GitHub disables scheduled workflows after 60 days of
  repo inactivity; the keep-alive step commits a timestamp under `.runs/`.
- **Nothing is published from CI.** No artifacts are uploaded, no acquired text
  is logged, and `out/` is gitignored.
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
