# Dead ends — approaches tried and refuted

Each of these looks reasonable and cost real time. The evidence is recorded so
they are not retried.

**Five of the first ten were the same mistake:** searching `data/*.js` for
information that lives in `views/*.html`. Establish which artefact holds which
kind of information before probing, and prefer dumping one small complete
example over sampling many large ones.

## 1. Browser automation for object pages

**Tried:** Playwright driving headless Firefox against
`object_16.html?object=NNNNN`, because a plain fetch returns
`<title>InSite</title>` and nothing else.

**Refuted by:** the page's own network log. It loads static `data/*.js` files
and calls no API.

**Lesson:** before building a renderer, list what the page actually requests. A
JS-rendered page is often a static-file app in disguise.

## 2. Reading a single data file

**Tried:** harvesting `all_objects_data_16.js` alone, because 16 is the number
in `object_16.html`.

**Refuted by:** probing `all_objects_data_N.js` — two dozen existed in the
first probe range, and the mapping file later showed roughly twice that. One
shard holds about 3% of the objects and well under two thirds of the service
domains.

**Lesson:** the failure was **silent**. The output looked complete and
plausible. It was caught only by comparing a service domain count against an
independently published view. Always cross-check a total against a source that
does not share the bug.

## 3. Message ordering from the relation graph

**Refuted by:** scanning every property of every message. No ordering field
exists, and messages have **no relations at all**.

## 4. Message ordering from object id order

**Tried:** sorting messages by numeric id, which produced a coherent login
sequence in the first diagram examined.

**Refuted by:** checking three more. In one, authentication appears *after*
retrieving product holdings. In another, *Submit Access Token* precedes
*Exchange Auth Code for Token* — the OAuth flow inverted.

**Lesson:** one confirming sample is not evidence. A wrong order is worse than
no diagram, because it looks authoritative.

## 5. Ordering from geometry in the data files

**Refuted by:** `Graphical shape`, `Line` and `Connection` objects carry **zero
properties** in `data/*.js`. BIAN stripped geometry from the object export.

**Resolution:** geometry exists inline in `views/view_<id>.html`. Approaches 3
to 5 were searching the wrong file the entire time.

## 6. Attribute-to-class ownership from relations

**Refuted by:** almost no attributes have any relation at all, and the few that
do point at `Graphical shape`. The `member end` verb is Class→Class — a UML
association end, not attribute ownership.

**Resolution:** geometric containment in the view page.

## 7. Attribute ownership from id adjacency

**Tried:** assuming a run of consecutive attribute ids belongs to the class
preceding it.

**Refuted by:** a run spans all the classes of a diagram undivided, and generic
names carry no owner — `Version Number` appears three times in one diagram.

## 8. Communication diagrams as a fallback

**Tried:** dropping the timeline and keeping who-talks-to-whom, since that
needs no ordering.

**Refuted by:** messages have no relation to their execution specifications, so
neither end of a message is reachable from the graph. Superseded anyway once
the view geometry was found.

## 9. Assuming one variable per data file

**Refuted by:** `JSONDecodeError: Extra data`. `all_objects_on_views.js`
defines both `objectsOnViews` and `insiteViews`.

**Resolution:** `json.JSONDecoder().raw_decode()` in a loop — see
`data-files.md`. `insiteViews` turned out to be a registry of every diagram
name, which is genuinely useful and was only found because of this bug.

## 10. Deriving message receivers from service operation ownership

Sound in principle, and never needed once the geometry was found. It would only
ever have yielded the *receiver*: the sender is not modelled, because BIAN
specifies what a domain offers, not who calls it.

## 11. Classifying views by what they contain

**Tried:** scoring each view by its members' categories — Messages and
Execution specifications mean sequence, Classes and Attributes mean class,
Business functions and Capabilities mean ArchiMate — so that only convertible
views would be fetched.

**Refuted by:** the counts. It returned about 30% more class views than the
reference count, and refining it made the over-count worse rather than better.
Several ArchiMate view types are built from objects it cannot distinguish from
UML classes, and of the view ids that are not objects in the model at all, it
confidently labelled two thirds "class" — every one of them wrong.

**Resolution:** the model names its own diagrams. A view id is the id of a
diagram object whose category is `Class diagram`, `Sequence diagram`,
`Total view` and so on. This reproduces the reference counts exactly.

**Lesson:** the heuristic was only *nearly* right, and near-right is the
expensive kind — it produces plausible numbers that survive review. Two CI
rounds went into refining it before anyone tried reading the fact directly.
**Ask whether the source already states the fact before inferring it.**

## 12. Assuming attributes live only in classes

**Tried:** collecting `UML_Class` rects as the only containers for
`UML_Attribute` rows. Validated on a sample: 147 attributes across 12 classes,
zero unassignable.

**Refuted by:** the full landscape, where about 1% of rows had no owner. **The
sample contained no enumerations.** `UML_Enumeration`, `UML_Interface`,
`UML_DataType`, `UML_Object`, `UML_Component`, `UML_Signal` and
`UML_PrimitiveType` all hold attribute rows, and the code was counting the
strays as a quality metric while discarding the content.

**Lesson:** a clean sample result is a smoke test, not a guarantee. **Ask what
the sample did not contain.** This is the same failure as dead end 2 — a
partial read that looked complete — and the same failure as a synthetic test
fixture built from the same assumption as the code it validates.
