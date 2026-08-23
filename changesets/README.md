# Changesets

Upload a changeset zip here, then run the **Apply changeset** workflow.

The zip contains `CHANGESET.json` (the operations), `MANIFEST.sha256` (the
exact end state) and `files/` (new and updated content mirroring repo paths).

Verification runs before the commit, so a changeset that does not produce the
declared state is never pushed.
