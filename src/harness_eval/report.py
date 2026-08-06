"""Turn trials into verdicts and a report a human can act on."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .model import Case
from .runner import CONTROL, TREATMENT, Trial
from .stats import (
    INCONCLUSIVE,
    INEFFECTIVE,
    LOAD_BEARING,
    NO_DATA,
    NO_OP,
    ArmStats,
    Verdict,
    ACTIVATION_FAIL,
    ACTIVATION_PASS,
    CONTAMINATED,
    classify,
    classify_activation,
    min_n_for_significance,
)

KEEP = "KEEP"
DROP = "DROP"
CONVERT = "CONVERT"
RERUN = "RERUN"
REWRITE = "REWRITE"
INVESTIGATE = "INVESTIGATE"
PASS = "PASS"

# With tools disabled a probe can still fail by making the model announce an
# inspection it cannot perform and then stop. Those trials are scored like any
# other, so the report has to say how many of them there were.
NARRATION = re.compile(
    r"(확인하겠습니다|확인해 ?보겠습니다|살펴보겠습니다|찾아보겠습니다|Let me (check|look|explore))"
)
NARRATION_MAX_CHARS = 320


def looks_like_a_non_answer(result: str) -> bool:
    """True when the model narrated a lookup instead of answering the probe."""
    text = result.strip()
    return bool(NARRATION.search(text[:200])) and len(text) < NARRATION_MAX_CHARS


@dataclass
class CaseVerdict:
    case_id: str
    title: str
    model: str
    verdict: Verdict
    action: str
    note: str
    errors: int
    cost_usd: float
    non_answers: int = 0


def recommend(case: Case, verdict: Verdict) -> tuple[str, str]:
    """Map a statistical verdict onto what to do with the harness artifact."""
    if verdict.label == ACTIVATION_PASS:
        return PASS, "discovery and activation transport are working"
    if verdict.label == ACTIVATION_FAIL:
        return INVESTIGATE, "the treatment skill did not activate — inspect its raw transcript"
    if verdict.label == CONTAMINATED:
        return INVESTIGATE, "the control is not isolated — fix the harness before rerunning"
    if verdict.label == LOAD_BEARING:
        return KEEP, "the artifact changes behaviour — keep it in the harness"
    if verdict.label == NO_OP:
        if case.deterministic_alternative:
            return CONVERT, f"prose is not carrying this — enforce it deterministically: {case.deterministic_alternative}"
        return DROP, "the model already behaves this way without the artifact — delete it"
    if verdict.label == INEFFECTIVE:
        return REWRITE, ("the rule does not produce the behaviour even when present — rewrite it, "
                         "enforce it mechanically, or check whether the probe is measuring the "
                         "right thing")
    if verdict.label == INCONCLUSIVE:
        return RERUN, f"underpowered; a clean split needs n >= {min_n_for_significance(case.alpha)} per arm"
    return INVESTIGATE, "trials failed to produce data — check raw/ for CLI errors"


def summarize(cases: list[Case], trials: list[Trial]) -> list[CaseVerdict]:
    by_case = {c.id: c for c in cases}
    keys = sorted({(t.case_id, t.model) for t in trials})
    out: list[CaseVerdict] = []

    for case_id, model in keys:
        case = by_case.get(case_id)
        if case is None:
            continue
        subset = [t for t in trials if t.case_id == case_id and t.model == model]
        usable = [t for t in subset if not t.error]
        arms = {
            arm: ArmStats(
                n=sum(1 for t in usable if t.arm == arm),
                passes=sum(1 for t in usable if t.arm == arm and t.passed),
            )
            for arm in (TREATMENT, CONTROL)
        }
        verdict = (
            classify_activation(arms[TREATMENT], arms[CONTROL])
            if case.measurement == "activation"
            else classify(arms[TREATMENT], arms[CONTROL], case.alpha)
        )
        action, note = recommend(case, verdict)
        out.append(
            CaseVerdict(
                case_id=case_id,
                title=case.title,
                model=model,
                verdict=verdict,
                action=action,
                note=note,
                errors=sum(1 for t in subset if t.error),
                cost_usd=round(sum(t.cost_usd or 0.0 for t in subset), 4),
                non_answers=sum(1 for t in usable if looks_like_a_non_answer(t.result)),
            )
        )
    return out


def render_markdown(summaries: list[CaseVerdict], run_id: str, cli_version: str) -> str:
    lines = [
        f"# Inkblot eval — run {run_id}",
        "",
        f"agent CLI: `{cli_version}`  ",
        f"cases x models: {len(summaries)}  ",
        f"total cost: ${sum(s.cost_usd for s in summaries):.4f}",
        "",
        "| case | model | treatment | control | delta | p | verdict | action |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        v = s.verdict
        flag = " ⚠" if s.non_answers else ""
        lines.append(
            f"| `{s.case_id}` | {s.model} | {v.treatment.passes}/{v.treatment.n} | "
            f"{v.control.passes}/{v.control.n} | {v.delta:+.2f} | {v.p_value:.4f} | "
            f"**{v.label}**{flag} | {s.action} |"
        )

    if any(s.non_answers for s in summaries):
        lines += [
            "",
            "⚠ = some trials look like the model narrated a lookup instead of answering. "
            "That is a probe defect, not a harness finding — treat those verdicts as "
            "provisional until the probe is self-contained.",
        ]

    lines += ["", "## Detail", ""]
    for s in summaries:
        lines += [
            f"### `{s.case_id}` — {s.title} ({s.model})",
            "",
            f"- verdict: **{s.verdict.label}** — {s.verdict.rationale}",
            f"- action: **{s.action}** — {s.note}",
        ]
        if s.errors:
            lines.append(f"- errored trials: {s.errors} (see `raw/`)")
        if s.non_answers:
            lines.append(f"- non-answers: {s.non_answers} trial(s) announced an inspection instead "
                         f"of answering — the probe, not the rule, is at fault")
        lines.append("")

    if not summaries:
        lines += ["", "_No trials recorded._"]
    return "\n".join(lines) + "\n"


def write_report(summaries: list[CaseVerdict], run_dir: Path, run_id: str,
                 cli_version: str) -> tuple[Path, Path]:
    md = render_markdown(summaries, run_id, cli_version)
    md_path = run_dir / "report.md"
    md_path.write_text(md, encoding="utf-8")

    payload = {
        "run_id": run_id,
        "cli_version": cli_version,
        "summaries": [
            {
                "case_id": s.case_id,
                "title": s.title,
                "model": s.model,
                "verdict": s.verdict.label,
                "p_value": s.verdict.p_value,
                "delta": s.verdict.delta,
                "treatment": {"n": s.verdict.treatment.n, "passes": s.verdict.treatment.passes},
                "control": {"n": s.verdict.control.n, "passes": s.verdict.control.passes},
                "action": s.action,
                "note": s.note,
                "errors": s.errors,
                "non_answers": s.non_answers,
                "cost_usd": s.cost_usd,
            }
            for s in summaries
        ],
    }
    json_path = run_dir / "summary.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


__all__ = ["CaseVerdict", "KEEP", "DROP", "CONVERT", "RERUN", "REWRITE", "INVESTIGATE",
           "looks_like_a_non_answer", "recommend", "render_markdown", "summarize",
           "write_report", "LOAD_BEARING", "NO_OP", "INEFFECTIVE", "INCONCLUSIVE", "NO_DATA"]
