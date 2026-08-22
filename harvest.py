#!/usr/bin/env python3
"""
BIAN Service Landscape harvester.

The landscape app is a Backbone client that loads its entire dataset from
static JavaScript files of the form `var objectData = { ... }`. There is no
API, no gate and no rendering step — so this just downloads those files,
strips the assignment prefix, parses the JSON and writes clean markdown.

    python harvest.py                 full run -> ./out/
    python harvest.py --object 42877  print one object, for checking

Replaces the Playwright crawler entirely. No browser, no secrets.
"""

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://bian.org/servicelandscape-14-0-0"
VIEW = 16                       # matches object_16.html / all_objects_data_16.js
OUTDIR = Path("out")
PER_FILE = 40                   # service domains per markdown bundle
TIMEOUT = 120

FILES = {
    "objects":   f"{BASE}/data/all_objects_data_{VIEW}.js",
    "relations": f"{BASE}/data/all_objects_relations.js",
    "mapping":   f"{BASE}/data/all_objects_data_mapping.js",
    "on_views":  f"{BASE}/data/all_objects_on_views.js",
    "config":    f"{BASE}/data/config_data.js",
}

UA = "Mozilla/5.0 (compatible; bian-harvester/1.0)"


def log(msg):
    print(msg, flush=True)


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_js_assignment(text):
    """Turn `var name = <json>;` into a Python object."""
    m = re.match(r"\s*var\s+\w+\s*=\s*", text)
    if not m:
        raise ValueError("unexpected file format — no var assignment")
    body = text[m.end():].strip()
    body = re.sub(r";\s*$", "", body)
    return json.loads(body)


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


def clean_rtf(value):
    """Values are HTML fragments with inline styling. Reduce to plain text,
    turning paragraph breaks into newlines rather than losing them."""
    if not value:
        return ""
    s = re.sub(r"<p[^>]*>|<br\s*/?>", "\n", value, flags=re.I)
    s = TAG_RE.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = WS_RE.sub(" ", s)
    lines = [l.strip() for l in s.splitlines()]
    return "\n".join(l for l in lines if l).strip()


def stereotypes(entry):
    for cat in entry.get("categories", []):
        if cat.get("type") == "table":
            st = cat.get("content", {}).get("Stereotypes", {}).get("stereotype", {})
            return list(st.get("value", []))
    return []


def properties(entry):
    for cat in entry.get("categories", []):
        if cat.get("type") == "table":
            return cat.get("content", {})
    return {}


def documentation(entry):
    """Ordered {title: text} from documentation categories, skipping empties."""
    out = {}
    for cat in entry.get("categories", []):
        if cat.get("type") != "documentation":
            continue
        title = cat.get("title", "documentation")
        text = clean_rtf(cat.get("content", {}).get("value", ""))
        if text:
            out[title] = text
    return out


def flatten(value):
    """Property values are strings, {type:link}, or {type:collection}."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "link":
            v = value.get("value", {})
            return f"{v.get('title', '')} — {v.get('location', '')}".strip(" —")
        if kind == "object":
            return value.get("value", {}).get("name", "")
        if kind == "collection":
            items = [flatten(i) for i in value.get("value", [])]
            return [i for i in items if i]
    return ""


def render_relations(oid, relations, names):
    """`var objectRelations = {id: [{via, to:[ids]}]}` — a labelled directed
    graph. Resolve target ids to names, skipping unnamed connector objects."""
    rels = relations.get(str(oid)) or []
    if not rels:
        return []
    lines = ["### Relationships"]
    for rel in sorted(rels, key=lambda r: r.get("via", "")):
        via = rel.get("via", "").strip()
        if via in ("", "<unknown role>"):
            continue
        targets = []
        for tid in rel.get("to", []):
            nm = names.get(str(tid))
            if nm and nm != "Realization relation":
                targets.append(f"{nm} ({tid})")
        if targets:
            lines.append(f"- **{via}:** " + "; ".join(sorted(targets)))
    return lines + [""] if len(lines) > 1 else []


def render(oid, entry, relations=None, names=None):
    """One object as a markdown section."""
    name = entry.get("name", f"Object {oid}")
    otype = entry.get("type", "")
    sts = stereotypes(entry)
    label = sts[0] if sts else otype

    lines = [f"## {name}", ""]
    lines.append(f"- **Object id:** {oid}")
    lines.append(f"- **Type:** {otype}" + (f" ({', '.join(sts)})" if sts else ""))
    lines.append(f"- **Source:** {BASE}/object_{VIEW}.html?object={oid}")
    lines.append("")

    for title, text in documentation(entry).items():
        if title == "documentation":
            title = "Description"
        lines.append(f"### {title}")
        lines.append(text)
        lines.append("")

    props = properties(entry)
    for group, fields in props.items():
        if group in ("Stereotypes",) or not isinstance(fields, dict):
            continue
        rows = []
        for key, raw in fields.items():
            val = flatten(raw)
            if isinstance(val, list):
                if not val:
                    continue
                rows.append(f"- **{key}:** ({len(val)})")
                rows += [f"  - {v}" for v in val]
            elif val:
                val = " / ".join(v.strip() for v in val.split("\n") if v.strip())
                rows.append(f"- **{key}:** {val}")
        if rows:
            lines.append(f"### {group}")
            lines += rows
            lines.append("")

    if relations is not None:
        lines += render_relations(oid, relations, names or {})

    lines.append("---")
    lines.append("")
    return "\n".join(lines), label


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--object", help="print a single object and exit")
    args = ap.parse_args()

    log(f"downloading {len(FILES)} data files")
    raw = {}
    for key, url in FILES.items():
        text = download(url)
        log(f"  {key:<10} {len(text) / 1024:>8.0f} KB")
        raw[key] = text

    objects = parse_js_assignment(raw["objects"])
    try:
        relations = parse_js_assignment(raw["relations"])
    except Exception:
        relations = {}
    names = {oid: (o.get("data") or [{}])[0].get("name", "")
             for oid, o in objects.items()}
    log(f"parsed {len(objects)} objects, {len(relations)} with relations")

    if args.object:
        entry = objects.get(str(args.object))
        if not entry:
            log(f"object {args.object} not found")
            return 2
        body, _ = render(args.object, entry["data"][0], relations, names)
        print(body)
        return 0

    # Group by stereotype so service domains are separable from the rest.
    groups = {}
    records = []
    for oid, obj in objects.items():
        data = obj.get("data") or []
        if not data:
            continue
        entry = data[0]
        body, label = render(oid, entry, relations, names)
        groups.setdefault(label or "Other", []).append((entry.get("name", ""), body))
        records.append({
            "id": oid,
            "name": entry.get("name", ""),
            "label": label,
            "sha256": hashlib.sha256(body.encode()).hexdigest(),
        })

    OUTDIR.mkdir(exist_ok=True)
    for f in OUTDIR.glob("*"):
        f.unlink()

    index = [
        "# BIAN Service Landscape 14.0.0",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} "
        f"from {len(objects)} objects.",
        "",
        "| Category | Count | Files |",
        "|---|---|---|",
    ]

    for label in sorted(groups, key=lambda k: -len(groups[k])):
        items = sorted(groups[label])
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "other"
        bundles = [items[i:i + PER_FILE] for i in range(0, len(items), PER_FILE)]
        names = []
        for n, bundle in enumerate(bundles, 1):
            fname = f"{slug}_{n:02d}.md" if len(bundles) > 1 else f"{slug}.md"
            header = f"# {label} ({len(bundle)} entries)\n\n"
            (OUTDIR / fname).write_text(
                header + "\n".join(b for _, b in bundle), encoding="utf-8")
            names.append(fname)
        index.append(f"| {label} | {len(items)} | {', '.join(f'`{n}`' for n in names)} |")
        log(f"  {label:<24} {len(items):>5} -> {len(names)} file(s)")

    (OUTDIR / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    (OUTDIR / "objects_raw.json").write_text(
        json.dumps(objects, ensure_ascii=False), encoding="utf-8")
    (OUTDIR / "manifest.json").write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "view": VIEW,
        "count": len(records),
        "objects": {r["id"]: {"sha256": r["sha256"], "name": r["name"],
                              "label": r["label"]} for r in records},
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    log(f"done: {len(records)} objects written to {OUTDIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
