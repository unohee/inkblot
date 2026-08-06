"""Case definitions and their loader.

A case is one falsifiable claim about one harness artifact. It carries the probe
prompt, the fragment under test, the deterministic checks, and the metadata
needed to detect drift against the live harness.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

CHECK_KINDS = {
    "regex_present",
    "regex_absent",
    "contains",
    "not_contains",
    "hook_fired",
    "tool_used",
    "tool_not_used",
    "file_exists",
    "file_contains",
}

TARGET_KINDS = {"rule", "skill", "hook", "command", "meta"}

# How an arm is isolated from the live harness. Measured against claude 2.1.221:
#   safe-mode      --safe-mode. Cleanest: user CLAUDE.md, skills, commands, MCP
#                  servers and hooks are all off. But it also suppresses hooks
#                  supplied through --settings, so hook cases cannot use it.
#   settings-only  --setting-sources "" --strict-mcp-config. Hooks from
#                  --settings do fire. Caveat: this does not suppress CLAUDE.md
#                  discovery, so both arms carry the live user memory equally.
#   project        --setting-sources "project" --strict-mcp-config. Loads the
#                  trial workspace's .claude/ (skills, settings) and nothing of
#                  the user's. Measured 2.1.221: skills planted in
#                  workspace/.claude/skills DO get listed; they do not under
#                  the other two modes.
ISOLATION_MODES = {"safe-mode", "settings-only", "project", "codex-project"}
ENGINES = {"claude", "codex"}


class CaseError(ValueError):
    """Raised when a case file is malformed. Always names the offending path."""


@dataclass(frozen=True)
class Check:
    kind: str
    why: str
    pattern: str | None = None
    scope: str = "result"
    path: str | None = None

    @property
    def needs_stream(self) -> bool:
        """Event-level checks require the streaming transcript, not just the result."""
        return self.kind in {"hook_fired", "tool_used", "tool_not_used"}


@dataclass(frozen=True)
class Target:
    kind: str
    source: Path | None
    anchor: str
    sha256: str | None = None
    # 1-indexed inclusive line ranges to cut for the control arm in excision
    # mode. Measurement comments adjacent to a rule belong in the range too —
    # a control doc that still says "이 줄은 load-bearing" leaks the rule.
    excise_lines: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    path: Path
    engine: str
    target: Target
    prompt: str
    checks: tuple[Check, ...]
    measurement: str = "ablation"
    models: tuple[str, ...] = ("opus", "sonnet")
    n: int = 6
    tools: tuple[str, ...] = ("",)  # ("",) disables every tool — cheapest, most stable
    permission_mode: str = "manual"
    max_budget_usd: float = 0.50
    timeout_s: int = 240
    baseline_fragments: tuple[Path, ...] = ()
    fragment: Path | None = None
    arm_settings: Path | None = None
    # Directory of a real skill (holding SKILL.md); planted into the treatment
    # workspace's .claude/skills/<name>/ so the trigger listing is the only
    # difference between arms. Requires isolation = "project".
    skill_dir: Path | None = None
    # Optional transport-only marker used as the copied treatment skill body.
    # This proves metadata discovery/activation, not real workflow quality.
    skill_canary: str = ""
    skill_eval_name: str = ""
    isolation: str = "safe-mode"
    # "addition": treatment = baselines + fragment, control = baselines.
    #   Measures "does this text work when it is read?" — a rule-friendly
    #   condition, because the fragment competes with nothing.
    # "excision": treatment = baselines + full target.source, control =
    #   baselines + source with target.excise_lines cut. Measures the rule's
    #   marginal contribution inside the whole document it actually ships in.
    mode: str = "addition"
    alpha: float = 0.05
    deterministic_alternative: str = ""
    enabled: bool = True
    notes: str = ""

    @property
    def needs_stream(self) -> bool:
        return any(c.needs_stream for c in self.checks)


def sha256_file(path: Path) -> str | None:
    """Hash a harness source file, or None when it no longer exists."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _resolve(base: Path, value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _resolve_many(base: Path, value: str | list[str] | None) -> tuple[Path, ...]:
    """Baselines may be one path or several, applied to both arms in order."""
    if not value:
        return ()
    values = [value] if isinstance(value, str) else list(value)
    return tuple(p for p in (_resolve(base, v) for v in values) if p is not None)


def _require(table: dict, key: str, path: Path) -> object:
    if key not in table:
        raise CaseError(f"{path}: missing required key '{key}'")
    return table[key]


def load_case(path: Path) -> Case:
    """Parse and validate one case file."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise CaseError(f"{path}: invalid TOML — {exc}") from exc

    schema = raw.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise CaseError(f"{path}: unsupported schema {schema} (expected {SCHEMA_VERSION})")

    base = path.parent
    target_raw = _require(raw, "target", path)
    if not isinstance(target_raw, dict):
        raise CaseError(f"{path}: [target] must be a table")
    if target_raw.get("kind") not in TARGET_KINDS:
        raise CaseError(f"{path}: target.kind must be one of {sorted(TARGET_KINDS)}")

    excise_lines: list[tuple[int, int]] = []
    for i, rng in enumerate(target_raw.get("excise_lines", []) or []):
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(v, int) for v in rng)):
            raise CaseError(f"{path}: target.excise_lines[{i}] must be [start, end] (1-indexed)")
        start, end = rng
        if not (1 <= start <= end):
            raise CaseError(f"{path}: target.excise_lines[{i}]: bad range [{start}, {end}]")
        excise_lines.append((start, end))
    excise_lines.sort()
    for (s1, e1), (s2, _) in zip(excise_lines, excise_lines[1:]):
        if s2 <= e1:
            raise CaseError(f"{path}: target.excise_lines ranges overlap at line {s2}")

    probe = _require(raw, "probe", path)
    if not isinstance(probe, dict):
        raise CaseError(f"{path}: [probe] must be a table")

    checks_raw = raw.get("checks") or []
    if not checks_raw:
        raise CaseError(f"{path}: at least one [[checks]] entry is required")

    checks: list[Check] = []
    for i, c in enumerate(checks_raw):
        kind = c.get("kind")
        if kind not in CHECK_KINDS:
            raise CaseError(f"{path}: checks[{i}].kind={kind!r} not in {sorted(CHECK_KINDS)}")
        if kind in {"regex_present", "regex_absent", "contains", "not_contains", "file_contains"}:
            if not c.get("pattern"):
                raise CaseError(f"{path}: checks[{i}] ({kind}) requires 'pattern'")
        if kind in {"file_exists", "file_contains"} and not c.get("path"):
            raise CaseError(f"{path}: checks[{i}] ({kind}) requires 'path'")
        checks.append(
            Check(
                kind=kind,
                why=c.get("why", ""),
                pattern=c.get("pattern"),
                scope=c.get("scope", "result"),
                path=c.get("path"),
            )
        )

    arms = raw.get("arms", {})
    verdict = raw.get("verdict", {})
    tools = probe.get("tools", [""])
    if isinstance(tools, str):
        tools = [tools]

    case = Case(
        id=str(_require(raw, "id", path)),
        title=str(raw.get("title", raw["id"])),
        path=path,
        engine=str(raw.get("engine", "claude")),
        target=Target(
            kind=target_raw["kind"],
            source=_resolve(base, target_raw.get("source")),
            anchor=str(target_raw.get("anchor", "")),
            sha256=target_raw.get("sha256"),
            excise_lines=tuple(excise_lines),
        ),
        prompt=str(_require(probe, "prompt", path)),
        checks=tuple(checks),
        measurement=str(raw.get("measurement", "ablation")),
        models=tuple(probe.get("models", ["opus", "sonnet"])),
        n=int(probe.get("n", 6)),
        tools=tuple(tools),
        permission_mode=str(probe.get("permission_mode", "manual")),
        max_budget_usd=float(probe.get("max_budget_usd", 0.50)),
        timeout_s=int(probe.get("timeout_s", 240)),
        baseline_fragments=_resolve_many(base, arms.get("baseline_fragment")),
        fragment=_resolve(base, arms.get("fragment")),
        arm_settings=_resolve(base, arms.get("settings")),
        skill_dir=_resolve(base, arms.get("skill")),
        skill_canary=str(arms.get("skill_canary", "")),
        skill_eval_name=str(arms.get("skill_eval_name", "")),
        isolation=str(arms.get("isolation", "safe-mode")),
        mode=str(arms.get("mode", "addition")),
        alpha=float(verdict.get("alpha", 0.05)),
        deterministic_alternative=str(verdict.get("deterministic_alternative", "")),
        enabled=bool(raw.get("enabled", True)),
        notes=str(raw.get("notes", "")),
    )

    if case.mode not in ("addition", "excision"):
        raise CaseError(f"{path}: arms.mode must be \"addition\" or \"excision\"")
    if case.engine not in ENGINES:
        raise CaseError(f"{path}: engine must be one of {sorted(ENGINES)}")
    if case.engine == "codex" and case.isolation != "codex-project":
        raise CaseError(f'{path}: codex cases require arms.isolation = "codex-project"')
    if case.engine == "claude" and case.isolation == "codex-project":
        raise CaseError(f'{path}: claude cases cannot use arms.isolation = "codex-project"')
    if case.measurement not in {"ablation", "activation"}:
        raise CaseError(f'{path}: measurement must be "ablation" or "activation"')
    if case.mode == "excision":
        if case.target.source is None:
            raise CaseError(f"{path}: excision mode needs target.source (the document to cut from)")
        if not case.target.excise_lines:
            raise CaseError(f"{path}: excision mode needs target.excise_lines")
        if case.target.sha256 is None:
            raise CaseError(
                f"{path}: excision mode needs target.sha256 — line ranges are meaningless "
                f"against an unpinned document"
            )
    elif case.fragment is None and case.arm_settings is None and case.skill_dir is None:
        raise CaseError(
            f"{path}: a case must vary something — set arms.fragment, arms.settings, or arms.skill"
        )
    if case.skill_dir is not None:
        expected_isolation = "codex-project" if case.engine == "codex" else "project"
        if case.isolation != expected_isolation:
            raise CaseError(
                f'{path}: arms.skill for {case.engine} requires arms.isolation = '
                f'"{expected_isolation}"'
            )
        if not (case.skill_dir / "SKILL.md").exists():
            raise CaseError(f"{path}: arms.skill has no SKILL.md: {case.skill_dir}")
    if case.skill_canary and case.skill_dir is None:
        raise CaseError(f"{path}: arms.skill_canary requires arms.skill")
    if case.skill_eval_name and not case.skill_canary:
        raise CaseError(f"{path}: arms.skill_eval_name requires arms.skill_canary")
    for label, p in ([("arms.fragment", case.fragment), ("arms.settings", case.arm_settings)]
                     + [("arms.baseline_fragment", b) for b in case.baseline_fragments]):
        if p is not None and not p.exists():
            raise CaseError(f"{path}: {label} points at a missing file: {p}")
    if case.n < 1:
        raise CaseError(f"{path}: probe.n must be >= 1")
    if case.isolation not in ISOLATION_MODES:
        raise CaseError(f"{path}: arms.isolation must be one of {sorted(ISOLATION_MODES)}")
    if case.arm_settings is not None and case.isolation == "safe-mode":
        raise CaseError(
            f"{path}: arms.settings requires arms.isolation = \"settings-only\" — measured on "
            f"claude 2.1.221, --safe-mode suppresses hooks even when they arrive via --settings, "
            f"so both arms would come out identical for the wrong reason"
        )
    return case


def load_cases(root: Path, recursive: bool = False) -> list[Case]:
    """Load every case file under `root`, sorted by id.

    Non-recursive by default so that `cases/selftest/` stays out of normal runs.
    """
    files = sorted(root.rglob("*.toml") if recursive else root.glob("*.toml"))
    cases = [load_case(f) for f in files]
    seen: dict[str, Path] = {}
    for c in cases:
        if c.id in seen:
            raise CaseError(f"duplicate case id {c.id!r}: {seen[c.id]} and {c.path}")
        seen[c.id] = c.path
    return sorted(cases, key=lambda c: c.id)


def excise(text: str, ranges: tuple[tuple[int, int], ...]) -> tuple[str, str]:
    """Cut 1-indexed inclusive line ranges from `text`.

    Returns (remaining, removed). Ranges must be sorted and non-overlapping,
    which load_case guarantees. Raises ValueError when a range runs past the
    end of the document — that means the ranges were written against a
    different version and the case must not run.
    """
    lines = text.splitlines(keepends=True)
    for start, end in ranges:
        if end > len(lines):
            raise ValueError(f"excise range [{start}, {end}] exceeds document ({len(lines)} lines)")
    cut = {i for start, end in ranges for i in range(start - 1, end)}
    remaining = "".join(line for i, line in enumerate(lines) if i not in cut)
    removed = "".join(line for i, line in enumerate(lines) if i in cut)
    return remaining, removed


@dataclass
class DriftReport:
    case_id: str
    source: Path
    recorded: str | None
    current: str | None
    status: str  # "ok" | "changed" | "missing" | "unrecorded"
    extra: dict = field(default_factory=dict)


def check_drift(cases: list[Case]) -> list[DriftReport]:
    """Compare each case's recorded source hash against the live harness file.

    A changed hash does not mean the case is wrong — it means the measurement is
    stale and the fragment should be re-extracted before the verdict is trusted.
    """
    out: list[DriftReport] = []
    for case in cases:
        src = case.target.source
        if src is None:
            continue
        current = sha256_file(src)
        if current is None:
            status = "missing"
        elif case.target.sha256 is None:
            status = "unrecorded"
        elif current == case.target.sha256:
            status = "ok"
        else:
            status = "changed"
        out.append(DriftReport(case.id, src, case.target.sha256, current, status))
    return out
