"""
Data-licence tiers and labeler clearance (requirements §8).

An item carries two things (owner decision, 2026-10-05):

- ``permissions``, the **release tier** of its row's labels (RELEASE_POLICY.md §2):
  libre < restricted, jev < jev+restricted. ``restricted`` and ``jev`` are two
  independent flags; ``jev+restricted`` has both. Any Jev output on a row marks
  every item of the row ``jev``, so exports can filter Jev out.
- ``visibility``, who may **see** it: ``libre`` or ``restricted``, from the source
  text's licence alone. Labelers work blind and never see teacher outputs, so Jev
  output doesn't change visibility. The exception: a batch that shows a Jev
  answer (``show_model_answer``, FR-34) is internal-only for the items carrying it.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from . import config

TIERS = ("libre", "restricted", "jev", "jev+restricted")

VISIBILITIES = ("libre", "restricted")

# What each clearance may see, by item visibility.
CLEARANCE_VISIBILITY = {
    "public": ("libre",),
    "internal": VISIBILITIES,
}

# FR-55: sources that are eval-only and bar training; import refuses them.
# Seeded from DATASET_INVENTORY.md §2.2 and §2.7. Extend via E13_EVAL_ONLY_SOURCES.
EVAL_ONLY_NO_TRAINING = frozenset({"llm_aggrefact", "halubench"})

SOURCE_PERMISSIONS_PATH = config.REPO_DIR / "docs" / "e13" / "source_permissions.json"


def visible_to(clearance: str) -> tuple:
    """The item visibilities a clearance may see."""
    return CLEARANCE_VISIBILITY.get(clearance, ("libre",))


def visibility_of(tier: str) -> str:
    """Visibility from the text's licence: a tier's restricted flag; its jev flag doesn't count."""
    return "restricted" if _flags(tier)[0] else "libre"


def _flags(tier: str) -> tuple[bool, bool]:
    """(restricted, jev) for a tier."""
    if tier not in TIERS:
        raise ValueError(f"unknown permissions tier: {tier}")
    return ("restricted" in tier, tier.startswith("jev"))


def _tier(restricted: bool, jev: bool) -> str:
    if restricted and jev:
        return "jev+restricted"
    if jev:
        return "jev"
    if restricted:
        return "restricted"
    return "libre"


def max_tier(*tiers: str) -> str:
    """FR-56: an item's tier is the maximum of everything attached to it."""
    restricted = jev = False
    for t in tiers:
        r, j = _flags(t)
        restricted, jev = restricted or r, jev or j
    return _tier(restricted, jev)


def tier_at_most(tier: str, ceiling: str) -> bool:
    """True if ``tier`` is within ``ceiling`` (used for batch tier_ceiling)."""
    r, j = _flags(tier)
    cr, cj = _flags(ceiling)
    return (not r or cr) and (not j or cj)


def tiers_within(ceiling: str) -> tuple:
    return tuple(t for t in TIERS if tier_at_most(t, ceiling))


@lru_cache(maxsize=4)
def load_source_permissions(path: Optional[Path] = None) -> dict:
    """source -> {"class": libre|restricted|unverified, "licence": ...}"""
    data = json.loads(Path(path or SOURCE_PERMISSIONS_PATH).read_text())
    return data.get("sources", data)


def source_tier(source: str, permissions: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """
    FR-6 steps 2-3: the tier of a source and its licence note.
    ``unverified`` and unknown sources map to ``restricted``.
    """
    permissions = load_source_permissions() if permissions is None else permissions
    entry = permissions.get(source)
    if not entry:
        return "restricted", None
    cls = entry.get("class")
    return ("libre" if cls == "libre" else "restricted"), entry.get("licence")


def is_eval_only(source: str, extra: Iterable[str] = ()) -> bool:
    return source.lower() in EVAL_ONLY_NO_TRAINING or source.lower() in {s.lower() for s in extra}
