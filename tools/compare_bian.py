#!/usr/bin/env python3
"""
Compare a pasted BIAN landscape page against what we actually harvested.

Answers: which service domains are missing, and do they cluster?

    python3 tools/compare_bian.py                  # fetch the V14 value chain view
    python3 tools/compare_bian.py <url>            # any views/view_NNNNN.html
    python3 tools/compare_bian.py landscape.txt    # or a saved paste

View pages are static HTML — the service domain names are in the served markup,
unlike the object pages which need JavaScript. So this can fetch directly.

Reads out/bian/manifest.json, so run a harvest first. Read-only apart from that
one GET; no credentials.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

# The V14.0 Value Chain View — the canonical picture of the landscape.
DEFAULT_VIEW = ("https://bian.org/servicelandscape-14-0-0/"
                "views/view_54486.html")
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"

MANIFEST = Path("out/bian/manifest.json")
MARKER = "\u00abServiceDomain\u00bb"


def normalise(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VIEW
    if arg.startswith("http"):
        print(f"  fetching {arg}")
        req = urllib.request.Request(arg, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", errors="replace")
        # Names sit in SVG/HTML text nodes; strip tags, keep the text.
        text = re.sub(r"<[^>]+>", "\n", raw)
        text = text.replace("&#171;", MARKER[0]).replace("&#187;", MARKER[-1])
        text = text.replace("&laquo;", MARKER[0]).replace("&raquo;", MARKER[-1])
    else:
        text = Path(arg).read_text(encoding="utf-8")

    if not MANIFEST.is_file():
        print(f"{MANIFEST} not found — run: python3 run.py harvest bian")
        return 2
    manifest = json.loads(MANIFEST.read_text())
    items = manifest.get("items", {})

    harvested = {normalise(m["name"]): m["name"]
                 for m in items.values() if m.get("category") == "ServiceDomain"}
    all_names = {normalise(m["name"]): (m["name"], m.get("category", ""))
                 for m in items.values() if m.get("name")}

    version = "unknown"
    v = re.search(r"[Vv]ersion\s+(\d+\.\d+)|V(\d+\.\d+)", text)
    if v:
        version = v.group(1) or v.group(2)

    # Each marker is followed by the service domain name, then possibly a
    # heading before the next marker. Match harvested names against the start
    # of each blob; whatever fails to match is reported for manual reading.
    blobs = [b.strip() for b in text.split(MARKER)[1:]]
    print(f"\n{'=' * 68}")
    print(f"  Pasted landscape: version {version}, {len(blobs)} "
          f"{MARKER} markers")
    print(f"  Harvested:        {len(harvested)} ServiceDomain, "
          f"{len(items)} objects total")
    print(f"{'=' * 68}\n")

    matched, missing, elsewhere = [], [], []
    for blob in blobs:
        words = blob.split()
        hit = None
        # Longest-prefix match: names run up to 8 words ("Customer Product and
        # Service Directory"), so try longest first.
        for n in range(min(9, len(words)), 0, -1):
            key = normalise(" ".join(words[:n]))
            if key in harvested:
                hit = harvested[key]
                break
            if key in all_names and len(key) > 8:
                nm, cat = all_names[key]
                elsewhere.append((nm, cat))
                hit = "__elsewhere__"
                break
        if hit == "__elsewhere__":
            continue
        if hit:
            matched.append(hit)
        else:
            missing.append(" ".join(words[:6]))

    # A service domain can appear more than once on a page, so report
    # unique names rather than marker occurrences.
    matched_u = sorted(set(matched))
    elsewhere_u = sorted(set(elsewhere))
    missing_u = sorted(set(missing))
    print(f"  MATCHED as ServiceDomain in our harvest : {len(matched_u)} "
          f"unique ({len(matched)} occurrences)")
    print(f"  Present but under another category      : {len(elsewhere_u)}")
    print(f"  NOT FOUND in our harvest at all         : {len(missing_u)}\n")

    if elsewhere_u:
        print("  --- Miscategorised (would be fixed by stereotype handling) ---")
        by_cat = {}
        for nm, cat in elsewhere_u:
            by_cat.setdefault(cat, []).append(nm)
        for cat, names in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  filed as {cat} ({len(names)}):")
            for nm in sorted(names)[:20]:
                print(f"    - {nm}")
            if len(names) > 20:
                print(f"    ... and {len(names) - 20} more")
        print()

    if missing_u:
        print("  --- Absent from the harvest entirely ---")
        print("  (first words shown; trailing text may be a section heading)\n")
        for m in missing_u[:60]:
            print(f"    - {m}")
        if len(missing_u) > 60:
            print(f"    ... and {len(missing_u) - 60} more")
        print()

    print(f"{'=' * 68}")
    print("  CONCLUSION")
    print(f"{'=' * 68}")
    if not missing_u and not elsewhere_u:
        print("  Full coverage — the harvest matches this view.")
    elif elsewhere_u and not missing_u:
        print("  Everything is present, but some service domains are filed")
        print("  under another category. Fix _stereotypes() to prefer")
        print("  ServiceDomain when it appears anywhere in the list.")
    elif missing_u and not elsewhere_u:
        print("  Objects are genuinely absent. The harvest is reading one")
        print("  data file; run tools/diagnose_bian.py to find the others.")
    else:
        print("  Both problems: some miscategorised, some absent entirely.")
    if version not in ("14.0", "unknown"):
        print(f"\n  NOTE: the pasted page is version {version} but the harvest")
        print("  targets 14.0. Some differences will be genuine version drift.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
