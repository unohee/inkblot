"""CLI-level tests for report replay.

A replayed report must be a faithful rendering of the stored run, regardless
of which --suite the caller happens to pass today.
"""

import json

import pytest

from harness_eval.cli import cmd_report

MINIMAL = """
schema = 1
id = "{case_id}"
[target]
kind = "rule"
[probe]
prompt = "hi"
[arms]
fragment = "f.md"
[[checks]]
kind = "contains"
pattern = "x"
"""


def make_root(tmp_path, case_ids, trial_case_ids):
    """A fake repo root: a cases/ corpus and one finished run under results/."""
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "f.md").write_text("frag", encoding="utf-8")
    for cid in case_ids:
        (cases / f"{cid}.toml").write_text(MINIMAL.format(case_id=cid), encoding="utf-8")
    run_dir = tmp_path / "results" / "20260806T000000"
    run_dir.mkdir(parents=True)
    rows = []
    for cid in trial_case_ids:
        for arm, passed in (("treatment", True), ("control", False)):
            rows.append({"case_id": cid, "arm": arm, "model": "m", "index": 0,
                         "passed": passed})
    (run_dir / "trials.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return run_dir


class Args:
    def __init__(self, root, run):
        self.root = str(root)
        self.run = str(run)
        self.suite = "codex"  # deliberately "wrong" — report must not care


def test_report_replay_ignores_suite_flag(tmp_path, capsys):
    """--suite must not decide which cases a stored run is scored against."""
    run_dir = make_root(tmp_path, case_ids=["demo"], trial_case_ids=["demo"])
    assert cmd_report(Args(tmp_path, run_dir)) == 0
    out = capsys.readouterr().out
    assert "demo" in out


def test_report_replay_warns_about_orphaned_trials(tmp_path, capsys):
    """Trials whose case file is gone must be flagged, not silently dropped."""
    run_dir = make_root(tmp_path, case_ids=["demo"],
                        trial_case_ids=["demo", "retired-case"])
    assert cmd_report(Args(tmp_path, run_dir)) == 0
    captured = capsys.readouterr()
    assert "retired-case" in captured.err
    assert "omitted" in captured.err


def test_report_with_no_runs_raises(tmp_path):
    with pytest.raises(SystemExit, match="no runs"):
        cmd_report(Args(tmp_path, tmp_path / "results" / "missing"))
