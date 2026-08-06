import json
from pathlib import Path

import pytest

from harness_eval.model import load_case
from harness_eval.runner import (
    CONTROL,
    TREATMENT,
    build_argv,
    materialize_system_prompt,
    parse_output,
    read_fragment,
)

CASE_BODY = """
schema = 1
id = "demo"
[target]
kind = "rule"
[probe]
prompt = "hi"
[arms]
baseline_fragment = "base.md"
fragment = "frag.md"
[[checks]]
kind = "contains"
pattern = "x"
"""


def make_case(tmp_path):
    (tmp_path / "base.md").write_text("doc\n\n---\n\nBASELINE TEXT\n", encoding="utf-8")
    (tmp_path / "frag.md").write_text("doc\n\n---\n\nFRAGMENT TEXT\n", encoding="utf-8")
    path = tmp_path / "case.toml"
    path.write_text(CASE_BODY, encoding="utf-8")
    return load_case(path)


def test_read_fragment_drops_provenance_above_the_separator(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("# why this exists\nnotes\n\n---\n\nINJECTED\n", encoding="utf-8")
    assert read_fragment(p) == "INJECTED"


def test_read_fragment_without_a_separator_injects_everything(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("  INJECTED  \n", encoding="utf-8")
    assert read_fragment(p) == "INJECTED"


def test_control_gets_the_baseline_only(tmp_path):
    case = make_case(tmp_path)
    path = materialize_system_prompt(case, CONTROL, tmp_path)
    body = path.read_text(encoding="utf-8")
    assert "BASELINE TEXT" in body
    assert "FRAGMENT TEXT" not in body
    assert "doc" not in body


def test_treatment_gets_baseline_plus_fragment(tmp_path):
    case = make_case(tmp_path)
    body = materialize_system_prompt(case, TREATMENT, tmp_path).read_text(encoding="utf-8")
    assert "BASELINE TEXT" in body and "FRAGMENT TEXT" in body


def test_system_prompt_never_lands_in_the_trial_workspace(tmp_path, monkeypatch):
    # A tool-enabled probe can read its own cwd; a control arm that finds the
    # treatment fragment on disk is not a control any more.
    import json as _json
    import subprocess

    from harness_eval import runner

    case = make_case(tmp_path)
    seen = {}

    class FakeProc:
        returncode = 0
        stdout = _json.dumps({"type": "result", "subtype": "success", "result": "x"})
        stderr = ""

    def fake_run(argv, cwd, **kwargs):
        seen["cwd"] = Path(cwd)
        seen["sysprompt"] = Path(argv[argv.index("--append-system-prompt-file") + 1])
        seen["listing"] = sorted(p.name for p in Path(cwd).iterdir())
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    trial = runner.run_trial(case, TREATMENT, "sonnet", 0, tmp_path / "run")

    assert not trial.error
    assert seen["listing"] == []  # the workspace the model can see is empty
    assert seen["cwd"] not in seen["sysprompt"].parents


def test_argv_isolates_the_session(tmp_path):
    case = make_case(tmp_path)
    argv = build_argv(case, CONTROL, "sonnet", tmp_path / "sp.md")
    assert argv[0] == "claude"
    for flag in ("--safe-mode", "--no-session-persistence", "--append-system-prompt-file"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""


def test_argv_switches_to_streaming_only_for_hook_checks(tmp_path):
    (tmp_path / "base.md").write_text("---\nb", encoding="utf-8")
    (tmp_path / "frag.md").write_text("---\nf", encoding="utf-8")
    body = CASE_BODY.replace('kind = "contains"\npattern = "x"', 'kind = "hook_fired"\npattern = "X"')
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    case = load_case(path)
    argv = build_argv(case, TREATMENT, "sonnet", None)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--include-hook-events" in argv


def make_hook_case(tmp_path):
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    (tmp_path / "base.md").write_text("---\nb", encoding="utf-8")
    (tmp_path / "frag.md").write_text("---\nf", encoding="utf-8")
    body = CASE_BODY.replace(
        'fragment = "frag.md"',
        'fragment = "frag.md"\nsettings = "s.json"\nisolation = "settings-only"',
    )
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    return load_case(path)


def test_arm_settings_are_treatment_only(tmp_path):
    case = make_hook_case(tmp_path)
    assert "--settings" in build_argv(case, TREATMENT, "sonnet", None)
    assert "--settings" not in build_argv(case, CONTROL, "sonnet", None)


def test_skill_canary_replaces_only_treatment_copy_body(tmp_path, monkeypatch):
    from harness_eval.model import Case, Check, Target
    from harness_eval.runner import run_trial

    skill = tmp_path / "real-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: real\ndescription: real skill\n---\nbody\n", encoding="utf-8"
    )
    case = Case(
        id="canary", title="canary", path=tmp_path / "case.toml", engine="codex",
        target=Target("skill", skill / "SKILL.md", "real"), prompt="probe",
        checks=(Check("contains", "", pattern="MARKER"),), models=("m",), n=1,
        skill_dir=skill, skill_canary="MARKER", isolation="codex-project",
    )
    seen = {}

    def fake_run(argv, cwd, **kwargs):
        planted = Path(cwd) / ".agents" / "skills" / "real-skill" / "SKILL.md"
        seen["text"] = planted.read_text(encoding="utf-8")
        return type("Proc", (), {"stdout": "", "stderr": "", "returncode": 1})()

    monkeypatch.setattr("subprocess.run", fake_run)
    run_trial(case, TREATMENT, "m", 0, tmp_path)
    assert "MARKER" in seen["text"]
    assert "MARKER" not in (skill / "SKILL.md").read_text(encoding="utf-8")


def test_load_trials_uses_latest_retry_for_each_trial_key(tmp_path):
    from harness_eval.runner import Trial, load_trials, write_trials

    old = Trial("case", TREATMENT, "model", 0, passed=False)
    retry = Trial("case", TREATMENT, "model", 0, passed=True)
    write_trials([old, retry], tmp_path)
    loaded = load_trials(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].passed


def test_settings_only_isolation_drops_safe_mode(tmp_path):
    # --safe-mode suppresses hooks delivered through --settings, so a hook case
    # must not carry it. Verified against claude 2.1.221.
    argv = build_argv(make_hook_case(tmp_path), TREATMENT, "sonnet", None)
    assert "--safe-mode" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv


def test_hook_settings_with_safe_mode_isolation_is_rejected(tmp_path):
    from harness_eval.model import CaseError
    (tmp_path / "s.json").write_text("{}", encoding="utf-8")
    (tmp_path / "base.md").write_text("---\nb", encoding="utf-8")
    (tmp_path / "frag.md").write_text("---\nf", encoding="utf-8")
    body = CASE_BODY.replace('fragment = "frag.md"', 'fragment = "frag.md"\nsettings = "s.json"')
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CaseError, match="settings-only"):
        load_case(path)


def test_a_good_trial_is_scorable():
    from harness_eval.runner import unusable_reason
    assert unusable_reason(0, "some answer", "", {"subtype": "success"}) == ""


def test_empty_result_is_never_scored():
    # Otherwise regex_absent("[가-힣]") would pass on nothing at all.
    from harness_eval.runner import unusable_reason
    assert "empty result" in unusable_reason(0, "   ", "", {"subtype": "success"})


def test_api_error_is_never_scored():
    from harness_eval.runner import unusable_reason
    assert unusable_reason(0, "Budget exceeded", "", {"is_error": True, "subtype": "success"})
    assert unusable_reason(0, "partial", "", {"subtype": "error_max_budget"})


def test_nonzero_exit_with_a_result_is_still_scored():
    from harness_eval.runner import unusable_reason
    assert unusable_reason(1, "an actual answer", "warning", {}) == ""


def test_parse_output_json_mode():
    payload = json.dumps({"type": "result", "result": "hello", "total_cost_usd": 0.01})
    text, meta, hooks, tools = parse_output(payload, streaming=False)
    assert text == "hello" and meta["total_cost_usd"] == 0.01 and hooks == []


def test_parse_output_falls_back_to_raw_text_on_bad_json():
    text, meta, hooks, tools = parse_output("not json at all", streaming=False)
    assert text == "not json at all" and meta == {}


def test_codex_argv_uses_ephemeral_read_only_isolation(tmp_path):
    body = CASE_BODY.replace("schema = 1", 'schema = 1\nengine = "codex"').replace(
        'fragment = "frag.md"', 'fragment = "frag.md"\nisolation = "codex-project"'
    )
    (tmp_path / "base.md").write_text("---\nb", encoding="utf-8")
    (tmp_path / "frag.md").write_text("---\nf", encoding="utf-8")
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    argv = build_argv(load_case(path), TREATMENT, "gpt-5.6-terra", None)
    assert argv[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--json"):
        assert flag in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


def test_parse_codex_jsonl_collects_final_message_and_tools():
    lines = [
        json.dumps({"type": "item.completed", "item": {
            "id": "1", "type": "command_execution", "command": "pwd"}}),
        json.dumps({"type": "item.completed", "item": {
            "id": "2", "type": "agent_message", "text": "done"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
    ]
    text, meta, hooks, tools = parse_output("\n".join(lines), False, "codex")
    assert text == "done"
    assert meta["type"] == "turn.completed"
    assert hooks == [] and tools[0]["name"] == "command_execution"


def test_parse_output_stream_mode_collects_result_and_hook_events():
    # The hook envelope below is copied from a real claude 2.1.221 transcript:
    # hooks arrive as system/hook_response, not as a dedicated top-level type.
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "system", "subtype": "hook_started",
                    "hook_name": "PostToolUse:Write", "hook_event": "PostToolUse"}),
        json.dumps({"type": "system", "subtype": "hook_response",
                    "hook_name": "PostToolUse:Write", "hook_event": "PostToolUse",
                    "output": "HOOKCANARY7-fired\n", "stdout": "HOOKCANARY7-fired\n",
                    "exit_code": 0, "outcome": "success"}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "ignored"}]}}),
        json.dumps({"type": "result", "result": "final", "num_turns": 2}),
        "garbage line",
    ]
    text, meta, hooks, tools = parse_output("\n".join(lines), streaming=True)
    assert text == "final"
    assert meta["num_turns"] == 2
    assert len(hooks) == 2

    from harness_eval.checks import TrialContext, run_check
    from harness_eval.model import Check
    ctx = TrialContext(hook_events=hooks)
    assert run_check(Check("hook_fired", "", pattern="HOOKCANARY7"), ctx).passed


def test_parse_output_ignores_non_hook_system_events():
    line = json.dumps({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50})
    _, _, hooks, _ = parse_output(line, streaming=True)
    assert hooks == []


def test_parse_output_stream_mode_falls_back_to_assistant_text():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": "partial"}]}})
    text, _, _, _ = parse_output(line, streaming=True)
    assert text == "partial"


EXCISION_BODY = """
schema = 1
id = "exc"
[target]
kind = "rule"
source = "doc.md"
sha256 = "{sha}"
excise_lines = [[2, 2]]
[probe]
prompt = "hi"
[arms]
mode = "excision"
[[checks]]
kind = "contains"
pattern = "x"
"""


def make_excision_case(tmp_path):
    from harness_eval.model import sha256_file
    doc = tmp_path / "doc.md"
    doc.write_text("keep1\nRULE LINE\nkeep2\n", encoding="utf-8")
    path = tmp_path / "case.toml"
    path.write_text(EXCISION_BODY.format(sha=sha256_file(doc)), encoding="utf-8")
    return load_case(path)


def test_excision_arms_differ_only_by_the_cut_lines(tmp_path):
    case = make_excision_case(tmp_path)
    treatment = materialize_system_prompt(case, TREATMENT, tmp_path).read_text(encoding="utf-8")
    control = materialize_system_prompt(case, CONTROL, tmp_path).read_text(encoding="utf-8")
    assert "RULE LINE" in treatment
    assert "RULE LINE" not in control
    assert "keep1" in control and "keep2" in control


def test_excision_refuses_a_drifted_source(tmp_path):
    from harness_eval.runner import SourceDrifted
    case = make_excision_case(tmp_path)
    case.target.source.write_text("edited\ndocument\nnow\n", encoding="utf-8")
    with pytest.raises(SourceDrifted, match="re-extract"):
        materialize_system_prompt(case, CONTROL, tmp_path)


def test_parse_output_stream_mode_collects_tool_uses():
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "tick-parser"}},
        {"type": "text", "text": "done"},
    ]}})
    _, _, _, tools = parse_output(line, streaming=True)
    assert len(tools) == 1 and tools[0]["name"] == "Skill"


def test_skill_case_plants_the_skill_in_treatment_only(tmp_path, monkeypatch):
    import json as _json
    import subprocess

    from harness_eval import runner

    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: myskill\ndescription: d\n---\nbody",
                                    encoding="utf-8")
    body = CASE_BODY.replace(
        'baseline_fragment = "base.md"\nfragment = "frag.md"',
        f'skill = "{skill}"\nisolation = "project"',
    ).replace('kind = "contains"\npattern = "x"', 'kind = "tool_used"\npattern = "Skill"')
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    case = load_case(path)

    seen = {}

    class FakeProc:
        returncode = 0
        stdout = _json.dumps({"type": "result", "subtype": "success", "result": "x"})
        stderr = ""

    def fake_run(argv, cwd, **kwargs):
        seen.setdefault("workspaces", []).append(
            (Path(cwd) / ".claude" / "skills" / "myskill" / "SKILL.md").exists())
        seen["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.run_trial(case, TREATMENT, "sonnet", 0, tmp_path / "run")
    runner.run_trial(case, CONTROL, "sonnet", 0, tmp_path / "run")
    assert seen["workspaces"] == [True, False]
    argv = seen["argv"]
    assert argv[argv.index("--setting-sources") + 1] == "project"


def test_skill_without_project_isolation_is_rejected(tmp_path):
    from harness_eval.model import CaseError
    skill = tmp_path / "myskill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("x", encoding="utf-8")
    body = CASE_BODY.replace(
        'baseline_fragment = "base.md"\nfragment = "frag.md"',
        f'skill = "{skill}"',
    )
    path = tmp_path / "case.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CaseError, match="project"):
        load_case(path)
