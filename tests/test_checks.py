import pytest

from harness_eval.checks import TrialContext, run_check, run_checks
from harness_eval.model import Check


def test_regex_absent_passes_when_pattern_missing():
    ctx = TrialContext(result="def ema(values, alpha):\n    # smoothing factor\n")
    assert run_check(Check("regex_absent", "no hangul", pattern="[가-힣]"), ctx).passed


def test_regex_absent_fails_on_hangul():
    ctx = TrialContext(result='"""지수 이동 평균"""')
    assert not run_check(Check("regex_absent", "no hangul", pattern="[가-힣]"), ctx).passed


def test_regex_present_is_case_sensitive_unless_pattern_says_otherwise():
    ctx = TrialContext(result="Co-Authored-By: someone")
    assert run_check(Check("regex_present", "", pattern="(?i)co-authored-by"), ctx).passed
    assert not run_check(Check("regex_present", "", pattern="co-authored-by"), ctx).passed


def test_contains_and_not_contains_are_literal():
    ctx = TrialContext(result="hello ZQ7X-CANARY")
    assert run_check(Check("contains", "", pattern="ZQ7X-CANARY"), ctx).passed
    assert not run_check(Check("not_contains", "", pattern="ZQ7X-CANARY"), ctx).passed


def test_scope_stderr_is_searchable():
    ctx = TrialContext(result="", stderr="boom")
    assert run_check(Check("contains", "", pattern="boom", scope="stderr"), ctx).passed


def test_unknown_scope_raises():
    with pytest.raises(ValueError):
        run_check(Check("contains", "", pattern="x", scope="nope"), TrialContext())


def test_hook_fired_searches_flattened_events():
    ctx = TrialContext(hook_events=[{"type": "hook_event", "hook_event_name": "PostToolUse",
                                     "stdout": "HOOKCANARY7-fired"}])
    assert run_check(Check("hook_fired", "", pattern="HOOKCANARY7"), ctx).passed
    assert not run_check(Check("hook_fired", "", pattern="NOPE"), ctx).passed


def test_hook_fired_fails_with_no_events():
    assert not run_check(Check("hook_fired", "", pattern="x"), TrialContext()).passed


def test_file_checks_resolve_inside_the_workspace(tmp_path):
    (tmp_path / "note.txt").write_text("ok", encoding="utf-8")
    ctx = TrialContext(workspace=tmp_path)
    assert run_check(Check("file_exists", "", path="note.txt"), ctx).passed
    assert run_check(Check("file_contains", "", pattern="^ok$", path="note.txt"), ctx).passed
    assert not run_check(Check("file_exists", "", path="missing.txt"), ctx).passed


def test_a_trial_passes_only_when_every_check_passes():
    ctx = TrialContext(result="hello")
    checks = [Check("contains", "", pattern="hello"), Check("contains", "", pattern="world")]
    passed, results = run_checks(checks, ctx)
    assert not passed
    assert [r.passed for r in results] == [True, False]


def test_tool_used_matches_name_and_input():
    ctx = TrialContext(tool_uses=[
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": "/Users/u/.claude/rules/commit-gate.md"}},
    ])
    assert run_check(Check("tool_used", "", pattern=r"Read .*commit-gate\.md"), ctx).passed
    assert not run_check(Check("tool_used", "", pattern=r"Skill"), ctx).passed
    assert run_check(Check("tool_not_used", "", pattern=r"Skill"), ctx).passed


def test_tool_used_fails_with_no_events():
    assert not run_check(Check("tool_used", "", pattern="Read"), TrialContext()).passed
