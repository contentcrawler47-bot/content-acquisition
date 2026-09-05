# Diagnosing a failed run

Procedure reference for the content-acquisition skill.

Extraction and publishing are deliberately separated, so the failing workflow
halves the search before you read anything: a red **Validate** is a source
problem and uses no Drive credentials; a red **Check publishing target** is a
Drive problem and touches no source; a red **Join** means neither source is
broken, so check the matching before the sources. In **Acquire**, a red
`acquire` job is a source or code problem and referenced no Drive secrets; a
red `archive` job is a Drive problem or a guard refusing (exit 2: the remote
folder already carries `ARCHIVED.json`, is a first-layout per-file folder, or
the run never finished) and touched no source. In **Extract** and
**Regenerate**, a red `restore` job is a Drive or archive problem (or a
refusal: run absent, incomplete, or a broken pointer) and a red `extract` job
has no credentials, fetched nothing and is code or data. A red **Render** with
a `::notice` is a cache miss naming the Regenerate dispatch, not a fault.
**Check raw archive target** tests the Drive half alone, lists every run
folder with its state, and sweeps every pointer against its target.

Within a chunked landscape run, read which *job* failed — `plan` made no page
requests at all, `chunk N` localises it to one slice, `assemble` means nothing
was published. Within extraction, validation is staged and stops at the first
failure.

**Check what the run actually executed, not what the repo says it should.**
A log carries the expanded command line and the resolved `env` block; a
workflow input evaluated from a stale dispatch value, or a checkout of an older
commit, are both visible there and neither is visible in the repo. Two runs in
this project were read as results before anyone noticed one had run five
changesets behind and another had not enforced.

**A stale checkout can verify clean.** A cached tarball of `main` matched its
own manifest perfectly while being five changesets old — the digest proves
internal consistency, not currency. Cache-bust the fetch, and confirm a marker
that moves with the change: a version constant, a new symbol, a schema version.

**Ask for the failing job or stage specifically**, not the whole log, and ask
for it as an **uploaded file** — pasting inline has repeatedly arrived empty in
this project.

**For a question about the data rather than the run, ask for the data as a
zip.** A run folder from `raw/` on Drive unzips in the sandbox and can be
queried with scripts over the whole population; only the answers enter the
context window, not the megabytes. Several questions that resisted reasoning
were settled exactly this way in minutes. No workflow uploads run or extract
bytes as an artifact since 073c: runs travel in the Actions cache and live on
Drive; an extract is rebuilt in the sandbox from its run with `run.py extract`
in under a minute, and its content digest says whether it is the one CI made.

`references/troubleshooting.md` lists the failures already encountered with
their causes.

## Where the retained data is

**Retained raw runs.** `raw/<source-id>/<run-id>/` on Drive, a sibling of
`content/`, written only by rclone from the **Acquire** workflow's archive
job: `run.json`, `manifest.json` and `RAW.sha256` plain, every payload file
inside `payload.zip` under its run-relative path, and `ARCHIVED.json` written
last -- its presence means the archive is complete, the way a snapshot's
README does. Read the plain files with the connector when a question is about
what the source served on a given day; the digests in `RAW.sha256` are of the
decoded bytes, and `tools/check_raw.py` reads members straight from the zip.
Never create anything under `raw/` by hand: rclone runs with the `drive.file`
scope and cannot see files it did not create. Deleting a folder rclone made is
safe. A folder with the marker is never rewritten; one without it is an
interrupted copy that the next archive of the same run resumes.

**De-duplicated runs.** When a run's payload bytes are identical to an
archived run's -- `bianlib.acquire.run_digest`, over the sidecar's payload
lines -- the folder holds `SAME_AS.json` naming that run and no
`payload.zip`. It is still a record that the source was checked that day.
Pointers are one hop. To read one in the sandbox, download **both** folders
side by side under one parent; `check_raw.py` and `extract` read the payload
through the sibling, verify it against the pointing run's own sidecar, and
say so. `ARCHIVED.json` records `run_digest` and `same_as`, and `check_raw`
recomputes both rather than trusting them. **Check raw archive target** lists
every run with its pointer and, with `pointers` on, sweeps the whole archive
from its record files (`check_raw.py --pointers` over a root of run folders
does the same locally). Never delete a run another run points to.

**Consuming a run.** `run.py restore <source> --run-id <id>` copies an
archived run -- and its pointer target as a sibling -- into
`out/_raw/<source>/` and verifies each folder; blank `--run-id` means the
newest archived run. Extract and Regenerate do exactly this in their
`restore` job, cache first for Extract, Drive only for Regenerate.
