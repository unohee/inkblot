"""Build transport-level activation cases for every installed Codex skill."""

from __future__ import annotations

import re
from pathlib import Path

from .codex_audit import codex_skill_catalog
from .model import Case, Check, Target


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unnamed"


def build_skill_activation_cases(codex_home: Path) -> list[Case]:
    """Create one activation case per unique skill name.

    Duplicate cache copies are represented by the first path in stable sorted
    order. Static audit still checks every physical copy independently.
    """
    cases: list[Case] = []
    seen: set[str] = set()
    for entry in codex_skill_catalog(codex_home):
        name = entry["name"]
        if not name or name in seen:
            continue
        seen.add(name)
        slug = _slug(name)
        marker = f"CODEX_SKILL_OK_{slug.upper().replace('-', '_')}"
        skill_file = Path(entry["path"])
        is_system = ".system" in skill_file.parts
        eval_name = f"inkblot-canary-{slug}" if is_system else name
        cases.append(Case(
            id=f"codex-skill-{slug}",
            title=f"Codex skill activation — {name}",
            path=skill_file,
            engine="codex",
            target=Target(
                kind="skill", source=skill_file,
                anchor=f"skill name={name}", sha256=entry["sha256"],
            ),
            prompt=(
                f"Use the ${eval_name} skill. Run only its activation self-check; "
                "do not perform the normal workflow or any external action."
            ),
            checks=(Check(
                kind="contains", pattern=marker,
                why="The planted treatment skill must be discovered and invoked.",
            ),),
            measurement="activation",
            models=("gpt-5.6-terra",),
            n=1,
            timeout_s=180,
            skill_dir=skill_file.parent,
            skill_canary=marker,
            skill_eval_name=eval_name if is_system else "",
            isolation="codex-project",
            notes=(
                "Transport-only activation check. This does not measure the "
                "skill's real workflow quality or natural-language trigger rate."
            ),
        ))
    return sorted(cases, key=lambda case: case.id)
