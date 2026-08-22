#!/usr/bin/env python3
"""
Stage 1: can we generate PlantUML from the BIAN landscape?

Answers four questions before any generator is written:

  Q1  Can a Class be resolved to its Attributes and Operations?
  Q2  Is Message ORDER recoverable for sequence diagrams?
      (the one genuine unknown — wrong order is worse than no diagram)
  Q3  What do diagram/view objects look like, and what do they contain?
  Q4  Which relation verbs connect what, so they can be mapped to
      PlantUML arrows?

Prints structure: field names, types, categories, relation verbs, and values
truncated to 80 characters. That is more than the harvest logs, and it is
deliberate — you cannot design a generator without seeing the shape. It is
still counts and field names rather than documentation prose.

    python3 tools/probe_uml.py

Read-only, no credentials. Delete once the generator exists.
"""

import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict

BASE = "https://bian.org/servicelandscape-14-0-0"
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"
TRUNC = 80

# Types whose internal shape decides whether PlantUML generation is viable.
TARGETS = ["Class", "Attribute", "Operation", "Enumeration",
           "Enumeration literal", "Message", "Interaction", "Lifeline",
           "Execution specification", "Fragment", "Interaction operand",
           "Sequence diagram", "Class diagram", "Association",
           "Generalization", "ServiceDomain"]

ORDER_HINTS = ("order", "index", "seq", "position", "rank", "number",
               "sortkey", "sort", "step", "nr")


def get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(text):
    m = re.match(r"\s*var\s+\w+\s*=\s*", text)
    if not m:
        raise ValueError("no var assignment")
    return json.loads(re.sub(r";\s*$", "", text[m.end():].strip()))


def d(x):
    return x if isinstance(x, dict) else {}


def l(x):
    return x if isinstance(x, list) else []


def first(obj):
    data = l(d(obj).get("data"))
    return d(data[0]) if data and isinstance(data[0], dict) else {}


def category(entry):
    for c in l(entry.get("categories")):
        if isinstance(c, dict) and c.get("type") == "table":
            st = d(d(d(c.get("content")).get("Stereotypes")).get("stereotype"))
            vals = [v for v in l(st.get("value")) if isinstance(v, str)]
            if vals:
                return vals[0]
    t = entry.get("type")
    return t if isinstance(t, str) else "?"


def short(v):
    if isinstance(v, str):
        s = re.sub(r"\s+", " ", v).strip()
        return f'"{s[:TRUNC]}{"..." if len(s) > TRUNC else ""}"'
    if isinstance(v, (int, float, bool)) or v is None:
        return repr(v)
    if isinstance(v, list):
        return f"[{len(v)} items] " + (short(v[0]) if v else "")
    if isinstance(v, dict):
        return "{" + ", ".join(sorted(v)[:6]) + ("..." if len(v) > 6 else "") + "}"
    return type(v).__name__


def dump(entry, indent="      "):
    """Print an object's shape without dumping documentation prose."""
    for k, v in entry.items():
        if k == "categories":
            continue
        print(f"{indent}{k}: {short(v)}", flush=True)
    for c in l(entry.get("categories")):
        if not isinstance(c, dict):
            print(f"{indent}category: (non-dict: {type(c).__name__})", flush=True)
            continue
        ctype, title = c.get("type"), c.get("title")
        content = c.get("content")
        if ctype == "documentation":
            n = len(d(content).get("value", "") or "") if isinstance(content, dict) else len(str(content))
            print(f"{indent}[doc] {title!r}: {n} chars", flush=True)
            continue
        print(f"{indent}[{ctype}] {title!r}:", flush=True)
        for group, fields in d(content).items():
            if isinstance(fields, dict):
                for fk, fv in fields.items():
                    print(f"{indent}    {group} / {fk}: {short(fv)}", flush=True)
            else:
                print(f"{indent}    {group}: {short(fields)}", flush=True)


# ---------------------------------------------------------------- load
print("downloading model...", flush=True)
mapping = parse(get(f"{BASE}/data/all_objects_data_mapping.js"))
relations = parse(get(f"{BASE}/data/all_objects_relations.js"))
on_views = parse(get(f"{BASE}/data/all_objects_on_views.js"))

shard_nums = sorted({int(v) for v in mapping.values()})
objects = {}
for n in range(min(shard_nums), max(shard_nums) + 1):
    try:
        for oid, obj in parse(get(f"{BASE}/data/all_objects_data_{n}.js")).items():
            objects.setdefault(oid, obj)
    except Exception as e:
        print(f"  shard {n}: {type(e).__name__}", flush=True)
print(f"  {len(objects)} objects, {len(relations)} with relations, "
      f"{len(on_views)} with view membership\n", flush=True)

cat_of, name_of = {}, {}
by_cat = defaultdict(list)
for oid, obj in objects.items():
    e = first(obj)
    c = category(e)
    cat_of[oid] = c
    nm = e.get("name")
    name_of[oid] = nm if isinstance(nm, str) else ""
    by_cat[c].append(oid)


def hdr(t):
    print("\n" + "=" * 72)
    print(f"  {t}")
    print("=" * 72, flush=True)


# ------------------------------------------------------- Q1/Q2/Q3 shapes
hdr("STRUCTURE OF EACH TYPE  (1 sample each)")
for cat in TARGETS:
    ids = by_cat.get(cat, [])
    print(f"\n--- {cat}  ({len(ids)} objects) ---", flush=True)
    if not ids:
        print("      none found", flush=True)
        continue
    oid = ids[0]
    print(f"      id={oid} name={name_of[oid]!r}", flush=True)
    dump(first(objects[oid]))
    rels = [r for r in l(relations.get(oid)) if isinstance(r, dict)]
    if rels:
        print(f"      relations ({len(rels)}):", flush=True)
        for r in rels[:8]:
            tgts = l(r.get("to"))[:4]
            desc = ", ".join(f"{cat_of.get(str(t), '?')}:{name_of.get(str(t), '')[:24]}"
                             for t in tgts)
            print(f"        {r.get('via')!r} -> {desc}"
                  f"{' ...' if len(l(r.get('to'))) > 4 else ''}", flush=True)
    views = l(on_views.get(oid))
    if views:
        print(f"      appears on {len(views)} views: "
              f"{[cat_of.get(str(v), '?') for v in views[:6]]}", flush=True)


# --------------------------------------------------------------- Q2 order
hdr("Q2  IS MESSAGE ORDER RECOVERABLE?")
inv = defaultdict(list)
for oid, views in on_views.items():
    for v in l(views):
        inv[str(v)].append(oid)

seq_views = by_cat.get("Sequence diagram", []) + by_cat.get("Interaction", [])
print(f"\n  {len(by_cat.get('Sequence diagram', []))} sequence diagrams, "
      f"{len(by_cat.get('Interaction', []))} interactions", flush=True)

target = None
for v in seq_views:
    members = inv.get(v, [])
    if sum(1 for m in members if cat_of.get(m) == "Message") >= 3:
        target = v
        break

if not target:
    print("\n  No sequence view with 3+ messages found via objectsOnViews.")
    print("  -> membership may not be recorded for sequence content;")
    print("     Stage 3 would need another route.", flush=True)
else:
    members = inv[target]
    print(f"\n  sample view id={target} name={name_of.get(target)!r} "
          f"cat={cat_of.get(target)}", flush=True)
    print(f"  {len(members)} members: "
          f"{dict(Counter(cat_of.get(m, '?') for m in members))}", flush=True)
    msgs = [m for m in members if cat_of.get(m) == "Message"]
    print(f"\n  first {min(6, len(msgs))} messages, id order vs any order field:",
          flush=True)
    for m in sorted(msgs, key=lambda x: int(x) if x.isdigit() else 0)[:6]:
        e = first(objects[m])
        fields = {}
        for c in l(e.get("categories")):
            for group, f in d(d(c).get("content")).items():
                if isinstance(f, dict):
                    for fk, fv in f.items():
                        if any(h in fk.lower().replace(" ", "") for h in ORDER_HINTS):
                            fields[fk] = short(fv)
        print(f"    id={m} name={name_of[m][:40]!r} order-ish={fields or 'NONE'}",
              flush=True)

    hits = Counter()
    for m in msgs:
        for c in l(first(objects[m]).get("categories")):
            for group, f in d(d(c).get("content")).items():
                if isinstance(f, dict):
                    for fk in f:
                        if any(h in fk.lower().replace(" ", "") for h in ORDER_HINTS):
                            hits[fk] += 1
    print(f"\n  order-looking fields across all {len(msgs)} messages: "
          f"{dict(hits) or 'NONE FOUND'}", flush=True)
    if not hits:
        print("  -> no explicit ordering field. Order would have to come from")
        print("     id sequence (risky) or Execution specification links.",
              flush=True)


# ------------------------------------------------------------ Q3 diagrams
hdr("Q3  WHAT DO DIAGRAMS CONTAIN?")
for cat in ("Class diagram", "Sequence diagram", "Total view",
            "Capability map view"):
    ids = by_cat.get(cat, [])
    if not ids:
        continue
    sizes = sorted((len(inv.get(i, [])) for i in ids), reverse=True)
    print(f"\n  {cat}: {len(ids)} diagrams, members "
          f"max={sizes[0] if sizes else 0} "
          f"median={sizes[len(sizes)//2] if sizes else 0}", flush=True)
    sample = max(ids, key=lambda i: len(inv.get(i, [])))
    print(f"    largest: {name_of.get(sample)!r} "
          f"({len(inv.get(sample, []))} members)", flush=True)
    print(f"    composition: "
          f"{dict(Counter(cat_of.get(m, '?') for m in inv.get(sample, [])).most_common(8))}",
          flush=True)


# --------------------------------------------------------------- Q4 verbs
hdr("Q4  RELATION VERBS -> PlantUML ARROWS")
verb_pairs = Counter()
verb_total = Counter()
for oid, rels in relations.items():
    src = cat_of.get(oid, "?")
    for r in l(rels):
        if not isinstance(r, dict):
            continue
        via = (r.get("via") or "").strip()
        if not via or via == "<unknown role>":
            continue
        verb_total[via] += 1
        for t in l(r.get("to"))[:20]:
            verb_pairs[(via, src, cat_of.get(str(t), "?"))] += 1

print(f"\n  {len(verb_total)} distinct verbs. Most frequent:\n", flush=True)
for via, n in verb_total.most_common(25):
    print(f"    {via:<26} {n:>8}", flush=True)

print("\n  Verb usage between UML types (what a class diagram needs):\n",
      flush=True)
uml = {"Class", "Attribute", "Operation", "Enumeration", "Enumeration literal",
       "Association", "Generalization", "Interaction", "Message", "Lifeline",
       "Execution specification", "Fragment", "Interaction operand"}
shown = 0
for (via, s, t), n in verb_pairs.most_common(400):
    if s in uml or t in uml:
        print(f"    {s:<24} --{via:^22}-> {t:<24} {n:>7}", flush=True)
        shown += 1
        if shown >= 30:
            break

hdr("SUMMARY")
print(f"""
  Classes                {len(by_cat.get('Class', [])):>7}
  Attributes             {len(by_cat.get('Attribute', [])):>7}
  Operations             {len(by_cat.get('Operation', [])):>7}
  Class diagrams         {len(by_cat.get('Class diagram', [])):>7}
  Sequence diagrams      {len(by_cat.get('Sequence diagram', [])):>7}
  Interactions           {len(by_cat.get('Interaction', [])):>7}
  Messages               {len(by_cat.get('Message', [])):>7}

  Read the Q1 relations above: if Class shows a verb pointing at Attribute
  objects, the data dictionary is buildable. Read Q2: if no ordering field
  exists, sequence diagrams are the risky stage.
""", flush=True)
