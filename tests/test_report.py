from pathlib import Path

from harness_eval.model import Case, Check, Target
from harness_eval.report import CONVERT, DROP, KEEP, PASS, RERUN, recommend, summarize
from harness_eval.runner import CONTROL, TREATMENT, Trial
from harness_eval.stats import ArmStats, classify


def fake_case(case_id: str = "demo", deterministic_alternative: str = "",
              measurement: str = "ablation") -> Case:
    """A synthetic case so these tests do not depend on any local case corpus."""
    return Case(
        id=case_id,
        title=case_id,
        path=Path(f"{case_id}.toml"),
        engine="claude",
        target=Target(kind="rule", source=None, anchor=""),
        prompt="probe",
        checks=(Check(kind="contains", why="", pattern="x"),),
        measurement=measurement,
        deterministic_alternative=deterministic_alternative,
    )


def trials(case_id: str, model: str, treatment_passes: int, control_passes: int, n: int):
    out = []
    for i in range(n):
        out.append(Trial(case_id, TREATMENT, model, i, passed=i < treatment_passes))
        out.append(Trial(case_id, CONTROL, model, i, passed=i < control_passes))
    return out


def test_load_bearing_maps_to_keep():
    assert recommend(fake_case(), classify(ArmStats(6, 6), ArmStats(6, 0)))[0] == KEEP


def test_no_op_with_an_alternative_maps_to_convert():
    case = fake_case(deterministic_alternative="settings.json field")
    assert recommend(case, classify(ArmStats(6, 6), ArmStats(6, 6)))[0] == CONVERT


def test_no_op_without_an_alternative_maps_to_drop():
    assert recommend(fake_case(), classify(ArmStats(6, 6), ArmStats(6, 6)))[0] == DROP


def test_underpowered_run_asks_for_a_rerun_not_a_deletion():
    assert recommend(fake_case(), classify(ArmStats(2, 2), ArmStats(2, 0)))[0] == RERUN


def test_summarize_groups_by_case_and_model_and_ignores_errored_trials():
    cases = [fake_case("demo")]
    data = trials("demo", "sonnet", treatment_passes=6, control_passes=0, n=6)
    data.append(Trial("demo", TREATMENT, "sonnet", 99, passed=False, error="timeout"))
    summaries = summarize(cases, data)
    assert len(summaries) == 1
    s = summaries[0]
    assert s.verdict.treatment.n == 6  # the errored trial is excluded from the denominator
    assert s.errors == 1
    assert s.action == KEEP


def test_activation_case_maps_clean_n1_split_to_pass():
    case = fake_case("skill-demo", measurement="activation")
    data = trials(case.id, "gpt-5.6-terra", treatment_passes=1, control_passes=0, n=1)
    summary = summarize([case], data)[0]
    assert summary.verdict.label == "activation-pass"
    assert summary.action == PASS


def test_ineffective_asks_for_a_rewrite_not_a_deletion():
    from harness_eval.report import REWRITE
    assert recommend(fake_case(), classify(ArmStats(6, 0), ArmStats(6, 0)))[0] == REWRITE


def test_non_answer_detection():
    from harness_eval.report import looks_like_a_non_answer
    assert looks_like_a_non_answer("현재 프로젝트 구조를 먼저 확인하겠습니다.\n\nls -la")
    assert not looks_like_a_non_answer("1. 스키마 정의 → 검증: pytest\n2. " + "x" * 400)
    assert not looks_like_a_non_answer("바로 답한다: source .venv/bin/activate && pytest")
