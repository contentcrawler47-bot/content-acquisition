---
name: bian-extraction
description: Extract content from the BIAN Service Landscape website (bian.org/servicelandscape-*) — service domains, service operations, control records, the UML data model, and sequence and class diagrams. Use this skill whenever the user mentions BIAN, the Banking Industry Architecture Network, service domains, service landscapes, InSite, or asks to scrape, harvest, crawl, or read content from bian.org, even if they do not name the site explicitly. Also use it when a task involves banking reference architecture, BIAN service operation APIs, or converting BIAN diagrams to PlantUML. It saves many hours: the landscape looks like a JavaScript app that must be browser-rendered, but is in fact static files — and several obvious-looking approaches are dead ends that this skill documents.
---

<!-- skill: bian-extraction v2 | repo: changeset 025 -->

# BIAN Service Landscape extraction

The landscape browser at `bian.org/servicelandscape-<version>/` looks like it
needs a headless browser. It does not. Everything is static files.

**Read `references/dead-ends.md` before attempting anything not described
here.** It records twelve approaches tried and refuted with evidence. They look
reasonable and cost several hours each, and five of the first ten were the same
mistake: searching the data files for information that lives in the view pages.

Counts, thresholds and canaries change between landscape versions, so this file
does not carry them. They are in `REFERENCE-DATA.md` on Drive, and the pinned
version lives in the source that uses it.

## Orientation

```
https://bian.org/servicelandscape-<version>/
├── object_16.html?object=NNNNN   JS-rendered — DO NOT fetch, returns an empty shell
├── views/view_NNNNN.html         STATIC — inline SVG with full diagram geometry
├── views/view_NNNNN_data.js      the view's name — rarely needed, see Titles
└── data/
    ├── all_objects_data_mapping.js  objectDataMapping = {objectId: shardNumber}
    ├── all_objects_data_N.js        objectData = {objectId: {...}}, many shards
    ├── all_objects_relations.js     objectRelations = {objectId: [{via, to:[ids]}]}
    ├── all_objects_on_views.js      objectsOnViews = {objectId: [viewIds]}
    │                                insiteViews    = {viewId: {name, ...}}
    └── config_data.js               languages
```

**Two independent sources, and this distinction matters more than anything
else here:**

| Source | Contains | Does NOT contain |
|---|---|---|
| `data/*.js` | objects, properties, documentation, typed relations | **any geometry** |
| `views/*.html` | full SVG geometry, diagram membership | documentation text |

Message ordering, sequence senders and receivers, and attribute ownership exist
**only** in the view geometry. Establish which artefact holds which kind of
information before probing.

## Getting the objects

1. Fetch `all_objects_data_mapping.js`. Its values are shard numbers, so
   `sorted(set(mapping.values()))` gives the shards to fetch.
2. Fetch every `all_objects_data_N.js` and merge, first occurrence winning.
   Objects repeat across shards with identical payloads.

**Reading one shard yields about 5% of the model, and the failure is silent** —
the output looks complete. It was caught only by comparing a service domain
count against a published view. Always cross-check a total against an
independent source.

Roughly a tenth of the objects are BIAN semantic content; the rest is UML and
ArchiMate modelling furniture. **Use an allowlist of wanted categories, not an
exclusion list** — well over a hundred categories exist and new junk appears
between versions.

An object's category is its **first stereotype, falling back to its UML
`type`**. Treat a blank stereotype value as absent, or the category comes out
empty and the object matches no allowlist and disappears.

`references/data-files.md` has the parser, the object payload shape, the
relation verbs and the allowlist.

## Choosing which views to fetch

Most views do not convert, and fetching them is a thousand pointless requests.
Decide before fetching — the model already knows.

**A view id is the id of its own diagram object.** Look it up in the merged
model and read that object's category: `Class diagram` and `Sequence diagram`
convert; `Total view`, `Capability map view` and a dozen other named ArchiMate
types do not; and a large minority of view ids are not objects in the model at
all.

**Do not infer the type from what a view contains.** Scoring views by their
members' categories looks sound and over-counts class views by roughly a third,
because several ArchiMate view types are built from objects indistinguishable
from UML classes. Dead end 11 has the numbers.

## Getting the diagrams

`objectsOnViews` inverted gives diagram membership, and the member count is a
good size estimate before fetching. Then fetch `views/view_<id>.html` and parse
the inline SVG, where every element carries `bizzconcept` (the UML metaclass),
`bizzid` (the diagram element) and `bizzsemantic` (the model object it
depicts).

- **Sequence:** order from message `y`; sender and receiver from `x1`/`x2`
  matched to the nearest lifeline column centre. **Bind to the lifeline's
  `bizzid`, not its label** — a diagram can show the same service domain on two
  lifelines, and a shared alias merges the columns and turns a message between
  them into a self-call.
- **Class:** attribute ownership by geometric containment — an attribute rect
  sits inside its owning box's rect.

**Attributes are not owned only by `UML_Class`.** `UML_Enumeration`,
`UML_Interface`, `UML_DataType`, `UML_Object`, `UML_Component`, `UML_Signal`
and `UML_PrimitiveType` all hold attribute rows, each needing a different
PlantUML keyword. Recognising only `UML_Class` silently drops every enumeration
literal in the landscape.

About 1% of rows still do not resolve at landscape scale. **Carry those into an
explicit "(unattached)" box** rather than counting and discarding them — they
are content BIAN published, and inventing an owner would be worse than showing
them plainly.

**Diagram names come from `insiteViews[viewId].name`.** Sequence diagrams also
carry a title in `UML_Interaction`'s label; class diagrams carry none. Taking
the name from `insiteViews` avoids a second request per class diagram, and
stops every untitled class diagram sharing a filename and overwriting the
others.

`references/view-pages.md` has the concept inventory, the extraction gotchas,
connector path handling and diagram sizing.

## Generating PlantUML that actually renders

**PlantUML draws an error image rather than refusing.** Invalid syntax produces
a file that looks fine to anything inspecting the surrounding markdown, so
nothing downstream notices. One fault of this kind broke every diagram of a
published landscape while every check passed.

- **An apostrophe only opens a comment at the start of a line.** A trailing
  `' object 34300` is a syntax error; inline comments need the paired form
  `/' object 34300 '/`.
- **Sanitise anything inside double quotes.** A quote closes the string early
  and a newline ends the statement — and BIAN names do contain newlines.
- **Validate with PlantUML itself.** Counting fenced blocks proves a block
  exists, not that it is valid. `-checkonly -failfast2` over the whole set is
  fast and returns 0 or 200; re-run a single file with `-tsvg` for
  `Error line N`.

## Sanity checks

Every harvest should assert a **canary** — a known object present with a known
name — and cross-check at least one total against an independently published
view. Both catch upstream restructuring that would otherwise thin the output
silently. The current values are in `REFERENCE-DATA.md`.

Two landscape facts that will catch out any filter or report:

- **The service domain category is spelled two ways**, spaced and unspaced. Any
  test matching one silently drops the other. Normalise — strip case and
  punctuation — then union every match.
- **Some service domains share a name with another**, so anything keyed by name
  loses the duplicates without saying so. Report objects and distinct names
  separately.

Views that yield neither messages nor classes are skips with a reason, not
failures.

## Etiquette and legal

The landscape files are served without authentication. That is not a licence to
redistribute them: keep a private working copy and check BIAN's terms before
any bulk use.

A full harvest is over a thousand requests on someone else's web server. What
makes that reasonable:

- **`Accept-Encoding: gzip`** — it cuts the shards by roughly three quarters.
  The single largest courtesy available, and it costs one header.
- **Keep-alive** — one TLS handshake per batch rather than one per page.
- **Pace the requests** — a floor of about a second, single threaded, never
  parallel.
- **Conditional GET works, but cache the *converted output* alongside the
  ETag.** A 304 has no body, so without it the page must be fetched again
  anyway — and the cache entry must carry a renderer version, or a rendering
  fix will never reach the output.
- **Back off on 429 and 5xx**, honour `Retry-After`, and stop entirely after a
  run of consecutive failures rather than pushing through a thousand of them.
- **Read `robots.txt`** before anything else. It has carried no rule against
  the landscape path, but that can change.

The version is pinned in the URL path. When BIAN publishes a new landscape,
update the base URL and re-run the sanity checks — counts and ids change
between versions.

## Reference files

- `references/dead-ends.md` — **read first**; twelve refuted approaches with evidence
- `references/data-files.md` — parser, object payload, relation verbs, allowlist
- `references/view-pages.md` — SVG concepts, geometry extraction, PlantUML output
