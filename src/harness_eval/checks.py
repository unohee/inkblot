"""Deterministic checkers.

No LLM judges. A trial either satisfies every check or it fails — so a verdict
can be reproduced from the stored transcript without spending a token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    kind: str
    passed: bool
    detail: str


@dataclass
class TrialContext:
    """Everything a checker is allowed to look at."""

    result: str = ""
    stderr: str = ""
    hook_events: list[dict[str, Any]] | None = None
    tool_uses: list[dict[str, Any]] | None = None
    workspace: Path | None = None

    def scope_text(self, scope: str) -> str:
        if scope == "result":
            return self.result
        if scope == "stderr":
            return self.stderr
        if scope == "all":
            return f"{self.result}\n{self.stderr}"
        raise ValueError(f"unknown check scope: {scope!r}")


def _excerpt(text: str, match: re.Match[str], width: int = 40) -> str:
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return text[start:end].replace("\n", "\\n")


def run_check(check, ctx: TrialContext) -> CheckResult:
    """Evaluate one check against a completed trial."""
    kind = check.kind

    if kind in {"regex_present", "regex_absent"}:
        text = ctx.scope_text(check.scope)
        m = re.search(check.pattern, text, re.MULTILINE)
        found = m is not None
        want = kind == "regex_present"
        detail = f"matched {_excerpt(text, m)!r}" if m else "no match"
        return CheckResult(kind, found == want, detail)

    if kind in {"contains", "not_contains"}:
        text = ctx.scope_text(check.scope)
        found = check.pattern in text
        want = kind == "contains"
        return CheckResult(kind, found == want, "found" if found else "absent")

    if kind in {"tool_used", "tool_not_used"}:
        uses = ctx.tool_uses or []
        hit = [u for u in uses if re.search(check.pattern, _tool_signature(u))]
        found = bool(hit)
        want = kind == "tool_used"
        return CheckResult(kind, found == want,
                           f"{len(hit)}/{len(uses)} tool_use events matched {check.pattern!r}")

    if kind == "hook_fired":
        events = ctx.hook_events or []
        needle = check.pattern or ""
        hit = [e for e in events if needle in _event_signature(e)]
        return CheckResult(kind, bool(hit), f"{len(hit)}/{len(events)} hook events matched {needle!r}")

    if kind in {"file_exists", "file_contains"}:
        if ctx.workspace is None:
            return CheckResult(kind, False, "no workspace captured")
        target = ctx.workspace / check.path
        if not target.exists():
            return CheckResult(kind, False, f"missing {check.path}")
        if kind == "file_exists":
            return CheckResult(kind, True, f"found {check.path}")
        body = target.read_text(encoding="utf-8", errors="replace")
        ok = re.search(check.pattern, body, re.MULTILINE) is not None
        return CheckResult(kind, ok, f"{check.path}: pattern {'found' if ok else 'absent'}")

    raise ValueError(f"unhandled check kind: {kind!r}")


def _tool_signature(use: dict[str, Any]) -> str:
    """Flatten a tool_use block into 'Name {input-json}' for regex matching."""
    import json as _json
    name = str(use.get("name", ""))
    try:
        payload = _json.dumps(use.get("input", {}), ensure_ascii=False)
    except (TypeError, ValueError):
        payload = str(use.get("input", ""))
    return f"{name} {payload}"


def _event_signature(event: dict[str, Any]) -> str:
    """Flatten a hook lifecycle event into one searchable string."""
    parts = [str(event.get("type", "")), str(event.get("subtype", ""))]
    for key in ("hook_name", "hook_event", "hook_event_name", "command", "tool_name",
                "outcome", "exit_code"):
        if key in event:
            parts.append(str(event[key]))
    for key in ("output", "stdout", "stderr", "hook_response"):
        if event.get(key):
            parts.append(str(event[key]))
    return " ".join(parts)


def run_checks(checks, ctx: TrialContext) -> tuple[bool, list[CheckResult]]:
    """A trial passes only when every check passes."""
    results = [run_check(c, ctx) for c in checks]
    return all(r.passed for r in results), results
