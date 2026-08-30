# Data files — structure and parsing

`data/*.js` holds objects, properties, documentation and typed relations. It
holds **no geometry** — see `view-pages.md` for that.

## Parsing: multiple variables per file

These are not one JSON object per file. `all_objects_on_views.js` defines two
variables, and `json.loads` on the whole file fails with `Extra data`.

```python
import json, re

def parse_js_vars(text: str) -> dict:
    out, dec, pos = {}, json.JSONDecoder(), 0
    pat = re.compile(r"var\s+(\w+)\s*=\s*")
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        try:
            value, end = dec.raw_decode(text, m.end())
        except ValueError:
            pos = m.end()
            continue
        out[m.group(1)] = value
        pos = end
    return out
```

## Which files to read

| File | Variable(s) | Purpose |
|---|---|---|
| `all_objects_data_mapping.js` | `objectDataMapping` | objectId → shard number. **Read this first**: the distinct values are the shards that exist. |
| `all_objects_data_N.js` | `objectData` | the objects themselves, sharded |
| `all_objects_relations.js` | `objectRelations` | typed edges |
| `all_objects_on_views.js` | `objectsOnViews`, `insiteViews` | diagram membership, and every diagram's name |

Merge the shards with first occurrence winning; objects repeat across shards
with identical payloads. **Do not guess the shard count** — derive it from the
mapping file. Reading one shard yields a plausible-looking fraction of the
model and nothing complains.

## Object payload

```json
"34300": {
  "id": 34300,
  "data": [{
    "lang": "en",
    "name": "Consumer Loan",
    "type": "Capability",
    "categories": [
      {"type": "documentation", "title": "1. Role Definition",
       "content": {"type": "rtf", "value": "<p>HTML fragment</p>"}},
      {"type": "table", "content": {
         "Stereotypes": {"stereotype": {"type": "collection",
                                        "value": ["ServiceDomain"]}},
         "Service Domain": {
           "API BIAN Portal": {"type": "link", "value": {
              "title": "...", "location": "https://portal.bian.org/..."}},
           "Scenarios": {"type": "collection", "value": [
              {"type": "object", "value": {"name": "...", "id": 54728}}]}
         }}}
    ]}]
}
```

**`typeIconPath` sits on the wrapper**, beside `"data"` — not inside `data[0]`
with `name` and `type`. Its shape is `data/icons/<Notation>/<Type>.png`, and
the notation is the segment after `icons`. Present on every object; absent only
if you look in the wrong place.

**Category = first stereotype**, falling back to `type` when absent — and a
**blank stereotype value counts as absent**, or the category comes out empty,
matches no allowlist, and the object silently disappears.

### Defensive access is required

Across the shards the shape is not uniform: fields that are dicts for most
objects are occasionally bare strings, and `data` is sometimes not a list. A
single unguarded `.get()` aborts a six-figure-object harvest near the end.
Coerce everything, and **skip-and-count** objects that still fail rather than
raising.

### Documentation sections

Service domains carry `1. Role Definition`, `2. Example of Use`,
`3. Executive Summary` and `4. Key Features`. The values are HTML fragments:
unescape entities, convert `<p>` and `<br>` to newlines, strip the remaining
tags.

### Names can contain newlines

At least one work-package name does. **Collapse whitespace** before rendering a
name into a bullet, a PlantUML string or a filename — an unexpected newline
splits a list item in two, and inside a PlantUML quoted string it ends the
statement.

## Relations

Thirty-eight verbs exist. The ones worth handling:

| Verb | Meaning |
|---|---|
| `general` / `specific` | UML generalization |
| `member end` | UML association end, Class→Class. **Not** attribute ownership |
| `is refinement of` / `is refined in` | layer bridge: Class→Business object |
| `is equal to` | ServiceDomain ↔ Class — the main semantic join |
| `realized by` / `realizes` | ServiceDomain→ServiceGroup |
| `aggregated by` / `composed of` / `is part of` | containment |
| `serves`, `triggers`, `accesses` | ArchiMate behaviour |
| `client` / `supplier` | UML dependency |
| `<unknown role>` | noise — skip |

Only a minority of objects have any relations at all. Attribute ownership is
**not** in here; it is geometric.

## Allowlist

Wanted categories, rather than an exclusion list — well over a hundred
categories exist and new ones appear between versions.

**The list itself is `INCLUDE_CATEGORIES` in `bianlib/landscape.py`, and the
filter is `is_wanted()`, which also drops anything whose category or name ends
in " relation". Read them there, and import them rather than copying them.**

A transcription of that list used to sit here. It was six categories short —
including `Service Domain`, the spaced spelling the paragraph below warns
about — and a tool built from it under-reported the wanted-object count while
looking entirely self-consistent. A constant the pipeline defines is a
measurement: name where it lives, never restate it.

`Business function` and `Work package` are deliberately absent and would be
added if ArchiMate views are ever rendered.

**Normalise before matching, and union every match.** The service domain
category is spelled both with and without a space, and a substring test finds
only one of them — which once produced a 0% join that looked like catastrophic
upstream drift.

The largest excluded categories are ArchiMate relation objects and UML
modelling furniture — realization, composition, refinement and aggregation
relations, attributes, execution specifications, enumeration literals,
messages, operations and classes. **Relation objects should be excluded
outright:** they carry no documentation, and their edges already render inline
on each real object.
