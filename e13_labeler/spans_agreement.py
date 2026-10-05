"""
Span agreement between labelers (requirements FR-39) and per-labeler QA
numbers (FR-41, §7.3). Standard library only, so offline analysis can import
it like ``agreement``.

Units are words, as the UI snaps to them (E07 finding 2). A span covers every
word it overlaps. A unit is ``"<where>|<word index>"``, where ``<where>`` is
``state``, ``state<pointer>`` for a JSON string value, or ``option:<key>``.

- token F1 and Jaccard: per pair of labelers on an item, per span role, and
  per triggering reason (only pairs where both checked the reason).
- AP (E07 style), where an item has three or more labelers: each labeler's
  state words in turn are the truth, scored by the share of the *other*
  labelers who marked each word; the item's whole state is the candidate set.
  To check against E07's own implementation (ROADMAP).
"""

import json
import re
import statistics
from collections import defaultdict
from itertools import combinations
from typing import Iterable, Mapping, Optional, Sequence

from .reasons import REASONS

WORD = re.compile(r"\w+(?:['’]\w+)*")
ROLES = ("support", "refute", "unsupported", "framing")
FAST_MS = 5000          # §7.3: median active time under 5 s is flagged
PREVALENCE_FACTOR = 3   # §7.3: a reason more than 3x as frequent as the batch rate
MIN_POSITIVES = 3       # ... once the labeler has checked it at least this often


# ============================================================================
# Words
# ============================================================================

def words(text: str) -> list[tuple[int, int]]:
    """(start, end) code-point offsets of the words in ``text``."""
    return [(m.start(), m.end()) for m in WORD.finditer(text)]


def _pointer_value(doc, pointer: str):
    value = doc
    for raw in pointer[1:].split("/") if pointer else []:
        token = raw.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _strings(doc, pointer: str = ""):
    """Every string value in a JSON document with its pointer."""
    if isinstance(doc, str):
        yield pointer, doc
    elif isinstance(doc, dict):
        for k, v in doc.items():
            yield from _strings(v, f"{pointer}/{str(k).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            yield from _strings(v, f"{pointer}/{i}")


def state_universe(state: str, state_format: str) -> list[str]:
    """All word units of a state: the candidate set for AP."""
    if state_format == "json":
        try:
            doc = json.loads(state)
        except ValueError:
            return []
        return [f"state{p}|{i}" for p, s in _strings(doc) for i, _ in enumerate(words(s))]
    return [f"state|{i}" for i, _ in enumerate(words(state))]


def span_units(span: Mapping, state: str, state_format: str, question: Mapping) -> list[str]:
    """The word units a span covers (see the module docstring)."""
    if span["side"] == "option":
        criteria = question.get("criteria")
        option = span.get("option")
        if question.get("type") == "score":
            target = criteria[int(option)] if criteria else None
        else:
            target = (criteria or {}).get(option) if isinstance(criteria, dict) else None
        where = f"option:{option}"
    elif state_format == "json":
        try:
            target = _pointer_value(json.loads(state), span.get("pointer") or "")
        except (ValueError, KeyError, IndexError, TypeError):
            return []
        where = f"state{span.get('pointer') or ''}"
    else:
        target, where = state, "state"
    if not isinstance(target, str):
        return [f"{where}|value"]  # a bare pointer to a number, key, object or array
    start, end = span.get("start"), span.get("end")
    if start is None:
        start, end = 0, len(target)  # a bare pointer marks the whole string
    return [f"{where}|{i}" for i, (s, e) in enumerate(words(target)) if s < end and e > start]


# ============================================================================
# Pairwise F1 / Jaccard and AP
# ============================================================================

def _f1(a: set, b: set) -> float:
    return 2 * len(a & b) / (len(a) + len(b))


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b)


def _summary(pairs: list[tuple[set, set]]) -> dict:
    """Mean per-pair F1 and Jaccard over pairs where at least one side marked something, plus pooled F1."""
    pairs = [(a, b) for a, b in pairs if a or b]
    if not pairs:
        return {"n_pairs": 0, "f1": None, "jaccard": None, "f1_pooled": None}
    inter = sum(len(a & b) for a, b in pairs)
    total = sum(len(a) + len(b) for a, b in pairs)
    return {"n_pairs": len(pairs), "f1": round(statistics.mean(_f1(a, b) for a, b in pairs), 4),
            "jaccard": round(statistics.mean(_jaccard(a, b) for a, b in pairs), 4),
            "f1_pooled": round(2 * inter / total, 4)}


def average_precision(scores: Mapping[str, float], truth: set) -> Optional[float]:
    """AP of ranking ``scores`` (over the candidate set) against ``truth``; ties share a threshold."""
    if not truth:
        return None
    ap, hits, seen, prev_recall = 0.0, 0, 0, 0.0
    by_score = defaultdict(list)
    for unit, s in scores.items():
        by_score[s].append(unit)
    for s in sorted(by_score, reverse=True):
        group = by_score[s]
        seen += len(group)
        hits += sum(u in truth for u in group)
        recall = hits / len(truth)
        ap += (recall - prev_recall) * (hits / seen)
        prev_recall = recall
    return ap


def span_agreement(records: Sequence[Mapping], universe: Mapping[str, list]) -> dict:
    """
    FR-39 over records that carry ``span_units``: [{role, reasons, units}] per
    annotation. ``records`` should be the inter-rater set (human, blind, first pass).
    """
    by_item = defaultdict(list)
    for r in records:
        if r.get("span_units") is not None and not r.get("skipped"):
            by_item[r["item_id"]].append(r)
    per_role = {role: [] for role in ROLES}
    per_reason = {reason: [] for reason in REASONS}
    any_role = []
    ap_all, ap_role = [], {role: [] for role in ("support", "refute")}

    def units(r, pred):
        return {u for s in r["span_units"] if pred(s) for u in s["units"]}

    for item_id, recs in by_item.items():
        for a, b in combinations(recs, 2):
            any_role.append((units(a, lambda s: True), units(b, lambda s: True)))
            for role in ROLES:
                per_role[role].append((units(a, lambda s, r=role: s["role"] == r),
                                       units(b, lambda s, r=role: s["role"] == r)))
            for reason in REASONS:
                if (a["reasons"] or {}).get(reason) and (b["reasons"] or {}).get(reason):
                    per_reason[reason].append((units(a, lambda s, x=reason: x in s["reasons"]),
                                               units(b, lambda s, x=reason: x in s["reasons"])))
        cand = universe.get(item_id) or []
        if len(recs) < 3 or not cand:
            continue
        for i, held in enumerate(recs):
            others = recs[:i] + recs[i + 1:]
            for role, sink in ((None, ap_all), *[(r, ap_role[r]) for r in ap_role]):
                pred = (lambda s: s["role"] == role) if role else (lambda s: True)
                truth = {u for u in units(held, pred) if u.startswith("state")}
                votes = [units(o, pred) for o in others]
                scores = {u: sum(u in v for v in votes) / len(votes) for u in cand}
                ap = average_precision(scores, truth & set(cand))
                if ap is not None:
                    sink.append(ap)

    def ap_summary(values):
        return {"n": len(values), "ap": round(statistics.mean(values), 4) if values else None}

    return {
        "any_role": _summary(any_role),
        "per_role": {role: _summary(p) for role, p in per_role.items()},
        "per_reason": {reason: _summary(p) for reason, p in per_reason.items()},
        "ap": {"any_role": ap_summary(ap_all), **{r: ap_summary(v) for r, v in ap_role.items()},
               "min_labelers": 3},
    }


# ============================================================================
# Reason confusion, pairwise agreement, gold accuracy, monitoring (FR-41, §7.3)
# ============================================================================

def answer_of(r: Mapping) -> frozenset:
    if r.get("answerable"):
        return frozenset({"answerable"})
    return frozenset(k for k, v in (r.get("reasons") or {}).items() if v)


def confusion(records: Sequence[Mapping]) -> dict:
    """
    Between pairs of labelers on the same item: when A checks x and not y while B
    checks y and not x, the pair (x, y) counts once (symmetric). That shows
    dilution, e.g. stale_state <-> not_enough_info. Also within-labeler
    co-occurrence of reasons.
    """
    labels = ("answerable", *REASONS)
    conf = {a: {b: 0 for b in labels} for a in labels}
    cooc = {a: {b: 0 for b in labels} for a in labels}
    by_item = defaultdict(list)
    for r in records:
        by_item[r["item_id"]].append(answer_of(r))
        s = sorted(answer_of(r))
        for x, y in combinations(s, 2):
            cooc[x][y] += 1
            cooc[y][x] += 1
    for answers in by_item.values():
        for a, b in combinations(answers, 2):
            for x in a - b:
                for y in b - a:
                    conf[x][y] += 1
                    conf[y][x] += 1
    top = sorted(({"a": x, "b": y, "count": conf[x][y]} for x, y in combinations(labels, 2) if conf[x][y]),
                 key=lambda d: (-d["count"], d["a"], d["b"]))
    return {"labels": list(labels), "confusion": conf, "top": top, "cooccurrence": cooc}


def pairwise(records: Sequence[Mapping]) -> dict:
    """
    Per labeler pair: shared items, exact agreement on the answer set, mean
    Jaccard. Per labeler: exact agreement over all their pair comparisons
    (``n_items`` counts comparisons, so an item labelled by three people counts twice).
    """
    by_item = defaultdict(dict)
    for r in records:
        by_item[r["item_id"]][r["labeler"]] = answer_of(r)
    stats = defaultdict(lambda: [0, 0, 0.0])
    for answers in by_item.values():
        for a, b in combinations(sorted(answers), 2):
            x, y = answers[a], answers[b]
            s = stats[(a, b)]
            s[0] += 1
            s[1] += x == y
            s[2] += len(x & y) / len(x | y) if x | y else 1.0
    pairs = [{"a": a, "b": b, "n_items": n, "exact": round(e / n, 4), "jaccard": round(j / n, 4)}
             for (a, b), (n, e, j) in sorted(stats.items())]
    per_labeler = {}
    for lab in sorted({r["labeler"] for r in records}):
        mine = [p for p in pairs if lab in (p["a"], p["b"])]
        n = sum(p["n_items"] for p in mine)
        per_labeler[lab] = {"n_items": n, "exact": round(sum(p["exact"] * p["n_items"] for p in mine) / n, 4)
                            if n else None}
    return {"pairs": pairs, "per_labeler": per_labeler}


def gold_accuracy(probes: Iterable[Mapping]) -> dict:
    """Per labeler accuracy on hidden gold, overall and per reason (FR-41, §7.1)."""
    from .quality import score

    out: dict = {}
    for p in probes:
        s = score(frozenset(p["answer"]), p["gold"])
        d = out.setdefault(p["labeler"], {"n_probes": 0, "correct": 0, "per_reason": defaultdict(int)})
        d["n_probes"] += 1
        d["correct"] += s["correct"]
        for r, ok in s["per_reason"].items():
            d["per_reason"][r] += ok
    return {lab: {"n_probes": d["n_probes"], "accuracy": round(d["correct"] / d["n_probes"], 4),
                  "per_reason": {r: round(n / d["n_probes"], 4) for r, n in d["per_reason"].items()}}
            for lab, d in sorted(out.items())}


def monitoring(records: Sequence[Mapping]) -> list[dict]:
    """
    §7.3 daily checks over human first-pass labels: median active time under
    5 s, and a reason checked more than 3x as often as the batch rate.
    """
    flags = []
    by_labeler = defaultdict(list)
    for r in records:
        by_labeler[r["labeler"]].append(r)
    for lab, recs in sorted(by_labeler.items()):
        times = [r["active_ms"] for r in recs if r.get("active_ms") is not None]
        if times and statistics.median(times) < FAST_MS:
            flags.append({"labeler": lab, "kind": "fast", "median_active_ms": statistics.median(times),
                          "n": len(times)})
    by_batch = defaultdict(list)
    for r in records:
        by_batch[r["batch"]].append(r)
    for batch, recs in sorted(by_batch.items(), key=lambda kv: str(kv[0])):
        for reason in REASONS:
            asked = [r for r in recs if (r["reasons"] or {}).get(reason) is not None]
            if not asked:
                continue
            batch_rate = sum(bool(r["reasons"][reason]) for r in asked) / len(asked)
            for lab in sorted({r["labeler"] for r in asked}):
                mine = [r for r in asked if r["labeler"] == lab]
                pos = sum(bool(r["reasons"][reason]) for r in mine)
                rate = pos / len(mine)
                if pos >= MIN_POSITIVES and rate > PREVALENCE_FACTOR * batch_rate:
                    flags.append({"labeler": lab, "kind": "prevalence", "batch": batch, "reason": reason,
                                  "rate": round(rate, 4), "batch_rate": round(batch_rate, 4), "n": len(mine)})
    return flags
