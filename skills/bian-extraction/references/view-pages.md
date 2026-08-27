# View pages — geometry and PlantUML generation

`views/view_<id>.html` is **static HTML with inline SVG**. Unlike
`object_N.html` it needs no JavaScript.

This is the only place message ordering, sequence senders and receivers, and
attribute ownership exist. They are not in `data/*.js`, and looking for them
there accounts for half the recorded dead ends.

## Tagged elements

| Attribute | Meaning |
|---|---|
| `bizzconcept` | UML metaclass |
| `bizzid` | diagram element id |
| `bizzsemantic` | the model object it depicts |

```html
<g bizzid="103876" bizzsemantic="103877" bizzconcept="UML_Message" ...>
  <path d="M 406.00 420.00 L 736.00 420.00"/>
```

Labels are separate groups named `label<bizzid>` containing `<text>` nodes.

**Extraction gotcha:** the licence comment near the top of the file contains a
literal `<svg></svg>`. Anchor on `<svg version=` and search for `</svg>` from
that offset, or the slice comes back empty.

## Concepts by diagram type

| Diagram | Concepts |
|---|---|
| Sequence | `UML_Interaction`, `UML_Lifeline`, `UML_LifelineElement`, `UML_LifelineLine`, `UML_ExecutionSpecification`, `UML_Message`, `UML_CombinedFragment` |
| Class | `UML_Class`, `UML_Enumeration`, `UML_Interface`, `UML_DataType`, `UML_Attribute`, `UML_Association`, `UML_Generalization`, `ViewGraphic`, `ViewHyperlink` |
| ArchiMate | a different set entirely, not examined |

## Sequence diagrams

- **Participants:** `UML_LifelineElement`. The column centre is `translate(x,y)`
  plus half the rect width. Sort by x.
- **Order:** message `y`, ascending. This is the only source of ordering — see
  dead ends 3, 4 and 5.
- **Sender and receiver:** `x1` and `x2` matched to the nearest lifeline column
  centre. Direction is `x1 → x2`.

**Bind messages to the lifeline's `bizzid`, not its label.** A diagram can show
the same service domain on two lifelines. Deriving aliases from names merges
the two columns into one and turns a message between them into a self-call.

## Class diagrams

**Attribute ownership is geometric containment** — each attribute rect sits
inside its container's rect. The elements are DOM-nested too, so either works.

**Containers are not only `UML_Class`.** `UML_Enumeration`, `UML_Interface`,
`UML_DataType`, `UML_Object`, `UML_Component`, `UML_Signal` and
`UML_PrimitiveType` all hold attribute rows, and each needs a different
PlantUML keyword. Recognising only `UML_Class` silently drops every enumeration
literal in the landscape.

This was dead end 12, and it is worth understanding why it survived review: the
`UML_Class`-only version was validated on a sample of 147 attributes across 12
classes with zero unassignable. The sample contained no enumerations.

**About 1% of rows still do not resolve** at landscape scale even with every
container recognised. Carry them into an explicit "(unattached attributes)"
box. Never count and discard them: they are content BIAN published, and
inventing an owner would be worse than showing them plainly.

Attribute labels carry types — `Interest Rate : Rate` — so the output is a real
data dictionary rather than a list of names.

## Connector paths are multi-segment

```
M 5180 2620 L 4100 2639.64 Q 4080 2640 4080 2620 L 4080 2360
```

Taking the first `L` lands mid-route. Keep the **first and last** points.

## Sizing before fetching

`objectsOnViews`, inverted, gives each view's member count without fetching it.
Use it to balance chunked harvests by **member count, not view count** — the
largest class diagrams carry two orders of magnitude more members than the
smallest, and a chunk balanced by view count is wildly uneven in cost.

Beyond roughly 50 nodes a generated diagram is unreadable. Degrade large
diagrams to a structured table rather than emitting something nobody can read.

## Titles

Sequence diagrams carry a title in `UML_Interaction`'s label. **Class diagrams
do not.**

Take the name from `insiteViews[viewId].name`, which is already downloaded.
`views/view_<id>_data.js` holds the same name but costs a second request per
class diagram — hundreds of avoidable requests. Falling back to a literal makes
every untitled class diagram share a filename and silently overwrite the
others.

## PlantUML that actually renders

**PlantUML draws an error image rather than refusing.** Invalid syntax produces
a file that looks fine to anything inspecting the surrounding markdown. One
fault of this kind broke every diagram of a published landscape while every
check passed.

- **An apostrophe only opens a comment at the start of a line.** A trailing
  `' object 34300` is a syntax error; inline comments need `/' object 34300 '/`.
- **Sanitise anything placed inside double quotes.** A quote closes the string
  early, a newline ends the statement, and BIAN names do contain newlines.
- **Validate with PlantUML itself.** `-checkonly -failfast2` over the whole set
  checks roughly 30 diagrams a second and returns 0 or 200 for the batch;
  re-run a single file with `-tsvg` to get `Error line N` when you need to know
  which one.

## Combined fragments

`UML_CombinedFragment` carries meaningful labels — "For Each Savings Account",
"If Interest Accrual". Rendering them as PlantUML `group`/`alt` blocks needs
the fragment rect's y-extent to decide which messages fall inside it. Not
implemented.

## Output shape that works

Diagrams grouped roughly 20 to a markdown file: heading, kind, view id, source
URL, then the PlantUML in a fenced block. One file per diagram is fine at
sample scale and produces an unwieldy folder at full landscape scale. Verified
readable through the Google Drive connector with participants and message order
intact.

**Cache the converted output, not just the page.** A conditional GET returns
304 with no body, so a cache holding only pages must fetch again anyway — and a
cache holding converted output must carry a **renderer version**, or a
rendering fix never reaches published content because the 304 keeps restoring
the broken conversion.
