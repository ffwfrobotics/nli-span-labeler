"""
Tests for the shared α module against the reference ``krippendorff`` package (FR-40).
"""
import random

import krippendorff
import numpy as np
import pytest

from e13_labeler.agreement import (
    alpha_nominal,
    any_abstain_units,
    bootstrap_ci,
    describe,
    reason_alphas,
    reason_units,
)


def reference_alpha(units, n_coders):
    """The reference package wants a coders × units matrix with NaN for missing values."""
    domain = sorted({v for u in units for v in u if v is not None}, key=repr)
    code = {v: i for i, v in enumerate(domain)}
    matrix = np.full((n_coders, len(units)), np.nan)
    for j, unit in enumerate(units):
        for i, v in enumerate(unit):
            if v is not None:
                matrix[i, j] = code[v]
    return krippendorff.alpha(reliability_data=matrix, level_of_measurement="nominal")


def random_units(rng, n_units, n_coders, domain, p_missing, p_agree=0.7):
    units = []
    for _ in range(n_units):
        truth = rng.choice(domain)
        unit = []
        for _ in range(n_coders):
            if rng.random() < p_missing:
                unit.append(None)
            else:
                unit.append(truth if rng.random() < p_agree else rng.choice(domain))
        units.append(unit)
    return units


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize("domain", [[True, False], ["a", "b", "c", "d"]])
def test_matches_reference(seed, domain):
    rng = random.Random(seed)
    n_coders = rng.choice([2, 3, 5])
    units = random_units(rng, rng.randint(20, 120), n_coders, domain, p_missing=rng.choice([0, 0.2, 0.5]))
    assert alpha_nominal(units) == pytest.approx(reference_alpha(units, n_coders), abs=1e-6)


def test_krippendorff_worked_example():
    """Krippendorff (2011), nominal example: 4 coders × 12 units with missing values, α = 0.743."""
    data = [
        [1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None],
        [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3],
        [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None],
        [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None],
    ]
    units = [list(col) for col in zip(*data)]
    assert alpha_nominal(units) == pytest.approx(0.743, abs=1e-3)


def test_perfect_agreement_and_undefined():
    assert alpha_nominal([[True, True], [False, False]]) == pytest.approx(1.0)
    # Only one value in use: α is undefined
    assert alpha_nominal([[False, False], [False, False]]) is None
    # Nothing pairable
    assert alpha_nominal([[True], [None, False]]) is None


def test_null_is_missing_not_absent():
    """§5.1: a reason not asked (None) is missing data; it must not count as False."""
    asked = [[True, True], [False, False], [True, False]]
    with_unasked = [u + [None] for u in asked]
    assert alpha_nominal(with_unasked) == pytest.approx(alpha_nominal(asked))


def test_bootstrap_ci_is_seeded_and_brackets_alpha():
    rng = random.Random(1)
    units = random_units(rng, 200, 2, [True, False], p_missing=0, p_agree=0.85)
    a = alpha_nominal(units)
    lo, hi = bootstrap_ci(units, n_boot=1000, seed=7)
    assert lo < a < hi
    assert bootstrap_ci(units, n_boot=1000, seed=7) == (lo, hi)


def test_describe_and_low_prevalence_warning():
    units = [[True, True]] * 2 + [[False, False]] * 98
    d = describe(units, n_boot=100)
    assert d["n_items"] == 100 and d["n_values"] == 200 and d["n_positive"] == 4
    assert d["prevalence"] == pytest.approx(0.02)
    assert d["unstable"] is True  # FR-42: fewer than 30 positives


def test_reason_alphas_from_annotations():
    anns = [
        {"item_id": "x#q", "answerable": False, "reasons": {"unrelated": True, "subjective": None}},
        {"item_id": "x#q", "answerable": False, "reasons": {"unrelated": True, "subjective": None}},
        {"item_id": "y#q", "answerable": True, "reasons": {"unrelated": False, "subjective": None}},
        {"item_id": "y#q", "answerable": False, "reasons": {"unrelated": False, "subjective": None}},
        {"item_id": "z#q", "skipped": "broken_item", "answerable": None, "reasons": None},
    ]
    assert reason_units(anns, "unrelated") == [[True, True], [False, False]]
    assert any_abstain_units(anns) == [[True, True], [False, True]]
    result = reason_alphas(anns, ["unrelated", "subjective"], n_boot=50)
    assert result["unrelated"]["alpha"] == pytest.approx(1.0)
    assert result["subjective"]["alpha"] is None and result["subjective"]["n_items"] == 0
    assert set(result) == {"unrelated", "subjective", "any_abstain"}


def test_module_has_no_web_dependencies():
    """FR-40: offline analysis can import the α module without FastAPI."""
    import ast
    import inspect

    import e13_labeler.agreement as mod

    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(ast.parse(inspect.getsource(mod)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {"random", "collections", "itertools", "typing", "math"}
