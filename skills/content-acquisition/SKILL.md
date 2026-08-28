---
name: content-acquisition
description: Operate the content-acquisition project — a GitHub Actions and Google Drive pipeline that harvests reference content from external sources, renders it to markdown and PlantUML, and publishes it privately for Claude to read. Use this skill whenever the user mentions content-acquisition, changesets, the harvest or publish workflows, adding a content source, repo digests or MANIFEST.sha256, or asks to change anything in that repo. Also use it when they mention BIAN together with harvesting, publishing or automation, or when a GitHub Actions log from this project is shared. Critically, changes to this repo must be delivered as verified changeset zips, never as loose files to paste — so consult this skill before proposing any modification to it.
---

<!-- skill: content-acquisition v2 | repo: changeset 025 -->

# content-acquisition

A pipeline: external source → GitHub Actions → Google Drive → Claude.

The user works from a locked-down machine, so **everything runs in the browser
or in CI**. Never suggest installing software or running git locally unless
they say they are on a personal machine.

## This file holds lessons, not measurements

A lesson stays true when the numbers change. A measurement — a count, a
threshold, a digest, an expiry date, a claim about what exists — has a date
attached even when it does not look like it, and belongs on Drive.

So this skill does not restate the repo's structure, commands or workflows.
Those are in the repo, which you can read directly. **Read the artefact rather
than recalling it**, and rather than trusting a description of it.

**A constant the repo defines is a measurement too.** A category set, a
threshold, a schema, a path — restating one here creates a copy that drifts,
and the drift is invisible because both look authoritative. Name the file and
the symbol instead. Any tool needing it **imports it**: a tool that
re-declares a constant the pipeline owns will eventually disagree with the
pipeline and be believed. This skill shipped a six-category-short copy of the
landscape allowlist, and the next tool written from it reported a wrong total
that nothing else contradicted.

## Getting the current state

**The repo.** Public, and readable from the sandbox:

```
curl -sL -o repo.tar.gz \
  https://codeload.github.com/contentcrawler47-bot/content-acquisition/tar.gz/refs/heads/main
tar xzf repo.tar.gz && cd content-acquisition-main
python3 tools/repo_manifest.py --verify
```

That last command proves the copy you are reading is the shipped state, and
prints the digest. Do it before documenting or changing anything structural. A
digest proves the repo matches its manifest; it does not tell you what is in
it. Read `README.md` and `SETUP.md` from the tarball for structure, commands,
schedule and secrets. The GitHub REST API rate-limits unauthenticated requests
immediately; the tarball does not.

If the sandbox is unavailable, ask the user to run **Verify repo contents**.

**The project state.** `content/_project-context/` on Drive. Read
`_START-HERE.md`, then the latest snapshot's `SESSION-HANDOVER.md`. Other files
as the task requires: `PROJECT-DESIGN.md` (rationale), `DECISION-LOG.md`
(rejected alternatives), `REFERENCE-DATA.md` (verified counts, thresholds,
digest history), `METHOD.md` (working method for a new source),
`BIAN-EXTRACTION-REFERENCE.md`.

Never assert a digest, an expiry, or what is outstanding from memory. **This
applies to the environment too:** a handover claimed a `references/` folder did
not exist, and it was repeated for two sessions without anyone running `ls`. It
did exist. Check the filesystem rather than inheriting a claim about it, and
never write "confirmed" for a check you did not run.

Filenames repeat across snapshots, so a title search can return a superseded
copy; check which folder a file came from.

For anything touching bian.org, use the `bian-extraction` skill.

## Changing the repo

**Never hand over loose files to paste.** That produced a doubled paste, a
truncated file and a wrong filename. Deliver a **changeset zip**:

```
changeset-NNN.zip
├── CHANGESET.json      operations + base_digest + skill_impact
├── MANIFEST.sha256     the exact state the repo must be in AFTERWARDS
└── files/              new and updated content, mirroring repo paths
```

Then the user uploads it to `changesets/` and runs **Apply changeset**, first
with `dry_run: true`, then `false`.

`references/changesets.md` has the format, what the applier enforces, and the
mistakes already made. **Read it before building one** rather than reading
`tools/apply_changeset.py` — earlier sessions did the latter because this file
was believed missing.

Rules that have each cost a rework:

- **Dry-run it yourself first.** Pull the repo, apply the zip with
  `tools/apply_changeset.py`, confirm it produces the digest you are about to
  quote. A changeset that has only been reasoned about is a guess.
- **Generate `MANIFEST.sha256` with `tools/repo_manifest.py --write` on the
  resulting tree**, never by composing it, and regenerate it last.
- **Generate it on the right base.** A manifest built against a stale local
  copy declares the wrong target; the base-digest check catches it, but only
  after you have handed it over.
- **Scan for codepoints above 127** before zipping.
- **One changeset at a time.** Two outstanding ones create an ordering
  dependency and the second is rejected.
- **Quote both digests** — the base it expects and the state it produces.
- **Warn when it touches `.github/workflows/`**, which needs `CHANGESET_TOKEN`.
- **Never put the expected digest inside a tracked file** — it changes the
  digest it describes.

## Skills live in this repo

Both skills are repo files under `skills/`, so a skill change is an ordinary
changeset: verified before commit, covered by `MANIFEST.sha256`, and repairable
with `--reconcile` exactly like code.

**`skill_impact` is required in `CHANGESET.json`.** An empty list is a valid
answer meaning "asked, and nothing"; an absent key is refused. It is
cross-checked against the operations in both directions — a skill named must be
touched, and a file touched under `skills/` must be named. A skill therefore
cannot be changed silently, and a declared change cannot fail to materialise.

**The authoritative version of a skill is its content in the repo at the
verified digest.** The marker at the top of each `SKILL.md` names the version;
the manifest holds its hash. Compare with:

```
sha256sum /mnt/skills/user/content-acquisition/SKILL.md
grep 'skills/content-acquisition/SKILL.md' MANIFEST.sha256
```

The marker is a claim the file makes about itself; **the hash is the decision**.
A matching marker with a mismatched hash means someone edited the installed
copy in place — resolve it, never leave it.

When the installed copy is behind, say so in one line and **read the repo
version before acting in any area the newer one changed**. Do not silently
follow the older instructions.

To release: bump the version in the marker *first*, make the change, ship the
changeset, then tell the user to run **Package skills** and install the
artifacts. Claude cannot write to the skills store and cannot see it change, so
that step is always the user's.

**Never edit an installed skill in place**, and never put project context
documents in this repo — it is public and they are deliberately not.

## Lessons that keep earning their place

- **A check that counts artefacts does not validate them.** "1,181 PlantUML
  blocks for 1,181 diagrams" passed while none of them rendered. Where a real
  validator exists, run the validator.
- **A cache holding rendered output must carry a renderer version.** Otherwise
  a rendering fix never reaches published content: the request returns 304, the
  old conversion is restored, and the fix survives its own correction.
- **Thresholds taken from a sample mis-fire at scale.** Prefer a gate that
  catches a broken mechanism over one demanding perfection, and where a metric
  can be preserved rather than counted, preserve it.
- **Never widen a gate to make a red run green.** If a measurement legitimately
  drops, move the threshold as a decision and record why.
- **Normalise before matching, then union every match.** A category name can
  exist in two spellings, and a substring test finds only one. This produced a
  0% join that looked like catastrophic upstream drift.
- **Anything keyed by name collapses duplicates**, silently. Report objects and
  distinct names separately.
- **Measure the whole set, not a sample.** Ask what the sample did not contain.
- **Do not build the fixture from the same belief as the code.** A synthetic
  fixture written alongside the code tests the assumption twice instead of once
  against reality. Build fixtures from observed data.
- **A second copy is a second thing to keep right**, and the one nobody reads
  is the one that rots. Learned three times here: duplicated changeset history
  that went three changesets stale, a duplicated extraction reference that
  drifted unread across three snapshots, and a duplicated category allowlist
  that silently changed a published count. Before writing something down,
  check whether it already exists somewhere that is checked.

## When citing a constraint, cite where it was decided

"Python standard library only" was quoted for weeks as a design rule with a
rationale behind it. It was a line in the README describing how the code
happened to stand, and `DECISION-LOG.md` had no such entry.

A description of the current state and a constraint read identically once
written down. Before refusing something on the grounds of a rule, find the
entry. If there is none, it is a description and may be revised.

The constraint that *is* real is **nothing installed on the user's machine**.
CI was never the constraint — it has downloaded pinned third-party binaries
since early on, and installs one pinned Python dependency for the API source.

## Diagnosing a failed run

Extraction and publishing are deliberately separated, so the failing workflow
halves the search before you read anything: a red **Validate** is a source
problem and uses no Drive credentials; a red **Check publishing target** is a
Drive problem and touches no source; a red **Join** means neither source is
broken, so check the matching before the sources.

Within a chunked landscape run, read which *job* failed — `plan` made no page
requests at all, `chunk N` localises it to one slice, `assemble` means nothing
was published. Within extraction, validation is staged and stops at the first
failure.

**Ask for the failing job or stage specifically**, not the whole log, and ask
for it as an **uploaded file** — pasting inline has repeatedly arrived empty in
this project.

`references/troubleshooting.md` lists the failures already encountered with
their causes.

## Ending a session

Write a **new complete snapshot** to `content/_project-context/`; Drive cannot
overwrite, so nothing is edited in place.

- `copy_file` everything unchanged — a server-side copy costing about fifty
  tokens whatever the size — and write only what changed.
- **Amend `SESSION-HANDOVER.md`; do not regenerate it.** Merging preserves
  detail a freshly composed summary drops.
- **Open it with what a reader of the previous snapshot may now believe
  wrongly.** Stale beliefs do not announce themselves.
- **Write `README.md` last**, listing every file with its byte size. It is the
  completeness marker: a folder without one is a failed write, to be ignored
  and deleted rather than read as current.
- **Record a decision only when alternatives existed** and a later reader could
  reasonably propose the opposite. Corrections and measurements are not
  decisions; they belong beside the claim they overturn.
- **Before deleting a document as redundant, name what only it contains.**
  "Overlapping" is not "duplicated".

**Writing costs what is emitted, not where it goes.** The sandbox cannot reach
Drive — egress is allowlisted to GitHub, npm, PyPI and Ubuntu — so there is no
path from disk to Drive that avoids re-emitting every byte. Write once,
straight to where it will live; `copy_file` rather than rewrite; emit the
generator rather than the output where a script can produce the file.
Compression does not help: base64 hands back most of what zip saves, and
emitting it means reading it into context first.

Reading is not free either. A whole snapshot is ~90 KB. Read
`SESSION-HANDOVER.md` first and fetch the rest only as the task requires.

## Adding a source

Copy `sources/_template/` and write `probes()` **before** `harvest()` — it is
what separates "cannot reach it" from "cannot parse it" for the source's whole
life. Full procedure in `docs/ADDING_A_SOURCE.md` in the repo, and
`PROJECT-DESIGN.md` on Drive.

- A source never imports another source, never publishes, and never writes
  outside its own `outdir`.
- **One source id per version** — `bian-v14`, `bian-v13` — each a thin subclass
  pinning a URL and its verified counts, with shared logic in a library outside
  `sources/`. Version isolation then comes free from publish scoping.
- **A question about two sources belongs in a tool**, not in either source.
- **Stagger cron times, and confirm reindex is still last.** Reindex was once
  scheduled before a newly added source, which would have written a week-stale
  date into the index every week.

Logs are public, so print counts, hashes and classifications — never harvested
text.

## Reference files

- `references/changesets.md` — building a changeset, and the errors already made
- `references/troubleshooting.md` — known failure modes and their causes
