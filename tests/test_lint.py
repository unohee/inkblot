import json
import os
from pathlib import Path

from harness_eval.lint import (
    is_trivial_noop,
    lint_harness,
    lint_settings,
    script_from_command,
)
from harness_eval.lint import LintReport


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def test_script_from_command_handles_a_bare_script():
    path, via = script_from_command("~/.claude/hooks/foo.sh", Path("/proj"))
    assert path == Path.home() / ".claude/hooks/foo.sh"
    assert via is False


def test_script_from_command_handles_an_interpreter_and_project_dir():
    path, via = script_from_command(
        'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/x.py', Path("/proj")
    )
    assert path == Path("/proj/.claude/hooks/x.py")
    assert via is True


def test_is_trivial_noop_detects_a_stub(tmp_path):
    stub = tmp_path / "stub.py"
    stub.write_text("#!/usr/bin/env python3\n# Stub: always passes\nimport sys\nsys.exit(0)\n")
    noop, evidence = is_trivial_noop(stub)
    assert noop and "sys.exit(0)" in evidence


def test_is_trivial_noop_detects_a_shell_stub(tmp_path):
    stub = tmp_path / "stub.sh"
    stub.write_text("#!/bin/bash\n# No-op: disabled\n\nexit 0\n")
    assert is_trivial_noop(stub)[0]


def test_is_trivial_noop_leaves_real_scripts_alone(tmp_path):
    real = tmp_path / "real.sh"
    real.write_text("#!/bin/bash\nif [ -f x ]; then echo hi; fi\nexit 0\n")
    assert not is_trivial_noop(real)[0]


def _settings(tmp_path, command: str) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash",
                                  "hooks": [{"type": "command", "command": command}]}]}
    }), encoding="utf-8")
    return path


def test_missing_hook_script_is_an_error(tmp_path):
    report = LintReport()
    lint_settings(_settings(tmp_path, str(tmp_path / "nope.sh")), tmp_path, report)
    assert "L001" in codes(report) and report.errors == 1


def test_non_executable_hook_script_is_an_error(tmp_path):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/bash\necho real work\n")
    os.chmod(script, 0o644)
    report = LintReport()
    lint_settings(_settings(tmp_path, str(script)), tmp_path, report)
    assert "L002" in codes(report)


def test_interpreted_hook_does_not_need_the_executable_bit(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("print('real work')\n")
    os.chmod(script, 0o644)
    report = LintReport()
    lint_settings(_settings(tmp_path, f"python3 {script}"), tmp_path, report)
    assert "L002" not in codes(report)


def test_stub_hook_is_reported_as_a_noop(tmp_path):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    os.chmod(script, 0o755)
    report = LintReport()
    lint_settings(_settings(tmp_path, str(script)), tmp_path, report)
    assert "L003" in codes(report)


def test_invalid_settings_json_is_reported(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    report = LintReport()
    lint_settings(path, tmp_path, report)
    assert "L000" in codes(report)


def test_dead_rule_reference_is_an_error(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("see ~/.claude/rules/{alpha,beta}.md for details", encoding="utf-8")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "alpha.md").write_text("x", encoding="utf-8")
    report = lint_harness(tmp_path, tmp_path)
    dead = [f for f in report.findings if f.code == "L004"]
    assert len(dead) == 1 and "beta.md" in dead[0].subject


def test_orphan_rule_file_is_info_only(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("no references here", encoding="utf-8")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "orphan.md").write_text("x", encoding="utf-8")
    report = lint_harness(tmp_path, tmp_path)
    orphans = [f for f in report.findings if f.code == "L006"]
    assert len(orphans) == 1 and orphans[0].severity == "info"


def test_empty_command_file_is_an_error(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    cmds = tmp_path / "commands"
    cmds.mkdir()
    (cmds / "hollow.md").write_text("---\ndescription: does nothing\n---\n\n", encoding="utf-8")
    (cmds / "real.md").write_text(
        "---\ndescription: real\n---\n\nDo the thing, then verify it by running the tests.\n",
        encoding="utf-8")
    report = lint_harness(tmp_path, tmp_path)
    hollow = [f for f in report.findings if f.code == "L009"]
    assert len(hollow) == 1 and "hollow.md" in hollow[0].subject
    assert report.scanned["commands"] == 2


def test_very_short_command_body_is_a_warning(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    cmds = tmp_path / "commands"
    cmds.mkdir()
    (cmds / "stub.md").write_text("---\ndescription: x\n---\n\nTODO\n", encoding="utf-8")
    report = lint_harness(tmp_path, tmp_path)
    assert [f.code for f in report.findings if f.code.startswith("L01")] == ["L010"]


def test_skill_without_manifest_is_an_error(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    good = tmp_path / "skills" / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nname: good\ndescription: does a thing\n---\n")
    report = lint_harness(tmp_path, tmp_path)
    assert "L007" in codes(report)
    assert "L008" not in codes(report)
