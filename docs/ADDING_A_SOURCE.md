# Adding a content source

Adding a source touches **only new files**. No central registry, no shared
module to edit, no existing workflow to modify — so a new source cannot break
an existing one.

## 1. Copy the template

```
cp -r sources/_template sources/acme
```

Directories beginning with `_` are skipped by discovery, so the template never
runs.

## 2. Implement the source

### Start with `probes()` — before `harvest()`

`probes()` declares what must be reachable and what each endpoint should return.
Write it first. It is what lets a new source prove it can connect at all, and
it is what separates "cannot reach the source" from "cannot parse it" for the
rest of the source's life.

```python
def probes(self):
    return [ProbeSpec(
        label="items endpoint",
        url="https://acme.example/api/items",
        expect_prefix="{",          # or expect_contains='"items"'
        min_bytes=100)]
```

Assert the payload's *shape*, not just an HTTP 200 — sites routinely return 200
with an error page or a login redirect. `probe()` classifies DNS failures, TLS
failures, timeouts, 401/403/404/429/5xx, gate redirects, truncated bodies and
wrong formats, each with its own remediation hint.

Then:

```
python3 run.py validate acme
```

If Connectivity and Payload pass, the source is reachable and you can write
`harvest()` knowing any further failure is your parsing, not the network.


Edit `sources/acme/source.py`. The class **must** be named `Source`.

```python
class Source(BaseSource):
    id = "acme"                                  # slug: CLI arg, out/ dir, Drive folder
    name = "ACME Standards Library"
    description = "One line for indexes and logs"
    required_secrets = ["ACME_API_TOKEN"]        # namespace with the source id

    def harvest(self, outdir: Path) -> HarvestResult: ...
    def checks(self, outdir: Path) -> list[Check]: ...
```

`harvest()` writes into `outdir`, which is exclusive to this source and emptied
before each run. Call `write_bundles()` from `core.render` at the end and every
source produces the same output shape — `index.md`, grouped markdown,
`manifest.json` — so the generic checks and the Drive layout apply unchanged.

Read credentials from `os.environ` only. Never write a credentials file.

### Write a canary, and tag every check with a stage

In `checks()`, assert one known item is present with a known name. This turns
silent upstream drift into a loud failure. BIAN's canary is object `34300`,
"Consumer Loan".

Tag each `Check` with the `Stage` it belongs to — `Stage.EXTRACT` for "did we
get the right items", `Stage.RENDER` for "is the markdown clean" — and give it
a `hint` saying what to look at:

```python
Check("object count", len(items) >= 1000, f"{len(items)} items",
      stage=Stage.EXTRACT,
      hint="Far fewer than expected. Compare against the live file "
           "before lowering this threshold.")
```

The hint is what a failed run shows someone at 9am who did not write the
source. Write it for them.

## 3. Test locally — extraction first, publishing second

```
python3 run.py list                     # is it discovered? what secrets?
python3 run.py validate acme            # can we EXTRACT? (no Drive)
python3 run.py check-publish            # can we PUBLISH? (no source)
python3 run.py publish acme --dry-run   # both, without writing
```

Keep these separate while onboarding. `validate` failing tells you about the
source; `check-publish` failing tells you about Drive. Running them together
only blurs the two.

## 4. Add secrets

Add each name in `required_secrets` as a repo secret. The CLI refuses to
harvest when any are missing, rather than producing partial output.

One secret per sensitive value — GitHub's log redaction matches exact values
and copes badly with structured blobs.

## 5. Add the workflows

**Two files per source.** Copy both, and change the same fields in each.

`validate-bian.yml` → `validate-acme.yml` gives you an extraction-only run that
references no Drive secrets — the thing you will use most while onboarding, and
the first thing to run when a scheduled harvest goes red.

`source-bian.yml` → `source-acme.yml` is the scheduled harvest-and-publish. It
runs extraction first and labels which half failed.

In both, change:

| What | To |
|---|---|
| `name:` | `Source — ACME Standards Library` |
| `env.SOURCE_ID` | `acme` |
| `concurrency.group` | `source-acme` |
| `jobs.<key>` and `jobs.<key>.name` | `acme` / a description |
| `cron` | a different time from other sources |
| Harvest/validate step `env:` | this source's secrets |

Leave the publish step alone — the Drive half is identical for every source.

**Stagger the cron times.** Sources are independent, but they share one Drive
account and its rate limits.

## 6. Reindex

Run the **Reindex published sources** workflow once so the new source appears
in the top-level `content/index.md`.

## Design rules

- **A source never imports another source.** Shared logic belongs in `core/`.
- **A source never publishes.** It writes to `outdir`; `core/publish.py` syncs.
- **A source never writes outside its `outdir`.**
- **Failures stay local.** A source that raises, has missing secrets, or won't
  even import is reported and skipped; the others still run.
