#!/usr/bin/env python3
"""
BIAN Service Landscape harvester.

Runs headless Firefox via Playwright, clears the name/email gate once per
session, visits each object id, and writes consolidated markdown plus a
manifest to ./out/.

IMPORTANT: this runs in a PUBLIC repo. Nothing extracted is ever printed to
stdout — only ids, character counts and status. Keep it that way.

    python crawl.py --probe 42877        one page, diagnostics only, no output files
    python crawl.py --ids-file ids.txt   full run
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

LANDING = "https://bian.org/servicelandscape-14-0-0/"
OBJECT_URL = "https://bian.org/servicelandscape-14-0-0/object_16.html?object={id}"

OUTDIR = Path("out")
PER_FILE = 25          # objects bundled into each markdown file
DELAY = 3.0            # seconds between pages
SETTLE_TIMEOUT = 25    # max seconds waiting for the SPA to render
MIN_CHARS = 400        # below this, treat the page as not rendered

GATE_NAME = os.environ.get("BIAN_NAME", "")
GATE_EMAIL = os.environ.get("BIAN_EMAIL", "")


def log(msg):
    print(msg, flush=True)


def clear_gate(page):
    """Submit the name/email gate if present. Selectors are best-effort —
    adjust after inspecting the probe output."""
    if not GATE_EMAIL:
        log("gate: no BIAN_EMAIL set, skipping")
        return False

    page.goto(LANDING, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)

    email_sel = (
        "input[type=email], input[name*=email i], "
        "input[id*=email i], input[placeholder*=email i]"
    )
    field = page.query_selector(email_sel)
    if not field:
        log("gate: no email field found — either already cleared or not gated")
        return False

    try:
        field.fill(GATE_EMAIL)
        name_sel = "input[name*=name i], input[id*=name i], input[placeholder*=name i]"
        name_field = page.query_selector(name_sel)
        if name_field and GATE_NAME:
            name_field.fill(GATE_NAME)

        submit = page.query_selector(
            "button[type=submit], input[type=submit], "
            "button:has-text('Submit'), button:has-text('Continue')"
        )
        if submit:
            submit.click()
        else:
            field.press("Enter")

        page.wait_for_timeout(5000)
        log("gate: submitted")
        return True
    except Exception as e:
        log(f"gate: submission failed ({type(e).__name__})")
        return False


def wait_until_rendered(page, timeout=SETTLE_TIMEOUT):
    deadline = time.time() + timeout
    last, stable = -1, 0
    while time.time() < deadline:
        try:
            n = page.evaluate("document.body.innerText.length")
        except Exception:
            n = 0
        if n == last and n > 200:
            stable += 1
            if stable >= 3:
                return True
        else:
            stable = 0
        last = n
        page.wait_for_timeout(500)
    return False


def extract(page, oid, captured):
    url = OBJECT_URL.format(id=oid)
    captured.clear()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    wait_until_rendered(page)

    text = page.evaluate("document.body.innerText") or ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    frames = []
    for f in page.frames[1:]:
        try:
            ft = f.evaluate("document.body.innerText") or ""
            if len(ft) > 200:
                frames.append(ft.strip())
        except Exception:
            pass
    if frames:
        text = text + "\n\n" + "\n\n".join(frames)

    return {
        "object_id": str(oid),
        "url": url,
        "title": page.title(),
        "text": text,
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "api_urls": sorted({c["url"] for c in captured}),
        "api_payloads": [c["body"] for c in captured][:5],
    }


def attach_listener(page, captured):
    def on_response(resp):
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype.lower():
            try:
                captured.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass
    page.on("response", on_response)


def write_output(records):
    OUTDIR.mkdir(exist_ok=True)
    for f in OUTDIR.glob("*"):
        f.unlink()

    records.sort(key=lambda r: int(r["object_id"]) if r["object_id"].isdigit() else 0)
    bundles = [records[i:i + PER_FILE] for i in range(0, len(records), PER_FILE)]

    index = ["# BIAN Service Landscape extract", ""]
    index.append(f"Objects: {len(records)}  |  Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    index.append("")

    for n, bundle in enumerate(bundles, 1):
        fname = f"objects_{n:03d}.md"
        lines = []
        for r in bundle:
            lines.append(f"## Object {r['object_id']} — {r['title']}")
            lines.append(f"Source: {r['url']}")
            lines.append("")
            lines.append(r["text"])
            lines.append("")
            lines.append("---")
            lines.append("")
        (OUTDIR / fname).write_text("\n".join(lines), encoding="utf-8")

        first, last = bundle[0]["object_id"], bundle[-1]["object_id"]
        index.append(f"- `{fname}` — objects {first} to {last} ({len(bundle)} entries)")

    (OUTDIR / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(records),
        "objects": {
            r["object_id"]: {
                "sha256": r["sha256"],
                "chars": r["chars"],
                "title": r["title"],
            } for r in records
        },
    }
    (OUTDIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    endpoints = sorted({u for r in records for u in r["api_urls"]})
    if endpoints:
        (OUTDIR / "api_endpoints.txt").write_text("\n".join(endpoints), encoding="utf-8")


def compare_manifest(records, prev_path="previous_manifest.json"):
    p = Path(prev_path)
    if not p.exists():
        return
    try:
        prev = json.loads(p.read_text())["objects"]
    except Exception:
        return
    changed = [r["object_id"] for r in records
               if prev.get(r["object_id"], {}).get("sha256") != r["sha256"]]
    new = [r["object_id"] for r in records if r["object_id"] not in prev]
    log(f"changed since last run: {len(changed)} | new: {len(new)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", help="file with one object id per line")
    ap.add_argument("--probe", help="single id, diagnostics only")
    args = ap.parse_args()

    if args.probe:
        ids = [args.probe]
    elif args.ids_file:
        ids = [l.strip() for l in Path(args.ids_file).read_text().splitlines()
               if l.strip() and not l.startswith("#")]
    else:
        ap.error("need --ids-file or --probe")

    records, failures = [], []

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        captured = []
        attach_listener(page, captured)

        clear_gate(page)

        for oid in ids:
            try:
                rec = extract(page, oid, captured)
            except Exception as e:
                log(f"{oid}: FAILED ({type(e).__name__})")
                failures.append(oid)
                continue

            status = "ok" if rec["chars"] >= MIN_CHARS else "THIN"
            log(f"{oid}: {rec['chars']} chars, {len(rec['api_urls'])} json calls [{status}]")
            if status == "THIN":
                failures.append(oid)
            records.append(rec)

            if args.probe:
                log("--- probe diagnostics ---")
                log(f"title: {rec['title']}")
                log("json endpoints observed:")
                for u in rec["api_urls"]:
                    log(f"  {u}")
                log(f"first 200 chars length check: {min(200, rec['chars'])}")
                browser.close()
                return 0 if rec["chars"] >= MIN_CHARS else 2

            time.sleep(DELAY)

        browser.close()

    write_output(records)
    compare_manifest(records)

    good = sum(1 for r in records if r["chars"] >= MIN_CHARS)
    log(f"done: {good}/{len(ids)} rendered, {len(failures)} problem ids")
    if good == 0:
        log("nothing rendered — check the gate selectors or IP blocking")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
