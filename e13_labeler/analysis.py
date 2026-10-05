"""
The agreement report (requirements FR-37, FR-38, FR-41 subset, FR-42, FR-48).

Input: agreement records, the minimal per-annotation fields α needs. The
agreement export ships exactly these records, so offline analysis can call
``report`` on them and get identical numbers (FR-48). Like ``agreement``, this
module uses only the standard library.

Which annotations count where:
- inter-rater α: human, not skipped, blind batches only (no model answer shown,
  FR-34), no gold probes (FR-28), no re-label batches. Items need two or more
  such labels; single-labelled items drop out as unpairable.
- intra-rater α: a labeler's re-label annotation paired with their own label of
  the same item in the source batch. Reported separately, never pooled.
- human vs model: per model pseudo-labeler (FR-9), one unit per (item, human)
  with that human's value and the model's value.
- model vs model: per pair of models, one unit per item.
"""

import statistics
from itertools import combinations
from typing import Iterable, Mapping, Sequence

from .agreement import by_item, by_item_and_labeler, describe, reason_alphas
from .reasons import CANDIDATES, ESTABLISHED, REASONS

AGREEMENT_SCHEMA = "e13.agreement/1"
RECORD_KEYS = ("item_id", "labeler", "labeler_kind", "batch", "relabel_of", "blind", "gold_probe",
               "skipped", "answerable", "reasons")


def agreement_record(record: Mapping) -> dict:
    """Reduce a records.load_annotations row to what α needs."""
    return {
        "item_id": record["item_id"], "labeler": record["labeler"], "labeler_kind": record["labeler_kind"],
        "batch": record["batch"], "relabel_of": record["_relabel_of"], "blind": record["_blind"],
        "gold_probe": record["_gold_probe"], "skipped": record["skipped"],
        "answerable": record["answerable"], "reasons": record["reasons"],
    }


def _usable(r: Mapping) -> bool:
    return not r["skipped"] and not r["gold_probe"] and r["blind"]


def _with_section(units_fn_records: list, reasons: Sequence[str], n_boot: int, seed: int, unit_key) -> dict:
    return reason_alphas(units_fn_records, reasons, n_boot=n_boot, seed=seed, unit_key=unit_key)


def candidate_comparison(per_reason: Mapping) -> dict:
    """FR-41 / §7.2: each candidate's α against the min and median of the established reasons' α."""
    established = [per_reason[r]["alpha"] for r in ESTABLISHED if r in per_reason and per_reason[r]["alpha"] is not None]
    ref = {"min": min(established) if established else None,
           "median": statistics.median(established) if established else None,
           "n_established": len(established)}
    out = {}
    for c in sorted(CANDIDATES):
        if c not in per_reason:
            continue
        a = per_reason[c]["alpha"]
        out[c] = {"alpha": a, "ci95": per_reason[c]["ci95"], **ref,
                  "below_min": None if a is None or ref["min"] is None else a < ref["min"],
                  "vs_median": None if a is None or ref["median"] is None else a - ref["median"]}
    return out


def _pair_units_records(left: list, right_by_item: Mapping, left_key) -> list:
    """Records for pairwise units: each left record with its counterpart, keyed so they share a unit."""
    out = []
    for r in left:
        other = right_by_item.get(r["item_id"])
        if other is None:
            continue
        key = left_key(r)
        out.append({**r, "_unit": key})
        out.append({**other, "_unit": key})
    return out


def _by_unit(r: Mapping):
    return r["_unit"]


def report(records: Iterable[Mapping], reasons: Sequence[str] = REASONS, n_boot: int = 1000, seed: int = 0) -> dict:
    """Every agreement number the dashboard and the agreement export show."""
    records = [dict(r) for r in records]
    usable = [r for r in records if _usable(r)]
    humans = [r for r in usable if r["labeler_kind"] == "human"]
    first_pass = [r for r in humans if r["relabel_of"] is None]
    models = sorted({r["labeler"] for r in usable if r["labeler_kind"] == "model"})

    # Inter-rater (FR-37, FR-38 any abstain)
    inter = _with_section(first_pass, reasons, n_boot, seed, by_item)
    counts: dict = {}
    for r in first_pass:
        counts.setdefault(r["item_id"], set()).add(r["labeler"])
    pairable_items = sorted(i for i, ls in counts.items() if len(ls) >= 2)
    result = {
        "schema": AGREEMENT_SCHEMA,
        "inter_rater": {
            "per_reason": {r: inter[r] for r in reasons},
            "any_abstain": inter["any_abstain"],
            "candidates": candidate_comparison(inter),
            "n_items_pairable": len(pairable_items),
            "labelers": sorted({r["labeler"] for r in first_pass if r["item_id"] in set(pairable_items)}),
        },
    }

    # Intra-rater: re-label pass vs the same labeler's first pass
    first_by_key = {(r["batch"], r["item_id"], r["labeler"]): r for r in first_pass}
    intra_records = []
    for r in humans:
        if r["relabel_of"] is None:
            continue
        original = first_by_key.get((r["relabel_of"], r["item_id"], r["labeler"]))
        if original:
            intra_records += [original, r]
    intra = _with_section(intra_records, reasons, n_boot, seed, by_item_and_labeler)
    result["intra_rater"] = {"per_reason": {r: intra[r] for r in reasons}, "any_abstain": intra["any_abstain"],
                             "n_pairs": len(intra_records) // 2}

    # Human vs each model (FR-38, E13 step 2)
    hvm = {}
    for m in models:
        model_by_item = {r["item_id"]: r for r in usable if r["labeler"] == m}
        recs = _pair_units_records(first_pass, model_by_item, lambda r: (r["item_id"], r["labeler"]))
        if recs:
            res = _with_section(recs, reasons, n_boot, seed, _by_unit)
            hvm[m] = {"per_reason": {r: res[r] for r in reasons}, "any_abstain": res["any_abstain"],
                      "n_pairs": len(recs) // 2}
    result["human_vs_model"] = hvm

    # Model vs model (prompt A vs prompt B)
    mvm = {}
    for a, b in combinations(models, 2):
        a_items = [r for r in usable if r["labeler"] == a]
        b_by_item = {r["item_id"]: r for r in usable if r["labeler"] == b}
        recs = _pair_units_records(a_items, b_by_item, lambda r: r["item_id"])
        if recs:
            res = _with_section(recs, reasons, n_boot, seed, _by_unit)
            mvm[f"{a}|{b}"] = {"per_reason": {r: res[r] for r in reasons}, "any_abstain": res["any_abstain"],
                               "n_pairs": len(recs) // 2}
    result["model_vs_model"] = mvm
    result["params"] = {"n_boot": n_boot, "seed": seed, "reasons": list(reasons)}
    return result

