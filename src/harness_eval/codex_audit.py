"""Static audit of the installed Codex harness without exposing credentials."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import hashlib
from pathlib import Path
from typing import Any


def _frontmatter(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    if not text.startswith("---\n"):
        return "", ""
    end = text.find("\n---", 4)
    if end < 0:
        return "", ""
    header = text[4:end]
    values: dict[str, str] = {}
    for line in header.splitlines():
        match = re.match(r"^(name|description):\s*[\"']?(.*?)[\"']?\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values.get("name", ""), values.get("description", "")


def _skill_files(codex_home: Path) -> list[Path]:
    roots = [codex_home / "skills", codex_home.parent / ".agents" / "skills",
             codex_home / "plugins" / "cache"]
    found: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("SKILL.md"):
            found[str(path.resolve())] = path
        # pathlib does not recurse into directory symlinks. Personal skills may
        # intentionally be symlinked to a development checkout.
        for child in root.iterdir() if root.is_dir() else ():
            candidate = child / "SKILL.md"
            if child.is_symlink() and candidate.is_file():
                found[str(candidate.resolve())] = candidate
    return sorted(found.values(), key=lambda p: str(p))


def codex_skill_catalog(codex_home: Path) -> list[dict[str, str]]:
    """Return every physical installed skill plus stable provenance metadata."""
    entries: list[dict[str, str]] = []
    for path in _skill_files(codex_home):
        name, description = _frontmatter(path)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = ""
        entries.append({
            "name": name,
            "description": description,
            "path": str(path),
            "resolved_path": str(path.resolve()),
            "sha256": digest,
        })
    return entries


def _plugin_manifests(codex_home: Path) -> list[Path]:
    root = codex_home / "plugins" / "cache"
    return sorted(root.rglob("plugin.json")) if root.exists() else []


def audit_codex_harness(codex_home: Path) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    try:
        version_proc = subprocess.run(
            ["codex", "--strict-config", "--version"], capture_output=True,
            text=True, timeout=20,
        )
        cli_version = (version_proc.stdout or version_proc.stderr).strip()
        if version_proc.returncode:
            findings.append({"severity": "error", "code": "config-invalid",
                             "subject": str(codex_home / "config.toml"),
                             "message": cli_version})
    except (OSError, subprocess.TimeoutExpired) as exc:
        cli_version = "unavailable"
        findings.append({"severity": "error", "code": "cli-unavailable",
                         "subject": "codex", "message": str(exc)})

    config_path = codex_home / "config.toml"
    if config_path.exists():
        try:
            tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            findings.append({"severity": "error", "code": "config-parse",
                             "subject": str(config_path), "message": str(exc)})
    else:
        findings.append({"severity": "error", "code": "config-missing",
                         "subject": str(config_path), "message": "config.toml is missing"})

    skills = codex_skill_catalog(codex_home)
    for skill in skills:
        path = Path(skill["path"])
        name, description = skill["name"], skill["description"]
        missing = [key for key, value in (("name", name), ("description", description)) if not value]
        if missing:
            findings.append({"severity": "error", "code": "skill-frontmatter",
                             "subject": str(path),
                             "message": f"missing frontmatter field(s): {', '.join(missing)}"})

    plugins: list[dict[str, str]] = []
    for path in _plugin_manifests(codex_home):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            plugins.append({"name": str(payload.get("name", "")), "path": str(path)})
        except (OSError, json.JSONDecodeError) as exc:
            findings.append({"severity": "error", "code": "plugin-manifest",
                             "subject": str(path), "message": str(exc)})

    hook_count = 0
    hooks_path = codex_home / "hooks.json"
    if hooks_path.exists():
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            hook_count = sum(len(v) for v in hooks.get("hooks", {}).values())
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            findings.append({"severity": "error", "code": "hooks-parse",
                             "subject": str(hooks_path), "message": str(exc)})

    doctor_summary: dict[str, Any] = {}
    try:
        doctor_proc = subprocess.run(
            ["codex", "doctor", "--json"], capture_output=True, text=True, timeout=60,
        )
        doctor = json.loads(doctor_proc.stdout) if doctor_proc.stdout.strip() else {}
        checks_raw = doctor.get("checks", {}) if isinstance(doctor, dict) else {}
        checks = list(checks_raw.values()) if isinstance(checks_raw, dict) else list(checks_raw)
        doctor_summary = {
            "overall_status": doctor.get("overallStatus", "unknown"),
            "checks": len(checks),
            "failed": sum(c.get("status") == "fail" for c in checks),
            "warnings": sum(c.get("status") == "warning" for c in checks),
        }
        for check in checks:
            status = check.get("status")
            if status not in {"fail", "warning"}:
                continue
            findings.append({
                "severity": "error" if status == "fail" else "warning",
                "code": f"doctor-{check.get('id', 'unknown')}",
                "subject": "codex doctor",
                "message": str(check.get("summary", "diagnostic check failed")),
            })
        if doctor_proc.returncode and not any(c.get("status") == "fail" for c in checks):
            findings.append({"severity": "error", "code": "doctor-failed",
                             "subject": "codex doctor",
                             "message": doctor_proc.stderr.strip()[:500] or "non-zero exit"})
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        findings.append({"severity": "error", "code": "doctor-unavailable",
                         "subject": "codex doctor", "message": str(exc)})

    return {
        "cli_version": cli_version,
        "codex_home": str(codex_home),
        "skills": skills,
        "unique_names": len({s["name"] for s in skills if s["name"]}),
        "plugins": plugins,
        "hook_count": hook_count,
        "doctor": doctor_summary,
        "findings": findings,
        "errors": sum(f["severity"] == "error" for f in findings),
    }
