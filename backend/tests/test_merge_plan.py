"""Unit tests for `_merge_plan`, order-independent regardless of set hash order.

Regression test for a bug where an absolute-count code (e.g. BASO_ABS) could be
iterated before its percentage sibling (BASO), causing it to appear both as its
own standalone plan entry AND as the percentage row's secondary.
"""

from app.routers.documents import _merge_plan

PAIRS = [
    ("NEUT", "NEUT_ABS"),
    ("LYMPH", "LYMPH_ABS"),
    ("MONO", "MONO_ABS"),
    ("EOS", "EOS_ABS"),
    ("BASO", "BASO_ABS"),
]


def test_merge_plan_pairs_never_duplicated():
    available = {code for pair in PAIRS for code in pair}
    plan = _merge_plan(available)

    plan_codes = [code for code, _ in plan]
    # Every code appears exactly once across the whole plan.
    assert len(plan_codes) == len(set(plan_codes))

    plan_by_code = dict(plan)
    for pct_code, abs_code in PAIRS:
        # The absolute code never appears as its own (code, None) entry.
        assert abs_code not in plan_by_code
        # The percentage code's entry pairs with the absolute code.
        assert plan_by_code[pct_code] == abs_code


def test_merge_plan_unpaired_codes_stand_alone():
    available = {"GLU", "NEUT", "NEUT_ABS"}
    plan = dict(_merge_plan(available))
    assert plan["GLU"] is None
    assert plan["NEUT"] == "NEUT_ABS"
    assert "NEUT_ABS" not in plan


def test_merge_plan_absolute_without_percentage_stands_alone():
    available = {"NEUT_ABS"}
    plan = dict(_merge_plan(available))
    assert plan["NEUT_ABS"] is None
