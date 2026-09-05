---
name: content-acquisition
description: Operate the content-acquisition project — a GitHub Actions and Google Drive pipeline that harvests reference content from external sources, renders it to markdown and PlantUML, and publishes it privately for Claude to read. Use this skill whenever the user mentions content-acquisition, changesets, the harvest or publish workflows, adding a content source, repo digests or MANIFEST.sha256, or asks to change anything in that repo. Also use it when they mention BIAN together with harvesting, publishing or automation, or when a GitHub Actions log from this project is shared. Critically, changes to this repo must be delivered as verified changeset zips, never as loose files to paste — so consult this skill before proposing any modification to it.
---

<!-- skill: content-acquisition v10 | repo: changeset 073b -->

# content-acquisition

A pipeline: external source → GitHub Actions → Google Drive → Claude.

The user works from a locked-down machine, so **everything runs in the browser
or in CI**. Never suggest installing software or running git locally unless
they say they are on a personal machine.

## What lives where

This skill holds **how the machinery works and what must never happen**. It
changes rarely. Everything else has one other home:

- **Current state and working lessons live on Drive**, in the snapshot:
  `SESSION-HANDOVER.md` for what is true now, `LESSONS.md` for what this
  project has learned, `REFERENCE-DATA.md` for measurements. A measurement — a
  count, a threshold, a digest, an expiry, a claim about what exists — has a
  date attached even when it does not look like it, and never belongs here.
- **Procedures live in `references/`**, read when the task calls for one.
- **Structure, commands and constants live in the repo.** Read the artefact
  rather than recalling it, and rather than trusting a description of it. A
  tool needing a constant the pipeline defines **imports it**; a copy drifts
  invisibly because both look authoritative.

**One home per item. This file points; it never restates.** A second copy is a
second thing to keep right, and the one nobody reads is the one that rots.

## Starting a session

1. **Pull the repo and verify it**, cache-busted — a stale tarball verifies
   clean against its own manifest:
   ```
   curl -sL -o repo.tar.gz \
     "https://codeload.github.com/contentcrawler47-bot/content-acquisition/tar.gz/refs/heads/main?nocache=$(date +%s)"
   tar xzf repo.tar.gz && cd content-acquisition-main
   python3 tools/repo_manifest.py --verify
   ```
   The digest proves the copy matches its manifest, not what is in it; confirm
   a marker that moves with the latest change. If the sandbox is unavailable,
   ask the user to run **Verify repo contents**.
2. **Compare the installed skill with the repo's.** `ls /mnt/skills` finds the
   mount; `sha256sum` it against the manifest's line for
   `skills/content-acquisition/SKILL.md`. The marker is a claim the file makes
   about itself; **the hash is the decision**. Behind means say so in one line
   and read the repo version before acting in any area it changed. A matching
   marker with a mismatched hash means an installed copy was edited in place —
   resolve it, never leave it.
3. **Read the project context through the mirror**, not the connector:
   `references/context-mirror.md`. The key is one small file on Drive; the
   snapshot then sits decrypted in the sandbox, readable by section and
   greppable across every document. Fall back to the connector only for what
   the mirror does not yet have.
4. **Read `SESSION-HANDOVER.md`, then `LESSONS.md`.** The handover opens with
   what a reader of the previous snapshot now believes wrongly; that section is
   the highest-value text in the project.
5. **Read the procedure for the task** from `references/`, and other
   documents **by section, when the task reaches them** — a document read at
   orientation is paid for on every turn that follows.

Never assert a digest, an expiry, or what is outstanding from memory, **and do
not infer state from your own last action** — handing over a changeset is not
it having been applied. Check the filesystem rather than inheriting a claim
about it, and never write "confirmed" for a check you did not run.

**Two access paths, and the question chooses.** The connector reads one Drive
file into context, whole; it sees no Actions runs and no logs. The sandbox
reads the repo, PyPI and the mirror, and runs code over whole populations with
only the answers entering context; it cannot reach Drive and cannot use the
Actions API without rate-limiting. For anything touching bian.org, use the
`bian-extraction` skill.

## Rules that are never relaxed

- **No credential enters a session.** Not a token, not a password, not in a
  link. The single exception is the mirror key, read from Drive by the
  connector, which decrypts one snapshot and grants nothing else
  (`references/context-mirror.md`).
- **A change to the repo is a changeset zip** — never loose files to paste,
  one outstanding at a time, base digest from a fresh **Verify repo
  contents**, dry-run in the sandbox before handover.
  `references/changing-the-repo.md` before building one.
- **A capability with no workflow behind it has not been shipped.** Adding one
  means adding the entry point that reaches it, or saying plainly that it is
  unreachable until a later changeset.
- **Logs and artifacts are public.** Print counts, hashes and classifications,
  never harvested text; never upload payload bytes or extracted text as an
  artifact. Inter-job transport is the Actions cache with an exact key.
  `tools/check_workflows.py` enforces this and names the exceptions.
- **Drive cannot overwrite.** A session ends with a new complete snapshot;
  `README.md` written last is the completeness marker, and a folder without one
  is a failed write. `references/ending-a-session.md`.
- **Never edit an installed skill in place**, and never put project context in
  this repo — it is public and the context is deliberately not. Ciphertext of
  it on the `context` branch is the one designed exception.
- **Skills are repo files.** A skill change is an ordinary changeset with
  `skill_impact` declared; then the user runs **Package skills** and installs.
  Claude cannot write to the skills store and cannot see it change.

## Reference files

- `references/context-mirror.md` — reading the snapshot from the sandbox; the key, the fallback, rotation
- `references/changing-the-repo.md` — changeset mechanics, skills as repo files, the rules that each cost a rework
- `references/changesets.md` — the zip format, what the applier enforces, mistakes already made
- `references/diagnosing-a-run.md` — which workflow's colour means what; where the retained data is
- `references/ending-a-session.md` — writing the snapshot
- `references/adding-a-source.md` — the template, probes first, one id per version
- `references/troubleshooting.md` — failures already encountered, with causes
