from harness_eval.stats import (
    ACTIVATION_FAIL,
    ACTIVATION_PASS,
    CONTAMINATED,
    INCONCLUSIVE,
    LOAD_BEARING,
    NO_DATA,
    NO_OP,
    ArmStats,
    classify,
    classify_activation,
    fisher_exact_greater,
    min_n_for_significance,
)


def test_activation_gate_is_deterministic_not_significance_based():
    assert classify_activation(ArmStats(1, 1), ArmStats(1, 0)).label == ACTIVATION_PASS
    assert classify_activation(ArmStats(1, 0), ArmStats(1, 0)).label == ACTIVATION_FAIL
    assert classify_activation(ArmStats(1, 1), ArmStats(1, 1)).label == CONTAMINATED


def test_fisher_perfect_separation_matches_hypergeometric():
    # 3/3 vs 0/3 -> 1 / C(6,3) = 1/20
    assert fisher_exact_greater(3, 0, 0, 3) == 1 / 20


def test_fisher_no_effect_is_not_significant():
    assert fisher_exact_greater(3, 3, 3, 3) > 0.05


def test_fisher_is_bounded():
    assert fisher_exact_greater(0, 0, 0, 0) == 1.0
    assert 0.0 <= fisher_exact_greater(6, 0, 0, 6) <= 1.0


def test_min_n_for_significance_at_default_alpha():
    assert min_n_for_significance(0.05) == 3


def test_clean_separation_is_load_bearing():
    v = classify(ArmStats(6, 6), ArmStats(6, 0))
    assert v.label == LOAD_BEARING
    assert v.delta == 1.0


def test_control_matching_treatment_is_a_no_op():
    v = classify(ArmStats(6, 6), ArmStats(6, 6))
    assert v.label == NO_OP


def test_control_beating_treatment_is_a_no_op():
    v = classify(ArmStats(6, 3), ArmStats(6, 5))
    assert v.label == NO_OP
    assert v.delta < 0


def test_weak_positive_effect_stays_inconclusive():
    # A rule must not be deleted on the strength of an underpowered run.
    v = classify(ArmStats(3, 3), ArmStats(3, 2))
    assert v.label == INCONCLUSIVE


def test_empty_arm_reports_no_data():
    assert classify(ArmStats(0, 0), ArmStats(3, 0)).label == NO_DATA


def test_two_failing_arms_are_ineffective_not_a_no_op():
    # Saying "the model already does this without the rule" about a 0/6
    # treatment arm would be false: neither arm reaches the behaviour.
    from harness_eval.stats import INEFFECTIVE
    assert classify(ArmStats(6, 0), ArmStats(6, 0)).label == INEFFECTIVE
    assert classify(ArmStats(6, 1), ArmStats(6, 1)).label == INEFFECTIVE


def test_compliant_equal_arms_are_still_a_no_op():
    assert classify(ArmStats(6, 6), ArmStats(6, 6)).label == NO_OP


def test_compliance_floor_is_configurable():
    from harness_eval.stats import INEFFECTIVE
    assert classify(ArmStats(6, 4), ArmStats(6, 4), compliance_floor=0.9).label == INEFFECTIVE
    assert classify(ArmStats(6, 4), ArmStats(6, 4), compliance_floor=0.5).label == NO_OP
