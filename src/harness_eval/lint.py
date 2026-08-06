"""Static no-op detection — the free CI layer.

Some harness artifacts can be falsified without spending a single token: a hook
registered against a script that does not exist, a stub that exits 0 with no
side effects, a rules file the instructions point at but nobody ever wrote.
These are no-ops by construction, so they are found by reading files rather
than by running an ablation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "deno", "uv", "uvx", "env"}

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


@dataclass
class Finding:
    code: str
    severity: str
    subject: str
    message: str
    evidence: str = ""
    fix: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code, "severity": self.severity, "subject": self.subject,
            "message": self.message, "evidence": self.evidence, "fix": self.fix,
        }


@dataclass
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    scanned: dict[str, int] = field(default_factory=dict)

    def add(self, *args, **kwargs) -> None:
        self.findings.append(Finding(*args, **kwargs))

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.code, f.subject))


def expand(path_str: str, project_dir: Path) -> Path:
    """Resolve a hook command path, substituting the vars Claude Code injects."""
    text = path_str.replace("$CLAUDE_PROJECT_DIR", str(project_dir))
    text = text.replace("${CLAUDE_PROJECT_DIR}", str(project_dir))
    return Path(os.path.expandvars(text)).expanduser()


def script_from_command(command: str, project_dir: Path) -> tuple[Path | None, bool]:
    """Pull the script path out of a hook command.

    Returns (path, invoked_via_interpreter). The second value matters because a
    script run as `python3 foo.py` does not need the executable bit, while one
    run as `~/.claude/hooks/foo.sh` does.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return None, False

    via_interpreter = Path(tokens[0]).name in INTERPRETERS
    candidates = tokens[1:] if via_interpreter else tokens[:1]
    for tok in candidates:
        if tok.startswith("-"):
            continue
        if "/" in tok or tok.endswith((".sh", ".py", ".js", ".ts")):
            return expand(tok, project_dir), via_interpreter
    return None, via_interpreter


def is_trivial_noop(path: Path) -> tuple[bool, str]:
    """True when a script provably does nothing but exit successfully."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"unreadable: {exc}"
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    if not lines:
        return True, "empty after comments"
    trivial = {"import sys", "sys.exit(0)", "exit 0", "exit(0)", "exit", "true", ":"}
    if all(line in trivial for line in lines):
        return True, "; ".join(lines)
    return False, ""


def iter_hook_commands(settings: dict) -> list[tuple[str, str]]:
    """Yield (event_name, command) for every command hook in a settings file."""
    out: list[tuple[str, str]] = []
    for event, entries in (settings.get("hooks") or {}).items():
        for entry in entries or []:
            for hook in (entry or {}).get("hooks", []) or []:
                if hook.get("type") == "command" and hook.get("command"):
                    out.append((event, str(hook["command"])))
    return out


def lint_settings(path: Path, project_dir: Path, report: LintReport) -> None:
    if not path.exists():
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.add("L000", "error", str(path), f"settings file is not valid JSON — Claude Code "
                   f"silently ignores it in print mode", evidence=str(exc))
        return

    commands = iter_hook_commands(settings)
    report.scanned["hooks"] = report.scanned.get("hooks", 0) + len(commands)

    for event, command in commands:
        subject = f"{path.name}:{event}"
        script, via_interpreter = script_from_command(command, project_dir)
        if script is None:
            report.add("L005", "info", subject, "hook command has no resolvable script path",
                       evidence=command)
            continue
        if not script.exists():
            scoped = "$CLAUDE_PROJECT_DIR" in command
            report.add(
                "L001", "error", subject,
                "hook registered against a script that does not exist"
                + (" in this project" if scoped else ""),
                evidence=f"{command} -> {script}",
                fix="delete the hook entry, or ship the script (project-scoped hooks need one "
                    "per repo)" if scoped else "delete the hook entry or restore the script",
            )
            continue
        if not via_interpreter and not os.access(script, os.X_OK):
            report.add("L002", "error", subject, "hook script is not executable — it can never run",
                       evidence=str(script), fix=f"chmod +x {script}")
        noop, evidence = is_trivial_noop(script)
        if noop:
            report.add("L003", "warn", subject, "hook script is a no-op (exits 0, no side effects)",
                       evidence=f"{script}: {evidence}",
                       fix="remove the registration, or implement the check it claims to perform")


def lint_rule_references(claude_md: Path, report: LintReport) -> None:
    """Every rules/*.md path named in CLAUDE.md must exist, or the pointer is dead."""
    if not claude_md.exists():
        report.add("L004", "error", str(claude_md), "CLAUDE.md not found")
        return
    text = claude_md.read_text(encoding="utf-8")
    referenced: set[str] = set()

    for match in re.finditer(r"~/\.claude/rules/\{([^}]+)\}\.md", text):
        referenced.update(f"{name.strip()}.md" for name in match.group(1).split(","))
    for match in re.finditer(r"~/\.claude/rules/([A-Za-z0-9_.-]+\.md)", text):
        referenced.add(match.group(1))

    rules_dir = claude_md.parent / "rules"
    report.scanned["rule_refs"] = len(referenced)
    for name in sorted(referenced):
        if not (rules_dir / name).exists():
            report.add("L004", "error", f"CLAUDE.md -> rules/{name}",
                       "CLAUDE.md points at a rules file that does not exist",
                       fix="write the rules file or drop the reference")

    if rules_dir.is_dir():
        for rule in sorted(rules_dir.glob("*.md")):
            if rule.name not in referenced:
                report.add("L006", "info", f"rules/{rule.name}",
                           "rules file exists but CLAUDE.md never points at it — nothing loads it",
                           fix="reference it from CLAUDE.md or move it out of rules/")


def lint_skills(skills_dir: Path, report: LintReport) -> None:
    if not skills_dir.is_dir():
        return
    dirs = [d for d in sorted(skills_dir.iterdir()) if d.is_dir()]
    report.scanned["skills"] = len(dirs)
    for skill in dirs:
        manifest = skill / "SKILL.md"
        if not manifest.exists():
            report.add("L007", "error", f"skills/{skill.name}",
                       "skill directory has no SKILL.md — it will never be offered",
                       fix="add SKILL.md with name/description frontmatter, or delete the dir")
            continue
        head = manifest.read_text(encoding="utf-8", errors="replace")[:600]
        if not head.lstrip().startswith("---") or "description:" not in head:
            report.add("L008", "warn", f"skills/{skill.name}",
                       "SKILL.md lacks frontmatter with a description — the model cannot route to it",
                       fix="add YAML frontmatter with name and description")


def lint_commands(commands_dir: Path, report: LintReport) -> None:
    """A command file with no body is a slash command that expands to nothing."""
    if not commands_dir.is_dir():
        return
    files = sorted(commands_dir.glob("*.md"))
    report.scanned["commands"] = len(files)
    for cmd in files:
        text = cmd.read_text(encoding="utf-8", errors="replace")
        body = _strip_frontmatter(text).strip()
        if not body:
            report.add("L009", "error", f"commands/{cmd.name}",
                       "command file has no body — invoking it expands to nothing",
                       fix="write the command's instructions, or delete the file")
        elif len(body) < 40:
            report.add("L010", "warn", f"commands/{cmd.name}",
                       f"command body is {len(body)} chars — likely a leftover stub",
                       evidence=body.replace("\n", " ")[:120])


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def lint_harness(claude_dir: Path, project_dir: Path) -> LintReport:
    """Run every static check over a Claude Code configuration directory."""
    report = LintReport()
    lint_settings(claude_dir / "settings.json", project_dir, report)
    lint_settings(claude_dir / "settings.local.json", project_dir, report)
    lint_rule_references(claude_dir / "CLAUDE.md", report)
    lint_skills(claude_dir / "skills", report)
    lint_commands(claude_dir / "commands", report)
    return report
