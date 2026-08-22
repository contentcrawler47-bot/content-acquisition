#!/usr/bin/env python3
"""
Why are we seeing 222 service domains when BIAN 14 has ~330?

Tests two hypotheses and prints the answer. Read-only, no credentials, safe to
run anywhere. Not part of the pipeline — a one-off investigation.

    python3 tools/diagnose_bian.py

A: we are reading one data file of several   -> probe all_objects_data_N.js
B: service domains are being misclassified   -> stereotype position matters
"""

import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

BASE = "https://bian.org/servicelandscape-14-0-0"
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
PROBE_RANGE = range(0, 25)


def get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(text):
    m = re.match(r"\s*var\s+\w+\s*=\s*", text)
    if not m:
        raise ValueError("no var assignment")
    return json.loads(re.sub(r";\s*$", "", text[m.end():].strip()))


def stereotypes(entry):
    for cat in entry.get("categories", []):
        if cat.get("type") == "table":
            st = cat.get("content", {}).get("Stereotypes", {}).get("stereotype", {})
            return list(st.get("value", []))
    return []


# ---------------------------------------------------------------- hypothesis A
print("=" * 68)
print("  A. Are there other data files besides all_objects_data_16.js?")
print("=" * 68)

found = {}
for n in PROBE_RANGE:
    url = f"{BASE}/data/all_objects_data_{n}.js"
    try:
        body = get(url, timeout=45)
    except urllib.error.HTTPError as e:
        print(f"  _{n:<3} HTTP {e.code}")
        continue
    except Exception as e:
        print(f"  _{n:<3} {type(e).__name__}")
        continue
    try:
        data = parse(body)
    except Exception:
        print(f"  _{n:<3} {len(body)/1024:>8.0f} KB  (unparseable)")
        continue
    found[n] = data
    print(f"  _{n:<3} {len(body)/1024:>8.0f} KB  {len(data):>6} objects  <-- EXISTS")

if len(found) <= 1:
    print("\n  -> Only one data file. Hypothesis A is WRONG; the gap is")
    print("     classification, not coverage.")
else:
    base = set(found.get(16, {}))
    extra = set()
    for n, data in found.items():
        if n != 16:
            extra |= set(data) - base
    print(f"\n  -> {len(found)} data files exist.")
    print(f"     Objects in file 16:            {len(base)}")
    print(f"     Objects NOT in file 16:        {len(extra)}")
    if extra:
        print("     Hypothesis A is CORRECT — the harvest is incomplete.")

# ---------------------------------------------------------------- hypothesis B
print()
print("=" * 68)
print("  B. Are service domains hiding under another stereotype?")
print("=" * 68)

objects = found.get(16) or parse(get(f"{BASE}/data/all_objects_data_16.js"))

first_pos = Counter()
anywhere = 0
combos = Counter()
sd_by_type = Counter()

for oid, obj in objects.items():
    data = obj.get("data") or []
    if not data:
        continue
    sts = stereotypes(data[0])
    label = sts[0] if sts else data[0].get("type", "")
    first_pos[label] += 1
    if "ServiceDomain" in sts:
        anywhere += 1
        sd_by_type[data[0].get("type", "")] += 1
        if len(sts) > 1:
            combos[" + ".join(sts)] += 1

print(f"\n  Counted as ServiceDomain today (first stereotype): "
      f"{first_pos['ServiceDomain']}")
print(f"  Objects listing ServiceDomain ANYWHERE in stereotypes: {anywhere}")

if anywhere > first_pos["ServiceDomain"]:
    print(f"\n  -> {anywhere - first_pos['ServiceDomain']} service domains are "
          f"being misfiled because")
    print("     ServiceDomain is not first in their stereotype list.")
    print("\n  Stereotype combinations involved:")
    for combo, n in combos.most_common(15):
        print(f"    {n:>5}  {combo}")
else:
    print("\n  -> No hidden service domains. Hypothesis B is WRONG.")

print("\n  Categories that might contain service domains:")
for label in ("ServiceDomain", "SDServiceGroup", "ServiceGroup", "Capability",
              "FunctionalPattern", "Grouping"):
    print(f"    {label:<22} {first_pos.get(label, 0):>6}")

# ---------------------------------------------------------------- conclusion
print()
print("=" * 68)
print("  CONCLUSION")
print("=" * 68)
if len(found) > 1:
    print("  Fix coverage: harvest every all_objects_data_N.js, not just 16.")
elif anywhere > first_pos["ServiceDomain"]:
    print("  Fix classification: _stereotypes() must prefer ServiceDomain when")
    print("  present, rather than taking the first entry.")
else:
    print("  Neither hypothesis explains the gap. The ~330 figure may count")
    print("  service domains across BIAN publications rather than this view,")
    print("  or some may live in a different view file. Compare a few named")
    print("  service domains from the BIAN site against the harvested output.")
print()
