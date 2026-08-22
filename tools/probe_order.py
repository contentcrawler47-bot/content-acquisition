#!/usr/bin/env python3
"""
Stage 1b: two questions left over from the first probe.

  A  Do Graphical shape / Line objects carry coordinates?
     If yes, sequence order is derivable from Y position — the true answer.
     The first probe showed no ordering field exists, so this is the fallback.

  B  Does id order match geometric order?
     The sample messages read as a coherent sequence in id order, which is
     suggestive but not proof. Compared across many diagrams it becomes
     evidence — or is refuted, which is just as useful.

  C  Is attribute-to-class ownership recoverable at all?
     The first probe found no Class -> Attribute relation. This checks every
     remaining route before class diagrams are written off.

    python3 tools/probe_order.py

Read-only, no credentials.
"""

import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict

BASE = "https://bian.org/servicelandscape-14-0-0"
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"

COORD_HINTS = ("x", "y", "top", "left", "bottom", "right", "width", "height",
               "pos", "coord", "bounds", "rect", "cx", "cy")


def get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_all(text):
    out, dec, pos = {}, json.JSONDecoder(), 0
    pat = re.compile(r"var\s+(\w+)\s*=\s*")
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        try:
            v, end = dec.raw_decode(text, m.end())
        except ValueError:
            pos = m.end()
            continue
        out[m.group(1)] = v
        pos = end
    return out


def one(text):
    return next(iter(parse_all(text).values()))


def d(x):
    return x if isinstance(x, dict) else {}


def l(x):
    return x if isinstance(x, list) else []


def first(o):
    dd = l(d(o).get("data"))
    return d(dd[0]) if dd and isinstance(dd[0], dict) else {}


def cat(e):
    for c in l(e.get("categories")):
        if isinstance(c, dict) and c.get("type") == "table":
            st = d(d(d(c.get("content")).get("Stereotypes")).get("stereotype"))
            v = [x for x in l(st.get("value")) if isinstance(x, str)]
            if v:
                return v[0]
    t = e.get("type")
    return t if isinstance(t, str) else "?"


def all_fields(e):
    """Every property name/value on an object, flattened."""
    out = {}
    for c in l(e.get("categories")):
        if not isinstance(c, dict) or c.get("type") == "documentation":
            continue
        for group, fields in d(c.get("content")).items():
            if isinstance(fields, dict):
                for k, v in fields.items():
                    out[f"{group}/{k}" if group else k] = v
            else:
                out[group] = fields
    return out


print("loading...", flush=True)
relations = one(get(f"{BASE}/data/all_objects_relations.js"))
vv = parse_all(get(f"{BASE}/data/all_objects_on_views.js"))
on_views = vv.get("objectsOnViews", {})
mapping = one(get(f"{BASE}/data/all_objects_data_mapping.js"))

objects = {}
nums = sorted({int(x) for x in mapping.values()})
for n in range(min(nums), max(nums) + 1):
    try:
        for oid, obj in one(get(f"{BASE}/data/all_objects_data_{n}.js")).items():
            objects.setdefault(oid, obj)
    except Exception:
        pass
print(f"  {len(objects)} objects\n", flush=True)

cat_of, name_of, fields_of = {}, {}, {}
by_cat = defaultdict(list)
for oid, obj in objects.items():
    e = first(obj)
    c = cat(e)
    cat_of[oid] = c
    nm = e.get("name")
    name_of[oid] = nm if isinstance(nm, str) else ""
    fields_of[oid] = all_fields(e)
    by_cat[c].append(oid)

inv = defaultdict(list)
for oid, views in on_views.items():
    for v in l(views):
        inv[str(v)].append(oid)


def hdr(t):
    print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72, flush=True)


# --------------------------------------------------------------------- A
hdr("A  DO GRAPHICAL OBJECTS CARRY COORDINATES?")
for c in ("Graphical shape", "Line", "Element", "Connection"):
    ids = by_cat.get(c, [])
    print(f"\n  {c}: {len(ids)} objects", flush=True)
    if not ids:
        continue
    for oid in ids[:2]:
        f = fields_of[oid]
        print(f"    id={oid} name={name_of[oid][:40]!r} fields={len(f)}",
              flush=True)
        for k, v in list(f.items())[:14]:
            print(f"      {k}: {str(v)[:70]}", flush=True)
    hits = Counter()
    for oid in ids[:400]:
        for k in fields_of[oid]:
            kl = k.lower().split("/")[-1].strip()
            if kl in COORD_HINTS or any(kl.startswith(h) for h in ("x", "y")):
                hits[k] += 1
    print(f"    coordinate-looking fields: {dict(hits) or 'NONE'}", flush=True)


# --------------------------------------------------------------------- B
hdr("B  DOES ID ORDER MATCH ANYTHING GEOMETRIC?")
seq = [v for v in by_cat.get("Sequence diagram", [])
       if sum(1 for m in inv.get(v, []) if cat_of.get(m) == "Message") >= 4]
print(f"\n  {len(seq)} sequence diagrams with 4+ messages\n", flush=True)

for v in seq[:4]:
    members = inv.get(v, [])
    msgs = sorted((m for m in members if cat_of.get(m) == "Message"),
                  key=lambda x: int(x) if x.isdigit() else 0)
    print(f"  --- {name_of.get(v)!r} ({len(msgs)} messages) ---", flush=True)
    for m in msgs:
        nm = re.sub(r"\s+", " ", name_of[m])[:62]
        print(f"    {m}  {nm}", flush=True)
    print("", flush=True)

print("  Read those: if each list reads as a coherent left-to-right",
      flush=True)
print("  conversation, id order is a usable proxy for sequence order.",
      flush=True)

print("\n  Message -> Execution specification wiring (the send/receive ends):",
      flush=True)
if seq:
    sample = seq[0]
    for m in sorted((x for x in inv[sample] if cat_of.get(x) == "Message"),
                    key=lambda x: int(x))[:4]:
        rels = [r for r in l(relations.get(m)) if isinstance(r, dict)]
        print(f"    msg {m}: "
              f"{[(r.get('via'), [cat_of.get(str(t), '?') for t in l(r.get('to'))[:3]]) for r in rels]}",
              flush=True)


# --------------------------------------------------------------------- C
hdr("C  CAN AN ATTRIBUTE BE TIED TO ITS CLASS?")
attrs = by_cat.get("Attribute", [])
with_rel = [a for a in attrs if relations.get(a)]
print(f"\n  {len(attrs)} attributes, {len(with_rel)} have ANY relation",
      flush=True)

if with_rel:
    print("\n  sample attribute relations:", flush=True)
    for a in with_rel[:6]:
        rels = [r for r in l(relations.get(a)) if isinstance(r, dict)]
        for r in rels[:3]:
            tg = [f"{cat_of.get(str(t), '?')}:{name_of.get(str(t), '')[:24]}"
                  for t in l(r.get("to"))[:3]]
            print(f"    {name_of[a][:28]!r} --{r.get('via')}-> {tg}", flush=True)

print("\n  Do any relations anywhere point AT an Attribute?", flush=True)
pointing = Counter()
for oid, rels in relations.items():
    for r in l(rels):
        if not isinstance(r, dict):
            continue
        for t in l(r.get("to")):
            if cat_of.get(str(t)) == "Attribute":
                pointing[(cat_of.get(oid, "?"), r.get("via"))] += 1
print(f"    {dict(pointing.most_common(10)) or 'NONE — ownership not in the graph'}",
      flush=True)

print("\n  Attribute property fields (is the owner named in a property?):",
      flush=True)
fc = Counter()
for a in attrs[:600]:
    for k in fields_of[a]:
        fc[k] += 1
for k, n in fc.most_common(12):
    print(f"    {k:<44} {n:>5}", flush=True)

print("\n  Are attribute ids contiguous with their class's id?", flush=True)
cd = [v for v in by_cat.get("Class diagram", [])
      if 4 <= len(inv.get(v, [])) <= 60]
if cd:
    v = cd[0]
    members = sorted(inv[v], key=lambda x: int(x) if x.isdigit() else 0)
    print(f"    diagram {name_of.get(v)!r}, members in id order:", flush=True)
    for m in members[:30]:
        print(f"      {m:>8}  {cat_of.get(m, '?'):<22} {name_of.get(m, '')[:38]}",
              flush=True)
    print("\n    If Attributes follow their Class in id order, ownership is",
          flush=True)
    print("    recoverable by grouping on id runs.", flush=True)
