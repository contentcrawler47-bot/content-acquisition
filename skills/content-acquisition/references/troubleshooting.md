# Known failure modes

Each entry is a failure that actually happened, with what it looked like and
what caused it. The pattern worth internalising: **most of these produced
plausible output, not an error.**

## Deciding where to look

Extraction and publishing are deliberately separate workflows, so the red one
already halves the search.

| Red workflow | Meaning |
|---|---|
| **Validate — \<source\>** | Source problem. Uses no Drive credentials. |
| **Check publishing target** | Drive problem. Touches no source. |
| **Source — \<source\>** | Read which step failed; extraction runs first. |
| **Acquire — \<source\>** | Two jobs. A red `acquire` referenced no Drive secrets, so it is a source or code problem. A red `archive` touched no source: a Drive problem, or a guard refusing with exit 2 — the remote folder already carries `ARCHIVED.json`, it is a first-layout per-file folder, or the run never finished. |
| **Check raw archive target** | The Drive half alone. Lists every run folder with its state. |

Within extraction, validation is staged — Connectivity, Payload, Parse,
Extract, Render — and stops at the first failure. **Ask for the failing stage
rather than the whole log**, and ask for logs as an uploaded file: pasting
them inline has repeatedly arrived empty in this project.

---

## Publishing

**`directory not found` on sync.** The Drive folder was created by hand in the
web UI. The OAuth scope is `drive.file`, which only sees files the application
itself created, so a hand-made folder is invisible to it. **Let rclone create
the folder.** Never pre-create a `content/<source>/` directory.

**Every rclone call dies with a version banner.** An environment variable was
named `RCLONE_VERSION`. rclone maps `RCLONE_<FLAG>` onto its own flags, so it
was read as `--version`. The workflows use `RC_VERSION` for this reason —
do not rename it back.

**Refresh token expired after about a week.** An own Google OAuth client in
*Testing* status expires refresh tokens in 7 days. The project uses rclone's
built-in client, whose app is published, so tokens do not expire. Do not
"improve" this by switching to a bespoke client.

**A source published but never appeared in the index.** `content/index.md` is
rebuilt only by **Reindex published sources**. Run it after any first publish.

**The index carried a week-stale date for one source.** Reindex was scheduled
before that source's harvest. The Monday order is load-bearing and reindex
must be **last**. Adding a source means re-checking that.

---

## Extraction

**A count that is plausible but wrong.** Reading one data shard instead of all
47 produced 222 service domains against a true 367. It looked like a
reasonable number and survived review. This is why thresholds and canaries
exist, and why a total should be checked against an independent source.

**A validator that passes on entirely broken output.** Counting fenced
PlantUML blocks passed on 1,181 diagrams of which **none rendered**. Counting
that a thing exists is not checking that it is valid. Run the real validator.

**A hand-written lint that could not catch the fault that shipped.** A lint
only catches faults already imagined. The fault that shipped had been reasoned
about and judged safe.

**A sample that was clean where the landscape was not.** The Savings Account
sample reported "0 unassignable attributes" because its 12 classes happened to
contain no enumerations. A clean sample is not a clean landscape — measure the
whole set before setting a threshold from it.

---

## Two paths that look alike and are not

**A trailing space in one sibling directory and not the other.**

```
release14.0.0/semantic-apis/oas3 /yamls/          <- trailing space, real
release14.0.0/apis-iso20022_ext-ddd/oas3/yamls/   <- no trailing space
```

The BIAN API repository genuinely has a trailing space after `oas3` in the
semantic path, and genuinely does not in the ISO 20022 path. Code that
normalises both by one rule — stripping the space, or adding it — finds
nothing in one of the two. Over HTTP the space must be encoded `%20`.

Symptom: operation counts collapse, or one set comes back empty while the
other is fine. Both paths are probed separately so the failing probe names the
cause immediately.

**No amount of reading the source's documentation would reveal this.** It is
only visible in the git tree. When a path looks odd, check its siblings before
assuming the oddity is uniform.

---

## A category name that exists in two spellings

The BIAN landscape spells its service domain category **both** ways:

| Category string | Objects |
|---|---|
| `ServiceDomain` | 367 |
| `Service Domain` | 2 |

A discovery heuristic testing `"service domain" in name.lower()` cannot match
the unspaced form — `"servicedomain"` has no space — so it silently locked
onto the 2-object stray and reported a **0% join** that looked like
catastrophic naming drift upstream.

Two lessons:

1. **Normalise category and key names the same way item names are
   normalised** — strip case and punctuation — and **union every match**
   rather than taking the first.
2. The tool already had the right normaliser and used it correctly on the
   names being joined. The knowledge was present and simply not applied one
   level up. When a comparison fails, check whether the codebase already
   solves that exact problem elsewhere.

Related: a dict keyed by name **collapses duplicates silently**. 369 domain
objects resolve to 348 distinct names because 21 share a name. Report objects
and distinct names separately, or a count will quietly mean something other
than its label.

---

## Delivering changes

**Loose files pasted into the web editor** produced a doubled paste, a
truncated file and a wrong filename. This is why changesets exist. See
`references/changesets.md`.

**A file silently missed** during manual application is what prompted manifest
verification in the first place.

**A file that reads correctly and fails the manifest.** An invisible character
— a zero-width space is the one actually encountered — survives writing,
reading back and review, because there is nothing to see. The manifest records
exact hashes, so it presents as an unexplainable content mismatch on a file
that looks right. Scan for codepoints above 127 before building a changeset,
and judge the hits rather than stripping them.

**The skills mount does not refresh mid-session.** After a skill is installed,
the new text is not visible until a new conversation. An installation cannot
be verified in the session that produced it; check the hash at the start of
the next one against `MANIFEST.sha256`.

## Reading the project context

**The connector read a file twice, once as base64.** `download_file_content`
returns base64, which is both unreadable and about a third larger than the
text; a session paid for a 15 KB handover twice before switching to
`read_file_content`. Use `read_file_content` for anything textual on Drive.

**The mirror is one snapshot behind Drive.** Expected at the start of most
sessions: the snapshot is written at the end of one session and mirrored by the
next scheduled run. Read the new snapshot's `README.md` to learn which files
were written rather than copied, and fetch only those through the connector —
or ask for a dispatch of **Mirror project context** and re-fetch.

**`gpg: decryption failed: Bad session key`** on a freshly fetched blob means
the key file and the blob come from different runs; the `snapshot` fields in
`latest-key.json` and `MIRROR.json` will disagree. A reader caught between the
push and the key write sees this for about a minute. Re-fetch both.

**`check_only` lists nothing.** `GDRIVE_MIRROR_TOKEN` was authorised at scope
`drive.file`, which cannot see files the connector wrote. Redo `SETUP.md` step 3a
with scope 2 (`drive.readonly`) and confirm with the `touch` refusal test
before extracting the token.

**Seven workflows reference `GDRIVE_CLIENT_ID` / `GDRIVE_CLIENT_SECRET`.** The
secrets have never existed; the lines expand to empty strings and select
rclone's built-in client, which is the intended path. They are inert, and are
removed as each workflow is next touched for its own reasons (`SETUP.md`,
Secrets section).
