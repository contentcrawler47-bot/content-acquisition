"""
Stage 2, selection: which extracted objects do we publish?

The extract is stored unfiltered, so this is the ONLY place the allowlist
acts. That is the whole point of the two-stage split: changing what is
published becomes a re-render against stored data rather than another pass
over someone else's web server.

`is_wanted` is imported from `bianlib.landscape`, never restated. A tool that
re-declares a constant the pipeline owns will eventually disagree with the
pipeline and be believed — this project shipped a six-category-short copy of
this very allowlist and published a wrong total from it.

WHAT THIS REPORTS, AND WHY IT LOOKS OVER-CAREFUL

Objects and distinct names are counted separately. Anything keyed by name
collapses duplicates silently, and nine service domain names are shared by
thirty objects here, including four distinct `Fraud Diagnosis` objects.

Dropped categories are reported, not just kept ones. A filter cannot see the
population it excludes, and asking only "what did we keep" makes an allowlist
that is missing a whole category look identical to one that is complete.

Every figure carries its denominator. A bare count of kept objects is not
interpretable without the total it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import landscape as L


@dataclass
class Selection:
    """The result of selecting over one extract.

    `kept` holds the object records themselves; everything else is the
    evidence needed to judge whether the selection did what was intended.
    """

    kept: list = field(default_factory=list)
    total: int = 0
    kept_names: int = 0
    total_names: int = 0
    kept_by_category: dict = field(default_factory=dict)
    dropped_by_category: dict = field(default_factory=dict)
    dropped_structural: int = 0
    malformed: int = 0

    @property
    def dropped(self) -> int:
        return self.total - len(self.kept)

    def canary(self, object_id: str, name: str) -> bool:
        """Did a known object survive selection?

        A floor on counts catches a filter that broke entirely; it does not
        catch one that quietly stopped keeping the thing the whole bundle
        exists for. Checked by id, because the name is not unique.
        """
        return any(o.get("object_id") == str(object_id)
                   and (o.get("name") or "") == name for o in self.kept)


def select(objects, keep=None) -> Selection:
    """Apply the allowlist to extracted object records.

    `keep(category, name) -> bool` defaults to the pipeline's own predicate.
    It is injectable ONLY so that an allowlist experiment can be run without
    editing code; the default is the one the pipeline publishes with.
    """
    if keep is None:
        keep = L.is_wanted

    sel = Selection()
    names_all: set = set()
    names_kept: set = set()

    for obj in objects:
        sel.total += 1
        category = obj.get("category")
        name = obj.get("name")

        # NEVER OBSERVED. Against the extract of 30 August 2026, `category`,
        # `name`, `object_id`, `id`, `type`, `notation` and `type_icon_path`
        # were present on all 128,270 objects, and neither category nor name
        # was ever empty. This branch is a guard against an upstream shape
        # change, not a known case — so `malformed: 0` means NOT OBSERVED
        # rather than checked, and a non-zero value means the extract's shape
        # has moved and the selection numbers should not be trusted until it
        # is understood.
        if category is None:
            sel.malformed += 1
            continue

        name = name or ""
        names_all.add(name)

        if keep(category, name):
            sel.kept.append(obj)
            names_kept.add(name)
            sel.kept_by_category[category] = \
                sel.kept_by_category.get(category, 0) + 1
        else:
            sel.dropped_by_category[category] = \
                sel.dropped_by_category.get(category, 0) + 1
            if L.is_structural(category, name):
                sel.dropped_structural += 1

    sel.kept_names = len(names_kept)
    sel.total_names = len(names_all)
    return sel


def report(sel: Selection, top: int = 15) -> list[str]:
    """Human-readable evidence. Counts, ratios and category names only.

    Never item text: Actions logs on a public repo are world-readable.
    """
    lines = [
        f"  objects kept    : {len(sel.kept)} of {sel.total}"
        + (f" ({100 * len(sel.kept) / sel.total:.1f}%)" if sel.total else
           "  NOT MEASURED — no objects were read"),
        f"  distinct names  : {sel.kept_names} of {sel.total_names}",
        f"  dropped         : {sel.dropped}"
        f"  ({sel.dropped_structural} structural)",
        f"  malformed       : {sel.malformed}",
        "",
        f"  kept, by category ({len(sel.kept_by_category)} categories):",
    ]
    for cat, n in sorted(sel.kept_by_category.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {n:>7}  {cat}")

    dropped = sorted(sel.dropped_by_category.items(), key=lambda kv: -kv[1])
    lines += [
        "",
        f"  dropped, by category ({len(sel.dropped_by_category)} categories,"
        f" largest {top}):",
    ]
    for cat, n in dropped[:top]:
        lines.append(f"    {n:>7}  {cat}")
    if len(dropped) > top:
        lines.append(f"    {'':>7}  ... and {len(dropped) - top} more")
    return lines


def allowlist_delta(add=(), drop=()):
    """A `keep` predicate for an allowlist EXPERIMENT.

    Returns a predicate, plus the description printed alongside every result
    it produces. An experimental selection must never be mistaken for the
    published one, so the caller is expected to print the description and to
    refuse to publish when it is non-empty.
    """
    add, drop = set(add), set(drop)
    unknown_drop = drop - L.INCLUDE_CATEGORIES
    overlap = add & L.INCLUDE_CATEGORIES

    def keep(category: str, name: str) -> bool:
        if category in drop:
            return False
        if category in add:
            return not L.is_structural(category, name)
        return L.is_wanted(category, name)

    notes = []
    if add:
        notes.append(f"ADDED to allowlist: {', '.join(sorted(add))}")
    if drop:
        notes.append(f"REMOVED from allowlist: {', '.join(sorted(drop))}")
    # Surfaced rather than silently ignored: a typo in a category name would
    # otherwise produce a run that looks like a clean experiment and changed
    # nothing.
    if unknown_drop:
        notes.append(
            "WARNING: asked to remove categories that are not in the "
            f"allowlist, so removing them changes nothing: "
            f"{', '.join(sorted(unknown_drop))}")
    if overlap:
        notes.append(
            "WARNING: asked to add categories already in the allowlist, so "
            f"adding them changes nothing: {', '.join(sorted(overlap))}")
    return keep, notes
