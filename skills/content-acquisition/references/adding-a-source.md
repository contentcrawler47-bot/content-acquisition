# Adding a source

Procedure reference for the content-acquisition skill. The rule that logs and
artifacts are public, and what may therefore never be printed or uploaded, is
in `SKILL.md`.

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

