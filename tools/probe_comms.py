#!/usr/bin/env python3
"""
Stage 1c: can a message be tied to its sender and receiver?

A communication diagram discards the timeline and keeps the graph — who
exchanges what with whom. Ordering, which we established is unrecoverable, is
exactly what it drops. But it still needs PARTICIPANTS per message, and that is
a different missing link.

The suspected chain, from the earlier probes:

    Message  --?-->  Execution specification
             --message end-->  Execution specification   (9,564 pairs)
    Execution specification  --message end-->  Line      (334 pairs)
    Line     --?-->  Lifeline
    Lifeline --is associated with-->  ServiceDomain / Class
    Element  .represents  ->  {name, id}

The gap is in the middle: which lifeline does an execution specification sit
on? Rather than guess, this dumps ONE small sequence diagram completely —
every member, every relation in both directions — so the answer is read off
rather than inferred.

    python3 tools/probe_comms.py

Read-only, no credentials.
"""

import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict

BASE = "https://bian.org/servicelandscape-14-0-0"
UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"

# '4 - Customer log-in' — 26 members, the smallest useful sequence diagram.
PREFERRED_VIEW = "41813"
MAX_MEMBERS = 40


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


def one(t):
    return next(iter(parse_all(t).values()))


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


def fields(e):
    out = {}
    for c in l(e.get("categories")):
        if not isinstance(c, dict) or c.get("type") == "documentation":
            continue
        for g, f in d(c.get("content")).items():
            if isinstance(f, dict):
                for k, v in f.items():
                    out[f"{g}/{k}" if g else k] = v
            else:
                out[g] = f
    return out


def represents(e):
    """Element objects carry `represents` -> the model object they depict."""
    for k, v in fields(e).items():
        if k.lower().endswith("represents") and isinstance(v, dict):
            val = d(v.get("value"))
            if val:
                return str(val.get("id", "")), val.get("name", "")
    return None, None


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

cat_of, name_of, ent = {}, {}, {}
by_cat = defaultdict(list)
for oid, obj in objects.items():
    e = first(obj)
    ent[oid] = e
    c = cat(e)
    cat_of[oid] = c
    nm = e.get("name")
    name_of[oid] = re.sub(r"\s+", " ", nm).strip() if isinstance(nm, str) else ""
    by_cat[c].append(oid)

inv = defaultdict(list)
for oid, views in on_views.items():
    for v in l(views):
        inv[str(v)].append(oid)

# Reverse relation index: who points AT this object, and with what verb.
incoming = defaultdict(list)
for src, rels in relations.items():
    for r in l(rels):
        if not isinstance(r, dict):
            continue
        via = r.get("via")
        for t in l(r.get("to")):
            incoming[str(t)].append((src, via))


def hdr(t):
    print("\n" + "=" * 74 + f"\n  {t}\n" + "=" * 74, flush=True)


def label(oid):
    return f"{cat_of.get(oid, '?')}:{name_of.get(oid, '')[:36]!r}"


# ------------------------------------------------------- choose a diagram
view = PREFERRED_VIEW
if view not in inv or not inv[view]:
    cands = [(len(inv.get(v, [])), v) for v in by_cat.get("Sequence diagram", [])
             if 8 <= len(inv.get(v, [])) <= MAX_MEMBERS]
    view = min(cands)[1] if cands else None

if not view:
    print("No suitable sequence diagram found.")
    sys.exit(1)

members = sorted(inv[view], key=lambda x: int(x) if x.isdigit() else 0)
hdr(f"FULL DUMP — {name_of.get(view)!r} (id {view}, {len(members)} members)")
print(f"\n  composition: {dict(Counter(cat_of.get(m, '?') for m in members))}\n",
      flush=True)

for m in members:
    e = ent.get(m, {})
    print(f"  [{cat_of.get(m, '?')}] id={m} name={name_of.get(m, '')[:56]!r}",
          flush=True)
    f = fields(e)
    for k, v in f.items():
        print(f"        field {k}: {str(v)[:110]}", flush=True)
    rid, rname = represents(e)
    if rid:
        print(f"        REPRESENTS -> {rid} "
              f"({cat_of.get(rid, '?')}: {rname!r})", flush=True)
    for r in l(relations.get(m)):
        if isinstance(r, dict):
            tg = [label(str(t)) for t in l(r.get("to"))[:6]]
            print(f"        out --{r.get('via')}--> {tg}", flush=True)
    inc = incoming.get(m, [])
    if inc:
        seen = [f"{label(s)} via {v!r}" for s, v in inc[:6]]
        print(f"        in  <-- {seen}", flush=True)
    print("", flush=True)


# ------------------------------------------------- can we reach a participant?
hdr("CAN A MESSAGE REACH A PARTICIPANT?")

msgs = [m for m in members if cat_of.get(m) == "Message"]
lifelines = [m for m in members if cat_of.get(m) == "Lifeline"]
print(f"\n  {len(msgs)} messages, {len(lifelines)} lifelines\n", flush=True)

print("  Lifelines and what they resolve to:", flush=True)
for ll in lifelines:
    tg = []
    for r in l(relations.get(ll)):
        if isinstance(r, dict):
            tg += [label(str(t)) for t in l(r.get("to"))]
    print(f"    {ll} {name_of.get(ll, '')[:34]!r} -> {tg or 'NOTHING'}",
          flush=True)


def walk(start, depth=3):
    """Breadth-first over relations in both directions, recording the path."""
    seen, frontier, paths = {start}, [start], {start: []}
    for _ in range(depth):
        nxt = []
        for node in frontier:
            edges = []
            for r in l(relations.get(node)):
                if isinstance(r, dict):
                    for t in l(r.get("to")):
                        edges.append((str(t), f"--{r.get('via')}->"))
            for s, via in incoming.get(node, []):
                edges.append((str(s), f"<-{via}--"))
            for nb, via in edges:
                if nb not in seen:
                    seen.add(nb)
                    paths[nb] = paths[node] + [(via, nb)]
                    nxt.append(nb)
        frontier = nxt
    return paths


print("\n  Walking outward from each message (3 hops), looking for a Lifeline,",
      flush=True)
print("  ServiceDomain or Class:\n", flush=True)

reached = 0
for m in msgs:
    paths = walk(m)
    hits = [(n, p) for n, p in paths.items()
            if cat_of.get(n) in ("Lifeline", "ServiceDomain", "Class")]
    if not hits:
        print(f"    msg {m} {name_of.get(m, '')[:40]!r}: NO participant reachable",
              flush=True)
        continue
    reached += 1
    print(f"    msg {m} {name_of.get(m, '')[:40]!r}:", flush=True)
    for n, p in hits[:4]:
        trail = " ".join(f"{via} {cat_of.get(nid, '?')}" for via, nid in p)
        print(f"        {trail}  =>  {label(n)}", flush=True)

hdr("VERDICT")
print(f"""
  {reached} of {len(msgs)} messages could reach a participant within 3 hops.

  If that is {len(msgs)} of {len(msgs)}, communication diagrams are viable:
  participants and edges are both derivable, and ordering is not needed.

  If it is 0, the message graph is disconnected from the lifelines and
  sequence content should be dropped entirely rather than guessed at.

  Anything in between means partial coverage — read the paths above and
  decide whether the reachable subset is worth generating.
""", flush=True)
