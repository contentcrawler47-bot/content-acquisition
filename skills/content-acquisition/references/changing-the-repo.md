# Changing the repo

Procedure reference for the content-acquisition skill. Read this before
building a changeset. The rules that must never be broken are in `SKILL.md`;
this file is how the machinery works and the mistakes it has already produced.

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

**The workflow reads one specific path by default.** Its `zip` input has a
default filename, and a zip uploaded under its own name with the input left
alone fails at the first step having read nothing — a confusing failure,
because the changeset is fine. Either upload it under the expected name or set
the input, and say which when handing it over.

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
sha256sum /mnt/skills/*/content-acquisition/SKILL.md   # wherever it is mounted
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

