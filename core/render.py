"""
Shared rendering helpers.

Every source produces the same output shape, so Claude reads them the same way
and the generic checks apply everywhere:

    out/<source>/index.md        entry point: categories, counts, file names
    out/<source>/<category>.md   items grouped by category, N per file
    out/<source>/manifest.json   per-item hash, name, category
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from pathlib import Path

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


#: The tags that produce a separator, as SETS -- the regexes below are built
#: from them so the two cannot drift. The gate imports SEPARATING_TAGS as its
#: declaration of what is handled, rather than keeping a second list: a
#: declaration maintained beside the parser instead of derived from it is how
#: `title` came to be declared handled while being dropped on table
#: categories, and how a missing `id` produced a 100% finding out of nothing.
PARAGRAPH_TAGS = frozenset({"p", "br"})

#: Block-level tags whose boundary is a line break.
BLOCK_BOUNDARY_TAGS = frozenset({
    "li", "ul", "ol", "tr", "thead", "tbody", "table", "div", "blockquote",
    "dd", "dt", "dl", "h1", "h2", "h3", "h4", "h5", "h6"})

#: A table CELL boundary is a separator within a line, not a line break.
CELL_BOUNDARY_TAGS = frozenset({"td", "th"})

SEPARATING_TAGS = PARAGRAPH_TAGS | BLOCK_BOUNDARY_TAGS | CELL_BOUNDARY_TAGS

BLOCK_BOUNDARY_RE = re.compile(
    r"</?\s*(?:" + "|".join(sorted(BLOCK_BOUNDARY_TAGS)) + r")[^>]*>", re.I)

CELL_BOUNDARY_RE = re.compile(
    r"</\s*(?:" + "|".join(sorted(CELL_BOUNDARY_TAGS)) + r")\s*>", re.I)


def clean_html(value: str) -> str:
    """Reduce an HTML fragment to plain text, keeping every text boundary.

    ADOPTED in changeset 059, on the measurements from run 33481175804 rather
    than on an argument: 36 of 5,734 published values change, 133 across all
    objects, no stranded punctuation introduced, and all 28,096 values without
    block tags byte-identical. It shipped unused for three changesets so those
    numbers could be taken while the old behaviour was still producing the
    stored text -- once the cleaner changes there is nothing left to compare
    against.

    The change is deliberately minimal: insert the separator that deletion
    removes, and nothing else. Cells are joined with a space and rows and list
    items with a newline, because the fault being corrected is missing
    separation -- not missing markdown. Restyling list items as bullets would
    change far more text than the 138 values that are actually broken.

    STRANDED DELIMITERS. Some BIAN markup puts quotes OUTSIDE the list item:
    `"<li>Request handling of an exceptional repayment",</li>"<li>Execute...`.
    Breaking at every boundary isolates each bare `"` on its own line, which
    run 33477632935 produced for object 141803 -- a value the old cleaner
    rendered readably and this one broke. So a line carrying no alphanumeric
    character is folded back into its neighbour: forward when it ends with an
    opening delimiter, backward otherwise, since a trailing comma belongs to
    the text before it and an opening quote to the text after.

    That regression was found by measuring the change against the source
    rather than by reading this function, and G27 now counts stranded lines
    every run so the next such case is a number rather than a surprise.
    """
    if not value:
        return ""
    # `_prepare` marks the boundaries this function inserts with a sentinel
    # rather than a newline, so the folding acts only on those. Folding across
    # pre-existing <p> and <br> breaks edits text whose boundaries were never
    # being deleted: it merged a row of dots used as a separator onto the line
    # above it in object 147626.
    s = _prepare(value)
    out, _ = _assemble(s)
    return out


def clean_html_stranded(value: str) -> list[str]:
    """Punctuation-only lines this cleaner leaves at a boundary IT inserted.

    G27's baseline after `clean_html_legacy` was removed. Comparing against the
    retired function measured a hypothetical once nothing called it; comparing
    against the source would flag BIAN's own dot-separator lines, which is the
    false positive that failed run 33480408308. So the cleaner reports its own
    residue instead: a segment that could not be folded into any neighbour,
    which can only arise from a boundary this function introduced.
    """
    if not value:
        return []
    _, stranded = _assemble(_prepare(value))
    return stranded


def _prepare(value: str) -> str:
    s = re.sub(r"<p[^>]*>|<br\s*/?>|</p>", "\n", value, flags=re.I)
    s = CELL_BOUNDARY_RE.sub(" ", s)
    s = BLOCK_BOUNDARY_RE.sub("\x00", s)
    s = TAG_RE.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    return WS_RE.sub(" ", s)


def _assemble(s: str) -> tuple[str, list[str]]:
    """Fold each sentinel-split line, returning the text and any residue."""
    out: list[str] = []
    stranded: list[str] = []
    for line in s.split("\n"):
        segments = [p.strip() for p in line.split("\x00")]
        parts = _fold_stranded(segments)
        if len(segments) > 1:
            stranded.extend(p for p in parts
                            if p and not ALNUM_RE.search(p))
        out.extend(p for p in parts if p)
    return "\n".join(l.strip() for l in out if l.strip()).strip(), stranded


#: A segment with no letter or digit in it is punctuation that lost its text.
ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)

#: Delimiters that open something, so the text they belong to follows them.
OPENING_DELIMS = "\"'\u201c\u2018([{"


def _fold_stranded(parts: list[str]) -> list[str]:
    """Fold punctuation-only segments back into the text they belong to.

    Applied only to segments split at a block boundary this module inserted,
    never across a break that was already in the markup.
    """
    if len(parts) < 2:
        return parts
    out: list[str] = []
    pending = ""
    for part in parts:
        if part and not ALNUM_RE.search(part):
            if part.endswith(tuple(OPENING_DELIMS)):
                pending += part          # belongs to the segment that follows
            elif out:
                out[-1] += part          # belongs to the segment before it
            else:
                pending += part
            continue
        if part:
            out.append(pending + part)
            pending = ""
    if pending and out:
        out[-1] += pending
    elif pending:
        out.append(pending)
    return out


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "other"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reset_dir(path: Path) -> Path:
    """Empty a source's output directory. Scoped to one source only."""
    path.mkdir(parents=True, exist_ok=True)
    for f in sorted(path.rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()
    return path


def write_bundles(
    outdir: Path,
    source_id: str,
    source_name: str,
    items: list[dict],
    per_file: int | dict = 40,
    per_file_default: int = 40,
    complete: bool = True,
    extra_index_lines: list[str] | None = None,
) -> dict:
    """Write grouped markdown, an index and a manifest.

    Each item: {"id", "name", "category", "body"} where body is markdown for
    one item, ending with its own separator.

    `per_file` may be a single number or {category: number}, because item sizes
    are not uniform once a source emits more than one kind of thing: a rendered
    diagram is two orders of magnitude larger than a property table, and one
    grouping size cannot suit both.

    `complete=False` marks a partial bundle. `core.publish` refuses to sync
    one, since `rclone sync` deletes whatever the source lacks — a partial
    harvest published over a full one silently destroys the difference.
    """
    outdir.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("category") or "Other", []).append(item)

    generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    index = [
        f"# {source_name}",
        "",
        f"Acquired {generated} — {len(items)} items.",
        "",
    ]
    if extra_index_lines:
        index += extra_index_lines + [""]
    index += ["| Category | Items | Files |", "|---|---|---|"]

    files_written = 0
    for category in sorted(groups, key=lambda k: -len(groups[k])):
        entries = sorted(groups[category], key=lambda i: i.get("name", ""))
        slug = slugify(category)
        size = (per_file.get(category, per_file_default)
                if isinstance(per_file, dict) else per_file)
        size = max(1, int(size))
        chunks = [entries[i:i + size] for i in range(0, len(entries), size)]
        names = []
        for n, chunk in enumerate(chunks, 1):
            fname = f"{slug}_{n:02d}.md" if len(chunks) > 1 else f"{slug}.md"
            header = f"# {source_name} — {category} ({len(chunk)} items)\n\n"
            (outdir / fname).write_text(
                header + "\n".join(i["body"] for i in chunk), encoding="utf-8")
            names.append(fname)
            files_written += 1
        index.append(
            f"| {category} | {len(entries)} | "
            + ", ".join(f"`{n}`" for n in names) + " |")

    (outdir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    manifest = {
        "source": source_id,
        "source_name": source_name,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "complete": bool(complete),
        "count": len(items),
        "categories": {c: len(v) for c, v in groups.items()},
        "items": {
            str(i["id"]): {
                "name": i.get("name", ""),
                "category": i.get("category", ""),
                "sha256": digest(i["body"]),
            } for i in items
        },
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "files_written": files_written + 2,
        "categories": manifest["categories"],
    }
