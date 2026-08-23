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


def clean_html(value: str) -> str:
    """Reduce an HTML fragment to plain text, keeping paragraph breaks."""
    if not value:
        return ""
    s = re.sub(r"<p[^>]*>|<br\s*/?>|</p>", "\n", value, flags=re.I)
    s = TAG_RE.sub("", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = WS_RE.sub(" ", s)
    return "\n".join(l.strip() for l in s.splitlines() if l.strip()).strip()


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
