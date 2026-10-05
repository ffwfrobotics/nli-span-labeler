"""
M2 dashboard: span agreement (FR-39), the confusion panel, pairwise agreement
and gold accuracy per labeler (FR-41), and §7.3 monitoring. Fixture values are
worked by hand.
"""
import json

import pytest
from fastapi.testclient import TestClient

from e13_labeler import spans_agreement as sa
from e13_labeler.reasons import REASONS


def rec(item, labeler, reasons=(), answerable=False, spans=(), batch="b", active_ms=10000):
    return {"item_id": item, "labeler": labeler, "labeler_kind": "human", "batch": batch, "relabel_of": None,
            "blind": True, "gold_probe": False, "skipped": None, "answerable": answerable,
            "reasons": {r: (r in reasons) for r in REASONS}, "active_ms": active_ms,
            "span_units": [{"role": role, "reasons": list(rs), "units": list(units)} for role, rs, units in spans]}


class TestWords:
    def test_text_units(self):
        state = "Sales rose 4% in the first quarter."
        assert sa.words(state)[:3] == [(0, 5), (6, 10), (11, 12)]
        # "rose 4" with a partial word at the end: covers words 1 and 2
        assert sa.span_units({"side": "state", "start": 6, "end": 12}, state, "text", {}) == ["state|1", "state|2"]
        assert sa.span_units({"side": "state", "start": 7, "end": 8}, state, "text", {}) == ["state|1"]
        assert len(sa.state_universe(state, "text")) == 7

    def test_json_and_option_units(self):
        state = json.dumps({"alert": {"msg": "disk full on host", "n": 3}})
        span = {"side": "state", "pointer": "/alert/msg", "start": 5, "end": 9}
        assert sa.span_units(span, state, "json", {}) == ["state/alert/msg|1"]
        assert sa.span_units({"side": "state", "pointer": "/alert/n"}, state, "json", {}) == ["state/alert/n|value"]
        assert sa.state_universe(state, "json") == [f"state/alert/msg|{i}" for i in range(4)]
        q = {"type": "choice", "criteria": {"a": "The disk is full", "b": "Fine"}}
        assert sa.span_units({"side": "option", "option": "a", "start": 4, "end": 8}, state, "json", q) == ["option:a|1"]

    def test_code_points(self):
        """Offsets count code points, like the rest of the app (an emoji is one)."""
        state = "😀 big win"
        assert sa.span_units({"side": "state", "start": 2, "end": 5}, state, "text", {}) == ["state|0"]


class TestSpanAgreement:
    def test_f1_jaccard_fixture(self):
        a = rec("i", "L1", ["not_enough_info"], spans=[("support", ["not_enough_info"], ["state|1", "state|2", "state|3"])])
        b = rec("i", "L2", ["not_enough_info"], spans=[("support", ["not_enough_info"], ["state|2", "state|3", "state|4"])])
        out = sa.span_agreement([a, b], {})
        assert out["per_role"]["support"] == {"n_pairs": 1, "f1": 0.6667, "jaccard": 0.5, "f1_pooled": 0.6667}
        assert out["per_role"]["refute"]["n_pairs"] == 0  # neither marked one
        assert out["per_reason"]["not_enough_info"]["f1"] == 0.6667
        assert out["per_reason"]["ambiguous"]["n_pairs"] == 0

    def test_reason_needs_both_checked(self):
        a = rec("i", "L1", ["ambiguous"], spans=[("framing", ["ambiguous"], ["state|1"])])
        b = rec("i", "L2", ["subjective"])
        assert sa.span_agreement([a, b], {})["per_reason"]["ambiguous"]["n_pairs"] == 0

    def test_average_precision_fixture(self):
        # sklearn.average_precision_score([1,1,0,0,0], [1,.5,.5,0,0]) == 0.8333
        scores = {"u0": 1.0, "u1": 0.5, "u2": 0.5, "u3": 0.0, "u4": 0.0}
        assert sa.average_precision(scores, {"u0", "u1"}) == pytest.approx(0.5 + 0.5 * 2 / 3)
        assert sa.average_precision(scores, set()) is None

    def test_ap_needs_three_labelers(self):
        universe = {"i": ["state|0", "state|1", "state|2", "state|3"]}
        two = [rec("i", f"L{n}", spans=[("support", [], ["state|1"])]) for n in range(2)]
        assert sa.span_agreement(two, universe)["ap"]["any_role"]["n"] == 0
        three = [rec("i", f"L{n}", spans=[("support", [], ["state|1"])]) for n in range(3)]
        out = sa.span_agreement(three, universe)["ap"]
        assert out["any_role"] == {"n": 3, "ap": 1.0} and out["support"]["ap"] == 1.0


class TestConfusionAndLabelers:
    def test_confusion(self):
        recs = [rec("i1", "A", ["stale_state"]), rec("i1", "B", ["not_enough_info"]),
                rec("i2", "A", ["stale_state", "ambiguous"]), rec("i2", "B", ["not_enough_info", "ambiguous"]),
                rec("i3", "A", answerable=True, reasons=()), rec("i3", "B", ["subjective"])]
        out = sa.confusion(recs)
        assert out["confusion"]["stale_state"]["not_enough_info"] == 2
        assert out["confusion"]["not_enough_info"]["stale_state"] == 2
        assert out["confusion"]["answerable"]["subjective"] == 1
        assert out["top"][0] == {"a": "not_enough_info", "b": "stale_state", "count": 2}
        assert out["cooccurrence"]["ambiguous"]["stale_state"] == 1

    def test_pairwise(self):
        recs = [rec("i1", "A", ["unrelated"]), rec("i1", "B", ["unrelated"]),
                rec("i2", "A", ["unrelated", "ambiguous"]), rec("i2", "B", ["unrelated"])]
        out = sa.pairwise(recs)
        assert out["pairs"] == [{"a": "A", "b": "B", "n_items": 2, "exact": 0.5, "jaccard": 0.75}]
        assert out["per_labeler"]["A"] == {"n_items": 2, "exact": 0.5}

    def test_gold_accuracy(self):
        gold = {"answerable": False, "reasons": ["ambiguous"], "alternatives": {"ambiguous": ["underspecified"]}}
        probes = [{"labeler": "A", "item_id": "g1", "answer": ["underspecified"], "gold": gold},
                  {"labeler": "A", "item_id": "g2", "answer": ["subjective"], "gold": gold}]
        out = sa.gold_accuracy(probes)["A"]
        assert out["n_probes"] == 2 and out["accuracy"] == 0.5 and out["per_reason"]["ambiguous"] == 0.5

    def test_monitoring(self):
        fast = [rec(f"i{n}", "F", ["unrelated"], active_ms=2000) for n in range(5)]
        steady = [rec(f"j{n}", f"S{k}", ["subjective"] if (k == 0 and n < 4) else [], answerable=not (k == 0 and n < 4))
                  for k in range(4) for n in range(10)]
        flags = sa.monitoring(fast + steady)
        assert {"labeler": "F", "kind": "fast", "median_active_ms": 2000, "n": 5} in flags
        prev = [f for f in flags if f["kind"] == "prevalence" and f["reason"] == "subjective"]
        # S0 checks subjective on 4 of 10; the batch rate is 4/45 (F's 5 count too): 0.4 > 3 x 0.089
        assert [f["labeler"] for f in prev] == ["S0"] and prev[0]["rate"] == 0.4


class TestDashboardApi:
    def test_fields(self, owner_client: TestClient):
        """FR-41 test: the dashboard API returns the per-reason, candidate, confusion and labeler fields."""
        rep = owner_client.get("/api/admin/agreement?n_boot=0").json()
        assert set(rep["inter_rater"]) >= {"per_reason", "any_abstain", "candidates"}
        assert set(rep["confusion"]) == {"labels", "confusion", "top", "cooccurrence"}
        assert set(rep["labelers"]) == {"pairwise", "gold", "monitoring", "status"}
        assert set(rep["spans"]) == {"any_role", "per_role", "per_reason", "ap"}
        assert rep["labelers"]["status"]["L01"]["status"] == "active"
