# Building a changeset

Everything here is checked against `tools/apply_changeset.py` and
`tools/repo_manifest.py`. Where behaviour is surprising, the surprise is in
the code, not in this document.

## The shape

```
changeset-NNN.zip
├── CHANGESET.json      operations + base_digest
├── MANIFEST.sha256     the state the repo must be in AFTERWARDS
└── files/              payload, mirroring repo paths exactly
```

`MANIFEST.sha256` sits at the **top level of the zip, not under `files/`**.
It is not a tracked file — `repo_manifest.py` excludes it by name — so it is
never an operation and never appears in `files/`.

## Order of work

The manifest describes the end state, so it must be generated **after** every
other file is final. The sequence that works:

1. Get the current digest. **Ask the user to run _Verify repo contents_.**
   Never assert it from memory or from a document; that is the single fact
   most likely to have moved since the last session.
2. Make every edit.
3. Regenerate: `python3 tools/repo_manifest.py --write`.
4. Assemble the zip.
5. Test by applying it to a pristine copy (see below).
6. Hand it over with **both digests and the filename to upload it under** —
   *Apply changeset* reads a default path, and a zip uploaded under its own name
   with the input left alone fails having read nothing.

Regenerating the manifest first and then editing produces a changeset that
fails verification after writing — recoverable, but it wastes a run.

## What the applier enforces

**`base_digest` is checked before anything is written.** Applying out of
order is rejected up front rather than surfacing later as missing files.

**Verification runs before any commit.** If the resulting tree does not match
`MANIFEST.sha256`, nothing is committed and the run fails. A bad changeset
cannot land.

**Workflow conformance is checked before anything is written**, on the
workflow set the changeset would produce rather than the one on disk — so a
changeset that unpins an action, adds a `pull_request` trigger, drops a job
timeout or uploads harvested bytes as an artifact is refused at `--dry-run`.
`tools/check_workflows.py` names the clause and the workflow. It also fails on
a *stale exception*: a rule it was told to permit for something that no longer
exists. When a changeset updates the checker itself, the shipped copy does the
checking, so a rule added in a changeset applies to that changeset.

**Valid ops are `add`, `update`, `delete`, `rename`** — nothing else. Anything
unrecognised is a hard problem.

**Every operation is validated before a single file is touched.** Problems are
collected and reported together, and then nothing changes.

**Every file under `files/` must be claimed by an operation.** A payload file
with no matching operation is an error: `files/x is in the zip but no
operation refers to it`. This is the check that catches a file left behind
from a previous draft.

**`rename` needs `from` and `to`**, and the source must exist in the repo.

**`add` and `update` are forgiving in one direction only.** `add` on an
existing path is treated as an update, and `update` on an absent path is
treated as an add; both print a note. But `add`/`update` with **no
corresponding file under `files/`** is a hard error.

**`delete` on an absent path is a no-op**, noted and skipped.

## Test before handing it over

The repo is public. It can be fetched, applied to and verified in the sandbox
before the user ever sees the zip:

```bash
curl -sSL -o repo.zip \
  "https://codeload.github.com/contentcrawler47-bot/content-acquisition/zip/refs/heads/main"
# extract, then:
python3 tools/repo_manifest.py --verify            # confirm the base digest
python3 tools/apply_changeset.py /path/to/cs.zip --dry-run
python3 tools/apply_changeset.py /path/to/cs.zip
python3 tools/repo_manifest.py --verify            # confirm the produced digest
```

If several changesets are outstanding in one session, chain them onto a fresh
copy in order — that is the only way to be sure the second one's `base_digest`
is right.

**Run the changed code as well as applying it.** A changeset can apply
perfectly and still ship a broken tool.

## What is excluded from the manifest

From `repo_manifest.py`:

- **Directories:** `.git`, `__pycache__`, `out`, `.venv`, `.runs`, `.idea`
- **Suffixes:** `.pyc`, `.pyo`, **`.zip`**
- **Names:** `MANIFEST.sha256`, `NEXT_STEPS.md`, `.DS_Store`

`.zip` being excluded is why uploaded changesets in `changesets/` do not
change the digest. `out/` being excluded is why a harvest left lying around
does not either — but delete it before regenerating anyway, so the tree you
verify is the tree you shipped.

## Mistakes already made

**Asserting the digest instead of asking.** The base digest is the whole
ordering mechanism. Quote it, and quote the digest the changeset produces, in
the message that hands it over.

**Citing a rule without reading the log.** "Python standard library only" was
described in one session as a logged decision with a rationale, and used to
argue against a dependency. It was not in `DECISION-LOG.md` at all — it was a
line in the README describing the code as it stood. Check the log before
treating a constraint as settled.

**Not putting the expected digest inside a tracked file.** Writing the
post-application digest into a tracked file changes the digest it describes.
It belongs in the handover message and in `REFERENCE-DATA.md` on Drive, which
is not in the repo.

**Forgetting the workflow token.** Anything under `.github/workflows/` cannot
be pushed by `GITHUB_TOKEN`. It needs the `CHANGESET_TOKEN` secret, and the
user should be warned in the same message that hands over the zip — not after
it fails. The token has an expiry date; it is recorded in `REFERENCE-DATA.md`
on Drive, not here, because it changes and this file does not.

**Bundling unrelated fixes.** One changeset at a time. Two outstanding
changesets create an ordering dependency and the second is rejected by its
base digest check.

**Shipping a fix whose test shares the bug's assumption.** A synthetic fixture
written alongside the code tests the assumption twice rather than testing it
once against reality. Build fixtures from observed data — a real manifest, a
real payload, real category strings — not from what the code expects.

**Shipping a file with an invisible character in it.** A zero-width space
(U+200B) reached a draft of `SETUP.md`, survived being written, read back and
reviewed, and was caught only by an explicit scan for codepoints above 127.
The manifest records exact hashes, so one invisible character is a mismatch
that no amount of reading the file will explain. Scan every payload file
before building the zip:

```python
bad = [(i, hex(ord(c))) for i, c in enumerate(text) if ord(c) > 127]
```

Then judge the hits rather than stripping them: an em-dash and a box-drawing
character are intentional, a zero-width space never is.
