from pathlib import Path

from harness_eval.codex_audit import codex_skill_catalog
from harness_eval.codex_skills import build_skill_activation_cases


def write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Trigger {name}.\n---\n\nInstructions.\n",
        encoding="utf-8",
    )


def test_catalog_follows_top_level_skill_symlinks(tmp_path):
    home = tmp_path / ".codex"
    external = tmp_path / "external-skill"
    write_skill(external, "external")
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "external").symlink_to(external, target_is_directory=True)

    entries = codex_skill_catalog(home)
    assert [entry["name"] for entry in entries] == ["external"]
    assert entries[0]["sha256"]


def test_activation_cases_deduplicate_names_and_add_canaries(tmp_path):
    home = tmp_path / ".codex"
    write_skill(home / "skills" / "first", "same-name")
    write_skill(home / "plugins" / "cache" / "p" / "skills" / "second", "same-name")
    write_skill(home / "skills" / "other", "Other Skill")

    cases = build_skill_activation_cases(home)
    assert [case.id for case in cases] == ["codex-skill-other-skill", "codex-skill-same-name"]
    assert all(case.engine == "codex" for case in cases)
    assert all(case.isolation == "codex-project" for case in cases)
    assert all(case.skill_canary.startswith("CODEX_SKILL_OK_") for case in cases)
    assert all(case.n == 1 for case in cases)
