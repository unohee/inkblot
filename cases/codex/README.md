# Codex eval suite

This directory holds Codex-specific behavioural cases. Run the free installed
harness inventory first, then the two mechanism canaries, and only then real
ablation cases:

```bash
inkblot audit
inkblot --suite codex selftest
inkblot skills
inkblot --suite codex run --cases <case-id> -n 6 \
  --models gpt-5.6-terra,gpt-5.6-sol

# Make aliases
make codex-audit
make codex-selftest
make codex-skills
make codex-eval CASES=<case-id> N=6 MODELS=gpt-5.6-terra,gpt-5.6-sol
```

Codex trials run with `--ephemeral --ignore-user-config --ignore-rules`, a
read-only sandbox, no approvals, and one temporary workspace per arm. Treatment
instructions are written to that workspace's `AGENTS.md`; treatment skills are
copied to `.agents/skills/<name>/SKILL.md`. The prompt materialization directory
stays outside the workspace so the control arm cannot inspect treatment text.
Each trial also receives a temporary `CODEX_HOME` containing only copied auth
state. This is necessary because `--ignore-user-config` does not suppress global
skills; without the temporary home, a control arm can activate the live skill
being measured. The temporary auth copy is removed after the trial and is never
written to results.

`codex-audit` discovers every `SKILL.md` under personal, shared-agent, and
installed-plugin roots. It validates required skill metadata, strict Codex
configuration parsing, plugin manifests, and hooks JSON without printing
credentials or raw configuration values. Static coverage is exhaustive;
behavioural coverage remains intentionally explicit because each skill needs a
goal-matched prompt and a deterministic checker. Do not auto-generate vague
prompts and call that a skill verdict.

`codex-skills` complements that static pass with one generated activation case
per unique skill name. The treatment arm preserves the real `SKILL.md`
frontmatter byte-for-byte and replaces only its temporary copy's body with a
deterministic activation canary; the control arm has no project skill. A clean
split proves Codex can discover and invoke that installed skill metadata.
It does **not** prove the skill's normal workflow quality or natural trigger
rate. Those claims still require hand-authored behavioural cases.

The self-test uses `n = 1` because it validates transport and discovery, not
statistical significance. Real cases should use at least `n = 6` per arm and
should normally compare both `gpt-5.6-terra` and `gpt-5.6-sol`.
