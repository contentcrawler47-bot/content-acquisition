#!/usr/bin/env python3
"""
View geometry: what a page carries that the object model does not.

The model says which objects and relations are drawn on a view. Only the page
says where, how large, and what contains what. That arrangement is the whole
reason to fetch a page at all, and it is what this module reads.

Three things were measured on 30 August 2026 across four saved pages -- two
ArchiMate Total views, a Capability map view and a UML class diagram -- and
each of them shaped what is below.

**A view's membership includes its edges.** `objectsOnViews` is everything
drawn, not the objects placed. View 53590's 48 members are 21 nodes and 27
relations; view 35509's 73 are 65 nodes and 7 associations. So the parser
classifies each block rather than assuming it is a node.

**The SVG concept is a shape, not a type.** All 16 ServiceDomain objects on
53590 are drawn as `StrategyCapability`, as is the one Capability. On 54486,
341 `StrategyCapability` blocks cover 339 service domains and 2 capabilities.
A renderer keyed on the concept would relabel 339 service domains. The type
comes from the model, through `object_id`; `concept` is retained only to say
what shape was drawn.

**ArchiMate nodes carry no `<rect>`.** They are rounded-rectangle path
outlines -- `M 545,80 h 2510 a 25,25 0 0 1 25,25 v 730 ...`. `RECT_RE` finds
nothing at all on a capability map, and taking a path's first and last point
returns nearly the same point on a closed outline. Walking the command list
recovers the box, and one function then serves both notations with `<rect>`
as the fast path.
"""

from __future__ import annotations

import re

from bianlib import views as V

#: Relation blocks are named <Source><Target><RelationType>, so the notation's
#: relation types are recognised by suffix. The set is closed and was measured
#: by the ArchiMate probe; `Realization` is included because the model uses it
#: heavily even though the sampled pages did not draw one.
EDGE_SUFFIXES = ("Triggering", "Association", "Flow", "Access", "Aggregation",
                 "Specialization", "Assignment", "Composition", "Junction",
                 "Influence", "Realization", "Serving")

#: Junctions are connector NODES in ArchiMate, not relationships. The suffix
#: test below catches `OrJunction` because it ends with `Junction`, which put
#: 13 of them into the edge collection with no endpoints on the first full run
#: -- invisible to a renderer drawing nodes and skipped by one drawing edges.
#: Named explicitly rather than fixed by pattern, because the pattern is what
#: got it wrong.
JUNCTION_ELEMENTS = {"Junction", "OrJunction", "AndJunction"}

#: UML edge concepts are named outright rather than by composition.
UML_EDGE_CONCEPTS = {"UML_Association", "UML_Generalization", "UML_Message",
                     "UML_Transition", "UML_Dependency", "UML_Realization"}

#: Drawn furniture that is neither a node nor an edge. `Canvas` is the view
#: itself and is the one block that legitimately has no box.
NON_ELEMENT = {"label", "icon", "Canvas", "ViewGraphic", "ViewEdge"}

_CMD = re.compile(r"([MmLlHhVvAaCcQqZz])([^MmLlHhVvAaCcQqZz]*)")
_NUM = re.compile(r"-?\d*\.?\d+")


def is_edge(concept: str) -> bool:
    """Whether a block is a relationship rather than an element.

    A relation block is named <Source><Target><RelationType>, so it is always
    strictly longer than the relation type itself. A concept equal to a bare
    suffix is an element that happens to share the name -- which is why the
    test excludes an exact match as well as the junction elements.
    """
    if not concept:
        return False
    if concept in JUNCTION_ELEMENTS:
        return False
    if concept in UML_EDGE_CONCEPTS:
        return True
    return any(concept.endswith(s) and concept != s for s in EDGE_SUFFIXES)


def path_bbox(chunk: str):
    """Bounding box of the largest shape path in a block, or None.

    Walks the command list rather than reading raw numbers, because arc
    commands carry radii and flags that are not coordinates and would inflate
    a naive minimum and maximum. The largest box wins: a node's block also
    contains its icon, which is a smaller path.
    """
    best = None
    for d in V.PATH_D_RE.findall(chunk):
        x = y = 0.0
        pts = []
        for cmd, args in _CMD.findall(d):
            n = [float(v) for v in _NUM.findall(args)]
            if cmd in "Mm":
                for i in range(0, len(n) - 1, 2):
                    x, y = (n[i], n[i + 1]) if cmd == "M" else (x + n[i], y + n[i + 1])
                    pts.append((x, y))
            elif cmd in "Ll":
                for i in range(0, len(n) - 1, 2):
                    x, y = (n[i], n[i + 1]) if cmd == "L" else (x + n[i], y + n[i + 1])
                    pts.append((x, y))
            elif cmd in "Hh":
                for v in n:
                    x = v if cmd == "H" else x + v
                    pts.append((x, y))
            elif cmd in "Vv":
                for v in n:
                    y = v if cmd == "V" else y + v
                    pts.append((x, y))
            elif cmd in "Aa":
                # rx ry rotation large-arc sweep x y
                for i in range(0, len(n) - 6, 7):
                    x, y = ((n[i + 5], n[i + 6]) if cmd == "A"
                            else (x + n[i + 5], y + n[i + 6]))
                    pts.append((x, y))
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if box[2] > 0 and box[3] > 0 and (best is None
                                          or box[2] * box[3] > best[2] * best[3]):
            best = box
    return best


def box_of(chunk: str):
    """A block's box: <rect> where there is one, otherwise its path outline."""
    r = V.RECT_RE.search(chunk)
    if r:
        x, y, w, h = (float(v) for v in r.groups())
        if w > 0 and h > 0:
            return x, y, w, h
    return path_bbox(chunk)


def _contains(outer, inner) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _assign_parents(nodes: list) -> None:
    """parent_id by smallest strictly-enclosing box.

    Derived rather than read: the markup nests a node inside its container's
    group, but `blocks()` chunks by tag position and does not preserve that
    nesting. Containment is what carries meaning on a Total view -- 54486 has
    51 CompositeGrouping boxes holding 339 service domains, and that grouping
    IS the value chain.
    """
    boxed = [n for n in nodes if n["w"] and n["h"]]
    for n in boxed:
        best = None
        for other in boxed:
            if other is n:
                continue
            if not _contains((other["x"], other["y"], other["w"], other["h"]),
                             (n["x"], n["y"], n["w"], n["h"])):
                continue
            if other["w"] * other["h"] <= n["w"] * n["h"]:
                continue          # same box or smaller: not a container
            if best is None or other["w"] * other["h"] < best["w"] * best["h"]:
                best = other
        n["parent_id"] = best["node_id"] if best else None


def _nearest(nodes: list, x: float, y: float):
    """The node whose box contains the point, else the nearest centre.

    An endpoint usually lands on a container's border rather than inside it,
    so containment alone strands most edges.
    """
    inside = [n for n in nodes
              if n["x"] <= x <= n["x"] + n["w"] and n["y"] <= y <= n["y"] + n["h"]]
    pool = inside or nodes
    if not pool:
        return None
    return min(pool, key=lambda n: (n["x"] + n["w"] / 2 - x) ** 2
               + (n["y"] + n["h"] / 2 - y) ** 2)


def parse_geometry(html: str, view_id) -> dict:
    """Nodes and edges with their geometry, from a saved or fetched page.

    Returns counts alongside the lists so a caller can report a denominator
    without walking them, and records how many blocks yielded no box at all --
    a page that parsed into nothing must not look like a page with nothing on
    it.
    """
    svg = V.extract_svg(html)
    bs = V.blocks(svg)

    nodes, edges, unboxed, skipped = [], [], 0, 0
    for bid, b in bs.items():
        concept = b.get("concept") or ""
        if concept in NON_ELEMENT or concept.startswith(("label", "icon")):
            skipped += 1
            continue
        label = V.label_for(bs, bid)
        if is_edge(concept):
            pts = V.path_endpoints(b["chunk"])
            edges.append({
                "edge_id": str(bid),
                "object_id": b.get("semantic"),
                "concept": concept,
                "label": label,
                "x1": pts[0] if pts else None, "y1": pts[1] if pts else None,
                "x2": pts[2] if pts else None, "y2": pts[3] if pts else None,
                "from_node": None, "to_node": None,
            })
            continue
        box = box_of(b["chunk"])
        if box is None:
            unboxed += 1
            continue
        x, y, w, h = box
        nodes.append({
            "node_id": str(bid),
            "object_id": b.get("semantic"),
            "concept": concept,
            "label": label,
            "x": x, "y": y, "w": w, "h": h,
            "parent_id": None,
        })

    _assign_parents(nodes)
    for e in edges:
        if e["x1"] is None:
            continue
        a = _nearest(nodes, e["x1"], e["y1"])
        b2 = _nearest(nodes, e["x2"], e["y2"])
        e["from_node"] = a["node_id"] if a else None
        e["to_node"] = b2["node_id"] if b2 else None

    uml = sum(1 for n in nodes if n["concept"].startswith("UML_"))
    return {
        "view_id": str(view_id),
        "notation": "UML" if uml > len(nodes) / 2 else "ArchiMate",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "unboxed": unboxed,
        "nodes": nodes,
        "edges": edges,
    }
