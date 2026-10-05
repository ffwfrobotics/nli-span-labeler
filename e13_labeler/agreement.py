"""
Agreement statistics for E13: Krippendorff's α with bootstrap confidence intervals.

This is the single α implementation shared by the app and the offline E13
analysis (requirements FR-40). It uses only the standard library, so analysis
scripts can import it without the web stack:

    from e13_labeler.agreement import alpha_nominal, reason_alphas

Conventions:
- A *unit* is one item. Its values are the labels its labelers gave, with
  ``None`` for "not asked" (requirements §5.1: a reason outside the batch's
  ``reason_set`` is missing data, not "absent").
- Only units with at least two non-missing values are pairable, as in
  Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability".
"""

import random
from collections import Counter
from itertools import permutations
from typing import Hashable, Iterable, Mapping, Optional, Sequence

Unit = Sequence[Optional[Hashable]]


def _unit_coincidences(values: Iterable[Optional[Hashable]]) -> Optional[Counter]:
    """One unit's contribution to the coincidence matrix, or None if it isn't pairable."""
    present = [v for v in values if v is not None]
    m = len(present)
    if m < 2:
        return None
    contribution = Counter()
    for a, b in permutations(present, 2):
        contribution[(a, b)] += 1 / (m - 1)
    return contribution


def _alpha_from_coincidences(o: Mapping) -> Optional[float]:
    """Nominal α from a coincidence matrix; None when undefined (fewer than two values in use)."""
    n_c = Counter()
    for (a, _b), w in o.items():
        n_c[a] += w
    n = sum(n_c.values())
    if n <= 1:
        return None
    observed = sum(w for (a, b), w in o.items() if a != b)
    expected = (n * n - sum(v * v for v in n_c.values())) / (n - 1)
    if expected <= 1e-12:
        return None
    return 1.0 - observed / expected


def alpha_nominal(units: Iterable[Unit]) -> Optional[float]:
    """Krippendorff's α for nominal data. ``units``: one sequence of values per item."""
    total = Counter()
    for unit in units:
        contribution = _unit_coincidences(unit)
        if contribution:
            total.update(contribution)
    return _alpha_from_coincidences(total)


def bootstrap_ci(
    units: Sequence[Unit], n_boot: int = 1000, seed: int = 0, level: float = 0.95
) -> Optional[tuple[float, float]]:
    """
    Percentile bootstrap CI for nominal α, resampling units with replacement
    (requirements FR-37: 1,000 resamples over items). Seeded, so reruns are identical.
    """
    contributions = [c for c in (_unit_coincidences(u) for u in units) if c]
    if len(contributions) < 2:
        return None
    rng = random.Random(seed)
    k = len(contributions)
    samples = []
    for _ in range(n_boot):
        total = Counter()
        for _ in range(k):
            total.update(contributions[rng.randrange(k)])
        value = _alpha_from_coincidences(total)
        if value is not None:
            samples.append(value)
    if not samples:
        return None
    samples.sort()
    lo_idx = int(round((1 - level) / 2 * (len(samples) - 1)))
    hi_idx = int(round((1 + level) / 2 * (len(samples) - 1)))
    return samples[lo_idx], samples[hi_idx]


def describe(units: Sequence[Unit], n_boot: int = 1000, seed: int = 0) -> dict:
    """
    α with the context the dashboard reports (FR-37): items, pairable values,
    prevalence of ``True``, positives, CI and the low-prevalence warning (FR-42).
    """
    pairable = [[v for v in u if v is not None] for u in units]
    pairable = [u for u in pairable if len(u) >= 2]
    values = [v for u in pairable for v in u]
    positives = sum(1 for v in values if v is True)
    prevalence = positives / len(values) if values else None
    return {
        "alpha": alpha_nominal(pairable),
        "ci95": bootstrap_ci(pairable, n_boot=n_boot, seed=seed),
        "n_items": len(pairable),
        "n_values": len(values),
        "n_positive": positives,
        "prevalence": prevalence,
        "unstable": positives < 30 or (prevalence is not None and prevalence < 0.03),
    }


def reason_units(annotations: Iterable[Mapping], reason: str) -> list[list]:
    """
    Group annotations by item into units for one reason. Each annotation is a
    mapping with ``item_id`` and ``reasons`` ({reason: True | False | None}),
    as in the §5.4 export. Pass only the latest version per (item, labeler).
    """
    by_item: dict[str, list] = {}
    for ann in annotations:
        if ann.get("skipped"):
            continue
        value = (ann.get("reasons") or {}).get(reason)
        by_item.setdefault(ann["item_id"], []).append(value)
    return list(by_item.values())


def any_abstain_units(annotations: Iterable[Mapping]) -> list[list]:
    """Units for "any abstain" (FR-38): True when the labeler did not choose answerable."""
    by_item: dict[str, list] = {}
    for ann in annotations:
        if ann.get("skipped") or ann.get("answerable") is None:
            continue
        by_item.setdefault(ann["item_id"], []).append(not ann["answerable"])
    return list(by_item.values())


def reason_alphas(
    annotations: Sequence[Mapping], reasons: Sequence[str], n_boot: int = 1000, seed: int = 0
) -> dict:
    """Per-reason α (FR-37) plus "any abstain" (FR-38) for a set of annotations."""
    result = {r: describe(reason_units(annotations, r), n_boot=n_boot, seed=seed) for r in reasons}
    result["any_abstain"] = describe(any_abstain_units(annotations), n_boot=n_boot, seed=seed)
    return result
