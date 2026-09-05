# The context mirror

Procedure reference for the content-acquisition skill. Introduced at changeset
072; the design decision and its rejected alternatives are recorded in the
project context on Drive, not here.

## What it is

The Drive connector reads one file at a time, whole, into context. A 49 KB
design document costs about 13,000 tokens to consult for one section, and is
paid for again on every turn after. The sandbox reads by line range and greps
across everything, but cannot reach Drive.

So the **Mirror project context** workflow copies the latest complete snapshot
of `content/_project-context/` out of Drive, adds a generated index and a
manifest, encrypts the whole thing under a fresh random key, and force-pushes
the ciphertext to the orphan `context` branch of this public repository. The
key goes to Drive as `context-mirror/latest-key.json`, where only the connector
(acting as the user) can read it.

Bulk bytes over the public channel, encrypted; the small secret over the
private channel the session already has. Nothing is typed by anyone.

## Reading it, at session start

1. Through the connector, read `context-mirror/latest-key.json` -- at the
   **Drive root**, not under `content/`; the folder is where rclone's
   `drive.file` remote creates it, and the connector finds it by title. It is
   about 120 characters: `{"snapshot": "<folder name>", "key": "<64 hex>"}`.
   Use `read_file_content`, and `excludeContentSnippets: true` on any listing
   that could include that folder.
2. In the sandbox:
   ```
   B="https://raw.githubusercontent.com/contentcrawler47-bot/content-acquisition/context"
   N="?nocache=$(date +%s)"
   curl -sL -o MIRROR.json "$B/MIRROR.json$N"
   curl -sL -o context.tar.gz.gpg "$B/context.tar.gz.gpg$N"
   sha256sum context.tar.gz.gpg              # must equal blob_sha256 in MIRROR.json
   printf '%s' '<key>' > key.txt && chmod 600 key.txt
   gpg --batch --quiet --decrypt --passphrase-file key.txt -o context.tar.gz context.tar.gz.gpg
   mkdir ctx && tar xzf context.tar.gz -C ctx && cd ctx
   sha256sum --quiet -c MANIFEST.sha256      # every file as the workflow saw it
   ```
   The `snapshot` in the key file must equal the one in `MIRROR.json`; a
   mismatch means the workflow was caught between pushing the blob and writing
   the key, and the next run resolves it. Try again in a few minutes, or ask
   the user to dispatch the workflow.
3. Read `INDEX.md` first: every document, its sections with line numbers, its
   size and hash. Then `view` the sections the task needs, and `grep -rn`
   across the snapshot for anything that spans documents.

**The key enters the transcript when it is read.** That is the designed
residual exposure: it decrypts one snapshot, whose contents the session would
partly contain anyway, and it is replaced at the next mirror run. A leaked
September key opens September.

## When the mirror is behind Drive

A snapshot written at the end of a session is mirrored by the next scheduled
run, so the mirror is often one snapshot behind at the start of the next
session. Confirm with one connector listing of `content/_project-context/`
(`excludeContentSnippets: true`): the latest dated folder that contains a
`README.md` is current.

If it is newer than `MIRROR.json` says, do **not** read the whole thing through
the connector. Its `README.md` lists which files were **written** in that
snapshot and which were **copied**; only the written ones differ from the
mirror. Read those few through the connector; everything else is in the
decrypted tree. Or ask the user to dispatch **Mirror project context**, which
takes about a minute, and re-fetch.

If the mirror is absent entirely — first session after 072, or the branch has
been deleted — fall back to the connector: `SESSION-HANDOVER.md`, then
`LESSONS.md`, then the rest only as the task requires.

## The archive's layout

```
ctx/
  INDEX.md              generated: per document, headings with line numbers, bytes, sha256
  MANIFEST.sha256       generated: every file under the snapshot folder
  2026-09-05e/          the snapshot, byte for byte as on Drive
    README.md
    SESSION-HANDOVER.md
    LESSONS.md
    playbooks/
    ...
```

Both generated files are produced by `tools/context_index.py`, which also runs
in the sandbox against any local copy of a snapshot.

## What the workflow does, and what can go wrong

`.github/workflows/mirror-context.yml`: dispatch, or four times a day.
Idempotent — it reads the current `MIRROR.json` from the branch and exits early
when the latest complete snapshot is already mirrored, unless dispatched with
`force: true`, which also rotates the key. `check_only: true` lists the folder
and exits without mirroring; it is the first run after any credential change.

Two rclone remotes, on purpose. `MIRROR` reads, with a `drive.readonly` token
(`GDRIVE_MIRROR_TOKEN`) and `root_folder_id` pinned to `_project-context`; the
`drive.file` token cannot see files the connector wrote, which is why it
exists. `GDRIVE`, the existing `drive.file` remote, does the one write: the key
file, into a folder rclone creates, so the file is user-owned and the connector
can see it. A service-account write would not be.

| Symptom | Cause |
|---|---|
| `check_only` lists nothing, or `403` | `GDRIVE_MIRROR_TOKEN` was authorised at scope `drive.file`; redo at `drive.readonly` (SETUP.md step 3a) |
| `no complete snapshot found` | every folder on Drive lacks a `README.md`; the last session's write failed |
| gpg: `decryption failed: Bad session key` | key and blob from different runs; see the mismatch note above |
| `latest-key.json` not visible to the connector | it was written by the wrong remote; must be `GDRIVE` |
| the sandbox fetch returns stale `MIRROR.json` | CDN cache; the `nocache` query is not optional |

## Rotation

Every successful run replaces the key, so rotation is the normal case. To
rotate deliberately — a transcript that held a key is suspected leaked —
dispatch with `force: true`. The old blob leaves the branch with the
force-push; a copy already downloaded stays decryptable with the old key, which
is why the exposure is bounded by "one snapshot" rather than by nothing.

Revoking rclone's access in the Google account's security settings revokes
**both** Drive tokens, since they share rclone's built-in client. Recovery is
re-authorising both (SETUP.md steps 3 and 3a).
