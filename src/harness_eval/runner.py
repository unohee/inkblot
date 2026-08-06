"""Arm construction and trial execution.

Isolation strategy (all documented CLI flags, verified against claude 2.1.221):

  control    claude --safe-mode -p PROBE  [--append-system-prompt-file BASELINE]
  treatment  same, plus the fragment under test appended to the system prompt
             (and/or --settings pointing at an arm-specific hook config)

`--safe-mode` disables every customization — user CLAUDE.md, skills, plugins,
hooks, MCP servers, custom commands — while leaving auth and built-in tools
working. That matters here: this machine authenticates over OAuth, so `--bare`
(which demands ANTHROPIC_API_KEY) is not an option.

The baseline fragment is applied to *both* arms. Without it, ablations are
confounded: a rule such as "docstrings must be English" only has anything to
push against when the surrounding "answer in Korean" pressure is also present.

Concurrency: every trial runs in its own mkdtemp workspace with session
persistence off and writes to its own result file, so trials never share cwd,
session state, or an output handle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .checks import CheckResult, TrialContext, run_checks
from .model import Case, excise, sha256_file

CONTROL = "control"
TREATMENT = "treatment"
ARMS = (CONTROL, TREATMENT)

MAX_STORED_STDOUT = 200_000


class SourceDrifted(RuntimeError):
    """The pinned document changed under an excision case; the trial must not run."""


@dataclass
class Trial:
    case_id: str
    arm: str
    model: str
    index: int
    passed: bool
    error: str = ""
    result: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    argv: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    duration_s: float = 0.0
    num_turns: int | None = None
    hook_event_count: int = 0
    tool_use_count: int = 0
    raw_path: str = ""


def build_argv(case: Case, arm: str, model: str, sysprompt: Path | None) -> list[str]:
    """Assemble one isolated Claude Code or Codex invocation."""
    if case.engine == "codex":
        return [
            "codex", "--ask-for-approval", "never", "exec", case.prompt,
            "--model", model, "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--json",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
        ]
    argv = [
        "claude",
        "-p",
        case.prompt,
        "--model",
        model,
        "--no-session-persistence",
        "--max-budget-usd",
        str(case.max_budget_usd),
        "--permission-mode",
        case.permission_mode,
    ]

    if case.isolation == "safe-mode":
        argv.append("--safe-mode")
    elif case.isolation == "project":
        # Loads only the trial workspace's .claude/ — which run_trial controls —
        # so a skill planted there is the sole difference between arms.
        argv += ["--setting-sources", "project", "--strict-mcp-config"]
    else:
        # --safe-mode would also kill the hooks the case is trying to measure.
        # Dropping every setting source plus MCP gets most of the isolation back.
        argv += ["--setting-sources", "", "--strict-mcp-config"]

    if case.needs_stream:
        argv += ["--output-format", "stream-json", "--include-hook-events", "--verbose"]
    else:
        argv += ["--output-format", "json"]

    argv += ["--tools", *case.tools]

    if sysprompt is not None:
        argv += ["--append-system-prompt-file", str(sysprompt)]

    if arm == TREATMENT and case.arm_settings is not None:
        argv += ["--settings", str(case.arm_settings)]

    return argv


def read_fragment(path: Path) -> str:
    """Return the injected body of a fragment file.

    Fragments carry provenance (which file, which line, why) above a `---`
    separator; only what follows the separator is injected, so the commentary
    never leaks into the system prompt and changes what is being measured.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text.strip()


def materialize_system_prompt(case: Case, arm: str, prompt_dir: Path) -> Path | None:
    """Write the arm's system-prompt addendum, or None when the arm adds nothing.

    `prompt_dir` must not be the trial workspace. A tool-enabled probe can list
    and read its own cwd, and a control-arm run that finds the treatment
    fragment lying on disk is no longer a control.

    Excision mode ships the whole target document in both arms — verbatim for
    treatment, with target.excise_lines cut for control — so the rule is
    measured in its real position among everything else it competes with. The
    document is verified against target.sha256 first; excising line ranges from
    a drifted document would silently measure the wrong text.
    """
    parts: list[str] = [read_fragment(b) for b in case.baseline_fragments]

    if case.mode == "excision":
        source = case.target.source
        current = sha256_file(source)
        if current != case.target.sha256:
            raise SourceDrifted(
                f"{source} sha256={current} does not match the pinned {case.target.sha256} — "
                f"re-extract the case (line ranges are stale)"
            )
        doc = source.read_text(encoding="utf-8")
        if arm == CONTROL:
            doc, _ = excise(doc, case.target.excise_lines)
        parts.append(doc.strip())
    elif arm == TREATMENT and case.fragment is not None:
        parts.append(read_fragment(case.fragment))

    if not parts:
        return None
    path = prompt_dir / f"_sysprompt.{arm}.md"
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return path


def parse_output(
    stdout: str, streaming: bool, engine: str = "claude"
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract (result text, result metadata, hook events, tool uses) from claude's stdout.

    Written defensively: the streaming envelope carries message types this code
    does not need to know about, so anything unrecognised is ignored rather than
    treated as an error.
    """
    if engine == "codex":
        return parse_codex_output(stdout)
    if not streaming:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, {}, [], []
        if isinstance(payload, dict):
            return str(payload.get("result", "")), payload, [], []
        return stdout, {}, [], []

    result_text = ""
    meta: dict[str, Any] = {}
    hook_events: list[dict[str, Any]] = []
    tool_uses: list[dict[str, Any]] = []
    assistant_text: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = str(event.get("type", ""))
        # Hook lifecycle arrives as {"type": "system", "subtype": "hook_started" |
        # "hook_response", "hook_name": "PostToolUse:Write", "output": "..."}.
        if "hook" in etype or "hook" in str(event.get("subtype", "")) or "hook_name" in event:
            hook_events.append(event)
        if etype == "result":
            result_text = str(event.get("result", ""))
            meta = event
        elif etype == "assistant":
            for block in (event.get("message", {}) or {}).get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    assistant_text.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    tool_uses.append(block)

    return result_text or "\n".join(assistant_text), meta, hook_events, tool_uses


def parse_codex_output(
    stdout: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract the final message and tool-like events from ``codex exec --json``."""
    messages: list[str] = []
    meta: dict[str, Any] = {}
    tool_uses: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"turn.completed", "turn.failed"}:
            meta = event
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message" and event.get("type") == "item.completed":
            messages.append(str(item.get("text", "")))
        elif item.get("type") in {
            "command_execution", "mcp_tool_call", "web_search", "file_change"
        }:
            tool_uses.append({
                "type": "tool_use",
                "name": str(item.get("type", "")),
                "input": item,
            })
    if meta.get("type") == "turn.failed":
        meta["is_error"] = True
    return (messages[-1] if messages else ""), meta, [], tool_uses


def unusable_reason(returncode: int, result: str, stderr: str, meta: dict[str, Any]) -> str:
    """Return why a trial cannot be scored, or "" when it can.

    This exists to protect the absence checks. `regex_absent` passes on an empty
    string and on an English-language API error, so a trial that never really
    ran would otherwise be counted as compliance and quietly move a verdict.
    """
    if returncode != 0 and not result:
        return f"exit {returncode}: {stderr.strip()[:400]}"
    if meta.get("is_error"):
        return f"claude reported an error: {(result or stderr)[:300]}"
    if meta and meta.get("subtype") not in (None, "success"):
        return f"claude subtype={meta.get('subtype')}: {result[:300]}"
    if not result.strip():
        return "empty result — nothing to score"
    return ""


def run_trial(case: Case, arm: str, model: str, index: int, run_dir: Path,
              keep_workspace: bool = False) -> Trial:
    """Execute one probe and score it. Never raises — failures become Trials."""
    workspace = Path(tempfile.mkdtemp(prefix=f"he-{case.id}-{arm}-"))
    prompt_dir = Path(tempfile.mkdtemp(prefix=f"he-sp-{case.id}-{arm}-"))
    eval_codex_home: Path | None = None
    raw_path = run_dir / "raw" / f"{case.id}__{model}__{arm}__{index}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    trial = Trial(case_id=case.id, arm=arm, model=model, index=index, passed=False,
                  raw_path=str(raw_path))
    started = time.monotonic()
    try:
        if case.skill_dir is not None and arm == TREATMENT:
            # Only SKILL.md is planted: the trigger decision is made from the
            # listing, which is built from the manifest — and some real skill
            # dirs carry tens of MB of reference material irrelevant to it.
            skill_root = ".agents" if case.engine == "codex" else ".claude"
            dest = workspace / skill_root / "skills" / case.skill_dir.name
            dest.mkdir(parents=True)
            source_text = (case.skill_dir / "SKILL.md").read_text(encoding="utf-8")
            if case.skill_canary:
                # Activation checks measure discovery of the real metadata, not
                # compliance with a potentially long or conflicting real body.
                # Preserve frontmatter byte-for-byte and replace only the
                # treatment copy's body with a deterministic canary.
                boundary = source_text.find("\n---", 4)
                frontmatter = source_text[:boundary + 4] if boundary >= 0 else source_text
                if case.skill_eval_name:
                    frontmatter = re.sub(
                        r"(?m)^name:\s*.*$", f"name: {case.skill_eval_name}",
                        frontmatter, count=1,
                    )
                source_text = (
                    frontmatter
                    + "\n\n# Harness activation self-check\n\n"
                    + "Reply with exactly `"
                    + case.skill_canary
                    + "` and nothing else. Do not execute any normal workflow.\n"
                )
            (dest / "SKILL.md").write_text(source_text, encoding="utf-8")
        sysprompt = materialize_system_prompt(case, arm, prompt_dir)
        if case.engine == "codex" and sysprompt is not None:
            shutil.copy2(sysprompt, workspace / "AGENTS.md")
        argv = build_argv(case, arm, model, sysprompt)
        trial.argv = argv
        proc_env = None
        if case.engine == "codex":
            # --ignore-user-config does not suppress global skills. A minimal
            # CODEX_HOME containing auth only is the actual clean-room control.
            eval_codex_home = Path(tempfile.mkdtemp(prefix="he-codex-home-"))
            live_codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
            for auth_name in ("auth.json", ".credentials.json"):
                source = live_codex_home / auth_name
                if source.is_file():
                    shutil.copy2(source, eval_codex_home / auth_name)
            proc_env = os.environ.copy()
            proc_env["CODEX_HOME"] = str(eval_codex_home)
        proc = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=case.timeout_s,
            env=proc_env,
        )
        stdout, stderr = proc.stdout, proc.stderr
        raw_path.write_text(
            f"$ {' '.join(argv)}\n\n--- exit {proc.returncode} ---\n"
            f"--- stdout ---\n{stdout[:MAX_STORED_STDOUT]}\n--- stderr ---\n{stderr[:20000]}\n",
            encoding="utf-8",
        )

        result, meta, hook_events, tool_uses = parse_output(
            stdout, case.needs_stream, case.engine
        )
        trial.result = result
        trial.cost_usd = meta.get("total_cost_usd")
        trial.num_turns = meta.get("num_turns")
        trial.hook_event_count = len(hook_events)
        trial.tool_use_count = len(tool_uses)

        trial.error = unusable_reason(proc.returncode, result, stderr, meta)
        if trial.error:
            return trial

        ctx = TrialContext(result=result, stderr=stderr, hook_events=hook_events,
                           tool_uses=tool_uses, workspace=workspace)
        passed, results = run_checks(case.checks, ctx)
        trial.passed = passed
        trial.checks = [asdict(r) for r in results]
    except subprocess.TimeoutExpired:
        trial.error = f"timeout after {case.timeout_s}s"
    except (SourceDrifted, ValueError) as exc:
        trial.error = str(exc)
    except OSError as exc:
        trial.error = f"{type(exc).__name__}: {exc}"
    finally:
        trial.duration_s = round(time.monotonic() - started, 2)
        shutil.rmtree(prompt_dir, ignore_errors=True)
        if eval_codex_home is not None:
            shutil.rmtree(eval_codex_home, ignore_errors=True)
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
    return trial


def run_case(case: Case, models: Iterable[str], n: int, run_dir: Path,
             workers: int = 3, keep_workspace: bool = False,
             on_trial=None) -> list[Trial]:
    """Run both arms of one case across the requested models."""
    jobs = [
        (case, arm, model, i)
        for model in models
        for arm in ARMS
        for i in range(n)
    ]
    trials: list[Trial] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [
            pool.submit(run_trial, c, arm, model, i, run_dir, keep_workspace)
            for (c, arm, model, i) in jobs
        ]
        for fut in futures:
            trial = fut.result()
            trials.append(trial)
            if on_trial is not None:
                on_trial(trial)
    return trials


def run_cases_parallel(
    cases: Iterable[Case], run_dir: Path, workers: int = 6,
    model_override: Iterable[str] | None = None, n_override: int | None = None,
    keep_workspace: bool = False, existing: Iterable[Trial] = (), on_trial=None,
) -> list[Trial]:
    """Run many cases through one worker pool, skipping persisted trial keys."""
    completed = {
        (t.case_id, t.arm, t.model, t.index)
        for t in existing
        if not t.error and (t.arm == CONTROL or t.passed)
    }
    jobs: list[tuple[Case, str, str, int]] = []
    override = tuple(model_override) if model_override is not None else None
    for case in cases:
        models = override if override is not None else case.models
        n = n_override or case.n
        for model in models:
            for arm in ARMS:
                for index in range(n):
                    if (case.id, arm, model, index) not in completed:
                        jobs.append((case, arm, model, index))

    trials: list[Trial] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(run_trial, case, arm, model, index, run_dir, keep_workspace): case
            for case, arm, model, index in jobs
        }
        for future in as_completed(futures):
            trial = future.result()
            trials.append(trial)
            # Checkpoint each completed trial so interruption is resumable.
            write_trials([trial], run_dir)
            if on_trial is not None:
                on_trial(trial)
    return trials


def write_trials(trials: list[Trial], run_dir: Path) -> Path:
    """Persist trials as JSONL. One writer, called after the pool has drained."""
    path = run_dir / "trials.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for t in trials:
            fh.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")
    return path


def load_trials(run_dir: Path) -> list[Trial]:
    path = run_dir / "trials.jsonl"
    latest: dict[tuple[str, str, str, int], Trial] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            trial = Trial(**json.loads(line))
            latest[(trial.case_id, trial.arm, trial.model, trial.index)] = trial
    return list(latest.values())


def check_cli_available(engine: str = "claude") -> str:
    """Return the selected engine version, or raise with an actionable message."""
    command = [engine, "--version"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{engine} CLI not runnable: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"{engine} --version failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


__all__ = [
    "ARMS", "CONTROL", "TREATMENT", "Trial", "build_argv", "check_cli_available",
    "load_trials", "materialize_system_prompt", "parse_output", "read_fragment",
    "run_case", "run_cases_parallel", "run_trial", "unusable_reason", "write_trials",
    "CheckResult",
]
