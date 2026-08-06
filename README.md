# Inkblot

> An LLM eval harness for all your harnesses.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![LLM judges](https://img.shields.io/badge/LLM_judges-0-red)

An eval CLI for Codex and Claude agent harnesses. It measures whether skills,
rules, hooks, and durable instructions actually change behaviour. The original
Claude suite targets the `~/.claude` configuration made of `CLAUDE.md` rules,
`rules/*.md`, skills, commands, and hooks.

Every harness artifact is an implicit claim: *this text changes what the model
does*. Most of the time nobody checks. Rules accumulate, models change under
them, and a file that once mattered quietly becomes decoration that still costs
context on every single turn.

This repo turns each claim into a measurement, and each measurement into one of
three actions:

| verdict | meaning | action |
|---|---|---|
| `load-bearing` | the artifact changes behaviour, significantly | **KEEP** |
| `no-op` | the model already behaves this way without it | **DROP**, or **CONVERT** to a deterministic mechanism (a setting, a hook, a script) |
| `ineffective` | neither arm reaches the behaviour — the rule asks for something it does not get | **REWRITE**, or enforce it mechanically |
| `inconclusive` | an effect may exist but the run was underpowered | **RERUN** at higher n |

`ineffective` is kept separate from `no-op` on purpose. Both have equal arms, but
"the model already does this without the rule" is simply false when the treatment
arm scored 0/6, and a report that says it would send you to delete a rule whose
real problem is that nobody is following it.

`CONVERT` is the interesting one. Prose in a system prompt is a probabilistic
request; a `settings.json` field or a `PreToolUse` hook is an enforcement. When
a rule turns out to be a no-op *and* a deterministic equivalent exists, the rule
should become that mechanism instead of being argued about in prose at the top
of every context window.

## Install

```bash
python -m pip install -e .          # Python 3.12+, zero runtime dependencies
inkblot --version
```

Trials shell out to the agent CLI under test — `claude` for the Claude suite,
`codex` for the Codex suite — so the one you are measuring must be installed
and authenticated. Behavioural runs are real API calls; the static layers
(`inkblot lint`, `inkblot audit`, `inkblot drift`, unit tests) are free.

This repo was born as `harness-eval` on 2026-08-04 and renamed on 2026-08-06;
older reports and commit messages use the old name for the same code.

## Codex suite

The repository also has a Codex-native suite. It preserves the same trial,
checker, statistics, and report format while using Codex's own isolation
contract:

```bash
inkblot audit
inkblot --suite codex selftest
inkblot skills
inkblot --suite codex run
inkblot report --run results/<run-id>

# Make aliases
make codex-audit       # all installed skills, plugins, hooks, strict config
make codex-selftest    # prove AGENTS.md and .agents/skills treatment isolation
make codex-skills      # activation-check every unique installed skill
make codex-eval        # run enabled cases under cases/codex/
```

Codex trials use `codex exec --ephemeral --ignore-user-config --ignore-rules
--json`, a read-only sandbox, and isolated temporary workspaces. See
`cases/codex/README.md` before adding behavioural skill cases; the static audit
covers every installed skill, but behavioural prompts and deterministic checks
must be written per skill rather than generated from descriptions.
Every Codex trial uses an auth-only temporary `CODEX_HOME`; CLI isolation flags
alone do not suppress globally installed skills.

`harness-eval` remains installed as a compatibility entrypoint, but new usage
should use `inkblot`.

## Two layers

**Static (`make lint`) — free, no API calls.** Some no-ops are provable by
reading files: a hook registered against a script that does not exist, a stub
that exits 0 with no side effects, a `rules/*.md` that `CLAUDE.md` never points
at, a skill directory with no `SKILL.md`. Run this on every change; it costs
nothing.

**Behavioural (`make eval`) — real API calls.** For everything else, run an
ablation: the same probe, N times, with and without the fragment, scored by
deterministic checkers, decided by Fisher's exact test.

## Two measurement modes

**Addition** (`arms.mode = "addition"`, the default) injects the fragment on its
own: treatment = baselines + fragment, control = baselines. It answers *"does
this text work when it is read?"* — a deliberately rule-friendly condition,
because the fragment competes with nothing.

**Excision** (`arms.mode = "excision"`) ships the entire target document in both
arms: treatment gets `target.source` verbatim, control gets it with
`target.excise_lines` cut, in place. It answers the question that actually
decides whether a rule earns its context cost: *"inside the ~13KB document this
line ships in, does it still make a marginal difference?"* An addition-mode
`load-bearing` can evaporate under excision when the line drowns among fifty
neighbours — that is not noise, that is the finding. Excision cases must pin
`target.sha256`; the runner refuses to cut line ranges from a drifted document.

## How the ablation is isolated

```
control    claude --safe-mode -p PROBE [--append-system-prompt-file BASELINE]
treatment  … plus the fragment under test, and/or --settings with the hook under test
```

`--safe-mode` disables every customization — user `CLAUDE.md`, skills, plugins,
hooks, MCP servers, custom commands — while auth, built-in tools, and
permissions keep working. That gives a clean control arm on machines that
authenticate over OAuth, where `--bare` (which demands `ANTHROPIC_API_KEY`) is
not usable.

Hook cases need the other mode. Measured on claude 2.1.221, `--safe-mode` also
suppresses hooks that arrive through `--settings`, so a hook case run that way
returns 0/n in *both* arms and reads as a no-op for entirely the wrong reason.
Those cases set `arms.isolation = "settings-only"`, which uses
`--setting-sources "" --strict-mcp-config` instead; the case loader rejects the
combination that silently lies. The trade-off is that `settings-only` does not
suppress `CLAUDE.md` discovery — both arms carry the live user memory equally,
which is fine for a hook delta and wrong for a rule case.

**The baseline fragment is applied to both arms.** This is not a detail. A rule
like *"docstrings must be English"* only has something to override when a
surrounding *"answer in your language"* pressure is also present. Ablate the pressure
along with the rule and the control arm looks compliant for the wrong reason,
and a load-bearing rule gets deleted.

Verdicts are never rendered by an LLM judge. Every check is a regex, a literal,
a file assertion, or a hook-event assertion, so a run can be re-scored from the
stored transcript years later without spending a token.

## Usage

```bash
make lint                       # static no-op detection over ~/.claude (free)
make drift                      # which cases are stale vs. the live harness (free)
make test                       # unit tests, no API calls
make selftest                   # prove the ablation rig still works (~12 small calls)
make eval                       # every enabled case
make eval CASES=commit-language N=6 MODELS=opus,sonnet
make report                     # re-render the latest run
```

`make selftest` is not optional ceremony. It asserts that the treatment arm
receives its fragment and the control arm does not, and that a hook injected via
`--settings` actually fires. If it fails, every other verdict in this repo is
meaningless — the CLI changed under the rig.

## Writing a case

One case is one falsifiable claim about one artifact. Your case corpus lives
at the top of `cases/` and is deliberately untracked — it describes *your*
harness. The tracked `cases/selftest/` and `cases/codex/selftest/` show the
format in full.

```toml
schema = 1
id = "commit-language"
title = "CLAUDE.md — commit messages must be written in English"

[target]
kind   = "rule"                     # rule | skill | hook | command | meta
source = "~/.claude/CLAUDE.md"
anchor = "Git section — the commit-language rule"
sha256 = "…"                        # the live file when the fragment was extracted

[probe]
prompt = "Write the complete git commit message for …"
models = ["opus", "sonnet"]
n      = 6
tools  = ""                          # "" disables every tool: cheapest and most stable

[arms]
baseline_fragment = "fragments/_baseline-language-pressure.md"  # both arms
fragment          = "fragments/commit-language.md"              # treatment only
# settings        = "arm-hooks.json"                            # for hook cases

[[checks]]
kind    = "regex_absent"
pattern = "[가-힣]"
why     = "the commit message must contain no Hangul"

[verdict]
alpha = 0.05
deterministic_alternative = ""       # set when a settings field or hook could replace the rule
```

Rules for cases that keep the results honest:

- **Fragments are verbatim.** Paraphrasing measures a different string than the
  one the harness ships. Provenance goes above a `---` separator in the fragment
  file; only what follows the separator is injected.
- **Record `sha256` of the live source.** `make drift` then tells you when a
  verdict has gone stale because the underlying rule was edited.
- **Ablate one thing.** Everything the treatment and control arms share belongs
  in `baseline_fragment`.
- **`n >= 3` per arm** or `load-bearing` is unreachable at `alpha = 0.05`, no
  matter how clean the separation (`min_n_for_significance`).

Check kinds: `regex_present`, `regex_absent`, `contains`, `not_contains`,
`hook_fired`, `tool_used`, `tool_not_used`, `file_exists`, `file_contains`.
A trial passes only when every check passes. `tool_used` / `tool_not_used`
match against the streamed `tool_use` blocks (name + JSON input together), so
a case can assert that a pointer or trigger caused an *action*, not just that
the reply text mentioned one.

## Scheduling

`scripts/run-eval.sh` is the cron entry point. It layers on purpose: the free
static pass runs on every fire, the paid behavioural pass only with
`--behavioural`, and the real cases only after `selftest` has passed. A lock
directory keeps overlapping fires from racing each other.

```bash
scripts/run-eval.sh                # lint + drift + unit tests, no API calls
scripts/run-eval.sh --behavioural  # + selftest, then every enabled case
```

## Layout

```
cases/            your case corpus (top-level *.toml — untracked; it describes your harness)
  selftest/       the rig's own checks (tracked)
  codex/selftest/ Codex-suite canaries (tracked)
  fragments/      verbatim excerpts of the harness under test (untracked)
src/harness_eval/ lint (static) · runner (ablation) · stats · report · cli
                   · codex_audit (static inventory) · codex_skills (activation cases)
tests/            unit tests; no API calls, safe to run anywhere
results/          per-run trials, raw transcripts, report.md (untracked)
reports/          your run baselines to compare later runs against (untracked)
scripts/          cron entry (run-eval.sh)
```

The split is deliberate: the repo ships the rig; everything the rig *measures*
— your fragments, case corpus, and findings — stays local and untracked,
because it is a verbatim mirror of your private configuration.

## What it found on a real harness

Numbers from the author's own configuration (claude 2.1.221, 2026-08-04,
1,200+ trials over four surfaces — the detailed reports stay local because
they quote that configuration):

- **Static lint paid for itself before the first API call**: three dead
  registrations — hooks pointing at paths that exist in almost no repo, and
  scripts whose entire body was `exit 0`.
- **11/11 skills were load-bearing** (6/6 vs 0/6 each) when the probe matched
  the skill's own trigger description — measured via `arms.isolation =
  "project"`, the only mode that loads a skill planted in the trial workspace.
- **Isolation flatters rules.** Nine rules measured 6/6 vs 0/6 in addition
  mode; re-measured in excision mode inside the full ~13KB document, four of
  nine verdicts moved down. An isolated 6/6 is a ceiling, not a value.
- **One rule was proven no-op on both models and deleted** — the default
  system prompt was already doing its job, so the paragraph paid context rent
  for nothing.
- **Verdicts split by model.** One rule was no-op on the stronger model
  (which does the behaviour unprompted, 6/6) and load-bearing on the weaker
  one (0/6 without it). A verdict measured on one model does not transfer.
- **One hook had an unreachable enforcement branch** — found because a hook
  case's strict-mode check could not be made to fire, fixed in the live hook.

## Limitations

- Verdicts are model- and version-specific. `report.md` records the CLI version;
  re-run after a model or CLI upgrade rather than trusting an old verdict.
- `--safe-mode` isolation is asserted by `make selftest` against one CLI version.
  A CLI change can silently break it, which is why the selftest is a gate rather
  than a formality.
- GitHub Actions cannot run this: `claude` here authenticates over OAuth, not
  `ANTHROPIC_API_KEY`. Scheduling is local — see `scripts/run-eval.sh`.
- This repo reports and recommends. It never edits `~/.claude`; applying a
  `DROP` or `CONVERT` is a human decision.

## Roadmap

- **Third-party model backends.** A 2026-08-06 spike confirmed the Codex engine
  can drive any OpenAI-compatible *Responses* backend (measured against
  OpenRouter, including a non-OpenAI model), which would let a case corpus
  answer "does my harness rule hold on other models?". Requires a custom
  `model_providers` block, `wire_api = "responses"` (`"chat"` was removed in
  codex 0.146.0), and an explicit `model_reasoning_effort`. Not wired into the
  runner yet.
- **Content-aware drift.** `make drift` currently pins `target.sha256` only; a
  re-pinned document can silently shift what `excise_lines` cuts.

## License

[MIT](LICENSE)
