#!/usr/bin/env python3
"""
Convert a BIAN InSite view page into PlantUML.

The data files under data/*.js contain no geometry, so ordering, senders and
receivers are absent from them. The geometry lives in the VIEW pages —
views/view_<id>.html embeds a fully tagged SVG:

    <g bizzid="103876" bizzsemantic="103877"
       bizzconcept="UML_Message" bizztype="relation" bizzsymbol="link">
      <path d="M 406.00 420.00 L 736.00 420.00"/>

  bizzconcept   the UML metaclass
  bizzid        the diagram element
  bizzsemantic  the model object it depicts — joins back to objectDataMapping

From that geometry:
  message order    y coordinate, ascending
  sender/receiver  x1 and x2 matched to the nearest lifeline column centre
  participants     UML_LifelineElement translate + rect width

Class diagrams use the same scheme with different concepts. Crucially,
UML_Attribute groups are nested inside their UML_Class group AND geometrically
contained by its rectangle, so attribute-to-class ownership — absent from the
relation graph entirely — is recoverable here by containment.

This module holds the parsing and generation only. Fetching belongs to
bianlib.fetch, which paces itself; tools/view_to_plantuml.py is the standalone
CLI over the top.
"""

from __future__ import annotations

import re
import urllib.request
from collections import defaultdict
from pathlib import Path

UA = "Mozilla/5.0 (compatible; content-acquisition/1.0)"

TAG_RE = re.compile(
    r'<g bizzid="([^"]+)"'
    r'(?: bizzsemantic="(\d+)")?'
    r' bizzconcept="([^"]+)"'
    r'(?: bizztype="([^"]*)")?'
    r'(?: bizzsymbol="([^"]*)")?')
TEXT_RE = re.compile(r"<text[^>]*>([^<]*)</text>")
TRANSLATE_RE = re.compile(r"translate\(([-\d.]+),\s*([-\d.]+)\)")
RECT_RE = re.compile(
    r'<rect[^>]*?x="([-\d.]+)"[^>]*?y="([-\d.]+)"[^>]*?'
    r'width="([-\d.]+)"[^>]*?height="([-\d.]+)"')
RECT_WH_RE = re.compile(r'<rect[^>]*?width="([-\d.]+)"[^>]*?height="([-\d.]+)"')
PATH_D_RE = re.compile(r'<path[^>]*?d="([^"]+)"')
NUM_RE = re.compile(r"[-\d.]+")

#: Boxes an attribute row can legitimately live inside. Only UML_Class was
#: recognised at first, which stranded every literal in an enumeration and
#: every field in an interface or data type — 35 rows in one chunk of 130
#: views. The PlantUML keyword differs per kind, hence the mapping rather
#: than a set.
CONTAINERS = {
    "UML_Class": "class",
    "UML_Enumeration": "enum",
    "UML_Interface": "interface",
    "UML_DataType": "class",
    "UML_Object": "object",
    "UML_Component": "component",
    "UML_Signal": "class",
    "UML_PrimitiveType": "class",
}


def path_endpoints(chunk: str):
    """First and last point of a connector path.

    Connectors are not always a single M/L pair — orthogonal routes carry
    several segments and a rounded corner, e.g.
        M 5180 2620 L 4100 2639.64 Q 4080 2640 4080 2620 L 4080 2360
    Taking the first L would land mid-route, so read the whole command list
    and keep the extreme points.
    """
    for d in PATH_D_RE.findall(chunk):
        if not d.lstrip().startswith("M"):
            continue
        nums = [float(n) for n in NUM_RE.findall(d) if n not in (".", "-")]
        if len(nums) >= 4:
            return nums[0], nums[1], nums[-2], nums[-1]
    return None


def fetch(url: str, timeout: int = 90) -> str:
    """Unpaced single fetch, for the standalone CLI and local files only.

    Bulk work goes through bianlib.fetch.Fetcher instead, which paces itself.
    """
    if not url.startswith("http"):
        return Path(url).read_text(encoding="utf-8", errors="replace")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_svg(html: str) -> str:
    """The licence comment contains a literal <svg></svg>, so anchor on the
    versioned opening tag and search for the close from there."""
    i = html.find('<svg version=')
    if i < 0:
        raise ValueError("no diagram SVG in this page")
    j = html.index("</svg>", i)
    return html[i:j + 6]


def blocks(svg: str) -> dict:
    """bizzid -> {concept, semantic, type, symbol, chunk}."""
    marks = [(m.start(), m.groups()) for m in TAG_RE.finditer(svg)]
    marks.append((len(svg), None))
    out = {}
    for i in range(len(marks) - 1):
        bid, sem, concept, btype, symbol = marks[i][1]
        out[bid] = {
            "concept": concept, "semantic": sem, "type": btype,
            "symbol": symbol, "chunk": svg[marks[i][0]:marks[i + 1][0]],
        }
    return out


def label_for(bs: dict, bid: str) -> str:
    chunk = bs.get("label" + bid, {}).get("chunk", "")
    parts = [t.strip() for t in TEXT_RE.findall(chunk) if t.strip()]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def diagram_title(bs: dict, svg: str) -> str:
    for bid, b in bs.items():
        if b["concept"] == "UML_Interaction":
            t = label_for(bs, bid)
            return re.sub(r"^sd\s+", "", t).strip()
    for bid, b in bs.items():
        if b["concept"] == "Canvas":
            return label_for(bs, bid) or "diagram"
    return "diagram"


def title_from_view_data(source: str, svg: str, base: str = "",
                         fetcher=None) -> str:
    """Class diagrams carry no UML_Interaction, so their name is not in the
    SVG. It lives in the sibling views/view_<id>_data.js as objectData.

    That is a second request per class diagram — 802 of them — so the bulk
    pipeline passes the name in from insiteViews instead and never gets here.
    """
    m = re.search(r'<svg version=[^>]*?bizzid="(\d+)"', svg)
    if not m:
        return ""
    vid = m.group(1)
    if source.startswith("http"):
        root = base or source.rsplit("/views/", 1)[0]
        cand = f"{root}/views/view_{vid}_data.js"
    else:
        parent = Path(source).parent
        hits = list(parent.rglob(f"view_{vid}_data*.js"))
        if not hits:
            return ""
        cand = str(hits[0])
    try:
        txt = fetcher.get(cand).text if (fetcher and cand.startswith("http")) \
            else fetch(cand)
    except Exception:
        return ""
    m = re.search(r'"name"\s*:\s*"([^"]+)"', txt)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def parse_view(html: str, source: str = "", known_title: str = "",
               base: str = "", fetcher=None) -> dict:
    svg = extract_svg(html)
    bs = blocks(svg)

    participants = []
    for bid, b in bs.items():
        if b["concept"] != "UML_LifelineElement":
            continue
        tr = TRANSLATE_RE.search(b["chunk"])
        wh = RECT_WH_RE.search(b["chunk"])
        if not (tr and wh):
            continue
        cx = float(tr.group(1)) + float(wh.group(1)) / 2
        name = label_for(bs, bid).lstrip(":").strip()
        participants.append({"x": cx, "name": name or f"object {bid}",
                             "semantic": b["semantic"], "bizzid": bid,
                             "key": bid})
    participants.sort(key=lambda p: p["x"])

    def column(x: float):
        return min(participants, key=lambda p: abs(p["x"] - x)) if participants else None

    messages = []
    for bid, b in bs.items():
        if b["concept"] != "UML_Message":
            continue
        pts = path_endpoints(b["chunk"])
        if not pts:
            continue
        x1, y1, x2, y2 = pts
        src, dst = column(x1), column(x2)
        messages.append({
            "y": y1, "x1": x1, "x2": x2,
            "from": src["name"] if src else "?",
            "to": dst["name"] if dst else "?",
            "from_key": src["key"] if src else None,
            "to_key": dst["key"] if dst else None,
            "self": bool(src and dst and src["key"] == dst["key"]),
            "text": label_for(bs, bid), "semantic": b["semantic"],
        })
    messages.sort(key=lambda m: m["y"])

    # ---- class diagram ------------------------------------------------
    classes = {}
    for bid, b in bs.items():
        if b["concept"] not in CONTAINERS:
            continue
        r = RECT_RE.search(b["chunk"])
        if not r:
            continue
        x, y, w, hh = (float(v) for v in r.groups())
        classes[bid] = {"x": x, "y": y, "w": w, "h": hh,
                        "kind": CONTAINERS[b["concept"]],
                        "name": label_for(bs, bid) or f"Class {bid}",
                        "semantic": b["semantic"], "attributes": []}

    unattached = []
    for bid, b in bs.items():
        if b["concept"] != "UML_Attribute":
            continue
        r = RECT_RE.search(b["chunk"])
        if not r:
            continue
        ax, ay, aw, ah = (float(v) for v in r.groups())
        owner = None
        for cid, c in classes.items():
            if (c["x"] <= ax and ax + aw <= c["x"] + c["w"]
                    and c["y"] <= ay and ay + ah <= c["y"] + c["h"]):
                owner = cid
                break
        if owner is None:
            # Never silently dropped. An attribute whose owner cannot be
            # established geometrically is still content BIAN published, so it
            # is carried through to an explicit "(unattached)" box rather than
            # being counted and discarded.
            unattached.append({"text": label_for(bs, bid),
                               "semantic": b["semantic"]})
            continue
        classes[owner]["attributes"].append(
            {"y": ay, "text": label_for(bs, bid), "semantic": b["semantic"]})
    for c in classes.values():
        c["attributes"].sort(key=lambda a: a["y"])

    def nearest_class(px, py):
        best, bd = None, None
        for cid, c in classes.items():
            dx = max(c["x"] - px, 0, px - (c["x"] + c["w"]))
            dy = max(c["y"] - py, 0, py - (c["y"] + c["h"]))
            d = dx * dx + dy * dy
            if bd is None or d < bd:
                bd, best = d, cid
        return best, (bd or 0) ** 0.5

    edges = []
    for bid, b in bs.items():
        if b["concept"] not in ("UML_Association", "UML_Generalization",
                                "UML_Aggregation", "UML_Composition",
                                "UML_Dependency", "UML_Realization"):
            continue
        pts = path_endpoints(b["chunk"])
        if not pts or not classes:
            continue
        x1, y1, x2, y2 = pts
        a, da = nearest_class(x1, y1)
        c, dc = nearest_class(x2, y2)
        edges.append({"kind": b["concept"], "from": a, "to": c,
                      "label": label_for(bs, bid),
                      "gap": max(da, dc), "semantic": b["semantic"]})

    fragments = []
    for bid, b in bs.items():
        if "Fragment" in b["concept"] or "Combined" in b["concept"]:
            fragments.append({"label": label_for(bs, bid), "concept": b["concept"],
                              "bizzid": bid})

    concepts = defaultdict(int)
    for b in bs.values():
        concepts[b["concept"]] += 1

    m = re.search(r'<svg version=[^>]*?bizzid="(\d+)"', svg)
    view_id = m.group(1) if m else ""

    title = diagram_title(bs, svg)
    if title in ("", "diagram"):
        # insiteViews already knows every diagram's name, so the bulk pipeline
        # supplies it here rather than spending a second request per page.
        title = known_title or title_from_view_data(
            source, svg, base=base, fetcher=fetcher) or (
            f"View {view_id}" if view_id else "diagram")

    return {"title": title, "view_id": view_id, "participants": participants,
            "messages": messages, "fragments": fragments,
            "classes": classes, "edges": edges,
            "unattached": unattached, "unassigned_attrs": len(unattached),
            "concepts": dict(concepts)}


def alias(name: str, used: set) -> str:
    """A unique alias per participant INSTANCE.

    A diagram can show the same service domain on two lifelines — "Customer
    Product and Service Directory" appears twice in the sweep-agreement
    diagrams, and "Savings Account" twice in interest settlement. They are
    distinct participants with distinct object ids, so they need distinct
    aliases; emitting the same alias twice is invalid PlantUML and silently
    merges two columns into one.
    """
    a = re.sub(r"[^A-Za-z0-9]", "", name) or "P"
    if a[0].isdigit():
        a = "P" + a
    base, n = a, 2
    while a in used:
        a = f"{base}_{n}"
        n += 1
    used.add(a)
    return a


def quoted(text: str) -> str:
    """A name safe to put inside PlantUML's double quotes.

    A double quote in a name would close the string early and a newline would
    end the statement, either of which turns the whole diagram into an error
    image rather than a slightly wrong label.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.replace('"', "'")


def comment(text: str) -> str:
    """An inline PlantUML comment.

    A leading apostrophe only opens a comment at the START of a line. Trailing
    "' object 34300" on a participant or class line is a syntax error, and
    PlantUML's response is to render an error image instead of the diagram —
    so every one of the 1,181 published diagrams failed to draw while the
    markdown around them looked perfectly fine.

    Inline comments use the paired form. Verified against PlantUML 1.2026.0.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    # "'/" inside the text would close the comment early.
    text = text.replace("'/", "' /")
    return f" /' {text} '/" if text else ""


ARROWS = {
    "UML_Generalization": "--|>",
    "UML_Realization": "..|>",
    "UML_Composition": "*--",
    "UML_Aggregation": "o--",
    "UML_Dependency": "..>",
    "UML_Association": "--",
}


def class_plantuml(d: dict, source: str) -> str:
    L = ["@startuml", f"' Generated from {source}",
         "' Attribute ownership derived from geometric containment:",
         "' each UML_Attribute rect sits inside its UML_Class rect.",
         "skinparam shadowing false",
         "skinparam classAttributeIconSize 0",
         "hide circle", ""]
    if d["title"]:
        L += [f"title {quoted(d['title'])}", ""]

    used, aliases = set(), {}
    for cid, c in sorted(d["classes"].items(), key=lambda kv: (kv[1]["x"], kv[1]["y"])):
        a = alias(c["name"], used)
        aliases[cid] = a
        sem = comment(f"object {c['semantic']}") if c["semantic"] else ""
        L.append(f'{c.get("kind", "class")} "{quoted(c["name"])}" as {a} '
                 f'{{{sem}')
        for attr in c["attributes"]:
            text = attr["text"].replace("{", "(").replace("}", ")").strip()
            if text:
                L.append(f"  {text}")
        L.append("}")
        L.append("")

    # Rows whose owning box could not be established geometrically. Shown
    # rather than dropped, and labelled so nobody mistakes them for members of
    # a real class.
    if d.get("unattached"):
        L.append('class "(unattached attributes)" as Unattached {')
        L.append("  /' Ownership not derivable from this view's geometry. '/")
        for attr in d["unattached"]:
            text = attr["text"].replace("{", "(").replace("}", ")").strip()
            if text:
                sem = comment(f"object {attr['semantic']}") if attr["semantic"] else ""
                L.append(f"  {text}{sem}")
        L += ["}", ""]

    for e in d["edges"]:
        if e["from"] not in aliases or e["to"] not in aliases:
            continue
        if e["from"] == e["to"]:
            continue
        arrow = ARROWS.get(e["kind"], "--")
        lbl = f" : {e['label']}" if e["label"] else ""
        note = "" if e["gap"] < 1 else comment(f"endpoint gap {e['gap']:.0f}")
        L.append(f"{aliases[e['from']]} {arrow} {aliases[e['to']]}{lbl}{note}")

    L += ["", "@enduml", ""]
    return "\n".join(L)


def to_plantuml(d: dict, source: str) -> str:
    L = ["@startuml", f"' Generated from {source}",
         "' Geometry-derived: order from y, direction from x1 -> x2.",
         "skinparam shadowing false",
         "skinparam sequenceMessageAlign left",
         "hide footbox", ""]
    if d["title"]:
        L.append(f"title {quoted(d['title'])}")
        L.append("")

    used: set = set()
    for p in d["participants"]:
        p["alias"] = alias(p["name"], used)
        sem = comment(f"object {p['semantic']}") if p["semantic"] else ""
        L.append(f'participant "{quoted(p["name"])}" as {p["alias"]}{sem}')
    L.append("")

    by_key = {p["key"]: p["alias"] for p in d["participants"]}
    for m in d["messages"]:
        src = by_key.get(m["from_key"], "?")
        dst = by_key.get(m["to_key"], "?")
        text = m["text"].replace("\n", " ").strip() or "(unlabelled)"
        arrow = "->" if not m["self"] else "->"
        L.append(f"{src} {arrow} {dst} : {text}")

    if d["fragments"]:
        L.append("")
        for f in d["fragments"]:
            L.append(f"' fragment present but not rendered: "
                     f"{f['concept']} {f['label']!r}")

    L += ["", "@enduml", ""]
    return "\n".join(L)


def summarise(d: dict) -> str:
    attrs = sum(len(c["attributes"]) for c in d["classes"].values())
    out = [f"  title        {d['title']!r}",
           f"  participants {len(d['participants'])}   messages {len(d['messages'])}",
           f"  classes      {len(d['classes'])}   attributes {attrs}"
           f"   unassigned {d['unassigned_attrs']}",
           f"  edges        {len(d['edges'])}",
           f"  concepts     {d['concepts']}"]
    return "\n".join(out)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


def render(d: dict, source: str) -> tuple[str, str] | tuple[None, None]:
    """PlantUML plus its kind, or (None, None) if this is neither kind.

    ArchiMate views — 645 Total views and 215 capability maps at v14 — use a
    different concept set entirely and produce nothing here. They are excluded
    by the planner so this rarely fires, but a misclassified view must degrade
    to "skipped", never to an empty diagram.
    """
    if d["messages"]:
        return to_plantuml(d, source), "sequence"
    if d["classes"]:
        return class_plantuml(d, source), "class"
    return None, None


def diagram_markdown(d: dict, body: str, kind: str, source: str) -> str:
    """One diagram as a markdown item, matching the sample bundle's format."""
    lines = [f"## {d['title']}", "",
             f"- **Kind:** {kind} diagram",
             f"- **View id:** {d.get('view_id') or '?'}"]
    if kind == "sequence":
        lines.append(f"- **Participants:** {len(d['participants'])}   "
                     f"**Messages:** {len(d['messages'])}")
    else:
        attrs = sum(len(c["attributes"]) for c in d["classes"].values())
        stray = len(d.get("unattached", []))
        lines.append(f"- **Classes:** {len(d['classes'])}   "
                     f"**Attributes:** {attrs + stray}"
                     + (f" ({stray} unattached)" if stray else "") + "   "
                     f"**Relationships:** {len(d['edges'])}")
    lines += [f"- **Source:** {source}", "",
              "Generated from the view page's SVG geometry: message order from "
              "the y coordinate, sender and receiver from the x coordinates "
              "matched to lifeline columns; class attribute ownership from "
              "geometric containment.",
              "", "```plantuml", body.rstrip(), "```", "", "---", ""]
    return "\n".join(lines)
