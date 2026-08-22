"""
Staged validation of content extraction.

Stages run in order and stop at the first failure, so a report names one cause
rather than a cascade of consequences. Publishing is deliberately absent: it is
a separate concern with its own failure modes, checked by core.publish.

Reports counts and pass/fail only — never acquired text — because this is
summarised into Actions logs that are world-readable on a public repo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .diagnostics import ProbeResult, probe_all
from .source import Check, Source, Stage

MIN_TOTAL_KB = 20

STAGE_MEANING = {
    Stage.CONNECT: "Could not reach the source. Nothing was extracted; this is "
                   "an upstream, network or credential problem, not a parsing "
                   "one.",
    Stage.PAYLOAD: "The source responded, but not with the content expected. "
                   "The endpoint may have moved, changed format, or be "
                   "returning an error page with a 200 status.",
    Stage.PARSE:   "Content was received but could not be interpreted. The "
                   "upstream format has most likely changed.",
    Stage.EXTRACT: "Content parsed but yielded the wrong items. Either the "
                   "upstream restructured, or the extraction logic is "
                   "selecting the wrong things.",
    Stage.RENDER:  "Items were extracted but the written output is incomplete "
                   "or malformed. This is a bug in this repo, not upstream.",
}


# --------------------------------------------------------------------------
# stage 1-2: connectivity and payload, from the source's declared probes
# --------------------------------------------------------------------------

def probe_checks(source: Source, timeout: int = 45
                 ) -> tuple[list[Check], list[ProbeResult]]:
    specs = source.probes()
    if not specs:
        return ([Check("source declares probes", True,
                       "none declared — connectivity is not verified",
                       warn=True, stage=Stage.CONNECT,
                       hint="Implement probes() so onboarding failures are "
                            "diagnosable. See docs/ADDING_A_SOURCE.md.")], [])

    results = probe_all(specs, timeout=timeout)
    checks: list[Check] = []
    for r in results:
        # Two distinct questions per endpoint, so a healthy run shows
        # Connectivity actually passing rather than sitting empty:
        #   CONNECT — did the server respond at all?
        #   PAYLOAD — was the response the content we expected?
        reached = r.ok or r.status is not None
        warn = r.spec.optional

        checks.append(Check(
            f"connect to {r.spec.label}", reached,
            f"HTTP {r.status}, {r.seconds:.1f}s" if reached else r.summary,
            warn=warn and not reached, stage=Stage.CONNECT,
            hint="" if reached else r.hint))

        if not reached:
            continue

        checks.append(Check(
            f"payload from {r.spec.label}", r.ok,
            f"{r.bytes_read / 1024:,.0f} KB as expected" if r.ok else r.summary,
            warn=warn and not r.ok, stage=Stage.PAYLOAD,
            hint="" if r.ok else r.hint))
    return checks, results


# --------------------------------------------------------------------------
# stage 4-5: generic checks on what was written
# --------------------------------------------------------------------------

def output_checks(outdir: Path, source: Source) -> list[Check]:
    out: list[Check] = []

    if not outdir.is_dir():
        return [Check("output directory exists", False,
                      f"{outdir} missing",
                      stage=Stage.EXTRACT,
                      hint="The harvest did not run or exited before writing. "
                           "Run `python3 run.py harvest <source>` and read the "
                           "traceback.")]

    files = [f for f in outdir.iterdir() if f.is_file()]
    md_files = [f for f in files if f.suffix == ".md"]
    total_kb = sum(f.stat().st_size for f in files) / 1024

    out.append(Check("manifest.json written", (outdir / "manifest.json").is_file(),
                     stage=Stage.EXTRACT,
                     hint="write_bundles() was not reached — harvest() likely "
                          "raised part way through."))
    out.append(Check("index.md written", (outdir / "index.md").is_file(),
                     stage=Stage.RENDER))
    out.append(Check("markdown produced", len(md_files) >= 2,
                     f"{len(md_files)} files", stage=Stage.RENDER))
    out.append(Check("output size plausible", total_kb >= MIN_TOTAL_KB,
                     f"{total_kb:.0f} KB", stage=Stage.RENDER,
                     hint="Far smaller than expected — extraction probably "
                          "matched only a fraction of the source."))

    manifest_path = outdir / "manifest.json"
    if not manifest_path.is_file():
        return out
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as e:
        out.append(Check("manifest parses", False, type(e).__name__,
                         stage=Stage.EXTRACT,
                         hint="manifest.json is corrupt — the run was probably "
                              "interrupted mid-write."))
        return out

    out.append(Check("manifest parses", True, stage=Stage.EXTRACT))
    out.append(Check("manifest source id matches",
                     manifest.get("source") == source.id,
                     f"got {manifest.get('source')!r}, expected {source.id!r}",
                     stage=Stage.EXTRACT,
                     hint="Output belongs to a different source — check the "
                          "id attribute and that out/ was not reused."))
    items = manifest.get("items", {})
    out.append(Check("items extracted", bool(items), f"{len(items)} items",
                     stage=Stage.EXTRACT,
                     hint="Connected and parsed, but nothing was selected. "
                          "The upstream structure has probably changed."))

    body = "\n".join(f.read_text(encoding="utf-8") for f in md_files)
    leaks = {
        "HTML tags": (len(re.findall(r"<(span|div|p|br|table|td)\b", body)),
                      "Markup reached the output — clean_html() was not applied "
                      "to a field."),
        "HTML entities": (body.count("&nbsp;") + len(re.findall(r"&#\d+;", body)),
                          "Entities were not unescaped — same cause as above."),
    }
    for what, (n, hint) in leaks.items():
        out.append(Check(f"no {what} in markdown", n == 0, f"{n} found",
                         stage=Stage.RENDER, hint=hint))

    return out


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def _print(c: Check) -> None:
    mark = "PASS" if c.ok else ("WARN" if c.warn else "FAIL")
    print(f"    [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""),
          flush=True)


def report(source: Source, checks: list[Check], strict: bool = False,
           skipped_from: Stage | None = None) -> int:
    """Print a staged report and return an exit code."""
    stages = [s for s in Stage if s is not Stage.PUBLISH]
    by_stage: dict[Stage, list[Check]] = {s: [] for s in stages}
    for c in checks:
        by_stage.setdefault(c.stage, []).append(c)

    print(f"\n{'=' * 70}", flush=True)
    print(f"  {source.name or source.id} — content extraction validation",
          flush=True)
    print(f"{'=' * 70}", flush=True)

    for n, stage in enumerate(stages, 1):
        group = by_stage.get(stage) or []
        print(f"\n  [{n}/{len(stages)}] {stage.value}", flush=True)
        if not group:
            if skipped_from is not None and stage.order >= skipped_from.order:
                print("    (skipped — an earlier stage failed)", flush=True)
            else:
                print("    (no checks)", flush=True)
            continue
        for c in group:
            _print(c)

    failed = [c for c in checks if not c.ok and not c.warn]
    warned = [c for c in checks if not c.ok and c.warn]
    passed = len(checks) - len(failed) - len(warned)

    print(f"\n{'-' * 70}", flush=True)
    print(f"  {passed} passed, {len(failed)} failed, {len(warned)} warnings",
          flush=True)

    if not failed:
        if warned:
            print("\n  Warnings (not fatal):", flush=True)
            for c in warned:
                print(f"    - [{c.stage.value}] {c.name}"
                      + (f" — {c.detail}" if c.detail else ""), flush=True)
                if c.hint:
                    print(f"        {c.hint}", flush=True)
        if warned and strict:
            print("\n  FAILED (--strict: warnings are fatal)", flush=True)
            return 1
        print(f"\n  RESULT: {source.name or source.id} content extracted and "
              f"validated successfully.", flush=True)
        print("  Extraction is healthy. Any Drive problem is a publishing "
              "issue, not a source issue.", flush=True)
        return 0

    first_failed_stage = min((c.stage for c in failed), key=lambda s: s.order)
    print(f"\n  RESULT: FAILED at stage — {first_failed_stage.value}",
          flush=True)

    print(f"\n{'-' * 70}", flush=True)
    print("  DIAGNOSIS", flush=True)
    print(f"{'-' * 70}", flush=True)
    print(f"\n  Stage: {first_failed_stage.value}", flush=True)
    meaning = STAGE_MEANING.get(first_failed_stage)
    if meaning:
        for line in _wrap(meaning, 66):
            print(f"  {line}", flush=True)

    print("\n  Failed checks:", flush=True)
    for c in failed:
        print(f"\n    x {c.name}", flush=True)
        if c.detail:
            print(f"      observed: {c.detail}", flush=True)
        if c.hint:
            for line in _wrap(c.hint, 62):
                print(f"      {line}", flush=True)

    print("\n  Next steps:", flush=True)
    for step in _next_steps(first_failed_stage, source):
        for i, line in enumerate(_wrap(step, 62)):
            print(f"    {'-' if i == 0 else ' '} {line}", flush=True)

    print("", flush=True)
    return 1


def _next_steps(stage: Stage, source: Source) -> list[str]:
    common = [
        f"Reproduce locally: python3 run.py validate {source.id}",
        "If it passes locally but fails in CI, suspect IP blocking or a "
        "missing secret in the workflow rather than the source itself.",
    ]
    specific = {
        Stage.CONNECT: [
            "Open the failing URL in a browser. If it loads there but not "
            "here, the client is being filtered.",
            f"Check required secrets are set: "
            f"{', '.join(source.required_secrets) or 'none for this source'}.",
        ],
        Stage.PAYLOAD: [
            "Compare the live URL against what probes() expects — the prefix "
            "or marker assertion is what failed.",
            "Check the version pinning at the top of the source module; "
            "upstream may have published a new release.",
        ],
        Stage.PARSE: [
            "Fetch the payload by hand and inspect its first few hundred "
            "bytes against the parser's assumptions.",
        ],
        Stage.EXTRACT: [
            "Run the harvest and compare item counts against the canary "
            "thresholds in the source's checks().",
            "If the upstream legitimately shrank, update the thresholds "
            "deliberately rather than lowering them to make this pass.",
        ],
        Stage.RENDER: [
            "This is a bug in this repo, not upstream. The failing check "
            "names the field or transform at fault.",
        ],
    }
    return specific.get(stage, []) + common


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines
