# Ending a session

Procedure reference for the content-acquisition skill.

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
- **Promote lessons.** Anything in the handover's "how this session worked"
  that will still be true when the numbers change goes into `LESSONS.md` as an
  entry, once. Rules that must never be broken go to the skill instead, as a
  changeset; that should be rare.
- **Carry the `playbooks/` subfolder.** Create it in the new snapshot and
  `copy_file` each playbook into it; the README lists them like any other file.
- **Then the mirror catches up on its own.** The next scheduled run of
  **Mirror project context** publishes the new snapshot; until it does, the
  next session reads this snapshot's README to learn which files were written
  rather than copied, and fetches only those through the connector.

**Copy Drive ids exactly** — file ids, and folder ids in `parentId`. One
dropped character cost a 48 KB document a second emission; the price of a
typo is the size of the file, not the size of the mistake.

**Writing costs what is emitted, not where it goes.** The sandbox cannot reach
Drive — egress is allowlisted to GitHub, npm, PyPI and Ubuntu — so there is no
path from disk to Drive that avoids re-emitting every byte. Write once,
straight to where it will live; `copy_file` rather than rewrite; emit the
generator rather than the output where a script can produce the file.
Compression does not help: base64 hands back most of what zip saves, and
emitting it means reading it into context first.

Reading is not free either, and the connector reads whole files only. Read
through the mirror by section (`references/context-mirror.md`); through the
connector, `SESSION-HANDOVER.md` first and the rest only as the task requires,
and always `read_file_content` for text — `download_file_content` returns
base64, which costs more and cannot be read.

