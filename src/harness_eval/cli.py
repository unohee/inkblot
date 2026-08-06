"""Inkblot agent-harness evaluation CLI.

    inkblot audit      static Codex setup and skill inventory
    inkblot skills     activation-check every installed Codex skill
    inkblot selftest   prove the selected suite's isolation mechanism
    inkblot run        run hand-authored behavioural cases
    inkblot report     re-render a finished run
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import __version__
from .lint import lint_harness
from .model import Case, CaseError, check_drift, load_cases
from .report import summarize, write_report
from .runner import (
    check_cli_available, load_trials, run_case, run_cases_parallel, write_trials,
)
from .codex_audit import audit_codex_harness
from .codex_skills import build_skill_activation_cases

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cases_dir(args) -> Path:
    base = Path(args.root) / "cases"
    return base / "codex" if args.suite == "codex" else base


def _select(cases: list[Case], wanted: str | None) -> list[Case]:
    if not wanted:
        return [c for c in cases if c.enabled]
    ids = {w.strip() for w in wanted.split(",") if w.strip()}
    missing = ids - {c.id for c in cases}
    if missing:
        raise SystemExit(f"unknown case id(s): {', '.join(sorted(missing))}")
    return [c for c in cases if c.id in ids]


def cmd_list(args) -> int:
    cases = load_cases(_cases_dir(args), recursive=True)
    for c in cases:
        flag = " " if c.enabled else "-"
        src = c.target.source.name if c.target.source else "-"
        print(f"{flag} {c.id:<28} {c.target.kind:<7} {src:<28} n={c.n} models={','.join(c.models)}")
    print(f"\n{len(cases)} case(s)")
    return 0


def cmd_lint(args) -> int:
    report = lint_harness(Path(args.claude_dir).expanduser(), Path(args.project_dir).resolve())
    if args.json:
        import json
        print(json.dumps({"scanned": report.scanned,
                          "findings": [f.as_dict() for f in report.sorted()]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"scanned: {report.scanned}")
        if not report.findings:
            print("no static no-ops found")
        for f in report.sorted():
            print(f"\n[{f.severity.upper():5}] {f.code} {f.subject}\n  {f.message}")
            if f.evidence:
                print(f"  evidence: {f.evidence}")
            if f.fix:
                print(f"  fix:      {f.fix}")
        print(f"\n{len(report.findings)} finding(s), {report.errors} error(s)")
    return 1 if report.errors else 0


def cmd_codex_audit(args) -> int:
    report = audit_codex_harness(Path(args.codex_home).expanduser())
    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"codex: {report['cli_version']}")
        print(f"skills: {len(report['skills'])} ({report['unique_names']} unique names)")
        print(f"plugins: {len(report['plugins'])}")
        print(f"hooks: {report['hook_count']}")
        for finding in report["findings"]:
            print(f"\n[{finding['severity'].upper():5}] {finding['code']} {finding['subject']}")
            print(f"  {finding['message']}")
        print(f"\n{len(report['findings'])} finding(s), {report['errors']} error(s)")
    return 1 if report["errors"] else 0


def cmd_codex_skills(args) -> int:
    cases = _select(
        build_skill_activation_cases(Path(args.codex_home).expanduser()), args.cases
    )
    if not cases:
        raise SystemExit("no installed Codex skills found")
    if args.resume:
        run_dir = Path(args.resume)
        run_id = run_dir.name
    else:
        run_id = "codex-skills-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        run_dir = Path(args.root) / "results" / run_id
    cli_version = check_cli_available("codex")
    run_dir.mkdir(parents=True, exist_ok=True)
    existing = load_trials(run_dir)
    models = [m.strip() for m in args.models.split(",")] if args.models else None
    print(
        f"run {run_id} — codex {cli_version}; {len(cases)} skills; "
        f"resuming {len(existing)} stored trial(s)"
    )

    def progress(trial):
        mark = "." if trial.passed else ("E" if trial.error else "x")
        print(
            f"{mark} {trial.case_id:<48} {trial.arm:<9} "
            f"{trial.model} #{trial.index} {trial.duration_s:>6.1f}s"
            f"{' ' + trial.error if trial.error else ''}",
            flush=True,
        )

    fresh = run_cases_parallel(
        cases, run_dir, workers=args.workers, model_override=models,
        n_override=args.n, keep_workspace=args.keep, existing=existing,
        on_trial=progress,
    )
    trials = load_trials(run_dir)
    # If a failed treatment was retried, keep only the newest row for scoring.
    latest = {}
    for trial in trials:
        latest[(trial.case_id, trial.arm, trial.model, trial.index)] = trial
    trials = list(latest.values())
    summaries = summarize(cases, trials)
    md_path, _ = write_report(summaries, run_dir, run_id, cli_version)
    print(f"\n{md_path.read_text(encoding='utf-8')}")
    print(f"report: {md_path}; new trials: {len(fresh)}")
    return 1 if any(summary.errors for summary in summaries) else 0


def cmd_drift(args) -> int:
    cases = load_cases(_cases_dir(args), recursive=True)
    reports = check_drift(cases)
    stale = 0
    by_id = {c.id: c for c in cases}
    for r in reports:
        marker = {"ok": "ok  ", "changed": "STALE", "missing": "GONE", "unrecorded": "??  "}[r.status]
        print(f"{marker} {r.case_id:<28} {r.source}")
        if r.status in {"changed", "missing"}:
            stale += 1
            print(f"      recorded={r.recorded} current={r.current}")
        case = by_id.get(r.case_id)
        if args.verbose and case and case.mode == "excision" and r.status == "ok":
            from .model import excise
            _, removed = excise(r.source.read_text(encoding="utf-8"), case.target.excise_lines)
            for line in removed.splitlines():
                print(f"      - {line[:100]}")
    print(f"\n{stale} stale case(s) of {len(reports)}")
    return 1 if stale else 0


def _run(cases: list[Case], args, run_dir: Path, run_id: str) -> int:
    engines = {case.engine for case in cases}
    if len(engines) != 1:
        raise RuntimeError("one run cannot mix Claude and Codex cases")
    engine = next(iter(engines))
    cli_version = check_cli_available(engine)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run {run_id} — {engine} {cli_version}")

    all_trials = []
    for case in cases:
        models = [m.strip() for m in args.models.split(",")] if args.models else list(case.models)
        n = args.n or case.n
        print(f"\n== {case.id} ({case.title}) — models={','.join(models)} n={n} per arm")

        def progress(t, _case=case):
            mark = "." if t.passed else ("E" if t.error else "x")
            print(f"   {mark} {t.arm:<9} {t.model:<7} #{t.index} {t.duration_s:>6.1f}s"
                  f"{' ' + t.error if t.error else ''}")

        trials = run_case(case, models, n, run_dir, workers=args.workers,
                          keep_workspace=args.keep, on_trial=progress)
        write_trials(trials, run_dir)
        all_trials.extend(trials)

    summaries = summarize(cases, all_trials)
    md_path, _ = write_report(summaries, run_dir, run_id, cli_version)
    print(f"\n{md_path.read_text(encoding='utf-8')}")
    print(f"report: {md_path}")

    failed = sum(s.errors for s in summaries)
    return 1 if failed else 0


def cmd_run(args) -> int:
    try:
        cases = _select(load_cases(_cases_dir(args)), args.cases)
    except CaseError as exc:
        raise SystemExit(str(exc)) from exc
    if not cases:
        print("no enabled cases to run")
        return 0
    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    return _run(cases, args, Path(args.root) / "results" / run_id, run_id)


def cmd_selftest(args) -> int:
    cases = _select(load_cases(_cases_dir(args) / "selftest"), args.cases)
    if not cases:
        raise SystemExit("no selftest cases found under cases/selftest/")
    run_id = "selftest-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    rc = _run(cases, args, Path(args.root) / "results" / run_id, run_id)
    print("\nThe selftest asserts the mechanism, not the harness: treatment must obey an"
          "\ninjected canary that control never sees. A failure here invalidates every"
          "\nother verdict in this repo.")
    return rc


def latest_run(results: Path) -> Path | None:
    """Most recent directory that actually holds trials.

    Sorting by name would pick `selftest-…` over a timestamped run (and `cron/`
    holds logs, not trials), so select on mtime among directories that have a
    trials.jsonl.
    """
    candidates = [p for p in results.glob("*") if p.is_dir() and (p / "trials.jsonl").exists()]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def cmd_report(args) -> int:
    run_dir = Path(args.run) if args.run else latest_run(Path(args.root) / "results")
    if run_dir is None or not run_dir.exists():
        raise SystemExit("no runs found — try `make eval` first")
    trials = load_trials(run_dir)
    if run_dir.name.startswith("codex-skills-"):
        cases = build_skill_activation_cases(Path("~/.codex").expanduser())
    else:
        # The run directory, not --suite, determines which cases the stored
        # trials belong to. Load the whole corpus so a codex run replays
        # correctly without the caller having to remember to re-pass --suite.
        cases = load_cases(Path(args.root) / "cases", recursive=True)
    dropped = sorted({t.case_id for t in trials} - {c.id for c in cases})
    if dropped:
        # summarize() skips trials whose case no longer exists (retired case,
        # uninstalled skill). Silence here would misreport a partial replay as
        # the whole run.
        print(
            f"warning: {len(dropped)} case id(s) in this run have no matching case "
            f"definition and are omitted: {', '.join(dropped)}",
            file=sys.stderr,
        )
    summaries = summarize(cases, trials)
    md_path, _ = write_report(summaries, run_dir, run_dir.name, "(replayed)")
    print(md_path.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inkblot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root", default=str(REPO_ROOT), help="repository root (default: %(default)s)")
    p.add_argument("--suite", choices=("claude", "codex"), default="claude",
                   help="case suite to use (default: claude)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the case corpus").set_defaults(func=cmd_list)

    lint = sub.add_parser("lint", help="static no-op detection (no API calls)")
    lint.add_argument("--claude-dir", default="~/.claude")
    lint.add_argument("--project-dir", default=".")
    lint.add_argument("--json", action="store_true")
    lint.set_defaults(func=cmd_lint)

    audit = sub.add_parser(
        "audit", aliases=["codex-audit"],
        help="audit installed Codex skills and setup",
    )
    audit.add_argument("--codex-home", default="~/.codex")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_codex_audit)

    skills = sub.add_parser(
        "skills", aliases=["codex-skills"],
        help="activation-check every installed Codex skill"
    )
    skills.add_argument("--codex-home", default="~/.codex")
    skills.add_argument("--cases", help="comma-separated generated case ids")
    skills.add_argument("-n", type=int, default=None,
                        help="trials per arm (default: 1 transport check)")
    skills.add_argument("--models", help="comma-separated models")
    skills.add_argument("--workers", type=int, default=3)
    skills.add_argument("--keep", action="store_true")
    skills.add_argument("--resume", help="resume a prior results/codex-skills-* directory")
    skills.set_defaults(func=cmd_codex_skills)

    drift = sub.add_parser("drift", help="detect cases stale versus the live harness")
    drift.add_argument("-v", "--verbose", action="store_true",
                       help="for excision cases, print exactly what the control arm loses")
    drift.set_defaults(func=cmd_drift)

    for name, fn, help_text in (("run", cmd_run, "run the real cases"),
                                ("selftest", cmd_selftest, "validate the ablation mechanism")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--cases", help="comma-separated case ids")
        s.add_argument("-n", type=int, default=None, help="trials per arm (overrides the case)")
        s.add_argument("--models", help="comma-separated models (overrides the case)")
        s.add_argument("--workers", type=int, default=3, help="concurrent trials (default: 3)")
        s.add_argument("--keep", action="store_true", help="keep trial workspaces for inspection")
        s.set_defaults(func=fn)

    rep = sub.add_parser("report", help="re-render a finished run")
    rep.add_argument("--run", help="path to a results/<run_id> directory")
    rep.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CaseError as exc:
        print(f"case error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
