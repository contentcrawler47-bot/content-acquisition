---
name: bian-extraction
description: Extract content from the BIAN Service Landscape website (bian.org/servicelandscape-*) — service domains, service operations, control records, the UML data model, and sequence and class diagrams. Use this skill whenever the user mentions BIAN, the Banking Industry Architecture Network, service domains, service landscapes, InSite, or asks to scrape, harvest, crawl, or read content from bian.org, even if they do not name the site explicitly. Also use it when a task involves banking reference architecture, BIAN service operation APIs, or converting BIAN diagrams to PlantUML. It saves many hours: the landscape looks like a JavaScript app that must be browser-rendered, but is in fact static files — and several obvious-looking approaches are dead ends that this skill documents.
---

<!-- skill: bian-extraction v18 | repo: changeset 065 -->

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
└── data/
    ├── all_objects_data_mapping.js  objectDataMapping = {objectId: shardNumber}
    ├── all_objects_data_N.js        objectData = {objectId: {...}}, many shards
    ├── all_objects_relations.js     objectRelations = {objectId: [{via, to:[ids]}]}
    ├── all_objects_on_views.js      objectsOnViews = {objectId: [viewIds]}
    │                                insiteViews    = {viewId: {name, ...}}
    ├── models_data.js               insite_models = [{name, views:[{id, title}]}]
    ├── view_NNNNN_data.js           one view: typeIconPath, objectReferences,
    │                                viewpointsData, vp_legends
    └── config_data.js               languages
```

**Everything but the view pages is under `data/`, including the per-view file.**
This file said `views/view_NNNNN_data.js` for three versions — it is loaded by a
page in `views/`, so the sibling path looks right — and the one function that
built that URL sits off the bulk path behind a bare `except`, so it never
returned anything but an empty string and no run ever disagreed. **A path no
request has ever succeeded on is a guess, however many places repeat it.**

`models_data.js` groups most views into named models, and the model name is the
nearest thing the landscape publishes to a statement of what a view is *for* —
`insiteViews` gives a view a name but never a purpose. The views it omits appear
to be exactly those that are not objects in the model, which is worth
re-checking rather than assuming. Counts are in `REFERENCE-DATA.md`.

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

**Notation is a separate axis from category, and it is on the object wrapper.**
`typeIconPath` sits beside `data`, not inside `data[0]`, and its shape is
`data/icons/<Notation>/<Type>.png` — so the notation is the path segment after
`icons`, read structurally rather than by matching substrings of the filename.
Looked for one level too deep it is simply absent, and a run once resolved it
for none of the objects while reporting nothing wrong.

Three notations exist: UML, ArchiMate and a model-package notation nothing had
noticed. **ArchiMate is not a synonym for furniture** — several of the most
wanted BIAN categories are ArchiMate-notated and already harvested, while a
category can span more than one notation. So a selective extraction keys on
notation **and** category, and a renderer reads notation per object rather than
inferring it from the category.

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
good size estimate before fetching.

**A view's members are not all nodes.** `objectsOnViews` lists everything drawn
on the view, relations included — on one sampled view, 27 of 48 members are
relation objects. It is the right weight for balancing work, because cost
tracks everything drawn, but anything reading it as "objects placed on the
view" is wrong by up to half. Membership can also name a *view*: a diagram
drawn on another diagram, which resolves as an object only when that view is
itself an object in the model. Then fetch `views/view_<id>.html` and parse
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

### ArchiMate views

Everything above is UML. ArchiMate views differ in ways that each cost a run:

- **The concept is a shape, not a type.** Hundreds of service domains are drawn
  as a capability's shape. `bizzconcept` says how a thing was depicted;
  **`bizzsemantic` into the model says what it is**. Anything classifying from
  the concept mislabels at scale.
- **Nodes carry no `<rect>`.** They are rounded-rectangle `<path>` outlines, so
  a rect-matching parser finds nothing at all on some view types, and a path's
  first and last point are nearly identical on a closed outline. Walk the
  command list for a bounding box, with `<rect>` as a fast path.
- **A junction is a node, not a relationship.** Relation blocks are named
  `<Source><Target><RelationType>`, so recognising one by suffix also catches
  the bare element `OrJunction`. Require the concept to be strictly longer than
  the relation type.
- **Containment is derivable** by smallest enclosing box, and exact — grouping
  boxes nest several deep, and that nesting is what a Total view is *for*.
- Pages also draw their own furniture — hyperlinks, charts, interface controls
  — carrying no `bizzsemantic`, or one that is in no shard. Count them, do not
  assume every drawn element is model content.

### Two page-reading traps

**`objectReferences` equals membership on ArchiMate views, not on UML ones.** A
class diagram carries more references than members. Do not generalise from one
view type.

**Distinct `bizzsemantic` is not a member count.** It over-counts substantially,
because each element is drawn with its `is equal to` UML twin nested behind it —
the semantic-to-UML bridge, present on the page as well as in the relation
graph.

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
## The source input gate

`bianlib/gate.py` observes what BIAN publishes against a declaration of what
the parser handles, and reports what is present and unconsumed. It runs inside
every extract, in either mode, and its result rides in the extract at
`status.gate`. The **design rationale, principles and general lessons are in
`GATE-DESIGN.md` on Drive and in the `content-acquisition` skill** — they are
not BIAN-specific and are not repeated here.

What the gate settled about v14, each measured rather than argued:

- **One language.** `config_data.js` declares one, and all 128,270 objects carry
  exactly one `data` entry. Reading only `data[0]` drops nothing *today*; a
  second published language would be silent loss, which is why the check stays.
- **One table category per object**, so `_properties` returning on the first is
  not a tail-drop. 42,861 table categories across the corpus.
- **Every table category carries a `title` that nothing reads** — a real and
  universal drop, kept because the titles have not been shown to add anything
  to the group names.
- **No third `categories[]` type, no undeclared wrapper or entry key, no
  unhandled property discriminator, no non-string relation target, no empty
  relation verb.** All measured zero.
- **All 47 shards read, and every id in `objectDataMapping` has an object.**
  1,359 objects exist that the mapping does not name — surplus, not loss.
- **`all_objects_on_views.js` is complete.** Every object a view's own
  `objectData` claims is recorded as a member of it.
- **Ten documentation values clean to nothing**, and all ten are `<span>`/`<p>`
  wrappers containing only `&nbsp;` — tags `p`, `span` and one `b` across all
  ten, no `img`, `table`, `li` or `a`. Empty markup, not discarded content.

## The per-view data file, and its two traps

`data/view_<id>_data.js` declares **seven** variables — `objectData`,
`objectReferences`, `objectRelations`, `viewData`, `viewReferences`,
`viewpointsData`, `vp_legends` — so parse **every** assignment. Reading the
first one reported an inventory of two keys and a viewpoint count of zero that
was really NOT MEASURED.

**`objectReferences` maps diagram element id to object id.** The keys are a
per-diagram presentation namespace: 504 of 530 resolve to nothing, and every
one of the 530 *values* resolves to an object we hold. Checking the keys
produced a permanently red finding about the wrong side of a mapping.

`viewpointsData` and `vp_legends` are present and **empty** in every sampled
view. That is the measured answer to the ArchiMate viewpoint question — the
earlier flat claim of zero came from thirty fetches that had all failed on a
wrong path.

## Two containment traps in the model

**A view's id is its own diagram object's id**, so the object appears in its
own view's file. It is the container, not a member.

**A view `is refinement of` the object it depicts** — its subject, also not a
member, and properly a member of some other view. That verb appears 6,860
times and only 382 of its sources are views, so any rule about it must be
narrowed to edges whose source is the view in question.

## Documentation HTML

`clean_html` separates paragraph, list, table, heading and definition
boundaries; every other tag is deleted without a separator. Before changeset
059 the block tags were deleted too, fusing list items and table cells —
`<li>A</li><li>B</li>` became `AB` — across 133 values, 36 of them published.

Some BIAN markup puts quotes **outside** the list item, so breaking at every
boundary strands a bare `"` on its own line; the cleaner folds a
punctuation-only segment back into its neighbour, but only at boundaries it
inserted itself. BIAN also uses a row of dots and a lone full stop as
deliberate separators on their own lines, which must survive untouched.

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
